"""Real Supabase integration tests for the rls_auto_enable hardening (#291).

Decision: PRESERVE + VERSION + HARDEN. The hosted mechanism (function
``public.rls_auto_enable()`` + event trigger ``ensure_rls``) is real and
active: it auto-enables ROW LEVEL SECURITY on new tables in the public
schema. The hosted body used dynamic SQL and a ``WHEN OTHERS`` handler that
swallowed failures, and EXECUTE was granted to ``PUBLIC``, ``anon``,
``authenticated`` and ``service_role``.

This suite proves the migration converges both a legacy drift database and a
clean database to the SAME canonical versioned state: the canonical function
(SECURITY DEFINER, owner postgres, ``search_path = pg_catalog``, zero
arguments, returns ``event_trigger``, fail-closed body) plus the canonical
``ensure_rls`` event trigger, with EXECUTE revoked from the four runtime
grantees. The legacy body is replaced, not preserved byte for byte.

This file is executed only by the database CI job against a freshly reset
local Supabase instance. It must never be collected by the ordinary backend
job (see the ignore list in ``.github/workflows/ci.yml``).

Covers:

1. Legacy drift upgrade: starting from the migrations previous to the new
   hardening migration, a safe fixture recreates the confirmed hosted drift
   (real function + ``ensure_rls`` trigger + the four EXECUTE grants + a
   problematic body), the new migration is applied, and the catalogs prove
   the function converged to the canonical definition, the trigger is the
   canonical ``ensure_rls`` configuration, the four runtime grantees lost
   ``EXECUTE`` and no new privilege was granted.
2. Idempotency: re-evaluating the migration on the same fixture state
   succeeds, does not recreate grants, does not alter the definition, does
   not duplicate the function or event trigger and keeps the canonical state.
3. Clean convergence: applying the migration to a database where the object
   never existed creates the canonical function and ``ensure_rls`` trigger
   and revokes the runtime EXECUTE grants.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from backend.tests.supabase_db_helpers import (
    query_json,
    query_scalar_bool,
    query_scalar_int,
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
EVENT_TRIGGER_NAME = "ensure_rls"
CANONICAL_TAGS = {"CREATE TABLE", "CREATE TABLE AS", "SELECT INTO"}

_FUNCTION_IDENTITY_SQL = (
    "SELECT n.nspname, p.proname, p.pronargs, p.prorettype::regtype::text, "
    "p.prosecdef, p.proowner::regrole::text, p.proconfig "
    "FROM pg_proc p "
    "JOIN pg_namespace n ON n.oid = p.pronamespace "
    "WHERE n.nspname = 'public' AND p.proname = 'rls_auto_enable' "
    "AND p.pronargs = 0"
)

_FUNCTION_COUNT_SQL = (
    "SELECT count(*)::int AS count "
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


def _assert_owner_keeps_execute() -> None:
    assert query_scalar_bool(
        "SELECT has_function_privilege('postgres', "
        "'public.rls_auto_enable()', 'EXECUTE') AS result",
        "result",
    ), "owner postgres lost EXECUTE on public.rls_auto_enable()"


def _function_definition() -> str:
    return query_scalar_text(
        "SELECT pg_get_functiondef(p.oid) AS definition "
        "FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = 'rls_auto_enable' "
        "AND p.pronargs = 0",
        "definition",
    )


def _event_trigger_state() -> dict:
    """Return the ensure_rls event trigger state, or None when absent."""
    data = query_json(
        "SELECT et.evtname, et.evtevent, et.evtfoid::text AS evtfoid, "
        "et.evtenabled, et.evttags "
        "FROM pg_event_trigger et WHERE et.evtname = '%s'" % EVENT_TRIGGER_NAME
    )
    if not data:
        return None
    assert len(data) == 1, "unexpected ensure_rls trigger rows"
    return data[0]


def _function_oid() -> str:
    return query_scalar_text(
        "SELECT p.oid::text AS oid "
        "FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = 'rls_auto_enable' "
        "AND p.pronargs = 0",
        "oid",
    )


def _trigger_count_for_function() -> int:
    return query_scalar_int(
        "SELECT count(*)::int AS count "
        "FROM pg_event_trigger et "
        "JOIN pg_proc p ON p.oid = et.evtfoid "
        "JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'public' AND p.proname = 'rls_auto_enable' "
        "AND p.pronargs = 0",
        "count",
    )


def _assert_canonical_state(function_oid: str, definition: str) -> None:
    """Assert the converged canonical state (function, trigger and ACL)."""
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
    ), "ensure_rls no longer references public.rls_auto_enable()"

    trigger = _event_trigger_state()
    assert trigger is not None, "ensure_rls missing in canonical state"
    assert trigger["evtevent"] == "ddl_command_end", (
        f"unexpected ensure_rls event: {trigger['evtevent']}"
    )
    assert trigger["evtfoid"] == function_oid, (
        "ensure_rls points to a different function"
    )
    assert trigger["evtenabled"] == "O", (
        f"ensure_rls not enabled: {trigger['evtenabled']}"
    )
    assert set(trigger["evttags"]) == CANONICAL_TAGS, (
        f"unexpected ensure_rls tags: {trigger['evttags']}"
    )
    assert _trigger_count_for_function() == 1, (
        "more than one event trigger points at rls_auto_enable"
    )

    assert "ENABLE ROW LEVEL SECURITY" in definition, (
        "canonical body no longer enables RLS"
    )
    assert "WHEN OTHERS" not in definition, (
        "canonical body still swallows failures with WHEN OTHERS"
    )

    _assert_execute_revoked_from_runtime_roles()
    _assert_owner_keeps_execute()
    assert query_text_array(_EXECUTE_GRANTEES_SQL, "exec_grantees") == [
        "postgres"
    ], "unexpected EXECUTE grantees in canonical state"


def _apply_legacy_drift_fixture() -> None:
    """Reset to the pre-migration baseline and apply the drift fixture."""
    _hide_new_migration()
    try:
        run_supabase("rls_auto_enable_reset", ["db", "reset"])
        run_psql_file(FIXTURE)
    finally:
        _restore_new_migration()


# ---------------------------------------------------------------------------
# SCENARIO 1: legacy drift upgrade converges to the canonical mechanism
# ---------------------------------------------------------------------------


@pytest.mark.database_integration
def test_legacy_drift_hardened():
    """A legacy database with the hosted drift must converge to the canonical
    function and ensure_rls trigger while losing the four runtime EXECUTE
    grants."""
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
    assert "WHEN OTHERS" in definition_before, "fixture drift body marker missing"
    assert "quote_ident" in definition_before, "fixture dynamic SQL marker missing"

    exec_grantees_before = query_text_array(_EXECUTE_GRANTEES_SQL, "exec_grantees")
    assert set(exec_grantees_before) == {
        "PUBLIC", "postgres", "anon", "authenticated", "service_role",
    }, "fixture EXECUTE grantees differ from the confirmed hosted drift"

    trigger_before = _event_trigger_state()
    assert trigger_before is not None, "fixture ensure_rls trigger missing"
    assert trigger_before["evtname"] == EVENT_TRIGGER_NAME
    assert trigger_before["evtevent"] == "ddl_command_end"
    assert set(trigger_before["evttags"]) == CANONICAL_TAGS, (
        "fixture trigger tags differ from the confirmed hosted tags"
    )

    # ---- Apply the new hardening migration ----
    run_supabase("rls_auto_enable_upgrade", ["migration", "up", "--local"])

    # ---- 1. Migration version registered ----
    assert query_scalar_bool(_MIGRATION_VERSION_SQL, "result"), (
        "harden_rls_auto_enable migration timestamp not registered"
    )

    # ---- 2. Function identity preserved (CREATE OR REPLACE semantics) ----
    identity_after = query_json(_FUNCTION_IDENTITY_SQL)
    assert identity_after == identity_before, (
        "function identity (schema, name, args, return type, SECURITY "
        "DEFINER, owner) changed during the upgrade"
    )
    assert identity_after[0]["proconfig"] == ["search_path=pg_catalog"], (
        "function search_path is not exactly pg_catalog"
    )

    # ---- 3. Definition CONVERGED to the canonical fail-closed body ----
    definition_after = _function_definition()
    assert definition_after != definition_before, (
        "legacy body was preserved instead of being converged"
    )
    assert "ENABLE ROW LEVEL SECURITY" in definition_after, (
        "canonical definition no longer enables RLS"
    )
    assert "WHEN OTHERS" not in definition_after, (
        "canonical definition still swallows failures"
    )

    # ---- 4. Canonical event trigger, ACL and no duplicates ----
    function_oid = _function_oid()
    _assert_canonical_state(function_oid, definition_after)

    # ---- 5. No additional privileges were granted ----
    exec_grantees_after = query_text_array(_EXECUTE_GRANTEES_SQL, "exec_grantees")
    assert set(exec_grantees_after) == {"postgres"}, (
        f"unexpected EXECUTE grantees after upgrade: {exec_grantees_after}"
    )
    assert set(exec_grantees_after) <= set(exec_grantees_before), (
        "migration granted a new EXECUTE privilege"
    )

    # ---- 6. Idempotency: re-evaluating the migration is a no-op ----
    with open(MIGRATION, encoding="utf-8") as f:
        migration_sql = f.read()
    run_psql(migration_sql)

    assert query_scalar_int(_FUNCTION_COUNT_SQL, "count") == 1, (
        "idempotent re-run created or removed the function"
    )
    assert _function_definition() == definition_after, (
        "idempotent re-run altered the canonical definition"
    )
    _assert_canonical_state(_function_oid(), definition_after)
    assert query_text_array(_EXECUTE_GRANTEES_SQL, "exec_grantees") == [
        "postgres"
    ], "idempotent re-run recreated grants"


# ---------------------------------------------------------------------------
# SCENARIO 2: clean database receives the canonical mechanism
# ---------------------------------------------------------------------------


@pytest.mark.database_integration
def test_clean_database_converges():
    """Applying the hardening migration to a database where the object never
    existed must create the canonical function and ensure_rls trigger and
    revoke the runtime EXECUTE grants."""
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
    assert _event_trigger_state() is None, (
        "clean baseline unexpectedly contains ensure_rls"
    )

    # Applying the migration must succeed and create the canonical mechanism
    run_supabase("rls_auto_enable_upgrade", ["migration", "up", "--local"])

    assert query_scalar_bool(_MIGRATION_VERSION_SQL, "result"), (
        "harden_rls_auto_enable migration timestamp not registered"
    )

    identity = query_json(_FUNCTION_IDENTITY_SQL)
    assert len(identity) == 1, "canonical function missing after migration"
    assert identity[0]["prosecdef"] is True
    assert identity[0]["proowner"] == "postgres"
    assert identity[0]["prorettype"] == "event_trigger"
    assert identity[0]["proconfig"] == ["search_path=pg_catalog"], (
        "canonical function search_path is not exactly pg_catalog"
    )

    definition = _function_definition()
    assert "ENABLE ROW LEVEL SECURITY" in definition, (
        "canonical body does not enable RLS"
    )
    assert "WHEN OTHERS" not in definition, (
        "canonical body swallows failures with WHEN OTHERS"
    )

    _assert_canonical_state(_function_oid(), definition)
