"""Final contract tests for provider-envelope issue #294.

These tests exercise the real memory retrieval path and the generation caller,
closing gaps that cannot be proven by isolated formatting or pruning tests.
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


def _memory_manager_with_rpc_documents(documents: list[object]) -> MemoryManager:
    """Build a MemoryManager double that exercises the real retrieval method."""
    manager = MemoryManager.__new__(MemoryManager)

    encoded = MagicMock()
    encoded.tolist.return_value = [0.1, 0.2, 0.3]
    manager.embedding_model = MagicMock()
    manager.embedding_model.encode.return_value = encoded

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


class TestRetrievedMemoryRealPath:
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
            "user-3",
            "cats",
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
            emotion,
            components,
            relationship,
            "Hello",
            "",
        )
        validate_provider_input(messages)
        system_content = messages[0]["content"]
        assert "User likes cats.\nThis is still the same memory." in system_content
        assert "Tags: pets, preference" in system_content


class TestSafetySuffixPrecedence:
    def test_all_user_derived_sections_precede_safety_suffix(self):
        engine = _generation_engine()
        emotion, relationship = _neutral_states()

        persona_marker = "PERSONA_SECTION_MARKER"
        history_marker = "HISTORY_PROMPT_INJECTION_MARKER"
        memory_marker = "MEMORY_PROMPT_INJECTION_MARKER"
        profile_marker = "PROFILE_PROMPT_INJECTION_MARKER"
        safety_marker = "=== TRANSPARÊNCIA DE IDENTIDADE ==="

        messages = engine._build_generation_messages(
            emotion,
            {
                "persona": persona_marker,
                "history_list": [
                    {"role": "user", "content": history_marker},
                ],
                "memory_str": f"{memory_marker}\nTags: injection, memory",
                "memory_entries": [
                    f"{memory_marker}\nTags: injection, memory",
                ],
                "user_profile_str": (
                    '{"injection":"' + profile_marker + '"}'
                ),
            },
            relationship,
            "Current message",
            "",
        )

        system_content = messages[0]["content"]
        persona_pos = system_content.find(persona_marker)
        history_pos = system_content.find(history_marker)
        memory_pos = system_content.find(memory_marker)
        profile_pos = system_content.find(profile_marker)
        safety_pos = system_content.find(safety_marker)

        assert safety_pos >= 0
        for position in (persona_pos, history_pos, memory_pos, profile_pos):
            assert position >= 0
            assert position < safety_pos

        after_safety = system_content[safety_pos:]
        for marker in (
            persona_marker,
            history_marker,
            memory_marker,
            profile_marker,
        ):
            assert marker not in after_safety


class TestGenerationPruningObservability:
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
        for marker in (
            "PERSONA_FITS",
            "HISTORY_FITS",
            "MEMORY_FITS",
            "PROFILE_FITS",
        ):
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
        for marker in (
            "PERSONA_SURVIVES",
            "HISTORY_SURVIVES",
            "MEMORY_SURVIVES",
            "PROFILE_SURVIVES",
        ):
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
        for marker in (
            "PERSONA_PRUNED_",
            "HISTORY_PRUNED_",
            "MEMORY_PRUNED_",
            "PROFILE_PRUNED_",
        ):
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


class TestContextFitContract:
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
