"""Explicit operational command for data retention (#316).

Usage::

    python -m backend.retention_cli --once

Runs ONE retention round and exits. There is no scheduler, no hidden loop,
no ``BackgroundTasks`` and no parallel server: the process starts, processes
bounded batches per the versioned policy, prints a sanitized aggregate
summary and terminates with exit code 0 (success) or 1 (any failure).

Configuration comes from the environment (``RetentionRuntimeConfig``):
``SUPABASE_URL`` and ``SUPABASE_SERVICE_ROLE_KEY`` are required (fail
closed, values never echoed); the Supabase transport timeout comes from the
standard ``TURN_SUPABASE_TIMEOUT`` variable through
``TurnExecutionConfig.from_env``.

Observability is sanitized: only the policy schema version and aggregate
per-category counts are printed; identifiers, HMACs, content, SQL, tokens
and secrets never appear. Failures emit a sanitized event and exit 1.

The entrypoint is testable without network by injecting ``runner_factory``
(or ``repository``/``clock``) in tests.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from .observability import EVENT_RETENTION_FAILED, emit_event
from .retention import (
    RetentionConfig,
    RetentionRepository,
    RetentionRunner,
    SupabaseRetentionRepository,
)
from .turn_execution import TurnExecutionConfig

logger = logging.getLogger(__name__)


class RetentionRuntimeConfigurationError(Exception):
    """Raised when the retention runtime environment is incomplete.

    The message is a constant; missing values are never echoed.
    """


@dataclass(frozen=True)
class RetentionRuntimeConfig:
    """Minimal, fail-closed runtime configuration for the retention CLI.

    Deliberately does NOT require the full application ``Settings`` (no
    Groq keys, no CORS): an operational retention round only needs the
    Supabase surface and the transport timeout.
    """

    supabase_url: str
    supabase_service_role_key: str  # never logged or echoed
    turn_config: TurnExecutionConfig

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "RetentionRuntimeConfig":
        source: Mapping[str, str] = os.environ if env is None else env
        url = source.get("SUPABASE_URL")
        key = source.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RetentionRuntimeConfigurationError(
                "retention runtime configuration is incomplete"
            )
        return cls(
            supabase_url=url,
            supabase_service_role_key=key,
            turn_config=TurnExecutionConfig.from_env(dict(source)),
        )


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Katherine Bot — operational data retention. Runs one round "
            "and exits."
        )
    )
    parser.add_argument(
        "--once",
        action="store_true",
        required=True,
        help="Run a single retention round and exit (the only supported mode)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Rows purged per statement (default: from RetentionConfig)",
    )
    return parser.parse_args(argv)


def _build_client(config: RetentionRuntimeConfig) -> Any:
    """Construct the Supabase client for the retention round."""
    from supabase import create_client
    from supabase.lib.client_options import SyncClientOptions

    options = SyncClientOptions(
        postgrest_client_timeout=config.turn_config.supabase_timeout
    )
    return create_client(
        config.supabase_url, config.supabase_service_role_key, options=options
    )


def main(
    argv: Optional[list[str]] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    runner_factory: Optional[Callable[..., RetentionRunner]] = None,
    repository: Optional[RetentionRepository] = None,
    clock: Optional[Callable[[], float]] = None,
) -> int:
    """Run one retention round and return the process exit code.

    Args:
        argv: Override for ``sys.argv`` (used in tests).
        env: Override for the environment (used in tests). When ``None`` the
            real ``os.environ`` is read.
        runner_factory: Callable that builds the runner from the runtime
            config; injected in tests to avoid network. When ``None`` a real
            ``SupabaseRetentionRepository`` backed runner is built.
        repository: Optional injected repository (overrides the default
            repository inside the runner).
        clock: Optional injected clock for the runner.

    Returns:
        ``0`` on success, ``1`` on any failure.
    """
    args = _parse_args(argv)
    try:
        config = RetentionRuntimeConfig.from_env(env)
        if runner_factory is not None:
            runner = runner_factory(config)
        else:
            client = _build_client(config)
            repo = repository if repository is not None else SupabaseRetentionRepository(client)
            runner = RetentionRunner(
                repository=repo,
                config=RetentionConfig(batch_size=args.batch_size)
                if args.batch_size is not None
                else RetentionConfig(),
                turn_config=config.turn_config,
                clock=clock,
            )
        result = asyncio.run(runner.run_once())
    except Exception:
        emit_event(logger, EVENT_RETENTION_FAILED, level=logging.ERROR, code="round_failed")
        return 1

    # Sanitized aggregate summary: version and counts only.
    print(f"retention_round schema_version={result.schema_version}")
    for category, category_result in result.results.items():
        print(
            f"retention_round category={category} "
            f"purged={category_result.purged} batches={category_result.batches}"
        )
    print(f"retention_round total_purged={result.total_purged}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
