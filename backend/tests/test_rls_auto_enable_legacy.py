"""Real Supabase integration tests for the rls_auto_enable hardening (#291).

Decision: PRESERVE_AND_HARDEN. The legacy hosted function
``public.rls_auto_enable()`` and its event trigger must be preserved during a
legacy upgrade, while ``EXECUTE`` is revoked from ``PUBLIC``, ``anon``,
``authenticated`` and ``service_role``. A clean database must never receive
the function, and re-evaluating the hardening block must be idempotent.

This file is executed only by the database CI job against a freshly reset
local Supabase instance. It must never be collected by the ordinary backend
job (see the ignore list in ``.github/workflows/ci.yml``).

Covers:

1. Legacy drift upgrade: starting from the migrations previous to the new
   hardening migration, a safe fixture recreates the hosted drift
   (function + event trigger + the four EXECUTE grants), the new migration is
   applied, and the catalogs prove the function and event trigger were
   preserved, the definition was not replaced, the four runtime grantees lost
   ``EXECUTE`` and no new privilege was granted.
2. Idempotency: re-evaluating the hardening block on the same fixture state
   succeeds, does not recreate grants, does not alter the definition and does
   not duplicate the object.
3. Absence: applying the new migration to a database where the object never
   existed is a no-op that completes normally.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from backend.tests.supabase_db_helpers import (
    query_json,
    query_scalar_bool,
    query_scalar_text,
    query_text_array,
    run_psql,
    run_psql_file,
    run_supabase,
)

# The hardening migration is located by its slug so the fixed-width version
# prefix is derived from the file name: renaming the migration never leaves
# the tests with a stale hard-coded timestamp (see #291).
LEGACY_HIDDEN_SUFFIX = ".legacy-test-hidden"
_HARDEN_FILENAME_RE = re.compile(
    r"^\d+_harden_rls_auto_enable\.sql(?:\.(?:tmp|legacy-test-hidden))?$"
)


def _find_harden_migration() -> Path:
    """Locate the hardening migration, tolerating CI/direct hidden states.

    CI renames the file to ``<name>.legacy-test-hidden`` before pytest starts
    and a direct run may leave a ``.tmp`` copy behind; every state must be
    found so the version can always be derived from the real file name.
    """
    matches = [
        p
        for p in Path("supabase/migrations").iterdir()
        if _HARDEN_FILENAME_RE.match(p.name)
    ]
    if not matches:
        raise FileNotFoundError(
            "supabase/migrations/*_harden_rls_auto_enable.sql not found"
        )
    # Prefer the live file; fall back to a leftover hidden state.
    return next((p for p in matches if p.name.endswith(".sql")), matches[0])


def _version_from_filename(name: str) -> str:
    """Extract the fixed-width version prefix from a migration file name."""
    return Path(name).name.split("_", 1)[0]


_HARDEN_MIGRATION = _find_harden_migration()
MIGRATION = str(_HARDEN_MIGRATION).removesuffix(LEGACY_HIDDEN_SUFFIX).removesuffix(".tmp")
MIGRATION_VERSION = _version_from_filename(MIGRATION)
MIGRATION_TMP = f"{MIGRATION}.tmp"

FIXTURE = "supabase/fixtures/legacy_rls_auto_enable_drift.sql"
EVENT_TRIGGER_NAME = "rls_auto_enable_legacy_trigger"

_FUNCTION_IDENTITY_SQL = (
    "SELECT n.nspname, p.proname, p.pronargs, p.prorettype::regtype::text, "
    "p.prosecdef, p.proowner::regrole::text "
    "FROM pg_proc p "
    "JOIN pg_namespace n ON n.oid = p.pronamespace "
    "WHERE n.nspname = 'public' AND p.proname = 'rls_auto_enable' "
    "AND p.pronargs = 0"
)

_EXECUTE_GRANTEES_SQL = (
    "SELECT COALESCE(array_agg("
    "CASE WHEN acl.grantee = 0 THEN 'PUBLIC' "
    "ELSE acl.grantee::regrole::text END ORDER BY 1), '{}'::text[]) "
    "AS exec_grantees "
    "FROM pg_proc p "
    "JOIN pg_namespace n ON n.oid = p.pronamespace "
    "LEFT JOIN LATERAL aclexplode(p.proacl) acl ON TRUE "
    "WHERE n.nspname = 'public' AND p.proname = 'rls_auto_enable' "
    "AND p.pronargs = 0"
)

_MIGRATION_VERSION_SQL = (
    "SELECT EXISTS("
    "SELECT 1 FROM supabase_migrations.schema_migrations "
    "WHERE version = '%s'"
    ") AS result" % MIGRATION_VERSION
)


# ---------------------------------------------------------------------------
# Migration file manipulation (restored in finally, CI-script aware)
# ---------------------------------------------------------------------------


def _hide_new_migration() -> None:
    """Hide the new migration before a legacy baseline reset.

    When the CI ``scripts/hide-migrations-after.sh`` already renamed the file
    to ``<name>.legacy-test-hidden`` it stays as-is; otherwise (direct run)
    the file is moved to ``<name>.tmp``. Both states are restored later.
    """
    if os.path.exists(MIGRATION) and not os.path.exists(MIGRATION_TMP):
        os.rename(MIGRATION, MIGRATION_TMP)


def _restore_new_migration() -> None:
    """Restore the new migration from either hidden state."""
    if os.path.exists(MIGRATION_TMP):
        os.rename(MIGRATION_TMP, MIGRATION)
    elif os.path.exists(MIGRATION + LEGACY_HIDDEN_SUFFIX):
        os.rename(MIGRATION + LEGACY_HIDDEN_SUFFIX, MIGRATION)


# ---------------------------------------------------------------------------
# Shared catalog checks
# ---------------------------------------------------------------------------

_ROLES_WITHOUT_EXECUTE = ("public", "anon", "authenticated", "service_role")


def _assert_execute_revoked_from_runtime_roles() -> None:
    for role in _ROLES_WITHOUT_EXECUTE:
        assert not query_scalar_bool(
            f"SELECT has_function_privilege('{role}', "
            "'public.rls_auto_enable()', 'EXECUTE') AS result",
            "result",
        ), f"{role} still has EXECUTE on public.rls_auto_enable()"


def _function_definition() -> str:
    return query_scalar_text(
        "SELECT pg_get_functiondef(p.oid) AS definition "
        "FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = 'rls_auto_enable' "
        "AND p.pronargs = 0",
        "definition",
    )


def _event_trigger_evtfoid() -> str:
    """Return the evtfoid of the fixture event trigger, or '' when absent."""
    return query_scalar_text(
        "SELECT COALESCE("
        "(SELECT et.evtfoid::text FROM pg_event_trigger et "
        "WHERE et.evtname = '%s'), '') AS evtfoid" % EVENT_TRIGGER_NAME,
        "evtfoid",
    )


def _apply_legacy_drift_fixture() -> None:
    """Reset to the pre-migration baseline and apply the drift fixture."""
    _hide_new_migration()
    try:
        run_supabase("rls_auto_enable_reset", ["db", "reset"])
        run_psql_file(FIXTURE)
    finally:
        _restore_new_migration()


# ---------------------------------------------------------------------------
# SCENARIO 1: legacy drift upgrade (PRESERVE_AND_HARDEN)
# ---------------------------------------------------------------------------


@pytest.mark.database_integration
def test_legacy_drift_hardened():
    """A legacy database with the drift must keep the function and event
    trigger while losing the four runtime EXECUTE grants."""
    _apply_legacy_drift_fixture()

    # ---- Before the upgrade: the drift is fully present ----
    identity_before = query_json(_FUNCTION_IDENTITY_SQL)
    assert len(identity_before) == 1, "fixture function missing before upgrade"
    assert identity_before[0]["proname"] == "rls_auto_enable"
    assert identity_before[0]["nspname"] == "public"
    assert identity_before[0]["pronargs"] == 0
    assert identity_before[0]["prorettype"] == "event_trigger"
    assert identity_before[0]["prosecdef"] is True
    assert identity_before[0]["proowner"] == "postgres"

    definition_before = _function_definition()
    assert "NULL" in definition_before, "fixture definition marker missing"

    exec_grantees_before = query_text_array(_EXECUTE_GRANTEES_SQL, "exec_grantees")
    assert set(exec_grantees_before) == {
        "PUBLIC", "postgres", "anon", "authenticated", "service_role",
    }, "fixture EXECUTE grantees differ from the audited drift"

    trigger_before = _event_trigger_evtfoid()
    assert trigger_before, "fixture event trigger missing before upgrade"

    # ---- Apply the new hardening migration ----
    run_supabase("rls_auto_enable_upgrade", ["migration", "up", "--local"])

    # ---- 1. Migration version registered ----
    assert query_scalar_bool(_MIGRATION_VERSION_SQL, "result"), (
        "harden_rls_auto_enable migration timestamp not registered"
    )

    # ---- 2. Function still exists with identical identity ----
    identity_after = query_json(_FUNCTION_IDENTITY_SQL)
    assert identity_after == identity_before, (
        "function identity (schema, name, args, return type, SECURITY "
        "DEFINER, owner) changed during the upgrade"
    )

    # ---- 3. Definition was NOT replaced by the migration ----
    assert _function_definition() == definition_before, (
        "function definition was replaced during the upgrade"
    )

    # ---- 4. Event trigger still exists and still points to the same function ----
    trigger_after = _event_trigger_evtfoid()
    assert trigger_after == trigger_before, (
        "event trigger was removed or re-targeted during the upgrade"
    )
    assert query_scalar_bool(
        "SELECT EXISTS("
        "SELECT 1 FROM pg_event_trigger et "
        "JOIN pg_proc p ON p.oid = et.evtfoid "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE et.evtname = '%s' "
        "AND n.nspname = 'public' AND p.proname = 'rls_auto_enable' "
        "AND p.pronargs = 0"
        ") AS result" % EVENT_TRIGGER_NAME,
        "result",
    ), "event trigger no longer references public.rls_auto_enable()"

    # ---- 5. The four runtime grantees lost EXECUTE ----
    _assert_execute_revoked_from_runtime_roles()

    # ---- 6. No additional privileges were granted ----
    # The structured ACL (aclexplode) must be a strict subset: the owner
    # entry remains, every drift grant is gone, nothing new appeared.
    exec_grantees_after = query_text_array(_EXECUTE_GRANTEES_SQL, "exec_grantees")
    assert set(exec_grantees_after) == {"postgres"}, (
        f"unexpected EXECUTE grantees after upgrade: {exec_grantees_after}"
    )
    assert set(exec_grantees_after) <= set(exec_grantees_before), (
        "migration granted a new EXECUTE privilege"
    )

    # ---- 7. No duplicate objects were created ----
    # (the idempotent re-run below re-verifies the object count stays at 1)

    # ---- 8. Idempotency: re-evaluating the hardening block is a no-op ----
    with open(MIGRATION, encoding="utf-8") as f:
        hardening_sql = f.read()
    run_psql(hardening_sql)

    assert query_scalar_bool(
        "SELECT ("
        "SELECT count(*) FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = 'rls_auto_enable' "
        "AND p.pronargs = 0"
        ") = 1 AS result",
        "result",
    ), "idempotent re-run created or removed the function"
    assert _function_definition() == definition_before, (
        "idempotent re-run altered the function definition"
    )
    assert _event_trigger_evtfoid() == trigger_before, (
        "idempotent re-run altered the event trigger"
    )
    _assert_execute_revoked_from_runtime_roles()
    assert query_text_array(_EXECUTE_GRANTEES_SQL, "exec_grantees") == [
        "postgres"
    ], "idempotent re-run recreated grants"


# ---------------------------------------------------------------------------
# SCENARIO 2: object absence is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.database_integration
def test_absence_is_noop():
    """Applying the hardening migration to a database without the legacy
    object must complete normally and must not create the function or any
    event trigger."""
    # Build the legacy baseline (migrations previous to the new one)
    # WITHOUT applying the drift fixture: the object never exists.
    _hide_new_migration()
    try:
        run_supabase("rls_auto_enable_reset", ["db", "reset"])
    finally:
        _restore_new_migration()

    # Object absent before the upgrade
    assert not query_scalar_bool(
        "SELECT EXISTS("
        "SELECT 1 FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = 'rls_auto_enable'"
        ") AS result",
        "result",
    ), "clean baseline unexpectedly contains rls_auto_enable"

    # Applying the migration must succeed
    run_supabase("rls_auto_enable_upgrade", ["migration", "up", "--local"])

    # Migration registered, object still absent, no artificial event trigger
    assert query_scalar_bool(_MIGRATION_VERSION_SQL, "result"), (
        "harden_rls_auto_enable migration timestamp not registered"
    )
    assert not query_scalar_bool(
        "SELECT EXISTS("
        "SELECT 1 FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = 'rls_auto_enable'"
        ") AS result",
        "result",
    ), "clean database received an artificial rls_auto_enable function"

    assert not query_scalar_bool(
        "SELECT EXISTS("
        "SELECT 1 FROM pg_event_trigger et "
        "JOIN pg_proc p ON p.oid = et.evtfoid "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = 'rls_auto_enable'"
        ") AS result",
        "result",
    ), "clean database received an artificial event trigger"
