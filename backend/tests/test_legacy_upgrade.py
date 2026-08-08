"""Test the legacy-to-hardened upgrade path using real Supabase migrations.

This test verifies that:
1. A baseline-only database can be seeded with valid legacy data and then hardened
   via ``supabase migration up --local``, preserving the legacy data.
2. Invalid legacy data causes the hardening migration to fail without destroying data.

The test manipulates migration files to create these scenarios and always restores
them in ``finally`` blocks.
"""

import os
import logging
import subprocess
import time
import pytest

from backend.tests.supabase_db_helpers import (
    query_scalar_bool,
    query_scalar_int,
    run_psql_file,
    run_supabase,
)

logger = logging.getLogger(__name__)


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
    """Create a Supabase service-role client for querying state after upgrade."""
    from supabase import create_client

    url = os.environ.get("SUPABASE_URL", "http://127.0.0.1:54321")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        result = run_supabase(
            "legacy_state_query",
            ["status", "-o", "env"],
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("Could not extract service role key from supabase status")
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
# PostgREST schema cache helpers
# ---------------------------------------------------------------------------


def _reload_postgrest_schema():
    """Force PostgREST to refresh its schema cache.

    ``supabase migration up --local`` applies migrations directly and does
    not reliably refresh PostgREST's cached schema, so queries against the
    migrated tables can fail with PGRST205 ("Could not find the table in the
    schema cache").  PostgREST reloads its schema cache when it receives
    ``NOTIFY pgrst, 'reload schema'``; the reload is processed
    asynchronously, so callers must also wait for the tables to become
    visible again (see ``_wait_for_postgrest_table``).
    """
    run_supabase(
        "legacy_state_query",
        [
            "db", "query", "--agent=no", "--output", "json",
            "select pg_notify('pgrst', 'reload schema')",
        ],
    )


def _restart_postgrest_container():
    """Restart the local PostgREST container to force a fresh schema cache.

    The notification-based reload (``NOTIFY pgrst, 'reload schema'``)
    requires PostgREST to be listening on its reload channel; if it is not
    available, restarting the container guarantees the schema cache is
    rebuilt from the current database state.
    """
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    candidates = [
        name
        for name in result.stdout.splitlines()
        if "supabase" in name and "rest" in name
    ]
    if not candidates:
        raise AssertionError(
            "PostgREST container not found for schema cache restart"
        )
    restarted = subprocess.run(
        ["docker", "restart", candidates[0]],
        capture_output=True,
        text=True,
        check=False,
    )
    if restarted.returncode != 0:
        raise AssertionError(
            "Failed to restart PostgREST container for schema cache"
        )


def _wait_for_postgrest_table(client, table: str, timeout: float = 30.0):
    """Wait until PostgREST exposes *table* in its schema cache.

    PostgREST serves the refreshed schema cache asynchronously after a
    reload; immediately after a raw migration apply the cache can still be
    stale (PGRST205).  Poll a trivial query until it succeeds or *timeout*
    seconds elapse, then fall back to restarting the PostgREST container
    once and poll again.

    Raises:
        AssertionError: If *table* is still not visible after the retries.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            client.table(table).select("user_id").limit(1).execute()
            return
        except Exception:
            time.sleep(1)

    _restart_postgrest_container()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            client.table(table).select("user_id").limit(1).execute()
            return
        except Exception:
            time.sleep(1)
    raise AssertionError(
        f"PostgREST schema cache did not expose table '{table}' after reload"
    )


# ---------------------------------------------------------------------------
# Helpers for moving migration files aside/back
# ---------------------------------------------------------------------------
HARDENING = "supabase/migrations/20240101000002_secure_server_owned_tables.sql"
HARDENING_TMP = "supabase/migrations/20240101000002_secure_server_owned_tables.sql.tmp"


def _move_hardening_aside():
    if os.path.exists(HARDENING) and not os.path.exists(HARDENING_TMP):
        os.rename(HARDENING, HARDENING_TMP)


def _restore_hardening():
    if os.path.exists(HARDENING_TMP) and not os.path.exists(HARDENING):
        os.rename(HARDENING_TMP, HARDENING)


def _ensure_hardening_present():
    if os.path.exists(HARDENING_TMP):
        if os.path.exists(HARDENING):
            os.remove(HARDENING_TMP)
        else:
            os.rename(HARDENING_TMP, HARDENING)


# ---------------------------------------------------------------------------
# Shared query constants
# ---------------------------------------------------------------------------

TABLES = ["profiles", "chat_logs", "memories", "archival_extractions"]

_MIGRATION_VERSION_SQL = (
    "SELECT EXISTS("
    "SELECT 1 FROM supabase_migrations.schema_migrations "
    "WHERE version = '20240101000002'"
    ") AS result"
)

_TABLE_RLS_SQL = (
    "SELECT EXISTS("
    "SELECT 1 FROM pg_class WHERE oid = '{tbl}'::regclass "
    "AND relrowsecurity = true"
    ") AS result"
)

_TABLE_FORCE_RLS_SQL = (
    "SELECT EXISTS("
    "SELECT 1 FROM pg_class WHERE oid = '{tbl}'::regclass "
    "AND relforcerowsecurity = true"
    ") AS result"
)

# ---------------------------------------------------------------------------
# SCENARIO 1: Valid legacy upgrade
# ---------------------------------------------------------------------------


@pytest.mark.database_integration
def test_valid_legacy_upgrade(supabase_service_client):
    _move_hardening_aside()
    try:
        run_supabase("legacy_baseline_reset", ["db", "reset"])

        run_psql_file("supabase/fixtures/legacy_upgrade_valid.sql")

        _restore_hardening()
        run_supabase("legacy_hardening_apply", ["migration", "up", "--local"])

        # PostgREST caches the schema and a raw `supabase migration up
        # --local` does not reliably refresh it, so force a reload and wait
        # until the migrated tables are visible through PostgREST (a stale
        # cache would otherwise surface as PGRST205).
        _reload_postgrest_schema()
        _wait_for_postgrest_table(supabase_service_client, "profiles")

        # ---- Verify migration timestamp ----
        assert query_scalar_bool(_MIGRATION_VERSION_SQL, "result"), (
            "Hardening migration timestamp not registered"
        )

        # ---- Verify legacy data preserved ----
        svc = supabase_service_client

        profiles_res = svc.table("profiles").select("*").eq(
            "user_id", "legacy_user_valid"
        ).execute()
        assert len(profiles_res.data) == 1
        assert profiles_res.data[0]["user_id"] == "legacy_user_valid"

        chat_res = svc.table("chat_logs").select("*").eq(
            "user_id", "legacy_user_valid"
        ).execute()
        assert len(chat_res.data) == 1
        assert chat_res.data[0]["content"] == "legacy message"
        assert chat_res.data[0]["role"] == "user"

        # ---- RLS and FORCE RLS enabled on all 4 tables ----
        for tbl in TABLES:
            assert query_scalar_bool(
                _TABLE_RLS_SQL.format(tbl=tbl), "result"
            ), f"RLS not enabled for {tbl}"
            assert query_scalar_bool(
                _TABLE_FORCE_RLS_SQL.format(tbl=tbl), "result"
            ), f"FORCE RLS not enabled for {tbl}"

        # ---- Constraints on chat_logs ----
        assert query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM pg_constraint "
            "WHERE conname = 'chat_logs_role_check' AND conrelid = 'chat_logs'::regclass"
            ") AS result",
            "result",
        ), "chat_logs_role_check not found"

        assert query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM pg_constraint "
            "WHERE conname = 'chat_logs_content_check' AND conrelid = 'chat_logs'::regclass"
            ") AS result",
            "result",
        ), "chat_logs_content_check not found"

        # ---- FK on chat_logs ----
        assert query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM pg_constraint "
            "WHERE conname = 'chat_logs_user_id_fkey' AND conrelid = 'chat_logs'::regclass"
            ") AS result",
            "result",
        ), "chat_logs_user_id_fkey not found"

        # ---- Composite index ----
        assert query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM pg_indexes "
            "WHERE indexname = 'chat_logs_user_id_created_at_id_idx' "
            "AND tablename = 'chat_logs'"
            ") AS result",
            "result",
        ), "chat_logs_user_id_created_at_id_idx not found"

        # ---- Grants for service_role ----
        for tbl in TABLES:
            for priv in ["SELECT", "INSERT", "UPDATE", "DELETE"]:
                assert query_scalar_bool(
                    "SELECT EXISTS("
                    "SELECT 1 FROM information_schema.role_table_grants "
                    f"WHERE grantee = 'service_role' "
                    f"AND table_name = '{tbl}' "
                    f"AND privilege_type = '{priv}'"
                    ") AS result",
                    "result",
                ), f"Missing {priv} for service_role on {tbl}"

        # ---- Sequence: service_role USAGE ----
        assert query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM information_schema.role_usage_grants "
            "WHERE grantee = 'service_role' "
            "AND object_name = 'chat_logs_id_seq' "
            "AND privilege_type = 'USAGE'"
            ") AS result",
            "result",
        ), "Missing USAGE for service_role on chat_logs_id_seq"

        # ---- No sequence privileges for anon / authenticated ----
        for role in ["anon", "authenticated"]:
            assert not query_scalar_bool(
                "SELECT EXISTS("
                "SELECT 1 FROM information_schema.role_usage_grants "
                f"WHERE grantee = '{role}' AND object_name = 'chat_logs_id_seq'"
                ") AS result",
                "result",
            ), f"Unexpected sequence privileges for {role}"

        # ---- Function: service_role EXECUTE on match_memories ----
        assert query_scalar_bool(
            "SELECT has_function_privilege('service_role', "
            "'public.match_memories(vector, double precision, integer, text)', "
            "'EXECUTE') AS result",
            "result",
        ), "service_role missing EXECUTE on match_memories"

        # ---- No function EXECUTE for anon / authenticated ----
        for role in ["anon", "authenticated"]:
            assert not query_scalar_bool(
                f"SELECT has_function_privilege('{role}', "
                "'public.match_memories(vector, double precision, integer, text)', "
                "'EXECUTE') AS result",
                "result",
            ), f"{role} should not have EXECUTE on match_memories"

        # ---- anon / authenticated have no privileges on tables ----
        for role in ["anon", "authenticated"]:
            for tbl in TABLES:
                assert not query_scalar_bool(
                    "SELECT has_table_privilege("
                    f"'{role}', "
                    f"'public.{tbl}', "
                    "'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'"
                    ") AS result",
                    "result",
                ), f"{role} should have no privileges on {tbl}"

        # ---- PUBLIC has no privileges (effective check via has_*_privilege) ----
        for tbl in TABLES:
            assert not query_scalar_bool(
                "SELECT has_table_privilege('public', "
                f"'public.{tbl}', "
                "'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'"
                ") AS result",
                "result",
            ), f"PUBLIC should have no privileges on {tbl}"

        assert not query_scalar_bool(
            "SELECT has_sequence_privilege('public', "
            "'public.chat_logs_id_seq', "
            "'USAGE, SELECT, UPDATE') AS result",
            "result",
        ), "PUBLIC should have no privileges on chat_logs_id_seq"

        assert not query_scalar_bool(
            "SELECT has_function_privilege('public', "
            "'public.match_memories(vector, double precision, integer, text)', "
            "'EXECUTE') AS result",
            "result",
        ), "PUBLIC should not have EXECUTE on match_memories"

    finally:
        _ensure_hardening_present()


# ---------------------------------------------------------------------------
# SCENARIO 2: Invalid legacy data → non-destructive failure
# ---------------------------------------------------------------------------


@pytest.mark.database_integration
def test_invalid_legacy_rejected():
    _move_hardening_aside()
    try:
        run_supabase("legacy_baseline_reset", ["db", "reset"])

        run_psql_file("supabase/fixtures/legacy_upgrade_valid.sql")
        run_psql_file("supabase/fixtures/legacy_upgrade_invalid.sql")

        _restore_hardening()

        # Attempt to apply hardening — should fail with SQLSTATE 23514
        res = run_supabase(
            "legacy_hardening_apply", ["migration", "up", "--local"], check=False
        )
        assert res.returncode != 0, (
            "Expected hardening migration to fail with invalid data"
        )
        # Verify the failure is specifically the preflight constraint check
        assert "23514" in res.stderr, (
            "Expected SQLSTATE 23514 from preflight validation"
        )

        # Verify hardening migration timestamp NOT registered
        assert not query_scalar_bool(_MIGRATION_VERSION_SQL, "result"), (
            "Hardening migration was registered despite invalid data"
        )

        # Verify all data preserved via direct SQL (PostgREST not available
        # because the failed migration never applied the service_role grants)
        assert query_scalar_int(
            "SELECT count(*)::int AS count FROM public.profiles", "count"
        ) == 2, "Expected 2 profiles preserved"
        assert query_scalar_int(
            "SELECT count(*)::int AS count FROM public.chat_logs", "count"
        ) == 2, "Expected 2 chat logs preserved"

        # Valid data intact
        assert query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM public.profiles "
            "WHERE user_id = 'legacy_user_valid'"
            ") AS result",
            "result",
        ), "Valid profile was affected"

        assert query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM public.chat_logs "
            "WHERE user_id = 'legacy_user_valid' "
            "AND content = 'legacy message' "
            "AND role = 'user'"
            ") AS result",
            "result",
        ), "Valid chat log was affected"

        # Invalid data also preserved (not deleted, corrected, or truncated)
        assert query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM public.profiles "
            "WHERE user_id = 'legacy_user_invalid'"
            ") AS result",
            "result",
        ), "Invalid profile was deleted"

        assert query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM public.chat_logs "
            "WHERE user_id = 'legacy_user_invalid' "
            "AND content = '' "
            "AND role = 'user'"
            ") AS result",
            "result",
        ), "Invalid chat log was deleted or content changed"

    finally:
        _ensure_hardening_present()
