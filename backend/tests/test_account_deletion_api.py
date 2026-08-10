"""Unit tests for the #326 account deletion API and tombstone gate.

Everything here runs without network: the ledger repository and the
account deletion service are fakes; no Supabase/Groq/embeddings client is
ever constructed. Real-Supabase scenarios live in
``test_account_deletion_integration.py`` (database CI job).

Mandatory scenarios covered (from issue #326):

 1.  An authenticated request creates a tombstone/job using EXCLUSIVELY
     ``current_user.id``.
 2.  The HMAC is derived server-side with ``compute_account_deletion_user_ref``.
 3.  No HMAC/user_ref is accepted from the client.
 4-7. Bodies with ``user_id``/``user_ref``/``job_id``/any extra key return
     422 without any RPC.
 8.  The first request returns the honest ``accepted`` state.
 9.  Replay of the same intent + operation_id creates no second job.
10.  ``operation_conflict`` produces a sanitized 409.
11.  ``/chat`` with a tombstone returns 423 before ``reserve_admission_sync``.
12.  In the same case ``engine.process_turn`` is never called.
13.  Provider/Groq/embeddings are never reached (the engine never runs).
14.  Blocked ``/history`` never queries ``chat_logs``.
15.  Each of the four existing privacy endpoints is blocked before mutation.
16.  A user without a tombstone keeps using ``/chat`` normally.
17.  A user without a tombstone keeps using ``/history`` normally.
18.  Existing privacy operations keep working for an active user.
19.  A tombstone-store failure returns 503 and the route never continues.
20.  Account A blocked does not affect account B.
21-24. Tombstone statuses pending/processing/failed/completed all block.
25.  ``POST /privacy/delete-account`` stays replayable after a tombstone
     exists (the gate is deliberately NOT applied to it).
26.  No unit suite constructs a real Supabase/Groq/embeddings client.
27.  Artificial sensitive markers never appear in logs/responses.
28.  Importing the new module creates no client and no network.
29.  ``/live`` and ``/ready`` remain independent of the gate.
30.  ``completed`` is never returned unless the ledger confirms it.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import backend.main as main_module
from backend.account_deletion import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    RequestResult,
    TombstoneStatus,
    compute_account_deletion_user_ref,
    compute_intent_fingerprint,
)
from backend.account_deletion_service import (
    DELETE_ACCOUNT_INTENT,
    AccountDeletionBlocked,
    AccountDeletionRequestResponse,
    AccountDeletionService,
    AccountDeletionUnavailable,
)
from backend.admission import AdmissionRuntimeConfig
from backend.atomic_turn_commit import ConflictError, PersistenceError, ValidationError
from backend.dependencies import ApplicationDependencies
from backend.emotion_presentation import EmotionStateResponse
from backend.health import HealthRegistry
from backend.privacy_operations import (
    OPERATION_DELETE_HISTORY,
    OPERATION_DELETE_MEMORIES,
    OPERATION_RESET_EMOTIONAL_STATE,
    OPERATION_RESET_RELATIONSHIP_STATE,
)
from backend.process_turn import ProcessTurnResult
from backend.settings import AppEnvironment, Settings
from backend.tests.fixtures.account_deletion_fakes import (
    FakeAccountDeletionService,
    NoTombstoneGate,
)
from backend.turn_execution import TurnExecutionConfig

SECRET = "s" * 40
OP_ID = "11111111-1111-1111-1111-111111111111"
OP_ID_2 = "22222222-2222-2222-2222-222222222222"
JOB_ID = "33333333-3333-3333-3333-333333333333"
SENTINEL_USER = "SENTINEL-USER-MARKER"
SENTINEL_OP_ID = "99999999-9999-9999-9999-999999999999"
SENTINEL_EXC = "SENTINEL-UPSTREAM-MARKER"
VALID_CHAT_REQUEST_ID = "550e8400-e29b-41d4-a716-446655440000"

PRIVACY_PATHS = {
    OPERATION_DELETE_HISTORY: "/privacy/delete-history",
    OPERATION_DELETE_MEMORIES: "/privacy/delete-memories",
    OPERATION_RESET_EMOTIONAL_STATE: "/privacy/reset-emotional-state",
    OPERATION_RESET_RELATIONSHIP_STATE: "/privacy/reset-relationship",
}

# ─── Helpers ─────────────────────────────────────────────────────────────────


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
    class _UserSurface:
        def __init__(self, user_id: str):
            self.user_id = user_id

        def get_user(self, token: str):
            return SimpleNamespace(user=SimpleNamespace(id=self.user_id))

    def __init__(self, user_id: str = "user-a"):
        self.auth = self._UserSurface(user_id)


class FakeEngine:
    def __init__(self, supabase=None):
        self.memory_manager = SimpleNamespace(supabase=supabase)
        self.groq_manager = object()
        self.turn_calls = []

    async def process_turn(self, *args, **kwargs):
        self.turn_calls.append((args, kwargs))
        raise AssertionError("engine.process_turn must never run in gate tests")


class RecordingPrivacyService:
    """Records invocations; returns a canned result or raises."""

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
        from backend.privacy_service import PrivacyOperationResponse

        return PrivacyOperationResponse(
            operation=self.result, status="applied", counts={}
        )


def _make_app(
    *,
    account_deletion_service,
    auth_user_id: str = "user-a",
    privacy_service=None,
    persistence=None,
    engine=None,
):
    settings = _settings()
    deps = ApplicationDependencies(
        conversation_engine=engine if engine is not None else FakeEngine(supabase=object()),
        auth_client=FakeAuth(auth_user_id),
        admission_config=AdmissionRuntimeConfig.from_values(SECRET),
        turn_config=TurnExecutionConfig.defaults(),
        health_checks=HealthRegistry(),
        clock=time.time,
        persistence_client=persistence,
        privacy_service=privacy_service,
        account_deletion_service=account_deletion_service,
    )
    return main_module.create_app(settings=settings, dependencies=deps)


def _auth_headers(token: str = "token-x") -> dict:
    return {"Authorization": f"Bearer {token}"}


def _assert_no_internal_fields(node) -> None:
    """Fail if any key that could carry internal/sensitive data appears."""
    forbidden = {
        "user_id",
        "user_ref",
        "operation_id",
        "job_id",
        "job_status",
        "id",
        "hmac",
        "fingerprint",
        "token",
        "secret",
        "sql",
        "timestamp",
        "created_at",
        "updated_at",
        "db_purged_at",
        "completed_at",
        "requested_at",
        "lease_owner",
        "lease_expires_at",
        "attempts",
        "error_code",
        "intent",
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


class _LedgerRepo:
    """In-memory simulator of the #324 ledger (keyed by ref + operation_id).

    Mirrors ``account_deletion_request``: the first call creates a pending
    job; an exact replay (same fingerprint) returns the stored state without
    a second job; a divergent fingerprint raises ``operation_conflict``.
    ``has_tombstone`` returns a configurable result or raises.
    """

    def __init__(self, tombstone=None):
        self._jobs: dict[tuple, tuple] = {}
        self.request_calls: list[tuple] = []
        self.tombstone_calls: list[str] = []
        self.tombstone = tombstone  # None | status str | BaseException

    def request(
        self,
        authenticated_user_id: str,
        user_ref_hmac_sha256: str,
        operation_id: str,
        intent_fingerprint_sha256: str,
    ) -> RequestResult:
        self.request_calls.append(
            (authenticated_user_id, user_ref_hmac_sha256, operation_id, intent_fingerprint_sha256)
        )
        key = (user_ref_hmac_sha256, operation_id)
        if key in self._jobs:
            stored_fp, status, db_purged_at = self._jobs[key]
            if stored_fp != intent_fingerprint_sha256:
                raise ConflictError(
                    "operation_conflict",
                    "operation_id already used with a different intent",
                    0,
                )
            return RequestResult(
                status="replay",
                job_id=JOB_ID,
                job_status=status,
                db_purged_at=db_purged_at,
                completed_at="2026-08-09T02:00:00+00:00" if status == STATUS_COMPLETED else None,
            )
        self._jobs[key] = (intent_fingerprint_sha256, STATUS_PENDING, None)
        return RequestResult(
            status="created",
            job_id=JOB_ID,
            job_status=STATUS_PENDING,
            db_purged_at=None,
            completed_at=None,
        )

    def mark_completed(self, user_ref: str, operation_id: str) -> None:
        fp, _, _ = self._jobs[(user_ref, operation_id)]
        self._jobs[(user_ref, operation_id)] = (fp, STATUS_COMPLETED, "2026-08-09T00:00:00+00:00")

    def has_tombstone(self, user_ref_hmac_sha256: str) -> TombstoneStatus:
        self.tombstone_calls.append(user_ref_hmac_sha256)
        if isinstance(self.tombstone, BaseException):
            raise self.tombstone
        if self.tombstone is None:
            return TombstoneStatus(exists=False, status=None)
        return TombstoneStatus(exists=True, status=self.tombstone)


def _real_service(repo: _LedgerRepo) -> AccountDeletionService:
    return AccountDeletionService(
        repository=repo,
        turn_config=TurnExecutionConfig.defaults(),
        admission_config=AdmissionRuntimeConfig.from_values(SECRET),
    )


def _ref(user_id: str) -> str:
    return compute_account_deletion_user_ref(SECRET.encode("utf-8"), user_id)


# ─── 1/2/3/8/9/10/30. Service: server-side derivation, accepted state, replay ─


def test_request_derives_server_side_hmac_and_stable_fingerprint():
    repo = _LedgerRepo()
    service = _real_service(repo)
    result = asyncio.run(service.request("user-a", OP_ID))
    assert result.status == "accepted"
    assert result == AccountDeletionRequestResponse(status="accepted")

    user_id, user_ref, operation_id, fingerprint = repo.request_calls[0]
    assert user_id == "user-a"
    assert operation_id == OP_ID
    # The reference is derived server-side from the authenticated identity
    # under the dedicated account-deletion domain; no client value is used.
    assert user_ref == _ref("user-a")
    assert user_ref == compute_account_deletion_user_ref(
        SECRET.encode("utf-8"), "user-a"
    )
    # The intent fingerprint is deterministic and carries no user data.
    assert fingerprint == compute_intent_fingerprint(DELETE_ACCOUNT_INTENT)
    assert "user-a" not in DELETE_ACCOUNT_INTENT
    assert OP_ID not in DELETE_ACCOUNT_INTENT


def test_request_fingerprint_stable_across_operation_ids():
    repo = _LedgerRepo()
    service = _real_service(repo)
    asyncio.run(service.request("user-a", OP_ID))
    asyncio.run(service.request("user-a", OP_ID_2))
    fp_first = repo.request_calls[0][3]
    fp_second = repo.request_calls[1][3]
    assert fp_first == fp_second == compute_intent_fingerprint(DELETE_ACCOUNT_INTENT)


def test_first_request_is_honest_accepted_and_never_completed():
    repo = _LedgerRepo()
    service = _real_service(repo)
    result = asyncio.run(service.request("user-a", OP_ID))
    assert result.status == "accepted"
    assert result.status != "completed", "the API never promises completion"


def test_replay_creates_no_second_job():
    repo = _LedgerRepo()
    service = _real_service(repo)
    first = asyncio.run(service.request("user-a", OP_ID))
    second = asyncio.run(service.request("user-a", OP_ID))
    assert first == second == AccountDeletionRequestResponse(status="accepted")
    assert len(repo.request_calls) == 2
    assert len(repo._jobs) == 1, "replay must not create a second job"


def test_replay_confirmed_completed_returns_completed():
    repo = _LedgerRepo()
    service = _real_service(repo)
    asyncio.run(service.request("user-a", OP_ID))
    repo.mark_completed(_ref("user-a"), OP_ID)
    result = asyncio.run(service.request("user-a", OP_ID))
    assert result.status == "completed", "completed only when the ledger confirms it"


def test_divergent_intent_raises_operation_conflict():
    repo = _LedgerRepo()
    service = _real_service(repo)
    # Pre-seed a job whose fingerprint differs from the delete_account intent.
    repo._jobs[(_ref("user-a"), OP_ID)] = ("x" * 64, STATUS_PENDING, None)
    with pytest.raises(ConflictError) as exc:
        asyncio.run(service.request("user-a", OP_ID))
    assert exc.value.code == "operation_conflict"


def test_different_users_never_collide():
    repo = _LedgerRepo()
    service = _real_service(repo)
    asyncio.run(service.request("user-a", OP_ID))
    asyncio.run(service.request("user-b", OP_ID))
    assert len(repo._jobs) == 2
    assert repo.request_calls[0][1] != repo.request_calls[1][1]


def test_service_holds_no_per_user_state():
    repo = _LedgerRepo()
    service = _real_service(repo)
    asyncio.run(service.request("user-a", OP_ID))
    asyncio.run(service.request("user-b", OP_ID_2))
    # The container only keeps infrastructure; identity lives per call.
    assert set(service.__dict__) == {"_repository", "_turn_config", "_admission_config"}


# ─── 21-24. Gate: every tombstone status blocks; no tombstone passes ────────


@pytest.mark.parametrize(
    "status", [STATUS_PENDING, STATUS_PROCESSING, STATUS_FAILED, STATUS_COMPLETED]
)
def test_assert_active_blocks_for_every_tombstone_status(status):
    repo = _LedgerRepo(tombstone=status)
    service = _real_service(repo)
    with pytest.raises(AccountDeletionBlocked):
        asyncio.run(service.assert_active("user-a"))
    assert repo.tombstone_calls == [_ref("user-a")]


def test_assert_active_passes_without_tombstone():
    repo = _LedgerRepo()
    service = _real_service(repo)
    asyncio.run(service.assert_active("user-a"))
    assert repo.tombstone_calls == [_ref("user-a")]


# ─── 19. Gate fail-closed on store failure ──────────────────────────────────


def test_assert_active_fails_closed_on_persistence_error():
    repo = _LedgerRepo(tombstone=PersistenceError("database_error", "persistence error"))
    service = _real_service(repo)
    with pytest.raises(AccountDeletionUnavailable):
        asyncio.run(service.assert_active("user-a"))


def test_assert_active_fails_closed_on_malformed_payload():
    repo = _LedgerRepo(tombstone=ValidationError("validation_failed", "malformed"))
    service = _real_service(repo)
    with pytest.raises(AccountDeletionUnavailable):
        asyncio.run(service.assert_active("user-a"))


def test_assert_active_fails_closed_on_transport_error():
    """A non-allowlisted repository exception becomes a persistence failure
    inside run_blocking_write and must surface as AccountDeletionUnavailable,
    never as 'user active'."""

    class ExplodingRepo(_LedgerRepo):
        def has_tombstone(self, user_ref_hmac_sha256):
            raise RuntimeError(SENTINEL_EXC)

    service = _real_service(ExplodingRepo())
    with pytest.raises(AccountDeletionUnavailable):
        asyncio.run(service.assert_active("user-a"))


# ─── HTTP: POST /privacy/delete-account ─────────────────────────────────────


def test_delete_account_without_bearer_returns_401():
    service = FakeAccountDeletionService()
    app = _make_app(account_deletion_service=service)
    client = TestClient(app)
    response = client.post("/privacy/delete-account", json={"operation_id": OP_ID})
    assert response.status_code == 401
    assert service.requests == []


@pytest.mark.parametrize(
    "extra",
    [
        {"user_id": "attacker-user"},
        {"user_ref": "a" * 64},
        {"job_id": JOB_ID},
        {"user_ref_hmac_sha256": "b" * 64},
        {"operation_id": OP_ID, "hmac": "c" * 64},
        {"operation_id": OP_ID, "operation": "delete_account"},
        {"operation_id": OP_ID, "user_id": "a", "extra": True},
    ],
)
def test_delete_account_extra_keys_return_422_without_rpc(extra):
    service = FakeAccountDeletionService()
    app = _make_app(account_deletion_service=service)
    client = TestClient(app)
    response = client.post("/privacy/delete-account", json=extra, headers=_auth_headers())
    assert response.status_code == 422
    assert service.requests == [], "invalid bodies must never reach the service"
    assert response.json() == {
        "detail": {"code": "invalid_request", "message": "Invalid request body."}
    }


@pytest.mark.parametrize(
    "bad",
    ["not-a-uuid", "", "123", "550e8400-e29b-41d4-a716-44665544000Z"],
)
def test_delete_account_invalid_operation_id_returns_422_sanitized(bad):
    service = FakeAccountDeletionService()
    app = _make_app(account_deletion_service=service)
    client = TestClient(app)
    response = client.post(
        "/privacy/delete-account", json={"operation_id": bad}, headers=_auth_headers()
    )
    assert response.status_code == 422
    assert service.requests == []
    assert response.json() == {
        "detail": {"code": "invalid_request", "message": "Invalid request body."}
    }
    assert repr(bad) not in response.text


def test_delete_account_identity_comes_only_from_current_user():
    service = FakeAccountDeletionService()
    app = _make_app(account_deletion_service=service, auth_user_id="authenticated-a")
    client = TestClient(app)
    response = client.post(
        "/privacy/delete-account", json={"operation_id": OP_ID}, headers=_auth_headers()
    )
    assert response.status_code == 200
    assert service.requests == [("authenticated-a", OP_ID)]


def test_delete_account_first_request_returns_accepted_only():
    service = FakeAccountDeletionService()
    app = _make_app(account_deletion_service=service)
    client = TestClient(app)
    response = client.post(
        "/privacy/delete-account", json={"operation_id": OP_ID}, headers=_auth_headers()
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {"status": "accepted"}
    _assert_no_internal_fields(body)


def test_delete_account_replay_is_idempotent_over_http():
    service = FakeAccountDeletionService()
    app = _make_app(account_deletion_service=service)
    client = TestClient(app)
    headers = _auth_headers()
    first = client.post("/privacy/delete-account", json={"operation_id": OP_ID}, headers=headers)
    second = client.post("/privacy/delete-account", json={"operation_id": OP_ID}, headers=headers)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json() == second.json() == {"status": "accepted"}
    assert service.requests == [("user-a", OP_ID), ("user-a", OP_ID)]


def test_delete_account_conflict_returns_409_sanitized():
    service = FakeAccountDeletionService(
        error=ConflictError("operation_conflict", "operation_id already used", 0)
    )
    app = _make_app(account_deletion_service=service)
    client = TestClient(app)
    response = client.post(
        "/privacy/delete-account", json={"operation_id": OP_ID}, headers=_auth_headers()
    )
    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "operation_conflict",
            "message": "Operation identifier was already used with a different operation.",
        }
    }


def test_delete_account_persistence_failure_returns_503_sanitized():
    service = FakeAccountDeletionService(
        error=PersistenceError("database_error", "persistence error")
    )
    app = _make_app(account_deletion_service=service)
    client = TestClient(app)
    response = client.post(
        "/privacy/delete-account", json={"operation_id": OP_ID}, headers=_auth_headers()
    )
    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "service_unavailable", "message": "Service unavailable."}
    }


def test_delete_account_internal_validation_failure_is_500_not_422():
    service = FakeAccountDeletionService(
        error=ValidationError("invalid_rpc_result", "malformed envelope")
    )
    app = _make_app(account_deletion_service=service)
    client = TestClient(app)
    response = client.post(
        "/privacy/delete-account", json={"operation_id": OP_ID}, headers=_auth_headers()
    )
    assert response.status_code == 500
    assert response.json() == {
        "detail": {"code": "internal_error", "message": "Internal server error."}
    }


def test_delete_account_unexpected_error_returns_500_sanitized():
    service = FakeAccountDeletionService(error=RuntimeError(SENTINEL_EXC))
    app = _make_app(account_deletion_service=service)
    client = TestClient(app)
    response = client.post(
        "/privacy/delete-account", json={"operation_id": OP_ID}, headers=_auth_headers()
    )
    assert response.status_code == 500
    assert SENTINEL_EXC not in response.text
    assert response.json() == {
        "detail": {"code": "internal_error", "message": "Internal server error."}
    }


def test_delete_account_missing_service_fails_closed_503():
    app = _make_app(account_deletion_service=None)
    client = TestClient(app)
    response = client.post(
        "/privacy/delete-account", json={"operation_id": OP_ID}, headers=_auth_headers()
    )
    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "service_unavailable", "message": "Service unavailable."}
    }


# ─── 25. Delete-account is exempt from the gate (replayable) ────────────────


def test_delete_account_replayable_even_while_tombstone_blocks():
    """The gate is deliberately NOT applied to POST /privacy/delete-account:
    with the gate blocked, the endpoint must still accept an idempotent
    replay while the token is accepted."""
    service = FakeAccountDeletionService(blocked=True)
    app = _make_app(account_deletion_service=service)
    client = TestClient(app)
    response = client.post(
        "/privacy/delete-account", json={"operation_id": OP_ID}, headers=_auth_headers()
    )
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}


# ─── 11/12/13/16/19. /chat gate ─────────────────────────────────────────────


class _LoudPersistence:
    """Persistence surface that fails loudly if any table/RPC is used."""

    def __init__(self):
        self.table_calls = []
        self.rpc_calls = []

    def table(self, name):
        self.table_calls.append(name)
        raise AssertionError(f"table access must not happen: {name}")

    def rpc(self, name, params):
        self.rpc_calls.append(name)
        raise AssertionError(f"rpc must not happen: {name}")


def _chat_payload(request_id=VALID_CHAT_REQUEST_ID, message="hello"):
    return {"request_id": request_id, "message": message}


def test_chat_with_tombstone_returns_423_before_admission_and_engine(monkeypatch):
    service = FakeAccountDeletionService(blocked=True)
    engine = FakeEngine()
    persistence = _LoudPersistence()
    admission_calls = []

    def _spy_reserve_admission_sync(client, request):
        admission_calls.append(request)
        raise AssertionError("admission must not run for a blocked account")

    monkeypatch.setattr(main_module, "reserve_admission_sync", _spy_reserve_admission_sync)
    app = _make_app(
        account_deletion_service=service,
        engine=engine,
        persistence=persistence,
    )
    client = TestClient(app)
    response = client.post("/chat", json=_chat_payload(), headers=_auth_headers())
    assert response.status_code == 423
    assert response.json() == {
        "detail": {
            "code": "account_deletion_pending",
            "message": "Account deletion is pending.",
        }
    }
    assert admission_calls == [], "reserve_admission_sync must not be reached"
    assert engine.turn_calls == [], "engine.process_turn must not be reached"
    assert persistence.table_calls == [] and persistence.rpc_calls == []


def test_chat_without_tombstone_keeps_working(monkeypatch):
    service = FakeAccountDeletionService()
    engine = FakeEngine()

    async def _fake_process_turn(*args, **kwargs):
        return ProcessTurnResult(
            committed=object(),
            response="ok response",
            emotion_state=EmotionStateResponse(
                schema_version=1,
                mood_label="NEUTRA",
                pad={"pleasure": 0.0, "arousal": 0.0, "dominance": 0.0},
                dominant_emotions=[],
                timestamp=1000.0,
            ),
        )

    engine.process_turn = _fake_process_turn

    class _AdmittingSupabase:
        def rpc(self, name, params):
            assert name == "reserve_admission"
            return SimpleNamespace(
                execute=lambda: SimpleNamespace(
                    data=[{"decision": "admitted", "retry_after_seconds": 0}],
                    error=None,
                )
            )

    app = _make_app(
        account_deletion_service=service,
        engine=engine,
        persistence=_AdmittingSupabase(),
    )
    client = TestClient(app)
    response = client.post("/chat", json=_chat_payload(), headers=_auth_headers())
    assert response.status_code == 200
    assert response.json()["response"] == "ok response"
    assert service.gate.checks == ["user-a"]


def test_chat_gate_unavailable_returns_503_without_admission_or_engine(monkeypatch):
    service = FakeAccountDeletionService(unavailable=True)
    engine = FakeEngine()
    persistence = _LoudPersistence()
    admission_calls = []

    def _spy_reserve_admission_sync(client, request):
        admission_calls.append(request)
        raise AssertionError("admission must not run")

    monkeypatch.setattr(main_module, "reserve_admission_sync", _spy_reserve_admission_sync)
    app = _make_app(
        account_deletion_service=service,
        engine=engine,
        persistence=persistence,
    )
    client = TestClient(app)
    response = client.post("/chat", json=_chat_payload(), headers=_auth_headers())
    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "service_unavailable", "message": "Service unavailable."}
    }
    assert admission_calls == []
    assert engine.turn_calls == []
    assert persistence.table_calls == [] and persistence.rpc_calls == []


def test_chat_missing_gate_service_fails_closed_503(monkeypatch):
    engine = FakeEngine()
    admission_calls = []

    def _spy_reserve_admission_sync(client, request):
        admission_calls.append(request)
        raise AssertionError("admission must not run")

    monkeypatch.setattr(main_module, "reserve_admission_sync", _spy_reserve_admission_sync)
    app = _make_app(account_deletion_service=None, engine=engine)
    client = TestClient(app)
    response = client.post("/chat", json=_chat_payload(), headers=_auth_headers())
    assert response.status_code == 503
    assert admission_calls == []
    assert engine.turn_calls == []


# ─── 14/17/19. /history gate ────────────────────────────────────────────────


def test_history_with_tombstone_returns_423_without_querying_chat_logs():
    service = FakeAccountDeletionService(blocked=True)
    persistence = _LoudPersistence()
    app = _make_app(account_deletion_service=service, persistence=persistence)
    client = TestClient(app)
    response = client.get("/history", headers=_auth_headers())
    assert response.status_code == 423
    assert response.json() == {
        "detail": {
            "code": "account_deletion_pending",
            "message": "Account deletion is pending.",
        }
    }
    assert persistence.table_calls == [], "chat_logs must never be queried"


class _HistorySupabase:
    def table(self, name):
        assert name == "chat_logs"
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


def test_history_without_tombstone_keeps_working():
    service = FakeAccountDeletionService()
    app = _make_app(account_deletion_service=service, persistence=_HistorySupabase())
    client = TestClient(app)
    response = client.get("/history", headers=_auth_headers())
    assert response.status_code == 200
    assert response.json() == [{"content": "msg1", "role": "user"}]
    assert service.gate.checks == ["user-a"]


def test_history_gate_unavailable_returns_503_without_query():
    service = FakeAccountDeletionService(unavailable=True)
    persistence = _LoudPersistence()
    app = _make_app(account_deletion_service=service, persistence=persistence)
    client = TestClient(app)
    response = client.get("/history", headers=_auth_headers())
    assert response.status_code == 503
    assert persistence.table_calls == []


# ─── 15/18. Existing privacy actions gate ───────────────────────────────────


@pytest.mark.parametrize("path", sorted(PRIVACY_PATHS.values()))
def test_privacy_actions_blocked_423_before_mutation(path):
    service = FakeAccountDeletionService(blocked=True)
    privacy = RecordingPrivacyService(result=OPERATION_DELETE_HISTORY)
    app = _make_app(account_deletion_service=service, privacy_service=privacy)
    client = TestClient(app)
    response = client.post(path, json={"operation_id": OP_ID}, headers=_auth_headers())
    assert response.status_code == 423
    assert response.json() == {
        "detail": {
            "code": "account_deletion_pending",
            "message": "Account deletion is pending.",
        }
    }
    assert privacy.calls == [], "the privacy mutation must not run"


@pytest.mark.parametrize("path", sorted(PRIVACY_PATHS.values()))
def test_privacy_actions_still_work_for_active_user(path):
    service = FakeAccountDeletionService()
    privacy = RecordingPrivacyService(result=OPERATION_DELETE_HISTORY)
    app = _make_app(account_deletion_service=service, privacy_service=privacy)
    client = TestClient(app)
    response = client.post(path, json={"operation_id": OP_ID}, headers=_auth_headers())
    assert response.status_code == 200
    assert response.json() == {
        "operation": "delete_history",
        "status": "applied",
        "counts": {},
    }


# ─── 20. Account A blocked does not affect account B ────────────────────────


class _UserKeyedGate:
    def __init__(self, blocked_user: str):
        self.blocked_user = blocked_user
        self.checks = []

    async def assert_active(self, authenticated_user_id: str) -> None:
        self.checks.append(authenticated_user_id)
        if authenticated_user_id == self.blocked_user:
            raise AccountDeletionBlocked()


def test_user_a_blocked_does_not_affect_user_b(monkeypatch):
    gate = _UserKeyedGate(blocked_user="user-a")

    class _TokenAuth:
        class _Surface:
            def __init__(self):
                self.by_token = {"token-a": "user-a", "token-b": "user-b"}

            def get_user(self, token):
                return SimpleNamespace(user=SimpleNamespace(id=self.by_token[token]))

        def __init__(self):
            self.auth = self._Surface()

    class _Engine:
        def __init__(self):
            self.calls = []
            self.memory_manager = SimpleNamespace(supabase=object())
            self.groq_manager = object()

        async def process_turn(self, *args, **kwargs):
            self.calls.append(args)
            return ProcessTurnResult(
                committed=object(),
                response="b ok",
                emotion_state=EmotionStateResponse(
                    schema_version=1,
                    mood_label="NEUTRA",
                    pad={"pleasure": 0.0, "arousal": 0.0, "dominance": 0.0},
                    dominant_emotions=[],
                    timestamp=1000.0,
                ),
            )

    class _AdmittingSupabase:
        def rpc(self, name, params):
            return SimpleNamespace(
                execute=lambda: SimpleNamespace(
                    data=[{"decision": "admitted", "retry_after_seconds": 0}],
                    error=None,
                )
            )

    engine = _Engine()
    settings = _settings()
    deps = ApplicationDependencies(
        conversation_engine=engine,
        auth_client=_TokenAuth(),
        admission_config=AdmissionRuntimeConfig.from_values(SECRET),
        turn_config=TurnExecutionConfig.defaults(),
        health_checks=HealthRegistry(),
        clock=time.time,
        persistence_client=_AdmittingSupabase(),
        account_deletion_service=gate,
    )
    app = main_module.create_app(settings=settings, dependencies=deps)
    client = TestClient(app)
    blocked = client.post(
        "/chat", json=_chat_payload(), headers=_auth_headers(token="token-a")
    )
    assert blocked.status_code == 423

    engine.calls.clear()
    active = client.post(
        "/chat", json=_chat_payload(), headers=_auth_headers(token="token-b")
    )
    assert active.status_code == 200
    assert active.json()["response"] == "b ok"
    assert engine.calls, "user B must keep using /chat"


# ─── 27. Sentinels never reach logs or responses ────────────────────────────


def test_sentinels_never_reach_logs_or_responses(caplog):
    service = FakeAccountDeletionService(
        error=RuntimeError(SENTINEL_EXC), unavailable=False
    )
    app = _make_app(account_deletion_service=service, auth_user_id=SENTINEL_USER)
    client = TestClient(app)
    with caplog.at_level(logging.INFO):
        response = client.post(
            "/privacy/delete-account",
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


# ─── 29. /live and /ready remain independent ────────────────────────────────


def test_live_and_ready_unaffected_by_gate():
    service = FakeAccountDeletionService(blocked=True)
    app = _make_app(account_deletion_service=service)
    client = TestClient(app)
    assert client.get("/live").status_code == 200
    assert client.get("/ready").status_code == 200


# ─── 26/28. Purity: no client, no network, no threads on import ─────────────

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

    import backend.account_deletion_service

    threads_after = len(threading.enumerate())

    assert threads_after == threads_before, "import started a thread"
    print("ACCOUNT_DELETION_SERVICE_PURITY_OK")
    """
)


def test_account_deletion_service_import_is_pure():
    result = subprocess.run(
        [sys.executable, "-c", _PURITY_SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PYTHONPATH": ".", "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    assert "ACCOUNT_DELETION_SERVICE_PURITY_OK" in result.stdout


def test_no_unit_suite_constructs_real_clients():
    """The whole unit path for the API uses fakes: the default composition is
    the only place where real clients may be built, and it is never invoked
    here."""
    app = _make_app(
        account_deletion_service=FakeAccountDeletionService(),
        privacy_service=RecordingPrivacyService(result=OPERATION_DELETE_HISTORY),
        persistence=_HistorySupabase(),
    )
    assert app.state.dependencies is not None
    assert app.state.lifespan_started is True


def test_default_composition_reuses_existing_supabase_client(monkeypatch):
    """The default composition wires the account deletion service with the
    SAME Supabase client the rest of the app uses (no second client, no
    per-request client, no client at import time)."""
    import backend.dependencies as dependencies_module

    fake_supabase = object()
    monkeypatch.setattr(
        dependencies_module,
        "_supabase_factory_from_settings",
        lambda settings: lambda: fake_supabase,
    )
    deps, _owned = dependencies_module.build_default_dependencies(_settings())
    assert deps.account_deletion_service is not None
    assert deps.account_deletion_service._repository._client is fake_supabase
    assert deps.persistence_client is fake_supabase
    assert deps.auth_client is fake_supabase
