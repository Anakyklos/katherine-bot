"""Real Supabase integration test: retention migration preflight fail-closed (#316).

This file is executed only by the database CI job. It must never be collected
by the ordinary backend unit job (see the ignore list in
``.github/workflows/ci.yml``).

The preflight of ``operational_data_retention`` must fail with SQLSTATE
23514 when ANY mandatory dependency is missing. Starting from a schema
compatible with the migration (baseline + 00..06 + hardening + privacy),
this suite drops EXACTLY ONE of the three operational tables (every other
dependency still present), applies the migration and requires:

1. the migration fails with 23514;
2. NONE of the new purge functions or the validation helper were installed;
3. the migration is NOT registered;
4. the environment is restored in ``finally`` (migration files and data).
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
_RETENTION_FILENAME_RE = re.compile(
    r"^\d+_operational_data_retention\.sql(?:\.(?:tmp|legacy-test-hidden))?$"
)

# Functions created by the retention migration. None may exist after a
# failed preflight.
MIGRATION_FUNCTIONS = (
    "purge_admission_reservations",
    "purge_privacy_operations",
    "purge_outbox_events",
    "retention_purge_validation_error",
)

# Tables that the retention migration hard-depends on. Dropping exactly one
# must make the migration fail closed.
MISSING_TABLE_CASES = (
    "admission_reservations",
    "privacy_operations",
    "outbox_events",
)


def _find_retention_migration() -> Path:
    matches = [
        p
        for p in Path("supabase/migrations").iterdir()
        if _RETENTION_FILENAME_RE.match(p.name)
    ]
    if not matches:
        raise FileNotFoundError(
            "supabase/migrations/*_operational_data_retention.sql not found"
        )
    return next((p for p in matches if p.name.endswith(".sql")), matches[0])


_RETENTION_MIGRATION = _find_retention_migration()
MIGRATION = str(_RETENTION_MIGRATION).removesuffix(LEGACY_HIDDEN_SUFFIX).removesuffix(".tmp")
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
        "retention_preflight_query",
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
        _run_supabase("retention_preflight_reset", ["db", "reset"])

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
            "retention_preflight_apply", ["migration", "up", "--local"], check=False
        )
        assert res.returncode != 0, (
            f"expected the retention migration to fail when {missing_table} is missing"
        )
        assert "23514" in res.stderr, (
            f"expected SQLSTATE 23514 from preflight, got: {res.stderr[-500:]}"
        )

        # NONE of the new functions may be installed.
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
            "WHERE name = 'operational_data_retention'"
            ") AS result"
        ), "operational_data_retention was registered despite the failed preflight"
    finally:
        _restore_new_migration()
