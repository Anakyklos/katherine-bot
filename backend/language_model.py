"""The canonical ``LanguageModel`` contract (issue #337).

Katherine is not a specific model. This module is the single, small
boundary between the Katherine core (conversation engine, turn flow,
trusted context) and any language-model implementation — remote today
(Groq behind ``backend.groq_language_model.py``), local in the future.

What the contract covers (derived exclusively from the real call
sites, not from imagined providers):

* ``appraise`` — emotional appraisal of the user's message (JSON mode);
* ``generate`` — response generation from validated structured messages;
* ``describe`` — sanitized provider/model identification for
  observability;
* canonical typed failures (``ModelFailure``) with constant messages.

What deliberately does NOT belong here:

* ``build_trusted_policy`` — the trusted system policy is a Katherine
  core responsibility (identity, presentation, safety rules); it
  lives in ``backend/trusted_policy.py``, not in the provider
  contract;
* capability flags, ``**kwargs`` passthrough, SDK objects, universal
  provider abstractions;
* timeout/deadline mechanics — ``TurnBudget`` (already shared by web
  and desktop) carries the deadline; the contract does not duplicate it.

Security invariants (testable in ``test_language_model_isolation.py``):

* no provider SDK import in this module;
* no secrets in ``ModelSelection``/exceptions (constant messages only);
* failure codes are low-cardinality and provider-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from backend.emotional_domain import AppraisalV1
from backend.turn_execution import TurnBudget, TurnErrorCode

__all__ = [
    "LanguageModel",
    "ModelSelection",
    "ModelFailure",
    "LanguageModelError",
    "LanguageModelRateLimitedError",
    "LanguageModelAuthFailedError",
    "LanguageModelConnectionFailedError",
    "LanguageModelServerError",
    "LanguageModelInvalidRequestError",
    "LanguageModelInvalidResponseError",
    "LanguageModelTimeoutError",
    "LanguageModelCancelledError",
    "LanguageModelConfigurationError",
    "language_failure_to_turn_code",
    "resolve_language_model_factory",
]


# ─── Selection (explicit, sanitized) ────────────────────────────────────────


@dataclass(frozen=True)
class ModelSelection:
    """The explicitly selected provider and models.

    ``provider`` is the selected provider identifier (today: ``"groq"``).
    Model ids are explicit and sanitized for observability. This object
    never carries keys, tokens, URLs with credentials or SDK details.
    Selection happens at composition time; there is no runtime
    re-selection, no auto-routing, no fallback to another provider.
    """

    provider: str
    main_model_id: str
    fast_model_id: str


#: Providers with a concrete adapter behind this contract. Adding a new
#: remote provider means adding a new adapter and extending this set —
#: nothing else in the domain changes.
SUPPORTED_PROVIDERS: frozenset[str] = frozenset({"groq"})


# ─── Canonical failure taxonomy ─────────────────────────────────────────────


class ModelFailure(str, Enum):
    """Sanitized, low-cardinality provider-agnostic failure codes.

    Identical vocabulary to the existing ``ProviderFailure`` taxonomy
    (``backend.groq_manager``), which is preserved bit-for-bit in
    ``language_failure_to_turn_code``. No raw exception text, key
    prefixes, HTTP bodies or user content ever travel with these codes.
    """

    rate_limited = "rate_limited"
    auth_failed = "auth_failed"
    connection_failed = "connection_failed"
    server_error = "server_error"
    invalid_request = "invalid_request"
    invalid_response = "invalid_response"
    timeout = "timeout"
    cancelled = "cancelled"


class LanguageModelError(Exception):
    """Base canonical error crossing the LanguageModel boundary.

    The ``MESSAGE`` of each concrete error is a constant, sanitized
    string; ``str(exc)`` never contains exception text from the
    provider SDK, keys, prompts, HTTP bodies or user content.
    """

    MESSAGE: str = "Language model request failed."

    def __init__(self, failure: ModelFailure) -> None:
        # Deliberately no message parameter: callers must never pass
        # SDK text, key material, HTTP bodies, prompts or paths through
        # the constructor. ``str(exc)`` is always this class's constant
        # ``MESSAGE`` — public messages are constant/allowlisted only.
        super().__init__(self.MESSAGE)
        self.failure = failure
        self.message = self.MESSAGE


class LanguageModelRateLimitedError(LanguageModelError):
    MESSAGE = "Language model is rate limited."

    def __init__(self) -> None:
        super().__init__(ModelFailure.rate_limited)


class LanguageModelAuthFailedError(LanguageModelError):
    MESSAGE = "Language model authentication failed."

    def __init__(self) -> None:
        super().__init__(ModelFailure.auth_failed)


class LanguageModelConnectionFailedError(LanguageModelError):
    MESSAGE = "Language model connection failed."

    def __init__(self) -> None:
        super().__init__(ModelFailure.connection_failed)


class LanguageModelServerError(LanguageModelError):
    MESSAGE = "Language model server error."

    def __init__(self) -> None:
        super().__init__(ModelFailure.server_error)


class LanguageModelInvalidRequestError(LanguageModelError):
    MESSAGE = "Language model rejected the request."

    def __init__(self) -> None:
        super().__init__(ModelFailure.invalid_request)


class LanguageModelInvalidResponseError(LanguageModelError):
    MESSAGE = "Language model returned an invalid response."

    def __init__(self) -> None:
        super().__init__(ModelFailure.invalid_response)


class LanguageModelTimeoutError(LanguageModelError):
    MESSAGE = "Language model request timed out."

    def __init__(self) -> None:
        super().__init__(ModelFailure.timeout)


class LanguageModelCancelledError(LanguageModelError):
    MESSAGE = "Language model request was cancelled."

    def __init__(self) -> None:
        super().__init__(ModelFailure.cancelled)


class LanguageModelConfigurationError(LanguageModelError):
    """Configuration is missing or invalid (no key, unknown provider).

    Sanitized: never names the missing secret's value, the keyring path,
    or the raw configuration error.
    """

    MESSAGE = "Language model is not configured."

    def __init__(self) -> None:
        super().__init__(ModelFailure.invalid_request)


#: Every canonical failure code → the concrete exception type carrying it.
_FAILURE_TO_ERROR: dict[ModelFailure, type[LanguageModelError]] = {
    ModelFailure.rate_limited: LanguageModelRateLimitedError,
    ModelFailure.auth_failed: LanguageModelAuthFailedError,
    ModelFailure.connection_failed: LanguageModelConnectionFailedError,
    ModelFailure.server_error: LanguageModelServerError,
    ModelFailure.invalid_request: LanguageModelInvalidRequestError,
    ModelFailure.invalid_response: LanguageModelInvalidResponseError,
    ModelFailure.timeout: LanguageModelTimeoutError,
    ModelFailure.cancelled: LanguageModelCancelledError,
}


def canonical_error_for(failure: ModelFailure) -> LanguageModelError:
    """Build the canonical exception for a failure code (constant message)."""
    return _FAILURE_TO_ERROR[failure]()


def language_failure_to_turn_code(failure: ModelFailure) -> TurnErrorCode:
    """Map a canonical failure to the public ``TurnErrorCode``.

    Preserves the existing provider_failure_to_turn_code mapping
    bit-for-bit so HTTP status codes and stage events are unchanged:

    * rate_limited → upstream_rate_limited (429)
    * auth_failed / invalid_request → provider_invalid_request (503)
    * connection_failed / server_error → provider_unavailable (503)
    * timeout → turn_timeout (504)
    * invalid_response → provider_invalid_response (500)
    * cancelled → propagated (internal_error only when converted)
    """
    mapping = {
        ModelFailure.rate_limited: TurnErrorCode.upstream_rate_limited,
        ModelFailure.auth_failed: TurnErrorCode.provider_invalid_request,
        ModelFailure.invalid_request: TurnErrorCode.provider_invalid_request,
        ModelFailure.connection_failed: TurnErrorCode.provider_unavailable,
        ModelFailure.server_error: TurnErrorCode.provider_unavailable,
        ModelFailure.timeout: TurnErrorCode.turn_timeout,
        ModelFailure.invalid_response: TurnErrorCode.provider_invalid_response,
        ModelFailure.cancelled: TurnErrorCode.internal_error,
    }
    return mapping.get(failure, TurnErrorCode.provider_invalid_response)


# ─── The contract ───────────────────────────────────────────────────────────


@runtime_checkable
class LanguageModel(Protocol):
    """The single language-model boundary used by the Katherine core.

    Implementations (remote adapters today, a local adapter in the
    future) receive already-validated structured messages and a
    ``TurnBudget`` carrying the deadline. They return plain text and
    typed appraisal objects — never SDK objects — and raise only the
    canonical ``LanguageModel*Error`` exceptions. A provider failure is
    a turn failure: implementations never fall back to another
    provider or model on their own.
    """

    async def appraise(self, message: str, budget: TurnBudget) -> AppraisalV1:
        """Appraise the emotional impact of the user's message."""
        ...

    async def generate(self, messages: list, budget: TurnBudget) -> str:
        """Generate a response from validated structured messages."""
        ...

    async def extract_archival(self, messages: list, budget: TurnBudget) -> str:
        """Extract long-term memory facts from a persisted turn.

        This is the third real call site (``run_archival_extraction``).
        It keeps its own contracted call shape — fast model, JSON
        mode, temperature 0, explicit token limit — which the adapter
        applies; collapsing it into ``generate`` would silently change
        the provider call parameters at runtime.
        """
        ...

    def describe(self) -> ModelSelection:
        """Sanitized provider/model identification for observability."""
        ...


def resolve_language_model_factory(
    provider: str,
    keys: tuple[str, ...] | list[str] | None = None,
    call_params: Any | None = None,
):
    """Resolve the explicit provider to a lazy adapter factory.

    Returns a zero-argument callable that builds the concrete adapter
    on first use (the adapter import happens inside the factory, so
    the contract module and its importers never load a provider SDK
    at import time). Unknown providers fail sanitized here.

    ``keys`` and ``call_params`` are captured **at composition time**
    from the application settings (``Settings.provider_keys()`` and
    the provider-call parameters derived from ``Settings.turn_config``).
    The factory then builds the adapter from exactly those captured
    values — the adapter never falls back to reading the process
    environment. ``call_params`` is typed as ``Any`` on purpose: it is
    an opaque, provider-specific composition payload (e.g.
    ``GroqCallParams``) whose concrete type belongs to the provider
    adapter; the generic contract does not define or inspect it.
    """
    if provider not in SUPPORTED_PROVIDERS:
        raise LanguageModelConfigurationError()

    captured_keys = tuple(
        k for k in (keys or ()) if isinstance(k, str) and k.strip()
    )
    captured_params = call_params

    def _factory() -> "LanguageModel":
        from importlib import import_module

        adapter = import_module(f"backend.{provider}_language_model")
        builder = getattr(adapter, f"build_{provider}_language_model", None)
        if builder is None:
            raise LanguageModelConfigurationError()
        return builder(keys=list(captured_keys), call_params=captured_params)

    def _configured() -> bool:
        """Readiness probe bound to the same captured configuration.

        Presence-only: ``True`` iff the composition captured at least
        one non-empty key for the explicitly selected provider. Pure
        configuration check — no SDK instantiation, no network, no
        inference call, and the key value is never echoed.
        """
        return bool(captured_keys)

    _factory.provider_configured_probe = _configured  # type: ignore[attr-defined]

    return _factory
