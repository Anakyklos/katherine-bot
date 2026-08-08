"""Shared helpers for real-Supabase database integration tests.

Centralizes the pieces every database integration test needs so they are not
reimplemented per test file:

* ``run_supabase``: sanitized Supabase CLI wrapper. Failure messages only ever
  expose a constant operation identifier, never the command, SQL or output.
* ``run_psql`` / ``run_psql_file``: execute raw SQL or fixture files inside the
  local ``supabase_db_app`` container via ``docker exec`` (no host psql
  needed).
* ``query_json`` and the ``query_scalar_*`` / ``query_text_array`` helpers:
  run ``supabase db query --output json`` and parse the result with constant,
  sanitized error messages that never leak SQL, stdout or stderr.

These helpers only make sense against a live local Supabase stack, so the
callers are the ``database_integration``-marked test files that CI runs
against a freshly reset instance.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from backend.supabase_cli import run_supabase_op

# Op id used by the shared query helpers; allowlisted in
# ``backend/supabase_cli.py`` ALLOWED_OPS.
QUERY_OP_ID = "database_integration_query"


def run_supabase(op_id: str, args: list[str], check: bool = True):
    """Run a Supabase CLI command via the sanitized helper.

    Raises:
        AssertionError: When *check* is True and the operation failed. The
            message only contains the constant *op_id*.
    """
    result = run_supabase_op(op_id, args, check=False)
    if check and result.returncode != 0:
        raise AssertionError(f"Supabase operation failed: {op_id}")
    return result


def run_psql(sql: str) -> None:
    """Execute a SQL script through the local Supabase DB container."""
    result = subprocess.run(
        [
            "docker", "exec", "-i", "supabase_db_app",
            "psql", "-U", "postgres",
            "-v", "ON_ERROR_STOP=1", "-q", "-f", "-",
        ],
        input=sql,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError("psql execution failed")


def run_psql_file(filepath: str | Path) -> None:
    """Execute a multi-statement SQL fixture file inside the DB container."""
    with open(filepath, encoding="utf-8") as f:
        run_psql(f.read())


def _parse_single_row_json(stdout: str, expected_key: str):
    """Parse ``supabase db query --output json`` output for one scalar row.

    Validates that the JSON structure is a list with exactly one dict
    containing exactly *expected_key*, then returns its value. Any mismatch
    raises ``AssertionError`` with a constant, sanitized message that never
    includes SQL, stdout, stderr or sensitive markers.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        raise AssertionError("Query result: invalid JSON response")

    if not isinstance(data, list):
        raise AssertionError("Query result: expected a list")
    if len(data) != 1:
        raise AssertionError("Query result: expected exactly one row")
    if not isinstance(data[0], dict):
        raise AssertionError("Query result: expected a JSON object")
    if len(data[0]) != 1:
        raise AssertionError("Query result: unexpected columns")
    if expected_key not in data[0]:
        raise AssertionError("Query result: missing expected key")

    return data[0][expected_key]


def parse_json_scalar(
    stdout: str,
    expected_key: str,
    expected_type: type,
    type_name: str,
    *,
    reject_bool: bool = False,
):
    """Parse JSON scalar output from ``supabase db query --output json``.

    Validates that the JSON structure is a list with exactly one dict
    containing exactly the *expected_key* and that the value matches
    *expected_type*.  On any mismatch raises ``AssertionError`` with a
    constant, sanitized message.

    When *reject_bool* is True (used for integer queries) Python booleans are
    rejected because ``bool`` is a subtype of ``int`` in Python.
    """
    value = _parse_single_row_json(stdout, expected_key)

    if reject_bool and isinstance(value, bool):
        raise AssertionError("Query result: expected an integer, got boolean")
    if not isinstance(value, expected_type):
        raise AssertionError(f"Query result: expected a {type_name} value")

    return value


def _query_stdout(query: str, op_id: str) -> str:
    """Run ``supabase db query --output json`` and return its stdout."""
    res = run_supabase(op_id, ["db", "query", "--agent=no", "--output", "json", query])
    return res.stdout


def query_json(query: str, *, op_id: str = QUERY_OP_ID) -> list:
    """Execute a SQL query and return the parsed JSON rows (list of dicts).

    The query runs with ``--agent=no --output json`` for deterministic
    machine-readable output.
    """
    try:
        data = json.loads(_query_stdout(query, op_id))
    except json.JSONDecodeError:
        raise AssertionError("Query result: invalid JSON response")
    if not isinstance(data, list):
        raise AssertionError("Query result: expected a list")
    return data


def query_scalar_bool(query: str, expected_key: str, *, op_id: str = QUERY_OP_ID) -> bool:
    """Execute a SQL query returning a single boolean scalar.

    The query must alias its single result column to *expected_key*.
    """
    return parse_json_scalar(_query_stdout(query, op_id), expected_key, bool, "boolean")


def query_scalar_int(query: str, expected_key: str, *, op_id: str = QUERY_OP_ID) -> int:
    """Execute a SQL query returning a single non-negative integer scalar.

    The query must alias its single result column to *expected_key*.
    """
    value = parse_json_scalar(
        _query_stdout(query, op_id), expected_key, int, "integer", reject_bool=True
    )
    if value < 0:
        raise AssertionError("Query result: expected a non-negative integer")
    return value


def query_scalar_text(query: str, expected_key: str, *, op_id: str = QUERY_OP_ID) -> str:
    """Execute a SQL query returning a single text scalar.

    The query must alias its single result column to *expected_key*.
    """
    return parse_json_scalar(_query_stdout(query, op_id), expected_key, str, "text")


def query_text_array(query: str, expected_key: str, *, op_id: str = QUERY_OP_ID) -> list[str]:
    """Execute a SQL query returning a single text-array scalar.

    The query must alias its single result column to *expected_key*.
    """
    value = _parse_single_row_json(_query_stdout(query, op_id), expected_key)
    if not isinstance(value, list):
        raise AssertionError("Query result: expected an array value")
    if not all(isinstance(item, str) for item in value):
        raise AssertionError("Query result: expected an array of strings")
    return value
