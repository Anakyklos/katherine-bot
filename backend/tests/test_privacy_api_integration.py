"""Real Supabase integration tests for the #315 privacy HTTP API frontier.

This file is executed ONLY by the database CI job against a freshly reset
local Supabase instance (no mocks: real GoTrue auth, real PostgreSQL
transactions, locks, ledger and revision increments). It must never be
collected by the ordinary backend unit job.

It deliberately does NOT re-test the #314 SQL invariants (that is
``test_privacy_operations_integration.py``); it covers the API frontier that
#315 creates:

 1. Real authentication + operation through the endpoint: the public
    response matches the #315 contract and ``profiles.revision`` increases
    exactly once.
 2. Replay of the same ``operation_id`` returns the same public result
    without a second mutation or revision increment.
 3. Users A and B are isolated: an operation of A never touches B's data,
    and reusing the same ``operation_id`` across users is independent.
 4. Resets persist the canonical neutral v1 snapshot through the endpoint.
 5. A request without authentication does not mutate the database.
 6. The public response never exposes ``user_id``, ``revision``,
    ``operation_id``, content or internal IDs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from supabase import Client, create_client

from backend.admission import AdmissionRuntimeConfig
from backend.dependencies import ApplicationDependencies
from backend.health import HealthRegistry
from backend.main import create_app
from backend.privacy_operations import (
    OPERATION_DELETE_HISTORY,
    OPERATION_RESET_EMOTIONAL_STATE,
    OPERATION_RESET_RELATIONSHIP_STATE,
    neutral_emotional_snapshot,
    neutral_relationship_snapshot,
    new_operation_id,
)
from backend.privacy_service import PrivacyService, SupabasePrivacyRepository
from backend.settings import AppEnvironment, Settings
from backend.turn_execution import TurnExecutionConfig

_SUPABASE_CLI = ["supabase"] if shutil.which("supabase") else ["npx", "supabase"]

SECRET = "ci-test-secret-0123456789abcdef0123456789abcdef"

PATHS = {
    "delete_history": "/privacy/delete-history",
    "delete_memories": "/privacy/delete-memories",
    "reset_emotional_state": "/privacy/reset-emotional-state",
    "reset_relationship": "/privacy/reset-relationship",
}


# ─── Environment / clients ───────────────────────────────────────────────────


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    assert value, f"{name} is required for privacy API integration tests"
    return value


def _close_http_transports(client: Client) -> None:
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


def _close_client(client: Client) -> None:
    if client is None:
        return
    _close_http_transports(client)
    auth = getattr(client, "auth", None)
    if auth is not None and hasattr(auth, "close"):
        auth.close()


@pytest.fixture(scope="module")
def supabase_url() -> str:
    return _required_env("SUPABASE_URL")


@pytest.fixture(scope="module")
def anon_key() -> str:
    return _required_env("SUPABASE_ANON_KEY")


@pytest.fixture(scope="module")
def service_role_key() -> str:
    return _required_env("SUPABASE_SERVICE_ROLE_KEY")


@pytest.fixture(scope="module")
def service_client(supabase_url: str, service_role_key: str) -> Client:
    client = create_client(supabase_url, service_role_key)
    yield client
    _close_client(client)


class _FakeEngine:
    """Engine double: privacy routes never touch the conversation engine."""

    def __init__(self):
        self.memory_manager = SimpleNamespace(supabase=None)
        self.groq_manager = object()


@pytest.fixture(scope="module")
def app_client(
    supabase_url: str, service_role_key: str
) -> tuple[TestClient, Client]:
    """The real FastAPI application wired to the local Supabase instance."""
    from backend.account_deletion import SupabaseAccountDeletionRepository
    from backend.account_deletion_service import AccountDeletionService

    client = create_client(supabase_url, service_role_key)
    privacy_service = PrivacyService(
        repository=SupabasePrivacyRepository(client),
        turn_config=TurnExecutionConfig.defaults(),
        clock=time.time,
    )
    admission_config = AdmissionRuntimeConfig.from_values(SECRET)
    account_deletion_service = AccountDeletionService(
        repository=SupabaseAccountDeletionRepository(client),
        turn_config=TurnExecutionConfig.defaults(),
        admission_config=admission_config,
    )
    settings = Settings(
        app_env=AppEnvironment.local,
        groq_api_key="ci-groq-key",
        admission_hmac_secret=SECRET,
        cors_allowed_origins=("http://localhost:3000",),
    )
    deps = ApplicationDependencies(
        conversation_engine=_FakeEngine(),
        auth_client=client,
        admission_config=admission_config,
        turn_config=TurnExecutionConfig.defaults(),
        health_checks=HealthRegistry(),
        clock=time.time,
        persistence_client=client,
        privacy_service=privacy_service,
        account_deletion_service=account_deletion_service,
    )
    app = create_app(settings=settings, dependencies=deps)
    yield TestClient(app), client
    _close_client(client)


@pytest.fixture(scope="module")
def users(
    supabase_url: str,
    anon_key: str,
    service_client: Client,
) -> dict[str, dict]:
    """Two real GoTrue users (``a`` and ``b``) with valid sessions."""
    created: list[dict] = []
    for label in ("a", "b"):
        email = f"privacy-api-{label}-{uuid.uuid4().hex[:8]}@test.local"
        password = "password123"
        anon = create_client(supabase_url, anon_key)
        try:
            anon.auth.sign_up({"email": email, "password": password})
            response = anon.auth.sign_in_with_password(
                {"email": email, "password": password}
            )
            assert response is not None and response.user is not None
            assert (
                response.session is not None
                and response.session.access_token is not None
            )
            created.append(
                {
                    "label": label,
                    "id": response.user.id,
                    "token": response.session.access_token,
                    "email": email,
                }
            )
        finally:
            _close_client(anon)
    yield {entry["label"]: entry for entry in created}
    for entry in created:
        for user in service_client.auth.admin.list_users():
            if user.email == entry["email"]:
                service_client.auth.admin.delete_user(user.id)


# ─── SQL helpers (pinned local Supabase CLI, sanitized) ──────────────────────


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
    assert result.returncode == 0, "sanitized privacy API test SQL operation failed"
    output = result.stdout.strip()
    if not output or output[0] not in "[{":
        return []
    parsed = json.loads(output)
    assert isinstance(parsed, list)
    return parsed


def _count(table: str, user_id: str) -> int:
    rows = _run_sql(
        f"SELECT count(*)::integer AS count FROM public.{table} "
        f"WHERE user_id = '{user_id}'"
    )
    return rows[0]["count"]


def _revision(user_id: str) -> int:
    rows = _run_sql(
        f"SELECT revision FROM public.profiles WHERE user_id = '{user_id}'"
    )
    return rows[0]["revision"]


def _emotional_state_of(user_id: str) -> dict:
    rows = _run_sql(
        f"SELECT emotional_state FROM public.profiles WHERE user_id = '{user_id}'"
    )
    return rows[0]["emotional_state"]


def _relationship_state_of(user_id: str) -> dict:
    rows = _run_sql(
        f"SELECT relationship_state FROM public.profiles WHERE user_id = '{user_id}'"
    )
    return rows[0]["relationship_state"]


# ─── Seeding / cleanup ───────────────────────────────────────────────────────


def _emotional_state() -> dict:
    return {
        "schema_version": 1, "pleasure": 0.9, "arousal": 0.8, "dominance": 0.7,
        "libido": 0.1, "aggression": 0.1, "connection": 0.5, "energy": 0.8,
        "tension": 0.1, "coping_mode": "MANIC", "timestamp": 1700000000.0,
    }


def _relationship_state() -> dict:
    return {
        "schema_version": 1, "trust": 0.9, "affection": 0.8, "tension": 0.1,
        "triggers": [], "timestamp": 1700000000.0,
    }


def _seed_user(
    user_id: str,
    *,
    revision: int = 2,
    with_turn: bool = True,
    with_memory: bool = True,
) -> None:
    _run_sql(
        "INSERT INTO public.profiles (user_id, persona_config, user_profile, "
        "relationship_state, emotional_state, revision) VALUES "
        f"('{user_id}', 'persona-config', '{{}}'::jsonb, "
        f"'{json.dumps(_relationship_state())}'::jsonb, "
        f"'{json.dumps(_emotional_state())}'::jsonb, {revision})"
    )
    _run_sql(
        "INSERT INTO public.chat_logs (user_id, role, content) VALUES "
        f"('{user_id}', 'user', 'hello'), ('{user_id}', 'assistant', 'hi there')"
    )
    if with_turn:
        turn_id = str(uuid.uuid4())
        _run_sql(
            "INSERT INTO public.turn_requests ("
            "id, user_id, request_id, payload_hash_sha256, status, expected_revision, "
            "committed_revision, replay_payload, created_at, updated_at, completed_at) "
            "VALUES "
            f"('{turn_id}', '{user_id}', '{uuid.uuid4()}', "
            f"'{'a' * 64}', 'completed', 0, {revision}, "
            f"'{{\"response\":\"hi there\",\"message_id\":\"{uuid.uuid4()}\"}}'::jsonb, "
            "now(), now(), now())"
        )
    if with_memory:
        _run_sql(
            "INSERT INTO public.memories (user_id, content, metadata) VALUES "
            f"('{user_id}', 'a durable memory', '{{\"tags\":[\"x\"]}}'::jsonb)"
        )
    _run_sql(
        "INSERT INTO public.archival_extractions ("
        "user_id, source_chat_log_id, extractor_version, schema_version, "
        "idempotency_key, facts) "
        "SELECT '{user_id}', id, 1, 1, '{user_id}-arch-1', '{{\"facts\":[]}}'::jsonb "
        "FROM public.chat_logs WHERE user_id = '{user_id}' AND role = 'user'".format(
            user_id=user_id
        )
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


def _assert_public_contract(body: dict) -> None:
    """The public projection contains ONLY operation/status/counts."""
    assert set(body) == {"operation", "status", "counts"}
    assert body["status"] == "applied"
    forbidden = {"user_id", "revision", "operation_id", "id", "content", "snapshot"}
    for key in forbidden:
        assert key not in body


# ─── 1/2. Operation via endpoint applies once and replays idempotently ──────


def test_delete_history_via_endpoint_applies_once_and_replays(
    app_client: tuple[TestClient, Client],
    users: dict[str, dict],
):
    client, _ = app_client
    user = users["a"]
    _seed_user(user["id"], revision=2)
    try:
        headers = {"Authorization": f"Bearer {user['token']}"}
        operation_id = new_operation_id()
        before = _revision(user["id"])
        assert _count("chat_logs", user["id"]) == 2

        response = client.post(
            PATHS["delete_history"],
            json={"operation_id": operation_id},
            headers=headers,
        )
        assert response.status_code == 200
        body = response.json()
        _assert_public_contract(body)
        assert body["operation"] == "delete_history"
        assert body["counts"]["chat_logs"] == 2

        after = _revision(user["id"])
        assert after == before + 1, "revision must increase exactly once"
        assert _count("chat_logs", user["id"]) == 0
        assert _count("turn_requests", user["id"]) == 0
        assert _count("archival_extractions", user["id"]) == 0
        # Non-history data is preserved.
        assert _count("memories", user["id"]) == 1

        # Replay: same operation_id -> same public result, no second mutation.
        replay = client.post(
            PATHS["delete_history"],
            json={"operation_id": operation_id},
            headers=headers,
        )
        assert replay.status_code == 200
        assert replay.json() == body
        assert _revision(user["id"]) == after, "replay must not increment revision"
    finally:
        _cleanup_user(user["id"])


# ─── 4. Resets persist the canonical neutral v1 snapshot ────────────────────


def test_reset_emotional_state_persists_neutral_snapshot(
    app_client: tuple[TestClient, Client],
    users: dict[str, dict],
):
    client, _ = app_client
    user = users["a"]
    _seed_user(user["id"], revision=2)
    try:
        headers = {"Authorization": f"Bearer {user['token']}"}
        response = client.post(
            PATHS["reset_emotional_state"],
            json={"operation_id": new_operation_id()},
            headers=headers,
        )
        assert response.status_code == 200
        body = response.json()
        _assert_public_contract(body)
        assert body["operation"] == "reset_emotional_state"

        stored = _emotional_state_of(user["id"])
        # The stored snapshot is exactly the canonical neutral v1 snapshot
        # built from its own timestamp (pins canonical neutrality).
        assert stored == neutral_emotional_snapshot(stored["timestamp"])
        assert stored["schema_version"] == 1
        assert stored["coping_mode"] == "HEALTHY"
        assert stored["pleasure"] == 0.0
        # The relationship snapshot is preserved.
        assert _relationship_state_of(user["id"])["trust"] == 0.9
    finally:
        _cleanup_user(user["id"])


def test_reset_relationship_state_persists_neutral_snapshot(
    app_client: tuple[TestClient, Client],
    users: dict[str, dict],
):
    client, _ = app_client
    user = users["a"]
    _seed_user(user["id"], revision=2)
    try:
        headers = {"Authorization": f"Bearer {user['token']}"}
        response = client.post(
            PATHS["reset_relationship"],
            json={"operation_id": new_operation_id()},
            headers=headers,
        )
        assert response.status_code == 200
        body = response.json()
        _assert_public_contract(body)
        assert body["operation"] == "reset_relationship_state"

        stored = _relationship_state_of(user["id"])
        assert stored == neutral_relationship_snapshot(stored["timestamp"])
        assert stored["schema_version"] == 1
        assert stored["trust"] == 0.5
        assert stored["affection"] == 0.3
        # The emotional snapshot is preserved.
        assert _emotional_state_of(user["id"])["coping_mode"] == "MANIC"
    finally:
        _cleanup_user(user["id"])


# ─── 3. Users A and B are isolated ──────────────────────────────────────────


def test_users_a_and_b_are_isolated(
    app_client: tuple[TestClient, Client],
    users: dict[str, dict],
):
    client, _ = app_client
    user_a, user_b = users["a"], users["b"]
    _seed_user(user_a["id"], revision=2)
    _seed_user(user_b["id"], revision=5)
    try:
        headers_a = {"Authorization": f"Bearer {user_a['token']}"}
        headers_b = {"Authorization": f"Bearer {user_b['token']}"}
        operation_id = new_operation_id()

        # User A deletes their history.
        response = client.post(
            PATHS["delete_history"],
            json={"operation_id": operation_id},
            headers=headers_a,
        )
        assert response.status_code == 200
        assert _count("chat_logs", user_a["id"]) == 0
        # User B's data is untouched by A's operation.
        assert _count("chat_logs", user_b["id"]) == 2

        # User B reuses the SAME operation_id: keyed per user, so it is an
        # independent operation, never a replay of A's ledger row.
        response_b = client.post(
            PATHS["delete_history"],
            json={"operation_id": operation_id},
            headers=headers_b,
        )
        assert response_b.status_code == 200
        assert response_b.json()["operation"] == "delete_history"
        assert _count("chat_logs", user_b["id"]) == 0

        # Both revisions increased independently, exactly once each.
        assert _revision(user_a["id"]) == 3
        assert _revision(user_b["id"]) == 6
    finally:
        _cleanup_user(user_a["id"])
        _cleanup_user(user_b["id"])


# ─── 5. Request without authentication does not mutate ──────────────────────


def test_request_without_auth_does_not_mutate(
    app_client: tuple[TestClient, Client],
    users: dict[str, dict],
):
    client, _ = app_client
    user = users["a"]
    _seed_user(user["id"], revision=2)
    try:
        before = _revision(user["id"])
        response = client.post(
            PATHS["delete_history"], json={"operation_id": new_operation_id()}
        )
        assert response.status_code == 401
        assert _revision(user["id"]) == before
        assert _count("chat_logs", user["id"]) == 2
        assert _count("privacy_operations", user["id"]) == 0
        assert _count("profiles", user["id"]) == 1
    finally:
        _cleanup_user(user["id"])


# ─── 6. Public response never exposes internal fields ───────────────────────


def test_public_responses_never_expose_internal_fields(
    app_client: tuple[TestClient, Client],
    users: dict[str, dict],
):
    client, _ = app_client
    user = users["a"]
    _seed_user(user["id"], revision=2)
    try:
        headers = {"Authorization": f"Bearer {user['token']}"}
        for path in PATHS.values():
            response = client.post(
                path, json={"operation_id": new_operation_id()}, headers=headers
            )
            assert response.status_code == 200
            _assert_public_contract(response.json())
    finally:
        _cleanup_user(user["id"])
