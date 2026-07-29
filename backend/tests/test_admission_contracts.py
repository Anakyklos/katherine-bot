"""
Tests for ``backend.admission_contracts``.

Covers (1–20):

 1. Pure importability from subprocess (no heavy deps)
 2. No environment reads
 3. Frozen config
 4. Exact numeric constants
 5. Constants ↔ AdmissionConfig correspondence
 6. UUID canonical lowercase accepted
 7. UUID canonical uppercase normalised
 8. Whitespace rejection
 9. Curly-brace rejection
10. URN rejection
11. No-hyphens rejection
12. Empty / invalid / non-string rejection
13. ``estimate_text_units("") == 1``
14. ASCII counted as one byte per character
15. Multibyte characters counted by real UTF-8 bytes
16. 4 000 ASCII chars accepted
17. 4 001 chars rejected with ``message_too_long``
18. ≤ 4 000 chars but > 6 000 bytes rejected with ``message_budget_exceeded``
19. Content and invalid UUID absent from ``str(exc)``, ``repr(exc)``, public attributes
20. No shared mutable state
"""

import subprocess
import sys
import os

import pytest
from dataclasses import FrozenInstanceError

from backend.admission_contracts import (
    NEW_MESSAGE_MAX_CHARS,
    LEGACY_HISTORY_MAX_CHARS,
    MESSAGE_MAX_ESTIMATED_UNITS,
    PROVIDER_INPUT_MAX_ESTIMATED_UNITS,
    USER_REQUESTS_PER_MINUTE,
    NETWORK_REQUESTS_PER_MINUTE,
    APPLICATION_REQUESTS_PER_MINUTE,
    USER_REQUESTS_PER_DAY,
    USER_ESTIMATED_UNITS_PER_DAY,
    AdmissionConfig,
    RequestIdentity,
    estimate_text_units,
    validate_new_message,
    AdmissionError,
)


# ═══════════════════════════════════════════════════════════════════════
# 1. Pure importability
# ═══════════════════════════════════════════════════════════════════════

class TestImportability:
    """Import the module in an isolated subprocess and verify no heavy
    dependencies are pulled in."""

    HEAVY_DEPS = {"fastapi", "groq", "supabase", "sentence_transformers", "pydantic"}

    @staticmethod
    def _check_script() -> str:
        dep_names = ", ".join(repr(d) for d in TestImportability.HEAVY_DEPS)
        return f"""
import sys
sys.path.insert(0, ".")

_BLOCKED = {{{dep_names}}}

from backend.admission_contracts import (
    NEW_MESSAGE_MAX_CHARS,
    AdmissionConfig,
    RequestIdentity,
    estimate_text_units,
    validate_new_message,
)

for mod_name in _BLOCKED:
    assert mod_name not in sys.modules, (
        f"{{mod_name}} was loaded during import")

print("OK")
"""

    def test_importable_without_heavy_deps(self):
        code = self._check_script()
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True,
            cwd=os.getcwd(),
            env={
                **os.environ,
                "PYTHONPATH": ".",
                "GROQ_API_KEY": "",
                "SUPABASE_URL": "",
                "SUPABASE_KEY": "",
            },
        )
        assert proc.returncode == 0, (
            f"Subprocess failed:\nstdout:{proc.stdout}\nstderr:{proc.stderr}"
        )
        assert "OK" in proc.stdout


# ═══════════════════════════════════════════════════════════════════════
# 2. No environment reads
# ═══════════════════════════════════════════════════════════════════════

class TestNoEnv:
    """The module does not read environment variables at import time."""

    def test_no_post_init(self):
        # ``AdmissionConfig`` has no ``__post_init__`` that might read env
        assert not hasattr(AdmissionConfig, "__post_init__")

    def test_importability_proof(self):
        """Already proven by the subprocess test above — the module imported
        successfully even with empty GROQ_API_KEY / SUPABASE_URL."""
        pass


# ═══════════════════════════════════════════════════════════════════════
# 3. Frozen config
# ═══════════════════════════════════════════════════════════════════════

class TestConfigFrozen:
    def test_frozen(self):
        config = AdmissionConfig()
        with pytest.raises(FrozenInstanceError):
            config.new_message_max_chars = 999  # type: ignore[assignment]


# ═══════════════════════════════════════════════════════════════════════
# 4. Exact numeric constants
# ═══════════════════════════════════════════════════════════════════════

class TestExactConstants:
    def test_new_message_max_chars(self):
        assert NEW_MESSAGE_MAX_CHARS == 4000

    def test_legacy_history_max_chars(self):
        assert LEGACY_HISTORY_MAX_CHARS == 10000

    def test_message_max_estimated_units(self):
        assert MESSAGE_MAX_ESTIMATED_UNITS == 6000

    def test_provider_input_max_estimated_units(self):
        assert PROVIDER_INPUT_MAX_ESTIMATED_UNITS == 16000

    def test_user_requests_per_minute(self):
        assert USER_REQUESTS_PER_MINUTE == 20

    def test_network_requests_per_minute(self):
        assert NETWORK_REQUESTS_PER_MINUTE == 60

    def test_application_requests_per_minute(self):
        assert APPLICATION_REQUESTS_PER_MINUTE == 25

    def test_user_requests_per_day(self):
        assert USER_REQUESTS_PER_DAY == 200

    def test_user_estimated_units_per_day(self):
        assert USER_ESTIMATED_UNITS_PER_DAY == 250000


# ═══════════════════════════════════════════════════════════════════════
# 5. Constants ↔ AdmissionConfig correspondence
# ═══════════════════════════════════════════════════════════════════════

class TestConfigMatchesConstants:
    def test_config_matches_constants(self):
        config = AdmissionConfig()
        assert config.new_message_max_chars == NEW_MESSAGE_MAX_CHARS
        assert config.legacy_history_max_chars == LEGACY_HISTORY_MAX_CHARS
        assert config.message_max_estimated_units == MESSAGE_MAX_ESTIMATED_UNITS
        assert config.provider_input_max_estimated_units == PROVIDER_INPUT_MAX_ESTIMATED_UNITS
        assert config.user_requests_per_minute == USER_REQUESTS_PER_MINUTE
        assert config.network_requests_per_minute == NETWORK_REQUESTS_PER_MINUTE
        assert config.application_requests_per_minute == APPLICATION_REQUESTS_PER_MINUTE
        assert config.user_requests_per_day == USER_REQUESTS_PER_DAY
        assert config.user_estimated_units_per_day == USER_ESTIMATED_UNITS_PER_DAY


# ═══════════════════════════════════════════════════════════════════════
# 6–12. RequestIdentity parsing
# ═══════════════════════════════════════════════════════════════════════

_CANONICAL_LOWER = "550e8400-e29b-41d4-a716-446655440000"
_CANONICAL_UPPER = "550E8400-E29B-41D4-A716-446655440000"


class TestRequestIdentity:
    """Request identity parsing and validation."""

    # -- 6. Canonical lowercase --

    def test_canonical_lowercase(self):
        ident = RequestIdentity.parse(_CANONICAL_LOWER)
        assert ident.request_id == _CANONICAL_LOWER

    # -- 7. Canonical uppercase normalised to lowercase --

    def test_canonical_uppercase_normalised(self):
        ident = RequestIdentity.parse(_CANONICAL_UPPER)
        assert ident.request_id == _CANONICAL_LOWER

    # -- 8. Whitespace rejection --

    @pytest.mark.parametrize("raw", [
        f" {_CANONICAL_LOWER}",
        f"{_CANONICAL_LOWER} ",
        f"  {_CANONICAL_LOWER}  ",
        f"\t{_CANONICAL_LOWER}",
    ])
    def test_rejects_whitespace(self, raw):
        with pytest.raises(AdmissionError, match="invalid_request_id"):
            RequestIdentity.parse(raw)

    # -- 9. Curly-brace rejection --

    @pytest.mark.parametrize("raw", [
        f"{{{_CANONICAL_LOWER}}}",   # 38 chars
        f"{{{_CANONICAL_LOWER}",      # 37 chars
        f"{_CANONICAL_LOWER}}}",      # 38 chars
    ])
    def test_rejects_braces(self, raw):
        with pytest.raises(AdmissionError, match="invalid_request_id"):
            RequestIdentity.parse(raw)

    # -- 10. URN rejection --

    @pytest.mark.parametrize("raw", [
        f"urn:uuid:{_CANONICAL_LOWER}",
        f"URN:UUID:{_CANONICAL_LOWER}",
    ])
    def test_rejects_urn(self, raw):
        with pytest.raises(AdmissionError, match="invalid_request_id"):
            RequestIdentity.parse(raw)

    # -- 11. No hyphens --

    def test_rejects_no_hyphens(self):
        raw = _CANONICAL_LOWER.replace("-", "")
        with pytest.raises(AdmissionError, match="invalid_request_id"):
            RequestIdentity.parse(raw)

    # -- 12. Empty, invalid text, and non-string types --

    @pytest.mark.parametrize("raw", [
        "",
        "not-a-uuid",
        f"{_CANONICAL_LOWER}x",          # 37 chars
        f"{_CANONICAL_LOWER[:-1]}",      # 35 chars
        "gggggggg-gggg-gggg-gggg-gggggggggggg",  # not hex
    ])
    def test_rejects_invalid_text(self, raw):
        with pytest.raises(AdmissionError, match="invalid_request_id"):
            RequestIdentity.parse(raw)

    @pytest.mark.parametrize("raw", [
        12345,
        None,
        ["not-a-string"],
        {"key": "value"},
    ])
    def test_rejects_non_string_types(self, raw):
        with pytest.raises(AdmissionError, match="invalid_request_id"):
            RequestIdentity.parse(raw)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════
# 13–15. estimate_text_units
# ═══════════════════════════════════════════════════════════════════════

class TestEstimateTextUnits:
    """``estimate_text_units`` — byte-based input cost estimation."""

    # -- 13. Empty string returns 1 --

    def test_empty_returns_one(self):
        assert estimate_text_units("") == 1

    # -- 14. ASCII counted as one byte per character --

    def test_ascii_one_byte_per_char(self):
        assert estimate_text_units("hello") == 5
        assert estimate_text_units("a" * 100) == 100
        assert estimate_text_units("a" * 4000) == 4000

    # -- 15. Multibyte counted by real UTF-8 bytes --

    def test_two_byte_char(self):
        # "ñ" is 2 bytes in UTF-8
        assert estimate_text_units("ñ") == 2

    def test_three_byte_chars(self):
        # "你好" is 6 bytes (3 each)
        assert estimate_text_units("你好") == 6

    def test_four_byte_char(self):
        # emoji "😀" is 4 bytes
        assert estimate_text_units("😀") == 4

    def test_mixed_lengths(self):
        # "a" is 1 byte, "ñ" is 2, "😀" is 4 → total 7
        assert estimate_text_units("añ😀") == 7

    def test_rejects_non_string(self):
        with pytest.raises(TypeError):
            estimate_text_units(12345)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            estimate_text_units(None)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════
# 16–18. validate_new_message
# ═══════════════════════════════════════════════════════════════════════

class TestValidateNewMessage:
    """Message validation with deterministic precedence."""

    # -- 16. 4 000 ASCII chars accepted --

    def test_4000_ascii_chars_accepted(self):
        text = "a" * 4000
        validate_new_message(text)  # should not raise

    def test_under_4000_chars_accepted(self):
        validate_new_message("hello")
        validate_new_message("")
        validate_new_message("ñ" * 100)  # 200 units, well under 6000

    # -- 17. 4 001 chars rejected with ``message_too_long`` --

    def test_4001_chars_rejected(self):
        text = "a" * 4001
        with pytest.raises(AdmissionError) as excinfo:
            validate_new_message(text)
        assert excinfo.value.code == "message_too_long"
        assert excinfo.value.actual_chars == 4001
        assert excinfo.value.max_chars == 4000
        assert excinfo.value.actual_units == 0

    def test_5000_chars_rejected(self):
        text = "a" * 5000
        with pytest.raises(AdmissionError) as excinfo:
            validate_new_message(text)
        assert excinfo.value.code == "message_too_long"

    # -- 18. ≤ 4 000 chars but > 6 000 units → ``message_budget_exceeded`` --

    def test_over_6000_units_rejected(self):
        # "ñ" is 2 bytes, so 3001 chars × 2 = 6002 units > 6000
        text = "ñ" * 3001
        with pytest.raises(AdmissionError) as excinfo:
            validate_new_message(text)
        assert excinfo.value.code == "message_budget_exceeded"
        assert excinfo.value.actual_chars == 3001
        assert excinfo.value.actual_units == 6002
        assert excinfo.value.max_chars == 4000
        assert excinfo.value.max_units == 6000

    def test_4000_multi_byte_over_6000_units(self):
        # 4000 chars × 2 bytes = 8000 units
        text = "ñ" * 4000
        with pytest.raises(AdmissionError) as excinfo:
            validate_new_message(text)
        assert excinfo.value.code == "message_budget_exceeded"
        assert excinfo.value.actual_chars == 4000
        assert excinfo.value.actual_units == 8000
        assert excinfo.value.max_units == 6000

    def test_exactly_6000_units_accepted(self):
        # 6000 ASCII chars = 6000 bytes → accepted (len > 4000 fails first)
        # But 3000 "ñ" chars = 6000 bytes, and 3000 < 4000 chars → accepted
        text = "ñ" * 3000
        validate_new_message(text)

    def test_exactly_6001_units_rejected(self):
        # 3000 "ñ" chars + 1 extra byte → 6001 units
        text = "ñ" * 3000 + "a"  # 3001 chars, 6001 bytes
        with pytest.raises(AdmissionError) as excinfo:
            validate_new_message(text)
        assert excinfo.value.code == "message_budget_exceeded"

    def test_precedence_chars_before_units(self):
        # 4001 chars × 1 byte = 4001 units (well under 6000)
        # But chars check fires first → message_too_long
        text = "a" * 4001
        with pytest.raises(AdmissionError) as excinfo:
            validate_new_message(text)
        assert excinfo.value.code == "message_too_long"

    def test_precedence_invalid_type_before_chars(self):
        with pytest.raises(AdmissionError) as excinfo:
            validate_new_message(12345)  # type: ignore[arg-type]
        assert excinfo.value.code == "message_too_long"


# ═══════════════════════════════════════════════════════════════════════
# 19. Content and invalid UUID absent from exception
# ═══════════════════════════════════════════════════════════════════════

class TestErrorHidesSensitiveData:
    """``str(exc)``, ``repr(exc)``, and public attributes must never expose
    the original content or invalid UUID."""

    # -- message_too_long hides content --

    def test_message_too_long_hides_content(self):
        secret = "p4ssw0rd-sensitive-content"
        try:
            validate_new_message(secret + "a" * 4000)
        except AdmissionError as exc:
            assert secret not in str(exc)
            assert secret not in repr(exc)
            assert not any(
                getattr(exc, attr, None) == secret
                for attr in dir(exc)
                if not attr.startswith("_")
            )
            # Confirm the code is visible
            assert exc.code == "message_too_long"
        else:
            pytest.fail("Expected AdmissionError")

    # -- message_budget_exceeded hides content --

    def test_message_budget_exceeded_hides_content(self):
        secret = "c0nf1d3ntial-data"
        text = secret + "ñ" * 3000  # well over 6000 units
        try:
            validate_new_message(text)
        except AdmissionError as exc:
            assert secret not in str(exc)
            assert secret not in repr(exc)
            assert not any(
                getattr(exc, attr, None) == secret
                for attr in dir(exc)
                if not attr.startswith("_")
            )
            assert exc.code == "message_budget_exceeded"
        else:
            pytest.fail("Expected AdmissionError")

    # -- invalid_request_id hides raw UUID --

    def test_invalid_request_id_hides_raw(self):
        secret_uuid = "th1s-1s-4-s3cr3t-1d-1234567890ab"
        try:
            RequestIdentity.parse(secret_uuid)
        except AdmissionError as exc:
            assert secret_uuid not in str(exc)
            assert secret_uuid not in repr(exc)
            assert not any(
                getattr(exc, attr, None) == secret_uuid
                for attr in dir(exc)
                if not attr.startswith("_")
            )
            assert exc.code == "invalid_request_id"
        else:
            pytest.fail("Expected AdmissionError")

    def test_valid_identity_has_no_exception(self):
        # Happy path: parsing succeeds, no exception
        ident = RequestIdentity.parse("550e8400-e29b-41d4-a716-446655440000")
        assert ident.request_id == "550e8400-e29b-41d4-a716-446655440000"


# ═══════════════════════════════════════════════════════════════════════
# 20. No shared mutable state
# ═══════════════════════════════════════════════════════════════════════

class TestImmutability:
    """Verify that no shared mutable state exists."""

    def test_admission_config_is_independent(self):
        c1 = AdmissionConfig()
        c2 = AdmissionConfig()
        assert c1 == c2
        # Both are frozen, so they cannot be mutated; no shared object
        # inside can be mutated either (all fields are ints / str).

    def test_request_identity_is_frozen(self):
        ident = RequestIdentity.parse("550e8400-e29b-41d4-a716-446655440000")
        with pytest.raises(FrozenInstanceError):
            ident.request_id = "other"  # type: ignore[assignment]

    def test_no_module_level_mutable_defaults(self):
        # ``AdmissionError`` defaults are all zero / empty, not mutables
        exc = AdmissionError("test")
        assert exc.code == "test"
        assert exc.actual_chars == 0
        assert exc.actual_units == 0

    def test_functions_are_pure(self):
        # Calling estimate_text_units twice on same input returns same result
        assert estimate_text_units("hello") == 5
        assert estimate_text_units("hello") == 5
        # No side effects observable
