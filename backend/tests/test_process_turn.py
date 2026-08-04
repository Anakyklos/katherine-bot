"""
Unit tests for the ProcessTurn use case and its repositories (#272).

Everything is tested with fake repositories, a fake provider and a fake
context loader — no database, no network, no provider. Coverage:

 1. Loaded state includes revision; missing profile yields defaults + rev 0
    with NO insert; duplicate rows / invalid revision fail closed
 2. Nominal path commits exactly once with the loaded revision
 3. Nominal path writes only through the commit repository (never
    save_turn / sync_state, never BackgroundTasks)
 4. Result comes from the persisted CommittedTurn, not pre-commit variables
 5. Replay returns the persisted result without the provider
 6. Replay does not run transitions / appraisal / context loading
 7. Replay semantics: pending with ACTIVE lease -> request_in_progress;
    pending with EXPIRED lease and expired -> request_replay_unavailable;
    no provider when the policy does not authorize recomputation; replay
    outcomes are terminal (no replay<->normal loop)
 8. Same-ID conflicts never retry the provider
 9. revision_mismatch retries exactly once, reloads state + context and
    recomputes a fresh response; a third attempt never occurs
10. Provider failure produces no committed turn
11. Deadline prevents retry
12. Lease owner is unique per instance, stable for the instance, sanitized
    and never shared / never logged
13. Cancellation before commit writes nothing; cancellation DURING commit
    drains a stateful write to completion before propagating the original
    CancelledError; replay recovers exactly the written result; a worker
    failing after cancellation never leaves an unretrieved task exception
14. Outbox event is idempotent and sanitized (no content / identity)
15. Correlation is the sanitized HMAC of the canonical request id, present
    in every ProcessTurn event, consistent across attempts and never the
    raw request id; replay and commit events are semantically distinct
16. Logs and errors never contain sensitive markers
"""

from __future__ import annotations

import asyncio
import logging
import re as _re
import threading
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Mapping, Optional

import pytest

from backend.atomic_turn_commit import (
    CommittedTurn,
    ConflictError,
    PersistenceError,
    ValidationError,
)
from backend.emotion_presentation import EmotionStateResponse
from backend.emotional_domain import (
    AppraisalV1,
    EmotionalStateV1,
    TransitionConfig,
)
from backend.process_turn import (
    LEASE_OWNER_PREFIX,
    MAX_COMMIT_ATTEMPTS,
    ProcessTurn,
    ProcessTurnInput,
    ProcessTurnResult,
    TurnMode,
    build_archival_outbox_event,
    new_lease_owner,
    parse_public_result,
)
from backend.relationship import (
    RelationshipStateV1,
    RelationshipTransitionConfig,
)
from backend.turn_execution import (
    DeadlineExceeded,
    TurnBudget,
    TurnErrorCode,
    TurnExecutionError,
)
from backend.turn_repositories import (
    REPLAY_STATUS_IN_PROGRESS,
    REPLAY_STATUS_UNAVAILABLE,
    LoadedUserState,
    ReplayOutcome,
    UserStateRepository,
    parse_replay_committed_turn_result,
)
from backend.trusted_context import LoadedContextData

UUID = "550e8400-e29b-41d4-a716-446655440000"
MESSAGE_ID = "87654321-4321-4321-4321-cba987654321"
CORRELATION = "c" * 64

_LEASE_OWNER_RE = _re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _budget() -> TurnBudget:
    return TurnBudget(deadline=1000.0, reserve=10.0, now_provider=lambda: 0.0)


def _emotion_dict() -> dict:
    return EmotionStateResponse(
        schema_version=1,
        mood_label="NEUTRA",
        pad={"pleasure": 0.0, "arousal": 0.0, "dominance": 0.0},
        dominant_emotions=[],
        timestamp=1000.0,
    ).model_dump()


def _committed(revision: int = 1, response: str = "persisted response") -> CommittedTurn:
    return CommittedTurn(
        user_id="user-a",
        request_id=UUID,
        committed_revision=revision,
        user_message_id=UUID,
        assistant_message_id=MESSAGE_ID,
        replay_payload={
            "response": response,
            "emotion_state": _emotion_dict(),
            "message_id": MESSAGE_ID,
            "duration_ms": 42,
        },
        outbox_events=(),
        created_at="2024-01-01T00:00:00Z",
        completed_at="2024-01-01T00:00:00Z",
    )


def _default_state(revision: int = 0) -> LoadedUserState:
    return LoadedUserState(
        user_id="user-a",
        revision=revision,
        persona_config="Katherine...",
        user_profile={},
        emotional_state=EmotionalStateV1.neutral(timestamp=1000.0).to_dict(),
        relationship_state=RelationshipStateV1.neutral(timestamp=1000.0).to_dict(),
    )


@dataclass
class FakeStateRepository:
    states: list[LoadedUserState] = field(default_factory=list)
    error: Optional[BaseException] = None
    loads: list[float] = field(default_factory=list)

    def load(self, user_id: str, default_timestamp: float) -> LoadedUserState:
        self.loads.append(default_timestamp)
        if self.error is not None:
            raise self.error
        if self.states:
            return self.states.pop(0)
        return _default_state()


@dataclass
class FakeCommitRepository:
    results: list = field(default_factory=list)
    error: Optional[BaseException] = None
    calls: list[dict] = field(default_factory=list)

    def commit(self, **kwargs: Any) -> CommittedTurn:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if self.results:
            return self.results.pop(0)
        return _committed()


@dataclass
class FakeReplayRepository:
    outcome: Optional[ReplayOutcome] = None
    error: Optional[BaseException] = None
    calls: list[tuple] = field(default_factory=list)

    def replay(self, authenticated_user_id: str, request_id: str) -> ReplayOutcome:
        self.calls.append((authenticated_user_id, request_id))
        if self.error is not None:
            raise self.error
        if self.outcome is None:
            return ReplayOutcome(status="completed", committed=_committed())
        return self.outcome


@dataclass
class FakeContextLoader:
    calls: int = 0
    error: Optional[BaseException] = None

    def __call__(self, user_id: str, current_message: str, user_state: dict):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return LoadedContextData()


@dataclass
class FakeProvider:
    responses: list[str] = field(default_factory=lambda: ["generated response"])
    appraise_error: Optional[BaseException] = None
    generate_error: Optional[BaseException] = None
    appraisals: int = 0
    generations: int = 0

    async def appraise(self, message: str, budget: TurnBudget) -> AppraisalV1:
        self.appraisals += 1
        if self.appraise_error is not None:
            raise self.appraise_error
        return AppraisalV1.neutral()

    async def generate(self, messages: list, budget: TurnBudget) -> str:
        self.generations += 1
        if self.generate_error is not None:
            raise self.generate_error
        if self.responses:
            return self.responses.pop(0)
        return "generated response"

    def build_trusted_policy(self, emotional_state, relationship, adaptation_strategy=""):
        return "policy"


def _use_case(**kwargs: Any) -> ProcessTurn:
    params: dict[str, Any] = {
        "state_repository": FakeStateRepository(),
        "commit_repository": FakeCommitRepository(),
        "replay_repository": FakeReplayRepository(),
        "context_loader": FakeContextLoader(),
        "provider": FakeProvider(),
        "transition_config": TransitionConfig.defaults(),
        "relationship_config": RelationshipTransitionConfig.defaults(),
        "clock": lambda: 1000.0,
        "supabase_timeout": 5.0,
        "archival_extraction_enabled": False,
        # lease_owner omitted: every instance generates its OWN owner.
    }
    params.update(kwargs)
    return ProcessTurn(**params)


def _input(mode: TurnMode = TurnMode.normal) -> ProcessTurnInput:
    return ProcessTurnInput(
        authenticated_user_id="user-a",
        request_id=UUID,
        user_message="hello",
        budget=_budget(),
        correlation=CORRELATION,
        mode=mode,
    )


async def _execute(use_case: ProcessTurn, inp: ProcessTurnInput) -> ProcessTurnResult:
    return await use_case.execute(inp)


# ═══════════════════════════════════════════════════════════════════
# 1. Nominal path
# ═══════════════════════════════════════════════════════════════════


class TestNominalPath:
    @pytest.mark.asyncio
    async def test_commit_called_exactly_once_with_loaded_revision(self):
        state_repo = FakeStateRepository(states=[_default_state(revision=3)])
        commit_repo = FakeCommitRepository()
        use_case = _use_case(state_repository=state_repo, commit_repository=commit_repo)

        result = await _execute(use_case, _input())

        assert isinstance(result, ProcessTurnResult)
        assert len(commit_repo.calls) == 1
        commit = commit_repo.calls[0]
        assert commit["expected_revision"] == 3
        assert commit["request_id"] == UUID
        assert commit["authenticated_user_id"] == "user-a"
        assert commit["user_message"] == "hello"
        # the instance owns a unique, sanitized, per-instance lease owner
        owner = commit["lease_owner"]
        assert owner.startswith(LEASE_OWNER_PREFIX + ":")
        assert _LEASE_OWNER_RE.fullmatch(owner)
        assert len(owner) <= 64
        # the assistant message id is generated before commit (valid UUID)
        assert len(commit["replay_payload"]["message_id"]) == 36

    @pytest.mark.asyncio
    async def test_result_comes_from_committed_turn_not_precommit_variables(self):
        # The provider generates one text, but the persisted result says
        # another: the use case must return the persisted one.
        provider = FakeProvider(responses=["generated text"])
        commit_repo = FakeCommitRepository(results=[_committed(response="persisted text")])
        use_case = _use_case(
            provider=provider, commit_repository=commit_repo
        )

        result = await _execute(use_case, _input())

        assert result.response == "persisted text"
        assert isinstance(result.emotion_state, EmotionStateResponse)
        assert result.committed.committed_revision == 1

    @pytest.mark.asyncio
    async def test_only_write_path_is_commit_repository(self):
        state_repo = FakeStateRepository()
        commit_repo = FakeCommitRepository()
        use_case = _use_case(state_repository=state_repo, commit_repository=commit_repo)

        await _execute(use_case, _input())

        # The use case has no reference to memory managers at all: the only
        # write-capable repository invoked is the commit repository.
        assert len(commit_repo.calls) == 1
        assert not hasattr(use_case, "save_turn")
        assert not hasattr(use_case, "sync_state")

    @pytest.mark.asyncio
    async def test_state_and_context_loaded_before_commit(self):
        state_repo = FakeStateRepository(states=[_default_state(revision=0)])
        context_loader = FakeContextLoader()
        use_case = _use_case(
            state_repository=state_repo, context_loader=context_loader
        )

        await _execute(use_case, _input())

        assert len(state_repo.loads) == 1
        assert context_loader.calls == 1

    @pytest.mark.asyncio
    async def test_input_type_validated(self):
        use_case = _use_case()
        with pytest.raises(TypeError):
            await use_case.execute("not an input")

    @pytest.mark.asyncio
    async def test_persistence_error_propagates_without_retry(self):
        commit_repo = FakeCommitRepository(
            error=PersistenceError("database_error", "persistence error")
        )
        provider = FakeProvider()
        use_case = _use_case(commit_repository=commit_repo, provider=provider)

        with pytest.raises(PersistenceError):
            await _execute(use_case, _input())
        assert provider.appraisals == 1
        assert provider.generations == 1
        assert len(commit_repo.calls) == 1


# ═══════════════════════════════════════════════════════════════════
# 2. Replay path
# ═══════════════════════════════════════════════════════════════════


class TestReplayPath:
    @pytest.mark.asyncio
    async def test_replay_returns_persisted_result_without_provider(self):
        replay_repo = FakeReplayRepository(
            outcome=ReplayOutcome(status="completed", committed=_committed())
        )
        provider = FakeProvider()
        state_repo = FakeStateRepository()
        context_loader = FakeContextLoader()
        commit_repo = FakeCommitRepository()
        use_case = _use_case(
            replay_repository=replay_repo,
            provider=provider,
            state_repository=state_repo,
            context_loader=context_loader,
            commit_repository=commit_repo,
        )

        result = await _execute(use_case, _input(mode=TurnMode.replay_attempt))

        assert result.response == "persisted response"
        # no context load, no appraisal, no generation, no transitions,
        # no state load, no commit
        assert context_loader.calls == 0
        assert provider.appraisals == 0
        assert provider.generations == 0
        assert len(state_repo.loads) == 0
        assert len(commit_repo.calls) == 0

    @pytest.mark.asyncio
    async def test_replay_in_progress_raises_structured_conflict(self):
        use_case = _use_case(
            replay_repository=FakeReplayRepository(
                outcome=ReplayOutcome(status=REPLAY_STATUS_IN_PROGRESS)
            )
        )
        with pytest.raises(ConflictError) as exc:
            await _execute(use_case, _input(mode=TurnMode.replay_attempt))
        assert exc.value.code == "request_in_progress"

    @pytest.mark.asyncio
    async def test_replay_unavailable_raises_structured_conflict(self):
        use_case = _use_case(
            replay_repository=FakeReplayRepository(
                outcome=ReplayOutcome(status=REPLAY_STATUS_UNAVAILABLE)
            )
        )
        with pytest.raises(ConflictError) as exc:
            await _execute(use_case, _input(mode=TurnMode.replay_attempt))
        assert exc.value.code == "request_replay_unavailable"


class TestReplaySemantics:
    """Stale/pending/reclaim policy (#308 review).

    Policy chosen: NO automatic reclaim through the endpoint in this version.
    A replay outcome is always terminal:
      * completed -> persisted result, never recomputes;
      * pending with ACTIVE lease -> request_in_progress (it IS being
        processed; retry the same request id later);
      * pending with EXPIRED lease / expired / missing -> the reservation can
        never complete on its own; the client needs a NEW request id (or
        operational cleanup).
    The provider is never called for any non-completed outcome and there is
    never a transition from replay back to the normal (commit) path.
    """

    @pytest.mark.asyncio
    async def test_pending_active_lease_is_request_in_progress_no_provider(self):
        provider = FakeProvider()
        commit_repo = FakeCommitRepository()
        use_case = _use_case(
            replay_repository=FakeReplayRepository(
                outcome=ReplayOutcome(status=REPLAY_STATUS_IN_PROGRESS)
            ),
            provider=provider,
            commit_repository=commit_repo,
        )
        with pytest.raises(ConflictError) as exc:
            await _execute(use_case, _input(mode=TurnMode.replay_attempt))
        assert exc.value.code == "request_in_progress"
        # The policy does not authorize recomputation: no provider, no commit.
        assert provider.appraisals == 0
        assert provider.generations == 0
        assert len(commit_repo.calls) == 0

    @pytest.mark.asyncio
    async def test_pending_expired_lease_is_replay_unavailable_no_provider(self):
        provider = FakeProvider()
        commit_repo = FakeCommitRepository()
        use_case = _use_case(
            replay_repository=FakeReplayRepository(
                outcome=ReplayOutcome(status=REPLAY_STATUS_UNAVAILABLE)
            ),
            provider=provider,
            commit_repository=commit_repo,
        )
        with pytest.raises(ConflictError) as exc:
            await _execute(use_case, _input(mode=TurnMode.replay_attempt))
        assert exc.value.code == "request_replay_unavailable"
        assert provider.appraisals == 0
        assert provider.generations == 0
        assert len(commit_repo.calls) == 0

    @pytest.mark.asyncio
    async def test_expired_status_is_replay_unavailable_no_provider(self):
        provider = FakeProvider()
        use_case = _use_case(
            replay_repository=FakeReplayRepository(
                outcome=ReplayOutcome(status=REPLAY_STATUS_UNAVAILABLE)
            ),
            provider=provider,
        )
        with pytest.raises(ConflictError) as exc:
            await _execute(use_case, _input(mode=TurnMode.replay_attempt))
        assert exc.value.code == "request_replay_unavailable"
        assert provider.appraisals == 0
        assert provider.generations == 0

    @pytest.mark.asyncio
    async def test_no_loop_between_replay_and_normal(self):
        """A failed replay never falls back to the normal commit path."""
        replay_repo = FakeReplayRepository(
            outcome=ReplayOutcome(status=REPLAY_STATUS_UNAVAILABLE)
        )
        state_repo = FakeStateRepository()
        commit_repo = FakeCommitRepository()
        context_loader = FakeContextLoader()
        provider = FakeProvider()
        use_case = _use_case(
            replay_repository=replay_repo,
            state_repository=state_repo,
            commit_repository=commit_repo,
            context_loader=context_loader,
            provider=provider,
        )

        with pytest.raises(ConflictError) as exc:
            await _execute(use_case, _input(mode=TurnMode.replay_attempt))

        assert exc.value.code == "request_replay_unavailable"
        # Terminal outcome: no state load, no context, no provider, no commit.
        assert len(state_repo.loads) == 0
        assert context_loader.calls == 0
        assert provider.appraisals == 0
        assert provider.generations == 0
        assert len(commit_repo.calls) == 0
        # The same request re-sent as NORMAL is a distinct admission; only a
        # repeated admission can enter replay mode. A single execution never
        # cycles replay -> normal.
        assert len(replay_repo.calls) == 1

    @pytest.mark.asyncio
    async def test_replay_does_not_claim_any_request(self):
        """Replay performs reads only: no commit_turn call is ever made."""
        replay_repo = FakeReplayRepository(
            outcome=ReplayOutcome(status="completed", committed=_committed())
        )
        commit_repo = FakeCommitRepository()
        use_case = _use_case(
            replay_repository=replay_repo, commit_repository=commit_repo
        )

        await _execute(use_case, _input(mode=TurnMode.replay_attempt))

        assert len(commit_repo.calls) == 0


# ═══════════════════════════════════════════════════════════════════
# 3. Conflicts
# ═══════════════════════════════════════════════════════════════════


class TestConflicts:
    @pytest.mark.asyncio
    async def test_payload_conflict_never_retries_provider(self):
        commit_repo = FakeCommitRepository(
            error=ConflictError(
                code="request_payload_conflict",
                message="conflict",
                expected_revision=0,
                request_id=UUID,
            )
        )
        provider = FakeProvider()
        use_case = _use_case(commit_repository=commit_repo, provider=provider)

        with pytest.raises(ConflictError) as exc:
            await _execute(use_case, _input())
        assert exc.value.code == "request_payload_conflict"
        assert provider.appraisals == 1
        assert provider.generations == 1
        assert len(commit_repo.calls) == 1

    @pytest.mark.asyncio
    async def test_request_in_progress_never_retries(self):
        commit_repo = FakeCommitRepository(
            error=ConflictError(
                code="request_in_progress", message="in progress", expected_revision=0
            )
        )
        provider = FakeProvider()
        use_case = _use_case(commit_repository=commit_repo, provider=provider)

        with pytest.raises(ConflictError) as exc:
            await _execute(use_case, _input())
        assert exc.value.code == "request_in_progress"
        assert len(commit_repo.calls) == 1


# ═══════════════════════════════════════════════════════════════════
# 4. Bounded revision retry
# ═══════════════════════════════════════════════════════════════════


def _mismatch(expected: int, actual: int) -> ConflictError:
    return ConflictError(
        code="revision_mismatch",
        message="Profile revision does not match",
        expected_revision=expected,
        actual_revision=actual,
    )


class TestRevisionRetry:
    @pytest.mark.asyncio
    async def test_mismatch_retries_exactly_once_and_reloads(self):
        state_repo = FakeStateRepository(
            states=[_default_state(revision=0), _default_state(revision=1)]
        )
        commit_repo = FakeCommitRepository(results=[_mismatch(0, 1), _committed(revision=2)])
        context_loader = FakeContextLoader()
        provider = FakeProvider(responses=["first", "second"])
        use_case = _use_case(
            state_repository=state_repo,
            commit_repository=commit_repo,
            context_loader=context_loader,
            provider=provider,
        )

        result = await _execute(use_case, _input())

        # two commit attempts: first with revision 0 (conflict), second with
        # the reloaded revision 1; the second response is fresh, not reused.
        assert [c["expected_revision"] for c in commit_repo.calls] == [0, 1]
        assert [c["public_response"] for c in commit_repo.calls] == ["first", "second"]
        assert len(state_repo.loads) == 2
        assert context_loader.calls == 2
        assert provider.appraisals == 2
        assert provider.generations == 2
        assert result.response == "persisted response"

    @pytest.mark.asyncio
    async def test_third_attempt_never_occurs(self):
        commit_repo = FakeCommitRepository(results=[_mismatch(0, 1), _mismatch(1, 2)])
        provider = FakeProvider()
        use_case = _use_case(commit_repository=commit_repo, provider=provider)

        with pytest.raises(ConflictError) as exc:
            await _execute(use_case, _input())
        assert exc.value.code == "revision_mismatch"
        assert exc.value.expected_revision == 1
        assert exc.value.actual_revision == 2
        assert len(commit_repo.calls) == MAX_COMMIT_ATTEMPTS
        assert provider.appraisals == MAX_COMMIT_ATTEMPTS

    @pytest.mark.asyncio
    async def test_deadline_prevents_retry(self):
        now = {"t": 0.0}
        budget = TurnBudget(deadline=100.0, reserve=0.0, now_provider=lambda: now["t"])

        class AdvanceThenMismatch(FakeCommitRepository):
            def commit(self, **kwargs):
                self.calls.append(kwargs)
                # The first commit succeeds on the wire but reports a revision
                # conflict after the deadline has passed.
                now["t"] = 200.0
                raise _mismatch(0, 1)

        commit_repo = AdvanceThenMismatch()
        provider = FakeProvider()
        use_case = _use_case(commit_repository=commit_repo, provider=provider)

        inp = ProcessTurnInput(
            authenticated_user_id="user-a",
            request_id=UUID,
            user_message="hello",
            budget=budget,
            correlation=CORRELATION,
            mode=TurnMode.normal,
        )

        # The retry is refused because the deadline is exhausted.
        with pytest.raises(DeadlineExceeded):
            await _execute(use_case, inp)
        assert len(commit_repo.calls) == 1
        assert provider.generations == 1

    @pytest.mark.asyncio
    async def test_provider_failure_produces_no_committed_turn(self):
        provider = FakeProvider(generate_error=TurnExecutionError(
            TurnErrorCode.provider_unavailable, "provider unavailable"
        ))
        commit_repo = FakeCommitRepository()
        use_case = _use_case(provider=provider, commit_repository=commit_repo)

        with pytest.raises(TurnExecutionError) as exc:
            await _execute(use_case, _input())
        assert exc.value.code == TurnErrorCode.provider_unavailable
        assert len(commit_repo.calls) == 0


# ═══════════════════════════════════════════════════════════════════
# 5. Cancellation
# ═══════════════════════════════════════════════════════════════════


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancel_before_commit_writes_nothing(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_generate(messages, budget):
            started.set()
            await release.wait()
            return "late response"

        provider = FakeProvider()
        provider.generate = slow_generate
        commit_repo = FakeCommitRepository()
        use_case = _use_case(provider=provider, commit_repository=commit_repo)

        task = asyncio.create_task(_execute(use_case, _input()))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Cancellation before the commit started: nothing written.
        assert len(commit_repo.calls) == 0


class StatefulWriteRepository:
    """commit() that really writes to a shared store, gated by threading
    events so the test can cancel the caller while the worker is active.

    Contract mirrors the real repository inside `run_blocking_write`: the
    write starts, blocks until released, writes the result to the shared
    store and only then returns. A configured ``error`` is raised instead.
    """

    def __init__(
        self,
        store: dict,
        started: threading.Event,
        release: threading.Event,
        finished: threading.Event,
        error: Optional[BaseException] = None,
        result: Optional[CommittedTurn] = None,
    ) -> None:
        self.store = store
        self.started = started
        self.release = release
        self.finished = finished
        self.error = error
        self.result = result

    def commit(self, **kwargs: Any) -> CommittedTurn:
        self.started.set()
        released = self.release.wait(timeout=10.0)
        assert released, "worker release timed out"
        if self.error is not None:
            raise self.error
        committed = self.result if self.result is not None else _committed()
        self.store["committed"] = committed
        self.finished.set()
        return committed


class StoreBackedReplayRepository:
    """Replay repository that reads exactly what the stateful write wrote."""

    def __init__(self, store: dict) -> None:
        self.store = store
        self.calls = 0

    def replay(self, authenticated_user_id: str, request_id: str) -> ReplayOutcome:
        self.calls += 1
        committed = self.store.get("committed")
        if committed is None:
            return ReplayOutcome(status=REPLAY_STATUS_UNAVAILABLE)
        return ReplayOutcome(status="completed", committed=committed)


class TestCancelDrainsStatefulWrite:
    @pytest.mark.asyncio
    async def test_cancel_during_commit_drains_then_replay_reads_written_result(self):
        """The write starts, the caller is cancelled while the worker is
        active, the worker writes the result and only then is released;
        run_blocking_write waits for the worker and only then propagates the
        original CancelledError; replay reads exactly the written result and
        the provider is not called again."""
        store: dict = {}
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        commit_repo = StatefulWriteRepository(store, started, release, finished)
        replay_repo = StoreBackedReplayRepository(store)
        provider = FakeProvider()
        use_case = _use_case(
            commit_repository=commit_repo,
            replay_repository=replay_repo,
            provider=provider,
        )

        task = asyncio.create_task(_execute(use_case, _input()))
        # 1. The write started inside run_blocking_write (worker active).
        started_ok = await asyncio.to_thread(started.wait, 5.0)
        assert started_ok, "commit write did not start"
        # 2. Cancel the calling task while the worker is still active.
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done(), "task must still be draining the write"
        # 3. The worker writes the result and only then is released.
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.wait(5.0), "worker never finished"
        # 4. The write really completed before the cancellation propagated.
        assert "committed" in store
        # 5. Replay recovers exactly the written result, without provider.
        result = await _execute(use_case, _input(mode=TurnMode.replay_attempt))
        assert result.response == "persisted response"
        assert result.committed is store["committed"]
        assert provider.generations == 1
        assert provider.appraisals == 1

    @pytest.mark.asyncio
    async def test_worker_failure_after_cancel_is_recovered_without_asyncio_warning(
        self, caplog
    ):
        """If the worker fails after the cancellation, the original
        CancelledError propagates and the worker's exception is retrieved:
        asyncio must never log 'Task exception was never retrieved'."""
        store: dict = {}
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        commit_repo = StatefulWriteRepository(
            store,
            started,
            release,
            finished,
            error=RuntimeError("worker exploded after cancel"),
        )
        use_case = _use_case(commit_repository=commit_repo)

        task = asyncio.create_task(_execute(use_case, _input()))
        started_ok = await asyncio.to_thread(started.wait, 5.0)
        assert started_ok, "commit write did not start"
        task.cancel()
        await asyncio.sleep(0.05)
        release.set()

        with caplog.at_level(logging.ERROR, logger="asyncio"):
            with pytest.raises(asyncio.CancelledError):
                await task
        # run_blocking_write only propagates after the worker task is done:
        # the CancelledError above is itself the proof the drain finished.
        assert "Task exception was never retrieved" not in caplog.text
        assert "worker exploded" not in caplog.text

    @pytest.mark.asyncio
    async def test_run_blocking_write_regression_consumes_worker_outcome_on_cancel(
        self, caplog
    ):
        """Regression for the helper race that could exit the drain loop
        without consuming ``worker_task.result()`` after a cancellation:
        the worker completes while the caller is cancelled and the outcome
        (success or failure) is always retrieved."""
        from backend.turn_execution import run_blocking_write

        store: dict = {}
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        commit_repo = StatefulWriteRepository(store, started, release, finished)
        replay_repo = StoreBackedReplayRepository(store)

        async def run():
            return await run_blocking_write(
                "commit_turn",
                _budget(),
                5.0,
                commit_repo.commit,
                authenticated_user_id="user-a",
                request_id=UUID,
                expected_revision=0,
                user_message="hello",
                assistant_message="ok",
                emotional_state=None,
                relationship_state=None,
                public_response="ok",
                outbox_events=[],
                replay_payload={},
                lease_owner=new_lease_owner(),
                allowlist_exceptions=(ConflictError, ValidationError, PersistenceError),
            )

        task = asyncio.create_task(run())
        started_ok = await asyncio.to_thread(started.wait, 5.0)
        assert started_ok, "commit write did not start"
        task.cancel()
        await asyncio.sleep(0.05)
        release.set()
        with caplog.at_level(logging.ERROR, logger="asyncio"):
            with pytest.raises(asyncio.CancelledError):
                await task
        # The worker's successful result was consumed; the write landed.
        assert finished.wait(5.0)
        assert "committed" in store
        assert "Task exception was never retrieved" not in caplog.text
        # The replay store is authoritative for what was drained.
        assert replay_repo.store["committed"] is store["committed"]


# ═══════════════════════════════════════════════════════════════════
# 6. Outbox
# ═══════════════════════════════════════════════════════════════════


class TestOutbox:
    @pytest.mark.asyncio
    async def test_archival_enabled_adds_exactly_one_sanitized_event(self):
        commit_repo = FakeCommitRepository()
        use_case = _use_case(
            commit_repository=commit_repo, archival_extraction_enabled=True
        )

        await _execute(use_case, _input())

        events = commit_repo.calls[0]["outbox_events"]
        assert len(events) == 1
        event_type, payload, idempotency_key = events[0]
        assert event_type == "archival_extraction_requested"
        assert set(payload) == {"message_id", "kind", "version"}
        assert payload["message_id"] == UUID
        assert payload["kind"] == "archival"
        assert payload["version"] == 1
        assert idempotency_key == f"archival:{UUID}:v1"
        serialized = str(payload)
        assert "user-a" not in serialized
        assert "hello" not in serialized

    @pytest.mark.asyncio
    async def test_archival_disabled_adds_no_events(self):
        commit_repo = FakeCommitRepository()
        use_case = _use_case(commit_repository=commit_repo)

        await _execute(use_case, _input())

        assert commit_repo.calls[0]["outbox_events"] == []


# ═══════════════════════════════════════════════════════════════════
# 7. Observability
# ═══════════════════════════════════════════════════════════════════


class TestObservability:
    @pytest.mark.asyncio
    async def test_logs_never_contain_sensitive_markers(self, caplog):
        commit_repo = FakeCommitRepository(results=[_mismatch(0, 1), _committed()])
        use_case = _use_case(commit_repository=commit_repo)

        with caplog.at_level(logging.INFO, logger="backend.process_turn"):
            await _execute(use_case, _input())

        text = caplog.text
        assert UUID not in text
        assert "user-a" not in text
        assert "hello" not in text
        assert "persisted response" not in text
        for record in caplog.records:
            assert record.levelname in ("INFO",)

    @pytest.mark.asyncio
    async def test_retry_events_are_low_cardinality(self, caplog):
        commit_repo = FakeCommitRepository(results=[_mismatch(0, 1), _committed()])
        use_case = _use_case(commit_repository=commit_repo)

        with caplog.at_level(logging.INFO, logger="backend.process_turn"):
            await _execute(use_case, _input())

        messages = [r.getMessage() for r in caplog.records]
        assert f"event=process_turn_attempt correlation={CORRELATION} attempt=1" in messages
        assert f"event=process_turn_revision_conflict correlation={CORRELATION} attempt=1" in messages
        assert f"event=process_turn_attempt correlation={CORRELATION} attempt=2" in messages
        assert f"event=process_turn_commit_completed correlation={CORRELATION} attempt=2" in messages

    @pytest.mark.asyncio
    async def test_lease_owner_never_appears_in_logs(self, caplog):
        commit_repo = FakeCommitRepository()
        use_case = _use_case(commit_repository=commit_repo)

        with caplog.at_level(logging.INFO, logger="backend.process_turn"):
            await _execute(use_case, _input())

        assert use_case._lease_owner not in caplog.text


class TestLeaseOwner:
    def test_two_instances_receive_different_owners(self):
        first = _use_case()
        second = _use_case()
        assert first._lease_owner != second._lease_owner

    def test_same_instance_reuses_the_same_owner(self):
        use_case = _use_case()
        owner = use_case._lease_owner
        assert use_case._lease_owner == owner
        assert _LEASE_OWNER_RE.fullmatch(owner)
        assert owner.startswith(LEASE_OWNER_PREFIX + ":")
        assert len(owner) <= 64

    def test_injected_owner_is_respected(self):
        use_case = _use_case(lease_owner="custom-owner-1")
        assert use_case._lease_owner == "custom-owner-1"

    def test_new_lease_owner_is_always_sanitized_and_unique(self):
        owners = {new_lease_owner() for _ in range(100)}
        assert len(owners) == 100
        for owner in owners:
            assert _LEASE_OWNER_RE.fullmatch(owner)
            assert owner.startswith(LEASE_OWNER_PREFIX + ":")
            assert len(owner) <= 64

    @pytest.mark.asyncio
    async def test_active_lease_of_another_instance_is_request_in_progress(self):
        """An active lease held by a DIFFERENT instance must surface as
        request_in_progress, never as a continuation of the same worker."""
        other_instance_owner = new_lease_owner()
        commit_repo = FakeCommitRepository(
            error=ConflictError(
                code="request_in_progress",
                message="Request is already in progress by another worker",
                expected_revision=0,
                request_id=UUID,
            )
        )
        use_case = _use_case(commit_repository=commit_repo)

        with pytest.raises(ConflictError) as exc:
            await _execute(use_case, _input())

        assert exc.value.code == "request_in_progress"
        # The instance must not have claimed the other worker's lease.
        assert use_case._lease_owner != other_instance_owner
        assert commit_repo.calls[0]["lease_owner"] != other_instance_owner

    @pytest.mark.asyncio
    async def test_owner_is_injectable_per_instance_for_tests(self):
        commit_repo = FakeCommitRepository()
        first = _use_case(commit_repository=commit_repo, lease_owner="worker-A")
        second = _use_case(commit_repository=commit_repo, lease_owner="worker-B")

        await _execute(first, _input())
        await _execute(second, _input())

        assert [c["lease_owner"] for c in commit_repo.calls] == [
            "worker-A",
            "worker-B",
        ]


class TestCorrelationObservability:
    def test_invalid_correlation_fails_closed(self):
        with pytest.raises(ValueError):
            ProcessTurnInput(
                authenticated_user_id="user-a",
                request_id=UUID,
                user_message="hello",
                budget=_budget(),
                correlation="not-a-hex-hmac",
            )
        with pytest.raises(ValueError):
            ProcessTurnInput(
                authenticated_user_id="user-a",
                request_id=UUID,
                user_message="hello",
                budget=_budget(),
                correlation=UUID,  # raw request id is never a valid correlation
            )

    @pytest.mark.asyncio
    async def test_replay_and_commit_events_are_semantically_distinct(self, caplog):
        replay_repo = FakeReplayRepository(
            outcome=ReplayOutcome(status="completed", committed=_committed())
        )
        use_case = _use_case(replay_repository=replay_repo)

        with caplog.at_level(logging.INFO, logger="backend.process_turn"):
            await _execute(use_case, _input(mode=TurnMode.replay_attempt))

        messages = [r.getMessage() for r in caplog.records]
        assert f"event=process_turn_attempt correlation={CORRELATION} attempt=1 mode=replay" in messages
        assert f"event=process_turn_replay correlation={CORRELATION}" in messages
        # A replay is NEVER logged as a commit completion.
        assert not any(
            "event=process_turn_commit_completed" in message for message in messages
        )

    @pytest.mark.asyncio
    async def test_correlation_is_consistent_across_retry_attempts(self, caplog):
        commit_repo = FakeCommitRepository(results=[_mismatch(0, 1), _committed()])
        use_case = _use_case(commit_repository=commit_repo)

        with caplog.at_level(logging.INFO, logger="backend.process_turn"):
            await _execute(use_case, _input())

        messages = [r.getMessage() for r in caplog.records]
        for message in messages:
            assert f"correlation={CORRELATION}" in message
        # the raw request id never appears in any event
        assert all(UUID not in message for message in messages)

    @pytest.mark.asyncio
    async def test_different_correlations_produce_distinct_events(self, caplog):
        other = "d" * 64
        commit_repo = FakeCommitRepository()
        use_case = _use_case(commit_repository=commit_repo)
        first_inp = _input()
        second_inp = ProcessTurnInput(
            authenticated_user_id="user-a",
            request_id=UUID,
            user_message="hello",
            budget=_budget(),
            correlation=other,
        )

        with caplog.at_level(logging.INFO, logger="backend.process_turn"):
            await _execute(use_case, first_inp)
            await _execute(use_case, second_inp)

        messages = [r.getMessage() for r in caplog.records]
        assert f"event=process_turn_commit_completed correlation={CORRELATION} attempt=1" in messages
        assert f"event=process_turn_commit_completed correlation={other} attempt=1" in messages


# ═══════════════════════════════════════════════════════════════════
# 8. UserStateRepository (fake client, no insert)
# ═══════════════════════════════════════════════════════════════════


class FakeQueryBuilder:
    def __init__(self, rows: list):
        self.rows = rows
        self.calls: list[str] = []

    def select(self, *cols):
        self.calls.append("select")
        return self

    def eq(self, key, value):
        self.calls.append("eq")
        return self

    def execute(self):
        self.calls.append("execute")
        return SimpleNamespace(data=self.rows)


class FakeTableClient:
    def __init__(self, rows: list):
        self.builder = FakeQueryBuilder(rows)
        self.insert_calls = 0
        self.upsert_calls = 0

    def table(self, name: str) -> FakeQueryBuilder:
        return self.builder

    def insert(self, *args, **kwargs):
        self.insert_calls += 1
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=[]))

    def upsert(self, *args, **kwargs):
        self.upsert_calls += 1
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=[]))


class TestUserStateRepository:
    def test_missing_profile_returns_defaults_with_revision_zero_and_no_insert(self):
        client = FakeTableClient(rows=[])
        repo = UserStateRepository(lambda: client)

        state = repo.load("user-a", default_timestamp=1000.0)

        assert state.revision == 0
        assert state.emotional_state["schema_version"] == 1
        assert state.relationship_state["schema_version"] == 1
        assert client.insert_calls == 0
        assert client.upsert_calls == 0
        assert client.builder.calls == ["select", "eq", "execute"]

    def test_existing_profile_returns_persisted_revision(self):
        row = {
            "revision": 7,
            "persona_config": "Katherine...",
            "user_profile": {"name": "x"},
            "emotional_state": EmotionalStateV1.neutral(timestamp=1.0).to_dict(),
            "relationship_state": RelationshipStateV1.neutral(timestamp=1.0).to_dict(),
        }
        repo = UserStateRepository(lambda: FakeTableClient(rows=[row]))

        state = repo.load("user-a", default_timestamp=1000.0)

        assert state.revision == 7
        assert state.user_profile == {"name": "x"}

    def test_duplicate_rows_fail_closed(self):
        row = {"revision": 1}
        repo = UserStateRepository(lambda: FakeTableClient(rows=[row, row]))

        with pytest.raises(TurnExecutionError) as exc:
            repo.load("user-a", default_timestamp=1000.0)
        assert exc.value.code == TurnErrorCode.internal_error

    @pytest.mark.parametrize(
        "revision", [True, False, -1, 1.5, "1", None]
    )
    def test_invalid_revision_fails_closed(self, revision):
        repo = UserStateRepository(
            lambda: FakeTableClient(rows=[{"revision": revision}])
        )
        with pytest.raises(TurnExecutionError) as exc:
            repo.load("user-a", default_timestamp=1000.0)
        assert exc.value.code == TurnErrorCode.internal_error

    def test_invalid_snapshot_fails_closed(self):
        repo = UserStateRepository(
            lambda: FakeTableClient(rows=[{"revision": 1, "emotional_state": "nope"}])
        )
        with pytest.raises(TurnExecutionError):
            repo.load("user-a", default_timestamp=1000.0)

    def test_no_client_fails_closed_as_persistence(self):
        repo = UserStateRepository(lambda: None)
        with pytest.raises(PersistenceError):
            repo.load("user-a", default_timestamp=1000.0)


# ═══════════════════════════════════════════════════════════════════
# 9. Replay result parsing
# ═══════════════════════════════════════════════════════════════════


class TestReplayResultParsing:
    def test_completed_uses_canonical_parser(self):
        result = _committed().to_db_row()
        outcome = parse_replay_committed_turn_result(result)
        assert outcome.status == "completed"
        assert isinstance(outcome.committed, CommittedTurn)

    def test_structured_statuses(self):
        for status in (REPLAY_STATUS_IN_PROGRESS, REPLAY_STATUS_UNAVAILABLE):
            outcome = parse_replay_committed_turn_result({"status": status})
            assert outcome.status == status
            assert outcome.committed is None

    def test_unknown_status_fails_closed(self):
        with pytest.raises(ValidationError):
            parse_replay_committed_turn_result({"status": "mystery"})

    def test_malformed_envelope_fails_closed(self):
        with pytest.raises(ValidationError):
            parse_replay_committed_turn_result("nope")
        with pytest.raises(ValidationError):
            parse_replay_committed_turn_result([])

    def test_error_envelope_propagates_canonical_conflict(self):
        with pytest.raises(ConflictError) as exc:
            parse_replay_committed_turn_result(
                {
                    "error": {
                        "code": "request_in_progress",
                        "message": "in progress",
                        "request_id": UUID,
                    }
                }
            )
        assert exc.value.code == "request_in_progress"


# ═══════════════════════════════════════════════════════════════════
# 10. Engine delegation (active path never touches legacy writers)
# ═══════════════════════════════════════════════════════════════════


class TestEngineDelegation:
    @pytest.mark.asyncio
    async def test_engine_delegates_to_process_turn_with_mode_and_request_id(self):
        from backend.chat_engine import ChatConversationEngine

        engine = object.__new__(ChatConversationEngine)
        captured = {}

        async def fake_execute(inp):
            captured["inp"] = inp
            return "ok"

        engine._process_turn = SimpleNamespace(execute=fake_execute)
        engine.lock_manager = SimpleNamespace(
            lock=lambda _user_id: SimpleNamespace(
                __aenter__=lambda: _noop_async(),
                __aexit__=lambda *a: _noop_async(),
            )
        )
        engine._monotonic = lambda: 0.0
        engine._turn_config = SimpleNamespace()

        result = await engine.process_turn(
            "user-a",
            "hello",
            UUID,
            budget=_budget(),
            mode=TurnMode.normal,
            correlation=CORRELATION,
        )
        assert result == "ok"
        assert captured["inp"].authenticated_user_id == "user-a"
        assert captured["inp"].request_id == UUID
        assert captured["inp"].user_message == "hello"
        assert captured["inp"].mode is TurnMode.normal
        assert captured["inp"].correlation == CORRELATION
        assert captured["inp"].budget is not None


async def _noop_async():
    return None


class TestActivePathNeverCallsLegacyWriters:
    @pytest.mark.asyncio
    async def test_active_path_runs_without_save_turn_or_sync_state(self):
        """If the active path ever called save_turn/sync_state, this fails."""
        from backend.chat_engine import ChatConversationEngine

        engine = object.__new__(ChatConversationEngine)

        def forbidden(*_args, **_kwargs):
            raise AssertionError("legacy write method called in active path")

        engine.memory_manager = SimpleNamespace(
            supabase=object(),
            save_turn=forbidden,
            sync_state=forbidden,
        )
        captured = {}

        async def fake_execute(inp):
            captured["mode"] = inp.mode
            return "ok"

        engine._process_turn = SimpleNamespace(execute=fake_execute)
        engine.lock_manager = SimpleNamespace(
            lock=lambda _user_id: SimpleNamespace(
                __aenter__=lambda: _noop_async(),
                __aexit__=lambda *a: _noop_async(),
            )
        )
        engine._monotonic = lambda: 0.0
        engine._turn_config = SimpleNamespace()

        result = await engine.process_turn(
            "user-a", "hello", UUID, budget=_budget(), correlation=CORRELATION
        )
        assert result == "ok"
        assert captured["mode"] is TurnMode.normal
