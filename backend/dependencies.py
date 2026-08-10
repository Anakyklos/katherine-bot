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

from .account_deletion import SupabaseAccountDeletionRepository
from .account_deletion_service import AccountDeletionService
from .admission import AdmissionRuntimeConfig
from .chat_engine import ChatConversationEngine
from .health import build_health_registry, HealthRegistry
from .observability import (
    EVENT_APP_SHUTDOWN_FAILED,
    EVENT_SUPABASE_CLIENT_CREATION_FAILED,
    emit_event,
)
from .privacy_service import PrivacyService, SupabasePrivacyRepository
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
    privacy_service: Any = None
    account_deletion_service: Any = None


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


def _build_database_probe(settings: Settings) -> Optional[Callable[[], Awaitable[None]]]:
    """Build the real database/Supabase availability probe from settings.

    The probe is a bounded async HTTP read of PostgREST
    (``{supabase_url}/rest/v1/profiles?select=user_id&limit=1``) with the
    transport timeout aligned to ``readiness_database_timeout_ms``. Because
    it is plain async I/O, the readiness timeout truly cancels it (no worker
    thread can outlive the lifespan). Returns ``None`` when the runtime
    Supabase configuration is absent (local/test without a database), which
    makes the ``database`` check fail honestly.
    """
    url = settings.supabase_url
    key = (
        settings.supabase_service_role_key.get_secret_value()
        if settings.supabase_service_role_key is not None
        else None
    )
    if not url or not key:
        return None
    from .health import _database_health_probe

    return _database_health_probe(
        url,
        key,
        settings.readiness_database_timeout_ms / 1000.0,
    )


def _build_auth_probe(settings: Settings) -> Optional[Callable[[], Awaitable[None]]]:
    """Build the real Auth service availability probe from validated settings.

    Probes ``{supabase_url}/auth/v1/health`` (GoTrue health endpoint) with a
    transport timeout aligned to ``readiness_auth_timeout_ms`` over async
    HTTP, so the readiness timeout truly cancels it. Returns ``None`` when no
    Supabase URL is configured; the readiness ``auth`` component then fails
    honestly (an instance without a probed Auth service must not report
    ready).
    """
    url = settings.supabase_url
    if not url:
        return None
    key = (
        settings.supabase_service_role_key.get_secret_value()
        if settings.supabase_service_role_key is not None
        else None
    )
    from .health import _auth_health_probe

    return _auth_health_probe(
        f"{url.rstrip('/')}/auth/v1",
        key,
        settings.readiness_auth_timeout_ms / 1000.0,
    )


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
        health_checks = build_health_registry(
            settings,
            engine,
            auth_client=supabase_client,
            auth_probe=_build_auth_probe(settings),
            persistence_client=supabase_client,
            database_probe=_build_database_probe(settings),
        )
        created.append(health_checks)

        # Stateless application service for the #315 privacy HTTP actions.
        # It receives identity and operation_id per call and never stores
        # per-user state; the injected clock and the operational turn
        # configuration drive reset timestamps and the write budget.
        privacy_service = PrivacyService(
            repository=SupabasePrivacyRepository(supabase_client),
            turn_config=settings.turn_config,
            clock=time.time,
        )

        # Stateless application service for the #326 account deletion API.
        # It reuses the SAME Supabase client (never creates a second one), the
        # same admission secret for the server-derived HMAC reference, and the
        # same operational timeout/budget. Identity and operation_id are
        # per-call arguments; the container stores no user state.
        account_deletion_service = AccountDeletionService(
            repository=SupabaseAccountDeletionRepository(supabase_client),
            turn_config=settings.turn_config,
            admission_config=admission_config,
        )

        dependencies = ApplicationDependencies(
            conversation_engine=engine,
            auth_client=supabase_client,
            admission_config=admission_config,
            turn_config=settings.turn_config,
            health_checks=health_checks,
            clock=time.time,
            persistence_client=supabase_client,
            privacy_service=privacy_service,
            account_deletion_service=account_deletion_service,
        )

        # Owned resources created by this builder, closed at shutdown and on
        # partial startup:
        # * ``health_checks`` is the sole owned resource: its async close
        #   cancels any in-flight readiness probe (async HTTP I/O, so the
        #   cancellation actually stops the request) and awaits its
        #   termination before returning, so no owned work outlives the
        #   lifespan. ``close()`` (sync) is used for partial-startup cleanup
        #   and only rejects new probes.
        # * Readiness probes are plain async HTTP requests (no client object,
        #   no executor, no thread), so there is no probe client to own.
        # The engine graph itself does not expose close()/aclose() contracts
        # today (Groq clients are request-scoped; the pinned Supabase SDK has
        # no close), so it is not part of the owned tuple.
        owned: tuple[Any, ...] = (health_checks,)

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
