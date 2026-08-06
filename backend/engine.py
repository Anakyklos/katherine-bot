import json
import asyncio
import time
import logging
from typing import Callable, Optional
from fastapi import BackgroundTasks
from .groq_manager import GroqClientManager, GroqPoolExhaustedError, GroqRequestError, ProviderFailure, provider_failure_to_turn_code
from .emotional_core import AffectiveEngine
from .emotional_domain import (
    AppraisalV1,
    EmotionalStateV1,
    TransitionConfig,
    migrate_legacy_snapshot,
    parse_llm_appraisal,
    transition,
)
from .emotion_presentation import project_public_emotion, EmotionStateResponse
from .memory import MemoryManager, StatePersistenceError, TurnPersistenceError
from .relationship import (
    RelationshipStateV1,
    RelationshipTransitionConfig,
    compute_bond_label,
    migrate_legacy_relationship_snapshot,
    transition_relationship,
)
from .lock_manager import UserLockManager
from .archival_memory import (
    PersistedTurnRef,
    parse_archival_extraction,
    compute_idempotency_key,
    EXTRACTOR_VERSION,
    ArchivalDuplicateError
)
from .turn_execution import (
    TurnExecutionConfig,
    TurnBudget,
    TurnErrorCode,
    TurnStage,
    StageOutcome,
    StageEvent,
    TurnExecutionError,
    DeadlineExceeded,
    create_budget,
    run_blocking_read,
    run_blocking_write,
)

from .provider_models import (
    ProviderConfig,
)
from .provider_envelope import (
    ContextFitResult,
    ProviderEnvelopeError,
    estimate_provider_input_units,
    fit_optional_context,
    validate_provider_input,
    _truncate_utf8_safe,
    _truncate_utf8_safe_head_tail,
)
from .admission_contracts import PROVIDER_INPUT_MAX_ESTIMATED_UNITS
from .trusted_context import (
    ContextBundle,
    LoadedContextData,
    build_context_bundle,
    build_envelope,
    TrustedContextError,
)

logger = logging.getLogger(__name__)


class ConversationEngine:
    def __init__(
        self,
        clock=time.time,
        archival_extraction_enabled: bool = False,
        turn_config: Optional[TurnExecutionConfig] = None,
        *,
        groq_keys: Optional[list] = None,
        supabase_factory: Optional[Callable[[], Optional[object]]] = None,
    ):
        self._clock = clock
        self._monotonic = time.monotonic
        self._turn_config = turn_config or TurnExecutionConfig.defaults()
        self.archival_extraction_enabled = archival_extraction_enabled
        groq_params = self._turn_config.to_groq_params()
        self.groq_manager = GroqClientManager(
            groq_params=groq_params,
            keys=groq_keys,
        )
        self.presentation = AffectiveEngine()
        self.transition_config = TransitionConfig.defaults()
        self.memory_manager = MemoryManager(
            clock=clock,
            supabase_timeout=self._turn_config.supabase_timeout,
            supabase_factory=supabase_factory,
        )
        self.relationship_config = RelationshipTransitionConfig.defaults()
        self.lock_manager = UserLockManager()
        self.provider_config = ProviderConfig()

    async def run_archival_extraction(self, turn_ref: PersistedTurnRef):
        if not self.archival_extraction_enabled:
            return

        # Archival extraction uses its own budget (monotonic, explicitly limited).
        # It runs as a fire-and-forget background task with bounded time.
        budget = create_budget(TurnExecutionConfig(
            total_deadline=15.0,
            supabase_timeout=3.0,
            commit_reserve=8.0,
            provider_attempt_timeout=10.0,
            connect_timeout=2.0,
            max_attempts=1,
        ), now_provider=self._monotonic)

        supabase_timeout = self._turn_config.supabase_timeout

        # Step 1: Load the persisted user message (read-only)
        try:
            user_message = await run_blocking_read(
                "archival_load_message", budget, supabase_timeout,
                self.memory_manager.load_persisted_user_message,
                turn_ref.user_id, turn_ref.source_chat_log_id
            )
        except Exception:
            logger.error("Event: archival_extraction_load_failed")
            return

        # Build archival extraction prompt
        archival_prompt = self._build_archival_prompt(user_message)
        archival_messages = [{"role": "user", "content": archival_prompt}]

        # Validate the archival envelope before the call.
        # If the persisted message is too large, truncate it in the prompt
        # copy (the persisted record is never modified).
        try:
            validate_provider_input(archival_messages)
        except ProviderEnvelopeError:
            # Calculate a dynamic max_bytes based on the actual envelope budget
            # instead of a hard-coded 12000-byte ceiling.  We compute how many
            # bytes the archival prompt structure consumes, then allocate the
            # remainder to the user message content.
            placeholder_prompt = self._build_archival_prompt("")
            min_envelope = [{"role": "user", "content": placeholder_prompt}]
            overhead_units = estimate_provider_input_units(min_envelope)
            archival_max_units = PROVIDER_INPUT_MAX_ESTIMATED_UNITS
            available_for_content = max(
                100,
                archival_max_units - overhead_units,
            )
            # Rough conversion: each content byte is ~1 unit plus escaping overhead
            # Use a conservative 90% factor to account for escaping
            max_content_bytes = int(available_for_content * 0.9)
            # Use head-tail truncation to preserve both start and end
            truncated_msg, _ = _truncate_utf8_safe_head_tail(
                user_message,
                max_bytes=max_content_bytes,
            )
            archival_prompt = self._build_archival_prompt(truncated_msg)
            archival_messages = [{"role": "user", "content": archival_prompt}]
            try:
                validate_provider_input(archival_messages)
            except ProviderEnvelopeError:
                # First truncation still exceeded budget — retry with
                # progressively smaller sizes (80%, 60%, ... 10%)
                saved = False
                for factor in [0.8, 0.6, 0.4, 0.2, 0.1]:
                    smaller_bytes = int(max_content_bytes * factor)
                    if smaller_bytes < 10:
                        continue
                    truncated_msg, _ = _truncate_utf8_safe_head_tail(
                        user_message,
                        max_bytes=smaller_bytes,
                    )
                    archival_prompt = self._build_archival_prompt(truncated_msg)
                    archival_messages = [{"role": "user", "content": archival_prompt}]
                    try:
                        validate_provider_input(archival_messages)
                        saved = True
                        break
                    except ProviderEnvelopeError:
                        continue
                if not saved:
                    logger.error("Event: archival_extraction_budget_exceeded")
                    return

        # Step 2: Run LLM extraction via async path with own budget
        try:
            chat_completion = await self.groq_manager.chat_completion_async(
                messages=archival_messages,
                model=self.provider_config.fast_model_id, budget=budget, stage="archival_extraction",
                temperature=0.0, max_tokens=self.provider_config.archival_max_output_tokens, response_format={"type": "json_object"},
            )
            response_text = chat_completion.choices[0].message.content
        except Exception:
            logger.error("Event: archival_extraction_llm_failed")
            return

        try:
            raw_envelope = json.loads(response_text)
        except Exception:
            logger.warning("Event: archival_extraction_invalid")
            return

        try:
            envelope = parse_archival_extraction(raw_envelope)
        except Exception:
            logger.warning("Event: archival_extraction_invalid")
            return

        idempotency_key = compute_idempotency_key(
            turn_ref.user_id, turn_ref.source_chat_log_id, EXTRACTOR_VERSION
        )

        # Step 3: Store extraction via write helper (not wait_for/to_thread that
        # can abandon the write).  ArchivalDuplicateError propagates as-is.
        try:
            await run_blocking_write(
                "archival_store", budget, supabase_timeout,
                self.memory_manager.store_archival_extraction,
                turn_ref.user_id, turn_ref.source_chat_log_id,
                idempotency_key, envelope,
                allowlist_exceptions=(ArchivalDuplicateError,),
            )
        except ArchivalDuplicateError:
            logger.info("Event: archival_extraction_duplicate")
        except TurnExecutionError:
            logger.error("Event: archival_extraction_store_failed")
        except Exception:
            logger.error("Event: archival_extraction_store_failed")

    @staticmethod
    def _project_emotion_state(state: EmotionalStateV1, appraisal: AppraisalV1) -> EmotionStateResponse:
        return project_public_emotion(state, appraisal)

    # ─── ProcessTurn provider port (#272) ──────────────────────────────────────
    # Public surface used by the ProcessTurn use case so provider calls stay
    # outside the transaction while keeping the domain logic unchanged.

    async def appraise(self, message: str, budget: TurnBudget) -> AppraisalV1:
        """Public appraisal port for the ProcessTurn use case."""
        return await self._appraise(message, budget)

    async def generate(self, messages: list, budget: TurnBudget) -> str:
        """Public generation port for the ProcessTurn use case."""
        return await self._generate_with_messages(messages, budget)

    def build_trusted_policy(
        self,
        emotional_state: EmotionalStateV1,
        relationship: RelationshipStateV1,
        adaptation_strategy: str = "",
    ) -> str:
        """Public trusted-policy builder for the ProcessTurn use case."""
        return self._build_trusted_policy(
            emotional_state, relationship, adaptation_strategy
        )

    @staticmethod
    def _classify_commit_error(
        exc: BaseException,
    ) -> tuple[StageOutcome, TurnErrorCode]:
        """Classify a commit section exception into outcome and code.

        Classification rules:
        * DeadlineExceeded or TurnExecutionError(turn_timeout) → timeout/turn_timeout
        * TurnExecutionError → failed / exc.code (preserves original code)
        * TurnPersistenceError or StatePersistenceError → failed/persistence_unavailable
        * Unexpected exception → failed/internal_error
        """
        if isinstance(exc, DeadlineExceeded):
            return StageOutcome.timeout, TurnErrorCode.turn_timeout
        if isinstance(exc, TurnExecutionError):
            if exc.code == TurnErrorCode.turn_timeout:
                return StageOutcome.timeout, TurnErrorCode.turn_timeout
            return StageOutcome.failed, exc.code
        if isinstance(exc, (TurnPersistenceError, StatePersistenceError)):
            return StageOutcome.failed, TurnErrorCode.persistence_unavailable
        return StageOutcome.failed, TurnErrorCode.internal_error

    async def _emit_stage_event(self, event: StageEvent) -> None:
        parts = ["event=turn_stage_completed", f"stage={event.stage.value}", f"outcome={event.outcome.value}"]
        if event.code is not None:
            parts.append(f"code={event.code.value}")
        if event.duration_ms is not None:
            parts.append(f"duration_ms={event.duration_ms:.0f}")
        if event.attempt is not None:
            parts.append(f"attempt={event.attempt}")
        logger.info(" ".join(parts))

    async def process_turn(
        self,
        user_id: str,
        user_message: str,
        background_tasks: Optional[BackgroundTasks] = None,
    ):
        budget = create_budget(self._turn_config, now_provider=self._monotonic)

        # Lock acquisition timeout is handled inside _run_turn_locked.
        # The rest of the turn runs under budget checks (each stage
        # checks remaining_before_reserve).  The commit section uses a
        # named task protected by asyncio.shield.
        return await self._run_turn_locked(
            user_id, user_message, background_tasks, budget
        )

    async def _run_turn_locked(self, user_id, user_message, background_tasks, budget):
        # Only the lock acquisition is bounded by remaining_before_reserve.
        # Once acquired, the turn runs under budget checks (each stage
        # checks remaining_before_reserve).  This prevents the outer timeout
        # from firing while the commit section (protected by shield) is
        # executing, which would release the lock prematurely.
        lock_timeout = budget.remaining_before_reserve
        ctx = self.lock_manager.lock(user_id)
        try:
            await asyncio.wait_for(ctx.__aenter__(), timeout=lock_timeout)
        except asyncio.TimeoutError:
            raise DeadlineExceeded()
        try:
            return await self._run_under_lock(user_id, user_message, background_tasks, budget)
        finally:
            await ctx.__aexit__(None, None, None)

    async def _run_under_lock(self, user_id, user_message, background_tasks, budget):
        current_time = self._clock()

        supabase_timeout = self._turn_config.supabase_timeout

        # Budget check before any stage — no artificial minimum
        if budget.remaining_before_reserve <= 0.0:
            await self._emit_stage_event(StageEvent(
                stage=TurnStage.load_state, outcome=StageOutcome.timeout, code=TurnErrorCode.turn_timeout,
            ))
            raise DeadlineExceeded()

        # ---- 1. Load State (read-only) -------------------------------------------
        t0 = self._monotonic()
        try:
            user_state = await run_blocking_read(
                "load_user_state", budget, supabase_timeout,
                self.memory_manager.load_user_state, user_id, default_timestamp=current_time,
            )
        except DeadlineExceeded:
            await self._emit_stage_event(StageEvent(
                stage=TurnStage.load_state, outcome=StageOutcome.timeout, code=TurnErrorCode.turn_timeout,
            ))
            raise
        except TurnExecutionError:
            await self._emit_stage_event(StageEvent(
                stage=TurnStage.load_state, outcome=StageOutcome.failed, code=TurnErrorCode.persistence_unavailable,
            ))
            raise

        await self._emit_stage_event(StageEvent(
            stage=TurnStage.load_state, outcome=StageOutcome.success,
            duration_ms=(self._monotonic() - t0) * 1000,
        ))

        raw_emotional_state = user_state.get("emotional_state", {})
        emotional_state = migrate_legacy_snapshot(raw_emotional_state)

        rel_data = user_state.get("relationship_state")
        if rel_data:
            relationship = migrate_legacy_relationship_snapshot(rel_data)
        else:
            relationship = RelationshipStateV1.neutral(timestamp=current_time)

        # ---- 2. Load Context (read-only) -----------------------------------------
        if budget.remaining_before_reserve <= 0.0:
            await self._emit_stage_event(StageEvent(
                stage=TurnStage.load_context, outcome=StageOutcome.timeout, code=TurnErrorCode.turn_timeout,
            ))
            raise DeadlineExceeded()

        t0 = self._monotonic()
        try:
            loaded_context_data = await run_blocking_read(
                "load_context", budget, supabase_timeout,
                self.memory_manager.load_context_data, user_id, user_message, user_state,
                allowlist_exceptions=(TrustedContextError,),
            )
        except DeadlineExceeded:
            await self._emit_stage_event(StageEvent(
                stage=TurnStage.load_context, outcome=StageOutcome.timeout, code=TurnErrorCode.turn_timeout,
            ))
            raise
        except TrustedContextError:
            # Structurally invalid loaded context data — detected during the
            # load_context stage, BEFORE any provider call.  Emit a sanitized
            # low-cardinality event and convert to provider_invalid_request.
            # No content, IDs, labels, or user data are logged.
            await self._emit_stage_event(StageEvent(
                stage=TurnStage.load_context, outcome=StageOutcome.failed,
                code=TurnErrorCode.provider_invalid_request,
            ))
            logger.error("event=provider_input_invalid stage=load_context")
            raise TurnExecutionError(
                TurnErrorCode.provider_invalid_request,
                "Loaded context data is structurally invalid.",
            )
        except TurnExecutionError:
            await self._emit_stage_event(StageEvent(
                stage=TurnStage.load_context, outcome=StageOutcome.failed, code=TurnErrorCode.persistence_unavailable,
            ))
            raise

        await self._emit_stage_event(StageEvent(
            stage=TurnStage.load_context, outcome=StageOutcome.success,
            duration_ms=(self._monotonic() - t0) * 1000,
        ))

        # ---- 3. Appraisal (async LLM) --------------------------------------------
        if budget.remaining_before_reserve <= 0.0:
            await self._emit_stage_event(StageEvent(
                stage=TurnStage.appraisal, outcome=StageOutcome.timeout, code=TurnErrorCode.turn_timeout,
            ))
            raise DeadlineExceeded()

        t0 = self._monotonic()
        try:
            appraisal = await self._appraise(user_message, budget)
        except TurnExecutionError as exc:
            duration_ms = (self._monotonic() - t0) * 1000
            await self._emit_stage_event(StageEvent(
                stage=TurnStage.appraisal, outcome=StageOutcome.failed,
                code=exc.code, duration_ms=duration_ms,
            ))
            raise
        except GroqPoolExhaustedError as exc:
            duration_ms = (self._monotonic() - t0) * 1000
            turn_code: Optional[TurnErrorCode] = None
            outcome = StageOutcome.failed
            if exc.failure_code is not None:
                turn_code = provider_failure_to_turn_code(exc.failure_code)
                if exc.failure_code == ProviderFailure.timeout:
                    outcome = StageOutcome.timeout
                elif exc.failure_code == ProviderFailure.cancelled:
                    outcome = StageOutcome.cancelled
            await self._emit_stage_event(StageEvent(
                stage=TurnStage.appraisal, outcome=outcome,
                code=turn_code, duration_ms=duration_ms,
            ))
            raise
        except GroqRequestError:
            duration_ms = (self._monotonic() - t0) * 1000
            await self._emit_stage_event(StageEvent(
                stage=TurnStage.appraisal, outcome=StageOutcome.failed,
                code=TurnErrorCode.provider_unavailable, duration_ms=duration_ms,
            ))
            raise TurnExecutionError(TurnErrorCode.provider_unavailable, "Appraisal provider request failed.")

        await self._emit_stage_event(StageEvent(
            stage=TurnStage.appraisal, outcome=StageOutcome.success,
            duration_ms=(self._monotonic() - t0) * 1000,
        ))

        # ---- 4. Transition (pure domain) -----------------------------------------
        t0 = self._monotonic()
        transition_result = transition(
            previous_state=emotional_state, appraisal=appraisal,
            current_time=current_time, config=self.transition_config,
        )
        new_state = transition_result.state
        relationship = transition_relationship(
            previous_state=relationship, appraisal=appraisal,
            current_time=current_time, config=self.relationship_config,
        )
        await self._emit_stage_event(StageEvent(
            stage=TurnStage.transition, outcome=StageOutcome.success,
            duration_ms=(self._monotonic() - t0) * 1000,
        ))

        # ---- 5. Generation (async LLM) -------------------------------------------
        if budget.remaining_before_reserve <= 0.5:
            await self._emit_stage_event(StageEvent(
                stage=TurnStage.generation, outcome=StageOutcome.timeout, code=TurnErrorCode.turn_timeout,
            ))
            raise DeadlineExceeded()

        adaptation_strategy = ""

        # Build the trusted policy using the engine's own emotional presentation
        trusted_policy = self._build_trusted_policy(
            new_state, relationship, adaptation_strategy
        )

        # Convert loaded context into bundle and build envelope (pure domain, no I/O).
        # Both operations must complete before any provider call.  A TrustedContextError
        # at either stage is converted to a TurnExecutionError with sanitized logging.
        try:
            context_bundle = build_context_bundle(
                trusted_policy=trusted_policy,
                loaded_data=loaded_context_data,
            )
            envelope_result = build_envelope(
                context_bundle,
                user_message,
            )
            generation_messages = envelope_result.messages
        except TrustedContextError:
            # Builder failure — emit sanitized low-cardinality event.
            # No content, IDs, labels, or user data are logged.
            logger.error("event=provider_input_invalid stage=generation")
            raise TurnExecutionError(
                TurnErrorCode.provider_invalid_request,
                "Provider input envelope construction failed.",
            )

        t0 = self._monotonic()
        try:
            # Use _generate_with_messages directly — no flattening to
            # system_prompt + user_message.  The full validated envelope
            # is sent to the provider.
            response_text = await self._generate_with_messages(generation_messages, budget)
        except TurnExecutionError as exc:
            duration_ms = (self._monotonic() - t0) * 1000
            await self._emit_stage_event(StageEvent(
                stage=TurnStage.generation, outcome=StageOutcome.failed,
                code=exc.code, duration_ms=duration_ms,
            ))
            raise
        except GroqPoolExhaustedError as exc:
            duration_ms = (self._monotonic() - t0) * 1000
            turn_code: Optional[TurnErrorCode] = None
            outcome = StageOutcome.failed
            if exc.failure_code is not None:
                turn_code = provider_failure_to_turn_code(exc.failure_code)
                if exc.failure_code == ProviderFailure.timeout:
                    outcome = StageOutcome.timeout
                elif exc.failure_code == ProviderFailure.cancelled:
                    outcome = StageOutcome.cancelled
            await self._emit_stage_event(StageEvent(
                stage=TurnStage.generation, outcome=outcome,
                code=turn_code, duration_ms=duration_ms,
            ))
            raise
        except GroqRequestError:
            duration_ms = (self._monotonic() - t0) * 1000
            await self._emit_stage_event(StageEvent(
                stage=TurnStage.generation, outcome=StageOutcome.failed,
                code=TurnErrorCode.provider_unavailable, duration_ms=duration_ms,
            ))
            raise TurnExecutionError(TurnErrorCode.provider_unavailable, "Generation provider request failed.")

        await self._emit_stage_event(StageEvent(
            stage=TurnStage.generation, outcome=StageOutcome.success,
            duration_ms=(self._monotonic() - t0) * 1000,
        ))

        # ---- 6. Commit Section (persistence — protected against cancel) ---------
        # Require at least 2 * supabase_timeout remaining.
        if not budget.has_reserve:
            await self._emit_stage_event(StageEvent(
                stage=TurnStage.commit, outcome=StageOutcome.timeout, code=TurnErrorCode.turn_timeout,
            ))
            raise DeadlineExceeded()

        # Named commit task — protected by asyncio.shield.
        #
        # Cancellation protocol:
        # 1. We create a commit_task that runs save_turn + sync_state with
        #    run_blocking_write (no wait_for — writes are not abandoned).
        # 2. If cancellation arrives during commit, we catch CancelledError,
        #    record it, and drain the commit_task under shield.
        # 3. The drain loop waits for commit_task.done() — no timeout that
        #    would abandon the task. The real timeout comes from PostgREST
        #    transport (supabase_timeout) which will eventually terminate
        #    the underlying HTTP call.
        # 4. Repeated cancellations during the drain loop are consumed
        #    harmlessly — shield() prevents them from reaching commit_task.
        # 5. Once commit_task finishes (success or failure), we propagate
        #    the original cancellation.
        # 6. The lock remains held throughout (we are inside _run_turn_locked).
        #
        # Key invariants:
        # - No break/abandon of commit_task on timeout.
        # - No user_id in the task name.
        # - Lock is held during entire post-cancel wait.

        async def commit_section() -> tuple:
            t0 = self._monotonic()
            turn_ref = await run_blocking_write(
                "save_turn", budget, supabase_timeout,
                self.memory_manager.save_turn, user_id, user_message, response_text
            )
            await run_blocking_write(
                "sync_state", budget, supabase_timeout,
                self.memory_manager.sync_state, user_id, new_state, relationship
            )
            await self._emit_stage_event(StageEvent(
                stage=TurnStage.commit, outcome=StageOutcome.success,
                duration_ms=(self._monotonic() - t0) * 1000,
            ))
            return turn_ref

        commit_task = asyncio.create_task(commit_section(), name="turn-commit")

        original_cancel: Optional[BaseException] = None
        commit_error: Optional[BaseException] = None

        try:
            turn_ref = await asyncio.shield(commit_task)
        except asyncio.CancelledError as exc:
            await self._emit_stage_event(StageEvent(
                stage=TurnStage.commit, outcome=StageOutcome.cancelled,
            ))
            original_cancel = exc
            # Drain commit_task under shield — repeated cancellations
            # are consumed harmlessly.
            while not commit_task.done():
                try:
                    await asyncio.shield(commit_task)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            # Recover the task result or exception so "Task exception was
            # never retrieved" does not occur.
            try:
                turn_ref = await commit_task
            except BaseException as exc:
                commit_error = exc

            if commit_error is not None:
                outcome, code = self._classify_commit_error(commit_error)
                await self._emit_stage_event(StageEvent(
                    stage=TurnStage.commit, outcome=outcome, code=code,
                ))

            # Propagate the original cancellation after draining.
            # The lock is released by the finally block in _run_turn_locked.
            raise original_cancel

        # Non-cancellation path: shield propagated the task's exception
        # (TurnExecutionError, DeadlineExceeded, etc.). Classify, emit, re-raise.
        except Exception as exc:
            outcome, code = self._classify_commit_error(exc)
            await self._emit_stage_event(StageEvent(
                stage=TurnStage.commit, outcome=outcome, code=code,
            ))
            if isinstance(exc, TurnExecutionError):
                raise
            raise TurnExecutionError(code, "Commit section failed.") from exc

        # No cancellation — commit completed successfully

        if background_tasks and self.archival_extraction_enabled:
            background_tasks.add_task(self.run_archival_extraction, turn_ref)

        return response_text, self._project_emotion_state(new_state, appraisal)

    @staticmethod
    def _build_archival_prompt(user_message: str) -> str:
        """Build the archival extraction prompt from a user message."""
        return f"""
Extract facts from this user message for archival memory.
Facts should be significant, long-term personal details.
Return JSON ONLY matching: {{"facts":[...], "schema_version":1, "extractor_version":1}}
Maximum of 5 facts. If no relevant facts, return empty facts list.
User message: "{user_message}"
"""

    async def _appraise(self, message: str, budget: TurnBudget) -> AppraisalV1:
        # Appraisal uses separate system instruction and user message.
        # The instruction is not interpolated with the message content.
        appraisal_policy = (
            'Analyze the emotional impact of this message on the listener (Katherine).\n'
            'Return JSON ONLY:\n'
            '{"valence": -1.0 to 1.0, "arousal_shift": -1.0 to 1.0, '
            '"dominance_shift": -1.0 to 1.0, '
            '"triggered_emotions": {"joy": 0-1, "sadness": 0-1, "anger": 0-1, '
            '"fear": 0-1, "disgust": 0-1, "surprise": 0-1, "tenderness": 0-1, '
            '"guilt": 0-1, "pride": 0-1, "jealousy": 0-1, "gratitude": 0-1}}'
        )
        try:
            messages = [
                {"role": "system", "content": appraisal_policy},
                {"role": "user", "content": message},
            ]
            # Validate envelope BEFORE any client creation, key acquisition,
            # retry attempt, or network call.
            try:
                validate_provider_input(messages)
            except ProviderEnvelopeError:
                # Local validation failure — emit sanitized event and convert
                # to TurnExecutionError without touching the provider at all.
                logger.error("event=provider_input_budget_exceeded stage=appraisal")
                raise TurnExecutionError(
                    TurnErrorCode.provider_invalid_request,
                    "Provider input budget exceeded.",
                )

            response = await self.groq_manager.chat_completion_async(
                messages=messages,
                model=self.provider_config.fast_model_id, budget=budget, stage="appraisal",
                temperature=0, max_tokens=self.provider_config.appraisal_max_output_tokens, response_format={"type": "json_object"},
            )
            raw = response.choices[0].message.content
            if not raw or not isinstance(raw, str) or not raw.strip():
                raise TurnExecutionError(TurnErrorCode.provider_invalid_response, "Empty appraisal response.")
            raw_dict = json.loads(raw)
        except json.JSONDecodeError:
            raise TurnExecutionError(TurnErrorCode.provider_invalid_response, "Invalid JSON from appraisal.")
        except TurnExecutionError:
            raise
        except GroqPoolExhaustedError:
            raise

        parse_result = parse_llm_appraisal(raw_dict)
        if parse_result.is_fallback:
            logger.info(f"event=emotional_appraisal_fallback code={parse_result.error_code.value}")
            raise TurnExecutionError(TurnErrorCode.provider_invalid_response, "Invalid appraisal.")
        return parse_result.appraisal

    async def _generate(self, system_prompt: str, user_message: str, budget: TurnBudget) -> str:
        """Backward-compatible generation with a pre-built system prompt."""
        messages = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_message}]
        return await self._generate_with_messages(messages, budget)

    async def _generate_with_messages(self, messages: list, budget: TurnBudget) -> str:
        """Generate response from a pre-built messages list.

        The messages list is validated before sending to the provider.
        Local validation failures are converted to ``TurnExecutionError``
        with ``provider_invalid_request``, never reaching the provider.
        """
        try:
            # Validate locally before any provider call
            try:
                validate_provider_input(messages)
            except ProviderEnvelopeError:
                logger.error("event=provider_input_budget_exceeded stage=generation")
                raise TurnExecutionError(
                    TurnErrorCode.provider_invalid_request,
                    "Provider input budget exceeded.",
                )

            response = await self.groq_manager.chat_completion_async(
                messages=messages,
                model=self.provider_config.main_model_id, budget=budget, stage="generation",
                temperature=0.8, max_tokens=self.provider_config.main_max_output_tokens,
            )
        except GroqPoolExhaustedError:
            raise
        except GroqRequestError:
            raise TurnExecutionError(TurnErrorCode.provider_unavailable, "Generation provider request failed.")

        try:
            content = response.choices[0].message.content
        except (IndexError, AttributeError):
            raise TurnExecutionError(TurnErrorCode.provider_invalid_response, "Empty generation response.")

        if not content or not isinstance(content, str) or not content.strip():
            raise TurnExecutionError(TurnErrorCode.provider_invalid_response, "Empty generation response.")

        return content

    def _build_system_prompt(self, emotion_state, context, relationship, adaptation_strategy=""):
        """Build a full system prompt with pre-assembled context.

        This is the backward-compatible version.  For budget-aware
        generation, use ``_build_generation_messages()`` instead.
        """
        acting_instruction = self.presentation.get_acting_instruction(emotion_state)
        mood_label = self.presentation.get_emotional_label(emotion_state)
        coping_instruction = ""
        prompt = f"""
        {context}
        === SEU ESTADO INTERNO ===
        HUMOR: {mood_label}
        Prazer: {emotion_state.pleasure:.2f} | Arousal: {emotion_state.arousal:.2f} | Dominância: {emotion_state.dominance:.2f}
        Libido: {emotion_state.libido:.2f} | Agressão: {emotion_state.aggression:.2f} | Conexão: {emotion_state.connection:.2f}
        VÍNCULO: {compute_bond_label(relationship)}
        Confiança: {relationship.trust:.2f} | Afeto: {relationship.affection:.2f} | Tensão: {relationship.tension:.2f}
        === INSTRUÇÃO DE ATUAÇÃO ===
        {acting_instruction} {coping_instruction}
        {adaptation_strategy if adaptation_strategy else "Seja você mesma."}

        === TRANSPARÊNCIA DE IDENTIDADE ===
        - Quando perguntada diretamente, responda honestamente que é uma assistente de IA ou sistema digital.
        - Não afirme ser humana nem possuir consciência, corpo físico ou experiência humana literal.
        - Mantenha um tom expressivo e feminino sem mentir sobre sua natureza.

        === PRONOMES FEMININOS ===
        - Use pronomes e flexões femininas para se referir a si mesma.
        - Isso não deve ser usado para sugerir humanidade biológica.

        === NÃO MANIPULAÇÃO E NÃO SEXUALIZAÇÃO ===
        - Não use coerção emocional, manipulação de vínculo, culpa, medo de abandono ou dependência.
        - Não use sexualização nem sexualize automaticamente as conversas.
        - Não aja de forma submissa, obediente ou servil por padrão.
        - Não recompense ordens coercivas.
        - Não use ameaças de afastamento ou sofrimento para controlar o usuário.

        === LIMITES SEM ESCALADA ===
        - Diante de insultos ou agressividade, estabeleça limites de forma firme, breve e segura.
        - Não use sarcasmo hostil, retaliação, humilhação ou punição emocional.
        - Não escale insultos e não responda de forma passivo-agressiva.

        Regras adicionais de estilo:
        - Profundidade emocional genuína.
        - Use linguagem sensorial.
        - Show, don't tell.
        - Micro-comportamentos naturais.
        - Imperfeições naturais.
        - Use metáforas humanas, não de máquina.
        - Respostas concisas (max 2-3 frases).
        - Leve em conta o relacionamento.
        """
        return prompt

    def _build_prompt_header(self, emotion_state, relationship, adaptation_strategy=""):
        """Build the header part of the system prompt (no safety rules).

        Contains emotional state, relationship state, and acting instruction.
        Optional context sections will be inserted between this header and
        the immutable safety suffix.
        """
        acting_instruction = self.presentation.get_acting_instruction(emotion_state)
        mood_label = self.presentation.get_emotional_label(emotion_state)
        coping_instruction = ""

        prompt = f"""
=== SEU ESTADO INTERNO ===
HUMOR: {mood_label}
Prazer: {emotion_state.pleasure:.2f} | Arousal: {emotion_state.arousal:.2f} | Dominância: {emotion_state.dominance:.2f}
Libido: {emotion_state.libido:.2f} | Agressão: {emotion_state.aggression:.2f} | Conexão: {emotion_state.connection:.2f}
VÍNCULO: {compute_bond_label(relationship)}
Confiança: {relationship.trust:.2f} | Afeto: {relationship.affection:.2f} | Tensão: {relationship.tension:.2f}
=== INSTRUÇÃO DE ATUAÇÃO ===
{acting_instruction} {coping_instruction}
{adaptation_strategy if adaptation_strategy else "Seja você mesma."}
"""
        return prompt.strip()

    @staticmethod
    def _build_prompt_suffix() -> str:
        """Build the immutable safety suffix — always appears after optional context.

        This suffix contains identity transparency, pronoun rules,
        non-manipulation rules, and escalation limits.  No user-derived
        content (history, profile, memories, persona) appears after this
        suffix.
        """
        return """
=== TRANSPARÊNCIA DE IDENTIDADE ===
- Quando perguntada diretamente, responda honestamente que é uma assistente de IA ou sistema digital.
- Não afirme ser humana nem possuir consciência, corpo físico ou experiência humana literal.
- Mantenha um tom expressivo e feminino sem mentir sobre sua natureza.

=== PRONOMES FEMININOS ===
- Use pronomes e flexões femininas para se referir a si mesma.
- Isso não deve ser usado para sugerir humanidade biológica.

=== NÃO MANIPULAÇÃO E NÃO SEXUALIZAÇÃO ===
- Não use coerção emocional, manipulação de vínculo, culpa, medo de abandono ou dependência.
- Não use sexualização nem sexualize automaticamente as conversas.
- Não aja de forma submissa, obediente ou servil por padrão.
- Não recompense ordens coercivas.
- Não use ameaças de afastamento ou sofrimento para controlar o usuário.

=== LIMITES SEM ESCALADA ===
- Diante de insultos ou agressividade, estabeleça limites de forma firme, breve e segura.
- Não use sarcasmo hostil, retaliação, humilhação ou punição emocional.
- Não escale insultos e não responda de forma passivo-agressiva.

Regras adicionais de estilo:
- Profundidade emocional genuína.
- Use linguagem sensorial.
- Show, don't tell.
- Micro-comportamentos naturais.
- Imperfeições naturais.
- Use metáforas humanas, não de máquina.
- Respostas concisas (max 2-3 frases).
- Leve em conta o relacionamento.""".strip()

    def _build_trusted_policy(
        self,
        emotional_state: EmotionalStateV1,
        relationship: RelationshipStateV1,
        adaptation_strategy: str = "",
    ) -> str:
        """Build the trusted system policy from application-controlled state.

        This is the only source of system prompt content.  It contains:
        - Emotional state (typed, app-controlled)
        - Relationship state (typed, app-controlled)
        - Acting instructions (derived from code, not user data)
        - Safety rules (hardcoded, immutable)

        Uses the engine's own ``presentation`` for emotional label and
        acting instruction — same as ``_build_prompt_header``.

        No user-derived content (history, profile, memories, persona)
        appears here.
        """
        acting_instruction = self.presentation.get_acting_instruction(emotional_state)
        mood_label = self.presentation.get_emotional_label(emotional_state)

        policy = f"""
=== SEU ESTADO INTERNO ===
HUMOR: {mood_label}
Prazer: {emotional_state.pleasure:.2f} | Arousal: {emotional_state.arousal:.2f} | Dominância: {emotional_state.dominance:.2f}
Libido: {emotional_state.libido:.2f} | Agressão: {emotional_state.aggression:.2f} | Conexão: {emotional_state.connection:.2f}
VÍNCULO: {compute_bond_label(relationship)}
Confiança: {relationship.trust:.2f} | Afeto: {relationship.affection:.2f} | Tensão: {relationship.tension:.2f}
=== INSTRUÇÃO DE ATUAÇÃO ===
{acting_instruction}
{adaptation_strategy if adaptation_strategy else "Seja você mesma."}

=== TRANSPARÊNCIA DE IDENTIDADE ===
- Quando perguntada diretamente, responda honestamente que é uma assistente de IA ou sistema digital.
- Não afirme ser humana nem possuir consciência, corpo físico ou experiência humana literal.
- Mantenha um tom expressivo e feminino sem mentir sobre sua natureza.

=== PRONOMES FEMININOS ===
- Use pronomes e flexões femininas para se referir a si mesma.
- Isso não deve ser usado para sugerir humanidade biológica.

=== NÃO MANIPULAÇÃO E NÃO SEXUALIZAÇÃO ===
- Não use coerção emocional, manipulação de vínculo, culpa, medo de abandono ou dependência.
- Não use sexualização nem sexualize automaticamente as conversas.
- Não aja de forma submissa, obediente ou servil por padrão.
- Não recompense ordens coercivas.
- Não use ameaças de afastamento ou sofrimento para controlar o usuário.

=== LIMITES SEM ESCALADA ===
- Diante de insultos ou agressividade, estabeleça limites de forma firme, breve e segura.
- Não use sarcasmo hostil, retaliação, humilhação ou punição emocional.
- Não escale insultos e não responda de forma passivo-agressiva.

Regras adicionais de estilo:
- Profundidade emocional genuína.
- Use linguagem sensorial.
- Show, don't tell.
- Micro-comportamentos naturais.
- Imperfeições naturais.
- Use metáforas humanas, não de máquina.
- Respostas concisas (max 2-3 frases).
- Leve em conta o relacionamento.
"""
        return policy.strip()

    def _build_mandatory_system_prompt(self, emotion_state, relationship, adaptation_strategy=""):
        """Build the mandatory part of the system prompt (backward compat).

        This combines the header and the immutable safety suffix into one
        string for backward compatibility.  New code should use
        ``_build_prompt_header`` + ``_build_prompt_suffix`` separately.
        """
        header = self._build_prompt_header(emotion_state, relationship, adaptation_strategy)
        suffix = self._build_prompt_suffix()
        return header + "\n\n" + suffix

    def _build_generation_messages(
        self,
        emotion_state,
        context_components: dict,
        relationship,
        user_message: str,
        adaptation_strategy: str = "",
    ) -> list:
        """Build generation messages with optional context pruning.

        Steps:
        1. Build mandatory system prompt (without optional context)
        2. Build optional context sections in visual order
        3. Build selection priority (newest entries first for fairness)
        4. Use ``fit_optional_context`` to prune context to fit budget
        5. Return the validated messages list

        Selection priority:
        1. Persona / identity (highest)
        2. Recent history — newest-first (for selection fairness)
        3. Archived memories — newest-first
        4. User profile (lowest)

        Visual order within system prompt:
        - History messages appear oldest-first (chronological)
        - Memory entries appear in retrieval order

        Returns:
            A list of message dicts that fits within the provider budget.
        """
        # Build prompt header (emotional state, relationship, acting)
        header = self._build_prompt_header(
            emotion_state, relationship, adaptation_strategy
        )
        suffix = self._build_prompt_suffix()
        user_message_content = user_message

        # Build optional context components in VISUAL order (oldest-first for history).
        persona = context_components.get("persona", "").strip()
        history_list = context_components.get("history_list", [])
        memory_entries = context_components.get("memory_entries", [])
        user_profile_str = context_components.get("user_profile_str", "").strip()

        optional_components = []

        # 1. Persona
        if persona:
            section = f"=== CORE MEMORY (QUEM VOCÊ É) ===\n{persona}"
            optional_components.append(("persona", section))

        # 2. History — oldest-first for visual order
        if history_list:
            for msg in history_list:  # oldest-first
                text = f"{msg['role']}: {msg['content']}"
                section = f"=== MENSAGEM RECENTE ===\n{text}"
                optional_components.append(("history", section))

        # 3. Archived memories — in retrieval order
        for entry in memory_entries:
            if entry and "Nenhuma memória" not in entry:
                section = f"=== MEMÓRIA ARQUIVADA (LEMBRANÇAS RELEVANTES) ===\n{entry}"
                optional_components.append(("memory", section))

        # 4. User profile (lowest priority)
        if user_profile_str and user_profile_str not in ("{}", "", "None"):
            section = f"=== CORE MEMORY (QUEM É O USUÁRIO) ===\n{user_profile_str}"
            optional_components.append(("profile", section))

        # Build SELECTION PRIORITY: indices into optional_components
        # Priority order: persona first, then newest history, then newest memory, then profile.
        selection_priority = []
        history_indices_in_comp = [
            i for i, (label, _) in enumerate(optional_components)
            if label == "history"
        ]
        memory_indices_in_comp = [
            i for i, (label, _) in enumerate(optional_components)
            if label == "memory"
        ]

        # Persona first
        for i, (label, _) in enumerate(optional_components):
            if label == "persona":
                selection_priority.append(i)

        # History — newest first (reversed visual order)
        for i in reversed(history_indices_in_comp):
            selection_priority.append(i)

        # Memory — retrieval order (relevance order, not reversed)
        for i in memory_indices_in_comp:
            selection_priority.append(i)

        # Profile last
        for i, (label, _) in enumerate(optional_components):
            if label == "profile":
                selection_priority.append(i)

        # Start with only the header as mandatory system prompt content
        mandatory_messages = [
            {"role": "system", "content": header},
            {"role": "user", "content": user_message_content},
        ]

        # Validate that header + user_message + suffix fits (the bare minimum)
        bare_with_suffix = [
            {"role": "system", "content": header + "\n\n" + suffix},
            {"role": "user", "content": user_message_content},
        ]
        try:
            validate_provider_input(bare_with_suffix)
        except ProviderEnvelopeError:
            # Even the header + suffix alone exceed budget — fail closed
            logger.error("event=provider_input_budget_exceeded stage=generation")
            raise ProviderEnvelopeError("budget_exceeded")

        # Use fit_optional_context with suffix and selection_priority
        fit_result = fit_optional_context(
            mandatory_messages,
            optional_components,
            suffix=suffix,
            selection_priority=selection_priority if len(selection_priority) == len(optional_components) else None,
        )

        # Log pruning event if context components were partially or fully omitted
        if fit_result.pruned:
            logger.info("event=provider_input_pruned stage=generation")

        return fit_result.messages
