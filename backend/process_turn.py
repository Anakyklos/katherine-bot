"""
Idempotent, transactional turn processing use case (#272).

Replaces the legacy active flow (``load state -> provider -> save_turn ->
sync_state -> BackgroundTasks``) with a single transactional unit:

    authenticated request identity
    -> admission result
    -> replay check when applicable
    -> load state + revision
    -> load trusted context
    -> appraisal / transitions / generation outside the transaction
    -> commit_turn atomically with expected_revision
    -> bounded revision retry
    -> return exactly the persisted result

Consistency comes from PostgreSQL (``public.commit_turn``); the process-local
``UserLockManager`` remains only a local optimization. The use case is
stateless: no per-user state lives in any global or singleton.

Public result contract: ``ProcessTurnResult`` is derived from
``CommittedTurn.replay_payload`` — even for a fresh commit the response and
public emotion state are read back from the persisted result, never from the
pre-commit variables.
"""

from __future__ import annotations

import logging
import time as _time
import uuid as _uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Protocol

from .atomic_turn_commit import (
    CommittedTurn,
    ConflictError,
    PersistenceError,
    ValidationError,
)
from .emotional_domain import (
    AppraisalV1,
    TransitionConfig,
    migrate_legacy_snapshot,
    transition,
)
from .emotion_presentation import EmotionStateResponse, project_public_emotion
from .relationship import (
    RelationshipStateV1,
    RelationshipTransitionConfig,
    migrate_legacy_relationship_snapshot,
    transition_relationship,
)
from .trusted_context import (
    TrustedContextError,
    build_context_bundle,
    build_envelope,
)
from .turn_execution import (
    DeadlineExceeded,
    TurnBudget,
    TurnErrorCode,
    TurnExecutionError,
    run_blocking_read,
    run_blocking_write,
)

logger = logging.getLogger(__name__)

#: Stable lease owner used when commit_turn needs to claim/reclaim a
#: pending/expired request row with the same payload hash.
LEASE_OWNER = "process-turn-v1"

#: Maximum total commit attempts (initial + exactly one revision retry).
MAX_COMMIT_ATTEMPTS = 2

#: Outbox event for archival extraction (references only, no content).
ARCHIVAL_EVENT_TYPE = "archival_extraction_requested"

#: Exceptions the repository adapters may raise that must propagate through
#: the read/write helpers without being wrapped into persistence_unavailable.
_REPOSITORY_ERRORS = (ConflictError, ValidationError, PersistenceError)


class TurnMode(str, Enum):
    """Whether the request is a fresh admission or a replay attempt."""

    normal = "normal"
    replay_attempt = "replay_attempt"


@dataclass(frozen=True)
class ProcessTurnInput:
    """Immutable input for one ProcessTurn execution."""

    authenticated_user_id: str
    request_id: str
    user_message: str
    budget: TurnBudget
    mode: TurnMode = TurnMode.normal


@dataclass(frozen=True)
class ProcessTurnResult:
    """Public outcome built exclusively from the persisted ``CommittedTurn``."""

    committed: CommittedTurn
    response: str
    emotion_state: EmotionStateResponse


class ProviderPort(Protocol):
    """Provider + policy surface used outside the transaction.

    The engine implements this port with its existing appraisal, generation
    and trusted-policy builders; tests substitute fakes.
    """

    async def appraise(self, message: str, budget: TurnBudget) -> AppraisalV1: ...

    async def generate(self, messages: list, budget: TurnBudget) -> str: ...

    def build_trusted_policy(
        self,
        emotional_state: Any,
        relationship: Any,
        adaptation_strategy: str = "",
    ) -> str: ...


@dataclass(frozen=True)
class _RevisionConflict:
    expected_revision: int
    actual_revision: Optional[int]


def parse_public_result(committed: CommittedTurn) -> ProcessTurnResult:
    """Parse the persisted public result (same parser for commit and replay).

    Raises ``ValidationError`` (internal contract error, sanitized 500) when
    the persisted contract is missing the required public fields.
    """
    payload = committed.replay_payload
    response = payload.get("response")
    if not isinstance(response, str):
        raise ValidationError("invalid_replay_payload", "persisted response missing")
    raw_emotion = payload.get("emotion_state")
    if not isinstance(raw_emotion, Mapping):
        raise ValidationError("invalid_replay_payload", "persisted emotion_state missing")
    try:
        emotion = EmotionStateResponse.model_validate(dict(raw_emotion))
    except Exception:
        raise ValidationError("invalid_replay_payload", "persisted emotion_state invalid") from None
    return ProcessTurnResult(committed=committed, response=response, emotion_state=emotion)


def build_archival_outbox_event(request_id: str) -> tuple[str, Mapping[str, Any], str]:
    """Build the idempotent archival outbox event (sanitized references only).

    Payload carries only the user message id (equal to request_id), the event
    kind and a version. Never includes the message, user id, prompt, response,
    snapshots, HMAC or any secret.
    """
    return (
        ARCHIVAL_EVENT_TYPE,
        {"message_id": request_id, "kind": "archival", "version": 1},
        f"archival:{request_id}:v1",
    )


def build_process_turn(engine: Any) -> "ProcessTurn":
    """Wire a ``ProcessTurn`` around a ``ConversationEngine`` instance."""
    from .turn_repositories import (
        TurnCommitRepository,
        TurnReplayRepository,
        UserStateRepository,
    )

    client_provider: Callable[[], Any] = lambda: engine.memory_manager.supabase
    return ProcessTurn(
        state_repository=UserStateRepository(client_provider),
        commit_repository=TurnCommitRepository(client_provider),
        replay_repository=TurnReplayRepository(client_provider),
        context_loader=engine.memory_manager.load_context_data,
        provider=engine,
        transition_config=engine.transition_config,
        relationship_config=engine.relationship_config,
        clock=engine._clock,
        supabase_timeout=engine._turn_config.supabase_timeout,
        archival_extraction_enabled=engine.archival_extraction_enabled,
        lease_owner=LEASE_OWNER,
    )


class ProcessTurn:
    """Idempotent, transactional turn processing use case."""

    def __init__(
        self,
        *,
        state_repository: Any,
        commit_repository: Any,
        replay_repository: Any,
        context_loader: Callable[..., Any],
        provider: ProviderPort,
        transition_config: TransitionConfig,
        relationship_config: RelationshipTransitionConfig,
        clock: Callable[[], float] = _time.time,
        supabase_timeout: float = 5.0,
        archival_extraction_enabled: bool = False,
        lease_owner: str = LEASE_OWNER,
    ) -> None:
        self._state_repository = state_repository
        self._commit_repository = commit_repository
        self._replay_repository = replay_repository
        self._context_loader = context_loader
        self._provider = provider
        self._transition_config = transition_config
        self._relationship_config = relationship_config
        self._clock = clock
        self._supabase_timeout = supabase_timeout
        self._archival_extraction_enabled = archival_extraction_enabled
        self._lease_owner = lease_owner

    # ─── Entry point ───────────────────────────────────────────────────────────

    async def execute(self, inp: ProcessTurnInput) -> ProcessTurnResult:
        if not isinstance(inp, ProcessTurnInput):
            raise TypeError("inp must be a ProcessTurnInput")
        if inp.mode is TurnMode.replay_attempt:
            committed = await self._replay(inp)
        else:
            committed = await self._execute_normal(inp)
        return parse_public_result(committed)

    # ─── Replay path (before the provider) ─────────────────────────────────────

    async def _replay(self, inp: ProcessTurnInput) -> CommittedTurn:
        logger.info("event=process_turn_attempt attempt=1 mode=replay")
        outcome = await run_blocking_read(
            "replay_turn",
            inp.budget,
            self._supabase_timeout,
            self._replay_repository.replay,
            inp.authenticated_user_id,
            inp.request_id,
            allowlist_exceptions=_REPOSITORY_ERRORS,
        )
        if outcome.status == "completed":
            logger.info("event=process_turn_replay")
            return outcome.committed
        if outcome.status == "request_in_progress":
            raise ConflictError(
                code="request_in_progress",
                message="Request is already in progress.",
                expected_revision=0,
            )
        raise ConflictError(
            code="request_replay_unavailable",
            message="Request replay is unavailable.",
            expected_revision=0,
        )

    # ─── Normal path with bounded revision retry ───────────────────────────────

    async def _execute_normal(self, inp: ProcessTurnInput) -> CommittedTurn:
        attempt = 1
        while True:
            logger.info("event=process_turn_attempt attempt=%s", attempt)
            result = await self._run_once(inp, attempt)
            if isinstance(result, CommittedTurn):
                logger.info("event=process_turn_commit_completed attempt=%s", attempt)
                return result

            # revision_mismatch: bounded retry (initial + exactly one retry).
            if attempt >= MAX_COMMIT_ATTEMPTS:
                logger.info(
                    "event=process_turn_conflict_exhausted attempt=%s", attempt
                )
                raise ConflictError(
                    code="revision_mismatch",
                    message="Profile revision changed concurrently.",
                    expected_revision=result.expected_revision,
                    actual_revision=result.actual_revision,
                )

            logger.info("event=process_turn_revision_conflict attempt=%s", attempt)
            # Verify budget before retrying: an exhausted deadline never retries.
            if inp.budget.remaining_before_reserve <= 0.0:
                raise DeadlineExceeded()
            attempt += 1

    async def _run_once(
        self, inp: ProcessTurnInput, attempt: int
    ) -> CommittedTurn | _RevisionConflict:
        budget = inp.budget
        if budget.remaining_before_reserve <= 0.0:
            raise DeadlineExceeded()
        current_time = self._clock()
        t0 = _time.monotonic()

        # ---- 1. Load state + revision (read-only, never inserts) --------------
        state = await run_blocking_read(
            "load_user_state",
            budget,
            self._supabase_timeout,
            self._state_repository.load,
            inp.authenticated_user_id,
            current_time,
            allowlist_exceptions=_REPOSITORY_ERRORS,
        )

        try:
            emotional_state = migrate_legacy_snapshot(state.emotional_state)
        except Exception:
            raise TurnExecutionError(
                TurnErrorCode.internal_error, "Invalid persisted emotional state."
            ) from None

        if state.relationship_state:
            try:
                relationship = migrate_legacy_relationship_snapshot(
                    state.relationship_state
                )
            except Exception:
                raise TurnExecutionError(
                    TurnErrorCode.internal_error,
                    "Invalid persisted relationship state.",
                ) from None
        else:
            relationship = RelationshipStateV1.neutral(timestamp=current_time)

        # ---- 2. Load trusted context (read-only, authorized) ------------------
        user_state = {
            "persona_config": state.persona_config,
            "user_profile": state.user_profile,
        }
        try:
            loaded_context_data = await run_blocking_read(
                "load_context",
                budget,
                self._supabase_timeout,
                self._context_loader,
                inp.authenticated_user_id,
                inp.user_message,
                user_state,
                allowlist_exceptions=(TrustedContextError,),
            )
        except TrustedContextError:
            logger.error("event=provider_input_invalid stage=load_context")
            raise TurnExecutionError(
                TurnErrorCode.provider_invalid_request,
                "Loaded context data is structurally invalid.",
            )

        # ---- 3. Appraisal (LLM, outside the transaction) ----------------------
        appraisal = await self._provider.appraise(inp.user_message, budget)

        # ---- 4. Transitions (pure domain, outside the transaction) ------------
        transition_result = transition(
            previous_state=emotional_state,
            appraisal=appraisal,
            current_time=current_time,
            config=self._transition_config,
        )
        new_state = transition_result.state
        relationship = transition_relationship(
            previous_state=relationship,
            appraisal=appraisal,
            current_time=current_time,
            config=self._relationship_config,
        )

        # ---- 5. Provider envelope (pure domain, no I/O) -----------------------
        adaptation_strategy = ""
        trusted_policy = self._provider.build_trusted_policy(
            new_state, relationship, adaptation_strategy
        )
        try:
            context_bundle = build_context_bundle(
                trusted_policy=trusted_policy,
                loaded_data=loaded_context_data,
            )
            envelope_result = build_envelope(context_bundle, inp.user_message)
            generation_messages = envelope_result.messages
        except TrustedContextError:
            logger.error("event=provider_input_invalid stage=generation")
            raise TurnExecutionError(
                TurnErrorCode.provider_invalid_request,
                "Provider input envelope construction failed.",
            )

        # ---- 6. Generation (LLM, outside the transaction) ---------------------
        response_text = await self._provider.generate(generation_messages, budget)

        duration_ms = max(0, int((_time.monotonic() - t0) * 1000))

        # ---- 7/8/9. Snapshots v1, replay payload, outbox events ---------------
        emotional_snapshot = new_state.to_dict()
        relationship_snapshot = relationship.to_dict()

        # assistant_message_id is generated BEFORE the commit (secure UUID);
        # the user message id continues to be derived from request_id.
        assistant_message_id = str(_uuid.uuid4())
        public_emotion = project_public_emotion(new_state, appraisal)
        replay_payload = {
            "response": response_text,
            "emotion_state": public_emotion.model_dump(),
            "message_id": assistant_message_id,
            "duration_ms": duration_ms,
        }

        outbox_events: list[tuple[str, Mapping[str, Any], str]] = []
        if self._archival_extraction_enabled:
            outbox_events.append(build_archival_outbox_event(inp.request_id))

        # ---- 10. Atomic commit with CAS (exactly one operation) ---------------
        try:
            return await run_blocking_write(
                "commit_turn",
                budget,
                self._supabase_timeout,
                self._commit_repository.commit,
                authenticated_user_id=inp.authenticated_user_id,
                request_id=inp.request_id,
                expected_revision=state.revision,
                user_message=inp.user_message,
                assistant_message=response_text,
                emotional_state=emotional_snapshot,
                relationship_state=relationship_snapshot,
                public_response=response_text,
                outbox_events=outbox_events,
                replay_payload=replay_payload,
                lease_owner=self._lease_owner,
                allowlist_exceptions=_REPOSITORY_ERRORS,
            )
        except ConflictError as exc:
            if exc.code == "revision_mismatch":
                return _RevisionConflict(
                    expected_revision=exc.expected_revision,
                    actual_revision=exc.actual_revision,
                )
            raise
