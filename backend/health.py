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
import concurrent.futures
import inspect
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Optional, Protocol

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
    """Minimal database/Supabase access check with a bounded, non-abandoning probe.

    The probe runs on a dedicated single-worker executor whose transport
    timeout is aligned with the readiness timeout (see
    :func:`build_health_registry` and ``backend.dependencies``). While one
    probe is still in flight (including after the registry-level timeout
    cancelled the await), further polls fail fast instead of queueing new
    work, so repeated readiness polling can never accumulate threads or
    exhaust the executor. When the in-flight probe self-terminates (bounded
    transport), the next poll runs a fresh probe.

    It never touches user data: no rows are read into memory beyond the
    transport-level response and no content is logged.
    """

    name = "database"

    def __init__(
        self,
        client: Any,
        timeout_seconds: float,
        *,
        probe_executor: Optional[concurrent.futures.Executor] = None,
        owns_client: bool = False,
    ) -> None:
        self._client = client
        self.timeout_seconds = timeout_seconds
        self._executor = probe_executor or concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="readiness-db",
        )
        self._probe_future: Optional[concurrent.futures.Future] = None
        self._closed = False
        # When True, this check owns the probe client and closes it in
        # ``aclose`` only after the in-flight probe is proven done.
        self._owns_client = owns_client

    def close(self) -> None:
        """Synchronous best-effort close: stop new probes and release the
        executor without waiting.

        Deliberately does NOT close the probe client: an in-flight probe
        thread may still be using it. Use :meth:`aclose` during async
        shutdown for the full drain-and-close protocol.
        """
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def aclose(self) -> None:
        """Full close protocol: stop new probes, drain the in-flight probe
        with a bounded wait, then release the executor and close the owned
        probe client. Cleanup is unconditional:

        * If the probe completed (success or exception), the worker thread
          has terminated and the client is closed after the drain. A probe
          exception is never propagated: it would otherwise escape the drain
          and skip executor/client cleanup.
        * If the drain bound expires (a probe ignoring its aligned transport
          timeout), the owned client is still closed: closing the transport
          fails the in-flight call, so the worker thread terminates instead
          of surviving the lifespan. The future reference is always cleared.
        """
        self._closed = True
        future = self._probe_future
        drained = future is None or future.done()
        if future is not None and not future.done():
            try:
                await asyncio.wait_for(
                    asyncio.wrap_future(future),
                    timeout=self.timeout_seconds + 1.0,
                )
                drained = True
            except (asyncio.TimeoutError, concurrent.futures.TimeoutError):
                # The probe is past the drain bound; closing the client below
                # fails its in-flight call and terminates the worker thread.
                drained = False
            except Exception:
                # The probe completed exceptionally: the worker thread has
                # terminated. Cleanup below is safe; never propagate it.
                drained = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._probe_future = None
        if self._owns_client:
            client, self._client = self._client, None
            closer = getattr(client, "aclose", None) or getattr(client, "close", None)
            if closer is not None:
                try:
                    result = closer()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    emit_event(
                        logger,
                        EVENT_READINESS_CHECK_FAILED,
                        level=logging.ERROR,
                        component=self.name,
                    )

    async def run(self) -> None:
        if self._client is None or self._closed:
            raise CheckFailure()
        if self._probe_future is not None and not self._probe_future.done():
            # A previous probe is still in flight. Its aligned transport
            # timeout will release the worker; never pile up work behind it.
            raise CheckFailure()
        if self._probe_future is None or self._probe_future.done():
            self._probe_future = self._executor.submit(self._probe)
        future = self._probe_future
        try:
            response = await asyncio.wait_for(
                asyncio.wrap_future(future),
                timeout=self.timeout_seconds,
            )
        except (asyncio.TimeoutError, concurrent.futures.TimeoutError):
            raise CheckFailure() from None
        except Exception:
            raise CheckFailure() from None
        finally:
            # Guarded clear: only drop the reference this call owns. A
            # concurrent poll may have already replaced the reference with a
            # newer probe future before this finally runs; clearing it would
            # let a third poll submit extra work and break the single-probe
            # invariant. The comparison is atomic in the event loop because
            # no await happens between reading and clearing.
            if self._probe_future is future and future.done():
                self._probe_future = None
        if response is None:
            raise CheckFailure()

    def _probe(self) -> Any:
        result = self._client.table("profiles").select("user_id").limit(1).execute()
        if result is None:
            raise CheckFailure()
        if getattr(result, "error", None):
            raise CheckFailure()
        return result


class AuthClientCheck:
    """Checks that the real authentication surface used by the routes is
    effectively callable (``client.auth.get_user``), not merely present.

    Attribute existence is not enough: ``hasattr(client, "auth")`` passes for
    ``None`` and for objects without a ``get_user`` method, yet the request
    auth dependency calls ``auth_client.auth.get_user(token)``. The check
    therefore verifies the exact callable path the routes use. It never
    invokes ``get_user`` (that would need a token and hit the network).

    A healthy probe client can never substitute for this: if the engine's
    real Supabase client failed to build, ``/chat`` and ``/history`` cannot
    authenticate no matter how healthy the dedicated database probe is.
    """

    name = "auth"
    timeout_seconds = DEFAULT_CHECK_TIMEOUT_SECONDS

    def __init__(self, auth_client: Any) -> None:
        self._auth_client = auth_client

    async def run(self) -> None:
        client = self._auth_client
        if client is None:
            raise CheckFailure()
        try:
            auth = getattr(client, "auth", None)
            get_user = getattr(auth, "get_user", None) if auth is not None else None
        except Exception:
            # A surface that raises on attribute access is not usable either.
            raise CheckFailure() from None
        if not callable(get_user):
            raise CheckFailure()


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
    persistence_client: Any = None,
    database_probe_client: Any = None,
    owns_probe_client: bool = False,
) -> HealthRegistry:
    """Build the default ordered registry for the running configuration.

    Components:
    1. ``configuration`` — validated settings (frozen model, fully revalidated).
    2. ``auth`` — the REAL authentication surface used by the routes exists.
    3. ``database`` — minimal Supabase read via a dedicated probe client whose
       transport timeout is aligned with ``readiness_database_timeout_ms``;
       never a substitute for the real surfaces.
    4. ``persistence`` — the REAL persistence surface used by admission and
       history exists.
    5. ``provider`` — provider keys/client path, bounded by
       ``readiness_provider_timeout_ms``.
    6. ``embeddings`` — only when ``embeddings_retrieval_enabled`` is true.

    ``lifespan`` is appended by the application at request time because it
    observes ``app.state``. When ``owns_probe_client`` is true the registry
    (through ``DatabaseCheck.aclose``) closes the probe client after draining
    any in-flight probe.
    """
    registry = HealthRegistry()
    registry.add(
        ConfigurationCheck(settings)
    )
    registry.add(
        AuthClientCheck(auth_client)
    )
    registry.add(
        DatabaseCheck(
            database_probe_client,
            timeout_seconds=settings.readiness_database_timeout_ms / 1000.0,
            owns_client=owns_probe_client,
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
