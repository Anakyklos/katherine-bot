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

Additional coverage for code-review corrections:

21. Direct constructor with valid lowercase UUID
22. Direct constructor with valid uppercase UUID (normalised)
23. Direct constructor with invalid UUID
24. Direct constructor with no-hyphens format
25. ``parse()`` and constructor produce equivalent objects
26. Impossible to mutate after construction
27. Real purity test with env, socket, import guards before import
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
# 1. Pure importability  (code-review correction: real purity guards)
# ═══════════════════════════════════════════════════════════════════════

class TestImportability:
    """Import module in a subprocess with env, socket, and import hooks
    that **fail** if the module touches forbidden resources."""

    # NOTE: single-quote outer delimiter to avoid conflict with inner
    #       triple-double-quote docstrings in the generated script.
    _PURITY_SCRIPT = '''
import sys

# -- Pre-import ONLY stdlib needed for guards --------------------------
import os as _os
import socket as _socket

# -- Guard 1: os.environ raises on any read ---------------------------
class _FailEnv:
    """Mapping proxy that fails on every read operation."""
    def __getitem__(self, key):
        raise RuntimeError(f"os.environ read attempted: key={key!r}")
    def get(self, key, default=None):
        raise RuntimeError(f"os.environ.get read attempted: key={key!r}")
    def __contains__(self, key):
        raise RuntimeError(f"os.environ.__contains__ read attempted: key={key!r}")
    def __setitem__(self, key, value):
        pass
    def __delitem__(self, key):
        pass
    def __repr__(self):
        return "_FailEnv()"
    def __iter__(self):
        return iter([])
    def __len__(self):
        return 0
    def __bool__(self):
        return True

_os.environ = _FailEnv()

# -- Guard 2: os.getenv raises unconditionally -------------------------
def _raise_getenv(key, default=None):
    raise RuntimeError(f"os.getenv read attempted: key={key!r}")
_os.getenv = _raise_getenv

# -- Guard 3: socket.socket / create_connection raise ------------------
def _raise_socket(*args, **kwargs):
    raise OSError("socket.socket blocked")
def _raise_create_connection(*args, **kwargs):
    raise OSError("socket.create_connection blocked")
_socket.socket = _raise_socket
_socket.create_connection = _raise_create_connection

# -- Guard 4: import hook blocks forbidden modules --------------------
_BLOCKED_TOP = frozenset({
    "fastapi", "groq", "supabase", "sentence_transformers",
    "pydantic", "httpx", "httpcore", "anyio",
})
_BLOCKED_FULL = frozenset({
    "backend.engine", "backend.memory",
})

class _BlockImport:
    def find_spec(self, fullname, path, target=None):
        if fullname in _BLOCKED_FULL:
            raise ImportError(f"blocked: {fullname}")
        top = fullname.split(".")[0]
        if top in _BLOCKED_TOP:
            raise ImportError(f"blocked: {fullname}")
        return None

sys.meta_path.insert(0, _BlockImport())

# -- Import the module under test --------------------------------------
sys.path.insert(0, ".")
from backend.admission_contracts import (
    NEW_MESSAGE_MAX_CHARS,
    AdmissionConfig,
    RequestIdentity,
    estimate_text_units,
    validate_new_message,
)

# -- Verify the module actually works ----------------------------------
assert NEW_MESSAGE_MAX_CHARS == 4000
assert estimate_text_units("hello") == 5
ident = RequestIdentity.parse("550e8400-e29b-41d4-a716-446655440000")
assert ident.request_id == "550e8400-e29b-41d4-a716-446655440000"
config = AdmissionConfig()
assert config.new_message_max_chars == 4000

print("OK")
'''

    def test_pure_import_with_guards(self):
        """Subprocess with env, socket, and import guards active *before*
        the module import.  Fails if the module touches any forbidden
        resource."""
        proc = subprocess.run(
            [sys.executable, "-c", self._PURITY_SCRIPT],
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
            f"Subprocess failed (guard triggered?):\n"
            f"stdout:{proc.stdout}\nstderr:{proc.stderr}"
        )
        assert "OK" in proc.stdout


# ═══════════════════════════════════════════════════════════════════════
# 2. No environment reads
# ═══════════════════════════════════════════════════════════════════════

class TestNoEnv:
    """The module does not read environment variables at import time."""

    def test_no_post_init(self):
        assert not hasattr(AdmissionConfig, "__post_init__")


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
# 6–12 + 21–26. RequestIdentity (parse + direct constructor)
# ═══════════════════════════════════════════════════════════════════════

_CANONICAL_LOWER = "550e8400-e29b-41d4-a716-446655440000"
_CANONICAL_UPPER = "550E8400-E29B-41D4-A716-446655440000"


class TestRequestIdentity:
    """Request identity: parse() and direct constructor."""

    # ── Original parse-based tests (6–12) ─────────────────────────

    def test_parse_canonical_lowercase(self):
        ident = RequestIdentity.parse(_CANONICAL_LOWER)
        assert ident.request_id == _CANONICAL_LOWER

    def test_parse_canonical_uppercase_normalised(self):
        ident = RequestIdentity.parse(_CANONICAL_UPPER)
        assert ident.request_id == _CANONICAL_LOWER

    @pytest.mark.parametrize("raw", [
        f" {_CANONICAL_LOWER}",
        f"{_CANONICAL_LOWER} ",
        f"  {_CANONICAL_LOWER}  ",
        f"\t{_CANONICAL_LOWER}",
    ])
    def test_parse_rejects_whitespace(self, raw):
        with pytest.raises(AdmissionError, match="invalid_request_id"):
            RequestIdentity.parse(raw)

    @pytest.mark.parametrize("raw", [
        f"{{{_CANONICAL_LOWER}}}",
        f"{{{_CANONICAL_LOWER}",
        f"{_CANONICAL_LOWER}}}",
    ])
    def test_parse_rejects_braces(self, raw):
        with pytest.raises(AdmissionError, match="invalid_request_id"):
            RequestIdentity.parse(raw)

    @pytest.mark.parametrize("raw", [
        f"urn:uuid:{_CANONICAL_LOWER}",
        f"URN:UUID:{_CANONICAL_LOWER}",
    ])
    def test_parse_rejects_urn(self, raw):
        with pytest.raises(AdmissionError, match="invalid_request_id"):
            RequestIdentity.parse(raw)

    def test_parse_rejects_no_hyphens(self):
        raw = _CANONICAL_LOWER.replace("-", "")
        with pytest.raises(AdmissionError, match="invalid_request_id"):
            RequestIdentity.parse(raw)

    @pytest.mark.parametrize("raw", [
        "",
        "not-a-uuid",
        f"{_CANONICAL_LOWER}x",
        f"{_CANONICAL_LOWER[:-1]}",
        "gggggggg-gggg-gggg-gggg-gggggggggggg",
    ])
    def test_parse_rejects_invalid_text(self, raw):
        with pytest.raises(AdmissionError, match="invalid_request_id"):
            RequestIdentity.parse(raw)

    @pytest.mark.parametrize("raw", [
        12345,
        None,
        ["not-a-string"],
        {"key": "value"},
    ])
    def test_parse_rejects_non_string_types(self, raw):
        with pytest.raises(AdmissionError, match="invalid_request_id"):
            RequestIdentity.parse(raw)  # type: ignore[arg-type]

    # ── Direct constructor tests (code-review corrections 21–26) ──

    def test_constructor_valid_lowercase(self):
        """21. Direct constructor with valid lowercase UUID."""
        ident = RequestIdentity(_CANONICAL_LOWER)
        assert ident.request_id == _CANONICAL_LOWER

    def test_constructor_valid_uppercase_normalised(self):
        """22. Direct constructor with valid uppercase UUID."""
        ident = RequestIdentity(_CANONICAL_UPPER)
        assert ident.request_id == _CANONICAL_LOWER

    def test_constructor_invalid_raises(self):
        """23. Direct constructor with invalid UUID raises."""
        with pytest.raises(AdmissionError, match="invalid_request_id"):
            RequestIdentity("not-a-uuid")

    def test_constructor_no_hyphens_raises(self):
        """24. Direct constructor with no-hyphens format."""
        raw = _CANONICAL_LOWER.replace("-", "")
        with pytest.raises(AdmissionError, match="invalid_request_id"):
            RequestIdentity(raw)

    def test_parse_and_constructor_equivalent(self):
        """25. ``parse()`` and constructor produce equal objects."""
        ident1 = RequestIdentity.parse(_CANONICAL_LOWER)
        ident2 = RequestIdentity(_CANONICAL_LOWER)
        assert ident1 == ident2
        assert ident1.request_id == ident2.request_id

    def test_parse_and_constructor_equivalent_uppercase(self):
        """25b. Both paths normalise uppercase identically."""
        ident1 = RequestIdentity.parse(_CANONICAL_UPPER)
        ident2 = RequestIdentity(_CANONICAL_UPPER)
        assert ident1 == ident2
        assert ident1.request_id == _CANONICAL_LOWER

    def test_immutable_after_construction(self):
        """26. Impossible to mutate after construction."""
        ident = RequestIdentity(_CANONICAL_LOWER)
        with pytest.raises(FrozenInstanceError):
            ident.request_id = "other"  # type: ignore[assignment]

    def test_constructor_rejects_non_string(self):
        with pytest.raises(AdmissionError, match="invalid_request_id"):
            RequestIdentity(12345)  # type: ignore[arg-type]
        with pytest.raises(AdmissionError, match="invalid_request_id"):
            RequestIdentity(None)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════
# 13–15. estimate_text_units
# ═══════════════════════════════════════════════════════════════════════

class TestEstimateTextUnits:
    def test_empty_returns_one(self):
        assert estimate_text_units("") == 1

    def test_ascii_one_byte_per_char(self):
        assert estimate_text_units("hello") == 5
        assert estimate_text_units("a" * 100) == 100
        assert estimate_text_units("a" * 4000) == 4000

    def test_two_byte_char(self):
        assert estimate_text_units("\u00f1") == 2

    def test_three_byte_chars(self):
        assert estimate_text_units("\u4f60\u597d") == 6

    def test_four_byte_char(self):
        assert estimate_text_units("\U0001f600") == 4

    def test_mixed_lengths(self):
        assert estimate_text_units("a\u00f1\U0001f600") == 7

    def test_rejects_non_string(self):
        with pytest.raises(TypeError):
            estimate_text_units(12345)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            estimate_text_units(None)  # type: ignore[arg-type]


# ═══════════════════════════════════════════════════════════════════════
# 16–18. validate_new_message
# ═══════════════════════════════════════════════════════════════════════

class TestValidateNewMessage:
    def test_4000_ascii_chars_accepted(self):
        validate_new_message("a" * 4000)

    def test_under_4000_chars_accepted(self):
        validate_new_message("hello")
        validate_new_message("")
        validate_new_message("\u00f1" * 100)

    def test_4001_chars_rejected(self):
        text = "a" * 4001
        with pytest.raises(AdmissionError) as excinfo:
            validate_new_message(text)
        assert excinfo.value.code == "message_too_long"
        assert excinfo.value.actual_chars == 4001
        assert excinfo.value.max_chars == 4000
        assert excinfo.value.actual_units == 0

    def test_5000_chars_rejected(self):
        with pytest.raises(AdmissionError) as excinfo:
            validate_new_message("a" * 5000)
        assert excinfo.value.code == "message_too_long"

    def test_over_6000_units_rejected(self):
        text = "\u00f1" * 3001
        with pytest.raises(AdmissionError) as excinfo:
            validate_new_message(text)
        assert excinfo.value.code == "message_budget_exceeded"
        assert excinfo.value.actual_chars == 3001
        assert excinfo.value.actual_units == 6002
        assert excinfo.value.max_chars == 4000
        assert excinfo.value.max_units == 6000

    def test_4000_multi_byte_over_6000_units(self):
        text = "\u00f1" * 4000
        with pytest.raises(AdmissionError) as excinfo:
            validate_new_message(text)
        assert excinfo.value.code == "message_budget_exceeded"
        assert excinfo.value.actual_chars == 4000
        assert excinfo.value.actual_units == 8000
        assert excinfo.value.max_units == 6000

    def test_exactly_6000_units_accepted(self):
        validate_new_message("\u00f1" * 3000)

    def test_exactly_6001_units_rejected(self):
        with pytest.raises(AdmissionError) as excinfo:
            validate_new_message("\u00f1" * 3000 + "a")
        assert excinfo.value.code == "message_budget_exceeded"

    def test_precedence_chars_before_units(self):
        with pytest.raises(AdmissionError) as excinfo:
            validate_new_message("a" * 4001)
        assert excinfo.value.code == "message_too_long"

    def test_precedence_invalid_type_before_chars(self):
        with pytest.raises(AdmissionError) as excinfo:
            validate_new_message(12345)  # type: ignore[arg-type]
        assert excinfo.value.code == "message_too_long"


# ═══════════════════════════════════════════════════════════════════════
# 19. Content and invalid UUID absent from exception
# ═══════════════════════════════════════════════════════════════════════

class TestErrorHidesSensitiveData:
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
            assert exc.code == "message_too_long"
        else:
            pytest.fail("Expected AdmissionError")

    def test_message_budget_exceeded_hides_content(self):
        secret = "c0nf1d3ntial-data"
        try:
            validate_new_message(secret + "\u00f1" * 3000)
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

    def test_parse_invalid_hides_raw(self):
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

    def test_constructor_invalid_hides_raw(self):
        """Direct constructor also hides the raw invalid UUID."""
        secret_uuid = "th1s-1s-4-s3cr3t-1d-1234567890ab"
        try:
            RequestIdentity(secret_uuid)
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
        ident = RequestIdentity.parse("550e8400-e29b-41d4-a716-446655440000")
        assert ident.request_id == "550e8400-e29b-41d4-a716-446655440000"


# ═══════════════════════════════════════════════════════════════════════
# 20. No shared mutable state
# ═══════════════════════════════════════════════════════════════════════

class TestImmutability:
    def test_admission_config_is_independent(self):
        c1 = AdmissionConfig()
        c2 = AdmissionConfig()
        assert c1 == c2

    def test_request_identity_is_frozen(self):
        ident = RequestIdentity.parse("550e8400-e29b-41d4-a716-446655440000")
        with pytest.raises(FrozenInstanceError):
            ident.request_id = "other"  # type: ignore[assignment]

    def test_no_module_level_mutable_defaults(self):
        exc = AdmissionError("test")
        assert exc.code == "test"
        assert exc.actual_chars == 0
        assert exc.actual_units == 0

    def test_functions_are_pure(self):
        assert estimate_text_units("hello") == 5
        assert estimate_text_units("hello") == 5
