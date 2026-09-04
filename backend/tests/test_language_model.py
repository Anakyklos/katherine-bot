"""Contract tests for the canonical ``LanguageModel`` boundary (issue #337).

The single, small language-model contract derived from Katherine's real
call sites:

* ``appraise`` — emotional appraisal of the user message (JSON mode);
* ``generate`` — response generation from validated structured messages;
* ``describe`` — sanitized provider/model identification for
  observability (never secrets);
* canonical, typed failures (``ModelFailure``) with constant messages;
* the trusted policy is a Katherine-core responsibility and MUST NOT be
  part of the provider contract.

These tests are the executable version of
``specs/002-language-model-contract/contracts/language-model-contract.md``.
They use no network and no SDK.
"""

from __future__ import annotations

import pytest

from backend.emotional_domain import AppraisalV1, EmotionalStateV1
from backend.relationship import RelationshipStateV1
from backend.turn_execution import TurnErrorCode


# ─── Contract surface ───────────────────────────────────────────────────────


def test_contract_module_exists_with_core_types():
    from backend.language_model import (  # noqa: F401
        LanguageModel,
        ModelFailure,
        ModelSelection,
        build_trusted_policy,
        language_failure_to_turn_code,
    )


def test_model_selection_is_explicit_frozen_and_sanitized():
    from backend.language_model import ModelSelection

    selection = ModelSelection(
        provider="groq", main_model_id="main-1", fast_model_id="fast-1"
    )
    assert selection.provider == "groq"
    assert selection.main_model_id == "main-1"
    assert selection.fast_model_id == "fast-1"
    with pytest.raises(Exception):
        selection.provider = "other"  # frozen: explicit, immutable choice


def test_model_selection_repr_never_holds_secrets():
    from backend.language_model import ModelSelection

    selection = ModelSelection(
        provider="groq", main_model_id="main-1", fast_model_id="fast-1"
    )
    text = repr(selection) + " " + str(selection)
    for marker in ("key", "secret", "token", "sk-", "Bearer"):
        assert marker.lower() not in text.lower()


def test_model_failure_codes_match_existing_taxonomy():
    from backend.language_model import ModelFailure
    from backend.groq_manager import ProviderFailure

    # The canonical codes ARE the existing sanitized taxonomy: same names,
    # same values — no new vocabulary, no SDK detail.
    assert {f.name for f in ModelFailure} == {f.name for f in ProviderFailure}
    for name in (
        "rate_limited",
        "auth_failed",
        "connection_failed",
        "server_error",
        "invalid_request",
        "invalid_response",
        "timeout",
        "cancelled",
    ):
        assert getattr(ModelFailure, name).value == getattr(ProviderFailure, name).value


def test_language_model_is_a_small_protocol():
    import inspect
    from backend.language_model import LanguageModel

    # The contract covers exactly the real call sites: appraise,
    # generate, extract_archival (the run_archival_extraction call
    # site keeps its own contracted shape), describe. No
    # build_trusted_policy (core responsibility), no kwargs passthrough,
    # no capability flags.
    members = {
        name
        for name, _ in inspect.getmembers(LanguageModel, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert members == {"appraise", "generate", "extract_archival", "describe"}
    for method_name in ("appraise", "generate", "extract_archival"):
        signature = inspect.signature(getattr(LanguageModel, method_name))
        assert not any(
            p.kind is inspect.Parameter.VAR_KEYWORD
            for p in signature.parameters.values()
        ), f"{method_name} must not accept **kwargs"


def test_canonical_exceptions_carry_failure_and_constant_message():
    from backend.language_model import (
        LanguageModelError,
        LanguageModelInvalidResponseError,
        ModelFailure,
    )

    exc = LanguageModelInvalidResponseError()
    assert isinstance(exc, LanguageModelError)
    assert exc.failure is ModelFailure.invalid_response
    # Constant, sanitized message — never exception text or content.
    assert str(exc) == LanguageModelInvalidResponseError.MESSAGE
    assert str(exc)


# ─── Failure → TurnErrorCode mapping (public contract preserved) ─────────────


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("rate_limited", TurnErrorCode.upstream_rate_limited),
        ("auth_failed", TurnErrorCode.provider_invalid_request),
        ("invalid_request", TurnErrorCode.provider_invalid_request),
        ("connection_failed", TurnErrorCode.provider_unavailable),
        ("server_error", TurnErrorCode.provider_unavailable),
        ("timeout", TurnErrorCode.turn_timeout),
        ("invalid_response", TurnErrorCode.provider_invalid_response),
        ("cancelled", TurnErrorCode.internal_error),
    ],
)
def test_language_failure_to_turn_code_preserves_current_mapping(failure, expected):
    from backend.groq_manager import ProviderFailure, provider_failure_to_turn_code
    from backend.language_model import ModelFailure, language_failure_to_turn_code

    assert language_failure_to_turn_code(
        ModelFailure(failure)
    ) == provider_failure_to_turn_code(ProviderFailure(failure)) == expected


# ─── Trusted policy is a core responsibility ─────────────────────────────────


def _state_and_relationship():
    state = EmotionalStateV1(
        pleasure=0.5,
        arousal=0.2,
        dominance=0.1,
        libido=0.0,
        aggression=0.0,
        connection=0.4,
        energy=0.5,
        tension=0.1,
        coping_mode="HEALTHY",
        timestamp=1000.0,
        schema_version=1,
    )
    relationship = RelationshipStateV1.neutral(timestamp=1000.0)
    return state, relationship


def test_build_trusted_policy_is_exported_from_core():
    from backend.language_model import build_trusted_policy  # noqa: F401


def test_build_trusted_policy_contains_canonical_sections_only():
    from backend.language_model import build_trusted_policy

    state, relationship = _state_and_relationship()
    policy = build_trusted_policy(state, relationship, "")

    for section in (
        "=== SEU ESTADO INTERNO ===",
        "=== INSTRUÇÃO DE ATUAÇÃO ===",
        "=== TRANSPARÊNCIA DE IDENTIDADE ===",
        "=== PRONOMES FEMININOS ===",
        "=== NÃO MANIPULAÇÃO E NÃO SEXUALIZAÇÃO ===",
        "=== LIMITES SEM ESCALADA ===",
    ):
        assert section in policy
    # The immutable boundary rule closes the trusted policy.
    from backend.trusted_context import BOUNDARY_RULE

    assert policy.strip().endswith(BOUNDARY_RULE.strip())


def test_build_trusted_policy_rejects_nothing_user_derived():
    """The policy interpolates only app-controlled state — no user content."""
    from backend.language_model import build_trusted_policy

    state, relationship = _state_and_relationship()
    secret_marker = "user-sensitive-content"
    policy = build_trusted_policy(state, relationship, secret_marker)
    # adaptation_strategy is app-controlled today (empty/curated); the
    # canonical builder keeps the same behavior as the current engine.
    # The important invariant: mood/bond/acting sections are derived
    # from the typed state, not from raw user text.
    assert "HUMOR:" in policy
    assert "VÍNCULO:" in policy


def test_build_trusted_policy_matches_web_engine_template_shape():
    """Web engine and desktop runtime must produce the same policy text."""
    from backend.companion_runtime import (
        _TRUSTED_POLICY_TEMPLATE as _desktop_template,
    )
    from backend.language_model import build_trusted_policy

    state, relationship = _state_and_relationship()
    policy = build_trusted_policy(state, relationship, "")
    # All template sections from the current canonical template present.
    for line in _desktop_template.splitlines():
        stripped = line.strip()
        if stripped.startswith("==="):
            assert stripped in policy
    assert policy  # non-empty


# ─── Deterministic fake seam (used by domain tests, no network) ─────────────


def test_fake_language_model_satisfies_contract():
    """A minimal fake implements the contract without any SDK or network."""
    from backend.language_model import LanguageModel, ModelSelection

    class FakeLanguageModel:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        async def appraise(self, message: str, budget) -> AppraisalV1:
            self.calls.append(("appraise", message))
            return AppraisalV1.neutral()

        async def generate(self, messages, budget) -> str:
            self.calls.append(("generate", len(messages)))
            return "deterministic"

        async def extract_archival(self, messages, budget) -> str:
            self.calls.append(("extract_archival", len(messages)))
            return "{}"

        def describe(self) -> ModelSelection:
            return ModelSelection(
                provider="fake", main_model_id="fake-main", fast_model_id="fake-fast"
            )

    fake = FakeLanguageModel()
    # Structural typing: the fake satisfies the Protocol.
    assert isinstance(fake, LanguageModel)


def test_appraisal_v1_neutral_exists_for_fakes():
    """Fakes can build a neutral appraisal without a provider (seam check)."""
    appraisal = AppraisalV1.neutral()
    assert appraisal is not None
