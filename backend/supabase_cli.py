"""
Sanitized subprocess helper for Supabase CLI operations.

Provides a wrapper around subprocess.run that:
- Uses list-based arguments (no shell=True)
- Returns CompletedProcess for expected failures
- Only exposes a constant operation identifier in exception messages
- Never includes command, query, path, payload, keys, or raw output in public messages
"""
import os
import subprocess
import logging

logger = logging.getLogger(__name__)

# Allowed operation identifiers
ALLOWED_OPS = frozenset({
    "legacy_baseline_reset",
    "legacy_hardening_apply",
    "legacy_state_query",
    "rls_auto_enable_reset",
    "rls_auto_enable_upgrade",
    "rls_auto_enable_query",
    "privacy_legacy_reset",
    "privacy_legacy_upgrade",
    "privacy_legacy_query",
    "retention_preflight_reset",
    "retention_preflight_apply",
    "retention_preflight_query",
})


def run_supabase_op(op_id: str, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a Supabase CLI operation with sanitized error handling.

    Args:
        op_id: A constant identifier from ALLOWED_OPS describing the operation.
        args: The real arguments for the subprocess (e.g. ["db", "reset"]).
        check: If True, raise on non-zero return; if False, return CompletedProcess.

    Returns:
        subprocess.CompletedProcess (caller can inspect .returncode, .stdout, .stderr).

    Raises:
        RuntimeError: On non-zero return when check=True. The message only contains
            the op_id, never the raw command, SQL, or output.
    """
    if op_id not in ALLOWED_OPS:
        raise ValueError(f"Unknown operation identifier: {op_id}")

    cmd = ["supabase"] + args

    # Disable CLI telemetry: the PostHog analytics flush intermittently times
    # out and converts a successful SQL command into a non-zero exit code,
    # which would make database integration tests flaky.
    child_env = dict(os.environ)
    child_env["SUPABASE_TELEMETRY_DISABLED"] = "1"
    child_env["SUPABASE_ANALYTICS_ENABLED"] = "false"

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            env=child_env,
        )
    except FileNotFoundError:
        raise RuntimeError(f"Supabase operation failed: {op_id} (binary not found)")

    if check and result.returncode != 0:
        # Sanitized: never expose cmd, args, query, payload, or raw output.
        logger.error("Supabase operation failed: %s (returncode=%d)", op_id, result.returncode)
        raise RuntimeError(f"Supabase operation failed: {op_id}")

    return result
