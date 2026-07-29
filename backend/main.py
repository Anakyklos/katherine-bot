import asyncio
from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

import logging
from supabase_auth.errors import AuthApiError, AuthRetryableError
logger = logging.getLogger(__name__)


from pydantic import BaseModel, ConfigDict, Field
from typing import List, Optional
import uvicorn
import os
from dotenv import load_dotenv
from .engine import ConversationEngine
from .memory import MAX_MESSAGE_LENGTH, StatePersistenceError
from .emotion_presentation import EmotionStateResponse
from .turn_execution import (
    TurnExecutionConfig,
    TurnExecutionError,
    TurnErrorCode,
    DeadlineExceeded,
)
from .groq_manager import GroqPoolExhaustedError, GroqRequestError

load_dotenv()

from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="SoulMate API", description="Backend for the Emotional Companion Bot")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
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

# Parse turn execution config from environment
_turn_config = TurnExecutionConfig.from_env()

# Initialize Engine with containment-aware configuration
engine = ConversationEngine(
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
    message: str = Field(max_length=MAX_MESSAGE_LENGTH)

class ChatResponse(BaseModel):
    response: str
    emotion_state: EmotionStateResponse


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


@app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(
    input_data: ChatInput,
    background_tasks: BackgroundTasks,
    current_user = Depends(get_current_user)
):
    try:
        user_id = current_user.id
        response_text, current_emotion = await engine.process_turn(
            user_id, input_data.message, background_tasks
        )
        return ChatResponse(response=response_text, emotion_state=current_emotion)
    except asyncio.CancelledError:
        # CancelledError must NOT be converted to HTTP 500 — propagate
        raise
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
