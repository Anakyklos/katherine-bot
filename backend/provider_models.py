"""
Pure configuration module for Groq provider model IDs and output token limits.

This module is the single source of truth for which production-supported
Groq models Katherine uses.  It must be importable without:

- FastAPI
- Groq SDK
- Supabase
- sentence_transformers
- environment variables
- network access
- ConversationEngine

Usage::

    from backend.provider_models import MAIN_MODEL_ID, FAST_MODEL_ID, ProviderConfig

    config = ProviderConfig()
    assert config.main_model_id == "openai/gpt-oss-120b"
"""

from __future__ import annotations

from dataclasses import dataclass


# ─── Model identifiers ───────────────────────────────────────────────────────
# These replace the legacy models ``llama-3.3-70b-versatile`` (generation)
# and ``llama-3.1-8b-instant`` (appraisal / archival extraction) which are
# scheduled for shutdown on 2026-08-16 on Free and Developer plans.

MAIN_MODEL_ID = "openai/gpt-oss-120b"
FAST_MODEL_ID = "openai/gpt-oss-20b"

# ─── Output token limits ─────────────────────────────────────────────────────
# Each value is chosen to balance response quality against latency and cost.

MAIN_MAX_OUTPUT_TOKENS = 200          # Generation (main model)
APPRAISAL_MAX_OUTPUT_TOKENS = 256     # Emotional appraisal (fast model)
ARCHIVAL_MAX_OUTPUT_TOKENS = 512      # Archival extraction (fast model)


@dataclass(frozen=True)
class ProviderConfig:
    """Immutable, user-independent provider configuration.

    All attributes are frozen (read-only at runtime).  No mutable state,
    no environment fallback, no per-user overrides.
    """

    main_model_id: str = MAIN_MODEL_ID
    fast_model_id: str = FAST_MODEL_ID

    main_max_output_tokens: int = MAIN_MAX_OUTPUT_TOKENS
    appraisal_max_output_tokens: int = APPRAISAL_MAX_OUTPUT_TOKENS
    archival_max_output_tokens: int = ARCHIVAL_MAX_OUTPUT_TOKENS
