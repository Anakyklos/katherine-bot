"""Real Supabase integration test: privacy migration on a legacy upgrade (#314).

This file is executed only by the database CI job. It must never be collected
by the ordinary backend unit job (see the ignore list in
``.github/workflows/ci.yml``).

Scenario 15 of issue #314: the migration must work on a clean reset (proven
by ``supabase/tests/database/06_privacy_data_operations.test.sql`` and every
other database-integration suite) AND on a legacy upgrade: a database at the
migrations previous to ``privacy_data_operations`` (00..06) with real legacy
user data (profile, chat_logs, turn_requests, outbox_events, memories,
archival_extractions, admission_reservations) receives the new migration via
``supabase migration up --local``.

Proves:
1. The migration registers its version and applies additively.
2. Legacy data is preserved untouched (no deletion, no revision change).
3. The privacy_operations ledger exists with RLS/FORCE RLS and no grants.
4. The four RPCs exist with SECURITY DEFINER, fixed search_path, and
   service_role-only EXECUTE; PUBLIC/anon/authenticated have none.
5. The primitives actually work on the upgraded database (delete_history
   applied end to end, preserving memories and admission reservations).
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


def _find_privacy_migration() -> Path:
    """Locate the privacy migration, tolerating CI/direct hidden states."""
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


def _version_from_filename(name: str) -> str:
    return Path(name).name.split("_", 1)[0]


_PRIVACY_MIGRATION = _find_privacy_migration()
MIGRATION = str(_PRIVACY_MIGRATION).removesuffix(LEGACY_HIDDEN_SUFFIX).removesuffix(".tmp")
MIGRATION_VERSION = _version_from_filename(MIGRATION)
MIGRATION_TMP = f"{MIGRATION}.tmp"

RPC_SIGNATURES = {
    "delete_history": "delete_history(text, uuid, jsonb)",
    "delete_memories": "delete_memories(text, uuid, jsonb)",
    "reset_emotional_state": "reset_emotional_state(text, uuid, jsonb)",
    "reset_relationship_state": "reset_relationship_state(text, uuid, jsonb)",
}


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


def _query_json(query: str) -> list:
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
    if os.path.exists(MIGRATION_TMP):
        os.rename(MIGRATION_TMP, MIGRATION)
    elif os.path.exists(MIGRATION + LEGACY_HIDDEN_SUFFIX):
        os.rename(MIGRATION + LEGACY_HIDDEN_SUFFIX, MIGRATION)


# ---------------------------------------------------------------------------
# Legacy fixture (data existing before the privacy migration)
# ---------------------------------------------------------------------------

LEGACY_USER = "privacy_legacy_user"


def _seed_legacy_data() -> None:
    _run_psql(
        f"""
        INSERT INTO public.profiles (user_id, persona_config, user_profile,
            relationship_state, emotional_state, revision)
        VALUES (
            '{LEGACY_USER}', 'legacy-persona', '{{"k":"v"}}'::jsonb,
            '{{"schema_version":1,"trust":0.9,"affection":0.8,"tension":0.1,"triggers":[],"timestamp":1700000000.0}}'::jsonb,
            '{{"schema_version":1,"pleasure":0.9,"arousal":0.8,"dominance":0.7,"libido":0.1,"aggression":0.1,"connection":0.5,"energy":0.8,"tension":0.1,"coping_mode":"MANIC","timestamp":1700000000.0}}'::jsonb,
            2
        );

        INSERT INTO public.chat_logs (user_id, role, content)
        VALUES ('{LEGACY_USER}', 'user', 'legacy hello'),
               ('{LEGACY_USER}', 'assistant', 'legacy hi');

        INSERT INTO public.turn_requests (
            id, user_id, request_id, payload_hash_sha256, status, expected_revision,
            committed_revision, replay_payload, created_at, updated_at, completed_at
        ) VALUES (
            'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid,
            '{LEGACY_USER}', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'::uuid,
            'a'::text || repeat('0', 63), 'completed', 0, 2,
            '{{"response":"legacy hi","message_id":"cccccccc-cccc-cccc-cccc-cccccccccccc"}}'::jsonb,
            now(), now(), now()
        );

        INSERT INTO public.outbox_events (
            event_type, contract_version, user_id, turn_request_id, payload, status,
            attempts, next_attempt_at, idempotency_key, created_at, updated_at
        ) VALUES (
            'turn_completed', 1, '{LEGACY_USER}',
            'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'::uuid,
            '{{"ref":"t1"}}'::jsonb, 'pending', 0, now() + interval '1 second',
            'legacy_k1', now(), now()
        );

        INSERT INTO public.memories (user_id, content, metadata)
        VALUES ('{LEGACY_USER}', 'legacy memory', '{{}}'::jsonb);

        INSERT INTO public.archival_extractions (
            user_id, source_chat_log_id, extractor_version, schema_version,
            idempotency_key, facts
        )
        SELECT '{LEGACY_USER}', id, 1, 1, 'legacy_arch_1', '{{"facts":[]}}'::jsonb
        FROM public.chat_logs WHERE user_id = '{LEGACY_USER}' AND role = 'user';

        INSERT INTO public.admission_reservations (
            user_id, request_id, message_hmac_sha256, network_hmac_sha256,
            estimated_units
        ) VALUES (
            '{LEGACY_USER}', 'dddddddd-dddd-dddd-dddd-dddddddddddd'::uuid,
            repeat('a', 64), repeat('b', 64), 10
        );
        """
    )


# ---------------------------------------------------------------------------
# Catalog assertions after the upgrade
# ---------------------------------------------------------------------------


def _assert_ledger_state() -> None:
    assert _query_scalar_bool(
        "SELECT EXISTS("
        "SELECT 1 FROM pg_class WHERE oid = 'public.privacy_operations'::regclass "
        "AND relrowsecurity = true) AS result"
    ), "RLS not enabled on privacy_operations"
    assert _query_scalar_bool(
        "SELECT EXISTS("
        "SELECT 1 FROM pg_class WHERE oid = 'public.privacy_operations'::regclass "
        "AND relforcerowsecurity = true) AS result"
    ), "FORCE RLS not enabled on privacy_operations"
    assert not _query_scalar_bool(
        "SELECT has_table_privilege('service_role', "
        "'public.privacy_operations', "
        "'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER') AS result"
    ), "service_role has table privileges on privacy_operations"
    assert not _query_scalar_bool(
        "SELECT has_table_privilege('public', "
        "'public.privacy_operations', "
        "'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER') AS result"
    ), "PUBLIC has table privileges on privacy_operations"


def _assert_rpc_state() -> None:
    for name, signature in RPC_SIGNATURES.items():
        assert _query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            f"WHERE n.nspname = 'public' AND p.proname = '{name}' AND p.pronargs = 3"
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


# ---------------------------------------------------------------------------
# SCENARIO: legacy upgrade preserves data and installs the primitives
# ---------------------------------------------------------------------------


@pytest.mark.database_integration
def test_legacy_upgrade_applies_privacy_primitives():
    _hide_new_migration()
    try:
        _run_supabase("privacy_legacy_reset", ["db", "reset"])
        _seed_legacy_data()

        _restore_new_migration()
        _run_supabase("privacy_legacy_upgrade", ["migration", "up", "--local"])

        # ---- 1. Migration registered ----
        assert _query_scalar_bool(
            "SELECT EXISTS("
            "SELECT 1 FROM supabase_migrations.schema_migrations "
            f"WHERE version = '{MIGRATION_VERSION}'"
            ") AS result"
        ), "privacy migration timestamp not registered"

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

        # ---- 3. Ledger + RPC state ----
        _assert_ledger_state()
        _assert_rpc_state()

        # ---- 4. Primitives work end to end on the upgraded database ----
        result = _query_json(
            "SELECT public.delete_history("
            f"'{LEGACY_USER}', '99999999-9999-9999-9999-999999999999'::uuid, '{{}}'::jsonb"
            ") AS result"
        )
        assert len(result) == 1 and result[0]["result"]["status"] == "applied"
        assert result[0]["result"]["revision"] == 3
        assert _query_scalar_int(
            "SELECT count(*)::int AS result FROM public.chat_logs "
            f"WHERE user_id = '{LEGACY_USER}'"
        ) == 0
        assert _query_scalar_int(
            "SELECT count(*)::int AS result FROM public.turn_requests "
            f"WHERE user_id = '{LEGACY_USER}'"
        ) == 0
        assert _query_scalar_int(
            "SELECT count(*)::int AS result FROM public.outbox_events "
            f"WHERE user_id = '{LEGACY_USER}'"
        ) == 0
        assert _query_scalar_int(
            "SELECT count(*)::int AS result FROM public.archival_extractions "
            f"WHERE user_id = '{LEGACY_USER}'"
        ) == 0
        assert _query_scalar_int(
            "SELECT count(*)::int AS result FROM public.memories "
            f"WHERE user_id = '{LEGACY_USER}'"
        ) == 1, "delete_history must preserve memories after the upgrade"
        assert _query_scalar_int(
            "SELECT count(*)::int AS result FROM public.admission_reservations "
            f"WHERE user_id = '{LEGACY_USER}'"
        ) == 1, "delete_history must preserve admission reservations after the upgrade"
    finally:
        _restore_new_migration()
