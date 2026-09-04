"""The Groq adapter behind the ``LanguageModel`` contract (issue #337).

This is the only module above ``backend.groq_manager`` allowed to name
Groq symbols (together with the composition roots and the adapter's
own tests). The domain never sees:

* ``GroqClientManager`` — request-scoped client pool, retries, key
  rotation, classification; unchanged, private to this boundary;
* ``GroqPoolExhaustedError`` / ``GroqRequestError`` / SDK exceptions —
  translated here into the canonical ``LanguageModel*Error`` taxonomy
  with constant, sanitized messages;
* ``ChatCompletion`` objects — the adapter extracts the text content
  and returns plain ``str`` / ``AppraisalV1``.

The adapter is deliberately dumb: explicit models from
``ProviderConfig``, explicit temperatures and token limits, validation
of the envelope before any provider call, and one attempt per turn
call (the manager's own bounded retry policy remains inside
``groq_manager`` — no provider-level fallback to another provider or
model ever happens here).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backend.emotional_domain import AppraisalV1, parse_llm_appraisal
from backend.language_model import (
    LanguageModelConfigurationError,
    LanguageModelInvalidResponseError,
    ModelSelection,
    canonical_error_for,
)
from backend.provider_envelope import validate_provider_input
from backend.provider_models import ProviderConfig
from backend.turn_execution import GroqCallParams, TurnBudget

logger = logging.getLogger("GroqLanguageModel")

#: Appraisal system instruction — fixed text, never interpolated with
#: user content (same contract as the previous engine/provider code).
_APPRAISAL_POLICY = (
    "Analyze the emotional impact of this message on the listener (Katherine).\n"
    "Return JSON ONLY:\n"
    '{"valence": -1.0 to 1.0, "arousal_shift": -1.0 to 1.0, '
    '"dominance_shift": -1.0 to 1.0, '
    '"triggered_emotions": {"joy": 0-1, "sadness": 0-1, "anger": 0-1, '
    '"fear": 0-1, "disgust": 0-1, "surprise": 0-1, "tenderness": 0-1, '
    '"guilt": 0-1, "pride": 0-1, "jealousy": 0-1, "gratitude": 0-1}}'
)


class GroqLanguageModel:
    """``LanguageModel`` implementation backed by the Groq service.

    Contracted behavior (pinned by ``test_groq_language_model.py``):

    * ``appraise`` — fast model, temperature 0, JSON mode, appraisal
      token limit; fixed system instruction + user message;
    * ``generate`` — main model, temperature 0.8, main token limit;
    * both validate the envelope locally before any provider call;
    * every provider-side failure crosses the boundary as a canonical
      ``LanguageModel*Error`` with a constant message — never SDK
      exception text, keys, HTTP bodies or user content;
    * ``describe`` — sanitized ``ModelSelection`` (provider + models);
    * one call per turn step; no fallback, no auto-routing.
    """

    def __init__(
        self,
        manager: Any,
        provider_config: ProviderConfig | None = None,
    ) -> None:
        self._manager = manager
        self._config = provider_config or ProviderConfig()

    async def appraise(self, message: str, budget: TurnBudget) -> AppraisalV1:
        messages = [
            {"role": "system", "content": _APPRAISAL_POLICY},
            {"role": "user", "content": message},
        ]
        validate_provider_input(messages)
        try:
            response = await self._manager.chat_completion_async(
                messages=messages,
                model=self._config.fast_model_id,
                budget=budget,
                stage="appraisal",
                temperature=0,
                max_tokens=self._config.appraisal_max_output_tokens,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            raise _translate_provider_error(exc) from None

        raw = _extract_content(response)
        if not raw or not isinstance(raw, str) or not raw.strip():
            raise LanguageModelInvalidResponseError()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            raise LanguageModelInvalidResponseError() from None
        parse_result = parse_llm_appraisal(payload)
        if parse_result.is_fallback:
            # Sanitized fallback event: code only, never payload/prompt/exception.
            logger.info(
                "event=emotional_appraisal_fallback code=%s",
                parse_result.error_code.value if parse_result.error_code else "unknown",
            )
            raise LanguageModelInvalidResponseError()
        return parse_result.appraisal

    async def generate(self, messages: list, budget: TurnBudget) -> str:
        validate_provider_input(messages)
        try:
            response = await self._manager.chat_completion_async(
                messages=messages,
                model=self._config.main_model_id,
                budget=budget,
                stage="generation",
                temperature=0.8,
                max_tokens=self._config.main_max_output_tokens,
            )
        except Exception as exc:
            raise _translate_provider_error(exc) from None

        content = _extract_content(response)
        if not content or not isinstance(content, str) or not content.strip():
            raise LanguageModelInvalidResponseError()
        return content

    async def extract_archival(self, messages: list, budget: TurnBudget) -> str:
        """Archival fact-extraction call (fast model, JSON mode).

        Preserves the exact contracted call shape of the legacy
        ``run_archival_extraction`` call site: fast model, temperature
        0, explicit token limit, JSON mode, ``archival_extraction``
        stage. Failure is a caller-logged background event; here it
        still surfaces as a canonical error.
        """
        validate_provider_input(messages)
        try:
            response = await self._manager.chat_completion_async(
                messages=messages,
                model=self._config.fast_model_id,
                budget=budget,
                stage="archival_extraction",
                temperature=0.0,
                max_tokens=self._config.archival_max_output_tokens,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            raise _translate_provider_error(exc) from None

        content = _extract_content(response)
        if not content or not isinstance(content, str) or not content.strip():
            raise LanguageModelInvalidResponseError()
        return content

    def describe(self) -> ModelSelection:
        return ModelSelection(
            provider="groq",
            main_model_id=self._config.main_model_id,
            fast_model_id=self._config.fast_model_id,
        )


def _extract_content(response: Any) -> Any:
    """Extract the message text without exposing the SDK object upward."""
    try:
        return response.choices[0].message.content
    except (IndexError, AttributeError, TypeError):
        return None


def _translate_provider_error(exc: BaseException) -> Exception:
    """Translate any provider-side failure into the canonical taxonomy.

    Inspects only exception *types* and typed fields (never
    ``str(exc)``), so no SDK text, key detail, HTTP body or user
    content can leak through the boundary.
    """
    from backend.groq_manager import (
        GroqConfigurationError,
        GroqPoolExhaustedError,
        GroqRequestError,
        ProviderFailure,
    )

    if isinstance(exc, GroqConfigurationError):
        return LanguageModelConfigurationError()
    if isinstance(exc, GroqPoolExhaustedError):
        failure = exc.failure_code
        if failure is None:
            from backend.language_model import LanguageModelError, ModelFailure

            return LanguageModelError(ModelFailure.server_error)
        return canonical_error_for(ProviderFailure(failure.value))
    if isinstance(exc, GroqRequestError):
        from backend.language_model import LanguageModelError, ModelFailure

        return LanguageModelError(ModelFailure.server_error)
    if isinstance(exc, BaseException) and type(exc).__name__ in (
        "RateLimitError",
        "AuthenticationError",
        "APITimeoutError",
        "APIConnectionError",
        "APIStatusError",
    ):
        # Defensive: the manager should always classify before raising,
        # but a raw SDK exception still translates through the same
        # classifier rather than crossing the boundary.
        from backend.groq_manager import classify_provider_error

        return canonical_error_for(
            ProviderFailure(classify_provider_error(exc).value)
        )
    from backend.language_model import LanguageModelError, ModelFailure

    return LanguageModelError(ModelFailure.server_error)


def build_groq_language_model(
    keys: list[str] | None = None,
    groq_params: GroqCallParams | None = None,
) -> GroqLanguageModel:
    """Build the real remote provider (Groq) for the composition roots.

    Keys are read on the Python side only (never in any frontend
    bundle, never through any bridge). Raises
    ``LanguageModelConfigurationError`` when no key is configured —
    callers map that to the sanitized configuration codes.
    """
    from backend.groq_manager import GroqClientManager, GroqConfigurationError

    try:
        manager = GroqClientManager(
            keys=keys,
            groq_params=groq_params or GroqCallParams(),
        )
    except GroqConfigurationError:
        raise LanguageModelConfigurationError() from None
    return GroqLanguageModel(manager)
