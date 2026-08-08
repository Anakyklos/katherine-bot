"""Real Supabase integration tests for the transactional schema migration (#270).

This file is executed only by the database CI job against a freshly reset
local Supabase instance. It must never be collected by the ordinary backend
job (see the ignore list in ``.github/workflows/ci.yml``).

Covers:

1. Applying the transactional schema migration over the legacy fixture
   (baseline + hardening + admission) preserves legacy data and backfills
   ``profiles.revision`` to 0.
2. The migration registers its version in ``schema_migrations``.
3. The new server-owned tables exist with RLS/FORCE RLS and the documented
   service_role grants.
4. The documented operational rollback (additive-only migration) can be
   exercised without losing pre-existing data.
"""

from __future__ import annotations

import os

import pytest

from backend.tests.supabase_db_helpers import (
    query_scalar_bool,
    run_psql,
    run_psql_file,
    run_supabase,
)

MIGRATION = "supabase/migrations/20240101000004_transactional_turn_schema.sql"
MIGRATION_TMP = "supabase/migrations/20240101000004_transactional_turn_schema.sql.tmp"

_MIGRATION_VERSION_SQL = (
    "SELECT EXISTS("
    "SELECT 1 FROM supabase_migrations.schema_migrations "
    "WHERE version = '20240101000004'"
    ") AS result"
)


# ---------------------------------------------------------------------------
# Migration file manipulation (always restored in finally)
# ---------------------------------------------------------------------------


def _move_migration_aside() -> None:
    if os.path.exists(MIGRATION) and not os.path.exists(MIGRATION_TMP):
        os.rename(MIGRATION, MIGRATION_TMP)


def _restore_migration() -> None:
    if os.path.exists(MIGRATION_TMP) and not os.path.exists(MIGRATION):
        os.rename(MIGRATION_TMP, MIGRATION)


def _ensure_migration_present() -> None:
    if os.path.exists(MIGRATION_TMP):
        if os.path.exists(MIGRATION):
            os.remove(MIGRATION_TMP)
        else:
            os.rename(MIGRATION_TMP, MIGRATION)


# ---------------------------------------------------------------------------
# Fixture and client helpers
# ---------------------------------------------------------------------------


def _run_legacy_fixture() -> None:
    run_psql_file("supabase/fixtures/legacy_upgrade_valid.sql")


def _close_client(client) -> None:
    """Close every HTTP transport a Supabase sync client may hold.

    supabase-py 2.31.0 does not expose a public close(); the auth transport is
    created eagerly and postgrest/storage/functions lazily. Close only the
    transports that were actually created so no unclosed-socket
    ``ResourceWarning`` is reported while ``filterwarnings = error`` is active.
    """
    if client is None:
        return
    for attr, session_attr in (
        ("_postgrest", "session"),
        ("_storage", "session"),
        ("_functions", "_client"),
    ):
        transport = getattr(client, attr, None)
        if transport is None:
            continue
        session = getattr(transport, session_attr, None)
        if session is not None and hasattr(session, "close"):
            session.close()
    auth = getattr(client, "auth", None)
    if auth is not None and hasattr(auth, "close"):
        auth.close()


@pytest.fixture(scope="module")
def supabase_service_client():
    """Service-role client for querying state after migration."""
    from supabase import create_client

    url = os.environ.get("SUPABASE_URL", "http://127.0.0.1:54321")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        result = run_supabase("legacy_state_query", ["status", "-o", "env"], check=False)
        if result.returncode != 0:
            pytest.skip("Could not extract service role key")
        for line in result.stdout.splitlines():
            if line.startswith("SERVICE_ROLE_KEY="):
                key = line.split("=", 1)[1].strip('"')
                break
        if not key:
            pytest.skip("SERVICE_ROLE_KEY not found")
    client = create_client(url, key)
    yield client
    _close_client(client)


# ---------------------------------------------------------------------------
# SCENARIO 1: valid legacy upgrade (migration applied over existing data)
# ---------------------------------------------------------------------------


@pytest.mark.database_integration
def test_valid_legacy_upgrade(supabase_service_client):
    _move_migration_aside()
    try:
        # Reset applies baseline + hardening + admission only (04 hidden).
        run_supabase("legacy_baseline_reset", ["db", "reset"])

        # Seed legacy data before the transactional schema exists.
        _run_legacy_fixture()

        # Restore and apply the transactional schema migration.
        _restore_migration()
        run_supabase("legacy_hardening_apply", ["migration", "up", "--local"])

        # ---- Migration version registered ----
        assert query_scalar_bool(_MIGRATION_VERSION_SQL, "result"), (
            "Transactional schema migration timestamp not registered"
        )

        # ---- Legacy data preserved with revision backfilled to 0 ----
        svc = supabase_service_client
        profiles_res = svc.table("profiles").select("*").eq(
            "user_id", "legacy_user_valid"
        ).execute()
        assert len(profiles_res.data) == 1
        assert profiles_res.data[0]["user_id"] == "legacy_user_valid"
        assert profiles_res.data[0]["revision"] == 0, (
            "existing profile revision must backfill to 0"
        )

        chat_res = svc.table("chat_logs").select("*").eq(
            "user_id", "legacy_user_valid"
        ).execute()
        assert len(chat_res.data) == 1
        assert chat_res.data[0]["content"] == "legacy message"
        assert chat_res.data[0]["role"] == "user"

        # ---- New tables exist with RLS + FORCE RLS ----
        for tbl in ("turn_requests", "outbox_events"):
            assert query_scalar_bool(
                "SELECT EXISTS("
                "SELECT 1 FROM pg_class WHERE oid = to_regclass('{tbl}') "
                "AND relrowsecurity = true"
                ") AS result".format(tbl=tbl),
                "result",
            ), f"RLS not enabled for {tbl}"
            assert query_scalar_bool(
                "SELECT EXISTS("
                "SELECT 1 FROM pg_class WHERE oid = to_regclass('{tbl}') "
                "AND relforcerowsecurity = true"
                ") AS result".format(tbl=tbl),
                "result",
            ), f"FORCE RLS not enabled for {tbl}"

        # ---- service_role grants on both tables ----
        for tbl in ("turn_requests", "outbox_events"):
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert query_scalar_bool(
                    "SELECT EXISTS("
                    "SELECT 1 FROM information_schema.role_table_grants "
                    "WHERE grantee = 'service_role' "
                    "AND table_name = '{tbl}' "
                    "AND privilege_type = '{priv}'"
                    ") AS result".format(tbl=tbl, priv=priv),
                    "result",
                ), f"Missing {priv} for service_role on {tbl}"

        # ---- anon / authenticated have no privileges ----
        for role in ("anon", "authenticated"):
            for tbl in ("turn_requests", "outbox_events"):
                assert not query_scalar_bool(
                    "SELECT has_table_privilege("
                    f"'{role}', 'public.{tbl}', "
                    "'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'"
                    ") AS result",
                    "result",
                ), f"{role} should have no privileges on {tbl}"

        # ---- New profile insert starts at revision 0 (upsert compatible) ----
        svc.table("profiles").upsert({"user_id": "tx_new_user"}).execute()
        new_res = svc.table("profiles").select("revision").eq(
            "user_id", "tx_new_user"
        ).execute()
        assert len(new_res.data) == 1
        assert new_res.data[0]["revision"] == 0

        # ---- service_role can operate the new tables ----
        req_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        insert_res = svc.table("turn_requests").insert({
            "user_id": "tx_new_user",
            "request_id": req_id,
            "payload_hash_sha256": "a" * 64,
            "status": "pending",
            "lease_owner": "worker-x",
            "lease_expires_at": "2099-01-01T00:00:00Z",
            "expected_revision": 0,
        }).execute()
        assert len(insert_res.data) == 1
        select_res = svc.table("turn_requests").select("status").eq(
            "user_id", "tx_new_user"
        ).execute()
        assert len(select_res.data) == 1
        assert select_res.data[0]["status"] == "pending"
        svc.table("turn_requests").delete().eq("user_id", "tx_new_user").execute()

    finally:
        _ensure_migration_present()


# ---------------------------------------------------------------------------
# SCENARIO 2: operational rollback is exercised (additive-only migration)
# ---------------------------------------------------------------------------


@pytest.mark.database_integration
def test_operational_rollback(supabase_service_client):
    # Fresh reset with the full migration stack (including 04) applied.
    run_supabase("legacy_baseline_reset", ["db", "reset"])

    # Seed legacy data that must survive the rollback untouched.
    _run_legacy_fixture()

    # Objects exist before rollback.
    for tbl in ("turn_requests", "outbox_events"):
        assert query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM pg_class WHERE oid = to_regclass('{tbl}')"
            ") AS result".format(tbl=tbl),
            "result",
        ), f"{tbl} missing before rollback"
    assert query_scalar_bool(
        "SELECT EXISTS("
        "SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('profiles') AND attname = 'revision'"
        ") AS result",
        "result",
    ), "profiles.revision missing before rollback"

    # Execute the documented operational rollback (additive-only migration).
    # Trigger and helpers are dropped in dependency order: the trigger first
    # (it references the function), then the tables (their CHECK constraints
    # reference the payload helpers), then the helpers and the column.
    run_psql(
        "DROP TRIGGER IF EXISTS turn_requests_message_refs_null_trigger "
        "ON public.chat_logs;\n"
        "DROP FUNCTION IF EXISTS public.turn_requests_null_message_refs();\n"
        "DROP TABLE IF EXISTS public.outbox_events;\n"
        "DROP TABLE IF EXISTS public.turn_requests;\n"
        "ALTER TABLE public.profiles DROP COLUMN IF EXISTS revision;\n"
        "DROP FUNCTION IF EXISTS public.jsonb_has_forbidden_key(jsonb, text[]);\n"
        "DROP FUNCTION IF EXISTS public.jsonb_keys_subset_of(jsonb, text[]);\n"
        "DROP FUNCTION IF EXISTS public.jsonb_outbox_payload_value_contract(jsonb);\n"
    )

    # Objects are gone after rollback.
    for tbl in ("turn_requests", "outbox_events"):
        assert not query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM pg_class WHERE oid = to_regclass('{tbl}')"
            ") AS result".format(tbl=tbl),
            "result",
        ), f"{tbl} still exists after rollback"
    assert not query_scalar_bool(
        "SELECT EXISTS("
        "SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass('profiles') AND attname = 'revision'"
        ") AS result",
        "result",
    ), "profiles.revision still exists after rollback"
    assert not query_scalar_bool(
        "SELECT EXISTS("
        "SELECT 1 FROM pg_trigger WHERE tgname = 'turn_requests_message_refs_null_trigger'"
        ") AS result",
        "result",
    ), "chat_logs trigger still exists after rollback"
    assert not query_scalar_bool(
        "SELECT EXISTS("
        "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' "
        "AND p.proname IN ("
        "  'jsonb_has_forbidden_key', 'jsonb_keys_subset_of', "
        "  'jsonb_outbox_payload_value_contract', "
        "  'turn_requests_null_message_refs')"
        ") AS result",
        "result",
    ), "payload helper functions still exist after rollback"

    # Pre-existing data survived the rollback.
    svc = supabase_service_client
    profiles_res = svc.table("profiles").select("*").eq(
        "user_id", "legacy_user_valid"
    ).execute()
    assert len(profiles_res.data) == 1
    chat_res = svc.table("chat_logs").select("*").eq(
        "user_id", "legacy_user_valid"
    ).execute()
    assert len(chat_res.data) == 1
