"""Tests for the #315 authenticated privacy HTTP API.

Covers the mandatory scenarios of issue #315:

 1.  The four privacy routes without a bearer token return 401 and never
     call the privacy service.
 2.  The identity passed to the service comes exclusively from
     ``current_user.id`` (never from the body, query, path or headers).
 3.  A body containing ``user_id`` returns 422.
 4.  Any extra key returns 422.
 5.  An invalid ``operation_id`` returns a sanitized 422.
 6.  Each endpoint calls exactly the corresponding operation.
 7.  ``delete_history`` never calls delete memories nor resets.
 8.  ``delete_memories`` never calls history nor resets.
 9.  Emotional reset builds the canonical neutral v1 snapshot.
10.  Relationship reset builds the canonical neutral v1 snapshot.
11.  The reset clock is injectable and deterministically testable.
12.  Replay of the same ``operation_id`` returns the same public result
     without a second mutation.
13.  Divergent reuse produces ``409 operation_conflict``.
14.  Persistence failure produces a constant 503.
15.  An unexpected error produces a sanitized 500.
16-19. The public result never contains ``user_id``, ``revision``,
     ``operation_id``, content or internal IDs.
20.  User A cannot provoke an operation on user B's data.
21.  Sentinels placed in ``user_id``, ``operation_id`` and the upstream
     exception never reach logs or responses.
22.  No route uses ``BackgroundTasks``.
23.  A Supabase write is never abandoned on cancellation.
24-29. ``/chat``, ``/history``, ``/live``, ``/ready``, ``ChatResponse`` and
     ``create_app()`` import behavior remain compatible.
30.  The whole backend suite stays green (CI).

Replay/conflict unit behavior is exercised through a ledger-simulating
repository that mirrors the #314 PostgreSQL contract; the real database
behavior is verified by ``test_privacy_api_integration.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient

import backend.main as main_module
import backend.privacy_service as privacy_service_module
from backend.admission import AdmissionRuntimeConfig
from backend.atomic_turn_commit import ConflictError, PersistenceError, ValidationError
from backend.emotion_presentation import EmotionStateResponse
from backend.health import HealthRegistry
from backend.privacy_operations import (
    OPERATION_DELETE_HISTORY,
    OPERATION_DELETE_MEMORIES,
    OPERATION_RESET_EMOTIONAL_STATE,
    OPERATION_RESET_RELATIONSHIP_STATE,
    PrivacyOperationResult,
    neutral_emotional_snapshot,
    neutral_relationship_snapshot,
)
from backend.privacy_service import (
    PrivacyOperationResponse,
    PrivacyService,
    SupabasePrivacyRepository,
)
from backend.process_turn import ProcessTurnResult
from backend.settings import AppEnvironment, Settings
from backend.turn_execution import TurnExecutionConfig

SECRET = "s" * 40
OP_ID = "11111111-1111-1111-1111-111111111111"
OP_ID_2 = "22222222-2222-2222-2222-222222222222"
SENTINEL_OP_ID = "99999999-9999-9999-9999-999999999999"
SENTINEL_USER = "SENTINEL-USER-MARKER"
SENTINEL_EXC = "SENTINEL-UPSTREAM-MARKER"
VALID_CHAT_REQUEST_ID = "550e8400-e29b-41d4-a716-446655440000"

PATHS = {
    OPERATION_DELETE_HISTORY: "/privacy/delete-history",
    OPERATION_DELETE_MEMORIES: "/privacy/delete-memories",
    OPERATION_RESET_EMOTIONAL_STATE: "/privacy/reset-emotional-state",
    OPERATION_RESET_RELATIONSHIP_STATE: "/privacy/reset-relationship",
}

# ─── Helpers ─────────────────────────────────────────────────────────────────


def _counts(**overrides) -> dict:
    counts = {
        "chat_logs": 0,
        "turn_requests": 0,
        "outbox_events": 0,
        "archival_extractions": 0,
        "memories": 0,
        "profiles": 0,
    }
    counts.update(overrides)
    return counts


def _rpc_envelope(op: str, op_id: str = OP_ID, user_id: str = "user-a", revision: int = 1) -> dict:
    """A #314 success envelope as returned by the PostgreSQL RPC."""
    return {
        "status": "applied",
        "operation": op,
        "operation_id": op_id,
        "user_id": user_id,
        "revision": revision,
        "counts": _counts(),
    }


def _result(op: str, op_id: str = OP_ID, revision: int = 1, counts: dict | None = None):
    return PrivacyOperationResult(
        operation=op,
        operation_id=op_id,
        user_id="user-123",
        revision=revision,
        counts=counts if counts is not None else _counts(),
    )


def _settings(**overrides) -> Settings:
    kwargs = {
        "app_env": AppEnvironment.local,
        "groq_api_key": "groq-key",
        "admission_hmac_secret": SECRET,
        "cors_allowed_origins": ("https://allowed.example",),
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


class FakeAuth:
    """Duck-typed auth surface exposing ``.auth.get_user(token)``."""

    class _UserSurface:
        def __init__(self, user_id: str):
            self.user_id = user_id
            self.calls: list[str] = []

        def get_user(self, token: str):
            self.calls.append(token)
            return SimpleNamespace(user=SimpleNamespace(id=self.user_id))

    def __init__(self, user_id: str = "user-123"):
        self.auth = self._UserSurface(user_id)


class FakeEngine:
    def __init__(self, supabase=None):
        self.memory_manager = SimpleNamespace(supabase=supabase)
        self.groq_manager = object()


class RecordingPrivacyService:
    """Records every invocation; returns a canned result or raises."""

    def __init__(self, result=None, error=None):
        self.calls: list[tuple] = []
        self.result = result
        self.error = error

    async def delete_history(self, user_id: str, operation_id: str):
        self.calls.append(("delete_history", user_id, operation_id))
        return await self._respond()

    async def delete_memories(self, user_id: str, operation_id: str):
        self.calls.append(("delete_memories", user_id, operation_id))
        return await self._respond()

    async def reset_emotional_state(self, user_id: str, operation_id: str):
        self.calls.append(("reset_emotional_state", user_id, operation_id))
        return await self._respond()

    async def reset_relationship_state(self, user_id: str, operation_id: str):
        self.calls.append(("reset_relationship_state", user_id, operation_id))
        return await self._respond()

    async def _respond(self):
        if self.error is not None:
            raise self.error
        if isinstance(self.result, PrivacyOperationResult):
            # Mirror the real service: the public projection is returned.
            return PrivacyOperationResponse.from_result(self.result)
        return self.result


class _FakeSupabase:
    """Duck-typed Supabase surface for the history route."""

    def table(self, name):
        return self

    def select(self, cols):
        return self

    def eq(self, key, value):
        return self

    def order(self, col, **kwargs):
        return self

    def limit(self, n):
        return self

    def execute(self):
        return SimpleNamespace(data=[{"content": "msg1", "role": "user"}], error=None)


class _ChatSupabase:
    """Duck-typed Supabase surface for the /chat admission RPC."""

    def rpc(self, name, params):
        return SimpleNamespace(
            execute=lambda: SimpleNamespace(
                data=[{"decision": "admitted", "retry_after_seconds": 0}], error=None
            )
        )


class _FakeChatEngine:
    """Engine double for the /chat compatibility test."""

    def __init__(self):
        self.memory_manager = SimpleNamespace(supabase=object())
        self.groq_manager = object()
        self.turn_calls = []

    async def process_turn(
        self, user_id, message, request_id, *, budget=None, mode=None, correlation=None, account_deletion_user_ref=None
    ):
        self.turn_calls.append((user_id, message, request_id))
        return ProcessTurnResult(
            committed=object(),
            response="compat response",
            emotion_state=EmotionStateResponse(
                schema_version=1,
                mood_label="NEUTRA",
                pad={"pleasure": 0.0, "arousal": 0.0, "dominance": 0.0},
                dominant_emotions=[],
                timestamp=1000.0,
            ),
        )


def _make_app(
    *,
    service,
    auth_user_id: str = "user-123",
    persistence=None,
    engine=None,
    account_deletion_service=None,
):
    from backend.tests.fixtures.account_deletion_fakes import NoTombstoneGate

    settings = _settings()
    deps = main_module.ApplicationDependencies(
        conversation_engine=engine if engine is not None else FakeEngine(supabase=object()),
        auth_client=FakeAuth(auth_user_id),
        admission_config=AdmissionRuntimeConfig.from_values(SECRET),
        turn_config=TurnExecutionConfig.defaults(),
        health_checks=HealthRegistry(),
        clock=time.time,
        persistence_client=persistence,
        privacy_service=service,
        account_deletion_service=(
            account_deletion_service
            if account_deletion_service is not None
            else NoTombstoneGate()
        ),
    )
    return main_module.create_app(settings=settings, dependencies=deps)


def _auth_headers(token: str = "token-x") -> dict:
    return {"Authorization": f"Bearer {token}"}


def _assert_no_internal_fields(node) -> None:
    """Fail if any key that could carry internal/sensitive data appears."""
    forbidden = {
        "user_id",
        "revision",
        "operation_id",
        "id",
        "content",
        "snapshot",
        "hmac",
        "token",
        "secret",
        "sql",
        "message_id",
        "memory_id",
        "ledger",
    }

    def walk(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                assert key.lower() not in forbidden, f"internal field leaked: {key}"
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(node)


class LedgerSimulatingRepository:
    """Mirrors the #314 durable ledger contract for unit tests.

    Keyed by ``(authenticated_user_id, operation_id)``: the first call
    applies (mutation counter +1) and stores the envelope; an identical
    replay returns the stored envelope without a new mutation; a divergent
    reuse of the same key returns the ``operation_conflict`` envelope. The
    real database behavior is verified by the integration suite.
    """

    def __init__(self, envelope_factory):
        self._ledger: dict[tuple, tuple] = {}
        self.mutations = 0
        self._envelope_factory = envelope_factory

    def call(self, rpc_name: str, params: dict) -> dict:
        key = (params["p_authenticated_user_id"], params["p_operation_id"])
        payload = params["p_operation_payload"]
        if key in self._ledger:
            stored_rpc, stored_payload, stored_envelope = self._ledger[key]
            if stored_rpc != rpc_name or stored_payload != payload:
                return {
                    "error": {
                        "code": "operation_conflict",
                        "message": "operation_id already used with a different operation or payload",
                    }
                }
            return stored_envelope
        envelope = self._envelope_factory(rpc_name, params)
        self._ledger[key] = (rpc_name, payload, envelope)
        self.mutations += 1
        return envelope


def _envelope_from_params(rpc_name: str, params: dict) -> dict:
    """Build a success envelope from the RPC params (privacy RPC names equal
    the canonical operation names)."""
    return _rpc_envelope(
        op=rpc_name,
        op_id=params["p_operation_id"],
        user_id=params["p_authenticated_user_id"],
    )


def _real_service(repo) -> PrivacyService:
    return PrivacyService(
        repository=repo,
        turn_config=TurnExecutionConfig.defaults(),
        clock=lambda: 1700000000.0,
    )


# ─── 1. 401 without bearer token, service untouched ─────────────────────────


@pytest.mark.parametrize("path", sorted(PATHS.values()))
def test_route_without_bearer_returns_401_and_does_not_call_service(path):
    service = RecordingPrivacyService(result=_result(OPERATION_DELETE_HISTORY))
    app = _make_app(service=service)
    client = TestClient(app)
    response = client.post(path, json={"operation_id": OP_ID})
    assert response.status_code == 401
    assert service.calls == [], "the privacy service must not be reached"


@pytest.mark.parametrize("path", sorted(PATHS.values()))
def test_route_without_wired_service_returns_503_constant(path):
    """When ``privacy_service`` is not wired in the container, every privacy
    endpoint fails closed with the sanitized 503 service-unavailable payload
    (never a 500 and never an internal detail)."""
    app = _make_app(service=None)
    client = TestClient(app)
    response = client.post(
        path, json={"operation_id": OP_ID}, headers=_auth_headers()
    )
    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "service_unavailable", "message": "Service unavailable."}
    }


# ─── 2. Identity comes exclusively from current_user.id ─────────────────────


def test_identity_passed_to_service_comes_only_from_current_user():
    service = RecordingPrivacyService(result=_result(OPERATION_DELETE_HISTORY))
    app = _make_app(service=service, auth_user_id="authenticated-user-a")
    client = TestClient(app)
    response = client.post(
        PATHS[OPERATION_DELETE_HISTORY],
        json={"operation_id": OP_ID},
        headers=_auth_headers(),
    )
    assert response.status_code == 200
    assert service.calls == [("delete_history", "authenticated-user-a", OP_ID)]


# ─── 3/4. Body with user_id or any extra key returns 422 ────────────────────


@pytest.mark.parametrize("path", sorted(PATHS.values()))
@pytest.mark.parametrize(
    "extra",
    [
        {"user_id": "attacker-user"},
        {"foo": "bar"},
        {"operation_id": OP_ID, "operation": "delete_history"},
        {"operation_id": OP_ID, "user_id": "a", "extra": True},
    ],
)
def test_body_with_user_id_or_extra_key_returns_422(path, extra):
    service = RecordingPrivacyService(result=_result(OPERATION_DELETE_HISTORY))
    app = _make_app(service=service)
    client = TestClient(app)
    response = client.post(path, json=extra, headers=_auth_headers())
    assert response.status_code == 422
    assert service.calls == [], "invalid bodies must never reach the service"
    body = response.json()
    assert body == {"detail": {"code": "invalid_request", "message": "Invalid request body."}}


# ─── 5. Invalid operation_id returns sanitized 422 ──────────────────────────


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-uuid",
        "",
        "123",
        "550e8400-e29b-41d4-a716-44665544000Z",
        ["11111111-1111-1111-1111-111111111111"],
    ],
)
def test_invalid_operation_id_returns_422_sanitized(bad):
    service = RecordingPrivacyService(result=_result(OPERATION_DELETE_HISTORY))
    app = _make_app(service=service)
    client = TestClient(app)
    response = client.post(
        PATHS[OPERATION_DELETE_HISTORY], json={"operation_id": bad}, headers=_auth_headers()
    )
    assert response.status_code == 422
    assert service.calls == []
    body = response.json()
    assert body == {"detail": {"code": "invalid_request", "message": "Invalid request body."}}
    # Sanitized: the offending input is never echoed.
    assert repr(bad) not in response.text


def test_compact_uuid_is_normalized_to_canonical_lowercase():
    """A compact (no-dash) canonical UUID is accepted and normalized to the
    dashed lowercase form required by the #314 contract."""
    service = RecordingPrivacyService(result=_result(OPERATION_DELETE_HISTORY))
    app = _make_app(service=service)
    client = TestClient(app)
    compact = "550e8400e29b41d4a716446655440000"
    response = client.post(
        PATHS[OPERATION_DELETE_HISTORY], json={"operation_id": compact}, headers=_auth_headers()
    )
    assert response.status_code == 200
    assert service.calls == [
        ("delete_history", "user-123", "550e8400-e29b-41d4-a716-446655440000")
    ]


def test_uppercase_uuid_is_normalized_to_lowercase_canonical():
    """The contract accepts any canonical UUID version and normalizes it to
    lowercase before reaching the privacy layer."""
    service = RecordingPrivacyService(result=_result(OPERATION_DELETE_HISTORY))
    app = _make_app(service=service)
    client = TestClient(app)
    uppercase = "550E8400-E29B-41D4-A716-446655440000"
    response = client.post(
        PATHS[OPERATION_DELETE_HISTORY], json={"operation_id": uppercase}, headers=_auth_headers()
    )
    assert response.status_code == 200
    assert service.calls == [("delete_history", "user-123", "550e8400-e29b-41d4-a716-446655440000")]


def test_non_v4_uuid_is_accepted():
    """The #314 contract accepts canonical UUIDs of any version; the API must
    not restrict to v4."""
    service = RecordingPrivacyService(result=_result(OPERATION_DELETE_HISTORY))
    app = _make_app(service=service)
    client = TestClient(app)
    v1_uuid = "11111111-1111-1111-8111-111111111111"
    response = client.post(
        PATHS[OPERATION_DELETE_HISTORY], json={"operation_id": v1_uuid}, headers=_auth_headers()
    )
    assert response.status_code == 200
    assert service.calls == [("delete_history", "user-123", v1_uuid)]


# ─── 6/7/8. Each endpoint calls exactly its own operation ───────────────────


def test_each_endpoint_calls_exactly_its_operation():
    expected = {
        PATHS[OPERATION_DELETE_HISTORY]: "delete_history",
        PATHS[OPERATION_DELETE_MEMORIES]: "delete_memories",
        PATHS[OPERATION_RESET_EMOTIONAL_STATE]: "reset_emotional_state",
        PATHS[OPERATION_RESET_RELATIONSHIP_STATE]: "reset_relationship_state",
    }
    for path, method_name in expected.items():
        service = RecordingPrivacyService(result=_result(method_name))
        app = _make_app(service=service)
        client = TestClient(app)
        response = client.post(path, json={"operation_id": OP_ID}, headers=_auth_headers())
        assert response.status_code == 200
        assert [call[0] for call in service.calls] == [method_name]
        assert service.calls[0][1:] == ("user-123", OP_ID)


def test_delete_history_never_calls_delete_memories_nor_resets():
    service = RecordingPrivacyService(result=_result(OPERATION_DELETE_HISTORY))
    app = _make_app(service=service)
    client = TestClient(app)
    response = client.post(
        PATHS[OPERATION_DELETE_HISTORY], json={"operation_id": OP_ID}, headers=_auth_headers()
    )
    assert response.status_code == 200
    assert service.calls == [("delete_history", "user-123", OP_ID)]


def test_delete_memories_never_calls_history_nor_resets():
    service = RecordingPrivacyService(result=_result(OPERATION_DELETE_MEMORIES))
    app = _make_app(service=service)
    client = TestClient(app)
    response = client.post(
        PATHS[OPERATION_DELETE_MEMORIES], json={"operation_id": OP_ID}, headers=_auth_headers()
    )
    assert response.status_code == 200
    assert service.calls == [("delete_memories", "user-123", OP_ID)]


# ─── 9/10/11. Resets build canonical neutral v1 snapshots with injected clock


def test_emotional_reset_builds_canonical_neutral_v1_snapshot():
    captured = {}

    class CapturingRepo:
        def call(self, rpc_name, params):
            captured["rpc"] = rpc_name
            captured["payload"] = params["p_operation_payload"]
            return _rpc_envelope(OPERATION_RESET_EMOTIONAL_STATE)

    service = PrivacyService(
        repository=CapturingRepo(),
        turn_config=TurnExecutionConfig.defaults(),
        clock=lambda: 1700000000.0,
    )
    asyncio.run(service.reset_emotional_state("user-a", OP_ID))
    expected = neutral_emotional_snapshot(1700000000.0)
    assert captured["rpc"] == "reset_emotional_state"
    assert captured["payload"] == expected
    assert captured["payload"]["schema_version"] == 1
    assert captured["payload"]["timestamp"] == 1700000000.0


def test_relationship_reset_builds_canonical_neutral_v1_snapshot():
    captured = {}

    class CapturingRepo:
        def call(self, rpc_name, params):
            captured["rpc"] = rpc_name
            captured["payload"] = params["p_operation_payload"]
            return _rpc_envelope(OPERATION_RESET_RELATIONSHIP_STATE)

    service = PrivacyService(
        repository=CapturingRepo(),
        turn_config=TurnExecutionConfig.defaults(),
        clock=lambda: 1700000000.0,
    )
    asyncio.run(service.reset_relationship_state("user-a", OP_ID))
    expected = neutral_relationship_snapshot(1700000000.0)
    assert captured["rpc"] == "reset_relationship_state"
    assert captured["payload"] == expected
    assert captured["payload"]["schema_version"] == 1
    assert captured["payload"]["timestamp"] == 1700000000.0


def test_reset_clock_is_injectable_and_deterministic():
    captured_timestamps = []

    class Repo:
        def call(self, rpc_name, params):
            captured_timestamps.append(params["p_operation_payload"]["timestamp"])
            return _rpc_envelope(
                rpc_name,
                op_id=params["p_operation_id"],
                user_id=params["p_authenticated_user_id"],
            )

    service = PrivacyService(
        repository=Repo(),
        turn_config=TurnExecutionConfig.defaults(),
        clock=lambda: 123456.789,
    )
    asyncio.run(service.reset_emotional_state("user-a", OP_ID))
    asyncio.run(service.reset_relationship_state("user-a", OP_ID_2))
    assert captured_timestamps == [123456.789, 123456.789]


def test_delete_operations_send_empty_payload():
    """Deletes never send a snapshot payload to the RPC."""
    captured = []

    class Repo:
        def call(self, rpc_name, params):
            captured.append((rpc_name, params["p_operation_payload"]))
            return _rpc_envelope(rpc_name, op_id=params["p_operation_id"], user_id=params["p_authenticated_user_id"])

    service = PrivacyService(
        repository=Repo(),
        turn_config=TurnExecutionConfig.defaults(),
        clock=lambda: 1700000000.0,
    )
    asyncio.run(service.delete_history("user-a", OP_ID))
    asyncio.run(service.delete_memories("user-a", OP_ID_2))
    assert captured == [("delete_history", {}), ("delete_memories", {})]


# ─── Repository: malformed Supabase response shapes fail closed ─────────────


def _repo_with_response(response) -> SupabasePrivacyRepository:
    client = SimpleNamespace(
        rpc=lambda name, params: SimpleNamespace(execute=lambda: response)
    )
    return SupabasePrivacyRepository(client)


def test_repository_accepts_single_mapping_response():
    repo = _repo_with_response(SimpleNamespace(data={"status": "applied"}))
    assert repo.call("delete_history", {}) == {"status": "applied"}


def test_repository_accepts_single_element_list_response():
    repo = _repo_with_response(SimpleNamespace(data=[{"status": "applied"}]))
    assert repo.call("delete_history", {}) == {"status": "applied"}


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(data=[]),
        SimpleNamespace(data=[{"a": 1}, {"b": 2}]),
        SimpleNamespace(data="not-a-mapping"),
        SimpleNamespace(data=None),
        SimpleNamespace(),  # missing ``data`` attribute
        "not-a-response",
        None,
    ],
)
def test_repository_maps_malformed_response_shapes_to_persistence_error(response):
    repo = _repo_with_response(response)
    with pytest.raises(PersistenceError) as exc:
        repo.call("delete_history", {})
    assert exc.value.code == "database_error"
    assert "persistence error" in str(exc.value)


def test_repository_maps_rpc_exception_to_persistence_error_sanitized():
    class ExplodingClient:
        def rpc(self, name, params):
            raise RuntimeError(SENTINEL_EXC)

    repo = SupabasePrivacyRepository(ExplodingClient())
    with pytest.raises(PersistenceError) as exc:
        repo.call("delete_history", {})
    assert SENTINEL_EXC not in str(exc.value)
    assert "persistence error" in str(exc.value)


def test_repository_without_client_raises_sanitized_persistence_error():
    repo = SupabasePrivacyRepository(None)
    with pytest.raises(PersistenceError) as exc:
        repo.call("delete_history", {})
    assert exc.value.code == "database_error"


# ─── 12. Idempotent replay, same public result, no second mutation ──────────


def test_replay_returns_same_public_result_without_second_mutation():
    repo = LedgerSimulatingRepository(_envelope_from_params)
    service = _real_service(repo)
    first = asyncio.run(service.delete_history("user-a", OP_ID))
    second = asyncio.run(service.delete_history("user-a", OP_ID))
    assert first == second
    assert first.operation == "delete_history"
    assert first.status == "applied"
    assert first.counts == _counts()
    assert repo.mutations == 1, "replay must not re-apply the mutation"


def test_replay_through_http_returns_identical_public_json():
    repo = LedgerSimulatingRepository(_envelope_from_params)
    service = _real_service(repo)
    app = _make_app(service=service, auth_user_id="user-a")
    client = TestClient(app)
    headers = _auth_headers()
    first = client.post(
        PATHS[OPERATION_DELETE_HISTORY], json={"operation_id": OP_ID}, headers=headers
    )
    second = client.post(
        PATHS[OPERATION_DELETE_HISTORY], json={"operation_id": OP_ID}, headers=headers
    )
    assert first.status_code == 200 and second.status_code == 200
    assert first.json() == second.json()
    assert repo.mutations == 1


# ─── 13. Divergent reuse produces 409 operation_conflict ────────────────────


def test_divergent_reuse_of_operation_id_conflicts_at_service_level():
    repo = LedgerSimulatingRepository(_envelope_from_params)
    service = _real_service(repo)
    asyncio.run(service.delete_history("user-a", OP_ID))
    with pytest.raises(ConflictError) as exc:
        asyncio.run(service.delete_memories("user-a", OP_ID))
    assert exc.value.code == "operation_conflict"


def test_divergent_reuse_returns_409_http():
    service = RecordingPrivacyService(
        error=ConflictError(
            code="operation_conflict",
            message="operation_id already used with a different operation or payload",
            expected_revision=0,
        )
    )
    app = _make_app(service=service)
    client = TestClient(app)
    response = client.post(
        PATHS[OPERATION_DELETE_HISTORY], json={"operation_id": OP_ID}, headers=_auth_headers()
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "operation_conflict",
            "message": "Operation identifier was already used with a different operation.",
        }
    }


# ─── 14. Persistence failure produces constant 503 ──────────────────────────


def test_persistence_failure_returns_503_constant():
    service = RecordingPrivacyService(
        error=PersistenceError("database_error", "persistence error")
    )
    app = _make_app(service=service)
    client = TestClient(app)
    response = client.post(
        PATHS[OPERATION_DELETE_HISTORY], json={"operation_id": OP_ID}, headers=_auth_headers()
    )
    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "persistence_unavailable", "message": "Persistence service unavailable."}
    }


def test_persistence_failure_via_real_service_returns_503():
    """A repository raising (upstream failure) surfaces as a constant 503."""

    class FailingRepo:
        def call(self, rpc_name, params):
            raise RuntimeError(SENTINEL_EXC)

    service = _real_service(FailingRepo())
    app = _make_app(service=service, auth_user_id="user-a")
    client = TestClient(app)
    response = client.post(
        PATHS[OPERATION_DELETE_HISTORY], json={"operation_id": OP_ID}, headers=_auth_headers()
    )
    assert response.status_code == 503
    assert SENTINEL_EXC not in response.text
    assert response.json() == {
        "detail": {"code": "persistence_unavailable", "message": "Persistence service unavailable."}
    }


# ─── 15. Unexpected/internal errors fail closed as sanitized 500 ────────────


def test_unexpected_error_returns_500_sanitized():
    service = RecordingPrivacyService(error=RuntimeError(SENTINEL_EXC))
    app = _make_app(service=service)
    client = TestClient(app)
    response = client.post(
        PATHS[OPERATION_DELETE_HISTORY], json={"operation_id": OP_ID}, headers=_auth_headers()
    )
    assert response.status_code == 500
    assert SENTINEL_EXC not in response.text
    assert response.json() == {
        "detail": {"code": "internal_error", "message": "Internal server error."}
    }


def test_internal_validation_error_is_500_not_422():
    """A #314 ValidationError after HTTP validation (invalid_rpc_result,
    divergent identity, malformed envelope) must fail closed as 500 and never
    be presented as a client 422."""
    divergent = "SECRET-OTHER-USER-MARKER"
    service = RecordingPrivacyService(
        error=ValidationError("invalid_rpc_result", "result user_id does not match the authenticated user")
    )
    app = _make_app(service=service)
    client = TestClient(app)
    response = client.post(
        PATHS[OPERATION_DELETE_HISTORY], json={"operation_id": OP_ID}, headers=_auth_headers()
    )
    assert response.status_code == 500
    assert divergent not in response.text
    assert response.json() == {
        "detail": {"code": "internal_error", "message": "Internal server error."}
    }


def test_unknown_conflict_code_fails_closed_500():
    service = RecordingPrivacyService(
        error=ConflictError(
            code="unknown_conflict_code",
            message="unexpected",
            expected_revision=0,
        )
    )
    app = _make_app(service=service)
    client = TestClient(app)
    response = client.post(
        PATHS[OPERATION_DELETE_HISTORY], json={"operation_id": OP_ID}, headers=_auth_headers()
    )
    assert response.status_code == 500
    assert response.json() == {
        "detail": {"code": "internal_error", "message": "Internal server error."}
    }


# ─── 16-19. Public result never exposes internal fields ─────────────────────


def test_public_projection_model_has_only_approved_fields():
    assert set(PrivacyOperationResponse.model_fields) == {"operation", "status", "counts"}


def test_public_response_json_contains_only_approved_data():
    service = RecordingPrivacyService(
        result=_result(
            OPERATION_DELETE_HISTORY,
            revision=7,
            counts=_counts(chat_logs=3, memories=0),
        )
    )
    app = _make_app(service=service)
    client = TestClient(app)
    response = client.post(
        PATHS[OPERATION_DELETE_HISTORY], json={"operation_id": OP_ID}, headers=_auth_headers()
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"operation", "status", "counts"}
    assert body["operation"] == "delete_history"
    assert body["status"] == "applied"
    assert body["counts"] == _counts(chat_logs=3, memories=0)
    _assert_no_internal_fields(body)


@pytest.mark.parametrize(
    "result",
    [
        _result(OPERATION_DELETE_HISTORY),
        _result(OPERATION_DELETE_MEMORIES),
        _result(OPERATION_RESET_EMOTIONAL_STATE),
        _result(OPERATION_RESET_RELATIONSHIP_STATE),
    ],
)
def test_all_operations_public_json_never_expose_internal_fields(result):
    service = RecordingPrivacyService(result=result)
    app = _make_app(service=service)
    client = TestClient(app)
    path = PATHS[result.operation]
    response = client.post(path, json={"operation_id": OP_ID}, headers=_auth_headers())
    assert response.status_code == 200
    _assert_no_internal_fields(response.json())


# ─── 20. User A cannot provoke operations on user B's data ──────────────────


def test_user_a_cannot_target_user_b_via_body():
    service = RecordingPrivacyService(result=_result(OPERATION_DELETE_HISTORY))
    app = _make_app(service=service, auth_user_id="user-a")
    client = TestClient(app)
    response = client.post(
        PATHS[OPERATION_DELETE_HISTORY],
        json={"operation_id": OP_ID, "user_id": "user-b"},
        headers=_auth_headers(),
    )
    assert response.status_code == 422
    assert service.calls == []


def test_same_operation_id_across_users_stays_isolated():
    """The ledger is keyed per user: user B reusing user A's operation_id is
    an independent operation, and divergent payloads never collide across
    users."""
    repo = LedgerSimulatingRepository(_envelope_from_params)
    service = _real_service(repo)
    asyncio.run(service.delete_history("user-a", OP_ID))
    result_b = asyncio.run(service.delete_history("user-b", OP_ID))
    assert result_b.operation == "delete_history"
    assert repo.mutations == 2


def test_divergent_result_identity_fails_closed_500():
    """A privileged result bound to another identity must fail closed as 500
    and never echo the divergent identity."""
    divergent = "SECRET-OTHER-USER-MARKER"

    class DivergentRepo:
        def call(self, rpc_name, params):
            envelope = _rpc_envelope(rpc_name, op_id=params["p_operation_id"])
            envelope["user_id"] = divergent
            return envelope

    service = _real_service(DivergentRepo())
    app = _make_app(service=service, auth_user_id="user-a")
    client = TestClient(app)
    response = client.post(
        PATHS[OPERATION_DELETE_HISTORY], json={"operation_id": OP_ID}, headers=_auth_headers()
    )
    assert response.status_code == 500
    assert divergent not in response.text


# ─── 21. Sentinels never reach logs or responses ────────────────────────────


def test_sentinels_never_reach_logs_or_responses(caplog):
    service = RecordingPrivacyService(error=RuntimeError(SENTINEL_EXC))
    app = _make_app(service=service, auth_user_id=SENTINEL_USER)
    client = TestClient(app)
    with caplog.at_level(logging.INFO):
        response = client.post(
            PATHS[OPERATION_DELETE_HISTORY],
            json={"operation_id": SENTINEL_OP_ID},
            headers=_auth_headers("SENTINEL-BEARER-TOKEN"),
        )
    assert response.status_code == 500
    assert SENTINEL_USER not in response.text
    assert SENTINEL_OP_ID not in response.text
    assert SENTINEL_EXC not in response.text
    assert "SENTINEL-BEARER-TOKEN" not in response.text
    assert SENTINEL_USER not in caplog.text
    assert SENTINEL_OP_ID not in caplog.text
    assert SENTINEL_EXC not in caplog.text
    assert "SENTINEL-BEARER-TOKEN" not in caplog.text


# ─── 22. No route uses BackgroundTasks / fire-and-forget work ───────────────


def test_no_route_uses_background_tasks():
    app = _make_app(service=RecordingPrivacyService(result=_result(OPERATION_DELETE_HISTORY)))
    for path in [*PATHS.values(), "/privacy/delete-account"]:
        route = next(r for r in app.routes if getattr(r, "path", None) == path)
        calls = [d.call for d in route.dependant.dependencies if d.call is not None]
        assert BackgroundTasks not in calls, f"{path} must not use BackgroundTasks"
    # The service module never schedules fire-and-forget work.
    source = inspect.getsource(privacy_service_module)
    for forbidden in ("BackgroundTasks", "asyncio.create_task", "ThreadPoolExecutor"):
        assert forbidden not in source


# ─── 23. A Supabase write is never abandoned on cancellation ────────────────


def test_write_is_drained_on_cancellation():
    entered = threading.Event()
    release = threading.Event()
    completed = []

    def blocking_call(rpc_name, params):
        entered.set()
        release.wait(10)
        completed.append(rpc_name)
        return _rpc_envelope(rpc_name, op_id=params["p_operation_id"], user_id=params["p_authenticated_user_id"])

    class BlockingRepo:
        def call(self, rpc_name, params):
            return blocking_call(rpc_name, params)

    service = PrivacyService(
        repository=BlockingRepo(),
        turn_config=TurnExecutionConfig.defaults(),
    )

    async def scenario():
        task = asyncio.create_task(service.delete_history("user-a", OP_ID))
        await asyncio.to_thread(entered.wait, 5)
        assert entered.is_set(), "worker never entered the blocking write"
        task.cancel()
        # The cancellation must not abandon the in-flight write: release the
        # worker so the drain loop can complete it before propagating.
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert completed == ["delete_history"], "blocking write must be drained on cancellation"

    asyncio.run(scenario())


# ─── 24-28. Existing routes and contracts remain compatible ─────────────────


def test_history_remains_compatible():
    app = _make_app(
        service=RecordingPrivacyService(result=_result(OPERATION_DELETE_HISTORY)),
        persistence=_FakeSupabase(),
    )
    client = TestClient(app)
    response = client.get("/history", headers=_auth_headers())
    assert response.status_code == 200
    assert response.json() == [{"content": "msg1", "role": "user"}]


def test_live_remains_compatible():
    app = _make_app(service=RecordingPrivacyService(result=_result(OPERATION_DELETE_HISTORY)))
    client = TestClient(app)
    response = client.get("/live")
    assert response.status_code == 200
    assert response.json() == {"status": "live"}


def test_ready_remains_compatible():
    app = _make_app(service=RecordingPrivacyService(result=_result(OPERATION_DELETE_HISTORY)))
    client = TestClient(app)
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"


def test_chat_remains_compatible():
    engine = _FakeChatEngine()
    app = _make_app(
        service=RecordingPrivacyService(result=_result(OPERATION_DELETE_HISTORY)),
        persistence=_ChatSupabase(),
        engine=engine,
    )
    client = TestClient(app)
    response = client.post(
        "/chat",
        json={"request_id": VALID_CHAT_REQUEST_ID, "message": "hello"},
        headers=_auth_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["response"] == "compat response"
    assert body["emotion_state"]["schema_version"] == 1
    assert len(engine.turn_calls) == 1
    assert engine.turn_calls[0][0] == "user-123"


def test_chat_response_contract_unchanged():
    assert set(main_module.ChatResponse.model_fields) == {"response", "emotion_state"}


def test_create_app_still_has_no_heavy_import_side_effects():
    """Importing the app module builds no dependencies and starts no threads;
    the factory defers heavy construction to the lifespan (also covered by
    test_import_safety)."""
    app = main_module.create_app(settings=_settings())
    assert app.state.dependencies is None
    assert app.state.lifespan_started is False
    assert app.state.owned_resources == ()


_PURITY_SCRIPT = textwrap.dedent(
    """
    import sys
    import threading

    import socket as _socket

    def _forbid(*args, **kwargs):
        raise AssertionError("network socket usage during import")

    _socket.socket.connect = _forbid
    _socket.socket.connect_ex = _forbid
    _socket.create_connection = _forbid

    import supabase as _supabase

    def _no_supabase_client(*args, **kwargs):
        raise AssertionError("real Supabase client constructed during import")

    _supabase.create_client = _no_supabase_client

    threads_before = len(threading.enumerate())

    import backend.privacy_service

    threads_after = len(threading.enumerate())

    assert threads_after == threads_before, "import started a thread"
    print("PRIVACY_SERVICE_PURITY_OK")
    """
)


def test_privacy_service_import_is_pure():
    result = subprocess.run(
        [sys.executable, "-c", _PURITY_SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PYTHONPATH": ".", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "PRIVACY_SERVICE_PURITY_OK" in result.stdout
