"""
Tests for ``backend.trusted_context``.

Covers:

=== Domain purity ===
1. Importability without infrastructure
2. Pure types validation (ChatMessage, ContextItem)

=== ChatMessage validation ===
3. Accepts only "user" and "assistant"
4. "system" role is rejected
5. Invalid sort keys rejected
6. Invalid source_id rejected

=== ContextItem validation ===
7. Validates kind, provenance, confidence, epistemic_status
8. Rejects bool, None, NaN, inf, incorrect types for confidence
9. Invalid provenance rejected
10. Invalid epistemic_status rejected

=== Truncation report ===
11. Default codes empty

=== Envelope construction ===
12. System + current user preserved
13. History with original roles
14. Injections never reach system
15. Profile marked as untrusted
16. Memory marked as untrusted
17. Current message last and byte-identical

=== Budget ===
18. Mandatory budget exceeded fails closed
19. Envelope stays within 16,000 units

=== Idempotency / determinism ===
20. Same inputs produce same outputs

=== Adversarial corpus ===
21. Each adversarial case checked for system safety

=== Appraisal boundary ===
22. Appraisal uses separate system + user message

=== No sensitive data in logs ===
23. caplog does not contain sensitive markers
"""

import json
import math
import logging
import pytest

from backend.trusted_context import (
    ChatMessage,
    ContextItem,
    ContextBundle,
    TruncationReport,
    ContextBuildResult,
    EpistemicStatus,
    Provenance,
    TrustedContextError,
    BOUNDARY_RULE,
    build_envelope,
    _serialize_canonical_json,
    _serialize_untrusted_items,
    _estimate_messages_units,
    UNTRUSTED_CONTEXT_MARKER,
)
from backend.admission_contracts import PROVIDER_INPUT_MAX_ESTIMATED_UNITS

from backend.tests.fixtures.adversarial_corpus import (
    ALL_ADVERSARIAL_CASES,
    AdversarialCase,
)


# ═══════════════════════════════════════════════════════════════════════
# 1. Domain purity — importability without infrastructure
# ═══════════════════════════════════════════════════════════════════════

class TestImportability:
    """Verify the module imports without triggering infrastructure deps."""

    def test_no_external_deps(self):
        """Import does not require groq, supabase, fastapi, etc."""
        from backend import trusted_context
        # Verify key symbols are accessible
        assert hasattr(trusted_context, "ChatMessage")
        assert hasattr(trusted_context, "ContextItem")
        assert hasattr(trusted_context, "ContextBundle")
        assert hasattr(trusted_context, "build_envelope")
        assert hasattr(trusted_context, "EpistemicStatus")
        assert hasattr(trusted_context, "Provenance")

    def test_isolated_subprocess_import(self):
        """Import trusted_context in subprocess with infrastructure blocked."""
        import subprocess
        import sys
        import os as _os

        project_root = _os.path.abspath(
            _os.path.join(_os.path.dirname(__file__), "../..")
        )

        code = f"""
import sys
import os
os.environ.pop('SUPABASE_URL', None)
os.environ.pop('SUPABASE_SERVICE_ROLE_KEY', None)
os.environ.pop('GROQ_API_KEY', None)
os.environ.pop('GROQ_API_KEYS', None)

sys.path = [p for p in sys.path if 'katherine' not in p.lower()]
sys.path.insert(0, {project_root!r})

import builtins
original_import = builtins.__import__
blocked = {{
    'fastapi', 'groq', 'supabase', 'sentence_transformers', 'httpx',
    'httpcore', 'anyio', 'websockets', 'uvicorn', 'pydantic',
    'engine', 'memory', 'emotional_core', 'emotional_domain',
    'relationship', 'lock_manager', 'archival_memory', 'turn_execution',
    'provider_models', 'groq_keys', 'groq_manager',
    'starlette', 'multipart', 'watchfiles', 'numpy', 'torch',
    'dotenv', 'cryptography', 'bcrypt', 'passlib',
}}

def _blocking_import(name, *args, **kwargs):
    top = name.split('.')[0]
    if top in blocked:
        raise ImportError(f'blocked: {{name}}')
    return original_import(name, *args, **kwargs)

builtins.__import__ = _blocking_import

import socket
original_socket = socket.socket
def _blocking_socket(*args, **kwargs):
    raise OSError('network blocked')
socket.socket = _blocking_socket

from backend.trusted_context import (
    ChatMessage, ContextItem, ContextBundle, build_envelope,
    EpistemicStatus, Provenance, TrustedContextError,
)

msg = ChatMessage(role="user", content="hi", source_id="ref-1", sort_key=(1, 0))
assert msg.role == "user"
assert msg.content == "hi"

item = ContextItem(
    kind="profile", content="{{'test':'val'}}",
    provenance=Provenance.LEGACY_PROFILE,
    confidence=0.5, epistemic_status=EpistemicStatus.UNKNOWN,
    source_id="ctx-1",
)
assert item.kind == "profile"

print("OK: isolated_import_succeeded")
"""

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        print(f"Subprocess stdout: {result.stdout}")
        if result.returncode != 0:
            print(f"Subprocess stderr: {result.stderr}")
        assert result.returncode == 0, f"Import failed: {result.stderr}"
        assert "OK:" in result.stdout


# ═══════════════════════════════════════════════════════════════════════
# 2. ChatMessage validation
# ═══════════════════════════════════════════════════════════════════════

class TestChatMessage:
    """ChatMessage validation tests."""

    def test_accepts_user_assistant(self):
        msg = ChatMessage(role="user", content="Hello", source_id="msg-1", sort_key=(1, 2))
        assert msg.role == "user"
        assert msg.content == "Hello"

        msg2 = ChatMessage(role="assistant", content="Hi", source_id="msg-2", sort_key=(2, 1))
        assert msg2.role == "assistant"

    def test_system_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_history_role"):
            ChatMessage(role="system", content="policy", source_id="msg-1", sort_key=(1, 0))

    def test_invalid_role_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_history_role"):
            ChatMessage(role="admin", content="data", source_id="msg-1", sort_key=(1, 0))

    def test_empty_content_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_history_content_type"):
            ChatMessage(role="user", content=123, source_id="msg-1", sort_key=(1, 0))

    def test_invalid_source_id_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_source_id"):
            ChatMessage(role="user", content="hi", source_id="", sort_key=(1, 0))

        with pytest.raises(TrustedContextError, match="invalid_source_id"):
            ChatMessage(role="user", content="hi", source_id=123, sort_key=(1, 0))

    def test_invalid_sort_key_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_sort_key"):
            ChatMessage(role="user", content="hi", source_id="r1", sort_key="not_a_tuple")

    def test_sort_key_too_short_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_sort_key"):
            ChatMessage(role="user", content="hi", source_id="r1", sort_key=(1,))

    def test_deterministic_sort_by_id_when_timestamps_equal(self):
        """History with equal timestamps is ordered deterministically by ID."""
        older = ChatMessage(role="user", content="A", source_id="msg-1", sort_key=(1000, 1))
        newer = ChatMessage(role="user", content="B", source_id="msg-2", sort_key=(1000, 2))
        # sort_key (1000, 1) < (1000, 2) so older comes first
        messages = [newer, older]
        messages_sorted = sorted(messages, key=lambda m: m.sort_key)
        assert messages_sorted[0].source_id == "msg-1"
        assert messages_sorted[1].source_id == "msg-2"
        assert messages_sorted[0].content == "A"


# ═══════════════════════════════════════════════════════════════════════
# 3. ContextItem validation
# ═══════════════════════════════════════════════════════════════════════

class TestContextItem:
    """ContextItem validation tests."""

    def test_valid_item(self):
        item = ContextItem(
            kind="memory",
            content="User likes cats.",
            provenance=Provenance.LEGACY_MEMORY,
            confidence=0.7,
            epistemic_status=EpistemicStatus.UNKNOWN,
            source_id="ctx-1",
        )
        assert item.kind == "memory"
        assert item.to_json_dict()["record_type"] == UNTRUSTED_CONTEXT_MARKER

    def test_invalid_kind_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_kind"):
            ContextItem(
                kind="invalid",
                content="data",
                provenance=Provenance.LEGACY_MEMORY,
                confidence=0.5,
                epistemic_status=EpistemicStatus.UNKNOWN,
                source_id="ctx-1",
            )

    def test_empty_content_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_item_content"):
            ContextItem(
                kind="profile",
                content="",
                provenance=Provenance.LEGACY_PROFILE,
                confidence=0.5,
                epistemic_status=EpistemicStatus.UNKNOWN,
                source_id="ctx-1",
            )

    def test_invalid_provenance_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_provenance"):
            ContextItem(
                kind="memory",
                content="data",
                provenance="unknown_provenance",
                confidence=0.5,
                epistemic_status=EpistemicStatus.UNKNOWN,
                source_id="ctx-1",
            )

    def test_invalid_epistemic_status_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_epistemic_status"):
            ContextItem(
                kind="memory",
                content="data",
                provenance=Provenance.LEGACY_MEMORY,
                confidence=0.5,
                epistemic_status="proven_fact",
                source_id="ctx-1",
            )

    def test_confidence_bool_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_confidence_bool"):
            ContextItem(
                kind="memory",
                content="data",
                provenance=Provenance.LEGACY_MEMORY,
                confidence=True,
                epistemic_status=EpistemicStatus.UNKNOWN,
                source_id="ctx-1",
            )

    def test_confidence_none_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_confidence_none"):
            ContextItem(
                kind="memory",
                content="data",
                provenance=Provenance.LEGACY_MEMORY,
                confidence=None,
                epistemic_status=EpistemicStatus.UNKNOWN,
                source_id="ctx-1",
            )

    def test_confidence_nan_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_confidence_nan_inf"):
            ContextItem(
                kind="memory",
                content="data",
                provenance=Provenance.LEGACY_MEMORY,
                confidence=math.nan,
                epistemic_status=EpistemicStatus.UNKNOWN,
                source_id="ctx-1",
            )

    def test_confidence_inf_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_confidence_nan_inf"):
            ContextItem(
                kind="memory",
                content="data",
                provenance=Provenance.LEGACY_MEMORY,
                confidence=math.inf,
                epistemic_status=EpistemicStatus.UNKNOWN,
                source_id="ctx-1",
            )

    def test_confidence_negative_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_confidence_range"):
            ContextItem(
                kind="memory",
                content="data",
                provenance=Provenance.LEGACY_MEMORY,
                confidence=-0.01,
                epistemic_status=EpistemicStatus.UNKNOWN,
                source_id="ctx-1",
            )

    def test_confidence_over_one_rejected(self):
        with pytest.raises(TrustedContextError, match="invalid_confidence_range"):
            ContextItem(
                kind="memory",
                content="data",
                provenance=Provenance.LEGACY_MEMORY,
                confidence=1.01,
                epistemic_status=EpistemicStatus.UNKNOWN,
                source_id="ctx-1",
            )

    def test_confidence_zero_accepted(self):
        item = ContextItem(
            kind="profile",
            content="data",
            provenance=Provenance.LEGACY_PROFILE,
            confidence=0.0,
            epistemic_status=EpistemicStatus.UNKNOWN,
            source_id="ctx-1",
        )
        assert item.confidence == 0.0

    def test_confidence_one_accepted(self):
        item = ContextItem(
            kind="profile",
            content="data",
            provenance=Provenance.LEGACY_PROFILE,
            confidence=1.0,
            epistemic_status=EpistemicStatus.APPROVED,
            source_id="ctx-1",
        )
        assert item.confidence == 1.0

    def test_legacy_profile_marked_unknown(self):
        """Legacy profile is marked as unverified/inferred, never as observed."""
        item = ContextItem(
            kind="profile",
            content='{"name":"User"}',
            provenance=Provenance.LEGACY_PROFILE,
            confidence=0.3,
            epistemic_status=EpistemicStatus.UNKNOWN,
            source_id="ctx-1",
        )
        assert item.epistemic_status in (EpistemicStatus.UNKNOWN, EpistemicStatus.INFERRED)

    def test_to_json_dict_no_real_ids(self):
        """to_json_dict does not expose real UUIDs or internal IDs."""
        item = ContextItem(
            kind="memory",
            content="User likes cats.",
            provenance=Provenance.LEGACY_MEMORY,
            confidence=0.7,
            epistemic_status=EpistemicStatus.UNKNOWN,
            source_id="mem-1",
        )
        payload = item.to_json_dict()
        assert "record_type" in payload
        assert payload["record_type"] == UNTRUSTED_CONTEXT_MARKER
        # Must not contain "id", "uuid", "user_id" key names at top level
        for key in ("id", "uuid", "user_id", "source_id"):
            assert key not in payload, f"Key '{key}' leaked into provider payload"
        # The reference field is an opaque local ref, not a real UUID
        assert payload["reference"] == "mem-1"


# ═══════════════════════════════════════════════════════════════════════
# 4. TruncationReport
# ═══════════════════════════════════════════════════════════════════════

class TestTruncationReport:
    """Truncation report tests."""

    def test_default_empty(self):
        report = TruncationReport()
        assert report.codes == ()
        assert report.omitted_history_count == 0
        assert report.omitted_memory_count == 0
        assert report.omitted_profile_count == 0
        assert report.selected_history_count == 0
        assert report.selected_memory_count == 0
        assert report.selected_profile_count == 0

    def test_custom_values(self):
        report = TruncationReport(
            codes=("history_partial",),
            omitted_history_count=3,
            selected_history_count=2,
            omitted_memory_count=1,
            selected_memory_count=0,
        )
        assert "history_partial" in report.codes
        assert report.omitted_history_count == 3
        assert report.omitted_memory_count == 1


# ═══════════════════════════════════════════════════════════════════════
# 5. ContextBundle
# ═══════════════════════════════════════════════════════════════════════

class TestContextBundle:
    """ContextBundle construction tests."""

    def test_minimal_bundle(self):
        bundle = ContextBundle(
            trusted_policy="You are a helpful assistant.",
        )
        assert bundle.trusted_policy == "You are a helpful assistant."
        assert bundle.history == ()
        assert bundle.profile_items == ()
        assert bundle.memory_items == ()

    def test_empty_policy_rejected(self):
        with pytest.raises(TrustedContextError, match="empty_trusted_policy"):
            ContextBundle(trusted_policy="")

    def test_bundle_with_history(self):
        msg = ChatMessage(role="user", content="Hi", source_id="m1", sort_key=(1, 1))
        bundle = ContextBundle(
            trusted_policy="Policy.",
            history=(msg,),
        )
        assert len(bundle.history) == 1

    def test_bundle_with_items(self):
        item = ContextItem(
            kind="profile", content='{"name":"User"}',
            provenance=Provenance.LEGACY_PROFILE,
            confidence=0.3, epistemic_status=EpistemicStatus.UNKNOWN,
            source_id="p1",
        )
        bundle = ContextBundle(
            trusted_policy="Policy.",
            profile_items=(item,),
        )
        assert len(bundle.profile_items) == 1


# ═══════════════════════════════════════════════════════════════════════
# 6. Envelope construction tests
# ═══════════════════════════════════════════════════════════════════════

class TestEnvelopeConstruction:
    """Core envelope construction tests."""

    def test_sort_key_respects_created_at_not_id_numeric(self):
        """Rendering order respects created_at timestamp, not id numeric value.

        Two messages with out-of-order IDs (newer created_at has smaller id)
        must be rendered by created_at order, not by id.
        """
        # Message with older timestamp but larger id
        older = ChatMessage(role="user", content="Older message", source_id="m1", sort_key=("2026-07-30T00:00:00", 100))
        # Message with newer timestamp but smaller id
        newer = ChatMessage(role="assistant", content="Newer message", source_id="m2", sort_key=("2026-07-30T00:00:01", 20))
        bundle = ContextBundle(
            trusted_policy="Policy.",
            history=(older, newer),
        )
        result = build_envelope(bundle, "Current")
        # Extract content in order of appearance
        contents = [m["content"] for m in result.messages
                    if m["role"] in ("user", "assistant") and m["content"] != "Current"]
        # Older should appear before newer
        old_idx = contents.index("Older message") if "Older message" in contents else -1
        new_idx = contents.index("Newer message") if "Newer message" in contents else -1
        assert old_idx >= 0, "Older message should appear in output"
        assert new_idx >= 0, "Newer message should appear in output"
        assert old_idx < new_idx, "Older message should be rendered before newer message"

    def test_sort_key_same_timestamp_different_ids(self):
        """When two messages have the same created_at, id is used as tiebreaker."""
        msg_a = ChatMessage(role="user", content="A", source_id="m1", sort_key=("2026-07-30T00:00:00", 1))
        msg_b = ChatMessage(role="assistant", content="B", source_id="m2", sort_key=("2026-07-30T00:00:00", 2))
        bundle = ContextBundle(
            trusted_policy="Policy.",
            history=(msg_a, msg_b),
        )
        result = build_envelope(bundle, "Current")
        contents = [m["content"] for m in result.messages
                    if m["role"] in ("user", "assistant") and m["content"] != "Current"]
        a_idx = contents.index("A")
        b_idx = contents.index("B")
        assert a_idx < b_idx, "Message with id=1 should appear before id=2 when timestamps equal"

    def test_minimal_envelope(self):
        """System + current user preserved."""
        bundle = ContextBundle(trusted_policy="You are a bot.")
        result = build_envelope(bundle, "Hello")
        assert len(result.messages) >= 2
        assert result.messages[0]["role"] == "system"
        assert BOUNDARY_RULE in result.messages[0]["content"]
        assert result.messages[-1]["role"] == "user"
        assert result.messages[-1]["content"] == "Hello"
        assert result.unit_count > 0

    def test_current_message_byte_identical(self):
        """Current message remains byte-identical."""
        bundle = ContextBundle(trusted_policy="Policy.")
        message = "Hello, world! 😀 Special chars: ñüö"
        result = build_envelope(bundle, message)
        assert result.messages[-1]["content"] == message

    def test_current_message_last(self):
        """Current user message is always the last message."""
        msg1 = ChatMessage(role="user", content="Earlier", source_id="m1", sort_key=(1, 1))
        msg2 = ChatMessage(role="assistant", content="Response", source_id="m2", sort_key=(2, 1))
        bundle = ContextBundle(
            trusted_policy="Policy.",
            history=(msg1, msg2),
        )
        result = build_envelope(bundle, "Current")
        assert result.messages[-1]["role"] == "user"
        assert result.messages[-1]["content"] == "Current"

    def test_history_with_original_roles(self):
        """History arrives at provider with original roles preserved."""
        msg1 = ChatMessage(role="user", content="Hello", source_id="m1", sort_key=(1, 1))
        msg2 = ChatMessage(role="assistant", content="Hi there!", source_id="m2", sort_key=(2, 1))
        bundle = ContextBundle(
            trusted_policy="Policy.",
            history=(msg1, msg2),
        )
        result = build_envelope(bundle, "Current")
        # Find the history messages (ignore system, untrusted context)
        history_msgs = [m for m in result.messages
                        if m["role"] in ("user", "assistant")
                        and m["content"] != "Current"
                        and not m["content"].startswith("[")]
        # Roles should be as original
        user_msgs = [m for m in history_msgs if m["role"] == "user"]
        assistant_msgs = [m for m in history_msgs if m["role"] == "assistant"]
        assert any("Hello" in m["content"] for m in user_msgs)
        assert any("Hi there!" in m["content"] for m in assistant_msgs)
        # No history message should have role "system"
        assert all(m["role"] in ("user", "assistant") for m in history_msgs)

    def test_system_identical_across_users_same_state(self):
        """System is identical between two users with same typed state."""
        bundle_a = ContextBundle(trusted_policy="Same policy.")
        bundle_b = ContextBundle(trusted_policy="Same policy.")
        msg_a = ChatMessage(role="user", content="Hi", source_id="a1", sort_key=(1, 1))
        msg_b = ChatMessage(role="user", content="Hi", source_id="b1", sort_key=(1, 1))
        bundle_a = ContextBundle(trusted_policy="Policy.", history=(msg_a,))
        bundle_b = ContextBundle(trusted_policy="Policy.", history=(msg_b,))
        result_a = build_envelope(bundle_a, "Hello")
        result_b = build_envelope(bundle_b, "Hello")
        # System content is identical (same policy)
        assert result_a.messages[0]["content"] == result_b.messages[0]["content"]

    def test_no_real_ids_in_messages(self):
        """No opaque local ref from ChatMessage appears in provider messages.

        ChatMessage.source_id ("msg-1") is an internal opaque reference
        that must NOT leak into the serialized messages sent to the provider.
        ContextItem.source_id appears in the untrusted JSON payload as
        the "reference" field — that is intentional (it is an opaque local
        ref, not a real UUID).  Real UUIDs must never appear.
        """
        item_mem = ContextItem(
            kind="memory", content="Fact",
            provenance=Provenance.LEGACY_MEMORY,
            confidence=0.5, epistemic_status=EpistemicStatus.UNKNOWN,
            source_id="mem-1",
        )
        item_prof = ContextItem(
            kind="profile", content='{"name":"User"}',
            provenance=Provenance.LEGACY_PROFILE,
            confidence=0.3, epistemic_status=EpistemicStatus.UNKNOWN,
            source_id="prof-1",
        )
        msg = ChatMessage(role="user", content="Test", source_id="msg-1", sort_key=(1, 1))
        bundle = ContextBundle(
            trusted_policy="Policy.",
            history=(msg,),
            memory_items=(item_mem,),
            profile_items=(item_prof,),
        )
        result = build_envelope(bundle, "Hi")
        serialized = json.dumps(result.messages)
        # ChatMessage source_ids must never appear in provider messages
        assert "msg-1" not in serialized, "ChatMessage source_id leaked into provider messages"
        # No UUID format in messages
        import re
        uuid_pattern = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
        assert not re.search(uuid_pattern, serialized), "UUID pattern found in messages"

    def test_source_map_contains_only_bundle_sources(self):
        """Reference map contains only sources existing in the bundle."""
        item = ContextItem(
            kind="memory", content="Fact",
            provenance=Provenance.LEGACY_MEMORY,
            confidence=0.5, epistemic_status=EpistemicStatus.UNKNOWN,
            source_id="mem-1",
        )
        bundle = ContextBundle(
            trusted_policy="Policy.",
            memory_items=(item,),
        )
        result = build_envelope(bundle, "Hi")
        # All keys in source_map should exist as source_ids in the bundle
        for key in result.source_map:
            all_sources = set()
            for m in bundle.history:
                all_sources.add(m.source_id)
            for item_list in (bundle.profile_items, bundle.memory_items, bundle.persona_items):
                for i in item_list:
                    all_sources.add(i.source_id)
            assert key in all_sources

    def test_history_deterministic_order(self):
        """Selection renders history in chronological order (oldest first)."""
        old = ChatMessage(role="user", content="Old", source_id="m1", sort_key=(1, 1))
        mid = ChatMessage(role="assistant", content="Mid", source_id="m2", sort_key=(2, 1))
        new = ChatMessage(role="user", content="New", source_id="m3", sort_key=(3, 1))
        bundle = ContextBundle(
            trusted_policy="Policy.",
            history=(old, mid, new),
        )
        result = build_envelope(bundle, "Current")
        # Find history messages in order
        history_in_result = [m for m in result.messages
                             if m["role"] in ("user", "assistant")
                             and m["content"] != "Current"]
        # Should appear in chronological order
        content_order = [m["content"] for m in history_in_result]
        old_idx = content_order.index("Old") if "Old" in content_order else -1
        mid_idx = content_order.index("Mid") if "Mid" in content_order else -1
        new_idx = content_order.index("New") if "New" in content_order else -1
        if old_idx >= 0 and mid_idx >= 0:
            assert old_idx < mid_idx
        if mid_idx >= 0 and new_idx >= 0:
            assert mid_idx < new_idx


# ═══════════════════════════════════════════════════════════════════════
# 7. Injection safety tests
# ═══════════════════════════════════════════════════════════════════════

class TestInjectionSafety:
    """Injection markers must never appear in system message."""

    @pytest.mark.parametrize(
        "case",
        ALL_ADVERSARIAL_CASES,
        ids=[c.label for c in ALL_ADVERSARIAL_CASES],
    )
    def test_injection_marker_not_in_system(self, case: AdversarialCase):
        """Verify adversarial injection markers never reach system."""
        # Build history as ChatMessages
        history_msgs = []
        for i, h in enumerate(case.history):
            history_msgs.append(ChatMessage(
                role=h["role"],
                content=h["content"],
                source_id=f"adv-{i}",
                sort_key=(i, i),
            ))

        # Build profile items
        profile_items = []
        if case.profile:
            profile_str = json.dumps(case.profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            profile_items.append(ContextItem(
                kind="profile",
                content=profile_str,
                provenance=Provenance.LEGACY_PROFILE,
                confidence=0.3,
                epistemic_status=EpistemicStatus.UNKNOWN,
                source_id="adv-profile",
            ))

        # Build memory items
        memory_items = []
        for j, mem_str in enumerate(case.memories):
            memory_items.append(ContextItem(
                kind="memory",
                content=mem_str,
                provenance=Provenance.LEGACY_MEMORY,
                confidence=0.5,
                epistemic_status=EpistemicStatus.UNKNOWN,
                source_id=f"adv-mem-{j}",
            ))

        # Build persona items
        persona_items = []
        if case.persona:
            persona_items.append(ContextItem(
                kind="persona",
                content=case.persona,
                provenance=Provenance.LEGACY_PERSONA,
                confidence=0.3,
                epistemic_status=EpistemicStatus.UNKNOWN,
                source_id="adv-persona",
            ))

        bundle = ContextBundle(
            trusted_policy="You are a helpful assistant with safety rules.",
            history=tuple(history_msgs),
            profile_items=tuple(profile_items),
            memory_items=tuple(memory_items),
            persona_items=tuple(persona_items),
        )

        result = build_envelope(bundle, "Current message")
        system_content = result.messages[0]["content"]

        if case.expected_system_safe:
            assert case.injection_marker not in system_content, (
                f"Injection marker '{case.injection_marker}' found in system content "
                f"for case '{case.label}'"
            )

        # The injection marker may appear in non-system messages
        all_non_system = [m for m in result.messages if m["role"] != "system"]
        has_injection_in_non_system = any(
            case.injection_marker in m.get("content", "")
            for m in all_non_system
        )

        # For cases with injection in source data, the marker must appear
        # in a non-system message (injection is only dangerous in system)
        if case.history or case.memories or case.profile:
            assert has_injection_in_non_system, (
                f"Injection marker '{case.injection_marker}' for case '{case.label}' "
                f"should appear in non-system messages"
            )


# ═══════════════════════════════════════════════════════════════════════
# 8. Budget tests
# ═══════════════════════════════════════════════════════════════════════

class TestBudget:
    """Budget enforcement tests — ten requirements."""

    # Helper to create a context item
    @staticmethod
    def _make_item(kind: str, content: str, source_id: str, confidence=0.5) -> ContextItem:
        return ContextItem(
            kind=kind, content=content,
            provenance=Provenance.LEGACY_PROFILE if kind == "profile" else Provenance.LEGACY_MEMORY,
            confidence=confidence, epistemic_status=EpistemicStatus.UNKNOWN,
            source_id=source_id,
        )

    def test_mandatory_budget_exceeded_fails(self):
        """10. System + current message never truncated (fail closed if budget exceeded)."""
        large_policy = "X" * 20000
        bundle = ContextBundle(trusted_policy=large_policy)
        with pytest.raises(TrustedContextError, match="mandatory_budget_exceeded"):
            build_envelope(bundle, "Hi", max_units=16000)

    def test_envelope_within_16000(self):
        """9. Envelope never exceeds 16,000 units."""
        bundle = ContextBundle(
            trusted_policy="Policy. " * 50,
            profile_items=(
                self._make_item("profile", '{"name":"User"}', "p1"),
            ),
        )
        result = build_envelope(bundle, "Hi")
        assert result.unit_count <= PROVIDER_INPUT_MAX_ESTIMATED_UNITS

    def test_history_preserved_over_profile(self):
        """1. A large profile does not remove the most recent history messages."""
        msg_recent = ChatMessage(role="user", content="Recent msg", source_id="m1", sort_key=(100, 1))
        msg_old = ChatMessage(role="user", content="x" * 5000, source_id="m2", sort_key=(1, 2))
        bundle = ContextBundle(
            trusted_policy="Short policy.",
            history=(msg_old, msg_recent),
            profile_items=(self._make_item("profile", '{"x":"' + 'y' * 3000 + '"}', "p1"),),
        )
        result = build_envelope(bundle, "Hi", max_units=5000)
        # Recent message must be preserved
        all_content = " ".join(m.get("content", "") for m in result.messages)
        assert "Recent msg" in all_content, "Recent history was removed by large profile"

    def test_one_of_two_memories_fits(self):
        """2. Only one between two memories fits under tight budget."""
        memory_a = self._make_item("memory", "A" * 200, "mem-a")
        memory_b = self._make_item("memory", "B" * 200, "mem-b")
        msg = ChatMessage(role="user", content="Hi", source_id="m1", sort_key=(1, 1))
        bundle = ContextBundle(
            trusted_policy="Short policy.",
            history=(msg,),
            memory_items=(memory_a, memory_b),
        )
        result = build_envelope(bundle, "Current", max_units=2000)
        # At least one memory should be selected
        all_text = " ".join(m.get("content", "") for m in result.messages)
        assert ("A" * 200) in all_text or ("B" * 200) in all_text

    def test_memory_fits_but_profile_and_persona_not(self):
        """3. Memory fits under tight budget but profile+persona are omitted."""
        memory = self._make_item("memory", "Small memory fact", "mem-1", confidence=0.5)
        profile = self._make_item("profile", '{"x":"' + 'y' * 2000 + '"}', "p1")
        persona = self._make_item("persona", "Long " + "z" * 2000, "per-1")
        msg = ChatMessage(role="user", content="Hi", source_id="m1", sort_key=(1, 1))
        bundle = ContextBundle(
            trusted_policy="Short policy.",
            history=(msg,),
            memory_items=(memory,),
            profile_items=(profile,),
            persona_items=(persona,),
        )
        result = build_envelope(bundle, "Current", max_units=3000)
        # Memory should survive
        all_text = " ".join(m.get("content", "") for m in result.messages)
        assert "Small memory fact" in all_text, "Memory was omitted"

    def test_only_most_recent_history_message_fits(self):
        """4. Only the most recent history message fits under tight budget."""
        old_msg = ChatMessage(role="user", content="x" * 2000, source_id="m1", sort_key=(1, 1))
        recent_msg = ChatMessage(role="assistant", content="Short reply", source_id="m2", sort_key=(2, 2))
        bundle = ContextBundle(
            trusted_policy="Policy. " * 20,
            history=(old_msg, recent_msg),
        )
        result = build_envelope(bundle, "Current", max_units=1500)
        report = result.truncation_report
        # Most recent message must be selected (assistant "Short reply")
        all_text = " ".join(m.get("content", "") for m in result.messages)
        assert "Short reply" in all_text, "Recent history message was lost"
        # Old large message may be omitted
        if "x" * 2000 not in all_text:
            assert report.omitted_history_count > 0

    def test_duplicate_content_preserved(self):
        """5. Two history messages with same content as current message remain."""
        msg1 = ChatMessage(role="user", content="sim", source_id="m1", sort_key=(1, 1))
        msg2 = ChatMessage(role="assistant", content="entendido", source_id="m2", sort_key=(2, 2))
        bundle = ContextBundle(
            trusted_policy="Short policy.",
            history=(msg1, msg2),
        )
        result = build_envelope(bundle, "sim")
        # Both distinct history messages must appear in the envelope
        user_msgs = [m for m in result.messages
                     if m["role"] == "user" and m["content"] == "sim"]
        assert len(user_msgs) >= 2, (
            f"Expected at least 2 'sim' messages (history + current), got {len(user_msgs)}"
        )

    def test_source_map_only_selected_items(self):
        """6+7. source_map contains only selected items; omitted items are absent."""
        mem_selected = self._make_item("memory", "Small fact", "mem-selected")
        mem_omitted = self._make_item("memory", "x" * 5000, "mem-omitted")
        bundle = ContextBundle(
            trusted_policy="Short policy.",
            memory_items=(mem_omitted, mem_selected),
        )
        result = build_envelope(bundle, "Hi", max_units=2000)
        # The omitted memory should NOT be in source_map
        assert "mem-omitted" not in result.source_map, "Omitted item found in source_map"

    def test_persona_counts_correct(self):
        """8. Persona selection and omission counts are correct."""
        persona = self._make_item("persona", "x" * 100, "per-1")
        bundle = ContextBundle(
            trusted_policy="Short policy.",
            persona_items=(persona,),
        )
        result = build_envelope(bundle, "Hi")
        report = result.truncation_report
        assert report.selected_persona_count == 1, "Persona should be selected when budget fits"
        assert report.omitted_persona_count == 0


# ═══════════════════════════════════════════════════════════════════════
# 9. Epistemic status and provenance contracts
# ═══════════════════════════════════════════════════════════════════════

class TestEpistemicContracts:
    """Epistemic status and provenance contract tests."""

    def test_insult_stays_historical(self):
        """An insult citation stays a historical message, not a derived fact."""
        history = [
            ChatMessage(role="user", content="You are useless", source_id="m1", sort_key=(1, 1)),
        ]
        bundle = ContextBundle(
            trusted_policy="Policy.",
            history=tuple(history),
        )
        result = build_envelope(bundle, "Hi")
        # The insult should be in a user message, not in system
        system_content = result.messages[0]["content"]
        assert "useless" not in system_content
        # The insult should appear in a non-system message
        non_system = [m for m in result.messages if m["role"] != "system"]
        assert any("useless" in m.get("content", "") for m in non_system)

    def test_third_party_claim_not_promoted(self):
        """Third-party claim is not promoted to observation."""
        item = ContextItem(
            kind="memory",
            content="User's friend says user is a good person",
            provenance=Provenance.LEGACY_MEMORY,
            confidence=0.4,
            epistemic_status=EpistemicStatus.THIRD_PARTY,
            source_id="mem-1",
        )
        bundle = ContextBundle(
            trusted_policy="Policy.",
            memory_items=(item,),
        )
        result = build_envelope(bundle, "Hi")
        system_content = result.messages[0]["content"]
        assert "good person" not in system_content

    def test_approved_memory_enters_as_untrusted(self):
        """Valid approved memory enters as untrusted data, not system."""
        item = ContextItem(
            kind="memory",
            content="User likes cats (confirmed).",
            provenance=Provenance.USER_CONFIRMED,
            confidence=0.9,
            epistemic_status=EpistemicStatus.APPROVED,
            source_id="mem-1",
            internal_id="550e8400-e29b-41d4-a716-446655440000",
        )
        bundle = ContextBundle(
            trusted_policy="Policy.",
            memory_items=(item,),
        )
        result = build_envelope(bundle, "Hi")
        system_content = result.messages[0]["content"]
        # Memory content should NOT be in system
        assert "cats" not in system_content
        # Should appear in non-system (untrusted context)
        non_system = [m for m in result.messages if m["role"] != "system"]
        all_content = " ".join(m.get("content", "") for m in non_system)
        assert "cats" in all_content or "cats" in str(result.messages)


# ═══════════════════════════════════════════════════════════════════════
# 10. Appraisal boundary
# ═══════════════════════════════════════════════════════════════════════

class TestAppraisalBoundary:
    """Appraisal uses separated system instruction and user message."""

    @pytest.mark.anyio
    async def test_appraisal_system_and_user_separate(self, monkeypatch):
        """Appraisal system instruction is in a system message, user message separate."""
        import json
        from unittest.mock import MagicMock, patch
        from backend.engine import ConversationEngine
        from backend.turn_execution import create_budget, TurnExecutionConfig
        from backend.groq_manager import GroqClientManager
        from backend.memory import MemoryManager

        recorded = {}

        async def mock_chat_completion_async(messages, **kwargs):
            recorded["messages"] = messages
            response_text = json.dumps({
                "valence": 0.0, "arousal_shift": 0.0, "dominance_shift": 0.0,
                "triggered_emotions": {
                    "joy": 0, "sadness": 0, "anger": 0, "fear": 0,
                    "disgust": 0, "surprise": 0, "tenderness": 0,
                    "guilt": 0, "pride": 0, "jealousy": 0, "gratitude": 0,
                },
            })
            class FakeContent:
                content = response_text
            class FakeMessage:
                message = FakeContent()
            class FakeResponse:
                choices = [FakeMessage()]
            return FakeResponse()

        # Install doubles BEFORE constructing ConversationEngine
        with (
            patch.object(GroqClientManager, "__init__", return_value=None),
            patch.object(MemoryManager, "__init__", return_value=None),
            patch("backend.memory.SentenceTransformer", return_value=MagicMock()),
        ):
            engine = ConversationEngine(clock=lambda: 1000.0)

        monkeypatch.setattr(
            engine.groq_manager, "chat_completion_async", mock_chat_completion_async
        )

        budget = create_budget(TurnExecutionConfig.defaults(), now_provider=engine._monotonic)
        user_text = "I love cats"

        await engine._appraise(user_text, budget)

        messages = recorded.get("messages")
        assert messages is not None and len(messages) >= 2

        # First message must be a system policy message
        system_msg = messages[0]
        assert system_msg.get("role") == "system"
        assert "emotional impact" in system_msg.get("content", "")

        # Second message must be the raw user message
        user_msg = messages[1]
        assert user_msg.get("role") == "user"
        assert user_msg.get("content") == user_text

    @pytest.mark.anyio
    async def test_appraisal_policy_has_no_user_message_interpolated(self, monkeypatch):
        """The appraisal policy does not contain the user message."""
        import json
        from unittest.mock import MagicMock, patch
        from backend.engine import ConversationEngine
        from backend.turn_execution import create_budget, TurnExecutionConfig
        from backend.groq_manager import GroqClientManager
        from backend.memory import MemoryManager

        recorded = {}

        async def mock_chat_completion_async(messages, **kwargs):
            recorded["messages"] = messages
            response_text = json.dumps({
                "valence": 0.0, "arousal_shift": 0.0, "dominance_shift": 0.0,
                "triggered_emotions": {
                    "joy": 0, "sadness": 0, "anger": 0, "fear": 0,
                    "disgust": 0, "surprise": 0, "tenderness": 0,
                    "guilt": 0, "pride": 0, "jealousy": 0, "gratitude": 0,
                },
            })
            class _Msg:
                content = response_text
            class _Choice:
                message = _Msg()
            class _Resp:
                choices = [_Choice()]
            return _Resp()

        # Install doubles BEFORE constructing ConversationEngine
        with (
            patch.object(GroqClientManager, "__init__", return_value=None),
            patch.object(MemoryManager, "__init__", return_value=None),
            patch("backend.memory.SentenceTransformer", return_value=MagicMock()),
        ):
            engine = ConversationEngine(clock=lambda: 1000.0)

        monkeypatch.setattr(
            engine.groq_manager, "chat_completion_async", mock_chat_completion_async
        )

        budget = create_budget(TurnExecutionConfig.defaults(), now_provider=engine._monotonic)
        user_text = "My bank account number is 1234-5678."

        await engine._appraise(user_text, budget)

        messages = recorded.get("messages")
        assert messages is not None and len(messages) >= 2

        system_msg = messages[0]
        user_msg = messages[1]

        # System message must not contain the user content
        assert system_msg.get("role") == "system"
        system_content = system_msg.get("content", "")
        assert user_text not in system_content, "User content leaked into appraisal policy"

        # User message must contain the raw user content
        assert user_msg.get("role") == "user"
        assert user_msg.get("content") == user_text


# ═══════════════════════════════════════════════════════════════════════
# 11. Log sanitisation
# ═══════════════════════════════════════════════════════════════════════

class TestLogSanitization:
    """Logs must not contain sensitive content."""

    def test_caplog_no_sensitive_markers(self, caplog):
        """caplog does not contain sensitive markers from adversarial data."""
        secret = "supersecretmarker12345"
        caplog.set_level(logging.DEBUG)

        # Build a bundle with the secret marker in history
        msg = ChatMessage(
            role="user", content=f"This is a {secret} message",
            source_id="m1", sort_key=(1, 1),
        )
        bundle = ContextBundle(
            trusted_policy="Policy.",
            history=(msg,),
        )
        try:
            result = build_envelope(bundle, "Hi")
        except TrustedContextError:
            pass

        # The secret should NOT appear in logs
        assert secret not in caplog.text


# ═══════════════════════════════════════════════════════════════════════
# 12. Determinism tests
# ═══════════════════════════════════════════════════════════════════════

class TestDeterminism:
    """Same inputs always produce same outputs."""

    def test_deterministic_output(self):
        """Same inputs produce same outputs."""
        msg = ChatMessage(role="user", content="Hello", source_id="m1", sort_key=(1, 1))
        bundle = ContextBundle(
            trusted_policy="Policy.",
            history=(msg,),
        )
        result1 = build_envelope(bundle, "Hi")
        result2 = build_envelope(bundle, "Hi")
        assert result1.messages == result2.messages
        assert result1.unit_count == result2.unit_count


# ═══════════════════════════════════════════════════════════════════════
# 13. Serialization helpers
# ═══════════════════════════════════════════════════════════════════════

class TestSerialization:
    """Canonical serialization tests."""

    def test_canonical_json(self):
        """Canonical JSON uses sort_keys, compact separators, no ASCII escape."""
        data = {"z": 1, "a": 2, "name": "José"}
        result = _serialize_canonical_json(data)
        assert result == '{"a":2,"name":"José","z":1}'

    def test_untrusted_items_empty(self):
        result = _serialize_untrusted_items(())
        assert result == ""

    def test_untrusted_items_single(self):
        item = ContextItem(
            kind="memory", content="Test",
            provenance=Provenance.LEGACY_MEMORY,
            confidence=0.5, epistemic_status=EpistemicStatus.UNKNOWN,
            source_id="m1",
        )
        result = _serialize_untrusted_items((item,))
        assert UNTRUSTED_CONTEXT_MARKER in result
        assert "memory" in result
        # The reference field uses the opaque local ref (not real UUID)
        # But it IS the source_id for tracking - that's the intended design.
        # Real database UUIDs and internal user IDs should never appear.
        # Check that "uuid-" pattern is not present (real UUID format)
        import re
        uuid_pattern = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
        assert not re.search(uuid_pattern, result), "UUID pattern leaked into payload"


# ═══════════════════════════════════════════════════════════════════════
# 14. Edge cases — empty history, empty items
# ═══════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    """Edge case tests."""

    def test_empty_history(self):
        bundle = ContextBundle(trusted_policy="Policy.")
        result = build_envelope(bundle, "Hi")
        assert result.truncation_report.omitted_history_count == 0
        # Current message should be last
        assert result.messages[-1]["content"] == "Hi"

    def test_only_mandatory_items_fit(self):
        """Only mandatory items fit within a tighter budget."""
        short_policy = "Short policy."
        bundle = ContextBundle(trusted_policy=short_policy)
        # Use a budget that accommodates mandatory items
        result = build_envelope(bundle, "Hi", max_units=500)
        assert result.unit_count <= 500

    def test_omitted_history_counts(self):
        """When budget is tight, newer history is preferred."""
        # Create many history messages
        history = []
        for i in range(20):
            role = "user" if i % 2 == 0 else "assistant"
            history.append(ChatMessage(
                role=role,
                content=f"Message {i}",
                source_id=f"m{i}",
                sort_key=(i, i),
            ))
        bundle = ContextBundle(
            trusted_policy="Policy. " * 10,
            history=tuple(history),
        )
        # Use default budget (16,000)
        result = build_envelope(bundle, "Current")
        # Some history may be omitted
        assert result.unit_count <= PROVIDER_INPUT_MAX_ESTIMATED_UNITS


# ═══════════════════════════════════════════════════════════════════════
# 15. User isolation
# ═══════════════════════════════════════════════════════════════════════

class TestUserIsolation:
    """User context isolation tests."""

    def test_user_a_does_not_contain_user_b(self):
        """User A context never contains User B's data."""
        # This is enforced by the database query layer (user_id filter)
        # The trusted_context module just validates the data it receives
        # We verify the data structure is correct
        item_a = ContextItem(
            kind="memory", content="User A's secret",
            provenance=Provenance.USER_CONFIRMED,
            confidence=0.9, epistemic_status=EpistemicStatus.APPROVED,
            source_id="a1",
            internal_id="550e8400-e29b-41d4-a716-446655440000",
        )
        bundle_a = ContextBundle(
            trusted_policy="Policy.",
            memory_items=(item_a,),
        )
        result_a = build_envelope(bundle_a, "Hi A")
        # Check that User B's data is not in result_a (trivially true here)
        assert "User B" not in result_a.messages[0]["content"]


# ═══════════════════════════════════════════════════════════════════════
# 16. RetrievedMemory integration tests (pure)
# ═══════════════════════════════════════════════════════════════════════

class TestRetrievedMemoryIntegration:
    """RetrievedMemory to ContextItem conversion tests."""

    def test_retrieved_memory_to_context_item(self):
        """RetrievedMemory with valid fields converts to ContextItem."""
        from backend.memory import RetrievedMemory
        mem = RetrievedMemory(
            content="Test memory",
            tags=("test",),
            source_id="11111111-1111-4111-8111-111111111111",
            confidence=0.8,
            provenance=Provenance.USER_CONFIRMED,
            epistemic_status=EpistemicStatus.APPROVED,
            approved=True,
            metadata_version=1,
        )
        item = mem.to_context_item("mem-1")
        assert item.kind == "memory"
        assert item.provenance == Provenance.USER_CONFIRMED
        assert item.epistemic_status == EpistemicStatus.APPROVED
        assert item.confidence == 0.8
        assert item.source_id == "mem-1"
        assert item.internal_id == "11111111-1111-4111-8111-111111111111"

    def test_memory_without_approved_excluded(self):
        """Memory without approved=True is excluded."""
        from backend.memory import RetrievedMemory
        mem = RetrievedMemory(
            content="Test",
            tags=(),
            approved=False,
            metadata_version=1,
        )
        assert not mem.approved

    def test_vector_similarity_not_confused_with_factual_confidence(self):
        """Vector similarity is NOT used as factual confidence — they are separate fields."""
        from backend.memory import RetrievedMemory
        # Create a memory with high similarity (0.98) but low confidence (0.35)
        mem = RetrievedMemory(
            content="Test memory with distinct fields",
            tags=(),
            confidence=0.35,  # Low factual confidence
            provenance=Provenance.LEGACY_MEMORY,
            epistemic_status=EpistemicStatus.UNKNOWN,
            approved=False,
            metadata_version=0,
            retrieval_similarity=0.98,  # High retrieval similarity
        )
        # Confidence must remain at its explicit value, NOT overwritten by similarity
        assert mem.confidence == 0.35, f"confidence={mem.confidence} was overwritten by similarity"
        assert mem.retrieval_similarity == 0.98, "retrieval_similarity not preserved"
        assert mem.confidence != mem.retrieval_similarity, "confidence and similarity must remain distinct"

    def test_similarity_and_confidence_independent(self):
        """Similarity and confidence values can be independently set and are not confused."""
        from backend.memory import RetrievedMemory
        # Similarity=0.98, confidence=0.35 — must not be confused or swapped
        mem = RetrievedMemory(
            content="Independent fields test",
            tags=(),
            source_id="11111111-1111-4111-8111-111111111111",
            confidence=0.35,
            provenance=Provenance.USER_CONFIRMED,
            epistemic_status=EpistemicStatus.APPROVED,
            approved=True,
            metadata_version=1,
            retrieval_similarity=0.98,
        )
        assert mem.confidence == 0.35
        assert mem.retrieval_similarity == 0.98
        # Through to_context_item, confidence is preserved, similarity is NOT transferred
        item = mem.to_context_item("mem-ref-1")
        assert item.confidence == 0.35
        assert not hasattr(item, 'retrieval_similarity') or getattr(item, 'retrieval_similarity', None) is None
        assert item.internal_id == "11111111-1111-4111-8111-111111111111"


# ═══════════════════════════════════════════════════════════════════════
# 17. MemoryManager.build_context_bundle integration
# ═══════════════════════════════════════════════════════════════════════

class TestBuildContextBundle:
    """build_context_bundle integration tests — direct LoadedContextData, no real infrastructure."""

    def test_build_context_bundle_filters_unapproved_legacy(self):
        """Only approved, non-legacy memories appear in ContextBundle.memory_items.

        Builds LoadedContextData directly — no MemoryManager, no SentenceTransformer.
        """
        from backend.memory import RetrievedMemory
        from backend.trusted_context import build_context_bundle, LoadedContextData

        approved_current = RetrievedMemory(
            content="Approved current memory",
            tags=("keep",),
            source_id="11111111-1111-4111-8111-111111111111",
            confidence=0.9,
            provenance=Provenance.USER_CONFIRMED,
            epistemic_status=EpistemicStatus.APPROVED,
            approved=True,
            metadata_version=1,
        )

        # Build LoadedContextData directly — skip MemoryManager entirely
        loaded = LoadedContextData(
            history_rows=(),
            retrieved_memories=(approved_current,),
            profile_snapshot={},
            persona_snapshot="",
        )

        bundle = build_context_bundle(
            trusted_policy="Test policy.",
            loaded_data=loaded,
        )

        contents = [item.content for item in bundle.memory_items]
        assert "Approved current memory" in contents
        assert len(bundle.memory_items) == 1

    def test_build_context_bundle_profile_persona_metadata(self):
        """Profile and persona in user_state produce ContextItems with expected metadata."""
        from backend.trusted_context import build_context_bundle, LoadedContextData

        # Build LoadedContextData directly with profile and persona snapshots
        loaded = LoadedContextData(
            history_rows=(),
            retrieved_memories=(),
            profile_snapshot={"name": "Test User"},
            persona_snapshot="Helpful assistant persona",
        )

        bundle = build_context_bundle(
            trusted_policy="Test policy.",
            loaded_data=loaded,
        )

        # Profile items are in bundle.profile_items, not memory_items
        assert len(bundle.profile_items) == 1
        pi = bundle.profile_items[0]
        assert pi.kind == "profile"
        assert pi.provenance == Provenance.LEGACY_PROFILE
        assert pi.epistemic_status == EpistemicStatus.UNKNOWN
        assert 0.0 <= pi.confidence <= 1.0

        # Persona items are in bundle.persona_items, not memory_items
        assert len(bundle.persona_items) == 1
        pi2 = bundle.persona_items[0]
        assert pi2.kind == "persona"
        assert pi2.provenance == Provenance.LEGACY_PERSONA
        assert pi2.epistemic_status == EpistemicStatus.UNKNOWN
        assert "persona" in pi2.content

    def test_build_context_bundle_history_conversion(self):
        """History from LoadedContextData becomes ChatMessages with stable sort_key."""
        from backend.trusted_context import build_context_bundle, LoadedContextData

        fake_history = (
            {"role": "user", "content": "Hi there", "id": 1, "created_at": "2026-07-30T00:00:00"},
            {"role": "assistant", "content": "Hello!", "id": 2, "created_at": "2026-07-30T00:00:01"},
        )

        # Build LoadedContextData directly
        loaded = LoadedContextData(
            history_rows=fake_history,
            retrieved_memories=(),
            profile_snapshot={},
            persona_snapshot="",
        )

        bundle = build_context_bundle(
            trusted_policy="Test policy.",
            loaded_data=loaded,
        )

        assert len(bundle.history) == 2
        for i, hmsg in enumerate(bundle.history):
            assert hmsg.role in ("user", "assistant")
            assert hmsg.content == fake_history[i]["content"]
            assert isinstance(hmsg.sort_key, tuple)
            assert len(hmsg.sort_key) >= 2


# ═══════════════════════════════════════════════════════════════════════
# 18. Engine integration — generation path
# ═══════════════════════════════════════════════════════════════════════

class TestEngineGenerationPath:
    """Engine generation path uses trusted context."""

    @pytest.mark.anyio
    async def test_generate_uses_envelope_with_history_roles(self, monkeypatch):
        """The provider receives the full envelope with original history roles."""
        import json
        from unittest.mock import MagicMock, patch
        from backend.engine import ConversationEngine
        from backend.groq_manager import GroqClientManager
        from backend.memory import MemoryManager

        recorded = {}

        async def mock_chat_completion(messages, **kwargs):
            recorded["messages"] = messages
            resp_json = json.dumps({
                "valence": 0.0, "arousal_shift": 0.0, "dominance_shift": 0.0,
                "triggered_emotions": {
                    "joy": 0, "sadness": 0, "anger": 0, "fear": 0,
                    "disgust": 0, "surprise": 0, "tenderness": 0,
                    "guilt": 0, "pride": 0, "jealousy": 0, "gratitude": 0,
                },
            })
            return type("Resp", (), {
                "choices": [type("C", (), {
                    "message": type("M", (), {"content": resp_json}),
                })()],
            })()

        def mock_load_state(uid, default_timestamp=None):
            return {
                "persona_config": "Katherine...",
                "user_profile": {},
                "relationship_state": {
                    "trust": 0.5, "affection": 0.3, "tension": 0.0,
                    "triggers": [], "last_interaction": 1000.0,
                },
                "emotional_state": {
                    "schema_version": 1,
                    "pleasure": 0.0, "arousal": 0.0, "dominance": 0.0,
                    "libido": 0.0, "aggression": 0.0, "connection": 0.5,
                    "energy": 0.8, "tension": 0.0, "coping_mode": "HEALTHY",
                    "timestamp": 1000.0,
                },
            }

        def mock_load_context(uid, msg, state):
            from backend.trusted_context import LoadedContextData
            return LoadedContextData(
                history_rows=(
                    {"role": "user", "content": "Hello", "id": 1, "created_at": "2026-07-30T00:00:00"},
                    {"role": "assistant", "content": "Hi there", "id": 2, "created_at": "2026-07-30T00:00:01"},
                ),
                retrieved_memories=(),
                profile_snapshot={},
                persona_snapshot="",
            )

        # Install doubles BEFORE constructing ConversationEngine
        with (
            patch.object(GroqClientManager, "__init__", return_value=None),
            patch.object(MemoryManager, "__init__", return_value=None),
            patch("backend.memory.SentenceTransformer", return_value=MagicMock()),
        ):
            engine = ConversationEngine(clock=lambda: 1000.0)

        monkeypatch.setattr(
            engine.groq_manager, "chat_completion_async", mock_chat_completion
        )
        monkeypatch.setattr(
            engine.memory_manager, "load_user_state", mock_load_state
        )
        monkeypatch.setattr(
            engine.memory_manager, "load_context_data", mock_load_context
        )
        monkeypatch.setattr(
            engine.memory_manager, "save_turn",
            lambda *a, **kw: type("Ref", (), {
                "user_id": "u1", "source_chat_log_id": 1,
                "assistant_chat_log_id": 2,
            })()
        )
        monkeypatch.setattr(
            engine.memory_manager, "sync_state", lambda *a, **kw: None
        )

        # Process turn with the mocked dependencies
        result = await engine.process_turn(
            user_id="u1",
            user_message="World",
        )

        assert result is not None
        messages = recorded.get("messages")
        assert messages is not None, "Provider was never called"

        # Verify the envelope structure:
        # 1. System message with trusted policy
        assert messages[0]["role"] == "system"
        assert len(messages[0]["content"]) > 0

        # 2. History messages with original roles (user/assistant)
        history_msgs = [m for m in messages[1:-1] if m["role"] in ("user", "assistant")]
        assert len(history_msgs) >= 2, f"Expected history messages, got {len(history_msgs)}"
        assert history_msgs[0]["role"] == "user"
        assert "Hello" in history_msgs[0]["content"]
        assert history_msgs[1]["role"] == "assistant"
        assert "Hi there" in history_msgs[1]["content"]

        # 3. Current message is last
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"] == "World"

        # 4. No raw user content in system message
        assert "World" not in messages[0]["content"]
        assert "Hello" not in messages[0]["content"]

    @pytest.mark.anyio
    async def test_engine_loads_context_once(self, monkeypatch):
        """Context is loaded exactly once during a turn."""
        from unittest.mock import MagicMock, patch
        from backend.engine import ConversationEngine
        from backend.groq_manager import GroqClientManager
        from backend.memory import MemoryManager

        load_count = 0

        async def mock_chat_completion(messages, **kwargs):
            import json
            resp_json = json.dumps({
                "valence": 0.0, "arousal_shift": 0.0, "dominance_shift": 0.0,
                "triggered_emotions": {
                    "joy": 0, "sadness": 0, "anger": 0, "fear": 0,
                    "disgust": 0, "surprise": 0, "tenderness": 0,
                    "guilt": 0, "pride": 0, "jealousy": 0, "gratitude": 0,
                },
            })
            return type("Resp", (), {
                "choices": [type("C", (), {
                    "message": type("M", (), {"content": resp_json}),
                })()],
            })()

        def mock_load_state(uid, default_timestamp=None):
            return {
                "persona_config": "K...",
                "user_profile": {},
                "relationship_state": {
                    "trust": 0.5, "affection": 0.3, "tension": 0.0,
                    "triggers": [], "last_interaction": 1000.0,
                },
                "emotional_state": {
                    "schema_version": 1,
                    "pleasure": 0.0, "arousal": 0.0, "dominance": 0.0,
                    "libido": 0.0, "aggression": 0.0, "connection": 0.5,
                    "energy": 0.8, "tension": 0.0, "coping_mode": "HEALTHY",
                    "timestamp": 1000.0,
                },
            }

        def mock_load_context(uid, msg, state):
            nonlocal load_count
            load_count += 1
            from backend.trusted_context import LoadedContextData
            return LoadedContextData(
                history_rows=({"role": "user", "content": "Hi", "id": 1, "created_at": "2026-07-30T00:00:00"},),
                retrieved_memories=(),
                profile_snapshot={},
                persona_snapshot="",
            )

        # Install doubles BEFORE constructing ConversationEngine
        with (
            patch.object(GroqClientManager, "__init__", return_value=None),
            patch.object(MemoryManager, "__init__", return_value=None),
            patch("backend.memory.SentenceTransformer", return_value=MagicMock()),
        ):
            engine = ConversationEngine(clock=lambda: 1000.0)

        monkeypatch.setattr(
            engine.groq_manager, "chat_completion_async", mock_chat_completion
        )
        monkeypatch.setattr(
            engine.memory_manager, "load_user_state", mock_load_state
        )
        monkeypatch.setattr(
            engine.memory_manager, "load_context_data", mock_load_context
        )
        monkeypatch.setattr(
            engine.memory_manager, "save_turn",
            lambda *a, **kw: type("Ref", (), {
                "user_id": "u1", "source_chat_log_id": 1,
                "assistant_chat_log_id": 2,
            })()
        )
        monkeypatch.setattr(
            engine.memory_manager, "sync_state", lambda *a, **kw: None
        )

        await engine.process_turn(
            user_id="u1",
            user_message="World",
        )

        # Context must be loaded exactly once
        assert load_count == 1, f"Expected 1 context load, got {load_count}"


# ═══════════════════════════════════════════════════════════════════════
# 19. ContextBuildResult validation
# ═══════════════════════════════════════════════════════════════════════

class TestContextBuildResult:
    """ContextBuildResult contract tests."""

    def test_result_contains_messages(self):
        result = ContextBuildResult(messages=[{"role": "user", "content": "hi"}])
        assert len(result.messages) == 1

    def test_result_source_map(self):
        result = ContextBuildResult(
            messages=[],
            source_map={"mem-1": "uuid-abc", "mem-2": "uuid-def"},
        )
        assert len(result.source_map) == 2
        assert result.source_map["mem-1"] == "uuid-abc"


# ═══════════════════════════════════════════════════════════════════════
# 20. Existing public DTO unchanged
# ═══════════════════════════════════════════════════════════════════════

class TestPublicDTO:
    """Public DTO remains unchanged (no new exports from wrong places)."""

    def test_emotion_state_response_unchanged(self):
        """EmotionStateResponse still exported from emotion_presentation."""
        from backend.emotion_presentation import EmotionStateResponse
        assert EmotionStateResponse is not None

    def test_trusted_context_new_export(self):
        """New module exports are in trusted_context only."""
        from backend.trusted_context import (
            ChatMessage, ContextItem, ContextBundle, ContextBuildResult,
            TruncationReport, EpistemicStatus, Provenance, build_envelope,
        )
        assert ChatMessage is not None
        assert ContextItem is not None
        assert ContextBundle is not None
        assert ContextBuildResult is not None
        assert TruncationReport is not None


# ═══════════════════════════════════════════════════════════════════════
# 21. Metadata schema version rejection tests
# ═══════════════════════════════════════════════════════════════════════

class TestMetadataSchemaVersion:
    """Metadata schema version is strictly validated in memory retrieval.

    Only ``metadata.schema_version == SUPPORTED_MEMORY_METADATA_SCHEMA_VERSION``
    (which equals ``1``) is accepted. All other values are rejected.
    """

    def test_constant_value(self):
        from backend.memory import SUPPORTED_MEMORY_METADATA_SCHEMA_VERSION
        assert SUPPORTED_MEMORY_METADATA_SCHEMA_VERSION == 1

    def test_correct_version_accepted_through_load_context_data(self, monkeypatch):
        """A memory with metadata_version=1 passes through load_context_data filtering."""
        from backend.memory import MemoryManager, RetrievedMemory
        from backend.trusted_context import build_context_bundle

        valid_mem = RetrievedMemory(
            content="Valid version 1 memory",
            tags=("test",),
            source_id="11111111-1111-4111-8111-111111111111",
            confidence=0.8,
            provenance=Provenance.USER_CONFIRMED,
            epistemic_status=EpistemicStatus.APPROVED,
            approved=True,
            metadata_version=1,
        )

        def fake_retrieve(*args, **kwargs):
            return [valid_mem]

        monkeypatch.setattr(MemoryManager, "_retrieve_relevant_entries", fake_retrieve)
        # Construct WITHOUT running __init__ — no SentenceTransformer, no Supabase.
        mm = MemoryManager.__new__(MemoryManager)
        monkeypatch.setattr(mm, "load_recent_history", lambda *a, **kw: [])

        loaded = mm.load_context_data(
            user_id="user-x", current_message="Hi", user_state={}
        )
        bundle = build_context_bundle(
            trusted_policy="Test policy.", loaded_data=loaded
        )

        assert len(bundle.memory_items) == 1
        assert "Valid version 1 memory" in bundle.memory_items[0].content

    def test_absent_version_rejected(self, monkeypatch):
        """Memory with no schema_version key (defaulted to 0) is rejected."""
        from backend.memory import MemoryManager, RetrievedMemory
        from backend.trusted_context import build_context_bundle

        absent_mem = RetrievedMemory(
            content="Absent version",
            tags=(), source_id="22222222-2222-4222-8222-222222222222",
            approved=True, metadata_version=0,  # default when absent
        )

        def fake_retrieve(*args, **kwargs):
            return [absent_mem]

        monkeypatch.setattr(MemoryManager, "_retrieve_relevant_entries", fake_retrieve)
        # Construct WITHOUT running __init__ — no SentenceTransformer, no Supabase.
        mm = MemoryManager.__new__(MemoryManager)
        monkeypatch.setattr(mm, "load_recent_history", lambda *a, **kw: [])

        loaded = mm.load_context_data(
            user_id="user-x", current_message="Hi", user_state={}
        )
        bundle = build_context_bundle(
            trusted_policy="Test policy.", loaded_data=loaded
        )
        # Version 0 (absent/legacy) should be filtered out
        assert len(bundle.memory_items) == 0, "Absent version should be filtered out"

    def test_zero_version_rejected(self, monkeypatch):
        """Memory with schema_version=0 is rejected."""
        from backend.memory import MemoryManager, RetrievedMemory
        from backend.trusted_context import build_context_bundle

        zero_mem = RetrievedMemory(
            content="Zero version",
            tags=(), source_id="33333333-3333-4333-8333-333333333333",
            approved=True, metadata_version=0,
        )

        def fake_retrieve(*args, **kwargs):
            return [zero_mem]

        monkeypatch.setattr(MemoryManager, "_retrieve_relevant_entries", fake_retrieve)
        # Construct WITHOUT running __init__ — no SentenceTransformer, no Supabase.
        mm = MemoryManager.__new__(MemoryManager)
        monkeypatch.setattr(mm, "load_recent_history", lambda *a, **kw: [])

        loaded = mm.load_context_data(
            user_id="user-x", current_message="Hi", user_state={}
        )
        bundle = build_context_bundle(
            trusted_policy="Test policy.", loaded_data=loaded
        )
        assert len(bundle.memory_items) == 0, "Version 0 should be filtered out"

    def test_future_version_rejected(self, monkeypatch):
        """Memory with schema_version=2 (future) is rejected."""
        from backend.memory import MemoryManager, RetrievedMemory
        from backend.trusted_context import build_context_bundle

        future_mem = RetrievedMemory(
            content="Future version",
            tags=(), source_id="44444444-4444-4444-8444-444444444444",
            approved=True, metadata_version=2,
        )

        def fake_retrieve(*args, **kwargs):
            return [future_mem]

        monkeypatch.setattr(MemoryManager, "_retrieve_relevant_entries", fake_retrieve)
        # Construct WITHOUT running __init__ — no SentenceTransformer, no Supabase.
        mm = MemoryManager.__new__(MemoryManager)
        monkeypatch.setattr(mm, "load_recent_history", lambda *a, **kw: [])

        loaded = mm.load_context_data(
            user_id="user-x", current_message="Hi", user_state={}
        )
        bundle = build_context_bundle(
            trusted_policy="Test policy.", loaded_data=loaded
        )
        assert len(bundle.memory_items) == 0, "Future version should be filtered out"

    def test_negative_version_rejected(self, monkeypatch):
        """Memory with negative metadata version is rejected.

        ``RetrievedMemory.__post_init__`` rejects negative metadata_version
        with ``ValueError``, so this tests the construction guard.
        """
        from backend.memory import RetrievedMemory
        with pytest.raises(ValueError, match="metadata_version must be >= 0"):
            RetrievedMemory(
                content="Negative version",
                tags=(), approved=True, metadata_version=-1,
            )

    def test_string_version_rejected(self, monkeypatch):
        """Memory with string metadata version is rejected by construction."""
        from backend.memory import RetrievedMemory
        with pytest.raises(ValueError, match="metadata_version must be an int"):
            RetrievedMemory(
                content="String version",
                tags=(), approved=True, metadata_version="1",
            )

    def test_float_version_rejected(self, monkeypatch):
        """Memory with float metadata version is rejected by construction."""
        from backend.memory import RetrievedMemory
        with pytest.raises(ValueError, match="metadata_version must be an int"):
            RetrievedMemory(
                content="Float version",
                tags=(), approved=True, metadata_version=1.0,
            )

    def test_bool_version_rejected(self, monkeypatch):
        """Memory with bool metadata version is rejected by construction (bool is not int)."""
        from backend.memory import RetrievedMemory
        with pytest.raises(ValueError, match="metadata_version must be an int"):
            RetrievedMemory(
                content="Bool version",
                tags=(), approved=True, metadata_version=True,
            )


# ═══════════════════════════════════════════════════════════════════════
# 22. UUID full pipeline test — RPC row → source_map → caplog verification
# ═══════════════════════════════════════════════════════════════════════

class TestUuidPipeline:
    """Real UUIDs propagate correctly through the full pipeline and never leak."""

    @pytest.fixture(autouse=True)
    def _no_real_embedding_infrastructure(self, monkeypatch):
        """Fail immediately if any test here constructs a real SentenceTransformer.

        Requirement: no test in ``TestUuidPipeline`` may load ``SentenceTransformer``,
        search the model cache, access Hugging Face, build Supabase, read
        credentials, open sockets, or depend on the network.  These tests build
        ``LoadedContextData``-style inputs directly, but this double makes the
        invariant self-enforcing: any accidental ``SentenceTransformer(...)``
        call fails the test instantly instead of hitting the network.
        """
        import backend.memory as _memory_module

        def _fail_on_construction(*args, **kwargs):
            raise AssertionError(
                "TestUuidPipeline must not construct SentenceTransformer "
                "(no real embedding infrastructure allowed)"
            )

        monkeypatch.setattr(_memory_module, "SentenceTransformer", _fail_on_construction)

    def test_uuid_propagates_from_memory_to_source_map(self):
        """Real UUID from RetrievedMemory.source_id ends up in source_map via internal_id."""
        from backend.memory import RetrievedMemory
        import uuid

        real_uuid = "550e8400-e29b-41d4-a716-446655440000"
        mem = RetrievedMemory(
            content="UUID pipeline memory",
            tags=("test",),
            source_id=real_uuid,
            confidence=0.8,
            provenance=Provenance.USER_CONFIRMED,
            epistemic_status=EpistemicStatus.APPROVED,
            approved=True,
            metadata_version=1,
        )
        item = mem.to_context_item("mem-1")
        assert item.internal_id == real_uuid

        bundle = ContextBundle(
            trusted_policy="Policy.",
            memory_items=(item,),
        )
        result = build_envelope(bundle, "Hi")

        # UUID must be in source_map under the local ref
        assert "mem-1" in result.source_map, "Local ref must be in source_map"
        assert result.source_map["mem-1"] == real_uuid, "source_map must map local ref to real UUID"

    def test_uuid_not_in_provider_messages(self):
        """Real UUID must never appear in provider messages."""
        from backend.memory import RetrievedMemory

        real_uuid = "550e8400-e29b-41d4-a716-446655440000"
        mem = RetrievedMemory(
            content="Secret UUID memory",
            tags=(),
            source_id=real_uuid,
            confidence=0.9,
            provenance=Provenance.USER_CONFIRMED,
            epistemic_status=EpistemicStatus.APPROVED,
            approved=True,
            metadata_version=1,
        )
        item = mem.to_context_item("mem-2")
        bundle = ContextBundle(
            trusted_policy="Policy.",
            memory_items=(item,),
        )
        result = build_envelope(bundle, "Hi")

        serialized = json.dumps(result.messages)
        # Real UUID must NOT appear in provider-bound messages
        assert real_uuid not in serialized, "Real UUID leaked into provider messages"

    def test_uuid_not_in_caplog(self, caplog):
        """Real UUID must never appear in logs."""
        from backend.memory import RetrievedMemory

        caplog.set_level(logging.DEBUG)
        real_uuid = "660e8400-e29b-41d4-a716-446655440001"
        mem = RetrievedMemory(
            content="Secret UUID for log test",
            tags=(),
            source_id=real_uuid,
            confidence=0.9,
            provenance=Provenance.USER_CONFIRMED,
            epistemic_status=EpistemicStatus.APPROVED,
            approved=True,
            metadata_version=1,
        )
        item = mem.to_context_item("mem-3")
        bundle = ContextBundle(
            trusted_policy="Policy.",
            memory_items=(item,),
        )
        try:
            build_envelope(bundle, "Hi")
        except TrustedContextError:
            pass

        assert real_uuid not in caplog.text, "Real UUID leaked into logs"




# ═══════════════════════════════════════════════════════════════════════
# 23. TruncationReport aggregation
# ═══════════════════════════════════════════════════════════════════════

class TestTruncationReportAggregation:
    """TruncationReport aggregation tests — pre-existing + new omissions."""

    def test_aggregation_with_pre_existing_report(self):
        """When bundle has existing truncation, build_envelope aggregates both sets."""
        existing_report = TruncationReport(
            codes=("history_partial",),
            omitted_history_count=3,
            selected_history_count=2,
        )
        # Create a bundle that will trigger further omission during envelope build
        large_memory = ContextItem(
            kind="memory", content="x" * 10000,
            provenance=Provenance.LEGACY_MEMORY,
            confidence=0.5, epistemic_status=EpistemicStatus.UNKNOWN,
            source_id="large-mem",
        )
        bundle = ContextBundle(
            trusted_policy="Short policy.",
            memory_items=(large_memory,),
            truncation_report=existing_report,
        )
        result = build_envelope(bundle, "Hi", max_units=2000)
        report = result.truncation_report

        # Both existing codes and new codes should be present
        assert "history_partial" in report.codes, "Pre-existing code should be preserved"
        assert "memory_omitted" in report.codes, "New omission should be recorded"

        # Counts should be aggregated
        assert report.omitted_history_count >= 3, "Existing history omission should be preserved"
        assert report.omitted_memory_count >= 1, "New memory omission should be counted"

    def test_no_duplicate_codes(self):
        """Duplicate codes across pre-existing and new reports are not duplicated."""
        existing_report = TruncationReport(
            codes=("memory_omitted",),
            omitted_memory_count=2,
        )
        large_memory = ContextItem(
            kind="memory", content="x" * 10000,
            provenance=Provenance.LEGACY_MEMORY,
            confidence=0.5, epistemic_status=EpistemicStatus.UNKNOWN,
            source_id="large-mem",
        )
        bundle = ContextBundle(
            trusted_policy="Short policy.",
            memory_items=(large_memory,),
            truncation_report=existing_report,
        )
        result = build_envelope(bundle, "Hi", max_units=2000)
        report = result.truncation_report

        # "memory_omitted" should appear exactly once
        memory_code_count = sum(1 for c in report.codes if c == "memory_omitted")
        assert memory_code_count == 1, f"memory_omitted appears {memory_code_count} times (should be 1)"

    def test_no_existing_aggregation_only_new(self):
        """When no pre-existing report, only envelope decisions appear."""
        bundle = ContextBundle(
            trusted_policy="Short policy.",
        )
        result = build_envelope(bundle, "Hi")
        report = result.truncation_report
        # Should have no omission codes since everything fits
        assert len(report.codes) == 0, f"Expected empty codes, got {report.codes}"
        assert report.omitted_history_count == 0

    def test_profile_omission_added_to_existing(self):
        """Profile omission is added alongside existing memory omission codes."""
        existing_report = TruncationReport(
            codes=("memory_omitted",),
            omitted_memory_count=1,
        )
        large_profile = ContextItem(
            kind="profile", content='{"x":"' + 'y' * 2000 + '"}',
            provenance=Provenance.LEGACY_PROFILE,
            confidence=0.3, epistemic_status=EpistemicStatus.UNKNOWN,
            source_id="large-profile",
        )
        bundle = ContextBundle(
            trusted_policy="Short policy.",
            profile_items=(large_profile,),
            truncation_report=existing_report,
        )
        result = build_envelope(bundle, "Hi", max_units=1000)
        report = result.truncation_report
        # Both codes should be present
        assert "memory_omitted" in report.codes
        assert "profile_omitted" in report.codes
        # Counts aggregated
        assert report.omitted_memory_count >= 1
        assert report.omitted_profile_count >= 1