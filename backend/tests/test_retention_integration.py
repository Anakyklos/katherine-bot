"""Real Supabase integration tests for the #316 operational retention runner.

This file is executed ONLY by the database CI job against a freshly reset
local Supabase instance (no mocks: real PostgreSQL transactions, RLS,
grants and the purge RPCs). It must never be collected by the ordinary
backend unit job.

It covers the API/application frontier of #316 against the real SQL purge
boundary:

 1.  admission_reservations older than 24h are removed; rows inside the
     horizon stay (quota ledger preserved).
 2.  Privacy operation ledger older than 30 days is removed; rows inside
     the horizon stay (#314 idempotency semantics preserved inside the
     horizon).
 3.  outbox_events completed/dead_letter with expired retention_until are
     removed; pending/processing/failed are never purged by age; a final
     event with future retention_until stays.
 4.  Batch size is really limited per statement.
 5.  A second run without new eligible rows is idempotent.
 6.  Two concurrent runs delete every eligible row exactly once with no
     corruption.
 7.  Cleanup never touches user-controlled content (chat_logs, memories,
     profiles snapshots, turn_requests) of users A and B.
 8.  A runner with an artificially fast process clock cannot advance
     deletion: the SQL boundary clamps every purge cutoff against
     authoritative PostgreSQL time, so rows inside the binding horizons
     survive.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from supabase import Client, create_client

from backend.admission import AdmissionRuntimeConfig
from backend.dependencies import ApplicationDependencies
from backend.health import HealthRegistry
from backend.main import create_app
from backend.retention import (
    RetentionConfig,
    RetentionRunner,
    RetentionRunResult,
    SupabaseRetentionRepository,
)
from backend.retention_policy import (
    RetentionCategory,
    default_retention_policy,
    retention_cutoffs,
)
from backend.settings import AppEnvironment, Settings
from backend.turn_execution import TurnExecutionConfig

_SUPABASE_CLI = ["supabase"] if shutil.which("supabase") else ["npx", "supabase"]

SECRET = "ci-test-secret-0123456789abcdef0123456789abcdef"


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    assert value, f"{name} is required for retention integration tests"
    return value


def _close_client(client: Client) -> None:
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
def supabase_url() -> str:
    return _required_env("SUPABASE_URL")


@pytest.fixture(scope="module")
def service_role_key() -> str:
    return _required_env("SUPABASE_SERVICE_ROLE_KEY")


@pytest.fixture(scope="module")
def service_client(supabase_url: str, service_role_key: str) -> Client:
    client = create_client(supabase_url, service_role_key)
    yield client
    _close_client(client)


# ─── SQL helpers (pinned local Supabase CLI, sanitized) ─────────────────────


def _run_sql(sql: str) -> list[dict]:
    child_env = dict(os.environ)
    child_env["SUPABASE_TELEMETRY_DISABLED"] = "1"
    child_env["SUPABASE_ANALYTICS_ENABLED"] = "false"
    result = subprocess.run(
        [
            *_SUPABASE_CLI,
            "db",
            "query",
            "--agent=no",
            "--output",
            "json",
            sql,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=child_env,
    )
    assert result.returncode == 0, "sanitized retention test SQL operation failed"
    output = result.stdout.strip()
    if not output or output[0] not in "[{":
        return []
    parsed = json.loads(output)
    assert isinstance(parsed, list)
    return parsed


def _count(table: str, predicate: str) -> int:
    rows = _run_sql(f"SELECT count(*)::integer AS count FROM public.{table} WHERE {predicate}")
    return rows[0]["count"]


def _uid(label: str) -> str:
    return f"ret_{label}_{uuid.uuid4().hex[:12]}"


# ─── Seeding ────────────────────────────────────────────────────────────────


def _seed_admission(user_id: str, expired_hours: float | None, current_hours: float | None) -> None:
    """Seed admission reservations: one expired (if expired_hours given) and
    one current (if current_hours given)."""
    statements: list[str] = []
    if expired_hours is not None:
        statements.append(
            "INSERT INTO public.admission_reservations "
            "(user_id, request_id, message_hmac_sha256, network_hmac_sha256, "
            "estimated_units, reserved_at) VALUES "
            f"('{user_id}', '{uuid.uuid4()}', repeat('a', 64), repeat('b', 64), 10, "
            f"now() - interval '{expired_hours} hours')"
        )
    if current_hours is not None:
        statements.append(
            "INSERT INTO public.admission_reservations "
            "(user_id, request_id, message_hmac_sha256, network_hmac_sha256, "
            "estimated_units, reserved_at) VALUES "
            f"('{user_id}', '{uuid.uuid4()}', repeat('c', 64), repeat('d', 64), 10, "
            f"now() - interval '{current_hours} hours')"
        )
    for statement in statements:
        _run_sql(statement)


def _seed_privacy_ledger(user_id: str, expired_days: float | None, current_days: float | None) -> None:
    statements: list[str] = []
    if expired_days is not None:
        statements.append(
            "INSERT INTO public.privacy_operations "
            "(user_id, operation_id, operation, operation_payload_sha256, status, "
            "applied_at, result) VALUES "
            f"('{user_id}', '{uuid.uuid4()}', 'delete_history', repeat('e', 64), 'applied', "
            f"now() - interval '{expired_days} days', '{{\"status\":\"applied\"}}'::jsonb)"
        )
    if current_days is not None:
        statements.append(
            "INSERT INTO public.privacy_operations "
            "(user_id, operation_id, operation, operation_payload_sha256, status, "
            "applied_at, result) VALUES "
            f"('{user_id}', '{uuid.uuid4()}', 'delete_memories', repeat('f', 64), 'applied', "
            f"now() - interval '{current_days} days', '{{\"status\":\"applied\"}}'::jsonb)"
        )
    for statement in statements:
        _run_sql(statement)


def _seed_outbox(
    user_id: str,
    *,
    completed_expired: bool = False,
    dead_expired: bool = False,
    completed_future: bool = False,
    pending_old: bool = False,
    processing_old: bool = False,
    failed_old: bool = False,
) -> None:
    statements: list[str] = []
    # completed: processed_at set, retention_until set
    if completed_expired:
        statements.append(
            "INSERT INTO public.outbox_events "
            "(id, event_type, contract_version, user_id, turn_request_id, payload, "
            "status, attempts, next_attempt_at, lease_owner, lease_expires_at, "
            "idempotency_key, error_code, created_at, updated_at, processed_at, "
            "dead_lettered_at, retention_until) VALUES "
            f"('{uuid.uuid4()}', 'turn_completed', 1, '{user_id}', NULL, "
            f"'{{\"ref\":\"c1\"}}'::jsonb, 'completed', 1, NULL, NULL, NULL, "
            f"'{user_id}-k-ce', NULL, now(), now(), now() - interval '2 days', NULL, "
            "now() - interval '1 day')"
        )
    if dead_expired:
        statements.append(
            "INSERT INTO public.outbox_events "
            "(id, event_type, contract_version, user_id, turn_request_id, payload, "
            "status, attempts, next_attempt_at, lease_owner, lease_expires_at, "
            "idempotency_key, error_code, created_at, updated_at, processed_at, "
            "dead_lettered_at, retention_until) VALUES "
            f"('{uuid.uuid4()}', 'turn_completed', 1, '{user_id}', NULL, "
            f"'{{\"ref\":\"d1\"}}'::jsonb, 'dead_letter', 10, NULL, NULL, NULL, "
            f"'{user_id}-k-de', 'delivery_failed', now(), now(), NULL, "
            "now() - interval '2 days', now() - interval '1 day')"
        )
    if completed_future:
        statements.append(
            "INSERT INTO public.outbox_events "
            "(id, event_type, contract_version, user_id, turn_request_id, payload, "
            "status, attempts, next_attempt_at, lease_owner, lease_expires_at, "
            "idempotency_key, error_code, created_at, updated_at, processed_at, "
            "dead_lettered_at, retention_until) VALUES "
            f"('{uuid.uuid4()}', 'turn_completed', 1, '{user_id}', NULL, "
            f"'{{\"ref\":\"c2\"}}'::jsonb, 'completed', 1, NULL, NULL, NULL, "
            f"'{user_id}-k-cf', NULL, now(), now(), now() - interval '2 days', NULL, "
            "now() + interval '1 day')"
        )
    if pending_old:
        statements.append(
            "INSERT INTO public.outbox_events "
            "(id, event_type, contract_version, user_id, turn_request_id, payload, "
            "status, attempts, next_attempt_at, lease_owner, lease_expires_at, "
            "idempotency_key, error_code, created_at, updated_at, processed_at, "
            "dead_lettered_at, retention_until) VALUES "
            f"('{uuid.uuid4()}', 'turn_completed', 1, '{user_id}', NULL, "
            f"'{{\"ref\":\"p1\"}}'::jsonb, 'pending', 0, "
            "now() - interval '40 days', NULL, NULL, "
            f"'{user_id}-k-pe', NULL, now() - interval '40 days', "
            "now() - interval '40 days', NULL, NULL, NULL)"
        )
    if processing_old:
        statements.append(
            "INSERT INTO public.outbox_events "
            "(id, event_type, contract_version, user_id, turn_request_id, payload, "
            "status, attempts, next_attempt_at, lease_owner, lease_expires_at, "
            "idempotency_key, error_code, created_at, updated_at, processed_at, "
            "dead_lettered_at, retention_until) VALUES "
            f"('{uuid.uuid4()}', 'turn_completed', 1, '{user_id}', NULL, "
            f"'{{\"ref\":\"pr1\"}}'::jsonb, 'processing', 1, NULL, "
            "'worker-a', now() - interval '40 days', "
            f"'{user_id}-k-pr', NULL, now() - interval '40 days', "
            "now() - interval '40 days', NULL, NULL, NULL)"
        )
    if failed_old:
        statements.append(
            "INSERT INTO public.outbox_events "
            "(id, event_type, contract_version, user_id, turn_request_id, payload, "
            "status, attempts, next_attempt_at, lease_owner, lease_expires_at, "
            "idempotency_key, error_code, created_at, updated_at, processed_at, "
            "dead_lettered_at, retention_until) VALUES "
            f"('{uuid.uuid4()}', 'turn_completed', 1, '{user_id}', NULL, "
            f"'{{\"ref\":\"f1\"}}'::jsonb, 'failed', 2, "
            "now() - interval '40 days', NULL, NULL, "
            f"'{user_id}-k-fa', 'delivery_failed', now() - interval '40 days', "
            "now() - interval '40 days', NULL, NULL, NULL)"
        )
    for statement in statements:
        _run_sql(statement)


def _seed_profile(user_id: str) -> None:
    _run_sql(
        "INSERT INTO public.profiles (user_id, persona_config, user_profile, "
        "relationship_state, emotional_state, revision) VALUES "
        f"('{user_id}', 'persona', '{{}}'::jsonb, "
        f"'{{\"schema_version\":1,\"trust\":0.5,\"affection\":0.3,\"tension\":0.0,"
        f"\"triggers\":[],\"timestamp\":1700000000.0}}'::jsonb, "
        f"'{{\"schema_version\":1,\"pleasure\":0.0,\"arousal\":0.0,\"dominance\":0.0,"
        f"\"libido\":0.0,\"aggression\":0.0,\"connection\":0.5,\"energy\":0.8,"
        f"\"tension\":0.0,\"coping_mode\":\"HEALTHY\",\"timestamp\":1700000000.0}}'::jsonb, "
        "2)"
    )
    _run_sql(
        "INSERT INTO public.chat_logs (user_id, role, content) VALUES "
        f"('{user_id}', 'user', 'hello'), ('{user_id}', 'assistant', 'hi there')"
    )
    _run_sql(
        "INSERT INTO public.memories (user_id, content, metadata) VALUES "
        f"('{user_id}', 'a durable memory', '{{\"tags\":[\"x\"]}}'::jsonb)"
    )


def _cleanup_user(user_id: str) -> None:
    for table in (
        "privacy_operations",
        "admission_reservations",
        "archival_extractions",
        "memories",
        "chat_logs",
        "turn_requests",
        "outbox_events",
        "profiles",
    ):
        _run_sql(f"DELETE FROM public.{table} WHERE user_id = '{user_id}'")


def _runner(service_client: Client, *, batch_size: int = 100, clock=time.time):
    return RetentionRunner(
        repository=SupabaseRetentionRepository(service_client),
        config=RetentionConfig(batch_size=batch_size, max_rows_per_category=10_000),
        turn_config=TurnExecutionConfig.defaults(),
        clock=clock,
    )


# ─── 1. admission_reservations horizon ──────────────────────────────────────


def test_admission_expired_removed_current_stays(service_client: Client):
    user_id = _uid("adm")
    _seed_admission(user_id, expired_hours=25, current_hours=23)
    try:
        result = asyncio.run(_runner(service_client).run_once())
        assert isinstance(result, RetentionRunResult)
        admission = result.results[RetentionCategory.ADMISSION_RESERVATIONS.value]
        assert admission.purged == 1
        assert _count("admission_reservations", f"user_id = '{user_id}'") == 1
        assert _count("admission_reservations", f"user_id = '{user_id}' AND reserved_at >= now() - interval '24 hours'") == 1
    finally:
        _cleanup_user(user_id)


# ─── 2. privacy_operations horizon ──────────────────────────────────────────


def test_privacy_ledger_expired_removed_current_stays(service_client: Client):
    user_id = _uid("prv")
    _seed_privacy_ledger(user_id, expired_days=31, current_days=29)
    try:
        result = asyncio.run(_runner(service_client).run_once())
        privacy = result.results[RetentionCategory.PRIVACY_OPERATIONS.value]
        assert privacy.purged == 1
        assert _count("privacy_operations", f"user_id = '{user_id}'") == 1
    finally:
        _cleanup_user(user_id)


# ─── 3. outbox_events eligibility ───────────────────────────────────────────


def test_outbox_final_expired_removed_active_stays(service_client: Client):
    user_id = _uid("out")
    # outbox_events has an FK to profiles(user_id); seed the profile first.
    _seed_profile(user_id)
    _seed_outbox(
        user_id,
        completed_expired=True,
        dead_expired=True,
        completed_future=True,
        pending_old=True,
        processing_old=True,
        failed_old=True,
    )
    try:
        result = asyncio.run(_runner(service_client).run_once())
        outbox = result.results[RetentionCategory.OUTBOX_EVENTS.value]
        assert outbox.purged == 2
        assert _count("outbox_events", f"user_id = '{user_id}' AND status IN ('completed', 'dead_letter')") == 1
        assert _count("outbox_events", f"user_id = '{user_id}' AND status IN ('pending', 'processing', 'failed')") == 3
    finally:
        _cleanup_user(user_id)


# ─── 4/5. Batch limiting and idempotent second run ─────────────────────────


def test_batch_limited_and_second_run_idempotent(service_client: Client):
    user_id = _uid("batch")
    for _ in range(10):
        _seed_admission(user_id, expired_hours=48, current_hours=None)
    try:
        runner = _runner(service_client, batch_size=3)
        first = asyncio.run(runner.run_once())
        admission = first.results[RetentionCategory.ADMISSION_RESERVATIONS.value]
        assert admission.purged == 10
        assert admission.batches == 4  # 3 + 3 + 3 + 1
        assert _count("admission_reservations", f"user_id = '{user_id}'") == 0

        second = asyncio.run(runner.run_once())
        assert second.results[RetentionCategory.ADMISSION_RESERVATIONS.value].purged == 0
        assert second.results[RetentionCategory.ADMISSION_RESERVATIONS.value].batches == 1
    finally:
        _cleanup_user(user_id)


# ─── 6. Concurrent runs are safe ────────────────────────────────────────────


def test_concurrent_runs_are_safe(supabase_url: str, service_role_key: str):
    user_id = _uid("conc")
    for _ in range(20):
        _seed_admission(user_id, expired_hours=48, current_hours=None)
    try:
        outcomes: list[int] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def _run():
            client = create_client(supabase_url, service_role_key)
            try:
                runner = RetentionRunner(
                    repository=SupabaseRetentionRepository(client),
                    config=RetentionConfig(batch_size=5, max_rows_per_category=100),
                    turn_config=TurnExecutionConfig.defaults(),
                )
                result = asyncio.run(runner.run_once())
                with lock:
                    outcomes.append(
                        result.results[RetentionCategory.ADMISSION_RESERVATIONS.value].purged
                    )
            except BaseException as exc:  # noqa: BLE001
                with lock:
                    errors.append(exc)
            finally:
                _close_client(client)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_run) for _ in range(2)]
            for future in futures:
                future.result(timeout=120)

        assert errors == [], f"concurrent runs failed: {errors}"
        assert sum(outcomes) == 20, f"rows double-processed or lost: {outcomes}"
        assert _count("admission_reservations", f"user_id = '{user_id}'") == 0
    finally:
        _cleanup_user(user_id)


# ─── 7. User A/B content is never touched ───────────────────────────────────


def test_user_content_never_touched(service_client: Client):
    user_a = _uid("a")
    user_b = _uid("b")
    _seed_profile(user_a)
    _seed_profile(user_b)
    _seed_admission(user_a, expired_hours=48, current_hours=None)
    _seed_admission(user_b, expired_hours=None, current_hours=2)
    try:
        result = asyncio.run(_runner(service_client).run_once())
        # Only A's expired operational row is purged.
        assert result.results[RetentionCategory.ADMISSION_RESERVATIONS.value].purged == 1
        # B's current row stays.
        assert _count("admission_reservations", f"user_id = '{user_b}'") == 1
        # User content is fully preserved for both users.
        for user_id in (user_a, user_b):
            assert _count("chat_logs", f"user_id = '{user_id}'") == 2
            assert _count("memories", f"user_id = '{user_id}'") == 1
            assert _count("profiles", f"user_id = '{user_id}'") == 1
        assert _count("turn_requests", f"user_id = '{user_a}' OR user_id = '{user_b}'") == 0
    finally:
        _cleanup_user(user_a)
        _cleanup_user(user_b)


# ─── 8. Fast process clock cannot advance deletion (SQL boundary) ───────────


def test_future_clock_cannot_violate_policy(service_client: Client):
    """A runner with an artificially fast process clock must not advance
    deletion.

    The runner computes cutoffs from the injected clock (here ~400 days in
    the future), so every caller cutoff is a future timestamp. The SQL
    boundary clamps each purge cutoff against authoritative PostgreSQL time
    (``clock_timestamp()``), so only genuinely expired rows are removed:
    rows inside the binding 24h/30d horizons and outbox events whose
    ``retention_until`` is still in the future survive.
    """
    user_id = _uid("clock")
    _seed_profile(user_id)
    _seed_admission(user_id, expired_hours=25, current_hours=23)
    _seed_privacy_ledger(user_id, expired_days=31, current_days=29)
    _seed_outbox(user_id, completed_expired=True, completed_future=True)
    try:
        def future_clock() -> float:
            return time.time() + 400 * 86400

        result = asyncio.run(_runner(service_client, clock=future_clock).run_once())
        admission = result.results[RetentionCategory.ADMISSION_RESERVATIONS.value]
        privacy = result.results[RetentionCategory.PRIVACY_OPERATIONS.value]
        outbox = result.results[RetentionCategory.OUTBOX_EVENTS.value]
        # Only genuinely expired rows are purged per category.
        assert admission.purged == 1
        assert privacy.purged == 1
        assert outbox.purged == 1
        # Rows inside the binding horizons survive the future clock.
        assert _count(
            "admission_reservations",
            f"user_id = '{user_id}' AND reserved_at >= now() - interval '24 hours'",
        ) == 1
        assert _count(
            "privacy_operations",
            f"user_id = '{user_id}' AND applied_at >= now() - interval '30 days'",
        ) == 1
        assert _count(
            "outbox_events",
            f"user_id = '{user_id}' AND retention_until >= now()",
        ) == 1
    finally:
        _cleanup_user(user_id)
