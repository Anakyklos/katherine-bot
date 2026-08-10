"""Operational CLI for the durable account deletion worker (#325).

Usage::

    python -m backend.account_deletion_cli --once

Runs ONE bounded round and exits. There is no scheduler, no hidden loop, no
``BackgroundTasks`` and no daemon: the process starts, processes at most
``max_batch`` jobs (DB purge first, then the Auth Admin hard delete, then
finalize), prints a sanitized aggregate summary and terminates.

* Empty queue is success: ``--once`` returns exit code 0 with ``no_work``
  when there is no eligible job, without sleeping or polling.
* Exit code 1 only for a real operational failure that prevents the round
  from running (missing configuration, invalid parameters, database
  unreachable at acquisition time).
* Per-job domain outcomes (retry scheduled, failed, lease lost) are part of
  the round and do not fail the process: retries/attempts are governed by
  the database (``next_attempt_at``), never by the CLI.

Configuration comes from the environment:

* ``SUPABASE_URL`` / ``SUPABASE_SERVICE_ROLE_KEY`` (required; values never
  echoed);
* ``ACCOUNT_DELETION_WORKER_ID`` (optional; default is a per-process random
  id ``cli-worker-<hex>`` that never contains PII and matches the SQL
  allowlist ``^[A-Za-z0-9_.:-]{1,64}$``);
* ``ACCOUNT_DELETION_LEASE_SECONDS`` (default 300, SQL bound 1..3600);
* ``ACCOUNT_DELETION_MAX_BATCH`` (default 10, SQL bound 1..1000);
* ``ACCOUNT_DELETION_AUTH_TIMEOUT_SECONDS`` (default 10.0; must be positive
  and no larger than the lease, so a legitimate lease loss is never
  overridden by an external call that runs longer than the lease).

Observability is sanitized: only aggregate counts and constant codes are
printed/logged. Raw user ids, HMACs, operation ids, job ids, emails,
tokens, payloads, SQL and upstream exception text never appear.

The entrypoint is testable without network by injecting ``worker_factory``
in tests.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from backend.account_deletion import SupabaseAccountDeletionRepository
from backend.account_deletion_worker import (
    AccountDeletionWorker,
    AccountDeletionWorkerConfig,
    SupabaseAccountDeletionAuthAdmin,
)
from backend.observability import EVENT_ACCOUNT_DELETION_FAILED, emit_event

logger = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 300
DEFAULT_MAX_BATCH = 10
DEFAULT_AUTH_TIMEOUT_SECONDS = 10.0


class AccountDeletionRuntimeConfigurationError(Exception):
    """Raised when the worker runtime environment is incomplete/invalid.

    The message is constant; missing values and secrets are never echoed.
    """


@dataclass(frozen=True)
class AccountDeletionRuntimeConfig:
    """Minimal, fail-closed runtime configuration for the worker CLI.

    Deliberately does NOT require the full application ``Settings``: no Groq
    keys, no CORS, no embeddings. Only the Supabase surface plus the worker
    bounds.
    """

    supabase_url: str
    supabase_service_role_key: str  # never logged or echoed
    worker_id: str
    lease_seconds: int
    max_batch: int
    auth_timeout_seconds: float

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "AccountDeletionRuntimeConfig":
        source: Mapping[str, str] = os.environ if env is None else env
        url = source.get("SUPABASE_URL")
        key = source.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise AccountDeletionRuntimeConfigurationError(
                "account deletion worker runtime configuration is incomplete"
            )
        worker_id = source.get("ACCOUNT_DELETION_WORKER_ID") or (
            f"cli-worker-{uuid.uuid4().hex[:12]}"
        )
        lease_seconds = _parse_int_env(
            source, "ACCOUNT_DELETION_LEASE_SECONDS", DEFAULT_LEASE_SECONDS
        )
        max_batch = _parse_int_env(
            source, "ACCOUNT_DELETION_MAX_BATCH", DEFAULT_MAX_BATCH
        )
        auth_timeout_seconds = _parse_float_env(
            source,
            "ACCOUNT_DELETION_AUTH_TIMEOUT_SECONDS",
            DEFAULT_AUTH_TIMEOUT_SECONDS,
        )
        # Mirror the SQL bounds so misconfiguration fails fast at startup.
        config = AccountDeletionWorkerConfig(
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            max_batch=max_batch,
        )
        if not (0.0 < auth_timeout_seconds <= config.lease_seconds):
            raise AccountDeletionRuntimeConfigurationError(
                "account deletion worker auth timeout is invalid"
            )
        return cls(
            supabase_url=url,
            supabase_service_role_key=key,
            worker_id=config.worker_id,
            lease_seconds=config.lease_seconds,
            max_batch=config.max_batch,
            auth_timeout_seconds=auth_timeout_seconds,
        )


def _parse_int_env(source: Mapping[str, str], key: str, default: int) -> int:
    value = source.get(key)
    if value is None:
        return default
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        raise AccountDeletionRuntimeConfigurationError(
            f"environment variable {key} is invalid"
        )


def _parse_float_env(source: Mapping[str, str], key: str, default: float) -> float:
    value = source.get(key)
    if value is None:
        return default
    try:
        parsed = float(value.strip())
    except (TypeError, ValueError):
        raise AccountDeletionRuntimeConfigurationError(
            f"environment variable {key} is invalid"
        )
    if parsed != parsed or parsed in (float("inf"), float("-inf")):  # NaN / inf
        raise AccountDeletionRuntimeConfigurationError(
            f"environment variable {key} is invalid"
        )
    return parsed


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Katherine Bot — durable account deletion worker. Processes one "
            "bounded round and exits."
        )
    )
    parser.add_argument(
        "--once",
        action="store_true",
        required=True,
        help="Run a single bounded worker round and exit (the only supported mode)",
    )
    return parser.parse_args(argv)


def _build_client(config: AccountDeletionRuntimeConfig) -> Any:
    """Construct the Supabase client for the worker round.

    A single ``httpx.Client`` with a configured transport timeout is shared
    by the PostgREST RPC transport and the Auth Admin client, so every
    external operation (DB RPC and Auth delete) is bounded. This function is
    only reachable from the CLI composition root.
    """
    import httpx
    from supabase import create_client
    from supabase.lib.client_options import SyncClientOptions

    http_client = httpx.Client(timeout=config.auth_timeout_seconds)
    options = SyncClientOptions(
        postgrest_client_timeout=config.auth_timeout_seconds,
        httpx_client=http_client,
    )
    return create_client(
        config.supabase_url, config.supabase_service_role_key, options=options
    )


def main(
    argv: Optional[list[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    worker_factory: Optional[Callable[[AccountDeletionRuntimeConfig], AccountDeletionWorker]] = None,
) -> int:
    """Run one bounded worker round and return the process exit code.

    Args:
        argv: Override for ``sys.argv`` (used in tests).
        env: Override for the environment (used in tests). When ``None`` the
            real ``os.environ`` is read.
        worker_factory: Callable that builds the worker from the runtime
            config; injected in tests to avoid network. When ``None`` a real
            Supabase-backed worker is built.

    Returns:
        ``0`` on success (including empty queue), ``1`` on any failure that
        prevents the round from running.
    """
    _parse_args(argv)
    try:
        config = AccountDeletionRuntimeConfig.from_env(env)
        if worker_factory is not None:
            worker = worker_factory(config)
        else:
            client = _build_client(config)
            repository = SupabaseAccountDeletionRepository(client)
            auth_admin = SupabaseAccountDeletionAuthAdmin(client.auth.admin)
            worker = AccountDeletionWorker(
                repository=repository,
                auth_admin=auth_admin,
                config=AccountDeletionWorkerConfig(
                    worker_id=config.worker_id,
                    lease_seconds=config.lease_seconds,
                    max_batch=config.max_batch,
                ),
            )
        result = worker.run_once()
    except Exception:
        emit_event(
            logger, EVENT_ACCOUNT_DELETION_FAILED, level=logging.ERROR, code="round_failed"
        )
        return 1

    # Sanitized aggregate summary: counts and constants only.
    print(
        "account_deletion_worker "
        f"no_work={result.no_work} completed={result.completed} "
        f"retry_scheduled={result.retry_scheduled} failed={result.failed} "
        f"lease_lost={result.lease_lost}"
    )
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
