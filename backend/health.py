"""Readiness checks for the Katherine Bot backend.

Semantics
=========

* ``/live`` (see ``backend.main``) proves only process/event-loop vitality and
  never touches dependencies.
* ``/ready`` aggregates small, typed checks over the critical dependencies an
  instance needs to accept nominal traffic: valid configuration, minimal
  database access, provider path availability, resources of the enabled
  feature mode, and a completed lifespan.

Check contract
==============

Each check implements :class:`ReadinessCheck` and is wrapped by
:class:`HealthRegistry` with an explicit per-check timeout. Checks never
execute a full generation, never include user content, and never return raw
exception text. A failed or timed-out check produces a sanitized
``unavailable`` result with no payload details.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Optional, Protocol

import httpx

from .observability import (
    EVENT_READINESS_CHECK_FAILED,
    emit_event,
)
from .settings import Settings

logger = logging.getLogger(__name__)

#: Fallback timeout for checks that do not declare their own.
DEFAULT_CHECK_TIMEOUT_SECONDS = 1.0


class CheckStatus(str, Enum):
    """Stable, sanitized readiness status values."""

    ok = "ok"
    unavailable = "unavailable"


@dataclass(frozen=True)
class CheckResult:
    """Result of one readiness check (name + sanitized status only)."""

    name: str
    status: CheckStatus


class CheckFailure(Exception):
    """Internal signal that a check did not pass (never rendered raw)."""


class ReadinessCheck(Protocol):
    """A single, cheap readiness check."""

    name: str
    timeout_seconds: float

    async def run(self) -> None:  # pragma: no cover - protocol
        """Return ``None`` when healthy; raise ``CheckFailure`` otherwise."""
        ...


@dataclass
class HealthRegistry:
    """Ordered registry of readiness checks with per-check timeouts.

    Order is deterministic: insertion order, which the default builder
    fixes as ``configuration``, ``auth``, ``database``, ``persistence``,
    ``provider``, then ``embeddings`` (only when the retrieval feature is
    enabled).

    The registry is closable: ``close()`` (sync, best-effort) and
    ``aclose()`` (full async protocol) delegate to every registered check,
    so the application can own the whole registry as one lifecycle resource.
    """

    checks: Mapping[str, ReadinessCheck] = field(default_factory=dict)

    def names(self) -> tuple[str, ...]:
        return tuple(self.checks)

    def add(self, check: ReadinessCheck) -> None:
        """Register one check (idempotent by name)."""
        self.checks[check.name] = check

    def close(self) -> None:
        """Close every closable check without aborting on individual failures.

        Synchronous best-effort variant used for partial-startup cleanup: it
        does NOT drain in-flight probes nor close owned probe clients. Use
        :meth:`aclose` during the normal async shutdown for the full protocol.
        """
        for check in self.checks.values():
            closer = getattr(check, "close", None)
            if closer is None:
                continue
            try:
                closer()
            except Exception:
                emit_event(
                    logger,
                    EVENT_READINESS_CHECK_FAILED,
                    level=logging.ERROR,
                    component=check.name,
                )

    async def aclose(self) -> None:
        """Full asynchronous close of every closable check.

        Prefers each check's ``aclose`` (which drains an in-flight probe with
        a bounded wait before closing the owned probe client, so the client is
        never invalidated underneath a live thread) and falls back to
        ``close``. Never aborts on individual failures.
        """
        for check in self.checks.values():
            closer = getattr(check, "aclose", None) or getattr(check, "close", None)
            if closer is None:
                continue
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                emit_event(
                    logger,
                    EVENT_READINESS_CHECK_FAILED,
                    level=logging.ERROR,
                    component=check.name,
                )

    async def run_all(self) -> list[CheckResult]:
        """Run every check in order with its own explicit timeout.

        A check that raises, times out, or never completes becomes
        ``unavailable``. Raw exception text is never propagated: callers only
        see names and statuses. A ``readiness_check_failed`` event is emitted
        per failing component with the sanitized component name.
        """
        results: list[CheckResult] = []
        for name, check in self.checks.items():
            timeout = getattr(check, "timeout_seconds", DEFAULT_CHECK_TIMEOUT_SECONDS)
            try:
                await asyncio.wait_for(check.run(), timeout=timeout)
                results.append(CheckResult(name, CheckStatus.ok))
            except asyncio.CancelledError:
                # Cancellation of the ready request must propagate, but the
                # check itself must not leak.
                raise
            except Exception:
                emit_event(
                    logger,
                    EVENT_READINESS_CHECK_FAILED,
                    level=logging.ERROR,
                    component=name,
                )
                results.append(CheckResult(name, CheckStatus.unavailable))
        return results


# ─── Concrete checks ────────────────────────────────────────────────────────


class ConfigurationCheck:
    """Validates that the running settings are still valid (no I/O).

    Settings are fully validated at construction; this check re-runs the
    cheap cross-field validation and guards against a missing or mutated
    settings reference.
    """

    name = "configuration"
    timeout_seconds = DEFAULT_CHECK_TIMEOUT_SECONDS

    def __init__(self, settings: Settings | None) -> None:
        self._settings = settings

    async def run(self) -> None:
        if self._settings is None:
            raise CheckFailure()
        try:
            self._settings.ensure_valid()
        except Exception:
            raise CheckFailure() from None


class DatabaseCheck:
    """Minimal database/Supabase access check with a cancelable async probe.

    The probe is an ``async`` callable performing a bounded HTTP request (the
    transport timeout is aligned with the readiness timeout; see
    :func:`build_health_registry` and ``backend.dependencies``). Because the
    probe is plain async I/O, the registry-level ``asyncio.wait_for`` truly
    cancels it: no worker thread survives a timeout, so repeated polling can
    never accumulate threads and ``aclose()`` needs no deferred cleanup.
    While one probe is still in flight, further polls fail fast (single-probe
    guard), so concurrent readiness polls do not stack duplicate requests.

    It never touches user data: no rows are read into memory beyond the
    transport-level response and no content is logged.
    """

    name = "database"

    def __init__(
        self,
        probe: Optional[Callable[[], Awaitable[None]]],
        timeout_seconds: float,
    ) -> None:
        self._probe = probe
        self.timeout_seconds = timeout_seconds
        self._inflight: Optional[asyncio.Task] = None
        self._closed = False

    def close(self) -> None:
        """Synchronous best-effort close: reject new probes. No threads exist,
        so there is nothing else to release here; use :meth:`aclose` during
        async shutdown to cancel any in-flight probe."""
        self._closed = True

    async def aclose(self) -> None:
        """Full close protocol: reject new probes and cancel any in-flight
        probe, awaiting its termination before returning.

        The probe is async HTTP I/O, so cancellation actually stops the
        request; no fire-and-forget cleanup is needed and no owned work
        outlives the lifespan.
        """
        self._closed = True
        task, self._inflight = self._inflight, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    async def run(self) -> None:
        if self._probe is None or self._closed:
            raise CheckFailure()
        if self._inflight is not None and not self._inflight.done():
            # A previous probe is still in flight; never pile up duplicate
            # requests behind it.
            raise CheckFailure()
        task = asyncio.ensure_future(self._probe())
        self._inflight = task
        try:
            await asyncio.wait_for(task, timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            raise CheckFailure() from None
        except Exception:
            raise CheckFailure() from None
        finally:
            if self._inflight is task:
                self._inflight = None
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task


def _auth_health_probe(
    auth_url: str,
    apikey: Optional[str],
    timeout: float,
) -> Callable[[], Awaitable[None]]:
    """Build the default Auth service availability probe.

    Performs a bounded ``GET {auth_url}/health`` (GoTrue health endpoint)
    over async HTTP, so the operation is truly cancelable by the readiness
    timeout (no worker thread can outlive it). The probe never depends on a
    user token and never reads user data: only the HTTP status is observed
    and the body is discarded, so nothing sensitive reaches responses or
    logs. ``timeout`` is the transport bound, aligned with the readiness
    timeout.
    """
    health_url = f"{auth_url.rstrip('/')}/health"

    async def probe() -> None:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                headers = {"apikey": apikey} if apikey else {}
                response = await client.get(health_url, headers=headers)
                if response.status_code != 200:
                    raise CheckFailure()
        except CheckFailure:
            raise
        except Exception:
            raise CheckFailure() from None

    return probe


def _database_health_probe(
    supabase_url: str,
    service_role_key: Optional[str],
    timeout: float,
) -> Callable[[], Awaitable[None]]:
    """Build the default database/Supabase availability probe.

    Performs a bounded ``GET {supabase_url}/rest/v1/profiles`` with
    ``select=user_id&limit=1`` (the same read the previous SDK probe
    executed) over async HTTP, so it is truly cancelable by the readiness
    timeout. Uses the service-role credentials from validated settings; the
    response body is discarded and only the HTTP status is observed, so no
    user data or secret reaches responses or logs.
    """
    rest_url = f"{supabase_url.rstrip('/')}/rest/v1/profiles"

    async def probe() -> None:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                headers = {
                    "apikey": service_role_key,
                    "Authorization": f"Bearer {service_role_key}",
                }
                response = await client.get(
                    rest_url,
                    params={"select": "user_id", "limit": 1},
                    headers=headers,
                )
                if response.status_code != 200:
                    raise CheckFailure()
        except CheckFailure:
            raise
        except Exception:
            raise CheckFailure() from None

    return probe


class AuthClientCheck:
    """Checks that the Auth service can actually authenticate requests.

    Two independent proofs, both cheap and sanitized:

    1. Surface: the exact route path ``auth_client.auth.get_user`` exists and
       is callable (never invoked; no token involved).
    2. Availability: a bounded async probe of the Auth service (default: HTTP
       GET of the GoTrue ``/health`` endpoint with the transport timeout
       aligned to ``readiness_auth_timeout_ms``). This is a real network
       probe, so a healthy PostgREST/database probe can never mask an Auth
       outage.

    Either proof failing makes the component ``unavailable``: an instance
    whose ``/chat`` and ``/history`` authentication would 503 must not report
    ready. The probe is plain async I/O, so the readiness timeout truly
    cancels it (no worker thread can outlive it) and ``aclose()`` cancels any
    in-flight probe before returning.
    """

    name = "auth"

    def __init__(
        self,
        auth_client: Any,
        timeout_seconds: float,
        *,
        probe: Optional[Callable[[], Awaitable[None]]] = None,
    ) -> None:
        self._auth_client = auth_client
        self.timeout_seconds = timeout_seconds
        self._probe = probe
        self._inflight: Optional[asyncio.Task] = None
        self._closed = False

    def close(self) -> None:
        """Synchronous best-effort close: reject new probes. No threads exist,
        so there is nothing else to release here; use :meth:`aclose` during
        async shutdown to cancel any in-flight probe."""
        self._closed = True

    async def aclose(self) -> None:
        """Full close protocol: reject new probes and cancel any in-flight
        probe, awaiting its termination before returning. No fire-and-forget
        cleanup is needed and no owned work outlives the lifespan."""
        self._closed = True
        task, self._inflight = self._inflight, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task

    async def run(self) -> None:
        # 1. The exact callable path the routes use must exist.
        client = self._auth_client
        if client is None:
            raise CheckFailure()
        try:
            auth = getattr(client, "auth", None)
            get_user = getattr(auth, "get_user", None) if auth is not None else None
        except Exception:
            raise CheckFailure() from None
        if not callable(get_user):
            raise CheckFailure()
        # 2. The Auth service itself must be reachable.
        if self._probe is None or self._closed:
            raise CheckFailure()
        if self._inflight is not None and not self._inflight.done():
            raise CheckFailure()
        task = asyncio.ensure_future(self._probe())
        self._inflight = task
        try:
            await asyncio.wait_for(task, timeout=self.timeout_seconds)
        except asyncio.TimeoutError:
            raise CheckFailure() from None
        except Exception:
            raise CheckFailure() from None
        finally:
            if self._inflight is task:
                self._inflight = None
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task


class PersistenceClientCheck:
    """Checks that the real persistence surface used by the routes is
    effectively callable: ``table(...)`` and ``rpc(...)`` must exist, be
    callable, and be invocable without raising.

    Attribute existence is not enough: ``hasattr(client, "table")`` passes
    for ``table=None``, yet history calls ``table("chat_logs").select(...)``
    and admission calls ``rpc("reserve_admission", ...)``. The check builds a
    query and an RPC request with benign arguments; on the real Supabase
    client that is pure object construction (no network I/O), so the proof is
    cheap and honest. A dedicated probe client can never substitute for this
    surface.
    """

    name = "persistence"
    timeout_seconds = DEFAULT_CHECK_TIMEOUT_SECONDS

    def __init__(self, persistence_client: Any) -> None:
        self._persistence_client = persistence_client

    async def run(self) -> None:
        client = self._persistence_client
        if client is None:
            raise CheckFailure()
        try:
            table = getattr(client, "table", None)
            rpc = getattr(client, "rpc", None)
        except Exception:
            # A surface that raises on attribute access is not usable either.
            raise CheckFailure() from None
        if not callable(table) or not callable(rpc):
            raise CheckFailure()
        try:
            table_builder = table("chat_logs")
            rpc_builder = rpc("match_memories", {})
        except Exception:
            # The surfaces exist but fail when invoked; routes cannot use them.
            raise CheckFailure() from None
        if table_builder is None or rpc_builder is None:
            raise CheckFailure()


class ProviderCheck:
    """Provider path availability check (configuration-level, no generation).

    Verifies that the Groq manager holds validated keys and is ready to build
    clients. It deliberately never runs a completion, never sends a request,
    and never loads models. Deployments that want a real network probe must
    inject their own check with an explicit, documented timeout.
    """

    name = "provider"

    def __init__(self, groq_manager: Any, timeout_seconds: float) -> None:
        self._groq_manager = groq_manager
        self.timeout_seconds = timeout_seconds

    async def run(self) -> None:
        if self._groq_manager is None:
            raise CheckFailure()
        try:
            configured = await asyncio.to_thread(self._groq_manager.is_configured)
        except Exception:
            raise CheckFailure() from None
        if not configured:
            raise CheckFailure()


class EmbeddingsCheck:
    """Checks that embeddings are available when the feature mode requires them.

    Only registered when ``embeddings_retrieval_enabled`` is true. The model is
    loaded once during startup; this check only verifies the loaded resource
    exists and never loads a model by itself. A missing model means the active
    mode cannot retrieve vector memory, so the instance must not serve
    traffic.
    """

    name = "embeddings"
    timeout_seconds = DEFAULT_CHECK_TIMEOUT_SECONDS

    def __init__(self, memory_manager: Any) -> None:
        self._memory_manager = memory_manager

    async def run(self) -> None:
        if self._memory_manager is None:
            raise CheckFailure()
        if getattr(self._memory_manager, "embedding_model", None) is None:
            raise CheckFailure()


class LifespanCheck:
    """Checks that the application lifespan has completed startup.

    The provider is a callable returning the current ``lifespan_started``
    flag from ``app.state``, so the check always observes the live state.
    """

    name = "lifespan"
    timeout_seconds = DEFAULT_CHECK_TIMEOUT_SECONDS

    def __init__(self, state_provider: Callable[[], bool]) -> None:
        self._state_provider = state_provider

    async def run(self) -> None:
        try:
            started = bool(self._state_provider())
        except Exception:
            raise CheckFailure() from None
        if not started:
            raise CheckFailure()


# ─── Default registry builder ───────────────────────────────────────────────


def build_health_registry(
    settings: Settings,
    engine: Any,
    *,
    auth_client: Any = None,
    auth_probe: Optional[Callable[[], Awaitable[None]]] = None,
    persistence_client: Any = None,
    database_probe: Optional[Callable[[], Awaitable[None]]] = None,
) -> HealthRegistry:
    """Build the default ordered registry for the running configuration.

    Components:
    1. ``configuration`` — validated settings (frozen model, fully revalidated).
    2. ``auth`` — the REAL authentication surface used by the routes is
       callable AND the Auth service is reachable via a bounded async
       availability probe (``auth_probe``; default builds the GoTrue /health
       probe).
    3. ``database`` — minimal Supabase read via a bounded async probe
       (``database_probe``; default builds the PostgREST /rest/v1/profiles
       probe with transport aligned to ``readiness_database_timeout_ms``).
       Probes are plain async I/O: the readiness timeout truly cancels them,
       so no worker thread or owned resource can outlive the lifespan.
    4. ``persistence`` — the REAL persistence surface used by admission and
       history exists.
    5. ``provider`` — provider keys/client path, bounded by
       ``readiness_provider_timeout_ms``.
    6. ``embeddings`` — only when ``embeddings_retrieval_enabled`` is true.

    ``lifespan`` is appended by the application at request time because it
    observes ``app.state``. When a probe is omitted, its component is
    unavailable (an instance whose critical dependency cannot be probed must
    not report ready).
    """
    registry = HealthRegistry()
    registry.add(
        ConfigurationCheck(settings)
    )
    registry.add(
        AuthClientCheck(
            auth_client,
            timeout_seconds=settings.readiness_auth_timeout_ms / 1000.0,
            probe=auth_probe,
        )
    )
    registry.add(
        DatabaseCheck(
            database_probe,
            timeout_seconds=settings.readiness_database_timeout_ms / 1000.0,
        )
    )
    registry.add(
        PersistenceClientCheck(persistence_client)
    )
    registry.add(
        ProviderCheck(
            engine.groq_manager,
            timeout_seconds=settings.readiness_provider_timeout_ms / 1000.0,
        )
    )
    if settings.embeddings_retrieval_enabled:
        registry.add(EmbeddingsCheck(engine.memory_manager))
    return registry


def build_ready_response(
    results: list[CheckResult],
    lifespan_started: bool,
) -> tuple[int, dict]:
    """Aggregate check results into the deterministic readiness response.

    Returns ``(http_status, body)``. The body schema is stable:

    .. code-block:: json

        {"status": "ready", "components": {"configuration": "ok", ...}}

    Component order is deterministic. Only sanitized status values are
    included; no URLs, keys, names, counts, or exception text.
    """
    components: dict[str, str] = {}
    for result in results:
        components[result.name] = result.status.value
    components["lifespan"] = (
        CheckStatus.ok.value if lifespan_started else CheckStatus.unavailable.value
    )
    ready = all(status == CheckStatus.ok.value for status in components.values())
    if ready:
        return 200, {"status": "ready", "components": components}
    return 503, {"status": "not_ready", "components": components}
