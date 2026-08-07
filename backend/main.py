"""HTTP application factory for the Katherine Bot backend.

Lifecycle
=========

* Importing this module has no side effects beyond declaring the FastAPI
  application (``app = create_app()``): no sockets, no Groq/Supabase clients,
  no embedding models, no threads, no environment reads outside
  ``Settings.from_env()``.
* Heavy resources (engine, providers, persistence) are built inside the
  lifespan by :func:`backend.dependencies.build_default_dependencies` and
  owned by the application.
* ``create_app(settings=..., dependencies=...)`` accepts injected doubles so
  tests run without Groq, Supabase, embeddings, or network.

Health semantics
================

* ``GET /live``  — process/event-loop vitality only; never touches providers.
* ``GET /ready`` — real checks over critical dependencies with explicit
  timeouts; 503 when any critical component is unavailable.
* ``GET /health`` — legacy alias kept for compatibility; asserts only that
  the process is alive, never readiness.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, StrictStr, field_validator
from pydantic_core import PydanticCustomError
from supabase_auth.errors import AuthApiError, AuthRetryableError

from .admission import (
    ADMITTED,
    APPLICATION_RATE_LIMITED,
    INVALID_ADMISSION_INPUT,
    NETWORK_RATE_LIMITED,
    REQUEST_ID_CONFLICT,
    REQUEST_REPLAY_UNAVAILABLE,
    USER_DAILY_REQUEST_QUOTA_EXCEEDED,
    USER_DAILY_UNIT_QUOTA_EXCEEDED,
    USER_RATE_LIMITED,
    AdmissionResult,
    AdmissionUnavailable,
    build_admission_request,
    compute_turn_correlation,
    compute_user_reference,
    reserve_admission_sync,
    resolve_network_identity,
)
from .admission_contracts import AdmissionError, RequestIdentity, validate_new_message
from .atomic_turn_commit import ConflictError, PersistenceError
from .dependencies import (
    ApplicationDependencies,
    build_default_dependencies,
    shutdown_dependencies,
)
from .emotion_presentation import EmotionStateResponse
from .groq_manager import GroqPoolExhaustedError, GroqRequestError
from .health import build_ready_response
from .memory import StatePersistenceError
from .observability import (
    EVENT_APP_STARTUP_FAILED,
    EVENT_AUTH_COMPLETED,
    EVENT_AUTH_FAILED,
    EVENT_HTTP_RESULT,
    EVENT_REQUEST_CONFLICT,
    EVENT_TURN_COMPLETED,
    EVENT_TURN_FAILED,
    emit_event,
)
from .process_turn import TurnMode
from .runtime_containment import validate_worker_configuration
from .settings import Settings, SettingsConfigurationError
from .turn_execution import (
    DeadlineExceeded,
    TurnErrorCode,
    TurnExecutionError,
    create_budget,
    run_blocking_write,
)

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)


# ─── Dependency access from app.state ───────────────────────────────────────


def get_dependencies(request: Request) -> ApplicationDependencies:
    """Resolve the application dependency container from ``app.state``.

    Returns 503 when the lifespan has not completed startup OR is no longer
    active, so no endpoint can run against a partially initialized
    application or a composition whose owned resources were already closed
    (injected dependencies remain in ``app.state`` after shutdown, but routes
    still refuse to operate outside the lifespan).
    """
    deps = getattr(request.app.state, "dependencies", None)
    lifespan_started = bool(getattr(request.app.state, "lifespan_started", False))
    if deps is None or not lifespan_started:
        raise HTTPException(
            status_code=503,
            detail={"code": "service_unavailable", "message": "Service unavailable."},
        )
    return deps


def _duration_ms(started_at: float) -> float:
    """Render a monotonic elapsed time in milliseconds."""
    return (time.monotonic() - started_at) * 1000


async def get_turn_correlation(request: Request) -> Optional[str]:
    """Best-effort sanitized correlation reference for the current request.

    Parses the JSON body to extract the request identifier and computes the
    HMAC correlation under the dedicated turn-correlation domain. This lets
    auth events that belong to a turn carry the same correlation as the turn
    events. Never logs raw identifiers; returns ``None`` when the body is
    unavailable or invalid (for example on pre-validation auth failures).
    """
    deps = getattr(request.app.state, "dependencies", None)
    if deps is None or getattr(deps, "admission_config", None) is None:
        return None
    try:
        body = json.loads(await request.body())
        request_id = body.get("request_id") if isinstance(body, dict) else None
        if not request_id:
            return None
        return compute_turn_correlation(deps.admission_config, request_id)
    except Exception:
        return None


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    correlation: Optional[str] = Depends(get_turn_correlation),
):
    """Authenticate the bearer token against the server-side auth client.

    The authenticated identity is resolved per request and never stored in
    the container or any global. The auth surface is ``deps.auth_client``
    (the injected auth dependency), and only sanitized codes, a monotonic
    duration, and a non-reversible HMAC user reference are logged.
    """
    started_at = time.monotonic()
    if not credentials:
        emit_event(
            logger,
            EVENT_AUTH_FAILED,
            code="missing_credentials",
            duration_ms=_duration_ms(started_at),
            correlation=correlation,
        )
        raise HTTPException(
            status_code=401,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials
    deps = get_dependencies(request)
    auth_client = deps.auth_client
    try:
        if not auth_client:
            emit_event(
                logger,
                EVENT_AUTH_FAILED,
                code="service_unavailable",
                duration_ms=_duration_ms(started_at),
                correlation=correlation,
            )
            raise HTTPException(status_code=503, detail="Authentication service unavailable")
        auth_response = auth_client.auth.get_user(token)
        if not auth_response.user:
            emit_event(
                logger,
                EVENT_AUTH_FAILED,
                code="invalid_token",
                duration_ms=_duration_ms(started_at),
                correlation=correlation,
            )
            raise HTTPException(
                status_code=401,
                detail="Authentication failed",
                headers={"WWW-Authenticate": "Bearer"},
            )
        user_ref = None
        try:
            user_ref = compute_user_reference(
                deps.admission_config, auth_response.user.id
            )
        except Exception:
            # The identity itself is still valid; only the sanitized reference
            # could not be derived, so the event is emitted without it.
            user_ref = None
        emit_event(
            logger,
            EVENT_AUTH_COMPLETED,
            outcome="ok",
            duration_ms=_duration_ms(started_at),
            correlation=correlation,
            user_ref=user_ref,
        )
        return auth_response.user
    except HTTPException:
        raise
    except AuthApiError as exc:
        if exc.status in (400, 401, 403):
            emit_event(
                logger,
                EVENT_AUTH_FAILED,
                code="invalid_token",
                duration_ms=_duration_ms(started_at),
                correlation=correlation,
            )
            raise HTTPException(
                status_code=401,
                detail="Authentication failed",
                headers={"WWW-Authenticate": "Bearer"},
            )
        emit_event(
            logger,
            EVENT_AUTH_FAILED,
            code="upstream_error",
            duration_ms=_duration_ms(started_at),
            correlation=correlation,
        )
        raise HTTPException(status_code=503, detail="Authentication service unavailable")
    except AuthRetryableError:
        emit_event(
            logger,
            EVENT_AUTH_FAILED,
            code="transport_error",
            duration_ms=_duration_ms(started_at),
            correlation=correlation,
        )
        raise HTTPException(status_code=503, detail="Authentication service unavailable")
    except Exception:
        emit_event(
            logger,
            EVENT_AUTH_FAILED,
            code="unexpected",
            duration_ms=_duration_ms(started_at),
            correlation=correlation,
        )
        raise HTTPException(status_code=503, detail="Authentication service unavailable")


# ─── Request/response contracts ─────────────────────────────────────────────


class ChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: StrictStr
    message: StrictStr

    @field_validator("request_id")
    @classmethod
    def _validate_request_id(cls, value: str) -> str:
        try:
            return RequestIdentity(value).request_id
        except AdmissionError:
            raise PydanticCustomError("invalid_request_id", "invalid_request_id")

    @field_validator("message")
    @classmethod
    def _validate_message(cls, value: str) -> str:
        try:
            validate_new_message(value)
        except AdmissionError as exc:
            raise PydanticCustomError(exc.code, exc.code)
        return value


class ChatResponse(BaseModel):
    response: str
    emotion_state: EmotionStateResponse


_VALIDATION_MESSAGES = {
    "invalid_request_id": "Invalid request identifier.",
    "message_too_long": "Message exceeds the character limit.",
    "message_budget_exceeded": "Message exceeds the input budget.",
    "invalid_request": "Invalid request body.",
}


# ─── Error mapping (public contract preserved) ──────────────────────────────


def _turn_code_to_http(code: TurnErrorCode) -> int:
    """Map a ``TurnErrorCode`` to an HTTP status code.

    Centralised single-entry mapping so both ``TurnExecutionError`` and
    ``GroqPoolExhaustedError`` produce identical status codes for the
    same logical failure.
    """
    mapping = {
        TurnErrorCode.turn_timeout: 504,
        TurnErrorCode.upstream_rate_limited: 429,
        TurnErrorCode.provider_unavailable: 503,
        TurnErrorCode.provider_invalid_request: 503,
        TurnErrorCode.provider_invalid_response: 500,
        TurnErrorCode.persistence_unavailable: 503,
        TurnErrorCode.internal_error: 500,
    }
    return mapping.get(code, 500)


def _turn_code_to_message(code: TurnErrorCode) -> str:
    """Map a ``TurnErrorCode`` to a public, sanitised message."""
    mapping = {
        TurnErrorCode.turn_timeout: "Turn deadline exceeded.",
        TurnErrorCode.upstream_rate_limited: "Upstream rate limited.",
        TurnErrorCode.provider_unavailable: "Service temporarily unavailable.",
        TurnErrorCode.provider_invalid_request: "Service temporarily unavailable.",
        TurnErrorCode.provider_invalid_response: "Invalid response from provider.",
        TurnErrorCode.persistence_unavailable: "Persistence service unavailable.",
        TurnErrorCode.internal_error: "Internal server error.",
    }
    return mapping.get(code, "Internal server error.")


def _map_turn_error(exc: Exception) -> HTTPException:
    """Map domain exceptions to stable HTTP error responses.

    Never exposes: model name, provider details, exception text, prompt,
    infrastructure details, stack trace, or user content.
    Uses ``detail.code`` for structured error responses.
    Both ``TurnExecutionError`` and ``GroqPoolExhaustedError`` go through
    the same ``_turn_code_to_http`` / ``_turn_code_to_message`` helpers.
    """
    if isinstance(exc, DeadlineExceeded):
        code = TurnErrorCode.turn_timeout
        return HTTPException(
            status_code=_turn_code_to_http(code),
            detail={"code": code.value, "message": _turn_code_to_message(code)},
        )

    if isinstance(exc, TurnExecutionError):
        code = exc.code
        return HTTPException(
            status_code=_turn_code_to_http(code),
            detail={"code": code.value, "message": _turn_code_to_message(code)},
        )

    if isinstance(exc, GroqPoolExhaustedError):
        from .groq_manager import provider_failure_to_turn_code

        turn_code = (
            provider_failure_to_turn_code(exc.failure_code) if exc.failure_code
            else TurnErrorCode.provider_unavailable
        )
        return HTTPException(
            status_code=_turn_code_to_http(turn_code),
            detail={"code": turn_code.value, "message": _turn_code_to_message(turn_code)},
        )

    if isinstance(exc, GroqRequestError):
        return HTTPException(
            status_code=503,
            detail={
                "code": TurnErrorCode.provider_unavailable.value,
                "message": "Provider request failed.",
            },
        )

    if isinstance(exc, StatePersistenceError):
        return HTTPException(
            status_code=503,
            detail={
                "code": TurnErrorCode.persistence_unavailable.value,
                "message": "Persistence service unavailable.",
            },
        )

    if isinstance(exc, PersistenceError):
        return HTTPException(
            status_code=503,
            detail={
                "code": TurnErrorCode.persistence_unavailable.value,
                "message": "Persistence service unavailable.",
            },
        )

    return HTTPException(
        status_code=500,
        detail={"code": TurnErrorCode.internal_error.value, "message": "Internal server error."},
    )


# Stable conflict mapping for the ProcessTurn use case (#272). One single
# documented policy per code; messages are public constants, never raw
# PostgreSQL/Supabase text. request_id / message / revisions are never echoed.
_PROCESS_TURN_CONFLICT_HTTP = {
    "revision_mismatch": (
        409,
        {"code": "revision_conflict", "message": "Request conflicts with a concurrent update."},
    ),
    "request_payload_conflict": (
        409,
        {"code": "request_id_conflict", "message": "Request identifier conflicts with a different message."},
    ),
    "request_in_progress": (
        409,
        {"code": "request_in_progress", "message": "Request is already being processed."},
    ),
    "lease_conflict": (
        409,
        {"code": "lease_conflict", "message": "Request lease conflict."},
    ),
    "request_replay_unavailable": (
        409,
        {"code": "request_replay_unavailable", "message": "Request was already received but its response is unavailable."},
    ),
}


def _map_process_turn_conflict(exc: ConflictError) -> HTTPException:
    status_code, detail = _PROCESS_TURN_CONFLICT_HTTP.get(
        exc.code,
        (409, {"code": "request_conflict", "message": "Request conflict."}),
    )
    return HTTPException(status_code=status_code, detail=detail)


_ADMISSION_HTTP = {
    REQUEST_REPLAY_UNAVAILABLE: (409, "Request was already received but its response is unavailable."),
    REQUEST_ID_CONFLICT: (409, "Request identifier conflicts with a different message."),
    USER_RATE_LIMITED: (429, "User request rate limit exceeded."),
    NETWORK_RATE_LIMITED: (429, "Network request rate limit exceeded."),
    APPLICATION_RATE_LIMITED: (429, "Application request rate limit exceeded."),
    USER_DAILY_REQUEST_QUOTA_EXCEEDED: (429, "Daily request quota exceeded."),
    USER_DAILY_UNIT_QUOTA_EXCEEDED: (429, "Daily input quota exceeded."),
    INVALID_ADMISSION_INPUT: (422, "Invalid admission input."),
}


def _map_admission_rejection(result: AdmissionResult) -> HTTPException:
    status_code, message = _ADMISSION_HTTP[result.decision]
    headers = {"Retry-After": str(result.retry_after_seconds)} if status_code == 429 else None
    return HTTPException(
        status_code=status_code,
        detail={"code": result.decision, "message": message},
        headers=headers,
    )


def _admission_unavailable_error() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={"code": AdmissionUnavailable.code, "message": "Admission service unavailable."},
    )


# ─── Lifespan ───────────────────────────────────────────────────────────────


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start and stop application resources.

    Startup: build a fresh owned composition when none is present (injected
    dependencies keep their ownership with the caller), store the completed
    container, and only then mark startup as complete.
    Shutdown: close owned resources, keep going past individual failures, and
    drop the owned composition so a second lifespan cycle builds fresh
    resources instead of reusing already-closed ones. Injected dependencies
    are never closed and are left in ``app.state`` for the caller, but routes
    refuse to operate once the lifespan is no longer active.
    """
    if app.state.dependencies is None:
        try:
            dependencies, owned = build_default_dependencies(app.state.settings)
        except Exception:
            # Partial startup: drain anything already recorded as owned, then
            # fail. Resources created inside build_default_dependencies are
            # cleaned by the builder itself.
            await shutdown_dependencies(app.state.owned_resources)
            app.state.owned_resources = ()
            emit_event(logger, EVENT_APP_STARTUP_FAILED, level=logging.ERROR)
            raise
        app.state.dependencies = dependencies
        app.state.owned_resources = owned
        app.state.dependencies_owned = True
    app.state.lifespan_started = True
    try:
        yield
    finally:
        app.state.lifespan_started = False
        await shutdown_dependencies(app.state.owned_resources)
        app.state.owned_resources = ()
        if app.state.dependencies_owned:
            # The owned composition is finished; drop it so the next lifespan
            # cycle builds a new one with fresh resources. Injected
            # dependencies (dependencies_owned False) stay with the caller.
            app.state.dependencies = None
            app.state.dependencies_owned = False
        app.state.shutdown_completed = True


# ─── Application factory ────────────────────────────────────────────────────


def create_app(
    settings: Optional[Settings] = None,
    dependencies: Optional[ApplicationDependencies] = None,
) -> FastAPI:
    """Build and configure the FastAPI application.

    Args:
        settings: Validated settings. Defaults to ``Settings.from_env()``.
        dependencies: Fully composed container. When provided, the caller
            keeps ownership of every resource inside it; the application
            performs no startup construction and marks the lifespan as
            already complete. When omitted, the lifespan builds the default
            composition from settings (owned by the application).

    The factory never constructs Groq/Supabase/embedding resources: heavy
    construction happens only inside the lifespan.
    """
    if settings is None:
        try:
            settings = Settings.from_env()
        except SettingsConfigurationError:
            emit_event(logger, EVENT_APP_STARTUP_FAILED, reason="invalid_settings")
            raise

    # Single-worker containment (fail early for multi-worker configs).
    validate_worker_configuration()

    app = FastAPI(
        title="SoulMate API",
        description="Backend for the Emotional Companion Bot",
        lifespan=_lifespan,
    )

    app.state.settings = settings
    app.state.dependencies = dependencies
    # Only the lifespan marks a composition it builds as owned; injected
    # dependencies belong to the caller and are never closed.
    app.state.dependencies_owned = False
    app.state.lifespan_started = dependencies is not None
    app.state.owned_resources = ()
    app.state.shutdown_completed = False

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allowed_origins),
        allow_credentials=settings.cors_allow_credentials,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.exception_handler(RequestValidationError)
    async def _sanitise_request_validation_error(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        error_types = {item.get("type") for item in exc.errors()}
        code = "invalid_request"
        for candidate in (
            "invalid_request_id",
            "message_too_long",
            "message_budget_exceeded",
        ):
            if candidate in error_types:
                code = candidate
                break
        return JSONResponse(
            status_code=422,
            content={"detail": {"code": code, "message": _VALIDATION_MESSAGES[code]}},
        )

    # ─── Routes (module-level handlers registered here) ───────────────────
    app.post("/chat", response_model=ChatResponse)(chat_endpoint)
    app.get("/history")(get_history)
    app.get("/live")(live_endpoint)
    app.get("/ready")(ready_endpoint)
    app.get("/health")(health_endpoint)

    return app


# ─── Route handlers (module-level for injection and direct testing) ─────────


async def chat_endpoint(
    input_data: ChatInput,
    request: Request,
    current_user=Depends(get_current_user),
):
    deps = get_dependencies(request)
    engine = deps.conversation_engine
    turn_config = deps.turn_config
    admission_config = deps.admission_config
    started_at = time.monotonic()
    correlation = None
    try:
        budget = create_budget(turn_config)
        user_id = getattr(current_user, "id", None)
        identity = RequestIdentity(input_data.request_id)
        peer_host = request.client.host if request.client is not None else None
        network_identity = resolve_network_identity(
            peer_host,
            request.headers.get("x-forwarded-for"),
            admission_config.trusted_proxy_networks,
        )
        # Sanitized correlation reference for observability: HMAC-SHA256 of
        # the canonical request id under the dedicated turn-correlation domain
        # (never the raw request id, user id, message or any secret). Computed
        # before admission so every event of the turn carries it.
        correlation = compute_turn_correlation(admission_config, identity.request_id)
        admission_request = build_admission_request(
            user_id=user_id,
            request_identity=identity,
            message=input_data.message,
            network_identity=network_identity,
            config=admission_config,
        )
        admission_result = await run_blocking_write(
            "reserve_admission",
            budget,
            turn_config.supabase_timeout,
            reserve_admission_sync,
            deps.persistence_client,
            admission_request,
            allowlist_exceptions=(AdmissionUnavailable,),
        )
        if admission_result.decision == ADMITTED:
            mode = TurnMode.normal
            logger.info("event=admission_admitted correlation=%s", correlation)
        elif admission_result.decision == REQUEST_REPLAY_UNAVAILABLE:
            # The ledger detected a repeated (user, request_id): try the
            # persisted transactional result BEFORE any provider call. A
            # replay is a distinct observable event from a fresh admission.
            mode = TurnMode.replay_attempt
            logger.info("event=admission_replay correlation=%s", correlation)
        else:
            logger.info("event=admission_rejected code=%s", admission_result.decision)
            http_exc = _map_admission_rejection(admission_result)
            emit_event(
                logger,
                EVENT_HTTP_RESULT,
                code=http_exc.status_code,
                correlation=correlation,
            )
            raise http_exc

        result = await engine.process_turn(
            user_id,
            input_data.message,
            identity.request_id,
            budget=budget,
            mode=mode,
            correlation=correlation,
        )
        duration_ms = (time.monotonic() - started_at) * 1000
        emit_event(
            logger,
            EVENT_TURN_COMPLETED,
            code="ok",
            duration_ms=duration_ms,
            mode=mode.value,
            correlation=correlation,
        )
        emit_event(
            logger,
            EVENT_HTTP_RESULT,
            code=200,
            correlation=correlation,
        )
        # The public DTO exposes exactly response + emotion_state; revisions,
        # outbox refs, internal IDs and CommittedTurn are never exposed.
        return ChatResponse(response=result.response, emotion_state=result.emotion_state)
    except asyncio.CancelledError:
        # CancelledError must NOT be converted to HTTP 500 — propagate
        raise
    except HTTPException:
        raise
    except AdmissionUnavailable:
        emit_event(logger, EVENT_TURN_FAILED, code="admission_unavailable")
        emit_event(
            logger,
            EVENT_HTTP_RESULT,
            code=503,
            correlation=correlation,
        )
        raise _admission_unavailable_error()
    except ConflictError as exc:
        emit_event(logger, EVENT_REQUEST_CONFLICT, code=exc.code, correlation=correlation)
        http_exc = _map_process_turn_conflict(exc)
        emit_event(
            logger,
            EVENT_HTTP_RESULT,
            code=http_exc.status_code,
            correlation=correlation,
        )
        raise http_exc
    except (DeadlineExceeded, TurnExecutionError, GroqPoolExhaustedError,
            GroqRequestError, StatePersistenceError, PersistenceError) as exc:
        http_exc = _map_turn_error(exc)
        detail = http_exc.detail
        code = detail.get("code") if isinstance(detail, dict) else "internal_error"
        emit_event(
            logger,
            EVENT_TURN_FAILED,
            level=logging.ERROR,
            code=code,
            correlation=correlation,
        )
        emit_event(
            logger,
            EVENT_HTTP_RESULT,
            code=http_exc.status_code,
            correlation=correlation,
        )
        raise http_exc
    except Exception:
        # Sanitize logging: avoid logging raw exceptions that might contain
        # secrets or tracebacks.
        emit_event(
            logger,
            EVENT_TURN_FAILED,
            level=logging.ERROR,
            code=TurnErrorCode.internal_error.value,
            correlation=correlation,
        )
        emit_event(
            logger,
            EVENT_HTTP_RESULT,
            code=500,
            correlation=correlation,
        )
        raise HTTPException(
            status_code=500,
            detail={"code": TurnErrorCode.internal_error.value, "message": "Internal server error."},
        )


def get_history(request: Request, current_user=Depends(get_current_user)):
    deps = get_dependencies(request)
    user_id = current_user.id
    try:
        supabase = deps.persistence_client
        if not supabase:
            return []

        response = supabase.table("chat_logs")\
            .select("*")\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .limit(50)\
            .execute()

        return response.data[::-1] if response.data else []
    except Exception:
        raise HTTPException(status_code=500, detail="Internal Server Error")


async def live_endpoint():
    """Liveness: proves the process and event loop can respond.

    Never touches providers, database, embeddings, or readiness state.
    """
    return JSONResponse(status_code=200, content={"status": "live"})


async def ready_endpoint(request: Request):
    """Readiness: checks critical dependencies with explicit timeouts.

    Returns 200 only when every critical component for the currently
    enabled mode is available and the lifespan completed startup;
    503 otherwise. The response never includes URLs, keys, project
    names, exception text, counts, or user identifiers.
    """
    deps = getattr(request.app.state, "dependencies", None)
    if deps is None:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "components": {}},
        )
    results = await deps.health_checks.run_all()
    lifespan_started = bool(getattr(request.app.state, "lifespan_started", False))
    status_code, body = build_ready_response(results, lifespan_started)
    return JSONResponse(status_code=status_code, content=body)


def health_endpoint():
    """Legacy compatibility endpoint.

    Documented as a process-alive alias: it never asserts readiness
    (no database/provider checks run here). New consumers must use
    ``/live`` and ``/ready``.
    """
    return {"status": "alive"}


# Module-level application for Uvicorn targets (``backend.main:app``).
# Only configuration and routing are built here; resources are created by
# the lifespan when the server actually starts.
app = create_app()


if __name__ == "__main__":
    # Development entrypoint — NOT for production use.
    # Use ``python -m backend.serve`` for production.
    import uvicorn

    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
