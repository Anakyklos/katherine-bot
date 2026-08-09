"""Real Supabase integration test: privacy migration preflight fail-closed (#314 review).

This file is executed only by the database CI job. It must never be collected
by the ordinary backend unit job (see the ignore list in
``.github/workflows/ci.yml``).

The preflight of ``privacy_data_operations`` must fail with SQLSTATE 23514
when ANY mandatory dependency is missing. The original implementation checked
``c.relname IN (...)`` which passes when at least ONE table of the set exists;
this suite proves the fix by starting from a schema compatible with the
migration (baseline + 00..06 + hardening), dropping EXACTLY ONE required table
(every other dependency still present), applying the migration and requiring:

1. the migration fails with 23514;
2. ``privacy_operations`` was NOT created;
3. NONE of the new functions (four RPCs, core, helpers) were installed;
4. the environment is restored in ``finally`` (migration files and data).

Each parametrized case exercises a table that the old ``IN (...)`` check would
have masked: with the other five tables present, the buggy condition would
have passed and let the migration install on a broken schema.
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
_PRIVACY_FILENAME_RE = re.compile(
    r"^\d+_privacy_data_operations\.sql(?:\.(?:tmp|legacy-test-hidden))?$"
)

# The four public RPCs plus every internal core/helper created by the
# migration. None of them may exist after a failed preflight.
MIGRATION_FUNCTIONS = (
    "delete_history",
    "delete_memories",
    "reset_emotional_state",
    "reset_relationship_state",
    "privacy_apply_operation",
    "privacy_operation_payload_sha256",
    "privacy_op_validation_error",
    "privacy_is_neutral_snapshot",
)

# Tables that the old preflight masked with a single IN (...) check.
MISSING_TABLE_CASES = (
    "admission_reservations",
    "turn_requests",
    "archival_extractions",
)


def _find_privacy_migration() -> Path:
    matches = [
        p
        for p in Path("supabase/migrations").iterdir()
        if _PRIVACY_FILENAME_RE.match(p.name)
    ]
    if not matches:
        raise FileNotFoundError(
            "supabase/migrations/*_privacy_data_operations.sql not found"
        )
    return next((p for p in matches if p.name.endswith(".sql")), matches[0])


_PRIVACY_MIGRATION = _find_privacy_migration()
MIGRATION = str(_PRIVACY_MIGRATION).removesuffix(LEGACY_HIDDEN_SUFFIX).removesuffix(".tmp")
MIGRATION_TMP = f"{MIGRATION}.tmp"


# ---------------------------------------------------------------------------
# Sanitized CLI / psql helpers
# ---------------------------------------------------------------------------


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
        "privacy_legacy_query",
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


# ---------------------------------------------------------------------------
# Migration file manipulation (restored in finally, CI-script aware)
# ---------------------------------------------------------------------------


def _hide_new_migration() -> None:
    if os.path.exists(MIGRATION) and not os.path.exists(MIGRATION_TMP):
        os.rename(MIGRATION, MIGRATION_TMP)


def _restore_new_migration() -> None:
    if os.path.exists(MIGRATION_TMP):
        os.rename(MIGRATION_TMP, MIGRATION)
    elif os.path.exists(MIGRATION + LEGACY_HIDDEN_SUFFIX):
        os.rename(MIGRATION + LEGACY_HIDDEN_SUFFIX, MIGRATION)


# ---------------------------------------------------------------------------
# SCENARIO: each mandatory table absence makes the migration fail closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing_table", MISSING_TABLE_CASES)
@pytest.mark.database_integration
def test_preflight_fails_closed_on_missing_table(missing_table: str):
    _hide_new_migration()
    try:
        # Baseline compatible with the migration (00..06 + hardening), with
        # every required table present.
        _run_supabase("privacy_legacy_reset", ["db", "reset"])

        # Remove EXACTLY one mandatory dependency while every other table of
        # the set remains present: this is the case the old IN (...) check
        # masked.
        _run_psql(f"DROP TABLE IF EXISTS public.{missing_table} CASCADE;")
        assert _query_scalar_bool(
            f"SELECT to_regclass('public.{missing_table}') IS NULL AS result"
        ), f"{missing_table} was not actually dropped"

        _restore_new_migration()

        # Applying the migration must FAIL with 23514 BEFORE installing
        # anything.
        res = _run_supabase(
            "privacy_legacy_upgrade", ["migration", "up", "--local"], check=False
        )
        assert res.returncode != 0, (
            f"expected the privacy migration to fail when {missing_table} is missing"
        )
        assert "23514" in res.stderr, (
            f"expected SQLSTATE 23514 from preflight, got: {res.stderr[-500:]}"
        )

        # The ledger must NOT have been created.
        assert not _query_scalar_bool(
            "SELECT to_regclass('public.privacy_operations') IS NOT NULL AS result"
        ), "privacy_operations was created despite the failed preflight"

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
            "WHERE name = 'privacy_data_operations'"
            ") AS result"
        ), "privacy migration was registered despite the failed preflight"
    finally:
        _restore_new_migration()
