"""Explicit dependency container and default composition for the backend.

Ownership contract
==================

* Resources created by :func:`build_default_dependencies` are **owned** by the
  application: the lifespan closes them on shutdown.
* Resources injected externally through :class:`ApplicationDependencies` keep
  their ownership with the caller: the application never closes them, unless
  the caller explicitly adds them to ``owned_resources``.

The container holds only process-wide, thread-safe infrastructure. It never
stores per-user state: authenticated identity continues to be resolved per
request, and emotional/relational snapshots never live here.
"""

from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .admission import AdmissionRuntimeConfig
from .chat_engine import ChatConversationEngine
from .health import build_health_registry, HealthRegistry
from .observability import (
    EVENT_APP_SHUTDOWN_FAILED,
    EVENT_SUPABASE_CLIENT_CREATION_FAILED,
    emit_event,
)
from .settings import Settings
from .turn_execution import TurnExecutionConfig

logger = logging.getLogger(__name__)


@dataclass
class ApplicationDependencies:
    """Composition root for the active request path.

    ``auth_client`` is the authenticated-auth surface used by the request
    auth dependency (the Supabase client). ``persistence_client`` is the
    persistence surface used by the history and admission routes; it is
    exposed explicitly so routes never navigate engine internals and the two
    surfaces can be injected independently in tests. ``health_checks``
    aggregate the readiness probes. ``clock`` provides wall time for domain
    code.
    """

    conversation_engine: ChatConversationEngine
    auth_client: Any
    admission_config: AdmissionRuntimeConfig
    turn_config: TurnExecutionConfig
    health_checks: HealthRegistry
    clock: Callable[[], float] = field(default_factory=time.time)
    persistence_client: Any = None


def _supabase_factory_from_settings(settings: Settings) -> Callable[[], Optional[Any]]:
    """Build the Supabase client factory from validated settings.

    Returns a factory that yields ``None`` when the runtime configuration is
    absent (local/test without a Supabase), preserving the historical
    degraded behavior, and a configured client otherwise.
    """
    url = settings.supabase_url
    key = (
        settings.supabase_service_role_key.get_secret_value()
        if settings.supabase_service_role_key is not None
        else None
    )
    if not url or not key:
        return lambda: None

    timeout = settings.turn_config.supabase_timeout

    def factory() -> Optional[Any]:
        try:
            from supabase import create_client
            from supabase.lib.client_options import ClientOptions

            options = ClientOptions(postgrest_client_timeout=timeout)
            return create_client(url, key, options=options)
        except Exception:
            emit_event(
                logger,
                EVENT_SUPABASE_CLIENT_CREATION_FAILED,
                level=logging.ERROR,
            )
            return None

    return factory


def _build_readiness_probe_client(settings: Settings) -> Optional[Any]:
    """Build the dedicated database probe client for readiness checks.

    The transport timeout is aligned with ``readiness_database_timeout_ms`` so
    a stuck probe self-terminates at the same bound the registry enforces via
    ``asyncio.wait_for``; the worker thread is never abandoned indefinitely.
    Returns ``None`` when the runtime Supabase configuration is absent
    (local/test without a database), which makes the ``database`` check fail
    honestly.
    """
    url = settings.supabase_url
    key = (
        settings.supabase_service_role_key.get_secret_value()
        if settings.supabase_service_role_key is not None
        else None
    )
    if not url or not key:
        return None
    try:
        from supabase import create_client
        from supabase.lib.client_options import ClientOptions

        options = ClientOptions(
            postgrest_client_timeout=settings.readiness_database_timeout_ms / 1000.0
        )
        return create_client(url, key, options=options)
    except Exception:
        emit_event(
            logger,
            EVENT_SUPABASE_CLIENT_CREATION_FAILED,
            level=logging.ERROR,
        )
        return None


def _close_sync(resource: Any) -> None:
    """Close one resource synchronously during partial startup cleanup."""
    closer = getattr(resource, "close", None)
    if closer is None:
        return
    try:
        closer()
    except Exception:
        # Sanitized: never log exception text, secrets, or identifiers.
        emit_event(
            logger,
            EVENT_APP_SHUTDOWN_FAILED,
            level=logging.ERROR,
        )


def build_default_dependencies(
    settings: Settings,
) -> tuple[ApplicationDependencies, tuple[Any, ...]]:
    """Build the default composition from validated settings.

    Returns ``(dependencies, owned_resources)``. Everything this function
    creates is owned by the application and closed at shutdown. This is the
    only place where real Groq/Supabase/embedding resources are constructed
    in the default path; it must only run inside the lifespan, never at
    import time.

    On partial failure, resources already created are closed before the
    exception propagates, so a broken startup never leaks owned resources.
    """
    created: list[Any] = []
    try:
        engine = ChatConversationEngine(
            archival_extraction_enabled=settings.archival_extraction_enabled,
            embeddings_enabled=settings.embeddings_retrieval_enabled,
            turn_config=settings.turn_config,
            groq_keys=list(settings.provider_keys()),
            supabase_factory=_supabase_factory_from_settings(settings),
        )
        created.append(engine)

        secret, cidrs = settings.to_admission_values()
        admission_config = AdmissionRuntimeConfig.from_values(
            secret,
            cidrs,
        )

        supabase_client = engine.memory_manager.supabase
        probe_client = _build_readiness_probe_client(settings)
        created.append(probe_client)
        health_checks = build_health_registry(
            settings,
            engine,
            database_probe_client=probe_client,
        )
        created.append(health_checks)

        dependencies = ApplicationDependencies(
            conversation_engine=engine,
            auth_client=supabase_client,
            admission_config=admission_config,
            turn_config=settings.turn_config,
            health_checks=health_checks,
            clock=time.time,
            persistence_client=supabase_client,
        )

        # Owned resources created by this builder, closed at shutdown and on
        # partial startup:
        # * ``health_checks`` closes every closable check, including the
        #   database probe executor (no new probes are queued; an in-flight
        #   probe self-terminates via its aligned transport timeout).
        # * ``probe_client`` is closed when the SDK exposes a compatible
        #   close/aclose contract (the pinned version does not).
        # The engine graph itself does not expose close()/aclose() contracts
        # today (Groq clients are request-scoped; the pinned Supabase SDK has
        # no close), so it is not part of the owned tuple.
        owned: tuple[Any, ...] = (health_checks, probe_client)

        return dependencies, owned
    except BaseException:
        for resource in reversed(created):
            _close_sync(resource)
        raise


async def close_resource(resource: Any) -> None:
    """Close one resource if it exposes ``aclose`` or ``close``.

    Swallows and logs a sanitized event on failure so one broken resource
    never aborts the shutdown of the remaining ones.
    """
    closer = getattr(resource, "aclose", None) or getattr(resource, "close", None)
    if closer is None:
        return
    try:
        result = closer()
        if inspect.isawaitable(result):
            await result
    except Exception:
        # Sanitized: never log exception text, secrets, or identifiers.
        emit_event(
            logger,
            EVENT_APP_SHUTDOWN_FAILED,
            level=logging.ERROR,
        )


async def shutdown_dependencies(owned_resources: tuple[Any, ...]) -> None:
    """Close every owned resource, continuing past individual failures.

    Idempotent by contract: callers must clear the owned-resource list from
    ``app.state`` after the first invocation so a second shutdown closes
    nothing.
    """
    for resource in owned_resources:
        await close_resource(resource)
