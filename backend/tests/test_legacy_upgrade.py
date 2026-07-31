"""Test the legacy-to-hardened upgrade path using real Supabase migrations.

This test verifies that:
1. A baseline-only database can be seeded with valid legacy data and then hardened
   via ``supabase migration up --local``, preserving the legacy data.
2. Invalid legacy data causes the hardening migration to fail without destroying data.

The test manipulates migration files to create these scenarios and always restores
them in ``finally`` blocks.
"""

import json
import os
import logging
import subprocess
import time
import pytest

from backend.supabase_cli import run_supabase_op

logger = logging.getLogger(__name__)


@pytest.fixture(scope="module")
def supabase_service_client():
    """Create a Supabase service-role client for querying state after upgrade."""
    from supabase import create_client

    url = os.environ.get("SUPABASE_URL", "http://127.0.0.1:54321")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        result = run_supabase_op(
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
    return create_client(url, key)


def _run_supabase(op_id: str, args: list[str], check: bool = True):
    """Run a Supabase CLI command via the sanitized helper."""
    result = run_supabase_op(op_id, args, check=False)
    if check and result.returncode != 0:
        raise AssertionError(f"Supabase operation failed: {op_id}")
    return result


def _run_fixture_file(filepath: str):
    """Execute a multi-statement SQL fixture file via Docker exec on the Supabase DB container.

    ``supabase db query --file`` does not support multiple SQL statements.
    This helper runs the file inside the ``supabase_db_app`` container using
    ``docker exec``, which avoids requiring ``psql`` to be installed on the
    host.  The container is created by ``supabase start`` and always uses the
    default credentials (postgres:postgres).
    """
    with open(filepath, "r") as f:
        sql = f.read()
    result = subprocess.run(
        [
            "docker", "exec", "-i", "supabase_db_app",
            "psql", "-U", "postgres",
            "-v", "ON_ERROR_STOP=1",
            "-q",
            "-f", "-",
        ],
        input=sql,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"Fixture execution failed: {filepath} (exited {result.returncode})"
        )


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
    _run_supabase(
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
# JSON-based query helpers (no textual CLI parsing)
# ---------------------------------------------------------------------------


def _parse_json_scalar(
    stdout: str,
    expected_key: str,
    expected_type: type,
    type_name: str,
    *,
    reject_bool: bool = False,
):
    """Parse JSON scalar output from ``supabase db query --output json``.

    Validates that the JSON structure is a list with exactly one dict containing
    exactly the *expected_key* and that the value matches *expected_type*.  On any
    mismatch raises ``AssertionError`` with a constant, sanitized message that never
    includes SQL, stdout, stderr, or sensitive markers.

    When *reject_bool* is True (used for integer queries) Python booleans are
    rejected because ``bool`` is a subtype of ``int`` in Python.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        raise AssertionError("Query result: invalid JSON response")

    if not isinstance(data, list):
        raise AssertionError("Query result: expected a list")
    if len(data) != 1:
        raise AssertionError("Query result: expected exactly one row")
    if not isinstance(data[0], dict):
        raise AssertionError("Query result: expected a JSON object")
    if len(data[0]) != 1:
        raise AssertionError("Query result: unexpected columns")
    if expected_key not in data[0]:
        raise AssertionError("Query result: missing expected key")

    value = data[0][expected_key]

    if reject_bool and isinstance(value, bool):
        raise AssertionError("Query result: expected an integer, got boolean")
    if not isinstance(value, expected_type):
        raise AssertionError(f"Query result: expected a {type_name} value")

    return value


def _query_scalar_bool(query: str, expected_key: str) -> bool:
    """Execute a SQL query returning a single boolean scalar via explicit JSON output.

    Wraps query with ``--agent=no --output json`` to get deterministic
    machine-readable output.  The query must alias its single result column to
    *expected_key*.

    Returns:
        The parsed boolean value.

    Raises:
        AssertionError: On any structural or type mismatch, with a sanitized message.
    """
    res = _run_supabase(
        "legacy_state_query",
        ["db", "query", "--agent=no", "--output", "json", query],
    )
    return _parse_json_scalar(res.stdout, expected_key, bool, "boolean")


def _query_scalar_int(query: str, expected_key: str) -> int:
    """Execute a SQL query returning a single integer scalar via explicit JSON output.

    Wraps query with ``--agent=no --output json`` to get deterministic
    machine-readable output.  The query must alias its single result column to
    *expected_key*.

    Returns:
        The parsed integer value (non-negative).

    Raises:
        AssertionError: On any structural or type mismatch, with a sanitized message.
    """
    res = _run_supabase(
        "legacy_state_query",
        ["db", "query", "--agent=no", "--output", "json", query],
    )
    value = _parse_json_scalar(
        res.stdout, expected_key, int, "integer", reject_bool=True
    )
    if value < 0:
        raise AssertionError("Query result: expected a non-negative integer")
    return value


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
        _run_supabase("legacy_baseline_reset", ["db", "reset"])

        _run_fixture_file("supabase/fixtures/legacy_upgrade_valid.sql")

        _restore_hardening()
        _run_supabase("legacy_hardening_apply", ["migration", "up", "--local"])

        # PostgREST caches the schema and a raw `supabase migration up
        # --local` does not reliably refresh it, so force a reload and wait
        # until the migrated tables are visible through PostgREST (a stale
        # cache would otherwise surface as PGRST205).
        _reload_postgrest_schema()
        _wait_for_postgrest_table(supabase_service_client)

        # ---- Verify migration timestamp ----
        assert _query_scalar_bool(_MIGRATION_VERSION_SQL, "result"), (
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
            assert _query_scalar_bool(
                _TABLE_RLS_SQL.format(tbl=tbl), "result"
            ), f"RLS not enabled for {tbl}"
            assert _query_scalar_bool(
                _TABLE_FORCE_RLS_SQL.format(tbl=tbl), "result"
            ), f"FORCE RLS not enabled for {tbl}"

        # ---- Constraints on chat_logs ----
        assert _query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM pg_constraint "
            "WHERE conname = 'chat_logs_role_check' AND conrelid = 'chat_logs'::regclass"
            ") AS result",
            "result",
        ), "chat_logs_role_check not found"

        assert _query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM pg_constraint "
            "WHERE conname = 'chat_logs_content_check' AND conrelid = 'chat_logs'::regclass"
            ") AS result",
            "result",
        ), "chat_logs_content_check not found"

        # ---- FK on chat_logs ----
        assert _query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM pg_constraint "
            "WHERE conname = 'chat_logs_user_id_fkey' AND conrelid = 'chat_logs'::regclass"
            ") AS result",
            "result",
        ), "chat_logs_user_id_fkey not found"

        # ---- Composite index ----
        assert _query_scalar_bool(
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
                assert _query_scalar_bool(
                    "SELECT EXISTS("
                    "SELECT 1 FROM information_schema.role_table_grants "
                    f"WHERE grantee = 'service_role' "
                    f"AND table_name = '{tbl}' "
                    f"AND privilege_type = '{priv}'"
                    ") AS result",
                    "result",
                ), f"Missing {priv} for service_role on {tbl}"

        # ---- Sequence: service_role USAGE ----
        assert _query_scalar_bool(
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
            assert not _query_scalar_bool(
                "SELECT EXISTS("
                "SELECT 1 FROM information_schema.role_usage_grants "
                f"WHERE grantee = '{role}' AND object_name = 'chat_logs_id_seq'"
                ") AS result",
                "result",
            ), f"Unexpected sequence privileges for {role}"

        # ---- Function: service_role EXECUTE on match_memories ----
        assert _query_scalar_bool(
            "SELECT has_function_privilege('service_role', "
            "'public.match_memories(vector, double precision, integer, text)', "
            "'EXECUTE') AS result",
            "result",
        ), "service_role missing EXECUTE on match_memories"

        # ---- No function EXECUTE for anon / authenticated ----
        for role in ["anon", "authenticated"]:
            assert not _query_scalar_bool(
                f"SELECT has_function_privilege('{role}', "
                "'public.match_memories(vector, double precision, integer, text)', "
                "'EXECUTE') AS result",
                "result",
            ), f"{role} should not have EXECUTE on match_memories"

        # ---- anon / authenticated have no privileges on tables ----
        for role in ["anon", "authenticated"]:
            for tbl in TABLES:
                assert not _query_scalar_bool(
                    "SELECT has_table_privilege("
                    f"'{role}', "
                    f"'public.{tbl}', "
                    "'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'"
                    ") AS result",
                    "result",
                ), f"{role} should have no privileges on {tbl}"

        # ---- PUBLIC has no privileges (effective check via has_*_privilege) ----
        for tbl in TABLES:
            assert not _query_scalar_bool(
                "SELECT has_table_privilege('public', "
                f"'public.{tbl}', "
                "'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER'"
                ") AS result",
                "result",
            ), f"PUBLIC should have no privileges on {tbl}"

        assert not _query_scalar_bool(
            "SELECT has_sequence_privilege('public', "
            "'public.chat_logs_id_seq', "
            "'USAGE, SELECT, UPDATE') AS result",
            "result",
        ), "PUBLIC should have no privileges on chat_logs_id_seq"

        assert not _query_scalar_bool(
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
        _run_supabase("legacy_baseline_reset", ["db", "reset"])

        _run_fixture_file("supabase/fixtures/legacy_upgrade_valid.sql")
        _run_fixture_file("supabase/fixtures/legacy_upgrade_invalid.sql")

        _restore_hardening()

        # Attempt to apply hardening — should fail with SQLSTATE 23514
        res = _run_supabase(
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
        assert not _query_scalar_bool(_MIGRATION_VERSION_SQL, "result"), (
            "Hardening migration was registered despite invalid data"
        )

        # Verify all data preserved via direct SQL (PostgREST not available
        # because the failed migration never applied the service_role grants)
        assert _query_scalar_int(
            "SELECT count(*)::int AS count FROM public.profiles", "count"
        ) == 2, "Expected 2 profiles preserved"
        assert _query_scalar_int(
            "SELECT count(*)::int AS count FROM public.chat_logs", "count"
        ) == 2, "Expected 2 chat logs preserved"

        # Valid data intact
        assert _query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM public.profiles "
            "WHERE user_id = 'legacy_user_valid'"
            ") AS result",
            "result",
        ), "Valid profile was affected"

        assert _query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM public.chat_logs "
            "WHERE user_id = 'legacy_user_valid' "
            "AND content = 'legacy message' "
            "AND role = 'user'"
            ") AS result",
            "result",
        ), "Valid chat log was affected"

        # Invalid data also preserved (not deleted, corrected, or truncated)
        assert _query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM public.profiles "
            "WHERE user_id = 'legacy_user_invalid'"
            ") AS result",
            "result",
        ), "Invalid profile was deleted"

        assert _query_scalar_bool(
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
