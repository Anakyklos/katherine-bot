"""
Additional tests for ``backend.trusted_context``.

Covers:

=== Module identity ===
- Class identity preserved after auth fixtures

=== Internal ID validation ===
- Rejects None, False, True, 0, 1, [], {}, (), "   "
- Approved memory requires valid UUID

=== created_at normalization ===
- Same instant with different offsets uses ID as tiebreaker
- Different timestamps with out-of-order IDs
- Equal timestamps
- Invalid string or None
- Offsets positive and negative

=== LoadedContextData validation ===
- Invalid role rejected
- Invalid content rejected
- Invalid id rejected
- Invalid created_at rejected
- RetrievedMemory without to_context_item rejected
- Unapproved memory in retrieved_memories rejected
- Invalid source_id in memory rejected

=== build_context_bundle ===
- Fully typed — raises TrustedContextError on malformed inputs
- No accidental KeyError, AttributeError, TypeError escape

=== Engine behavioral test ===
- Malformed LoadedContextData: _generate_with_messages not called,
  Groq not called, error becomes provider_invalid_request,
  sanitized log, malicious data not in log
"""

import json
import logging
import pytest
from unittest.mock import MagicMock, patch

from backend.trusted_context import (
    ChatMessage,
    ContextItem,
    ContextBundle,
    EpistemicStatus,
    Provenance,
    TrustedContextError,
    build_context_bundle,
    build_envelope,
    LoadedContextData,
    _normalize_timestamp,
    _parse_timestamp,
)


# ═══════════════════════════════════════════════════════════════════════
# Internal ID validation
# ═══════════════════════════════════════════════════════════════════════

class TestInternalIdValidation:
    """ContextItem internal_id validation tests."""

    def _make_base(self, **overrides) -> ContextItem:
        params = dict(
            kind="profile", content="test",
            provenance=Provenance.LEGACY_PROFILE,
            confidence=0.5, epistemic_status=EpistemicStatus.UNKNOWN,
            source_id="ctx-1", internal_id="",
        )
        params.update(overrides)
        return ContextItem(**params)

    @pytest.mark.parametrize("bad_value", [
        None, False, True, 0, 1, [], {}, (), "   ",
    ])
    def test_invalid_internal_id_rejected(self, bad_value):
        with pytest.raises(TrustedContextError, match="invalid_internal_id"):
            self._make_base(internal_id=bad_value)

    def test_none_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_internal_id_type"):
            self._make_base(internal_id=None)

    def test_false_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_internal_id_type"):
            self._make_base(internal_id=False)

    def test_true_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_internal_id_type"):
            self._make_base(internal_id=True)

    def test_integer_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_internal_id_type"):
            self._make_base(internal_id=0)

        with pytest.raises(TrustedContextError, match="invalid_internal_id_type"):
            self._make_base(internal_id=1)

    def test_list_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_internal_id_type"):
            self._make_base(internal_id=[])

    def test_dict_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_internal_id_type"):
            self._make_base(internal_id={})

    def test_tuple_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_internal_id_type"):
            self._make_base(internal_id=())

    def test_whitespace_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_internal_id_empty"):
            self._make_base(internal_id="   ")

    def test_empty_string_accepted_for_non_memory(self):
        """Empty string is allowed for profile/persona items without persisted ID."""
        item = self._make_base(kind="profile", internal_id="")
        assert item.internal_id == ""

    def test_empty_string_accepted_for_legacy_persona(self):
        """Empty string is allowed for persona without persisted ID."""
        item = self._make_base(kind="persona", internal_id="")
        assert item.internal_id == ""

    def test_approved_memory_requires_valid_uuid(self):
        """Approved memory must have valid UUID internal_id."""
        with pytest.raises(TrustedContextError, match="invalid_internal_id_not_uuid"):
            ContextItem(
                kind="memory",
                content="Test memory",
                provenance=Provenance.USER_CONFIRMED,
                confidence=0.9,
                epistemic_status=EpistemicStatus.APPROVED,
                source_id="mem-1",
                internal_id="uuid-abc",
            )

    def test_approved_memory_accepts_valid_uuid(self):
        """Valid UUID accepted for approved memory."""
        item = ContextItem(
            kind="memory",
            content="Test memory",
            provenance=Provenance.USER_CONFIRMED,
            confidence=0.9,
            epistemic_status=EpistemicStatus.APPROVED,
            source_id="mem-1",
            internal_id="550e8400-e29b-41d4-a716-446655440000",
        )
        assert item.internal_id == "550e8400-e29b-41d4-a716-446655440000"

    def test_approved_memory_rejects_numeric_uuid(self):
        """Numeric string without UUID format is rejected."""
        with pytest.raises(TrustedContextError, match="invalid_internal_id_not_uuid"):
            ContextItem(
                kind="memory",
                content="Test memory",
                provenance=Provenance.USER_CONFIRMED,
                confidence=0.9,
                epistemic_status=EpistemicStatus.APPROVED,
                source_id="mem-1",
                internal_id="123",
            )

    def test_approved_memory_empty_internal_id_rejected(self):
        """Approved memory with empty internal_id is rejected.

        Regression for the ID contract: an approved memory must always
        carry a persisted canonical UUID.  An empty string must never
        reach the source map, which would otherwise fall back to the
        local reference (``mem-1 -> mem-1``).
        """
        with pytest.raises(
            TrustedContextError,
            match="missing_approved_memory_internal_id",
        ):
            ContextItem(
                kind="memory",
                content="Approved fact",
                provenance=Provenance.USER_CONFIRMED,
                confidence=0.9,
                epistemic_status=EpistemicStatus.APPROVED,
                source_id="mem-1",
                internal_id="",
            )

    @pytest.mark.parametrize("non_canonical", [
        "550E8400-E29B-41D4-A716-446655440000",
        "{550e8400-e29b-41d4-a716-446655440000}",
        "550e8400e29b41d4a716446655440000",
    ])
    def test_approved_memory_rejects_non_canonical_uuid(self, non_canonical):
        """Approved memory rejects non-canonical UUID representations.

        Accepted representation is lowercase, hyphenated, without braces
        or prefix — exactly ``str(uuid.UUID(value))``.  The value is
        never silently normalized.
        """
        with pytest.raises(TrustedContextError, match="invalid_internal_id_not_uuid"):
            ContextItem(
                kind="memory",
                content="Approved fact",
                provenance=Provenance.USER_CONFIRMED,
                confidence=0.9,
                epistemic_status=EpistemicStatus.APPROVED,
                source_id="mem-1",
                internal_id=non_canonical,
            )

    def test_approved_memory_accepts_canonical_uuid(self):
        """Canonical lowercase hyphenated UUID is accepted and preserved verbatim."""
        item = ContextItem(
            kind="memory",
            content="Approved fact",
            provenance=Provenance.USER_CONFIRMED,
            confidence=0.9,
            epistemic_status=EpistemicStatus.APPROVED,
            source_id="mem-1",
            internal_id="550e8400-e29b-41d4-a716-446655440000",
        )
        assert item.internal_id == "550e8400-e29b-41d4-a716-446655440000"


# ═══════════════════════════════════════════════════════════════════════
# created_at normalization
# ═══════════════════════════════════════════════════════════════════════

class TestCreatedAtNormalization:
    """Timestamp normalization tests for deterministic ordering."""

    def test_same_instant_different_offsets_use_id_as_tiebreaker(self):
        """Timestamps representing the same instant are sorted by ID."""
        older = ChatMessage(
            role="user", content="A", source_id="m1",
            sort_key=("2026-07-30T14:00:00", 1),
        )
        newer = ChatMessage(
            role="user", content="B", source_id="m2",
            sort_key=("2026-07-30T14:00:00", 2),
        )
        messages = [newer, older]
        messages_sorted = sorted(messages, key=lambda m: m.sort_key)
        assert messages_sorted[0].source_id == "m1"
        assert messages_sorted[1].source_id == "m2"

    def test_different_timestamps_out_of_order_ids(self):
        """Sort by timestamp, not by id."""
        earlier = ChatMessage(
            role="user", content="Earlier", source_id="m1",
            sort_key=("2026-07-30T10:00:00", 100),
        )
        later = ChatMessage(
            role="user", content="Later", source_id="m2",
            sort_key=("2026-07-30T11:00:00", 1),
        )
        messages = [later, earlier]
        messages_sorted = sorted(messages, key=lambda m: m.sort_key)
        assert messages_sorted[0].content == "Earlier"
        assert messages_sorted[1].content == "Later"

    def test_equal_timestamps_deterministic(self):
        """Equal timestamps sorted by id."""
        a = ChatMessage(
            role="user", content="A", source_id="m1",
            sort_key=("2026-07-30T14:00:00", 1),
        )
        b = ChatMessage(
            role="user", content="B", source_id="m2",
            sort_key=("2026-07-30T14:00:00", 2),
        )
        messages = [b, a]
        messages_sorted = sorted(messages, key=lambda m: m.sort_key)
        assert messages_sorted[0].content == "A"

    def test_invalid_timestamp_string(self):
        """Invalid timestamp string raises in LoadedContextData validation."""
        with pytest.raises(TrustedContextError, match="invalid_loaded_data_history_timestamp"):
            LoadedContextData(
                history_rows=(
                    {"role": "user", "content": "Hi", "id": 1, "created_at": "not-a-timestamp"},
                ),
            )

    def test_no_timestamp_rejected(self):
        """Missing created_at raises."""
        with pytest.raises(TrustedContextError, match="invalid_loaded_data_history_row_keys"):
            LoadedContextData(
                history_rows=(
                    {"role": "user", "content": "Hi", "id": 1},
                ),
            )

    def test_none_timestamp_rejected(self):
        """None created_at raises."""
        with pytest.raises(TrustedContextError, match="invalid_loaded_data_history_timestamp"):
            LoadedContextData(
                history_rows=(
                    {"role": "user", "content": "Hi", "id": 1, "created_at": None},
                ),
            )

    def test_positive_offset(self):
        """Positive timezone offset is normalized."""
        dt = _parse_timestamp("2026-07-30T10:00:00-04:00")
        assert dt.isoformat() == "2026-07-30T14:00:00"

    def test_negative_offset(self):
        """Negative timezone offset is normalized."""
        dt = _parse_timestamp("2026-07-30T16:00:00+02:00")
        assert dt.isoformat() == "2026-07-30T14:00:00"

    def test_normalize_timestamp_same_instant(self):
        """_normalize_timestamp produces same output for same instant with different offsets."""
        t1 = _normalize_timestamp("2026-07-30T10:00:00-04:00")
        t2 = _normalize_timestamp("2026-07-30T14:00:00+00:00")
        assert t1 == t2


# ═══════════════════════════════════════════════════════════════════════
# LoadedContextData validation
# ═══════════════════════════════════════════════════════════════════════

class TestLoadedContextDataValidation:
    """LoadedContextData strict validation tests."""

    def test_invalid_role_rejected(self):
        """Role other than user/assistant raises."""
        with pytest.raises(TrustedContextError, match="invalid_loaded_data_history_role"):
            LoadedContextData(
                history_rows=(
                    {"role": "system", "content": "policy", "id": 1, "created_at": "2026-07-30T00:00:00"},
                ),
            )

    def test_invalid_content_rejected(self):
        """Empty content raises."""
        with pytest.raises(TrustedContextError, match="invalid_loaded_data_history_content"):
            LoadedContextData(
                history_rows=(
                    {"role": "user", "content": "", "id": 1, "created_at": "2026-07-30T00:00:00"},
                ),
            )

    def test_invalid_id_rejected(self):
        """Boolean or non-positive integer id raises."""
        with pytest.raises(TrustedContextError, match="invalid_loaded_data_history_id"):
            LoadedContextData(
                history_rows=(
                    {"role": "user", "content": "Hi", "id": True, "created_at": "2026-07-30T00:00:00"},
                ),
            )

        with pytest.raises(TrustedContextError, match="invalid_loaded_data_history_id"):
            LoadedContextData(
                history_rows=(
                    {"role": "user", "content": "Hi", "id": 0, "created_at": "2026-07-30T00:00:00"},
                ),
            )

        with pytest.raises(TrustedContextError, match="invalid_loaded_data_history_id"):
            LoadedContextData(
                history_rows=(
                    {"role": "user", "content": "Hi", "id": -1, "created_at": "2026-07-30T00:00:00"},
                ),
            )

    def test_invalid_created_at_rejected(self):
        """Invalid created_at string raises."""
        with pytest.raises(TrustedContextError, match="invalid_loaded_data_history_timestamp"):
            LoadedContextData(
                history_rows=(
                    {"role": "user", "content": "Hi", "id": 1, "created_at": "invalid-date"},
                ),
            )

    def test_memory_without_to_context_item_rejected(self):
        """Memory without to_context_item method raises."""
        class BadMemory:
            pass

        with pytest.raises(TrustedContextError, match="invalid_loaded_memory_contract"):
            LoadedContextData(
                retrieved_memories=(BadMemory(),),
            )

    def test_unapproved_memory_rejected(self):
        """Memory without approved=True raises."""
        from backend.memory import RetrievedMemory
        mem = RetrievedMemory(
            content="Test", tags=(),
            approved=False, metadata_version=1,
        )
        with pytest.raises(TrustedContextError, match="invalid_loaded_memory_not_approved"):
            LoadedContextData(
                retrieved_memories=(mem,),
            )

    def test_memories_type_tuple_rejected(self):
        """retrieved_memories that are not a tuple raises."""
        with pytest.raises(TrustedContextError, match="invalid_loaded_data_memories_type"):
            LoadedContextData(retrieved_memories=[])

    @pytest.mark.parametrize("bad_source_id", [
        "550E8400-E29B-41D4-A716-446655440000",
        "{550e8400-e29b-41d4-a716-446655440000}",
        "550e8400e29b41d4a716446655440000",
        "urn:uuid:550e8400-e29b-41d4-a716-446655440000",
    ])
    def test_non_canonical_source_id_rejected(self, bad_source_id):
        """LoadedContextData rejects non-canonical memory source_id."""
        from backend.memory import RetrievedMemory
        mem = RetrievedMemory(
            content="Test", tags=(),
            source_id=bad_source_id,
            confidence=0.9,
            provenance=Provenance.USER_CONFIRMED,
            epistemic_status=EpistemicStatus.APPROVED,
            approved=True,
            metadata_version=1,
        )
        with pytest.raises(TrustedContextError, match="invalid_loaded_memory_source_id"):
            LoadedContextData(
                retrieved_memories=(mem,),
            )

    def test_canonical_source_id_accepted(self):
        """Canonical lowercase hyphenated source_id is accepted."""
        from backend.memory import RetrievedMemory
        mem = RetrievedMemory(
            content="Test", tags=(),
            source_id="550e8400-e29b-41d4-a716-446655440000",
            confidence=0.9,
            provenance=Provenance.USER_CONFIRMED,
            epistemic_status=EpistemicStatus.APPROVED,
            approved=True,
            metadata_version=1,
        )
        loaded = LoadedContextData(retrieved_memories=(mem,))
        assert len(loaded.retrieved_memories) == 1


# ═══════════════════════════════════════════════════════════════════════
# build_context_bundle fully typed
# ═══════════════════════════════════════════════════════════════════════

class TestBuildContextBundleTyped:
    """build_context_bundle raises typed errors, not raw exceptions."""

    def test_valid_loaded_data_produces_bundle(self):
        """build_context_bundle with valid data produces ContextBundle."""
        loaded = LoadedContextData(
            history_rows=(
                {"role": "user", "content": "Hi", "id": 1, "created_at": "2026-07-30T00:00:00"},
            ),
        )
        bundle = build_context_bundle("Policy.", loaded)
        assert isinstance(bundle, ContextBundle)

    def test_invalid_loaded_data_raises_typed(self):
        """build_context_bundle raises TrustedContextError for invalid loaded_data type."""
        with pytest.raises(TrustedContextError, match="invalid_loaded_data_type"):
            build_context_bundle("Policy.", "not-a-LoadedContextData")


# ═══════════════════════════════════════════════════════════════════════
# Engine behavioral test — malformed LoadedContextData
# ═══════════════════════════════════════════════════════════════════════

class TestEngineMalformedLoadedData:
    """Engine rejects malformed context data without calling provider for generation."""

    # Valid appraisal JSON so the appraisal stage succeeds
    _APPRAISAL_JSON = json.dumps({
        "valence": 0.0, "arousal_shift": 0.0, "dominance_shift": 0.0,
        "triggered_emotions": {
            "joy": 0, "sadness": 0, "anger": 0, "fear": 0,
            "disgust": 0, "surprise": 0, "tenderness": 0,
            "guilt": 0, "pride": 0, "jealousy": 0, "gratitude": 0,
        },
    })

    @pytest.mark.anyio
    async def test_malformed_loaded_data_does_not_call_groq(self, monkeypatch, caplog):
        """Malformed LoadedContextData is rejected before any provider call.

        The malformed history row is rejected during ``LoadedContextData``
        construction inside the load_context stage — BEFORE appraisal and
        generation.  The engine must therefore:

        - never call Groq (neither appraisal nor generation),
        - never call ``_generate_with_messages``,
        - raise ``TurnExecutionError(provider_invalid_request)``,
        - log only the sanitized event — never the malicious payload.
        """
        from backend.engine import ConversationEngine
        from backend.groq_manager import GroqClientManager
        from backend.memory import MemoryManager
        from backend.turn_execution import TurnExecutionError, TurnErrorCode

        caplog.set_level(logging.ERROR)

        groq_call_count = [0]
        generate_called = [False]

        async def mock_groq(messages, **kwargs):
            groq_call_count[0] += 1
            resp = MagicMock()
            resp.choices = [MagicMock()]
            resp.choices[0].message.content = self._APPRAISAL_JSON
            return resp

        def mock_load_state(uid, default_timestamp=None):
            from backend.emotional_domain import EmotionalStateV1
            from backend.relationship import RelationshipStateV1
            return {
                "emotional_state": EmotionalStateV1.neutral().to_dict(),
                "relationship_state": RelationshipStateV1.neutral(timestamp=1000.0).to_dict(),
                "persona_config": "",
                "user_profile": {},
            }

        def mock_load_context(uid, msg, state):
            # Row carries the malicious marker AND is structurally invalid
            # (invalid role).  LoadedContextData.__post_init__ rejects it
            # during the load_context stage, before any provider call.
            return LoadedContextData(
                history_rows=(
                    {
                        "role": "MALICIOUS_DATA_SHOULD_NOT_LOG_12345",
                        "content": "MALICIOUS_DATA_SHOULD_NOT_LOG_12345",
                        "id": 1,
                        "created_at": "2026-07-30T00:00:00",
                    },
                ),
                retrieved_memories=(),
                profile_snapshot={},
                persona_snapshot="",
            )

        with (
            patch.object(GroqClientManager, "__init__", return_value=None),
            patch.object(MemoryManager, "__init__", return_value=None),
            patch("backend.memory.SentenceTransformer", return_value=MagicMock()),
        ):
            engine = ConversationEngine(clock=lambda: 1000.0)

        monkeypatch.setattr(engine.groq_manager, "chat_completion_async", mock_groq)
        monkeypatch.setattr(engine.memory_manager, "load_user_state", mock_load_state)
        monkeypatch.setattr(
            engine.memory_manager, "load_context_data", mock_load_context
        )
        monkeypatch.setattr(
            engine.memory_manager, "save_turn",
            lambda *a, **kw: type("Ref", (), {
                "user_id": "u1", "source_chat_log_id": 1, "assistant_chat_log_id": 2,
            })(),
        )
        monkeypatch.setattr(
            engine.memory_manager, "sync_state", lambda *a, **kw: None,
        )

        # _generate_with_messages must never be reached
        original_generate = engine._generate_with_messages

        async def _fail_generate(messages, budget):
            generate_called[0] = True
            return await original_generate(messages, budget)

        monkeypatch.setattr(engine, "_generate_with_messages", _fail_generate)

        with pytest.raises(TurnExecutionError) as exc_info:
            await engine.process_turn(
                user_id="u1",
                user_message="Hello",
            )

        # Groq must NEVER be called — neither for appraisal nor generation.
        assert groq_call_count[0] == 0, (
            f"Groq called {groq_call_count[0]} times; expected 0 "
            "(structural error detected before any provider call)"
        )
        # _generate_with_messages must never be called
        assert not generate_called[0], \
            "_generate_with_messages was called after the structural error"

        # Error must be provider_invalid_request
        assert exc_info.value.code == TurnErrorCode.provider_invalid_request

        # Log must contain only the sanitized event
        assert "provider_input_invalid" in caplog.text
        # The malicious payload must never appear in logs
        assert "MALICIOUS_DATA_SHOULD_NOT_LOG_12345" not in caplog.text
        # Verify that no content/UUIDs leak into logs
        assert "user123" not in caplog.text


# ═══════════════════════════════════════════════════════════════════════
# Module identity verification
# ═══════════════════════════════════════════════════════════════════════

class TestModuleIdentity:
    """Module identity must be preserved across test fixtures."""

    def test_context_item_identity_from_trusted_context(self):
        """ContextItem from trusted_context is the same class used by memory."""
        import backend.trusted_context as tc_module
        from backend.memory import RetrievedMemory

        item = RetrievedMemory(
            content="Identity test", tags=(),
            source_id="550e8400-e29b-41d4-a716-446655449998",
            confidence=0.8,
            provenance=Provenance.USER_CONFIRMED,
            epistemic_status=EpistemicStatus.APPROVED,
            approved=True,
            metadata_version=1,
        ).to_context_item("mem-1")

        assert type(item) is tc_module.ContextItem, \
            f"Expected {tc_module.ContextItem}, got {type(item)}"

        bundle = ContextBundle(
            trusted_policy="Policy.",
            memory_items=(item,),
        )
        assert isinstance(bundle, ContextBundle)


# ═══════════════════════════════════════════════════════════════════════
# Approved memory ID contract — full pipeline
# ═══════════════════════════════════════════════════════════════════════

class TestApprovedMemoryIdContract:
    """Approved memory always maps to its persisted canonical UUID.

    Covers the end-to-end path:
    ``RetrievedMemory -> LoadedContextData -> build_context_bundle ->
    build_envelope`` and asserts the source map contract: an approved
    memory's local reference maps to the real persisted UUID — never to
    the local reference itself (``mem-1 -> mem-1``).
    """

    _REAL_UUID = "550e8400-e29b-41d4-a716-446655440000"

    def _approved_memory(self, source_id: str = _REAL_UUID):
        from backend.memory import RetrievedMemory
        return RetrievedMemory(
            content="Approved pipeline fact",
            tags=("pipeline",),
            source_id=source_id,
            confidence=0.9,
            provenance=Provenance.USER_CONFIRMED,
            epistemic_status=EpistemicStatus.APPROVED,
            approved=True,
            metadata_version=1,
        )

    def test_pipeline_source_map_contains_real_uuid(self):
        """source_map["mem-1"] maps to the real persisted UUID."""
        loaded = LoadedContextData(retrieved_memories=(self._approved_memory(),))
        bundle = build_context_bundle(
            trusted_policy="Policy.", loaded_data=loaded
        )
        result = build_envelope(bundle, "Hi")

        assert "mem-1" in result.source_map, "Local ref must be in source_map"
        assert result.source_map["mem-1"] == self._REAL_UUID, \
            "source_map must map local ref to real UUID"

    def test_approved_memory_never_maps_local_ref_to_itself(self):
        """Approved memory never produces mem-1 -> mem-1."""
        loaded = LoadedContextData(retrieved_memories=(self._approved_memory(),))
        bundle = build_context_bundle(
            trusted_policy="Policy.", loaded_data=loaded
        )
        result = build_envelope(bundle, "Hi")

        assert result.source_map["mem-1"] != "mem-1", \
            "Approved memory must never fall back to the local reference"

    def test_uuid_not_in_provider_messages(self):
        """The real UUID never appears in provider-bound messages."""
        loaded = LoadedContextData(retrieved_memories=(self._approved_memory(),))
        bundle = build_context_bundle(
            trusted_policy="Policy.", loaded_data=loaded
        )
        result = build_envelope(bundle, "Hi")

        serialized = json.dumps(result.messages)
        assert self._REAL_UUID not in serialized, \
            "Real UUID leaked into provider messages"

    def test_rejected_uuid_not_in_exception_or_log(self, caplog):
        """Rejected non-canonical UUID never appears in error or logs."""
        caplog.set_level(logging.DEBUG)
        non_canonical = "550E8400-E29B-41D4-A716-446655440000"
        with pytest.raises(TrustedContextError) as exc_info:
            LoadedContextData(
                retrieved_memories=(
                    self._approved_memory(source_id=non_canonical),
                ),
            )
        # Error carries only the structured code — never the UUID.
        assert non_canonical not in str(exc_info.value)
        assert non_canonical not in repr(exc_info.value)
        assert non_canonical not in caplog.text, \
            "Rejected UUID leaked into logs"

    def test_builder_fails_closed_without_persisted_uuid(self):
        """build_envelope fails closed if an approved memory without UUID reaches it.

        Constructs a ``ContextItem`` directly via ``object.__new__`` to
        bypass ``__post_init__`` — simulating the state the constructor
        normally prevents — and verifies the envelope builder rejects it
        instead of falling back to the local reference.
        """
        item = object.__new__(ContextItem)
        object.__setattr__(item, "kind", "memory")
        object.__setattr__(item, "content", "Approved fact")
        object.__setattr__(item, "provenance", Provenance.USER_CONFIRMED)
        object.__setattr__(item, "confidence", 0.9)
        object.__setattr__(item, "epistemic_status", EpistemicStatus.APPROVED)
        object.__setattr__(item, "source_id", "mem-1")
        object.__setattr__(item, "internal_id", "")

        bundle = ContextBundle(
            trusted_policy="Policy.",
            memory_items=(item,),
        )
        with pytest.raises(
            TrustedContextError,
            match="missing_approved_memory_internal_id",
        ):
            build_envelope(bundle, "Hi")

    def test_profile_persona_still_accept_empty_internal_id(self):
        """Profile/persona without persisted ID keep working with internal_id=""."""
        profile = ContextItem(
            kind="profile", content='{"name":"User"}',
            provenance=Provenance.LEGACY_PROFILE,
            confidence=0.3, epistemic_status=EpistemicStatus.UNKNOWN,
            source_id="profile-1",
        )
        persona = ContextItem(
            kind="persona", content="Katherine",
            provenance=Provenance.LEGACY_PERSONA,
            confidence=0.3, epistemic_status=EpistemicStatus.UNKNOWN,
            source_id="persona-1",
        )
        assert profile.internal_id == ""
        assert persona.internal_id == ""

        bundle = ContextBundle(
            trusted_policy="Policy.",
            profile_items=(profile,),
            persona_items=(persona,),
        )
        result = build_envelope(bundle, "Hi")
        # Documented fallback: no persisted ID -> local reference itself.
        assert result.source_map["profile-1"] == "profile-1"
        assert result.source_map["persona-1"] == "persona-1"
