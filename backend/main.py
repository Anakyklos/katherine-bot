import asyncio
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

import logging
from supabase_auth.errors import AuthApiError, AuthRetryableError
logger = logging.getLogger(__name__)


from pydantic import BaseModel, ConfigDict, StrictStr, field_validator
from pydantic_core import PydanticCustomError
from typing import Optional
import uvicorn
import os
from dotenv import load_dotenv
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
    AdmissionRuntimeConfig,
    AdmissionUnavailable,
    build_admission_request,
    reserve_admission_sync,
    resolve_network_identity,
)
from .admission_contracts import AdmissionError, RequestIdentity, validate_new_message
from .chat_engine import ChatConversationEngine
from .memory import StatePersistenceError
from .emotion_presentation import EmotionStateResponse
from .turn_execution import (
    TurnExecutionConfig,
    TurnExecutionError,
    TurnErrorCode,
    DeadlineExceeded,
    create_budget,
    run_blocking_write,
)
from .groq_manager import GroqPoolExhaustedError, GroqRequestError

load_dotenv()

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="SoulMate API", description="Backend for the Emotional Companion Bot")

# Comma-separated origins allowed by CORS. Default preserves the historical
# development origin; production sets its own public frontend origin(s).
cors_allowed_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Validate runtime containment before initialising the engine.
# This runs at module load time, so multi-worker configurations fail early.
from .runtime_containment import (
    validate_worker_configuration,
    parse_archival_extraction_flag,
)

validate_worker_configuration()

# Parse archival extraction flag from environment (default: disabled)
_archival_extraction_enabled = parse_archival_extraction_flag(
    os.environ.get("ARCHIVAL_EXTRACTION_ENABLED")
)

# Parse turn execution and admission config from environment. Admission has no
# fallback secret and fails closed during application initialisation.
_turn_config = TurnExecutionConfig.from_env()
_admission_config = AdmissionRuntimeConfig.from_env()

# Initialize Engine with containment-aware configuration
engine = ChatConversationEngine(
    archival_extraction_enabled=_archival_extraction_enabled,
    turn_config=_turn_config,
)


security = HTTPBearer(auto_error=False)

def get_current_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    token = credentials.credentials
    try:
        if not engine.memory_manager.supabase:
            raise HTTPException(status_code=503, detail="Authentication service unavailable")

        auth_response = engine.memory_manager.supabase.auth.get_user(token)
        if not auth_response.user:
            raise HTTPException(
                status_code=401,
                detail="Authentication failed",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return auth_response.user
    except HTTPException:
        raise
    except AuthApiError as e:
        # e.status is present in AuthApiError
        if e.status in (400, 401, 403):
            raise HTTPException(status_code=401, detail="Authentication failed", headers={"WWW-Authenticate": "Bearer"})
        logger.error("Authentication service failure: Upstream AuthApiError")
        raise HTTPException(status_code=503, detail="Authentication service unavailable")
    except AuthRetryableError:
        logger.error("Authentication service failure: Transport/Fetch error")
        raise HTTPException(status_code=503, detail="Authentication service unavailable")
    except Exception:
        logger.error("Authentication service failure: Unexpected error")
        raise HTTPException(status_code=503, detail="Authentication service unavailable")

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


# ─── Error mapping ───────────────────────────────────────────────────────────

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
        # Map the failure code if available
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
            detail={"code": TurnErrorCode.provider_unavailable.value, "message": "Provider request failed."},
        )

    if isinstance(exc, StatePersistenceError):
        return HTTPException(
            status_code=503,
            detail={"code": TurnErrorCode.persistence_unavailable.value, "message": "Persistence service unavailable."},
        )

    # Unknown/unexpected — sanitize to generic 500
    return HTTPException(
        status_code=500,
        detail={"code": TurnErrorCode.internal_error.value, "message": "Internal server error."},
    )


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


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(
    input_data: ChatInput,
    request: Request,
    background_tasks: BackgroundTasks,
    current_user = Depends(get_current_user)
):
    try:
        budget = create_budget(_turn_config)
        user_id = getattr(current_user, "id", None)
        identity = RequestIdentity(input_data.request_id)
        peer_host = request.client.host if request.client is not None else None
        network_identity = resolve_network_identity(
            peer_host,
            request.headers.get("x-forwarded-for"),
            _admission_config.trusted_proxy_networks,
        )
        admission_request = build_admission_request(
            user_id=user_id,
            request_identity=identity,
            message=input_data.message,
            network_identity=network_identity,
            config=_admission_config,
        )
        admission_result = await run_blocking_write(
            "reserve_admission",
            budget,
            _turn_config.supabase_timeout,
            reserve_admission_sync,
            engine.memory_manager.supabase,
            admission_request,
            allowlist_exceptions=(AdmissionUnavailable,),
        )
        if admission_result.decision != ADMITTED:
            logger.info("event=admission_rejected code=%s", admission_result.decision)
            raise _map_admission_rejection(admission_result)

        logger.info("event=admission_admitted")
        response_text, current_emotion = await engine.process_turn(
            user_id, input_data.message, background_tasks, budget=budget
        )
        return ChatResponse(response=response_text, emotion_state=current_emotion)
    except asyncio.CancelledError:
        # CancelledError must NOT be converted to HTTP 500 — propagate
        raise
    except HTTPException:
        raise
    except AdmissionUnavailable:
        logger.error("event=admission_unavailable")
        raise _admission_unavailable_error()
    except (DeadlineExceeded, TurnExecutionError, GroqPoolExhaustedError,
            GroqRequestError, StatePersistenceError) as exc:
        raise _map_turn_error(exc)
    except Exception:
        # Sanitize logging: avoid logging raw exceptions that might contain secrets or tracebacks
        logger.error("Event: Chat Turn Failure")
        raise HTTPException(
            status_code=500,
            detail={"code": TurnErrorCode.internal_error.value, "message": "Internal server error."},
        )

@app.get("/health")
def health_check():
    return {"status": "alive", "engine_status": "ready"}

@app.get("/history")
def get_history(current_user = Depends(get_current_user)):
    user_id = current_user.id
    try:
        if not engine.memory_manager.supabase:
            return []
            
        response = engine.memory_manager.supabase.table("chat_logs")\
            .select("*")\
            .eq("user_id", user_id)\
            .order("created_at", desc=True)\
            .limit(50)\
            .execute()
            
        return response.data[::-1] if response.data else []
    except Exception:
        raise HTTPException(status_code=500, detail="Internal Server Error")

if __name__ == "__main__":
    # Development entrypoint — NOT for production use.
    # Use ``python -m backend.serve`` for production.
    uvicorn.run("backend.main:app", host="0.0.0.0", port=8000, reload=True)
