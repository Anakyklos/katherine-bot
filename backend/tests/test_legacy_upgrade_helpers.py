"""Unit tests for the JSON scalar query helpers in test_legacy_upgrade.py.

These tests are fully offline — they mock ``run_supabase_op`` and never talk to
a real Supabase instance, Docker, or the network.
"""

import json
import subprocess
import pytest
from unittest.mock import patch

from backend.tests.test_legacy_upgrade import (
    _parse_json_scalar,
    _query_scalar_bool,
    _query_scalar_int,
)


# ---------------------------------------------------------------------------
# Helper to build a fake subprocess.CompletedProcess
# ---------------------------------------------------------------------------

def _fake_result(stdout: str, returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["supabase", "db", "query"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# ===================================================================
# _parse_json_scalar — structure and type validation
# ===================================================================


class TestParseJsonScalar:
    """Direct tests for the low-level JSON parser (no mocking needed)."""

    def test_valid_bool_true(self):
        """1. Valid JSON with boolean True must return True."""
        data = json.dumps([{"result": True}])
        assert _parse_json_scalar(data, "result", bool, "boolean") is True

    def test_valid_bool_false(self):
        data = json.dumps([{"result": False}])
        assert _parse_json_scalar(data, "result", bool, "boolean") is False

    def test_valid_int(self):
        """2. Valid JSON with integer must return the integer."""
        data = json.dumps([{"count": 42}])
        assert (
            _parse_json_scalar(data, "count", int, "integer", reject_bool=True) == 42
        )

    def test_valid_int_zero(self):
        data = json.dumps([{"count": 0}])
        assert (
            _parse_json_scalar(data, "count", int, "integer", reject_bool=True) == 0
        )

    def test_valid_int_without_reject_bool(self):
        """Without reject_bool, a JSON boolean passes isinstance check for int."""
        data = json.dumps([{"count": True}])
        # True is instance of int and bool in Python
        assert _parse_json_scalar(data, "count", int, "integer") is True

    def test_invalid_json(self):
        """3. Malformed JSON must raise AssertionError."""
        with pytest.raises(AssertionError, match="invalid JSON response"):
            _parse_json_scalar("{bad json", "result", bool, "boolean")

    def test_not_a_list(self):
        """4. Result that is not a list must raise AssertionError."""
        data = json.dumps({"result": True})
        with pytest.raises(AssertionError, match="expected a list"):
            _parse_json_scalar(data, "result", bool, "boolean")

    def test_empty_list(self):
        """5. Empty list must raise AssertionError."""
        data = json.dumps([])
        with pytest.raises(AssertionError, match="expected exactly one row"):
            _parse_json_scalar(data, "result", bool, "boolean")

    def test_more_than_one_row(self):
        """6. More than one row must raise AssertionError."""
        data = json.dumps([{"result": True}, {"result": False}])
        with pytest.raises(AssertionError, match="expected exactly one row"):
            _parse_json_scalar(data, "result", bool, "boolean")

    def test_missing_key(self):
        """7. Absent expected key must raise AssertionError."""
        data = json.dumps([{"other": 1}])
        with pytest.raises(AssertionError, match="missing expected key"):
            _parse_json_scalar(data, "result", bool, "boolean")

    def test_unexpected_columns(self):
        """8. Extra columns beyond the expected key must raise AssertionError."""
        data = json.dumps([{"result": True, "extra": 1}])
        with pytest.raises(AssertionError, match="unexpected columns"):
            _parse_json_scalar(data, "result", bool, "boolean")

    def test_bool_where_int_rejected(self):
        """9. Boolean where integer is expected and reject_bool=True must raise."""
        data = json.dumps([{"count": False}])
        with pytest.raises(AssertionError, match="expected an integer, got boolean"):
            _parse_json_scalar(data, "count", int, "integer", reject_bool=True)

    def test_string_where_bool_expected(self):
        """10. String where bool is expected must raise."""
        data = json.dumps([{"result": "true"}])
        with pytest.raises(AssertionError, match="expected a boolean value"):
            _parse_json_scalar(data, "result", bool, "boolean")

    def test_string_where_int_expected(self):
        """10b. String where int is expected must raise."""
        data = json.dumps([{"count": "42"}])
        with pytest.raises(AssertionError, match="expected a integer value"):
            _parse_json_scalar(data, "count", int, "integer", reject_bool=True)

    def test_null_value(self):
        """11. JSON null value must raise AssertionError."""
        data = json.dumps([{"result": None}])
        with pytest.raises(AssertionError, match="expected a boolean value"):
            _parse_json_scalar(data, "result", bool, "boolean")

    def test_null_value_for_int(self):
        data = json.dumps([{"count": None}])
        with pytest.raises(AssertionError, match="expected a integer value"):
            _parse_json_scalar(data, "count", int, "integer", reject_bool=True)

    def test_sanitized_message_no_raw_output(self):
        """12. Error message must NOT contain raw stdout, SQL, or sensitive markers.

        This test verifies that failure paths never include the raw JSON,
        SQL fragments, or sensitive data in the exception message.
        """
        raw_payloads = [
            json.dumps([{"result": None}]),
            "RAW_SENSITIVE_SQL_DROP_TABLE",
            "SENSITIVE_JWT_eyJ_test",
        ]
        for raw in raw_payloads:
            with pytest.raises(AssertionError) as exc:
                _parse_json_scalar(raw, "result", bool, "boolean")
            msg = str(exc.value)
            # The message must be a constant string without the raw input
            assert raw not in msg, f"Raw payload leaked: {raw!r}"

    def test_value_not_a_dict(self):
        """List element that is not a dict must raise."""
        data = json.dumps([[1, 2, 3]])
        with pytest.raises(AssertionError, match="expected a JSON object"):
            _parse_json_scalar(data, "result", bool, "boolean")


# ===================================================================
# _query_scalar_bool — integrated with mocked run_supabase_op
# ===================================================================


class TestQueryScalarBool:
    """Tests for _query_scalar_bool with a mocked ``run_supabase_op``."""

    @patch("backend.tests.test_legacy_upgrade.run_supabase_op")
    def test_returns_true(self, mock_run_op):
        """Boolean true from JSON returns True."""
        mock_run_op.return_value = _fake_result('[{"result": true}]')
        assert _query_scalar_bool("SELECT true AS result", "result") is True

    @patch("backend.tests.test_legacy_upgrade.run_supabase_op")
    def test_returns_false(self, mock_run_op):
        """Boolean false from JSON returns False."""
        mock_run_op.return_value = _fake_result('[{"result": false}]')
        assert _query_scalar_bool("SELECT false AS result", "result") is False

    @patch("backend.tests.test_legacy_upgrade.run_supabase_op")
    def test_uses_supported_json_cli_contract(self, mock_run_op):
        """Must call run_supabase_op with --agent=no --output json (not --output-format)."""
        mock_run_op.return_value = _fake_result('[{"result": true}]')
        _query_scalar_bool("SELECT true AS result", "result")
        mock_run_op.assert_called_once_with(
            "legacy_state_query",
            [
                "db",
                "query",
                "--agent=no",
                "--output",
                "json",
                "SELECT true AS result",
            ],
            check=False,
        )

    @patch("backend.tests.test_legacy_upgrade.run_supabase_op")
    def test_invalid_json_sanitized(self, mock_run_op):
        """Invalid JSON output must raise sanitized error, not raw text."""
        mock_run_op.return_value = _fake_result("not valid json")
        with pytest.raises(AssertionError) as exc:
            _query_scalar_bool("SELECT 1", "result")
        assert "not valid json" not in str(exc.value)

    @patch("backend.tests.test_legacy_upgrade.run_supabase_op")
    def test_subprocess_failure_sanitized(self, mock_run_op):
        """Non-zero returncode from run_supabase_op must not leak SQL or output."""
        mock_run_op.return_value = _fake_result(
            "SENSITIVE_SQL", returncode=1, stderr="SENSITIVE_STDERR"
        )
        with pytest.raises(AssertionError) as exc:
            _query_scalar_bool("SELECT 1", "result")
        msg = str(exc.value)
        assert "SENSITIVE_SQL" not in msg
        assert "SENSITIVE_STDERR" not in msg

    @patch("backend.tests.test_legacy_upgrade.run_supabase_op")
    def test_null_sanitized(self, mock_run_op):
        """JSON null value must produce sanitized error."""
        mock_run_op.return_value = _fake_result('[{"result": null}]')
        with pytest.raises(AssertionError) as exc:
            _query_scalar_bool("SELECT NULL AS result", "result")
        assert "expected a boolean value" in str(exc.value)


# ===================================================================
# _query_scalar_int — integrated with mocked run_supabase_op
# ===================================================================


class TestQueryScalarInt:
    """Tests for _query_scalar_int with a mocked ``run_supabase_op``."""

    @patch("backend.tests.test_legacy_upgrade.run_supabase_op")
    def test_returns_int(self, mock_run_op):
        """Valid integer from JSON returns Python int."""
        mock_run_op.return_value = _fake_result('[{"count": 42}]')
        assert _query_scalar_int("SELECT 42 AS count", "count") == 42

    @patch("backend.tests.test_legacy_upgrade.run_supabase_op")
    def test_uses_supported_json_cli_contract(self, mock_run_op):
        """Must call run_supabase_op with --agent=no --output json (not --output-format)."""
        mock_run_op.return_value = _fake_result('[{"count": 42}]')
        _query_scalar_int("SELECT 42 AS count", "count")
        mock_run_op.assert_called_once_with(
            "legacy_state_query",
            [
                "db",
                "query",
                "--agent=no",
                "--output",
                "json",
                "SELECT 42 AS count",
            ],
            check=False,
        )

    @patch("backend.tests.test_legacy_upgrade.run_supabase_op")
    def test_returns_zero(self, mock_run_op):
        """Zero is a valid count."""
        mock_run_op.return_value = _fake_result('[{"count": 0}]')
        assert _query_scalar_int("SELECT 0 AS count", "count") == 0

    @patch("backend.tests.test_legacy_upgrade.run_supabase_op")
    def test_bool_rejected(self, mock_run_op):
        """Boolean where int is expected must raise."""
        mock_run_op.return_value = _fake_result('[{"count": true}]')
        with pytest.raises(AssertionError, match="expected an integer, got boolean"):
            _query_scalar_int("SELECT true AS count", "count")

    @patch("backend.tests.test_legacy_upgrade.run_supabase_op")
    def test_string_rejected(self, mock_run_op):
        """String where int is expected must raise."""
        mock_run_op.return_value = _fake_result('[{"count": "42"}]')
        with pytest.raises(AssertionError, match="expected a integer value"):
            _query_scalar_int("SELECT '42' AS count", "count")

    @patch("backend.tests.test_legacy_upgrade.run_supabase_op")
    def test_negative_rejected(self, mock_run_op):
        """Negative integer must raise."""
        mock_run_op.return_value = _fake_result('[{"count": -1}]')
        with pytest.raises(AssertionError, match="expected a non-negative integer"):
            _query_scalar_int("SELECT -1 AS count", "count")

    @patch("backend.tests.test_legacy_upgrade.run_supabase_op")
    def test_subprocess_failure_sanitized(self, mock_run_op):
        """Non-zero returncode must not leak SQL or output."""
        mock_run_op.return_value = _fake_result(
            "SENSITIVE_SQL", returncode=1, stderr="SENSITIVE_STDERR"
        )
        with pytest.raises(AssertionError) as exc:
            _query_scalar_int("SELECT 1 AS count", "count")
        msg = str(exc.value)
        assert "SENSITIVE_SQL" not in msg
        assert "SENSITIVE_STDERR" not in msg

    @patch("backend.tests.test_legacy_upgrade.run_supabase_op")
    def test_missing_key_sanitized(self, mock_run_op):
        """Missing key must produce sanitized error."""
        mock_run_op.return_value = _fake_result('[{"other": 1}]')
        with pytest.raises(AssertionError) as exc:
            _query_scalar_int("SELECT 1 AS other", "count")
        assert "expected" in str(exc.value).lower()
