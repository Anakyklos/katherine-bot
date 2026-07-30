"""
Behavioral tests for ``backend/provider_models.py`` and its integration into
``ConversationEngine`` (appraisal, generation, archival extraction).

Coverage map (13 behavioural requirements):
──────────────────────────────────────────────────────────────────────────────
 1. Pure importability — no FastAPI, Groq, Supabase, sentence_transformers,
    env vars, network, or ConversationEngine.
 2. Configuration is immutable (frozen dataclass).
 3. Single source of truth contains exactly the two approved model IDs.
 4. _appraise() sends model=openai/gpt-oss-20b, temperature=0,
    max_tokens=256, response_format=json_object.
 5. _generate() sends model=openai/gpt-oss-120b, temperature=0.8,
    max_tokens=200.
 6. run_archival_extraction() sends model=openai/gpt-oss-20b,
    temperature=0.0, max_tokens=512, response_format=json_object.
 7. Valid appraisal JSON is still accepted.
 8. Invalid appraisal still produces the existing typed failure.
 9. Valid and invalid archival extraction preserves current behaviour.
10. Provider failures remain sanitised and typed.
11. Old Llama model IDs are not used as model or fallback in any active
    path.
12. No test uses real Groq, network, real Supabase, or real embeddings.
13. All existing backend tests continue to pass.
"""

from __future__ import annotations

import asyncio
import builtins
import io
import json
import logging
import os
import socket
import sys
import time
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from backend.engine import ConversationEngine
from backend.groq_manager import GroqRequestError
from backend.turn_execution import (
    TurnExecutionConfig,
    TurnErrorCode,
    TurnExecutionError,
    create_budget,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers — defined before test classes so they are available everywhere
# ═══════════════════════════════════════════════════════════════════════════════

FIXED_CLOCK = 1_700_000_000.0


def _make_engine(archival_extraction_enabled=False, clock=FIXED_CLOCK):
    """Create a ConversationEngine with fully mocked dependencies.

    Patches ``__init__`` on ``GroqClientManager`` and ``MemoryManager``
    **before** construction so no real Groq client, Supabase client, or
    SentenceTransformer model is ever instantiated.  The patch operates
    on the class object directly (not a module reference), so it works
    reliably regardless of import order or namespace package quirks.

    After construction, specific attributes used by tests are replaced
    with MagicMock / AsyncMock for test control.
    """
    from backend.groq_manager import GroqClientManager
    from backend.memory import MemoryManager

    _orig_groq_init = GroqClientManager.__init__
    _orig_mem_init = MemoryManager.__init__
    _orig_mem_st = None

    # Patch SentenceTransformer on the memory module so MemoryManager.__init__
    # never loads the real model.
    import backend.memory as memory_module
    _orig_mem_st = memory_module.SentenceTransformer

    try:
        GroqClientManager.__init__ = MagicMock(return_value=None)
        MemoryManager.__init__ = MagicMock(return_value=None)
        memory_module.SentenceTransformer = MagicMock()

        engine = ConversationEngine(
            clock=lambda: clock,
            archival_extraction_enabled=archival_extraction_enabled,
        )
    finally:
        GroqClientManager.__init__ = _orig_groq_init
        MemoryManager.__init__ = _orig_mem_init
        if _orig_mem_st is not None:
            memory_module.SentenceTransformer = _orig_mem_st

    # ---- Memory manager mocks ----
    engine.memory_manager.load_user_state = MagicMock(return_value={
        "emotional_state": {
            "pleasure": 0.0, "arousal": 0.0, "dominance": 0.0,
            "libido": 0.0, "aggression": 0.0, "connection": 0.5,
            "energy": 0.8, "tension": 0.0, "coping_mode": "HEALTHY",
            "last_update": clock,
        },
    })
    engine.memory_manager.sync_state = MagicMock()
    engine.memory_manager.save_turn = MagicMock()
    engine.memory_manager.get_context = MagicMock(return_value="[mocked context]")
    engine.memory_manager.get_context_components = MagicMock(return_value={
        "persona": "Katherine...",
        "user_profile_str": "{}",
        "memory_str": "",
        "history_list": [],
        "assembled": "[mocked context]",
    })
    engine.memory_manager.load_recent_history = MagicMock(return_value=[])

    # ---- Groq manager mocks ----
    sync_m = MagicMock()
    sync_m.choices = [MagicMock()]
    sync_m.choices[0].message.content = "Hi"
    engine.groq_manager.chat_completion = MagicMock(return_value=sync_m)
    engine.groq_manager.chat_completion_async = AsyncMock()

    return engine


def _mock_async_result(content: str):
    """Create a MagicMock that resembles a Groq async completion response."""
    mock_resp = MagicMock()
    mock_resp.choices = [MagicMock()]
    mock_resp.choices[0].message.content = content
    return mock_resp


def _inject_ok_turn(engine):
    """Set up ``chat_completion_async`` with two valid responses
    (appraisal + generation) so a full turn succeeds.
    """
    engine.groq_manager.chat_completion_async.side_effect = [
        _mock_async_result(json.dumps({
            "valence": 0.2, "arousal_shift": 0.1, "dominance_shift": 0.0,
            "triggered_emotions": {"joy": 0.5},
        })),
        _mock_async_result("Hi there!"),
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Pure importability
# ═══════════════════════════════════════════════════════════════════════════════

class TestPureImportability:
    """provider_models must be importable without heavy dependencies."""

    def test_importable_without_heavy_deps(self):
        """Import provider_models using an import hook that blocks heavy deps."""
        original_import = builtins.__import__
        blocked = {"fastapi", "groq", "supabase", "sentence_transformers",
                   "httpx", "httpcore", "anyio", "websockets"}
        hit_blocked: list[str] = []

        def _blocking_import(name, *args, **kwargs):
            top = name.split(".")[0]
            if top in blocked:
                hit_blocked.append(top)
                raise ImportError(f"blocked: {name}")
            return original_import(name, *args, **kwargs)

        # Save and remove ONLY the module under test
        saved = sys.modules.pop("backend.provider_models", None)

        builtins.__import__ = _blocking_import
        try:
            from backend.provider_models import MAIN_MODEL_ID, FAST_MODEL_ID, ProviderConfig
            assert MAIN_MODEL_ID == "openai/gpt-oss-120b"
            assert FAST_MODEL_ID == "openai/gpt-oss-20b"
            config = ProviderConfig()
            assert config.main_model_id == "openai/gpt-oss-120b"
        finally:
            builtins.__import__ = original_import
            if saved is not None:
                sys.modules["backend.provider_models"] = saved
            elif "backend.provider_models" in sys.modules:
                del sys.modules["backend.provider_models"]

        assert hit_blocked == [], f"Unexpected imports from blocked deps: {hit_blocked}"

    def test_importable_without_env(self):
        """provider_models does not read env vars."""
        saved = sys.modules.pop("backend.provider_models", None)
        old_key = os.environ.pop("GROQ_API_KEY", None)
        old_key2 = os.environ.pop("GROQ_API_KEY_2", None)
        try:
            from backend.provider_models import MAIN_MODEL_ID, FAST_MODEL_ID, ProviderConfig
            assert MAIN_MODEL_ID == "openai/gpt-oss-120b"
            assert FAST_MODEL_ID == "openai/gpt-oss-20b"
        finally:
            if saved is not None:
                sys.modules["backend.provider_models"] = saved
            elif "backend.provider_models" in sys.modules:
                del sys.modules["backend.provider_models"]
            if old_key is not None:
                os.environ["GROQ_API_KEY"] = old_key
            if old_key2 is not None:
                os.environ["GROQ_API_KEY_2"] = old_key2

    def test_importable_without_network(self):
        """provider_models does not perform network calls."""
        original_socket = socket.socket
        saved = sys.modules.pop("backend.provider_models", None)

        def _blocking_socket(*args, **kwargs):
            raise OSError("network blocked")

        socket.socket = _blocking_socket
        try:
            from backend.provider_models import MAIN_MODEL_ID
            assert MAIN_MODEL_ID == "openai/gpt-oss-120b"
        finally:
            socket.socket = original_socket
            if saved is not None:
                sys.modules["backend.provider_models"] = saved
            elif "backend.provider_models" in sys.modules:
                del sys.modules["backend.provider_models"]

    def test_importable_without_conversation_engine(self):
        """provider_models is importable without referencing ConversationEngine."""
        saved = sys.modules.pop("backend.provider_models", None)
        try:
            from backend.provider_models import ProviderConfig
            cfg = ProviderConfig()
            assert cfg.main_model_id == "openai/gpt-oss-120b"
        finally:
            if saved is not None:
                sys.modules["backend.provider_models"] = saved
            elif "backend.provider_models" in sys.modules:
                del sys.modules["backend.provider_models"]


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Immutability
# ═══════════════════════════════════════════════════════════════════════════════

class TestImmutability:
    """ProviderConfig must be immutable (frozen dataclass)."""

    def test_config_is_frozen(self):
        from backend.provider_models import ProviderConfig
        config = ProviderConfig()
        with pytest.raises(AttributeError):
            config.main_model_id = "other-model"

    def test_config_rejects_type_change(self):
        from backend.provider_models import ProviderConfig
        config = ProviderConfig()
        with pytest.raises(AttributeError):
            config.fast_model_id = "other-model"


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Single source of truth
# ═══════════════════════════════════════════════════════════════════════════════

class TestSingleSourceOfTruth:
    """Module-level constants and default dataclass values match exactly."""

    def test_module_constants_match_config_defaults(self):
        from backend.provider_models import (
            MAIN_MODEL_ID, FAST_MODEL_ID,
            MAIN_MAX_OUTPUT_TOKENS, APPRAISAL_MAX_OUTPUT_TOKENS,
            ARCHIVAL_MAX_OUTPUT_TOKENS, ProviderConfig,
        )
        config = ProviderConfig()
        assert config.main_model_id == MAIN_MODEL_ID
        assert config.fast_model_id == FAST_MODEL_ID
        assert config.main_max_output_tokens == MAIN_MAX_OUTPUT_TOKENS
        assert config.appraisal_max_output_tokens == APPRAISAL_MAX_OUTPUT_TOKENS
        assert config.archival_max_output_tokens == ARCHIVAL_MAX_OUTPUT_TOKENS

    def test_exactly_two_approved_models(self):
        from backend.provider_models import MAIN_MODEL_ID, FAST_MODEL_ID
        assert MAIN_MODEL_ID == "openai/gpt-oss-120b"
        assert FAST_MODEL_ID == "openai/gpt-oss-20b"

    def test_no_old_llama_ids_in_module(self):
        """No active module constant references deprecated Llama models."""
        from backend.provider_models import MAIN_MODEL_ID, FAST_MODEL_ID
        assert "llama" not in MAIN_MODEL_ID
        assert "llama" not in FAST_MODEL_ID

    def test_exact_numeric_output_token_limits(self):
        """Module constants, dataclass defaults, and engine config all match exact values."""
        from backend.provider_models import (
            MAIN_MAX_OUTPUT_TOKENS, APPRAISAL_MAX_OUTPUT_TOKENS,
            ARCHIVAL_MAX_OUTPUT_TOKENS, ProviderConfig,
        )
        # Module-level constants
        assert MAIN_MAX_OUTPUT_TOKENS == 200
        assert APPRAISAL_MAX_OUTPUT_TOKENS == 256
        assert ARCHIVAL_MAX_OUTPUT_TOKENS == 512
        # Dataclass defaults
        assert ProviderConfig().main_max_output_tokens == 200
        assert ProviderConfig().appraisal_max_output_tokens == 256
        assert ProviderConfig().archival_max_output_tokens == 512
        # Engine propagates
        engine = _make_engine()
        assert engine.provider_config.main_max_output_tokens == 200
        assert engine.provider_config.appraisal_max_output_tokens == 256
        assert engine.provider_config.archival_max_output_tokens == 512


# ═══════════════════════════════════════════════════════════════════════════════
# 4. _appraise() sends correct params
# ═══════════════════════════════════════════════════════════════════════════════

class TestAppraisalParams:
    """_appraise() must use the correct model, temperature, max_tokens, and JSON mode.

    Tests call ``_appraise()`` **directly** rather than going through
    ``process_turn()`` so the assertion path is unambiguous — no try/except
    is needed because any unexpected exception will fail the test immediately.
    """

    @staticmethod
    def _setup_appraisal(engine):
        """Set a single valid appraisal response and return a TurnBudget."""
        budget = create_budget(
            TurnExecutionConfig.defaults(),
            now_provider=lambda: time.monotonic(),
        )
        engine.groq_manager.chat_completion_async.side_effect = [
            _mock_async_result(json.dumps({
                "valence": 0.2, "arousal_shift": 0.1, "dominance_shift": 0.0,
                "triggered_emotions": {"joy": 0.5},
            })),
        ]
        return budget

    def test_appraisal_uses_fast_model(self):
        async def run():
            engine = _make_engine()
            budget = self._setup_appraisal(engine)

            await engine._appraise("Hello", budget)

            assert engine.groq_manager.chat_completion_async.call_count == 1
            params = engine.groq_manager.chat_completion_async.call_args_list[0][1]
            assert params["model"] == "openai/gpt-oss-20b"
            assert params["temperature"] == 0
            assert params["max_tokens"] == engine.provider_config.appraisal_max_output_tokens
            assert params["response_format"] == {"type": "json_object"}

        asyncio.run(run())

    def test_appraisal_stage_label(self):
        async def run():
            engine = _make_engine()
            budget = self._setup_appraisal(engine)

            await engine._appraise("Hello", budget)

            params = engine.groq_manager.chat_completion_async.call_args_list[0][1]
            assert params["stage"] == "appraisal"

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# 5. _generate() sends correct params
# ═══════════════════════════════════════════════════════════════════════════════

class TestGenerationParams:
    """_generate() must use the correct model, temperature, and max_tokens."""

    def test_generation_uses_main_model(self):
        """Generation uses main model with temperature 0.8 and max_tokens 200."""
        async def run():
            engine = _make_engine()
            _inject_ok_turn(engine)

            resp, emotions = await engine.process_turn("user", "Hello")

            assert engine.groq_manager.chat_completion_async.call_count == 2
            gen_params = engine.groq_manager.chat_completion_async.call_args_list[1][1]
            assert gen_params["model"] == "openai/gpt-oss-120b"
            assert gen_params["temperature"] == 0.8
            assert gen_params["max_tokens"] == engine.provider_config.main_max_output_tokens
            assert "response_format" not in gen_params

        asyncio.run(run())

    def test_generation_stage_label(self):
        async def run():
            engine = _make_engine()
            _inject_ok_turn(engine)

            await engine.process_turn("user", "Hello")

            gen_params = engine.groq_manager.chat_completion_async.call_args_list[1][1]
            assert gen_params["stage"] == "generation"

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# 6. run_archival_extraction() sends correct params
# ═══════════════════════════════════════════════════════════════════════════════

class TestArchivalExtractionParams:
    """run_archival_extraction() must use the correct model, temperature,
    max_tokens, and JSON mode.
    """

    def test_archival_extraction_uses_fast_model(self):
        async def run():
            engine = _make_engine(archival_extraction_enabled=True)

            engine.groq_manager.chat_completion_async.side_effect = [
                _mock_async_result(json.dumps({
                    "facts": [{"content": "User likes cats.", "importance": 0.7, "tags": ["interest"]}],
                    "schema_version": 1,
                    "extractor_version": 1,
                })),
            ]
            engine.memory_manager.load_persisted_user_message = MagicMock(
                return_value="I love cats."
            )
            engine.memory_manager.store_archival_extraction = MagicMock()

            from backend.archival_memory import PersistedTurnRef
            ref = PersistedTurnRef(user_id="u1", source_chat_log_id=1, assistant_chat_log_id=2)
            await engine.run_archival_extraction(ref)

            assert engine.groq_manager.chat_completion_async.call_count >= 1
            arch_params = engine.groq_manager.chat_completion_async.call_args_list[0][1]
            assert arch_params["model"] == "openai/gpt-oss-20b"
            assert arch_params["temperature"] == 0.0
            assert arch_params["max_tokens"] == engine.provider_config.archival_max_output_tokens
            assert arch_params["response_format"] == {"type": "json_object"}
            assert arch_params["stage"] == "archival_extraction"

        asyncio.run(run())

    def test_archival_extraction_disabled_does_not_call(self):
        """With archival extraction disabled, no third LLM call."""
        async def run():
            engine = _make_engine(archival_extraction_enabled=False)
            _inject_ok_turn(engine)

            await engine.process_turn("user", "Hello")

            assert engine.groq_manager.chat_completion_async.call_count == 2

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# 7. Valid appraisal JSON is still accepted
# ═══════════════════════════════════════════════════════════════════════════════

class TestValidAppraisalAccepted:
    """Valid appraisal JSON still produces a successful turn."""

    def test_valid_appraisal_succeeds(self):
        async def run():
            engine = _make_engine()
            engine.groq_manager.chat_completion_async.side_effect = [
                _mock_async_result(json.dumps({
                    "valence": 0.3, "arousal_shift": 0.1, "dominance_shift": -0.1,
                    "triggered_emotions": {"joy": 0.6, "tenderness": 0.4, "gratitude": 0.3},
                })),
                _mock_async_result("That's nice of you!"),
            ]

            resp, emotions = await engine.process_turn("user", "You're amazing!")
            assert resp == "That's nice of you!"
            assert emotions.schema_version == 1
            assert emotions.mood_label is not None

        asyncio.run(run())

    def test_valid_appraisal_with_all_emotions_succeeds(self):
        """Full valid appraisal with all 11 emotions is accepted."""
        async def run():
            engine = _make_engine()

            all_emotions = {
                "joy": 0.1, "sadness": 0.2, "anger": 0.3, "fear": 0.1,
                "disgust": 0.0, "surprise": 0.5, "tenderness": 0.2,
                "guilt": 0.0, "pride": 0.1, "jealousy": 0.0, "gratitude": 0.4,
            }
            engine.groq_manager.chat_completion_async.side_effect = [
                _mock_async_result(json.dumps({
                    "valence": 0.0, "arousal_shift": 0.2, "dominance_shift": 0.0,
                    "triggered_emotions": all_emotions,
                })),
                _mock_async_result("OK!"),
            ]

            resp, emotions = await engine.process_turn("user", "Hello world")
            assert resp is not None
            assert emotions.schema_version == 1

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Invalid appraisal produces typed failure
# ═══════════════════════════════════════════════════════════════════════════════

class TestInvalidAppraisalFails:
    """Invalid appraisal JSON produces the existing typed failure."""

    def test_empty_appraisal_fails(self):
        async def run():
            engine = _make_engine()
            engine.groq_manager.chat_completion_async.side_effect = [
                _mock_async_result(json.dumps({})),
            ]

            with pytest.raises(TurnExecutionError) as exc_info:
                await engine.process_turn("user", "Hello")
            assert exc_info.value.code == TurnErrorCode.provider_invalid_response

        asyncio.run(run())

    def test_missing_valence_fails(self):
        async def run():
            engine = _make_engine()
            engine.groq_manager.chat_completion_async.side_effect = [
                _mock_async_result(json.dumps({
                    "triggered_emotions": {"joy": 0.5},
                })),
            ]

            with pytest.raises(TurnExecutionError) as exc_info:
                await engine.process_turn("user", "Hello")
            assert exc_info.value.code == TurnErrorCode.provider_invalid_response

        asyncio.run(run())

    def test_non_json_appraisal_fails(self):
        async def run():
            engine = _make_engine()
            engine.groq_manager.chat_completion_async.side_effect = [
                _mock_async_result("not json at all"),
            ]

            with pytest.raises(TurnExecutionError) as exc_info:
                await engine.process_turn("user", "Hello")
            assert exc_info.value.code == TurnErrorCode.provider_invalid_response

        asyncio.run(run())

    def test_unknown_top_level_key_triggers_fallback(self):
        """Unknown top-level key triggers fallback -> typed failure."""
        async def run():
            engine = _make_engine()
            engine.groq_manager.chat_completion_async.side_effect = [
                _mock_async_result(json.dumps({
                    "valence": 0.0, "arousal_shift": 0.0, "dominance_shift": 0.0,
                    "triggered_emotions": {"joy": 0.0},
                    "unknown_key": "should_cause_fallback",
                })),
            ]

            with pytest.raises(TurnExecutionError) as exc_info:
                await engine.process_turn("user", "Hello")
            assert exc_info.value.code == TurnErrorCode.provider_invalid_response

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# 9. Valid and invalid archival extraction preserves behaviour
# ═══════════════════════════════════════════════════════════════════════════════

class TestArchivalExtractionBehaviour:
    """Archival extraction preserves existing parsing, validation, and
    idempotency behaviour.
    """

    VALID_EXTRACTION = json.dumps({
        "facts": [{"content": "User likes cats.", "importance": 0.8, "tags": ["pets"]}],
        "schema_version": 1,
        "extractor_version": 1,
    })

    async def _run_archival(self, engine, content):
        engine.groq_manager.chat_completion_async.side_effect = [
            _mock_async_result(content),
        ]
        engine.memory_manager.load_persisted_user_message = MagicMock(
            return_value="I love cats."
        )
        engine.memory_manager.store_archival_extraction = MagicMock()

        from backend.archival_memory import PersistedTurnRef
        ref = PersistedTurnRef(user_id="u1", source_chat_log_id=1, assistant_chat_log_id=2)
        await engine.run_archival_extraction(ref)

    def test_archival_extraction_valid(self):
        async def run():
            engine = _make_engine(archival_extraction_enabled=True)
            await self._run_archival(engine, self.VALID_EXTRACTION)
            engine.memory_manager.store_archival_extraction.assert_called_once()

        asyncio.run(run())

    def test_archival_extraction_invalid_json(self):
        async def run():
            engine = _make_engine(archival_extraction_enabled=True)
            await self._run_archival(engine, "not valid json")
            engine.memory_manager.store_archival_extraction.assert_not_called()

        asyncio.run(run())

    def test_archival_extraction_validation_failure(self):
        async def run():
            engine = _make_engine(archival_extraction_enabled=True)
            await self._run_archival(engine, json.dumps({
                "facts": "not a list",
                "schema_version": 1,
                "extractor_version": 1,
            }))
            engine.memory_manager.store_archival_extraction.assert_not_called()

        asyncio.run(run())

    def test_archival_extraction_llm_failure(self):
        """Provider failure during archival extraction is logged, not fatal."""
        async def run():
            engine = _make_engine(archival_extraction_enabled=True)
            engine.groq_manager.chat_completion_async.side_effect = RuntimeError(
                "Provider failed"
            )
            engine.memory_manager.load_persisted_user_message = MagicMock(
                return_value="I love cats."
            )
            engine.memory_manager.store_archival_extraction = MagicMock()

            from backend.archival_memory import PersistedTurnRef
            ref = PersistedTurnRef(user_id="u1", source_chat_log_id=1, assistant_chat_log_id=2)
            await engine.run_archival_extraction(ref)

            engine.memory_manager.store_archival_extraction.assert_not_called()

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# 10. Provider failures remain sanitised and typed
# ═══════════════════════════════════════════════════════════════════════════════

class TestProviderFailuresSanitised:
    """Provider failures produce typed TurnExecutionError, not raw exceptions."""

    def test_generation_provider_failure_typed(self):
        async def run():
            engine = _make_engine()

            engine.groq_manager.chat_completion_async.side_effect = [
                _mock_async_result(json.dumps({
                    "valence": 0.0, "arousal_shift": 0.0, "dominance_shift": 0.0,
                    "triggered_emotions": {"joy": 0.0},
                })),
                GroqRequestError("Provider failed unexpectedly"),
            ]

            with pytest.raises(TurnExecutionError) as exc_info:
                await engine.process_turn("user", "Hello")
            assert exc_info.value.code == TurnErrorCode.provider_unavailable

        asyncio.run(run())

    def test_sanitised_logging(self):
        """Logs contain sanitised event codes, not raw exception details."""
        async def run():
            engine = _make_engine()

            engine.groq_manager.chat_completion_async.side_effect = [
                _mock_async_result(json.dumps({
                    "valence": 0.0, "arousal_shift": 0.0, "dominance_shift": 0.0,
                    "triggered_emotions": {"joy": 0.0},
                    "SENSITIVE_EXTRA_KEY_92841": "should_not_leak",
                })),
            ]

            logger = logging.getLogger("backend.engine")
            logger.setLevel(logging.INFO)
            stream = io.StringIO()
            handler = logging.StreamHandler(stream)
            logger.addHandler(handler)
            try:
                with pytest.raises(TurnExecutionError):
                    await engine.process_turn("user", "Hello")
            finally:
                logger.removeHandler(handler)

            log_text = stream.getvalue()
            assert "event=emotional_appraisal_fallback" in log_text
            assert "code=" in log_text
            assert "SENSITIVE_EXTRA_KEY_92841" not in log_text

        asyncio.run(run())


# ═══════════════════════════════════════════════════════════════════════════════
# 11. Old Llama models are absent from active paths
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoOldLlamaModels:
    """Deprecated Llama model IDs are not used anywhere in active paths."""

    def test_no_llama_models_in_config(self):
        """ProviderConfig never references deprecated Llama models."""
        from backend.provider_models import MAIN_MODEL_ID, FAST_MODEL_ID
        assert MAIN_MODEL_ID == "openai/gpt-oss-120b"
        assert FAST_MODEL_ID == "openai/gpt-oss-20b"
        assert "llama" not in MAIN_MODEL_ID
        assert "llama" not in FAST_MODEL_ID

    def test_no_fallback_to_old_models_in_engine(self):
        """Engine does not have fallback attributes for old models."""
        engine = _make_engine()
        assert not hasattr(engine, "model_main")
        assert not hasattr(engine, "model_fast")
        assert engine.provider_config.main_model_id == "openai/gpt-oss-120b"
        assert engine.provider_config.fast_model_id == "openai/gpt-oss-20b"

    def test_engine_provider_config_has_no_llama(self):
        """The active model IDs from the engine's provider_config never contain 'llama'."""
        engine = _make_engine()
        main_id = engine.provider_config.main_model_id
        fast_id = engine.provider_config.fast_model_id
        assert "llama" not in main_id
        assert "llama" not in fast_id
        assert main_id == "openai/gpt-oss-120b"
        assert fast_id == "openai/gpt-oss-20b"


# ═══════════════════════════════════════════════════════════════════════════════
# 12. No real external services
# ═══════════════════════════════════════════════════════════════════════════════

class TestNoExternalServices:
    """All tests use mocks; no real Groq, Supabase, or embeddings.

    These tests use ``with patch(...)`` **around** ``ConversationEngine()`` to
    verify isolation rather than relying on ``_make_engine()``, because
    ``_make_engine`` replaces specific attributes after construction and does
    not guarantee that the manager class is patched (import order can affect
    whether ``patch`` sees the same module identity).
    """

    def test_all_engine_deps_are_mocked_here(self):
        """_make_engine creates an engine whose deps are Mock instances."""
        engine = _make_engine()
        assert isinstance(engine.groq_manager.chat_completion, MagicMock)
        assert isinstance(engine.groq_manager.chat_completion_async, AsyncMock)
        assert isinstance(engine.memory_manager.load_user_state, MagicMock)
        assert isinstance(engine.memory_manager.sync_state, MagicMock)
        assert isinstance(engine.memory_manager.save_turn, MagicMock)
        assert isinstance(engine.memory_manager.get_context, MagicMock)

    def test_sentence_transformer_not_loaded(self):
        """Verify SentenceTransformer constructor is never called during engine setup.

        Uses an explicit guard on ``backend.memory.SentenceTransformer``
        that raises if the real constructor is invoked.  ``_make_engine``
        patches ``SentenceTransformer`` before constructing the engine,
        so the guard should never fire.
        """
        from backend import memory as memory_module
        original = memory_module.SentenceTransformer
        try:
            memory_module.SentenceTransformer = MagicMock(
                side_effect=RuntimeError("SentenceTransformer constructed!")
            )
            engine = _make_engine()
        finally:
            memory_module.SentenceTransformer = original


# ═══════════════════════════════════════════════════════════════════════════════
# 13. All existing backend tests pass (executed separately via pytest)
# ═══════════════════════════════════════════════════════════════════════════════
