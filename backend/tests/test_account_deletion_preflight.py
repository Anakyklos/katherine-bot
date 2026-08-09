"""Real Supabase integration test: account deletion preflight fail-closed (#324).

This file is executed only by the database CI job. It must never be collected
by the ordinary backend unit job (see the ignore list in
``.github/workflows/ci.yml``).

The preflight of ``account_deletion_ledger`` must fail with SQLSTATE 23514
when ANY mandatory dependency is missing. Starting from a schema compatible
with the migration (baseline + turn schema + privacy + retention), this
suite drops EXACTLY ONE of the required tables (every other dependency still
present), applies the migration and requires:

1. the migration fails with 23514;
2. ``account_deletion_jobs`` was NOT created;
3. NONE of the new functions were installed;
4. the environment is restored in ``finally`` (migration files and data).

Each parametrized case exercises a table that a naive single ``IN (...)``
preflight would have masked.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from backend.supabase_cli import run_supabase_op

LEGACY_HIDDEN_SUFFIX = ".legacy-test-hidden"
_ADL_FILENAME_RE = re.compile(
    r"^\d+_account_deletion_ledger\.sql(?:\.(?:tmp|legacy-test-hidden))?$"
)

# Every function created by the migration. None may exist after a failed
# preflight.
MIGRATION_FUNCTIONS = (
    "account_deletion_request",
    "account_deletion_has_tombstone",
    "account_deletion_acquire_lease",
    "account_deletion_purge",
    "account_deletion_record_failure",
    "account_deletion_record_retry",
    "account_deletion_finalize",
    "account_deletion_purge_completed",
    "account_deletion_validation_error",
    "account_deletion_assert_owner",
    "account_deletion_intent_fingerprint_sha256",
)

# Tables the migration hard-depends on. Dropping exactly one must make the
# migration fail closed.
MISSING_TABLE_CASES = (
    "chat_logs",
    "memories",
    "archival_extractions",
    "turn_requests",
    "outbox_events",
    "admission_reservations",
    "privacy_operations",
    "profiles",
)


def _find_adl_migration() -> Path:
    matches = [
        p
        for p in Path("supabase/migrations").iterdir()
        if _ADL_FILENAME_RE.match(p.name)
    ]
    if not matches:
        raise FileNotFoundError(
            "supabase/migrations/*_account_deletion_ledger.sql not found"
        )
    return next((p for p in matches if p.name.endswith(".sql")), matches[0])


_ADL_MIGRATION = _find_adl_migration()
MIGRATION = str(_ADL_MIGRATION).removesuffix(LEGACY_HIDDEN_SUFFIX).removesuffix(".tmp")
MIGRATION_TMP = f"{MIGRATION}.tmp"


def _run_supabase(op_id: str, args: list[str], check: bool = True):
    result = run_supabase_op(op_id, args, check=False)
    if check and result.returncode != 0:
        raise AssertionError(f"Supabase operation failed: {op_id}")
    return result


def _run_psql(sql: str) -> None:
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


def _query_scalar_bool(query: str) -> bool:
    res = _run_supabase(
        "account_deletion_preflight_query",
        ["db", "query", "--agent=no", "--output", "json", query],
        check=False,
    )
    if res.returncode != 0:
        raise AssertionError("Query result: operation failed")
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        raise AssertionError("Query result: invalid JSON response")
    if not isinstance(data, list) or len(data) != 1 or "result" not in data[0]:
        raise AssertionError("Query result: expected exactly one scalar row")
    value = data[0]["result"]
    if not isinstance(value, bool):
        raise AssertionError("Query result: expected a boolean")
    return value


def _hide_new_migration() -> None:
    if os.path.exists(MIGRATION) and not os.path.exists(MIGRATION_TMP):
        os.rename(MIGRATION, MIGRATION_TMP)


def _restore_new_migration() -> None:
    if os.path.exists(MIGRATION_TMP):
        os.rename(MIGRATION_TMP, MIGRATION)
    elif os.path.exists(MIGRATION + LEGACY_HIDDEN_SUFFIX):
        os.rename(MIGRATION + LEGACY_HIDDEN_SUFFIX, MIGRATION)


@pytest.mark.parametrize("missing_table", MISSING_TABLE_CASES)
@pytest.mark.database_integration
def test_preflight_fails_closed_on_missing_table(missing_table: str):
    _hide_new_migration()
    try:
        # Baseline compatible with the migration: every required table present.
        _run_supabase("account_deletion_preflight_reset", ["db", "reset"])

        # Remove EXACTLY one mandatory dependency while every other
        # dependency of the set remains present.
        _run_psql(f"DROP TABLE IF EXISTS public.{missing_table} CASCADE;")
        assert _query_scalar_bool(
            f"SELECT to_regclass('public.{missing_table}') IS NULL AS result"
        ), f"{missing_table} was not actually dropped"

        _restore_new_migration()

        # Applying the migration must FAIL with 23514 BEFORE installing
        # anything.
        res = _run_supabase(
            "account_deletion_preflight_apply", ["migration", "up", "--local"], check=False
        )
        assert res.returncode != 0, (
            f"expected the account deletion migration to fail when {missing_table} is missing"
        )
        assert "23514" in res.stderr, (
            f"expected SQLSTATE 23514 from preflight, got: {res.stderr[-500:]}"
        )

        # NONE of the new objects may be installed.
        assert not _query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = 'account_deletion_jobs'"
            ") AS result"
        ), "account_deletion_jobs was created despite the failed preflight"
        for fn in MIGRATION_FUNCTIONS:
            assert not _query_scalar_bool(
                "SELECT EXISTS("
                "SELECT 1 FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                f"WHERE n.nspname = 'public' AND p.proname = '{fn}'"
                ") AS result"
            ), f"{fn} was installed despite the failed preflight"

        # The migration must NOT be registered.
        assert not _query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM supabase_migrations.schema_migrations "
            "WHERE name = 'account_deletion_ledger'"
            ") AS result"
        ), "account_deletion_ledger was registered despite the failed preflight"
    finally:
        _restore_new_migration()


@pytest.mark.database_integration
def test_preflight_fails_closed_on_drift_of_own_object():
    """A preexisting object with a name owned by the migration must block it.

    Every object this migration creates (including internal helpers such as
    ``account_deletion_assert_owner``) is part of the drift gate: a
    preexisting function with one of those names must fail the migration
    with 23514 BEFORE anything is installed or replaced.
    """
    _hide_new_migration()
    try:
        _run_supabase("account_deletion_preflight_reset", ["db", "reset"])

        # Install drift: a preexisting function that the migration also
        # creates via CREATE OR REPLACE (would otherwise be silently
        # replaced instead of blocking the upgrade).
        _run_psql(
            "CREATE OR REPLACE FUNCTION public.account_deletion_assert_owner("
            "p_job_id uuid, p_worker_id text) RETURNS text "
            "LANGUAGE sql IMMUTABLE AS $$ SELECT 'drift'::text $$;"
        )
        assert _query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname = 'account_deletion_assert_owner'"
            ") AS result"
        ), "drift function was not actually installed"

        _restore_new_migration()

        res = _run_supabase(
            "account_deletion_preflight_apply", ["migration", "up", "--local"], check=False
        )
        assert res.returncode != 0, (
            "expected the account deletion migration to fail on drift of its own object"
        )
        assert "23514" in res.stderr, (
            f"expected SQLSTATE 23514 from the drift gate, got: {res.stderr[-500:]}"
        )

        # NONE of the new objects may be installed; the migration is not
        # registered. The preexisting drift function itself remains (the
        # migration failed before touching it), so it is excluded from the
        # installed-check.
        assert not _query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = 'account_deletion_jobs'"
            ") AS result"
        ), "account_deletion_jobs was created despite the drift failure"
        for fn in MIGRATION_FUNCTIONS:
            if fn == "account_deletion_assert_owner":
                continue
            assert not _query_scalar_bool(
                "SELECT EXISTS("
                "SELECT 1 FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                f"WHERE n.nspname = 'public' AND p.proname = '{fn}'"
                ") AS result"
            ), f"{fn} was installed despite the drift failure"
        assert not _query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM supabase_migrations.schema_migrations "
            "WHERE name = 'account_deletion_ledger'"
            ") AS result"
        ), "account_deletion_ledger was registered despite the drift failure"
    finally:
        _restore_new_migration()
