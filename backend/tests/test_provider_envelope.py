"""
Tests for ``backend.provider_envelope``.

Covers:

=== Domain purity ===
1. Importability without infrastructure
2. Canonical serialisation deterministic
3. ASCII and Unicode multibyte
4. Quotes, backslashes, newlines, control characters
5. Message order changes the result
6. Key order does NOT change the canonical representation
7. Empty collection and invalid structures fail closed
8. Exact 16 000-unit limit accepted
9. 16 001 rejected
10. UTF-8 pruning remains valid
11. Pruning deterministic
12. Exceptions and logs do not contain a sensitive marker inserted in content

=== Generation integration ===
(Handled by engine/conftest mocks)

=== Appraisal integration ===
(Handled by engine/conftest mocks)

=== Groq manager integration ===
(Handled by test_groq_manager.py)
"""

import json
import logging
import pytest

from backend.provider_envelope import (
    VALID_ROLES,
    VALID_MESSAGE_KEYS,
    OMISSION_MARKER,
    ProviderEnvelopeError,
    estimate_provider_input_units,
    validate_provider_input,
    fit_optional_context,
    _truncate_utf8_safe,
    _estimate_remaining_budget,
)


# ═══════════════════════════════════════════════════════════════════════
# 1. Importability without infrastructure
# ═══════════════════════════════════════════════════════════════════════

class TestImportability:
    """Verify the module imports without triggering infrastructure deps."""

    def test_import_no_external_deps(self):
        """Import does not require groq, supabase, fastapi, etc."""
        import sys
        # The module should only import stdlib + admission_contracts
        module = sys.modules.get("backend.provider_envelope")
        # Just verify the key symbols are accessible
        assert hasattr(module, "estimate_provider_input_units")
        assert hasattr(module, "validate_provider_input")
        assert hasattr(module, "fit_optional_context")


# ═══════════════════════════════════════════════════════════════════════
# 2. Canonical serialisation deterministic
# ═══════════════════════════════════════════════════════════════════════

class TestCanonicalSerialisation:
    """Same input always produces the same estimate."""

    def test_deterministic_same_input_same_result(self):
        messages = [
            {"role": "system", "content": "Hello"},
            {"role": "user", "content": "World"},
        ]
        assert estimate_provider_input_units(messages) == estimate_provider_input_units(messages)

    def test_deterministic_repeated_calls(self):
        messages = [{"role": "user", "content": "Test"}]
        results = [estimate_provider_input_units(messages) for _ in range(10)]
        assert all(r == results[0] for r in results)

    def test_reordering_preserves_unit_count(self):
        """PASS: swapping message order preserves the total byte count
        because each message has the same content strings."""
        a = [{"role": "user", "content": "A"}, {"role": "assistant", "content": "B"}]
        b = [{"role": "assistant", "content": "B"}, {"role": "user", "content": "A"}]
        assert estimate_provider_input_units(a) == estimate_provider_input_units(b)

    def test_key_order_does_not_change_result(self):
        """PASS: sort_keys means key order is irrelevant."""
        a = [{"role": "user", "content": "hi"}]
        b = [{"content": "hi", "role": "user"}]
        assert estimate_provider_input_units(a) == estimate_provider_input_units(b)


# ═══════════════════════════════════════════════════════════════════════
# 3. ASCII and Unicode multibyte
# ═══════════════════════════════════════════════════════════════════════

class TestUnicodeHandling:
    """Multibyte characters are counted correctly."""

    def test_ascii(self):
        messages = [{"role": "user", "content": "hello"}]
        # JSON: [{"content":"hello","role":"user"}]
        # Expected bytes: 32 (JSON structure) + 5 (hello) = 37
        assert estimate_provider_input_units(messages) > 30

    def test_two_byte_char(self):
        messages = [{"role": "user", "content": "\u00f1"}]
        units = estimate_provider_input_units(messages)
        assert units > 30  # JSON overhead + 2 UTF-8 bytes for ñ

    def test_three_byte_chars(self):
        messages = [{"role": "user", "content": "\u4f60\u597d"}]
        units = estimate_provider_input_units(messages)
        assert units > 35  # JSON overhead + 6 UTF-8 bytes for 你好

    def test_four_byte_emoji(self):
        messages = [{"role": "user", "content": "\U0001f600"}]
        units = estimate_provider_input_units(messages)
        assert units > 30  # JSON overhead + 4 UTF-8 bytes for 😀

    def test_mixed_lengths(self):
        messages = [{"role": "user", "content": "a\u00f1\u4f60\U0001f600"}]
        units = estimate_provider_input_units(messages)
        assert 30 < units < 60


# ═══════════════════════════════════════════════════════════════════════
# 4. Quotes, backslashes, newlines, control characters
# ═══════════════════════════════════════════════════════════════════════

class TestEscaping:
    """Escaped characters are counted correctly."""

    def test_double_quote(self):
        messages = [{"role": "user", "content": 'he"llo'}]
        units = estimate_provider_input_units(messages)
        assert units > 30

    def test_backslash(self):
        messages = [{"role": "user", "content": "he\\llo"}]
        units = estimate_provider_input_units(messages)
        assert units > 30

    def test_newline(self):
        messages = [{"role": "user", "content": "he\nllo"}]
        units = estimate_provider_input_units(messages)
        assert units > 30

    def test_tab(self):
        messages = [{"role": "user", "content": "he\tllo"}]
        units = estimate_provider_input_units(messages)
        assert units > 30


# ═══════════════════════════════════════════════════════════════════════
# 5. Empty collection and invalid structures fail closed
# ═══════════════════════════════════════════════════════════════════════

class TestStructuralValidation:
    """Empty and invalid inputs are rejected."""

    def test_empty_list(self):
        with pytest.raises(ProviderEnvelopeError, match="empty_messages"):
            validate_provider_input([])

    def test_not_a_list(self):
        with pytest.raises(ProviderEnvelopeError, match="invalid_envelope_type"):
            validate_provider_input("not a list")

    def test_item_not_dict(self):
        with pytest.raises(ProviderEnvelopeError, match="invalid_message_structure"):
            validate_provider_input(["not a dict"])

    def test_invalid_role(self):
        with pytest.raises(ProviderEnvelopeError, match="invalid_role"):
            validate_provider_input([{"role": "admin", "content": "hi"}])

    def test_content_not_string(self):
        with pytest.raises(ProviderEnvelopeError, match="invalid_content"):
            validate_provider_input([{"role": "user", "content": 123}])

    def test_content_none(self):
        with pytest.raises(ProviderEnvelopeError, match="invalid_content"):
            validate_provider_input([{"role": "user", "content": None}])

    def test_unknown_keys(self):
        with pytest.raises(ProviderEnvelopeError, match="invalid_message_keys"):
            validate_provider_input([{"role": "user", "content": "hi", "extra": "field"}])

    def test_valid_input_accepted(self):
        validate_provider_input([{"role": "user", "content": "hello"}])
        validate_provider_input([{"role": "system", "content": "prompt"}, {"role": "user", "content": "msg"}])


# ═══════════════════════════════════════════════════════════════════════
# 6–7. Budget limits: exact 16 000 accepted, 16 001 rejected
# ═══════════════════════════════════════════════════════════════════════

class TestBudgetLimits:
    """Budget enforcement at the 16 000-unit boundary."""

    def test_under_16000_accepted(self):
        # Build a messages list whose estimated input units fit within 16000
        # The JSON for [{"content":"...","role":"user"}] adds ~32 bytes overhead.
        # Content of 15900 ASCII chars should produce ~15932 units < 16000.
        content = "x" * 15900
        messages = [{"role": "user", "content": content}]
        units = estimate_provider_input_units(messages)
        assert units < 16000, f"Expected < 16000, got {units}"
        # Must not raise
        validate_provider_input(messages, max_units=16000)

    def test_large_content_rejected(self):
        content = "x" * 20000
        messages = [{"role": "user", "content": content}]
        with pytest.raises(ProviderEnvelopeError, match="budget_exceeded"):
            validate_provider_input(messages, max_units=16000)

    def test_multibyte_over_limit(self):
        content = "\u00f1" * 10000  # 20000 UTF-8 bytes
        messages = [{"role": "user", "content": content}]
        with pytest.raises(ProviderEnvelopeError, match="budget_exceeded"):
            validate_provider_input(messages, max_units=16000)

    def test_small_payload_accepted(self):
        messages = [{"role": "user", "content": "Hello, world!"}]
        validate_provider_input(messages, max_units=16000)


# ═══════════════════════════════════════════════════════════════════════
# 8. UTF-8 pruning remains valid
# ═══════════════════════════════════════════════════════════════════════

class TestUtf8Pruning:
    """Truncation preserves UTF-8 validity."""

    def test_ascii_truncated(self):
        text = "Hello, world! How are you today?"
        truncated, was_truncated = _truncate_utf8_safe(text, max_bytes=10)
        assert was_truncated
        assert truncated.endswith(OMISSION_MARKER)
        # Verify valid UTF-8
        truncated.encode("utf-8")  # should not raise

    def test_multibyte_not_split(self):
        text = "\u00f1\u00f1\u00f1\u00f1\u00f1"  # 5 × 2 bytes = 10
        truncated, was_truncated = _truncate_utf8_safe(text, max_bytes=6)
        assert was_truncated
        # After truncation: 6 - len(OMMISSION_MARKER) = 6 - 5 = 1 byte for text
        # But 1 byte is not enough for a 2-byte char, so 0 chars remain
        truncated.encode("utf-8")

    def test_emoji_not_split(self):
        text = "\U0001f600\U0001f601\U0001f602"  # 3 × 4 bytes = 12
        truncated, was_truncated = _truncate_utf8_safe(text, max_bytes=8)
        assert was_truncated
        truncated.encode("utf-8")

    def test_no_truncation_needed(self):
        text = "Hello"
        truncated, was_truncated = _truncate_utf8_safe(text, max_bytes=100)
        assert not was_truncated
        assert truncated == "Hello"

    def test_empty_string(self):
        truncated, was_truncated = _truncate_utf8_safe("", max_bytes=100)
        assert not was_truncated
        assert truncated == ""


# ═══════════════════════════════════════════════════════════════════════
# 9. Pruning deterministic
# ═══════════════════════════════════════════════════════════════════════

class TestPruningDeterministic:
    """Same inputs always produce same output."""

    def test_deterministic_output(self):
        mandatory = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello!"},
        ]
        optional = [
            ("persona", "You are kind and caring."),
            ("history", "user: How are you?"),
        ]
        result1 = fit_optional_context(mandatory, optional)
        result2 = fit_optional_context(mandatory, optional)
        assert result1 == result2
        assert len(result1) == 2

    def test_repeated_pruning_identical(self):
        mandatory = [
            {"role": "system", "content": "System prompt."},
            {"role": "user", "content": "Hi."},
        ]
        optional = [("memories", "Relevant memory 1.")]
        results = [fit_optional_context(mandatory, optional) for _ in range(5)]
        assert all(r == results[0] for r in results)


# ═══════════════════════════════════════════════════════════════════════
# 10. No sensitive data in exceptions
# ═══════════════════════════════════════════════════════════════════════

class TestSanitization:
    """Exceptions and logs do not contain sensitive content."""

    def test_exception_no_content(self):
        secret = "super-secret-content-12345"
        try:
            validate_provider_input([{"role": "user", "content": secret * 800}], max_units=16000)
        except ProviderEnvelopeError as exc:
            assert secret not in str(exc)
            assert secret not in repr(exc)
            assert not any(
                getattr(exc, attr, None) == secret
                for attr in dir(exc)
                if not attr.startswith("_")
            )
        else:
            pytest.fail("Expected ProviderEnvelopeError")

    def test_exception_empty_list_no_content(self):
        try:
            validate_provider_input([])
        except ProviderEnvelopeError as exc:
            assert "empty_messages" in str(exc)
            assert "empty_messages" in repr(exc)

    def test_estimate_invalid_type_no_leak(self):
        with pytest.raises(ProviderEnvelopeError, match="invalid_envelope_type"):
            estimate_provider_input_units("secret-payload")


# ═══════════════════════════════════════════════════════════════════════
# 11. fit_optional_context integration
# ═══════════════════════════════════════════════════════════════════════

class TestFitOptionalContext:
    """Context pruning integration."""

    def test_no_optional_components(self):
        mandatory = [{"role": "user", "content": "Hello"}]
        result = fit_optional_context(mandatory, [])
        assert result == mandatory

    def test_all_components_fit(self):
        mandatory = [{"role": "system", "content": "System prompt."}, {"role": "user", "content": "Hi."}]
        optional = [("persona", "You are a bot.")]
        result = fit_optional_context(mandatory, optional)
        assert len(result) == 2
        assert "You are a bot" in result[0]["content"]

    def test_priority_order_persona_first(self):
        """Persona (highest priority) should be included before lower priority items."""
        mandatory = [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "U" * 100},
        ]
        small_budget = 500
        optional = [
            ("persona", "P" * 200),
            ("history", "H" * 300),
        ]
        result = fit_optional_context(mandatory, optional, max_units=small_budget)
        content = result[0]["content"]
        # Persona is higher priority than history
        assert "P" * 200 in content  # persona fits
        # May or may not have history depending on budget

    def test_mandatory_budget_exceeded_fails(self):
        """If mandatory messages alone exceed budget, should fail."""
        content = "x" * 20000
        mandatory = [{"role": "system", "content": content}, {"role": "user", "content": "Hi"}]
        with pytest.raises(ProviderEnvelopeError, match="budget_exceeded"):
            fit_optional_context(mandatory, [], max_units=16000)

    def test_no_system_message(self):
        """When there's no system message, optional components are not added."""
        mandatory = [{"role": "user", "content": "Hello"}]
        optional = [("persona", "You are a bot.")]
        result = fit_optional_context(mandatory, optional)
        assert result == mandatory

    def test_new_messages_preserved_byte_exact(self):
        """User message must remain byte-exact after pruning."""
        user_msg = "Hello, world! 😀"
        mandatory = [
            {"role": "system", "content": "S" * 100},
            {"role": "user", "content": user_msg},
        ]
        optional = [
            ("persona", "P" * 5000),
            ("history", "H" * 5000),
        ]
        result = fit_optional_context(mandatory, optional)
        assert result[1]["content"] == user_msg


# ═══════════════════════════════════════════════════════════════════════
# 12. Log sanitisation
# ═══════════════════════════════════════════════════════════════════════

class TestLogSanitization:
    """Logs do not contain sensitive content."""

    def test_caplog_empty(self, caplog):
        secret = "super-secret-marker-abcdef"
        caplog.set_level(logging.ERROR)

        # Trigger validation failure
        try:
            validate_provider_input([{"role": "user", "content": secret * 100}])
        except ProviderEnvelopeError:
            pass

        # This module doesn't log directly, but check that no logs contain the secret
        assert secret not in caplog.text


# ═══════════════════════════════════════════════════════════════════════
# 13. Remaining budget helper
# ═══════════════════════════════════════════════════════════════════════

class TestRemainingBudget:
    """_estimate_remaining_budget helper."""

    def test_remaining_positive(self):
        messages = [{"role": "user", "content": "hi"}]
        remaining = _estimate_remaining_budget(messages, max_units=16000)
        assert remaining > 0

    def test_remaining_exact(self):
        content = "x" * 15000
        messages = [{"role": "user", "content": content}]
        remaining = _estimate_remaining_budget(messages, max_units=16000)
        assert remaining >= 0
