"""Adapter tests: the Groq implementation behind the LanguageModel contract.

Issue #337 requirement 2: the current remote adapter preserves its
contracted behavior; requirement 9: the adapter is testable separately
from the domain, with a mocked SDK manager and no network.

Everything here exercises ``backend.groq_language_model`` through the
canonical contract surface. ``GroqClientManager`` itself keeps its own
783-LOC suite (``test_groq_manager.py``); these tests pin the
translation layer: calls in, canonical typed errors out, no Groq
symbols above the adapter.
"""

from __future__ import annotations

import json

import pytest

from backend.groq_manager import (
    GroqConfigurationError,
    GroqPoolExhaustedError,
    GroqRequestError,
    ProviderFailure,
)
from backend.provider_models import ProviderConfig
from backend.turn_execution import TurnBudget


def _budget() -> TurnBudget:
    return TurnBudget(
        deadline=1_000.0,
        reserve=10.0,
        now_provider=lambda: 0.0,
    )


class MockCompletion:
    def __init__(self, content="Mock response"):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class RecordingManager:
    """Deterministic manager fake: records calls, returns scripted results."""

    def __init__(self, completion=None, error=None):
        self.calls: list[dict] = []
        self.completion = completion or MockCompletion("ok")
        self.error = error

    async def chat_completion_async(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.completion


def _appraisal_payload(**overrides):
    payload = {
        "valence": 0.2,
        "arousal_shift": 0.1,
        "dominance_shift": 0.0,
        "triggered_emotions": {"joy": 0.5},
    }
    payload.update(overrides)
    return json.dumps(payload)


# ─── Appraise: contracted call shape ─────────────────────────────────────────


@pytest.mark.anyio
async def test_appraise_uses_fast_model_json_mode_and_validates_input_first():
    from backend.groq_language_model import GroqLanguageModel

    config = ProviderConfig()
    manager = RecordingManager(completion=MockCompletion(_appraisal_payload()))
    model = GroqLanguageModel(manager=manager, provider_config=config)

    appraisal = await model.appraise("oi", _budget())

    assert appraisal is not None
    call = manager.calls[0]
    assert call["model"] == config.fast_model_id
    assert call["temperature"] == 0
    assert call["response_format"] == {"type": "json_object"}
    assert call["max_tokens"] == config.appraisal_max_output_tokens
    assert call["stage"] == "appraisal"
    assert call["budget"] is not None
    # System instruction + user message; no interpolation of user content
    # into the system role.
    roles = [m["role"] for m in call["messages"]]
    assert roles == ["system", "user"]


@pytest.mark.anyio
async def test_generate_uses_main_model_and_temperature():
    from backend.groq_language_model import GroqLanguageModel

    config = ProviderConfig()
    manager = RecordingManager(completion=MockCompletion("resposta"))
    model = GroqLanguageModel(manager=manager, provider_config=config)

    text = await model.generate(
        [{"role": "system", "content": "policy"}, {"role": "user", "content": "oi"}],
        _budget(),
    )

    assert text == "resposta"
    call = manager.calls[0]
    assert call["model"] == config.main_model_id
    assert call["temperature"] == 0.8
    assert call["max_tokens"] == config.main_max_output_tokens
    assert call["stage"] == "generation"


@pytest.mark.anyio
async def test_generate_rejects_empty_response_sanitized():
    from backend.groq_language_model import GroqLanguageModel
    from backend.language_model import (
        LanguageModelInvalidResponseError,
        ModelFailure,
    )

    manager = RecordingManager(completion=MockCompletion("   "))
    model = GroqLanguageModel(manager=manager, provider_config=ProviderConfig())

    with pytest.raises(LanguageModelInvalidResponseError) as excinfo:
        await model.generate([{"role": "user", "content": "oi"}], _budget())
    assert excinfo.value.failure is ModelFailure.invalid_response


# ─── Error translation at the boundary ───────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure,expected_exc",
    [
        (ProviderFailure.rate_limited, "LanguageModelRateLimitedError"),
        (ProviderFailure.auth_failed, "LanguageModelAuthFailedError"),
        (ProviderFailure.connection_failed, "LanguageModelConnectionFailedError"),
        (ProviderFailure.server_error, "LanguageModelServerError"),
        (ProviderFailure.invalid_request, "LanguageModelInvalidRequestError"),
        (ProviderFailure.invalid_response, "LanguageModelInvalidResponseError"),
        (ProviderFailure.timeout, "LanguageModelTimeoutError"),
    ],
)
async def test_pool_exhaustion_translates_to_canonical_errors(failure, expected_exc):
    from backend import language_model as lm
    from backend.groq_language_model import GroqLanguageModel

    manager = RecordingManager(error=GroqPoolExhaustedError(code=failure))
    model = GroqLanguageModel(manager=manager, provider_config=ProviderConfig())

    with pytest.raises(getattr(lm, expected_exc)):
        await model.generate([{"role": "user", "content": "oi"}], _budget())


@pytest.mark.anyio
async def test_request_error_translates_to_canonical_unavailable():
    from backend.groq_language_model import GroqLanguageModel
    from backend.language_model import LanguageModelError, ModelFailure

    manager = RecordingManager(error=GroqRequestError("boom"))
    model = GroqLanguageModel(manager=manager, provider_config=ProviderConfig())

    with pytest.raises(LanguageModelError) as excinfo:
        await model.generate([{"role": "user", "content": "oi"}], _budget())
    assert excinfo.value.failure in (
        ModelFailure.connection_failed,
        ModelFailure.server_error,
    )
    # Sanitized: the raw SDK/manager text never crosses the boundary.
    assert "boom" not in str(excinfo.value)


@pytest.mark.anyio
async def test_invalid_appraisal_json_maps_to_invalid_response():
    from backend.groq_language_model import GroqLanguageModel
    from backend.language_model import (
        LanguageModelInvalidResponseError,
        ModelFailure,
    )

    manager = RecordingManager(completion=MockCompletion("não é json"))
    model = GroqLanguageModel(manager=manager, provider_config=ProviderConfig())

    with pytest.raises(LanguageModelInvalidResponseError) as excinfo:
        await model.appraise("oi", _budget())
    assert excinfo.value.failure is ModelFailure.invalid_response


@pytest.mark.anyio
async def test_appraisal_fallback_parse_maps_to_invalid_response():
    from backend.groq_language_model import GroqLanguageModel
    from backend.language_model import (
        LanguageModelInvalidResponseError,
        ModelFailure,
    )

    # Structurally invalid appraisal payload: parser falls back.
    manager = RecordingManager(completion=MockCompletion(json.dumps({"nada": 1})))
    model = GroqLanguageModel(manager=manager, provider_config=ProviderConfig())

    with pytest.raises(LanguageModelInvalidResponseError) as excinfo:
        await model.appraise("oi", _budget())
    assert excinfo.value.failure is ModelFailure.invalid_response


# ─── describe(): explicit, sanitized observability ──────────────────────────


def test_describe_returns_explicit_sanitized_selection():
    from backend.groq_language_model import GroqLanguageModel
    from backend.language_model import ModelSelection

    config = ProviderConfig()
    model = GroqLanguageModel(manager=RecordingManager(), provider_config=config)

    selection = model.describe()
    assert isinstance(selection, ModelSelection)
    assert selection.provider == "groq"
    assert selection.main_model_id == config.main_model_id
    assert selection.fast_model_id == config.fast_model_id
    text = repr(selection)
    for marker in ("key", "secret", "token", "Bearer", "sk-"):
        assert marker.lower() not in text.lower()


# ─── No fallback, no second provider ────────────────────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    "failure", [ProviderFailure.rate_limited, ProviderFailure.server_error]
)
async def test_provider_failure_never_escalates_to_second_call(failure):
    """Explicit no-fallback: exactly one attempt, then the turn fails."""
    from backend.groq_language_model import GroqLanguageModel
    from backend.language_model import LanguageModelError

    manager = RecordingManager(error=GroqPoolExhaustedError(code=failure))
    model = GroqLanguageModel(manager=manager, provider_config=ProviderConfig())

    with pytest.raises(LanguageModelError):
        await model.generate([{"role": "user", "content": "oi"}], _budget())

    assert len(manager.calls) == 1, "adapter must not retry or fall back on its own"


# ─── Factory: composition seam ──────────────────────────────────────────────


def test_build_groq_language_model_factory_exists():
    from backend.groq_language_model import GroqLanguageModel, build_groq_language_model

    model = build_groq_language_model(
        keys=["test-key-never-printed"],
        groq_params=None,
    )
    assert isinstance(model, GroqLanguageModel)
    assert model.describe().provider == "groq"


def test_build_groq_language_model_without_keys_raises_configuration():
    from backend.groq_language_model import build_groq_language_model
    from backend.language_model import LanguageModelConfigurationError

    with pytest.raises((LanguageModelConfigurationError, GroqConfigurationError)):
        build_groq_language_model(keys=[])


def test_groq_adapter_factory_is_lazy_and_probe_bound(monkeypatch):
    """Second review: provider composition helper lives in the adapter.

    ``build_groq_language_model_factory`` (in the Groq adapter module,
    not the generic contract) captures keys at composition time, defers
    the adapter construction to first use and binds the readiness probe
    to the same captured values.
    """
    import backend.groq_keys as groq_keys_module
    from backend.groq_language_model import build_groq_language_model_factory

    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY_2", raising=False)
    monkeypatch.setattr(groq_keys_module, "get_groq_api_keys", lambda: [None, None])

    keys = ["gsk_test_composition_capture"]
    factory = build_groq_language_model_factory(keys=keys)

    # Probe answers from the captured configuration only: True with a
    # captured key, False without one — and it never constructs the
    # adapter (no SDK instantiation, no inference call).
    assert factory.provider_configured_probe() is True
    empty = build_groq_language_model_factory(keys=[])
    assert empty.provider_configured_probe() is False

    model = factory()
    assert model.describe().provider == "groq"
    # The adapter was built from exactly the captured keys.
    assert model._manager._keys == ["gsk_test_composition_capture"]


def test_cancellation_propagates_natively_without_second_call():
    """Second review: executable cancellation semantics.

    ``asyncio.CancelledError`` is control flow: the adapter propagates
    it immediately, untranslated (never a ``LanguageModel*Error``), and
    deterministic proof that no second provider call happens after the
    cancellation.
    """
    import asyncio

    from backend.groq_language_model import GroqLanguageModel

    class CancellingManager:
        """Manager that raises CancelledError on the Nth call."""

        def __init__(self, cancel_on: int) -> None:
            self.calls = 0
            self._cancel_on = cancel_on

        async def chat_completion_async(self, **kwargs):
            self.calls += 1
            if self.calls >= self._cancel_on:
                raise asyncio.CancelledError()
            return object()

    async def _run() -> tuple[type | None, int]:
        manager = CancellingManager(cancel_on=1)
        model = GroqLanguageModel(manager)
        budget = _budget()
        try:
            await model.generate([{"role": "user", "content": "hi"}], budget)
        except BaseException as exc:  # noqa: BLE001 (capture native type)
            return type(exc), manager.calls
        return None, manager.calls

    loop = asyncio.new_event_loop()
    try:
        exc_type, calls = loop.run_until_complete(_run())
    finally:
        loop.close()

    # Native cancellation propagated as itself, exactly one provider
    # call, no retry, no translation into the canonical taxonomy.
    assert exc_type is asyncio.CancelledError
    assert calls == 1
