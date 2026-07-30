"""
Final contract tests for provider-envelope issue #294.

Covers:

=== Suffix preservation without optional components ===
1. Suffix appended exactly once with empty optional_context_components
2. Budget exceeded when mandatory + suffix exceeds max_units
3. No system message fails closed with suffix

=== RPC response validation in memory.py ===
4. response is None returns []
5. object without data attribute returns []
6. response.data is None returns []
7. response.data is not a list returns []
8. execute() raises exception returns [], no sensitive data in logs
9. Valid documents preserved before and after invalid entries/metadata

=== Real memory retrieval path ===
10. RetrievedMemory content/tags/duplicate normalization
11. Invalid metadata does not discard adjacent documents
12. RPC relevance order preserved
13. Content and tags use single representation in context

=== Safety suffix precedence ===
14. All user-derived sections precede safety suffix

=== Generation pruning observability ===
15. No prune event when all context fits
16. Partial pruning keeps some discards others (single event)
17. Total pruning removes all optional (single event)
18. Prune log is sanitized

=== ContextFitResult contract ===
19. Return annotation and result contracts
20. No system message fails closed without logging content
"""

from __future__ import annotations

import logging
from typing import get_type_hints
from unittest.mock import MagicMock, patch

import pytest

from backend.memory import MemoryManager, RetrievedMemory
from backend.provider_envelope import (
    ContextFitResult,
    ProviderEnvelopeError,
    fit_optional_context,
    validate_provider_input,
)


PRUNED_EVENT = "event=provider_input_pruned stage=generation"


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════


def _memory_manager_with_rpc_documents(documents: list[object]) -> MemoryManager:
    """Build a MemoryManager double that exercises the real retrieval method."""
    manager = MemoryManager.__new__(MemoryManager)
    manager.embedding_model = MagicMock()
    manager.embedding_model.encode.return_value.tolist.return_value = [0.1, 0.2, 0.3]
    response = MagicMock()
    response.data = documents
    rpc_result = MagicMock()
    rpc_result.execute.return_value = response
    manager.supabase = MagicMock()
    manager.supabase.rpc.return_value = rpc_result
    return manager


def _generation_engine():
    """Construct ConversationEngine without provider, database, or embeddings."""
    from backend.engine import ConversationEngine
    from backend.groq_manager import GroqClientManager

    with (
        patch.object(GroqClientManager, "__init__", return_value=None),
        patch.object(MemoryManager, "__init__", return_value=None),
    ):
        return ConversationEngine(clock=lambda: 1000.0)


def _neutral_states():
    from backend.emotional_domain import EmotionalStateV1
    from backend.relationship import RelationshipStateV1

    return (
        EmotionalStateV1.neutral(timestamp=1000.0),
        RelationshipStateV1.neutral(timestamp=1000.0),
    )


def _pruned_messages(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage() == PRUNED_EVENT
    ]


# ═══════════════════════════════════════════════════════════════════════
# 1. Suffix preservation: empty optional_context_components
# ═══════════════════════════════════════════════════════════════════════

class TestSuffixPreservation:
    """Suffix is preserved when optional_context_components is empty."""

    def test_suffix_preserved_with_empty_optional(self):
        """Suffix appears exactly once after header when no optional context.

        Verifies:
        - ContextFitResult is returned
        - selected_indices is empty
        - pruned is False
        - user message is byte-exact preserved
        - suffix appears exactly once
        - suffix appears after the header
        - final envelope passes validation
        """
        result = fit_optional_context(
            [
                {"role": "system", "content": "MANDATORY_HEADER"},
                {"role": "user", "content": "CURRENT_MESSAGE"},
            ],
            [],
            suffix="MANDATORY_SAFETY_SUFFIX",
        )

        assert isinstance(result, ContextFitResult)
        assert result.selected_indices == frozenset()
        assert result.pruned is False
        assert result.messages[1]["content"] == "CURRENT_MESSAGE"
        assert result.messages[0]["content"].count(
            "MANDATORY_SAFETY_SUFFIX"
        ) == 1

        # Suffix appears after the header
        header_pos = result.messages[0]["content"].find("MANDATORY_HEADER")
        suffix_pos = result.messages[0]["content"].find("MANDATORY_SAFETY_SUFFIX")
        assert header_pos >= 0
        assert suffix_pos >= 0
        assert header_pos < suffix_pos, (
            "Suffix should appear after mandatory header"
        )

        # Final envelope validates
        validate_provider_input(result.messages)

    def test_suffix_without_header_only_user(self):
        """Suffix without optional components and without system message fails.

        No system message means there is no place to append the suffix.
        """
        with pytest.raises(
            ProviderEnvelopeError,
            match="no_system_message",
        ):
            fit_optional_context(
                [{"role": "user", "content": "Hello"}],
                [],
                suffix="SAFETY_SUFFIX",
            )

    def test_suffix_exactly_once_with_system_header(self):
        """Suffix appears exactly once; mandatory content is unchanged."""
        mandatory = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"},
        ]
        suffix = "SAFETY_RULES_END"
        result = fit_optional_context(mandatory, [], suffix=suffix)

        system_content = result.messages[0]["content"]
        assert system_content.count(suffix) == 1, (
            f"Suffix should appear exactly once, found {system_content.count(suffix)}"
        )
        assert "You are a helpful assistant." in system_content
        assert result.messages[1]["content"] == "Hello!"


# ═══════════════════════════════════════════════════════════════════════
# 2. Budget boundary: mandatory + suffix exceeds max_units
# ═══════════════════════════════════════════════════════════════════════

class TestSuffixBudgetBoundary:
    """Budget boundary when mandatory + suffix exceeds max_units."""

    def test_mandatory_only_fits_suffix_exceeds(self):
        """Mandatory alone fits within max_units, but suffix pushes over.

        Must raise budget_exceeded — not return without suffix.
        Also verifies that the same call without suffix does NOT raise,
        confirming the failure is specifically caused by the suffix.
        """
        from backend.provider_envelope import estimate_provider_input_units

        mandatory = [
            {"role": "system", "content": "Short header."},
            {"role": "user", "content": "Hi"},
        ]
        base_units = estimate_provider_input_units(mandatory)

        suffix = "X" * 100

        with_suffix = [
            {"role": "system", "content": "Short header.\n\n" + suffix},
            {"role": "user", "content": "Hi"},
        ]
        with_suffix_units = estimate_provider_input_units(with_suffix)

        tight_budget = base_units + 5  # barely over mandatory, under with suffix
        assert tight_budget < with_suffix_units, (
            f"Budget {tight_budget} should be less than with-suffix cost {with_suffix_units}"
        )

        # Same call without suffix must NOT raise
        fit_optional_context(mandatory, [], max_units=tight_budget)

        # Call with suffix must raise budget_exceeded
        with pytest.raises(
            ProviderEnvelopeError,
            match="budget_exceeded",
        ):
            fit_optional_context(
                mandatory,
                [],
                max_units=tight_budget,
                suffix=suffix,
            )


# ═══════════════════════════════════════════════════════════════════════
# 3. No system message with suffix
# ═══════════════════════════════════════════════════════════════════════

class TestSuffixNoSystemMessage:
    """Fit_optional_context fails closed when no system message exists."""

    def test_no_system_message_with_suffix(self):
        """Empty optional components, non-empty suffix, only user message.

        Expected: ProviderEnvelopeError("no_system_message") because
        there is no system message to append the suffix to.
        """
        with pytest.raises(
            ProviderEnvelopeError,
            match="no_system_message",
        ):
            fit_optional_context(
                [{"role": "user", "content": "Just a user message"}],
                [],
                suffix="MANDATORY_SAFETY_SUFFIX",
            )

    def test_no_system_message_with_empty_suffix(self):
        """No system message but empty suffix — should still pass.

        When suffix is empty, no system message is needed because
        nothing needs to be appended.
        """
        result = fit_optional_context(
            [{"role": "user", "content": "Just a user message"}],
            [],
            suffix="",
        )
        assert result.messages == [{"role": "user", "content": "Just a user message"}]


# ═══════════════════════════════════════════════════════════════════════
# 4–9. RPC response validation in _retrieve_relevant_entries
# ═══════════════════════════════════════════════════════════════════════

class TestRpcResponseValidation:
    """_retrieve_relevant_entries handles structurally invalid RPC responses.

    Uses MemoryManager with doubles installed before the call — no real
    Supabase access.
    """

    @pytest.fixture
    def manager(self):
        """Create a MemoryManager with mocked supabase and embedding model.

        Uses a supabase_factory that returns None to avoid loading real
        SentenceTransformer model and real Supabase client.
        """
        mm = MemoryManager(supabase_factory=lambda: None)
        mm.embedding_model = MagicMock()
        mm.embedding_model.encode.return_value.tolist.return_value = [
            0.1, 0.2, 0.3,
        ]
        mm.supabase = MagicMock()
        return mm

    def test_response_none_returns_empty(self, manager):
        """response = None returns []."""
        rpc_mock = MagicMock()
        rpc_mock.execute.return_value = None
        manager.supabase.rpc.return_value = rpc_mock
        result = manager._retrieve_relevant_entries("user-id", "query")
        assert result == []

    def test_response_without_data_returns_empty(self, manager):
        """Object without data attribute returns []."""
        response = object()
        rpc_mock = MagicMock()
        rpc_mock.execute.return_value = response
        manager.supabase.rpc.return_value = rpc_mock
        result = manager._retrieve_relevant_entries("user-id", "query")
        assert result == []

    def test_response_data_none_returns_empty(self, manager):
        """response.data = None returns []."""
        rpc_mock = MagicMock()
        response = MagicMock()
        response.data = None
        rpc_mock.execute.return_value = response
        manager.supabase.rpc.return_value = rpc_mock
        result = manager._retrieve_relevant_entries("user-id", "query")
        assert result == []

    def test_response_data_not_list_returns_empty(self, manager):
        """response.data is a dict (not list) returns []."""
        rpc_mock = MagicMock()
        response = MagicMock()
        response.data = {"not": "a list"}
        rpc_mock.execute.return_value = response
        manager.supabase.rpc.return_value = rpc_mock
        result = manager._retrieve_relevant_entries("user-id", "query")
        assert result == []

    def test_execute_raises_exception_returns_empty(self, manager, caplog):
        """execute() raising RuntimeError returns [] and no sensitive data."""
        sensitive = "SENSITIVE_RPC_ERROR"
        rpc_mock = MagicMock()
        rpc_mock.execute.side_effect = RuntimeError(sensitive)
        manager.supabase.rpc.return_value = rpc_mock
        caplog.set_level(logging.DEBUG)
        result = manager._retrieve_relevant_entries("user-id", "query")
        assert result == []
        assert sensitive not in caplog.text

    def test_valid_documents_preserved_across_invalid_entries(self, manager):
        """Valid docs before/after invalid entries (whitespace, empty, missing keys)."""
        rpc_mock = MagicMock()
        response = MagicMock()
        response.data = [
            {"content": "First valid memory.", "metadata": {"tags": ["tag1"]}},
            {"content": "   ", "metadata": {"tags": []}},
            {"content": "Second valid memory.", "metadata": {"tags": ["tag2"]}},
            {"content": "", "metadata": {"tags": []}},
            {"content": "Third valid memory.", "metadata": {"tags": ["tag3"]}},
            {},
        ]
        rpc_mock.execute.return_value = response
        manager.supabase.rpc.return_value = rpc_mock
        result = manager._retrieve_relevant_entries("user-id", "query")
        assert len(result) == 3
        assert result[0].content == "First valid memory."
        assert result[1].content == "Second valid memory."
        assert result[2].content == "Third valid memory."

    def test_invalid_metadata_isolated_to_document(self, manager):
        """Non-dict metadata (string, None, int) does not crash the batch.

        Documents with invalid metadata are still included; the tag policy
        handles non-dict metadata gracefully by falling back to empty tags.
        """
        rpc_mock = MagicMock()
        response = MagicMock()
        response.data = [
            {"content": "First valid memory.", "metadata": {"tags": ["tag1"]}},
            {"content": "Memory with bad metadata.", "metadata": "invalid_string"},
            {"content": "Memory with None metadata.", "metadata": None},
            {"content": "Second valid memory.", "metadata": {"tags": ["tag2"]}},
            {"content": "Memory with int metadata.", "metadata": 42},
            {"content": "Third valid memory.", "metadata": {"tags": ["tag3"]}},
        ]
        rpc_mock.execute.return_value = response
        manager.supabase.rpc.return_value = rpc_mock
        result = manager._retrieve_relevant_entries("user-id", "query")

        # All 6 documents returned; invalid metadata entries get empty tags
        assert [entry.content for entry in result] == [
            "First valid memory.",
            "Memory with bad metadata.",
            "Memory with None metadata.",
            "Second valid memory.",
            "Memory with int metadata.",
            "Third valid memory.",
        ]
        assert [entry.tags for entry in result] == [
            ("tag1",),
            (),
            (),
            ("tag2",),
            (),
            ("tag3",),
        ]


# ═══════════════════════════════════════════════════════════════════════
# 10. RetrievedMemory: content/tags/duplicate normalization
# ═══════════════════════════════════════════════════════════════════════

class TestRetrievedMemoryRealPath:
    """Exercises the real memory retrieval path via _retrieve_relevant_entries."""

    def test_content_tags_and_duplicate_normalization(self):
        content = "First line\nSecond line"
        memory = RetrievedMemory(
            content=content,
            tags=(" pets ", "preference", "pets", "", 42, {"bad": "tag"}),
        )
        assert memory.content == content
        assert memory.tags == ("pets", "preference")
        assert memory.to_prompt_text() == (
            "First line\nSecond line\nTags: pets, preference"
        )

    def test_invalid_metadata_does_not_discard_adjacent_documents(self):
        manager = _memory_manager_with_rpc_documents([
            {
                "content": "First valid memory.",
                "metadata": {"tags": ["first", "shared"]},
            },
            {
                "content": "Valid content with invalid metadata.",
                "metadata": ["not", "a", "mapping"],
            },
            {
                "content": "Last valid memory.",
                "metadata": {"tags": ["last"]},
            },
        ])
        entries = manager._retrieve_relevant_entries("user-1", "query")
        assert [entry.content for entry in entries] == [
            "First valid memory.",
            "Valid content with invalid metadata.",
            "Last valid memory.",
        ]
        assert [entry.tags for entry in entries] == [
            ("first", "shared"),
            (),
            ("last",),
        ]

    def test_rpc_relevance_order_is_preserved(self):
        manager = _memory_manager_with_rpc_documents([
            {"content": "Most relevant", "metadata": {"tags": ["one"]}},
            {"content": "Second relevant", "metadata": {"tags": ["two"]}},
            {"content": "Third relevant", "metadata": {"tags": ["three"]}},
        ])
        entries = manager._retrieve_relevant_entries("user-2", "query")
        assert [entry.content for entry in entries] == [
            "Most relevant",
            "Second relevant",
            "Third relevant",
        ]

    def test_context_uses_one_representation_for_content_and_tags(self):
        manager = MemoryManager.__new__(MemoryManager)
        manager.load_recent_history = MagicMock(return_value=[])
        memory = RetrievedMemory(
            content="User likes cats.\nThis is still the same memory.",
            tags=("pets", "preference", "pets"),
        )
        manager._retrieve_relevant_entries = MagicMock(return_value=[memory])
        components = manager.get_context_components(
            "user-3", "cats",
            {"persona_config": "Katherine", "user_profile": {}},
        )
        expected = (
            "User likes cats.\nThis is still the same memory."
            "\nTags: pets, preference"
        )
        assert components["memory_entries"] == [expected]
        assert components["memory_str"] == expected
        engine = _generation_engine()
        emotion, relationship = _neutral_states()
        messages = engine._build_generation_messages(
            emotion, components, relationship, "Hello", "",
        )
        validate_provider_input(messages)
        system_content = messages[0]["content"]
        assert "User likes cats.\nThis is still the same memory." in system_content
        assert "Tags: pets, preference" in system_content


# ═══════════════════════════════════════════════════════════════════════
# 14. Safety suffix: user-derived sections precede safety suffix
# ═══════════════════════════════════════════════════════════════════════

class TestSafetySuffixPrecedence:
    """Safety rules appear after all user-derived content."""

    def test_all_user_derived_sections_precede_safety_suffix(self):
        engine = _generation_engine()
        emotion, relationship = _neutral_states()
        safety_marker = "=== TRANSPARÊNCIA DE IDENTIDADE ==="
        messages = engine._build_generation_messages(
            emotion,
            {
                "persona": "PERSONA_SECTION_MARKER",
                "history_list": [
                    {"role": "user", "content": "HISTORY_PROMPT_INJECTION_MARKER"},
                ],
                "memory_str": "MEMORY_PROMPT_INJECTION_MARKER\nTags: injection, memory",
                "memory_entries": [
                    "MEMORY_PROMPT_INJECTION_MARKER\nTags: injection, memory",
                ],
                "user_profile_str": '{"injection":"PROFILE_PROMPT_INJECTION_MARKER"}',
            },
            relationship,
            "Current message",
            "",
        )
        system_content = messages[0]["content"]
        markers = {
            "persona": "PERSONA_SECTION_MARKER",
            "history": "HISTORY_PROMPT_INJECTION_MARKER",
            "memory": "MEMORY_PROMPT_INJECTION_MARKER",
            "profile": "PROFILE_PROMPT_INJECTION_MARKER",
        }
        safety_pos = system_content.find(safety_marker)
        assert safety_pos >= 0
        for name, marker in markers.items():
            pos = system_content.find(marker)
            assert pos >= 0, f"{name} marker not found"
            assert pos < safety_pos, f"{name} marker appears after safety suffix"
        after_safety = system_content[safety_pos:]
        for marker in markers.values():
            assert marker not in after_safety


# ═══════════════════════════════════════════════════════════════════════
# 15–18. Generation pruning observability
# ═══════════════════════════════════════════════════════════════════════

class TestGenerationPruningObservability:
    """Pruning events logged correctly, sanitized, and not duplicated."""

    def test_all_context_fits_without_prune_event(self, caplog):
        engine = _generation_engine()
        emotion, relationship = _neutral_states()
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="backend.engine"):
            messages = engine._build_generation_messages(
                emotion,
                {
                    "persona": "PERSONA_FITS",
                    "history_list": [
                        {"role": "user", "content": "HISTORY_FITS"},
                    ],
                    "memory_str": "MEMORY_FITS\nTags: small",
                    "memory_entries": ["MEMORY_FITS\nTags: small"],
                    "user_profile_str": '{"marker":"PROFILE_FITS"}',
                },
                relationship,
                "Hello",
                "",
            )
        system_content = messages[0]["content"]
        for marker in ("PERSONA_FITS", "HISTORY_FITS", "MEMORY_FITS", "PROFILE_FITS"):
            assert marker in system_content
        assert _pruned_messages(caplog) == []

    def test_partial_pruning_keeps_some_and_discards_others_once(self, caplog):
        engine = _generation_engine()
        emotion, relationship = _neutral_states()
        pruned_marker = "HISTORY_MUST_BE_PRUNED_" + ("x" * 20000)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="backend.engine"):
            messages = engine._build_generation_messages(
                emotion,
                {
                    "persona": "PERSONA_SURVIVES",
                    "history_list": [
                        {"role": "user", "content": "HISTORY_SURVIVES"},
                        {"role": "assistant", "content": pruned_marker},
                    ],
                    "memory_str": "MEMORY_SURVIVES\nTags: retained",
                    "memory_entries": [
                        "MEMORY_SURVIVES\nTags: retained",
                    ],
                    "user_profile_str": '{"marker":"PROFILE_SURVIVES"}',
                },
                relationship,
                "Hello",
                "",
            )
        system_content = messages[0]["content"]
        for marker in ("PERSONA_SURVIVES", "HISTORY_SURVIVES", "MEMORY_SURVIVES", "PROFILE_SURVIVES"):
            assert marker in system_content
        assert "HISTORY_MUST_BE_PRUNED_" not in system_content
        assert _pruned_messages(caplog) == [PRUNED_EVENT]

    def test_total_pruning_removes_every_optional_component_once(self, caplog):
        engine = _generation_engine()
        emotion, relationship = _neutral_states()
        huge = "x" * 20000
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="backend.engine"):
            messages = engine._build_generation_messages(
                emotion,
                {
                    "persona": "PERSONA_PRUNED_" + huge,
                    "history_list": [
                        {"role": "user", "content": "HISTORY_PRUNED_" + huge},
                    ],
                    "memory_str": "MEMORY_PRUNED_" + huge,
                    "memory_entries": ["MEMORY_PRUNED_" + huge],
                    "user_profile_str": '{"marker":"PROFILE_PRUNED_' + huge + '"}',
                },
                relationship,
                "Hello",
                "",
            )
        system_content = messages[0]["content"]
        for marker in ("PERSONA_PRUNED_", "HISTORY_PRUNED_", "MEMORY_PRUNED_", "PROFILE_PRUNED_"):
            assert marker not in system_content
        assert _pruned_messages(caplog) == [PRUNED_EVENT]

    def test_prune_log_is_sanitized(self, caplog):
        engine = _generation_engine()
        emotion, relationship = _neutral_states()
        sensitive = "SENSITIVE_USER_MARKER_87342"
        huge = sensitive + ("z" * 20000)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="backend.engine"):
            engine._build_generation_messages(
                emotion,
                {
                    "persona": "Katherine",
                    "history_list": [{"role": "user", "content": huge}],
                    "memory_str": huge,
                    "memory_entries": [huge],
                    "user_profile_str": '{"secret":"' + huge + '"}',
                },
                relationship,
                "Hello",
                "",
            )
        assert _pruned_messages(caplog) == [PRUNED_EVENT]
        assert sensitive not in caplog.text
        prune_record = next(
            record for record in caplog.records
            if record.getMessage() == PRUNED_EVENT
        )
        assert prune_record.getMessage() == PRUNED_EVENT


# ═══════════════════════════════════════════════════════════════════════
# 19–20. ContextFitResult contract
# ═══════════════════════════════════════════════════════════════════════

class TestContextFitContract:
    """ContextFitResult return type and error contracts."""

    def test_return_annotation_and_result_contract(self):
        assert get_type_hints(fit_optional_context)["return"] is ContextFitResult
        result = fit_optional_context(
            [
                {"role": "system", "content": "Header"},
                {"role": "user", "content": "Hello"},
            ],
            [("memory", "Memory")],
            max_units=16000,
        )
        assert isinstance(result, ContextFitResult)
        assert result.messages[0]["role"] == "system"
        assert result.selected_indices == frozenset({0})
        assert result.pruned is False

    def test_no_system_message_fails_closed_without_logging_content(self, caplog):
        sensitive = "NO_SYSTEM_SENSITIVE_MARKER"
        caplog.clear()
        with caplog.at_level(logging.INFO):
            with pytest.raises(ProviderEnvelopeError) as exc_info:
                fit_optional_context(
                    [{"role": "user", "content": "Hello"}],
                    [("sensitive-label", sensitive)],
                )
        assert exc_info.value.code == "no_system_message"
        assert sensitive not in caplog.text
        assert "sensitive-label" not in caplog.text


# ═══════════════════════════════════════════════════════════════════════
# 10b. Suffix preservation edge cases
# ═══════════════════════════════════════════════════════════════════════

class TestSuffixEdgeCases:
    """Edge cases for suffix preservation."""

    def test_empty_suffix_no_effect(self):
        mandatory = [
            {"role": "system", "content": "Header"},
            {"role": "user", "content": "Hi"},
        ]
        result = fit_optional_context(mandatory, [], suffix="")
        assert result.messages[0]["content"] == "Header"
        assert result.messages[1]["content"] == "Hi"

    def test_suffix_with_multibyte_characters(self):
        mandatory = [
            {"role": "system", "content": "HEADER"},
            {"role": "user", "content": "Hi"},
        ]
        suffix = "SAFETY: ñ 😀 café"
        result = fit_optional_context(mandatory, [], suffix=suffix)
        system_content = result.messages[0]["content"]
        assert "ñ" in system_content
        assert "😀" in system_content
        assert "café" in system_content
        assert system_content.count("ñ") == 1
        validate_provider_input(result.messages)

    def test_suffix_newline_delimiter_preserved(self):
        mandatory = [
            {"role": "system", "content": "HEADER"},
            {"role": "user", "content": "Hi"},
        ]
        result = fit_optional_context(mandatory, [], suffix="SUFFIX")
        assert "\n\nSUFFIX" in result.messages[0]["content"]
        assert result.messages[0]["content"].count("\n\nSUFFIX") == 1
