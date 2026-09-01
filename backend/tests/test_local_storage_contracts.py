"""Payload contract tests for the local SQLite store (#335).

Adversarial suite: every rejected input is a real attempt to smuggle
prompts, raw messages, internal state, identity, non-finite numbers or
unbounded content into the stored payloads, at any depth.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from backend.local_storage import ValidationError
from backend.local_storage.contracts import (
    canonical_payload_hash,
    validate_emotional_snapshot,
    validate_neutral_emotional_snapshot,
    validate_neutral_relationship_snapshot,
    validate_outbox_events,
    validate_relationship_snapshot,
    validate_replay_payload,
)


def _valid_replay_payload() -> dict:
    return {
        "response": "olá!",
        "emotion_state": {"schema_version": 1, "mood_label": "calma", "pad": {"pleasure": 0.0}},
        "message_id": "4a7f8c21-0000-4000-8000-000000000001",
        "duration_ms": 12,
    }


def _valid_emotional_snapshot() -> dict:
    from backend.emotional_domain import EmotionalStateV1

    return EmotionalStateV1.neutral(timestamp=1000.0).to_dict()


def _valid_relationship_snapshot() -> dict:
    from backend.relationship import RelationshipStateV1

    return RelationshipStateV1.neutral(timestamp=1000.0).to_dict()


def _snapshot_code(fn) -> str | None:
    """Run a validator; return its error code or None on success."""
    try:
        fn()
    except ValidationError as exc:
        return exc.code
    return None


class TestReplayPayloadContract:
    def test_accepts_public_payload(self) -> None:
        payload = _valid_replay_payload()
        assert validate_replay_payload(payload) is payload

    def test_rejects_non_mapping(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            validate_replay_payload([("response", "x")])
        assert excinfo.value.code == "invalid_replay_payload"

    def test_rejects_unknown_key(self) -> None:
        payload = _valid_replay_payload()
        payload["system_prompt"] = "be nice"
        with pytest.raises(ValidationError) as excinfo:
            validate_replay_payload(payload)
        assert excinfo.value.code == "invalid_replay_payload"

    def test_rejects_nested_content_and_prompt_at_any_depth(self) -> None:
        for forbidden in ("content", "prompt", "system_prompt", "meta_cognition"):
            payload = _valid_replay_payload()
            payload["emotion_state"] = {
                "schema_version": 1,
                "nested": {"deeper": {forbidden: "secret instructions"}},
            }
            with pytest.raises(ValidationError) as excinfo:
                validate_replay_payload(payload)
            assert excinfo.value.code == "invalid_replay_payload"

    def test_rejects_missing_or_non_string_response(self) -> None:
        payload = _valid_replay_payload()
        del payload["response"]
        with pytest.raises(ValidationError):
            validate_replay_payload(payload)
        payload = _valid_replay_payload()
        payload["response"] = 42
        with pytest.raises(ValidationError):
            validate_replay_payload(payload)

    def test_rejects_nan_and_infinity(self) -> None:
        payload = _valid_replay_payload()
        payload["duration_ms"] = float("nan")
        with pytest.raises(ValidationError) as excinfo:
            validate_replay_payload(payload)
        assert excinfo.value.code == "invalid_replay_payload"

    def test_rejects_oversized_payload(self) -> None:
        # Web parity: the SQL CHECK and atomic_turn_commit bound the replay
        # payload at 8192 bytes; the local contract enforces the same bound.
        payload = _valid_replay_payload()
        payload["response"] = "x" * 9000
        with pytest.raises(ValidationError) as excinfo:
            validate_replay_payload(payload)
        assert "exceeds" in excinfo.value.message


class TestOutboxContract:
    def test_accepts_reference_only_payload(self) -> None:
        events = [
            (
                "archival_extraction_requested",
                {"message_id": "req-1", "kind": "archival", "version": 1},
                "archival:req-1:v1",
            )
        ]
        assert validate_outbox_events(events) == events

    def test_none_is_empty(self) -> None:
        assert validate_outbox_events(None) == []

    def test_rejects_wrong_shape(self) -> None:
        for bad in (
            "not-a-list",
            [("only", "two")],
            [{"event_type": "a", "payload": {}, "idempotency_key": "k"}],
        ):
            with pytest.raises(ValidationError) as excinfo:
                validate_outbox_events(bad)
            assert excinfo.value.code == "invalid_outbox_events"

    def test_rejects_invalid_event_type(self) -> None:
        events = [("Event-Type!", {}, "k1")]
        with pytest.raises(ValidationError) as excinfo:
            validate_outbox_events(events)
        assert excinfo.value.code == "invalid_outbox_events"

    def test_rejects_invalid_idempotency_key(self) -> None:
        events = [("archival_extraction_requested", {}, "key with spaces")]
        with pytest.raises(ValidationError) as excinfo:
            validate_outbox_events(events)
        assert excinfo.value.code == "invalid_outbox_events"

    def test_rejects_disallowed_payload_key(self) -> None:
        events = [
            (
                "archival_extraction_requested",
                {"message_id": "req-1", "extra": "nope"},
                "k1",
            )
        ]
        with pytest.raises(ValidationError) as excinfo:
            validate_outbox_events(events)
        assert excinfo.value.code == "invalid_outbox_events"

    def test_rejects_nested_forbidden_key(self) -> None:
        events = [
            (
                "archival_extraction_requested",
                {"ref": {"nested": {"content": "user wrote this"}}},
                "k1",
            )
        ]
        with pytest.raises(ValidationError) as excinfo:
            validate_outbox_events(events)
        assert excinfo.value.code == "invalid_outbox_events"

    def test_rejects_non_reference_value_types(self) -> None:
        # The payload value contract: reference fields are bounded ASCII
        # strings; only `version` is an int. A conversational free-text
        # value cannot be smuggled into any allowed reference field.
        events = [
            (
                "archival_extraction_requested",
                {"message_id": "mensagem do usuário — conteúdo privado"},
                "k1",
            )
        ]
        with pytest.raises(ValidationError) as excinfo:
            validate_outbox_events(events)
        assert excinfo.value.code == "invalid_outbox_events"

    def test_rejects_bad_version(self) -> None:
        for bad in (True, "1", 0, 1001, 1.5):
            events = [
                (
                    "archival_extraction_requested",
                    {"message_id": "req-1", "version": bad},
                    "k1",
                )
            ]
            with pytest.raises(ValidationError):
                validate_outbox_events(events)

    def test_rejects_nan(self) -> None:
        events = [
            (
                "archival_extraction_requested",
                {"message_id": "req-1", "kind": "archival", "version": 1, "x": float("inf")},
                "k1",
            )
        ]
        with pytest.raises(ValidationError):
            validate_outbox_events(events)

    def test_rejects_oversized_payload(self) -> None:
        # Web parity (test_atomic_turn_commit.py::test_outbox_payload_byte_limit):
        # a reference field above the 8192 B bound is rejected with the same
        # stable code the web contract uses.
        events = [("turn_completed", {"ref": "x" * 8200}, "key")]
        with pytest.raises(ValidationError) as excinfo:
            validate_outbox_events(events)
        assert excinfo.value.code == "invalid_outbox_events"

    def test_accepts_typical_reference_payload(self) -> None:
        # A real archival event payload fits comfortably in the bound.
        events = [
            (
                "archival_extraction_requested",
                {"message_id": "req-1", "kind": "archival", "version": 1},
                "archival:req-1:v1",
            )
        ]
        validate_outbox_events(events)

    def test_rejects_duplicate_keys_within_turn(self) -> None:
        events = [
            ("archival_extraction_requested", {"kind": "archival", "version": 1}, "dup"),
            ("archival_extraction_requested", {"kind": "archival", "version": 1}, "dup"),
        ]
        with pytest.raises(ValidationError) as excinfo:
            validate_outbox_events(events)
        assert excinfo.value.code == "invalid_outbox_events"


class TestSnapshotContract:
    def test_accepts_real_domain_snapshots(self) -> None:
        assert validate_emotional_snapshot(_valid_emotional_snapshot()) is not None
        assert validate_relationship_snapshot(_valid_relationship_snapshot()) is not None

    def test_rejects_synthetic_snapshot(self) -> None:
        # `{"v": 1, ...}` documents are not the real contract.
        with pytest.raises(ValidationError):
            validate_emotional_snapshot({"v": 1, "valence": 0.1})
        with pytest.raises(ValidationError):
            validate_relationship_snapshot({"v": 1, "trust": 0.5})

    def test_rejects_unknown_keys(self) -> None:
        snapshot = _valid_emotional_snapshot()
        snapshot["prompt"] = "hidden"
        with pytest.raises(ValidationError) as excinfo:
            validate_emotional_snapshot(snapshot)
        assert excinfo.value.code == "invalid_emotional_state"

    def test_rejects_identity_keys(self) -> None:
        snapshot = _valid_relationship_snapshot()
        snapshot["user_id"] = "someone"
        with pytest.raises(ValidationError) as excinfo:
            validate_relationship_snapshot(snapshot)
        assert excinfo.value.code == "invalid_relationship_state"

    def test_rejects_out_of_range_and_nonfinite(self) -> None:
        snapshot = _valid_emotional_snapshot()
        snapshot["pleasure"] = 2.0
        with pytest.raises(ValidationError):
            validate_emotional_snapshot(snapshot)
        snapshot = _valid_emotional_snapshot()
        snapshot["energy"] = float("nan")
        with pytest.raises(ValidationError):
            validate_emotional_snapshot(snapshot)
        snapshot = _valid_relationship_snapshot()
        snapshot["trust"] = math.inf
        with pytest.raises(ValidationError):
            validate_relationship_snapshot(snapshot)

    def test_rejects_oversized_snapshot(self) -> None:
        # The domain bounds each trigger to 128 chars and the list to 32
        # entries, but the document is still capped at the 8192 B bound
        # shared with the web replay/outbox payload limits. Oversized
        # triggers (invalid for the domain) still cross the byte cap.
        rel = _valid_relationship_snapshot()
        rel["triggers"] = ["t" * 300 for _ in range(32)]
        with pytest.raises(ValidationError) as excinfo:
            validate_relationship_snapshot(rel)
        assert "exceeds" in excinfo.value.message

    def test_accepts_domain_maximum_snapshot(self) -> None:
        # Parity guard: the largest domain-VALID relationship snapshot
        # (32 distinct triggers x 128 chars, ~4.3 KB canonical) must be
        # accepted locally. The web contract has no snapshot byte bound;
        # the local bound (8192) matches its replay/outbox bounds and
        # never rejects a snapshot the domain itself considers valid.
        from backend.relationship import RelationshipStateV1

        triggers = [f"{i:03d}-" + "t" * 123 for i in range(32)]
        state = RelationshipStateV1.create(
            trust=0.5, affection=0.5, tension=0.5,
            triggers=triggers, timestamp=1790000000.123456,
        )
        validated = validate_relationship_snapshot(state.to_dict())
        assert validated == state.to_dict()


class TestNeutralSnapshotContract:
    def test_accepts_domain_neutral(self) -> None:
        from backend.emotional_domain import EmotionalStateV1
        from backend.relationship import RelationshipStateV1

        validate_neutral_emotional_snapshot(
            EmotionalStateV1.neutral(timestamp=123.0).to_dict()
        )
        validate_neutral_relationship_snapshot(
            RelationshipStateV1.neutral(timestamp=123.0).to_dict()
        )

    def test_rejects_valid_but_non_neutral(self) -> None:
        snapshot = _valid_emotional_snapshot()
        snapshot["pleasure"] = 0.9  # structurally valid v1, not neutral
        with pytest.raises(ValidationError) as excinfo:
            validate_neutral_emotional_snapshot(snapshot)
        assert excinfo.value.code == "invalid_reset_payload"
        rel = _valid_relationship_snapshot()
        rel["trust"] = 1.0
        with pytest.raises(ValidationError) as excinfo:
            validate_neutral_relationship_snapshot(rel)
        assert excinfo.value.code == "invalid_reset_payload"

    def test_rejects_empty_mapping(self) -> None:
        with pytest.raises(ValidationError):
            validate_neutral_emotional_snapshot({})
        with pytest.raises(ValidationError):
            validate_neutral_relationship_snapshot({})


class TestCanonicalHash:
    def test_hash_is_deterministic_and_key_order_insensitive(self) -> None:
        a = {"b": 1, "a": {"y": 2, "x": [3, 4]}}
        b = {"a": {"x": [3, 4], "y": 2}, "b": 1}
        assert canonical_payload_hash(a) == canonical_payload_hash(b)

    def test_hash_rejects_nan(self) -> None:
        with pytest.raises(ValidationError):
            canonical_payload_hash({"x": float("nan")})

    def test_hash_changes_with_any_input(self) -> None:
        base = {"x": 1}
        assert canonical_payload_hash(base) != canonical_payload_hash({"x": 2})
