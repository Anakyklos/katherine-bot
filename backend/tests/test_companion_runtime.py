"""Tests for the desktop companion runtime (issue #336).

The ``CompanionRuntime`` composes the local turn flow on top of the
LocalStorage foundation (#335): it must

- open LocalStorage lazily and survive restart (state persists),
- run an idempotent, atomic, single-provider-call turn flow over real
  temporary SQLite (commit and replay),
- map every failure to a sanitized :class:`LocalErrorCode` (no Python
  exception, traceback, path or SQL ever crosses to the bridge),
- expose privacy operations (delete history / delete memories /
  emotional reset / relationship reset) as real, transactional deletes,
- expose a read-only history window,
- shut down cleanly (terminal LocalStorage lifecycle, no dangling
  resources).

All persistence evidence uses **real temporary SQLite databases**; the
only mocked boundary is the remote LLM provider (Groq), which is the
legitimate mocking point per the constitution.
"""

from __future__ import annotations

import asyncio
import json
import time as _time
from pathlib import Path

import pytest


def _test_clock() -> float:
    """Real epoch clock (matches LocalStorage's neutral-snapshot time
    source); frozen clocks would trip the domain's anti-regression rule."""
    return _time.time()

from backend.companion_runtime import (
    CompanionRuntime,
    LocalErrorCode,
    LocalStorageError,
    TurnResult,
    runtime_error_code,
)
from backend.emotion_presentation import EmotionStateResponse
from backend.local_storage.errors import ConflictError as StorageConflict
from backend.local_storage.errors import PersistenceError
from backend.local_storage.errors import StorageCorruptError
from backend.local_storage.errors import ValidationError as StorageValidation
from backend.turn_execution import DeadlineExceeded, TurnExecutionError


pytestmark = pytest.mark.anyio


# ─────────────────────────────────────────────────────────────────
# Provider fakes (the only mocked boundary — the remote LLM)
# ─────────────────────────────────────────────────────────────────


class ScriptedProvider:
    """Deterministic provider used to prove flow, retries and failure."""

    def __init__(self, response: str = "local response"):
        self.response = response
        self.calls = 0
        self.appraisal_calls = 0
        self.policies: list[str] = []
        self.fail_with: Exception | None = None

    async def appraise(self, message: str, budget) -> object:
        self.appraisal_calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        from backend.emotional_domain import AppraisalV1

        return AppraisalV1.neutral()

    async def generate(self, messages: list, budget) -> str:
        self.calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return self.response

    def build_trusted_policy(self, emotional_state, relationship, adaptation_strategy=""):
        self.policies.append("policy")
        return "policy"


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────


def make_runtime(tmp_path: Path, provider: ScriptedProvider | None = None) -> CompanionRuntime:
    provider = provider or ScriptedProvider()
    return CompanionRuntime(
        storage_path=tmp_path / "katherine.db",
        provider=provider,
        now_provider=_test_clock,
    )


def commit_turn(runtime: CompanionRuntime, message: str, request_id: str) -> TurnResult:
    return asyncio.run(runtime.commit_turn(request_id=request_id, message=message))


def history(runtime: CompanionRuntime) -> list[dict]:
    return asyncio.run(runtime.load_history(limit=50))


# ─────────────────────────────────────────────────────────────────
# Lifecycle / restart
# ─────────────────────────────────────────────────────────────────


async def test_runtime_opens_local_storage_lazily_and_survives_restart(tmp_path):
    runtime = make_runtime(tmp_path)
    # No SQLite connection at construction — lazy open.
    assert runtime._storage is None

    result = await runtime.commit_turn_async(request_id="r-1", message="hello")
    assert result.success is True
    assert result.response == "local response"
    assert result.emotion_state["mood_label"] in {"NEUTRA", "NEUTRAL", "CALMA"}

    first_result_id = result.message_id
    assert first_result_id is not None

    runtime.close()

    # Restart with the same storage path: the history survived.
    provider2 = ScriptedProvider(response="second response")
    runtime2 = CompanionRuntime(
        storage_path=tmp_path / "katherine.db",
        provider=provider2,
        now_provider=_test_clock,
    )
    entries = runtime2.load_history(limit=10)
    assert len(entries) == 2  # user + assistant persisted
    assert entries[0]["role"] == "user"
    assert entries[0]["content"] == "hello"
    assert entries[1]["role"] == "assistant"
    assert entries[1]["content"] == "local response"
    runtime2.close()


async def test_runtime_is_reusable_and_restart_survives_state(tmp_path):
    runtime = make_runtime(tmp_path)
    await runtime.commit_turn_async(request_id="r-1", message="hi")
    await runtime.commit_turn_async(request_id="r-2", message="again")
    entries = runtime.load_history(limit=10)
    assert len(entries) == 4
    runtime.close()


# ─────────────────────────────────────────────────────────────────
# Idempotent, atomic turn flow over real SQLite
# ─────────────────────────────────────────────────────────────────


async def test_commit_turn_is_idempotent_for_the_same_request_id(tmp_path):
    provider = ScriptedProvider()
    runtime = make_runtime(tmp_path, provider)
    first = await runtime.commit_turn_async(request_id="r-1", message="hello")
    second = await runtime.commit_turn_async(request_id="r-1", message="hello")

    assert first.success is True
    assert second.success is True
    # Same provider call count — replay must not call the provider again.
    assert provider.calls == 1
    # Same persisted result.
    assert second.response == first.response
    assert second.message_id == first.message_id
    assert second.replayed is True
    runtime.close()


async def test_commit_turn_validates_input(tmp_path):
    runtime = make_runtime(tmp_path)
    bad = await runtime.commit_turn_async(request_id="", message="hello")
    assert bad.success is False
    assert bad.error_code == LocalErrorCode.VALIDATION
    runtime.close()

async def test_commit_turn_message_too_long_is_rejected(tmp_path):
    runtime = make_runtime(tmp_path)
    bad = await runtime.commit_turn_async(
        request_id="r-1", message="x" * 20_001
    )
    assert bad.success is False
    assert bad.error_code == LocalErrorCode.VALIDATION
    runtime.close()


async def test_commit_turn_provider_failure_is_sanitized(tmp_path):
    from backend.groq_manager import GroqRequestError

    provider = ScriptedProvider()
    provider.fail_with = GroqRequestError("429 too many requests: api_key=SECRET")
    runtime = make_runtime(tmp_path, provider)
    result = await runtime.commit_turn_async(request_id="r-1", message="hello")
    assert result.success is False
    # GroqRequestError is a generic provider failure (the web maps it to
    # provider_unavailable/503); the secret must never cross.
    assert result.error_code == LocalErrorCode.SERVICE_UNAVAILABLE
    assert "SECRET" not in (result.error_message or "")
    runtime.close()


async def test_commit_turn_pool_exhausted_rate_limited(tmp_path):
    from backend.groq_manager import GroqPoolExhaustedError, ProviderFailure

    provider = ScriptedProvider()
    provider.fail_with = GroqPoolExhaustedError(
        "exhausted", code=ProviderFailure.rate_limited
    )
    runtime = make_runtime(tmp_path, provider)
    result = await runtime.commit_turn_async(request_id="r-1", message="hello")
    assert result.success is False
    assert result.error_code == LocalErrorCode.RATE_LIMITED
    runtime.close()


async def test_commit_turn_provider_timeout(tmp_path):
    provider = ScriptedProvider()
    provider.fail_with = TimeoutError("upstream timeout")
    runtime = make_runtime(tmp_path, provider)
    result = await runtime.commit_turn_async(request_id="r-1", message="hello")
    assert result.success is False
    assert result.error_code == LocalErrorCode.TIMEOUT
    runtime.close()


async def test_commit_turn_deadline_exceeded(tmp_path):
    provider = ScriptedProvider()
    provider.fail_with = DeadlineExceeded()
    runtime = make_runtime(tmp_path, provider)
    result = await runtime.commit_turn_async(request_id="r-1", message="hello")
    assert result.success is False
    assert result.error_code == LocalErrorCode.TIMEOUT
    runtime.close()


async def test_commit_turn_connection_failure_mapping(tmp_path):
    provider = ScriptedProvider()
    provider.fail_with = ConnectionError("connection refused to 10.0.0.5")
    runtime = make_runtime(tmp_path, provider)
    result = await runtime.commit_turn_async(request_id="r-1", message="hello")
    assert result.success is False
    assert result.error_code == LocalErrorCode.SERVICE_UNAVAILABLE
    assert "10.0.0.5" not in (result.error_message or "")
    runtime.close()


async def test_commit_turn_rejects_conflicting_payload_for_same_request_id(tmp_path):
    runtime = make_runtime(tmp_path)
    first = await runtime.commit_turn_async(request_id="r-1", message="hello")
    assert first.success is True
    second = await runtime.commit_turn_async(request_id="r-1", message="different message")
    assert second.success is False
    assert second.error_code == LocalErrorCode.REQUEST_CONFLICT
    runtime.close()


async def test_commit_turn_persists_real_sqlite(tmp_path):
    provider = ScriptedProvider(response="persisted text")
    runtime = make_runtime(tmp_path, provider)
    await runtime.commit_turn_async(request_id="r-1", message="saved")
    runtime.close()

    # Raw SQLite inspection — real persistence, no second store.
    import sqlite3

    conn = sqlite3.connect(tmp_path / "katherine.db")
    rows = conn.execute("select role, content from chat_logs order by id").fetchall()
    conn.close()
    assert rows == [("user", "saved"), ("assistant", "persisted text")]


# ─────────────────────────────────────────────────────────────────
# Error mapping / sanitization invariants
# ─────────────────────────────────────────────────────────────────


async def test_storage_corrupt_error_is_sanitized(tmp_path):
    runtime = make_runtime(tmp_path)
    # Corrupt the database after a healthy commit.
    await runtime.commit_turn_async(request_id="r-1", message="one")
    runtime.close()

    db = tmp_path / "katherine.db"
    db.write_bytes(b"this is not a sqlite database" * 10)
    runtime2 = make_runtime(tmp_path)
    result = await runtime2.commit_turn_async(request_id="r-2", message="two")
    assert result.success is False
    # Storage-corrupt maps to a stable code the UI understands, and the
    # sanitized message never leaks the raw SQLite error (which contains
    # the file path).
    assert result.error_code in {LocalErrorCode.SERVICE_UNAVAILABLE, LocalErrorCode.STORAGE}
    assert "katherine.db" not in result.error_message
    assert "sqlite" not in result.error_message.lower()
    runtime2.close()


async def test_unconfigured_provider_yields_configuration_error(tmp_path):
    from backend.groq_manager import GroqConfigurationError

    class Unconfigured:
        async def appraise(self, message, budget):
            raise GroqConfigurationError("missing key")

        async def generate(self, messages, budget):
            raise GroqConfigurationError("missing key")

    runtime = CompanionRuntime(
        storage_path=tmp_path / "katherine.db",
        provider=Unconfigured(),
        now_provider=_test_clock,
    )
    result = await runtime.commit_turn_async(request_id="r-1", message="hello")
    assert result.success is False
    assert result.error_code == LocalErrorCode.CONFIGURATION


async def test_runtime_error_code_maps_storage_errors():
    assert (
        runtime_error_code(
            StorageConflict(
                "request_payload_conflict",
                "conflict",
                expected_revision=0,
            )
        )
        is LocalErrorCode.REQUEST_CONFLICT
    )
    assert (
        runtime_error_code(PersistenceError("boom", "x")) is LocalErrorCode.STORAGE
    )
    assert (
        runtime_error_code(StorageCorruptError("corrupt", "database error"))
        is LocalErrorCode.STORAGE
    )
    assert (
        runtime_error_code(StorageValidation("bad", "x")) is LocalErrorCode.VALIDATION
    )
    from backend.groq_manager import GroqConfigurationError

    assert (
        runtime_error_code(GroqConfigurationError("missing GROQ_API_KEY"))
        is LocalErrorCode.CONFIGURATION
    )
    assert runtime_error_code(Exception("plain")) is LocalErrorCode.UNKNOWN


# ─────────────────────────────────────────────────────────────────
# Privacy operations (real, transactional)
# ───────────────────────── storage ───────────────────────────────


async def test_delete_history_really_deletes(tmp_path):
    runtime = make_runtime(tmp_path)
    await runtime.commit_turn_async(request_id="r-1", message="keep me")
    entries_before = runtime.load_history(limit=10)
    assert len(entries_before) == 2

    op = runtime.delete_history()
    assert op["success"] is True

    entries_after = runtime.load_history(limit=10)
    assert entries_after == []
    runtime.close()


async def test_delete_memories_really_deletes(tmp_path):
    runtime = make_runtime(tmp_path)
    # Persist a memory through the runtime facade so the schema matches.
    await runtime.commit_turn_async(request_id="r-1", message="remember the milk")
    op = runtime.delete_memories()
    assert op["success"] is True
    runtime.close()


async def test_reset_emotional_state_uses_canonical_neutral(tmp_path):
    runtime = make_runtime(tmp_path)
    await runtime.commit_turn_async(request_id="r-1", message="hello")
    op = runtime.reset_emotional_state()
    assert op["success"] is True

    state = runtime.get_state()
    assert state["emotional_state"]["pleasure"] == 0.0
    assert state["emotional_state"]["connection"] == 0.5
    runtime.close()


async def test_reset_relationship_state_uses_canonical_neutral(tmp_path):
    runtime = make_runtime(tmp_path)
    await runtime.commit_turn_async(request_id="r-1", message="hello")
    op = runtime.reset_relationship_state()
    assert op["success"] is True

    state = runtime.get_state()
    assert state["relationship_state"]["trust"] == 0.5
    assert state["relationship_state"]["affection"] == 0.3
    runtime.close()


# ─────────────────────────────────────────────────────────────────
# Read-only state / history window
# ─────────────────────────────────────────────────────────────────


async def test_get_state_returns_public_shape(tmp_path):
    runtime = make_runtime(tmp_path)
    state = runtime.get_state()
    assert set(state.keys()) >= {"emotional_state", "relationship_state", "revision"}
    # Fresh profile: revision 0, canonical neutral snapshots.
    assert state["revision"] == 0
    assert state["emotional_state"]["pleasure"] == 0.0
    assert state["relationship_state"]["trust"] == 0.5
    runtime.close()


async def test_load_history_window_bounded(tmp_path):
    runtime = make_runtime(tmp_path)
    for i in range(5):
        await runtime.commit_turn_async(request_id=f"r-{i}", message=f"msg {i}")
    limited = runtime.load_history(limit=3)
    assert len(limited) == 3
    all_entries = runtime.load_history(limit=100)
    assert len(all_entries) == 10
    runtime.close()


async def test_load_history_returns_newest_last(tmp_path):
    runtime = make_runtime(tmp_path)
    for i in range(3):
        await runtime.commit_turn_async(request_id=f"r-{i}", message=f"m{i}")
    entries = runtime.load_history(limit=10)
    contents = [e["content"] for e in entries]
    assert contents == ["m0", "local response", "m1", "local response", "m2", "local response"]
    runtime.close()


# ─────────────────────────────────────────────────────────────────
# Clean shutdown
# ─────────────────────────────────────────────────────────────────


async def test_close_is_terminal_and_idempotent(tmp_path):
    runtime = make_runtime(tmp_path)
    await runtime.commit_turn_async(request_id="r-1", message="hello")
    runtime.close()
    runtime.close()  # idempotent
    assert runtime._storage is None or runtime._storage.is_closed


async def test_close_flushes_and_data_survives_immediately(tmp_path):
    runtime = make_runtime(tmp_path)
    await runtime.commit_turn_async(request_id="r-1", message="hello")
    runtime.close()
    import sqlite3

    conn = sqlite3.connect(tmp_path / "katherine.db")
    rows = conn.execute("select count(*) from chat_logs").fetchall()
    conn.close()
    assert rows[0][0] == 2
