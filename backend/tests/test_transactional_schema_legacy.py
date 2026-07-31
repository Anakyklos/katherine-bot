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

import json
import os
import subprocess

import pytest

MIGRATION = "supabase/migrations/20240101000004_transactional_turn_schema.sql"
MIGRATION_TMP = "supabase/migrations/20240101000004_transactional_turn_schema.sql.tmp"

_MIGRATION_VERSION_SQL = (
    "SELECT EXISTS("
    "SELECT 1 FROM supabase_migrations.schema_migrations "
    "WHERE version = '20240101000004'"
    ") AS result"
)


# ---------------------------------------------------------------------------
# CLI helpers (sanitized; never echo secrets or raw output)
# ---------------------------------------------------------------------------


def _run_cli(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["supabase", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError("Supabase operation failed")
    return result


def _run_psql(sql: str) -> None:
    """Execute a SQL script through the local Supabase DB container."""
    result = subprocess.run(
        [
            "docker", "exec", "-i", "supabase_db_app",
            "psql", "-U", "postgres",
            "-v", "ON_ERROR_STOP=1", "-q", "-f", "-",
        ],
        input=sql,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError("psql execution failed")


def _query_scalar_bool(query: str, expected_key: str) -> bool:
    res = _run_cli(["db", "query", "--agent=no", "--output", "json", query])
    data = json.loads(res.stdout)
    assert isinstance(data, list) and len(data) == 1, "expected one row"
    assert expected_key in data[0], "missing expected key"
    value = data[0][expected_key]
    assert isinstance(value, bool), "expected boolean"
    return value


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
    with open("supabase/fixtures/legacy_upgrade_valid.sql", encoding="utf-8") as f:
        _run_psql(f.read())


@pytest.fixture(scope="module")
def supabase_service_client():
    """Service-role client for querying state after migration."""
    from supabase import create_client

    url = os.environ.get("SUPABASE_URL", "http://127.0.0.1:54321")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        result = _run_cli(["status", "-o", "env"], check=False)
        if result.returncode != 0:
            pytest.skip("Could not extract service role key")
        for line in result.stdout.splitlines():
            if line.startswith("SERVICE_ROLE_KEY="):
                key = line.split("=", 1)[1].strip('"')
                break
        if not key:
            pytest.skip("SERVICE_ROLE_KEY not found")
    return create_client(url, key)


# ---------------------------------------------------------------------------
# SCENARIO 1: valid legacy upgrade (migration applied over existing data)
# ---------------------------------------------------------------------------


@pytest.mark.database_integration
def test_valid_legacy_upgrade(supabase_service_client):
    _move_migration_aside()
    try:
        # Reset applies baseline + hardening + admission only (04 hidden).
        _run_cli(["db", "reset"])

        # Seed legacy data before the transactional schema exists.
        _run_legacy_fixture()

        # Restore and apply the transactional schema migration.
        _restore_migration()
        _run_cli(["migration", "up", "--local"])

        # ---- Migration version registered ----
        assert _query_scalar_bool(_MIGRATION_VERSION_SQL, "result"), (
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
            assert _query_scalar_bool(
                "SELECT EXISTS("
                "SELECT 1 FROM pg_class WHERE oid = '{tbl}'::regclass "
                "AND relrowsecurity = true"
                ") AS result".format(tbl=tbl),
                "result",
            ), f"RLS not enabled for {tbl}"
            assert _query_scalar_bool(
                "SELECT EXISTS("
                "SELECT 1 FROM pg_class WHERE oid = '{tbl}'::regclass "
                "AND relforcerowsecurity = true"
                ") AS result".format(tbl=tbl),
                "result",
            ), f"FORCE RLS not enabled for {tbl}"

        # ---- service_role grants on both tables ----
        for tbl in ("turn_requests", "outbox_events"):
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                assert _query_scalar_bool(
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
                assert not _query_scalar_bool(
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
    _run_cli(["db", "reset"])

    # Seed legacy data that must survive the rollback untouched.
    _run_legacy_fixture()

    # Objects exist before rollback.
    for tbl in ("turn_requests", "outbox_events"):
        assert _query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM pg_class WHERE oid = '{tbl}'::regclass"
            ") AS result".format(tbl=tbl),
            "result",
        ), f"{tbl} missing before rollback"
    assert _query_scalar_bool(
        "SELECT EXISTS("
        "SELECT 1 FROM pg_attribute "
        "WHERE attrelid = 'profiles'::regclass AND attname = 'revision'"
        ") AS result",
        "result",
    ), "profiles.revision missing before rollback"

    # Execute the documented operational rollback (additive-only migration).
    _run_psql(
        "DROP TABLE IF EXISTS public.outbox_events;\n"
        "DROP TABLE IF EXISTS public.turn_requests;\n"
        "ALTER TABLE public.profiles DROP COLUMN IF EXISTS revision;\n"
    )

    # Objects are gone after rollback.
    for tbl in ("turn_requests", "outbox_events"):
        assert not _query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM pg_class WHERE oid = '{tbl}'::regclass"
            ") AS result".format(tbl=tbl),
            "result",
        ), f"{tbl} still exists after rollback"
    assert not _query_scalar_bool(
        "SELECT EXISTS("
        "SELECT 1 FROM pg_attribute "
        "WHERE attrelid = 'profiles'::regclass AND attname = 'revision'"
        ") AS result",
        "result",
    ), "profiles.revision still exists after rollback"

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
