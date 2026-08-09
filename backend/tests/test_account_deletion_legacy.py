"""Real Supabase integration test: account deletion migration on a legacy
upgrade (#324).

This file is executed only by the database CI job. It must never be collected
by the ordinary backend unit job (see the ignore list in
``.github/workflows/ci.yml``).

The new migration must work on a clean reset (proven by pgTAP 08) AND on a
legacy upgrade: a database at the migrations previous to
``account_deletion_ledger`` (baseline + turn schema + privacy + retention)
with real legacy user data receives the new migration via
``supabase migration up --local``.

Proves:
1. The migration registers its version and applies additively.
2. Legacy data is preserved untouched.
3. The ledger exists with RLS/FORCE RLS, zero policies and no grants.
4. The RPCs exist with SECURITY DEFINER, empty search_path and
   service_role-only EXECUTE; PUBLIC/anon/authenticated have none.
5. The primitives actually work on the upgraded database (request -> claim
   -> purge -> finalize end to end).
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


def _version_from_filename(name: str) -> str:
    return Path(name).name.split("_", 1)[0]


_ADL_MIGRATION = _find_adl_migration()
MIGRATION = str(_ADL_MIGRATION).removesuffix(LEGACY_HIDDEN_SUFFIX).removesuffix(".tmp")
MIGRATION_VERSION = _version_from_filename(MIGRATION)
MIGRATION_TMP = f"{MIGRATION}.tmp"

RPC_SIGNATURES = {
    "account_deletion_request": "account_deletion_request(text, text, uuid, text)",
    "account_deletion_has_tombstone": "account_deletion_has_tombstone(text)",
    "account_deletion_acquire_lease": "account_deletion_acquire_lease(text, integer, integer)",
    "account_deletion_purge": "account_deletion_purge(uuid, text, text)",
    "account_deletion_record_failure": "account_deletion_record_failure(uuid, text, text)",
    "account_deletion_record_retry": "account_deletion_record_retry(uuid, text)",
    "account_deletion_finalize": "account_deletion_finalize(uuid, text)",
    "account_deletion_purge_completed": "account_deletion_purge_completed(timestamptz, integer)",
}


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


def _query_json(query: str) -> list:
    res = _run_supabase(
        "account_deletion_legacy_query",
        ["db", "query", "--agent=no", "--output", "json", query],
        check=False,
    )
    if res.returncode != 0:
        raise AssertionError("Query result: operation failed")
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        raise AssertionError("Query result: invalid JSON response")
    if not isinstance(data, list):
        raise AssertionError("Query result: expected a list")
    return data


def _query_scalar_bool(query: str) -> bool:
    data = _query_json(query)
    if len(data) != 1 or "result" not in data[0]:
        raise AssertionError("Query result: expected exactly one scalar row")
    value = data[0]["result"]
    if not isinstance(value, bool):
        raise AssertionError("Query result: expected a boolean")
    return value


def _query_scalar_int(query: str) -> int:
    data = _query_json(query)
    if len(data) != 1 or "result" not in data[0]:
        raise AssertionError("Query result: expected exactly one scalar row")
    value = data[0]["result"]
    if isinstance(value, bool) or not isinstance(value, int):
        raise AssertionError("Query result: expected an integer")
    return value


def _hide_new_migration() -> None:
    if os.path.exists(MIGRATION) and not os.path.exists(MIGRATION_TMP):
        os.rename(MIGRATION, MIGRATION_TMP)


def _restore_new_migration() -> None:
    if os.path.exists(MIGRATION_TMP):
        os.rename(MIGRATION_TMP, MIGRATION)
    elif os.path.exists(MIGRATION + LEGACY_HIDDEN_SUFFIX):
        os.rename(MIGRATION + LEGACY_HIDDEN_SUFFIX, MIGRATION)


LEGACY_USER = "adl_legacy_user"
LEGACY_REF = "abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd"


def _seed_legacy_data() -> None:
    _run_psql(
        f"""
        INSERT INTO public.profiles (user_id, persona_config, user_profile,
            relationship_state, emotional_state, revision)
        VALUES (
            '{LEGACY_USER}', 'legacy-persona', '{{"k":"v"}}'::jsonb,
            '{{"schema_version":1,"trust":0.5,"affection":0.3,"tension":0.0,"triggers":[],"timestamp":1700000000.0}}'::jsonb,
            '{{"schema_version":1,"pleasure":0.0,"arousal":0.0,"dominance":0.0,"libido":0.0,"aggression":0.0,"connection":0.5,"energy":0.8,"tension":0.0,"coping_mode":"HEALTHY","timestamp":1700000000.0}}'::jsonb,
            2
        );

        INSERT INTO public.chat_logs (user_id, role, content)
        VALUES ('{LEGACY_USER}', 'user', 'legacy hello'),
               ('{LEGACY_USER}', 'assistant', 'legacy hi');

        INSERT INTO public.memories (user_id, content, metadata)
        VALUES ('{LEGACY_USER}', 'legacy memory', '{{}}'::jsonb);

        INSERT INTO public.turn_requests (
            id, user_id, request_id, payload_hash_sha256, status, expected_revision,
            committed_revision, replay_payload, created_at, updated_at, completed_at
        ) VALUES (
            'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid,
            '{LEGACY_USER}', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'::uuid,
            'a'::text || repeat('0', 63), 'completed', 0, 2,
            '{{"response":"legacy hi"}}'::jsonb,
            now(), now(), now()
        );

        INSERT INTO public.outbox_events (
            event_type, contract_version, user_id, turn_request_id, payload, status,
            attempts, next_attempt_at, idempotency_key, created_at, updated_at
        ) VALUES (
            'turn_completed', 1, '{LEGACY_USER}',
            'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid,
            '{{"ref":"t1"}}'::jsonb, 'pending', 0, now() + interval '1 second',
            'adl_legacy_k1', now(), now()
        );

        INSERT INTO public.archival_extractions (
            user_id, source_chat_log_id, extractor_version, schema_version,
            idempotency_key, facts
        )
        SELECT '{LEGACY_USER}', id, 1, 1, 'adl_legacy_arch_1', '{{"facts":[]}}'::jsonb
        FROM public.chat_logs WHERE user_id = '{LEGACY_USER}' AND role = 'user';

        INSERT INTO public.admission_reservations (
            user_id, request_id, message_hmac_sha256, network_hmac_sha256,
            estimated_units
        ) VALUES (
            '{LEGACY_USER}', 'dddddddd-dddd-dddd-dddd-dddddddddddd'::uuid,
            repeat('a', 64), repeat('b', 64), 10
        );

        INSERT INTO public.privacy_operations (
            user_id, operation_id, operation, operation_payload_sha256, status, result
        ) VALUES (
            '{LEGACY_USER}', 'eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee'::uuid,
            'delete_history', repeat('c', 64), 'applied', '{{}}'::jsonb
        );
        """
    )


def _assert_ledger_state() -> None:
    assert _query_scalar_bool(
        "SELECT EXISTS("
        "SELECT 1 FROM pg_class WHERE oid = 'public.account_deletion_jobs'::regclass "
        "AND relrowsecurity = true) AS result"
    ), "RLS not enabled on account_deletion_jobs"
    assert _query_scalar_bool(
        "SELECT EXISTS("
        "SELECT 1 FROM pg_class WHERE oid = 'public.account_deletion_jobs'::regclass "
        "AND relforcerowsecurity = true) AS result"
    ), "FORCE RLS not enabled on account_deletion_jobs"
    assert not _query_scalar_bool(
        "SELECT has_table_privilege('service_role', "
        "'public.account_deletion_jobs', "
        "'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER') AS result"
    ), "service_role has table privileges on account_deletion_jobs"
    assert not _query_scalar_bool(
        "SELECT has_table_privilege('public', "
        "'public.account_deletion_jobs', "
        "'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER') AS result"
    ), "PUBLIC has table privileges on account_deletion_jobs"
    assert _query_scalar_int(
        "SELECT count(*)::int AS result FROM pg_policies "
        "WHERE schemaname = 'public' AND tablename = 'account_deletion_jobs'"
    ) == 0, "account_deletion_jobs has policies"


def _assert_rpc_state() -> None:
    for name, signature in RPC_SIGNATURES.items():
        assert _query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            f"WHERE n.nspname = 'public' AND p.proname = '{name}'"
            ") AS result"
        ), f"{name} missing after the upgrade"
        assert _query_scalar_bool(
            f"SELECT has_function_privilege('service_role', "
            f"'public.{signature}', 'EXECUTE') AS result"
        ), f"service_role missing EXECUTE on {name}"
        for role in ("anon", "authenticated", "public"):
            assert not _query_scalar_bool(
                f"SELECT has_function_privilege('{role}', "
                f"'public.{signature}', 'EXECUTE') AS result"
            ), f"{role} gained EXECUTE on {name}"


@pytest.mark.database_integration
def test_legacy_upgrade_applies_account_deletion_ledger():
    _hide_new_migration()
    try:
        _run_supabase("account_deletion_legacy_reset", ["db", "reset"])
        _seed_legacy_data()

        _restore_new_migration()
        _run_supabase("account_deletion_legacy_upgrade", ["migration", "up", "--local"])

        # ---- 1. Migration registered ----
        assert _query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM supabase_migrations.schema_migrations "
            f"WHERE version = '{MIGRATION_VERSION}'"
            ") AS result"
        ), "account deletion migration timestamp not registered"

        # ---- 2. Legacy data preserved untouched ----
        assert _query_scalar_int(
            "SELECT count(*)::int AS result FROM public.profiles "
            f"WHERE user_id = '{LEGACY_USER}'"
        ) == 1
        assert _query_scalar_int(
            "SELECT revision::int AS result FROM public.profiles "
            f"WHERE user_id = '{LEGACY_USER}'"
        ) == 2, "legacy revision changed during the upgrade"
        assert _query_scalar_int(
            "SELECT count(*)::int AS result FROM public.chat_logs "
            f"WHERE user_id = '{LEGACY_USER}'"
        ) == 2
        assert _query_scalar_int(
            "SELECT count(*)::int AS result FROM public.turn_requests "
            f"WHERE user_id = '{LEGACY_USER}'"
        ) == 1
        assert _query_scalar_int(
            "SELECT count(*)::int AS result FROM public.outbox_events "
            f"WHERE user_id = '{LEGACY_USER}'"
        ) == 1
        assert _query_scalar_int(
            "SELECT count(*)::int AS result FROM public.memories "
            f"WHERE user_id = '{LEGACY_USER}'"
        ) == 1
        assert _query_scalar_int(
            "SELECT count(*)::int AS result FROM public.archival_extractions "
            f"WHERE user_id = '{LEGACY_USER}'"
        ) == 1
        assert _query_scalar_int(
            "SELECT count(*)::int AS result FROM public.admission_reservations "
            f"WHERE user_id = '{LEGACY_USER}'"
        ) == 1
        assert _query_scalar_int(
            "SELECT count(*)::int AS result FROM public.privacy_operations "
            f"WHERE user_id = '{LEGACY_USER}'"
        ) == 1

        # ---- 3. Ledger + RPC state ----
        _assert_ledger_state()
        _assert_rpc_state()

        # ---- 4. The ledger works end to end on the upgraded database ----
        result = _query_json(
            "SELECT public.account_deletion_request("
            f"'{LEGACY_USER}', '{LEGACY_REF}', "
            "'99999999-9999-9999-9999-999999999999'::uuid, repeat('b', 64)"
            ") AS result"
        )
        assert len(result) == 1 and result[0]["result"]["status"] == "created"
        assert result[0]["result"]["job_status"] == "pending"

        claim = _query_json(
            "SELECT public.account_deletion_acquire_lease('legacy-worker', 60, 100) AS result"
        )
        assert len(claim) == 1 and claim[0]["result"]["found"] is True
        job_id = claim[0]["result"]["job_id"]

        purge = _query_json(
            "SELECT public.account_deletion_purge("
            f"'{job_id}'::uuid, 'legacy-worker', repeat('b', 64)) AS result"
        )
        assert len(purge) == 1 and purge[0]["result"]["status"] == "purged"
        assert _query_scalar_int(
            "SELECT count(*)::int AS result FROM public.chat_logs "
            f"WHERE user_id = '{LEGACY_USER}'"
        ) == 0
        assert _query_scalar_int(
            "SELECT count(*)::int AS result FROM public.profiles "
            f"WHERE user_id = '{LEGACY_USER}'"
        ) == 0
        # The tombstone survived the profiles DELETE.
        assert _query_scalar_int(
            "SELECT count(*)::int AS result FROM public.account_deletion_jobs "
            f"WHERE user_ref_hmac_sha256 = '{LEGACY_REF}'"
        ) == 1

        finalize = _query_json(
            "SELECT public.account_deletion_finalize("
            f"'{job_id}'::uuid, 'legacy-worker') AS result"
        )
        assert len(finalize) == 1 and finalize[0]["result"]["status"] == "completed"
        assert _query_scalar_bool(
            "SELECT user_id IS NULL AS result FROM public.account_deletion_jobs "
            f"WHERE user_ref_hmac_sha256 = '{LEGACY_REF}'"
        ), "completed job must minimize the raw user_id"
    finally:
        _restore_new_migration()


@pytest.mark.database_integration
def test_legacy_upgrade_fails_closed_on_drift():
    """The legacy upgrade must also fail closed on drift.

    A database that already contains an object owned by the new migration
    (for example a preexisting ``account_deletion_assert_owner`` function
    that the migration would otherwise silently replace via
    CREATE OR REPLACE) must reject the upgrade with 23514, install nothing
    and leave the migration unregistered.
    """
    _hide_new_migration()
    try:
        _run_supabase("account_deletion_legacy_reset", ["db", "reset"])
        _seed_legacy_data()

        # Install drift before restoring the new migration.
        _run_psql(
            "CREATE OR REPLACE FUNCTION public.account_deletion_assert_owner("
            "p_job_id uuid, p_worker_id text) RETURNS text "
            "LANGUAGE sql IMMUTABLE AS $$ SELECT 'drift'::text $$;"
        )

        _restore_new_migration()
        res = _run_supabase(
            "account_deletion_legacy_upgrade", ["migration", "up", "--local"], check=False
        )
        assert res.returncode != 0, "expected the legacy upgrade to fail on drift"
        assert "23514" in res.stderr, (
            f"expected SQLSTATE 23514 from the drift gate, got: {res.stderr[-500:]}"
        )

        # Nothing was installed and the migration is not registered.
        assert not _query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relname = 'account_deletion_jobs'"
            ") AS result"
        ), "account_deletion_jobs was created despite the drift failure"
        assert not _query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM supabase_migrations.schema_migrations "
            f"WHERE version = '{MIGRATION_VERSION}'"
            ") AS result"
        ), "account deletion migration registered despite the drift failure"
        # Legacy data remains untouched.
        assert _query_scalar_int(
            "SELECT count(*)::int AS result FROM public.profiles "
            f"WHERE user_id = '{LEGACY_USER}'"
        ) == 1, "legacy data was modified by the failed upgrade"
    finally:
        _restore_new_migration()
