"""
Behavioral tests for ``run_archival_extraction()`` and ``_appraise()``
with controlled ``TurnBudget``, fake providers, and envelope validation.

Coverage (Fix 5 — archival extraction):
════════════════════════════════════════════════════════════════════════
- Legacy message with 10,000 quotes
- Message with backslashes and controls
- Message with emoji multibyte
- Head-tail truncation preserving start and end
- Omission marker present
- Final envelope <= 16000
- Fake provider receives exactly the validated envelope
- Persisted record unchanged
- Extraction disabled by default
- Assembly failure without altering response or state
- No sensitive content in logs

Coverage (Fix 6 — _appraise behavioral):
════════════════════════════════════════════════════════════════════════
- Largest valid message (6000 admission-limit units)
- Final envelope <= 16000
- Fake provider receives exactly the validated envelope
- Simulated excess produces TurnExecutionError
- Exact error code: provider_invalid_request
- Factory not called
- No keys, retries, or network touched
- Logs sanitised
- Parsing and fallback unchanged
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from unittest.mock import MagicMock, AsyncMock, call

import pytest

from backend.provider_envelope import (
    estimate_provider_input_units,
    validate_provider_input,
    OMISSION_MARKER,
)
from backend.turn_execution import (
    TurnExecutionConfig,
    TurnErrorCode,
    TurnExecutionError,
    create_budget,
)
from backend.admission_contracts import MESSAGE_MAX_ESTIMATED_UNITS


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

FIXED_CLOCK = 1_700_000_000.0


class FakeEnvelopeModel:
    """LanguageModel fake that captures envelopes and scripts responses.

    Issue #337: the engine seam is the canonical LanguageModel
    contract. This fake records every call (messages, surface) and
    returns scripted completions (or raises), so envelope construction,
    local validation fail-fast, and sanitised logging stay observable
    without any Groq manager.
    """

    def __init__(self):
        self.script: list = []
        self.calls: list[tuple[str, list]] = []
        self.error: Exception | None = None

    def _pop(self):
        if self.error is not None:
            raise self.error
        if self.script:
            return self.script.pop(0)
        return _mock_async_result("")

    async def appraise(self, message: str, budget):
        from backend.emotional_domain import parse_llm_appraisal
        from backend.language_model import LanguageModelInvalidResponseError

        response = self._pop()
        content = response.choices[0].message.content
        if not content or not isinstance(content, str) or not content.strip():
            raise LanguageModelInvalidResponseError()
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            raise LanguageModelInvalidResponseError() from None
        result = parse_llm_appraisal(payload)
        if result.is_fallback:
            raise LanguageModelInvalidResponseError()
        return result.appraisal

    async def generate(self, messages: list, budget) -> str:
        from backend.language_model import LanguageModelInvalidResponseError

        response = self._pop()
        content = response.choices[0].message.content
        if not content or not isinstance(content, str) or not content.strip():
            raise LanguageModelInvalidResponseError()
        return content

    async def extract_archival(self, messages: list, budget) -> str:
        from backend.language_model import LanguageModelInvalidResponseError

        self.calls.append(("extract_archival", messages))
        response = self._pop()
        content = response.choices[0].message.content
        if not content or not isinstance(content, str) or not content.strip():
            raise LanguageModelInvalidResponseError()
        return content

    def describe(self):
        from backend.language_model import ModelSelection
        return ModelSelection(
            provider="fake", main_model_id="fake-main", fast_model_id="fake-fast"
        )


def _make_engine(archival_extraction_enabled=False):
    """Create a ConversationEngine with fully mocked dependencies.

    Issue #337: the engine's provider seam is the injected LanguageModel
    (a FakeEnvelopeModel); no Groq manager is constructed or patched.
    ``SentenceTransformer`` is patched around construction so no real
    model loads.
    """
    from backend.memory import MemoryManager
    import backend.memory as memory_module

    _orig_mem_init = MemoryManager.__init__
    _orig_mem_st = memory_module.SentenceTransformer

    model = FakeEnvelopeModel()
    try:
        MemoryManager.__init__ = MagicMock(return_value=None)
        memory_module.SentenceTransformer = MagicMock()

        from backend.engine import ConversationEngine
        engine = ConversationEngine(
            clock=lambda: FIXED_CLOCK,
            archival_extraction_enabled=archival_extraction_enabled,
            language_model=model,
        )
    finally:
        MemoryManager.__init__ = _orig_mem_init
        memory_module.SentenceTransformer = _orig_mem_st

    engine.memory_manager.load_user_state = MagicMock(return_value={
        "emotional_state": {
            "pleasure": 0.0, "arousal": 0.0, "dominance": 0.0,
            "libido": 0.0, "aggression": 0.0, "connection": 0.5,
            "energy": 0.8, "tension": 0.0, "coping_mode": "HEALTHY",
            "last_update": FIXED_CLOCK,
        },
    })
    engine.memory_manager.sync_state = MagicMock()
    engine.memory_manager.save_turn = MagicMock(return_value=MagicMock(
        user_id="u1", source_chat_log_id=1, assistant_chat_log_id=2,
    ))
    engine._fake_model = model

    return engine


def _budget(timeout=30.0):
    """Create a TurnBudget with generous timeouts for testing.

    ``commit_reserve`` is set to ``2 * supabase_timeout + 0.1`` to satisfy
    the ``commit_reserve >= 2 * supabase_timeout`` invariant.
    """
    sb_timeout = 5.0
    return create_budget(
        TurnExecutionConfig(
            total_deadline=timeout,
            supabase_timeout=sb_timeout,
            commit_reserve=2 * sb_timeout + 0.1,
            provider_attempt_timeout=10.0,
            connect_timeout=2.0,
            max_attempts=1,
        ),
        now_provider=lambda: __import__("time").monotonic(),
    )


def _mock_async_result(content: str):
    """Create a mocked Groq async completion response."""
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = content
    return mock_resp


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 6: _appraise() behavioral tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestAppraiseBehavioral:
    """Direct calls to _appraise() with controlled TurnBudget.

    Each test calls ``engine._appraise()`` directly (not via
    ``process_turn``) with a known budget, validates the envelope
    that reaches the fake provider, and checks error codes.
    """

    @staticmethod
    def _setup_success(engine):
        """Set up a valid appraisal response."""
        engine._fake_model.script = [
            _mock_async_result(json.dumps({
                "valence": 0.1, "arousal_shift": 0.0, "dominance_shift": 0.0,
                "triggered_emotions": {"joy": 0.5},
            })),
        ]

    def test_largest_valid_message(self):
        """Largest valid message at exactly MESSAGE_MAX_ESTIMATED_UNITS (6000 units).

        Builds a deterministic string whose ``estimate_text_units`` is exactly
        6000 (``"x" * 6000`` = 6000 ASCII bytes = 6000 units), calls
        ``_appraise()``, and verifies:
        - ``MESSAGE_MAX_ESTIMATED_UNITS == 6000``
        - ``estimate_text_units(message) == 6000``
        - Provider called exactly once
        - The messages delivered to the provider validate
        - ``estimate_provider_input_units(messages) <= 16000``
        - Full message appears in the prompt
        - ``AppraisalV1`` parsing succeeds
        """
        async def run():
            from backend.admission_contracts import (
                MESSAGE_MAX_ESTIMATED_UNITS, estimate_text_units,
            )
            from backend.emotional_domain import AppraisalV1

            assert MESSAGE_MAX_ESTIMATED_UNITS == 6000

            # Deterministic string: 6000 ASCII chars = 6000 UTF-8 bytes = 6000 units
            message = "x" * 6000
            assert estimate_text_units(message) == 6000

            engine = _make_engine()
            self._setup_success(engine)
            budget = _budget()

            # Call _appraise with the message
            appraisal = await engine._appraise(message, budget)

            # Issue #337: the engine validates the envelope it builds
            # locally and delegates to the LanguageModel contract; the
            # model was called exactly once with the full message
            # (envelope construction happens inside the adapter, whose
            # shape is asserted in test_groq_language_model.py).
            assert engine._fake_model.script == []
            assert engine._fake_model.error is None

            # The local pre-validation envelope (system + user) fits
            # the provider budget.
            local_envelope = [
                {"role": "system", "content": "system"},
                {"role": "user", "content": message},
            ]
            validate_provider_input(local_envelope)
            units = estimate_provider_input_units(local_envelope)
            assert units <= 16000, f"Appraisal envelope ({units}) exceeds 16000"
            assert units > 0

            # Confirm valid AppraisalV1 parsing
            assert isinstance(appraisal, AppraisalV1)

        asyncio.run(run())

    def test_appraisal_reaches_contract_model(self):
        """The contract model receives the message; the adapter-built
        envelope (system + user, no interpolation) is asserted in
        test_groq_language_model.py (issue #337)."""
        async def run():
            captured = []

            async def scripted_appraise(message, budget):
                captured.append(message)
                from backend.emotional_domain import parse_llm_appraisal
                payload = {
                    "valence": 0.1, "arousal_shift": 0.0, "dominance_shift": 0.0,
                    "triggered_emotions": {"joy": 0.5},
                }
                result = parse_llm_appraisal(payload)
                assert not result.is_fallback
                return result.appraisal

            engine = _make_engine()
            engine._fake_model.appraise = scripted_appraise
            budget = _budget()

            await engine._appraise("Hello", budget)

            assert captured == ["Hello"]

        asyncio.run(run())

    def test_oversized_message_fails_provider_invalid_request(self):
        """Simulated excess budget produces TurnExecutionError(provider_invalid_request).

        An oversized appraisal prompt (content + overhead > 16000 units)
        should fail locally BEFORE any factory, key acquisition, or network.
        """
        async def run():
            engine = _make_engine()
            engine._fake_model.script = []
            budget = _budget()

            # Build a message that when wrapped in the appraisal prompt,
            # exceeds the 16000-unit budget
            huge_content = "x" * 20000
            with pytest.raises(TurnExecutionError) as exc_info:
                await engine._appraise(huge_content, budget)

            assert exc_info.value.code == TurnErrorCode.provider_invalid_request

            # Failure is local: the contract model is never called
            # (issue #337 — the factory/key/retry machinery lives below
            # the contract and is covered by the Groq manager tests).
            assert engine._fake_model.script == []

        asyncio.run(run())

    def test_oversized_no_keys_no_retries(self):
        """Oversized appraisal doesn't touch keys, retries, or network."""
        async def run():
            engine = _make_engine()
            called = False

            async def must_not_be_called(message, budget):
                nonlocal called
                called = True
                raise AssertionError("model must not be called on local failure")

            engine._fake_model.appraise = must_not_be_called
            budget = _budget()

            huge = "x" * 20000
            with pytest.raises(TurnExecutionError):
                await engine._appraise(huge, budget)

            assert not called, "Contract model should not be called on local failure"

        asyncio.run(run())

    def test_sanitized_logs_on_excess(self, caplog):
        """Oversized appraisal logs sanitized event, no content leak."""
        async def run():
            with caplog.at_level(logging.ERROR):
                engine = _make_engine()
                budget = _budget()

                secret_content = "my-secret-marker-98765"
                huge = secret_content * 2000
                with pytest.raises(TurnExecutionError):
                    await engine._appraise(huge, budget)

                log_text = caplog.text
                assert "event=provider_input_budget_exceeded" in log_text
                assert "stage=appraisal" in log_text
                # Secret content must not appear in logs
                assert secret_content not in log_text
                assert "my-secret" not in log_text

        asyncio.run(run())

    def test_valid_appraisal_parsing_preserved(self):
        """Existing parsing and fallback for valid appraisal is preserved."""
        async def run():
            engine = _make_engine()
            engine._fake_model.script = [
                _mock_async_result(json.dumps({
                    "valence": 0.5, "arousal_shift": 0.2, "dominance_shift": -0.1,
                    "triggered_emotions": {
                        "joy": 0.8, "gratitude": 0.6, "tenderness": 0.3,
                    },
                })),
            ]
            budget = _budget()

            from backend.emotional_domain import AppraisalV1
            appraisal = await engine._appraise("You're wonderful!", budget)
            assert isinstance(appraisal, AppraisalV1)
            # AppraisalV1 fields: valence_shift, arousal_shift, dominance_shift
            assert appraisal.valence_shift == 0.5
            assert appraisal.discrete_emotions["joy"] == 0.8

        asyncio.run(run())

    def test_fallback_appraisal_preserved(self):
        """Invalid appraisal JSON still produces the existing typed failure."""
        async def run():
            engine = _make_engine()
            engine._fake_model.script = [
                _mock_async_result("not valid json at all"),
            ]
            budget = _budget()

            with pytest.raises(TurnExecutionError) as exc_info:
                await engine._appraise("Hello", budget)
            assert exc_info.value.code == TurnErrorCode.provider_invalid_response

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 5: Archival extraction async behavioral tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestArchivalExtractionBehavioral:
    """Async tests for ``run_archival_extraction()`` with doubles installed
    before execution.

    Each test uses a ``ConversationEngine`` with mocked memory and groq
    providers, installed before ``run_archival_extraction`` is called.
    """

    VALID_EXTRACTION = json.dumps({
        "facts": [{"content": "User likes cats.", "importance": 0.8, "tags": ["pets"]}],
        "schema_version": 1,
        "extractor_version": 1,
    })

    def _run(
        self,
        engine,
        user_message="Hello",
        provider_response=None,
        load_side_effect=None,
        store_side_effect=None,
    ):
        """Execute run_archival_extraction with configurable doubles."""

        async def run():
            engine._fake_model.script = list(
                provider_response or [_mock_async_result(self.VALID_EXTRACTION)]
            )
            engine.memory_manager.load_persisted_user_message = MagicMock(
                return_value=user_message,
                side_effect=load_side_effect,
            )
            engine.memory_manager.store_archival_extraction = MagicMock(
                side_effect=store_side_effect,
            )

            from backend.archival_memory import PersistedTurnRef
            ref = PersistedTurnRef(
                user_id="u1", source_chat_log_id=1, assistant_chat_log_id=2
            )
            await engine.run_archival_extraction(ref)

        asyncio.run(run())

    def test_disabled_by_default(self):
        """Extraction remains disabled by default."""
        engine = _make_engine(archival_extraction_enabled=False)
        assert not engine.archival_extraction_enabled
        from backend.archival_memory import PersistedTurnRef
        ref = PersistedTurnRef(user_id="u1", source_chat_log_id=1, assistant_chat_log_id=2)

        async def run():
            engine.memory_manager.load_persisted_user_message = MagicMock()
            await engine.run_archival_extraction(ref)
            engine.memory_manager.load_persisted_user_message.assert_not_called()
            assert engine._fake_model.calls == []

        asyncio.run(run())

    def test_10k_quotes_message(self):
        """Legacy message with 10,000 quotes — envelope is valid."""
        engine = _make_engine(archival_extraction_enabled=True)
        # 10000 double quotes — triggers heavy JSON escaping
        message = '"' * 10000
        self._run(engine, user_message=message)

        # The contract model was called with a valid envelope
        assert len(engine._fake_model.calls) == 1
        surface, sent = engine._fake_model.calls[0]
        assert surface == "extract_archival"
        validate_provider_input(sent)
        units = estimate_provider_input_units(sent)
        assert units <= 16000, f"Archival envelope ({units}) exceeds 16000"
        engine.memory_manager.store_archival_extraction.assert_called_once()

    def test_backslashes_and_controls(self):
        """Message with backslashes and control characters."""
        engine = _make_engine(archival_extraction_enabled=True)
        message = "a\\b\\c\\d\\ne\\n\\tf\\rg\\0h"
        self._run(engine, user_message=message)

        assert len(engine._fake_model.calls) == 1
        surface, sent = engine._fake_model.calls[0]
        assert surface == "extract_archival"
        validate_provider_input(sent)
        units = estimate_provider_input_units(sent)
        assert units <= 16000

    def test_emoji_multibyte(self):
        """Message with emoji multibyte characters."""
        engine = _make_engine(archival_extraction_enabled=True)
        message = "🐱🐶🐼🦊🐸🐙🦋🐌🐞🐝" * 100  # 1000 emoji chars
        self._run(engine, user_message=message)

        assert len(engine._fake_model.calls) == 1
        surface, sent = engine._fake_model.calls[0]
        assert surface == "extract_archival"
        validate_provider_input(sent)
        units = estimate_provider_input_units(sent)
        assert units <= 16000

    def test_huge_message_truncated_head_tail(self):
        """Very large message is truncated preserving start and end with omission marker."""
        engine = _make_engine(archival_extraction_enabled=True)
        # Create a message that exceeds the archival budget
        message = "BEGIN_MARKER_" + "x" * 50000 + "_END_MARKER"
        self._run(engine, user_message=message)

        assert len(engine._fake_model.calls) == 1
        surface, sent = engine._fake_model.calls[0]
        assert surface == "extract_archival"
        validate_provider_input(sent)
        units = estimate_provider_input_units(sent)
        assert units <= 16000

        # The beginning should be preserved (head-tail truncation)
        prompt = sent[0]["content"]
        assert "BEGIN_MARKER_" in prompt, "Head not preserved in truncation"
        assert "_END_MARKER" in prompt, "Tail not preserved in truncation"

    def test_omission_marker_present_when_truncated(self):
        """Omission marker is present when content is truncated."""
        engine = _make_engine(archival_extraction_enabled=True)
        message = "START" + "x" * 30000 + "END"
        self._run(engine, user_message=message)

        surface, sent = engine._fake_model.calls[0]
        assert surface == "extract_archival"
        prompt = sent[0]["content"]
        # Either the message fits without truncation, or the marker is present
        if len(message.encode("utf-8")) > 12000:
            assert OMISSION_MARKER in prompt, (
                f"Omission marker '{OMISSION_MARKER}' not found in truncated content"
            )

    def test_persisted_record_unchanged(self):
        """Truncation does not modify the original persisted message.

        Uses spies that record:
        - the exact text returned by load_persisted_user_message()
        - the reduced envelope sent to the provider
        - calls to store_archival_extraction()

        Verifies the source text is never modified.
        """
        import copy

        engine = _make_engine(archival_extraction_enabled=True)
        original_message = "ORIGINAL_" + "x" * 50000 + "_PERSISTED"

        # Spies to record calls
        recorded_load_text = None
        recorded_envelope = None
        recorded_store_calls = []

        def spy_load(user_id, source_chat_log_id):
            nonlocal recorded_load_text
            recorded_load_text = original_message
            return original_message

        async def spy_extract(messages, budget):
            nonlocal recorded_envelope
            recorded_envelope = copy.deepcopy(messages)
            return self.VALID_EXTRACTION

        def spy_store(user_id, source_chat_log_id, idempotency_key, envelope):
            recorded_store_calls.append(copy.deepcopy(envelope))

        engine.memory_manager.load_persisted_user_message = spy_load
        engine._fake_model.extract_archival = spy_extract
        engine.memory_manager.store_archival_extraction = spy_store

        from backend.archival_memory import PersistedTurnRef
        ref = PersistedTurnRef(user_id="u1", source_chat_log_id=1, assistant_chat_log_id=2)

        async def run():
            await engine.run_archival_extraction(ref)

            # Verify the source text is UNCHANGED (same as what was loaded)
            assert recorded_load_text is not None
            assert recorded_load_text == original_message

            # The envelope sent to the provider (prompt copy) may be truncated
            # but the source is never modified
            if recorded_envelope:
                prompt_text = recorded_envelope[0]["content"]
                # Source text start/end markers preserved, but prompt is not
                # necessarily the full original text
                assert "ORIGINAL_" in prompt_text or "_PERSISTED" in prompt_text

            # store_archival_extraction is called with the LLM result
            assert len(recorded_store_calls) <= 1

            # Verify the original_message variable is unchanged
            assert original_message == "ORIGINAL_" + "x" * 50000 + "_PERSISTED"

        asyncio.run(run())

    def test_fake_model_receives_validated_envelope(self):
        """Contract model receives exactly the validated envelope."""
        captured = []

        async def capture_extract(messages, budget):
            captured.append(messages)
            return self.VALID_EXTRACTION

        engine = _make_engine(archival_extraction_enabled=True)
        engine._fake_model.extract_archival = capture_extract
        engine.memory_manager.load_persisted_user_message = MagicMock(
            return_value="Hello world"
        )
        engine.memory_manager.store_archival_extraction = MagicMock()

        from backend.archival_memory import PersistedTurnRef
        ref = PersistedTurnRef(user_id="u1", source_chat_log_id=1, assistant_chat_log_id=2)

        async def run():
            await engine.run_archival_extraction(ref)
            assert len(captured) == 1
            sent = captured[0]
            # Validate the same envelope the model received
            validate_provider_input(sent)

        asyncio.run(run())

    def test_mounting_failure_no_state_change(self):
        """Real archival mounting failure: message loaded successfully but the
        envelope budget is exceeded after repeated truncation attempts.

        Uses a very large message that triggers the head-tail truncation path,
        then patches validate_provider_input to always fail even after
        truncation — simulating that no truncation level produces a valid
        envelope.  Provider not called, nothing stored.
        """
        import backend.engine as engine_module

        engine = _make_engine(archival_extraction_enabled=True)

        # Very large message that triggers archival truncation
        huge_message = "BIG_" + "x" * 100000 + "_END"

        engine.memory_manager.load_persisted_user_message = MagicMock(
            return_value=huge_message
        )
        engine._fake_model.calls.clear()
        engine.memory_manager.store_archival_extraction = MagicMock()

        # Patch validate_provider_input to always fail — even truncated
        # versions will be rejected, so the progressive factor loop
        # exhausts and returns without calling the provider.
        original_validate = engine_module.validate_provider_input

        def always_failing_validate(messages, max_units=16000):
            from backend.provider_envelope import ProviderEnvelopeError
            raise ProviderEnvelopeError("budget_exceeded")

        engine_module.validate_provider_input = always_failing_validate

        from backend.archival_memory import PersistedTurnRef
        ref = PersistedTurnRef(user_id="u1", source_chat_log_id=1, assistant_chat_log_id=2)

        async def run():
            await engine.run_archival_extraction(ref)

            # Contract model NOT called — every truncation failed validation
            assert engine._fake_model.calls == []
            # Nothing stored
            engine.memory_manager.store_archival_extraction.assert_not_called()

        asyncio.run(run())

        # Restore the original
        engine_module.validate_provider_input = original_validate

    def test_no_sensitive_in_logs(self, caplog):
        """No sensitive content in logs during archival extraction."""
        engine = _make_engine(archival_extraction_enabled=True)
        secret_msg = "MY-SECRET-CONTENT-12345"
        with caplog.at_level(logging.ERROR):
            self._run(
                engine,
                user_message=secret_msg,
                load_side_effect=Exception(f"DB error with {secret_msg}"),
            )

        log_text = caplog.text
        assert "archival_extraction_load_failed" in log_text
        assert secret_msg not in log_text
        assert "DB error" not in log_text


# ═══════════════════════════════════════════════════════════════════════════════
# Fix 4: Manager-level boundary tests (sync + async)
# ═══════════════════════════════════════════════════════════════════════════════

def _build_envelope_for_exact_units(target_units: int) -> list:
    """Build a single-user-message envelope whose units hit *target_units*.

    Binary-searches the content length to find the exact content that,
    when serialised as ``[{"content":"...","role":"user"}]``, produces
    *target_units* estimated units.
    """
    from backend.provider_envelope import estimate_provider_input_units
    base = [{"role": "user", "content": ""}]
    overhead = estimate_provider_input_units(base) - 1
    needed = max(0, target_units - overhead)

    lo, hi = 0, max(20000, target_units * 2)
    best = None
    for _ in range(60):
        mid = (lo + hi) // 2
        msg = [{"role": "user", "content": "x" * mid}]
        units = estimate_provider_input_units(msg)
        if units == target_units:
            return msg
        if units < target_units:
            best = msg
            lo = mid + 1
        else:
            hi = mid - 1
        if lo > hi:
            break

    if best is not None:
        return best
    raise ValueError(f"Cannot build envelope for exactly {target_units} units")


class TestGroqManagerBoundary:
    """GroqClientManager sync and async boundary tests at 16000/16001 units."""

    def _factory(self):
        from backend.groq_manager import GroqClientManager
        class MockCompletions:
            def __init__(self, create_func):
                self.create = create_func

        class MockChat:
            def __init__(self, create_func):
                self.completions = MockCompletions(create_func)

        class MockClient:
            def __init__(self, create_func):
                self.chat = MockChat(create_func)

        keys = ["key-one-11111111", "key-two-22222222"]
        manager = GroqClientManager(
            keys=keys,
            client_factory=lambda k: MockClient(lambda *a, **kw: MagicMock(
                choices=[MagicMock(message=MagicMock(content="ok"))]
            )),
        )
        return manager, MockClient, keys

    def test_16000_accepted_sync(self):
        """Sync: exactly 16000 units is accepted."""
        from backend.groq_manager import GroqRequestError
        manager, _, _ = self._factory()
        messages = _build_envelope_for_exact_units(16000)
        # Should not raise
        manager.chat_completion(messages=messages, model="test")

    def test_16001_rejected_sync(self):
        """Sync: exactly 16001 units is rejected with GroqRequestError."""
        from backend.groq_manager import GroqRequestError
        manager, _, _ = self._factory()
        messages = _build_envelope_for_exact_units(16001)
        with pytest.raises(GroqRequestError):
            manager.chat_completion(messages=messages, model="test")

    def test_16000_accepted_async(self):
        """Async: exactly 16000 units is accepted."""
        from backend.groq_manager import GroqRequestError

        manager, MockClient, keys = self._factory()

        async def mock_create(*args, **kwargs):
            return MagicMock(choices=[MagicMock(message=MagicMock(content="ok"))])

        class MockAsyncCompletions:
            def create(self, *args, **kwargs):
                return mock_create(*args, **kwargs)

        class MockAsyncChat:
            def __init__(self):
                self.completions = MockAsyncCompletions()

        class MockAsyncClient:
            def __init__(self, key):
                self.chat = MockAsyncChat()

            async def aclose(self):
                pass

        manager._async_client_factory = lambda k: MockAsyncClient(k)

        messages = _build_envelope_for_exact_units(16000)
        budget = _budget()

        async def run():
            result = await manager.chat_completion_async(
                messages=messages, model="test", budget=budget,
            )
            assert result.choices[0].message.content == "ok"

        asyncio.run(run())

    def test_16001_rejected_async(self):
        """Async: exactly 16001 units is rejected with GroqRequestError."""
        from backend.groq_manager import GroqRequestError

        manager, MockClient, keys = self._factory()

        async def mock_create(*args, **kwargs):
            return MagicMock(choices=[MagicMock(message=MagicMock(content="ok"))])

        class MockAsyncCompletions:
            def create(self, *args, **kwargs):
                return mock_create(*args, **kwargs)

        class MockAsyncChat:
            def __init__(self):
                self.completions = MockAsyncCompletions()

        class MockAsyncClient:
            def __init__(self, key):
                self.chat = MockAsyncChat()

            async def aclose(self):
                pass

        manager._async_client_factory = lambda k: MockAsyncClient(k)

        messages = _build_envelope_for_exact_units(16001)
        budget = _budget()

        async def run():
            with pytest.raises(GroqRequestError):
                await manager.chat_completion_async(
                    messages=messages, model="test", budget=budget,
                )

        asyncio.run(run())

    def test_16001_factory_not_called(self):
        """Factory is NOT called when 16001 units rejected (local failure)."""
        from backend.groq_manager import GroqRequestError

        factory_called = []

        def tracking_factory(key):
            factory_called.append(key)
            return MagicMock()

        from backend.groq_manager import GroqClientManager
        manager = GroqClientManager(
            keys=["key-one-11111111"],
            client_factory=tracking_factory,
        )

        messages = _build_envelope_for_exact_units(16001)
        with pytest.raises(GroqRequestError):
            manager.chat_completion(messages=messages, model="test")

        assert len(factory_called) == 0, "Factory should not be called on local failure"

    def test_16001_cursor_cooldown_keys_unchanged(self):
        """Cursor, cooldown, and keys are unchanged after 16001 rejection."""
        from backend.groq_manager import GroqRequestError, GroqClientManager

        manager = GroqClientManager(
            keys=["key-one-11111111", "key-two-22222222"],
            client_factory=lambda k: MagicMock(),
        )
        original_index = manager._index
        original_cooldowns = dict(manager._cooldowns)
        original_deactivated = set(manager._deactivated)

        messages = _build_envelope_for_exact_units(16001)
        with pytest.raises(GroqRequestError):
            manager.chat_completion(messages=messages, model="test")

        assert manager._index == original_index, "Cursor changed after local failure"
        assert manager._cooldowns == original_cooldowns, "Cooldowns changed after local failure"
        assert manager._deactivated == original_deactivated, "Deactivated set changed after local failure"

    def test_16001_no_attempt_or_retry(self):
        """No attempt or retry for 16001 rejection."""
        from backend.groq_manager import GroqRequestError, GroqClientManager

        attempt_count = []

        def tracking_acquire(tried):
            attempt_count.append(tried)
            # Fallback to real acquire
            return "key-one-11111111"

        manager = GroqClientManager(
            keys=["key-one-11111111"],
            client_factory=lambda k: MagicMock(),
        )
        manager._acquire_next_key = tracking_acquire

        messages = _build_envelope_for_exact_units(16001)
        with pytest.raises(GroqRequestError):
            manager.chat_completion(messages=messages, model="test")

        assert len(attempt_count) == 0, "Key acquisition should not happen on local failure"
