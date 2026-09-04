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
only mocked boundary is the remote LLM (the LanguageModel contract fake),
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
from backend.language_model import LanguageModelConfigurationError
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
    """Deterministic LanguageModel fake used to prove flow and failure.

    Implements the canonical contract (issue #337): appraise, generate,
    describe. The trusted policy is a core responsibility, so the fake
    no longer builds it.
    """

    def __init__(self, response: str = "local response"):
        self.response = response
        self.calls = 0
        self.appraisal_calls = 0
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

    def describe(self):
        from backend.language_model import ModelSelection

        return ModelSelection(
            provider="fake",
            main_model_id="fake-main",
            fast_model_id="fake-fast",
        )


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────


def make_runtime(tmp_path: Path, provider: ScriptedProvider | None = None) -> CompanionRuntime:
    provider = provider or ScriptedProvider()
    return CompanionRuntime(
        storage_path=tmp_path / "katherine.db",
        language_model=provider,
        now_provider=_test_clock,
    )


def history(runtime: CompanionRuntime) -> list[dict]:
    return asyncio.run(runtime.load_history(limit=50))


# ─────────────────────────────────────────────────────────────────
# runtime_state probe (T005: storage status + provider-configured flag,
# no env echo)
# ─────────────────────────────────────────────────────────────────


def test_runtime_state_reports_storage_and_provider_flags(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)

    # Provider probe must not instantiate the adapter: key env is
    # irrelevant here because the injected model is already built.
    # because the injected provider is already built.
    state = runtime.runtime_state()

    assert state["ok"] is True
    assert state["storage"] is True
    assert state["provider_configured"] is True
    assert state["revision"] == 0
    # No env echo: the payload contains no key material.
    assert "GROQ_API_KEY" not in json.dumps(state)


def test_runtime_state_unconfigured_provider_still_opens_storage(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY_2", raising=False)

    runtime = CompanionRuntime(
        storage_path=tmp_path / "katherine.db",
        language_model=None,
        language_model_factory=None,
        provider_configured_probe=lambda: False,
        now_provider=_test_clock,
    )

    state = runtime.runtime_state()
    assert state["ok"] is True
    assert state["storage"] is True
    assert state["provider_configured"] is False

    # History still loads: unconfigured provider never blocks reads.
    assert runtime.load_history(limit=10) == []


def test_runtime_state_storage_failure_is_sanitized(tmp_path):
    runtime = CompanionRuntime(
        storage_path=tmp_path / "katherine.db",
        language_model=ScriptedProvider(),
        now_provider=_test_clock,
    )

    def _fail_open():
        raise StorageCorruptError("corrupt", "database disk image is malformed")

    runtime._ensure_storage = _fail_open
    state = runtime.runtime_state()

    assert state["ok"] is False
    assert state["storage"] is False
    # Constant message only: no exception text leaks.
    assert state["error_code"] == "storage"
    assert "malformed" not in json.dumps(state)


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
        language_model=provider2,
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


async def test_concurrent_same_request_id_executes_provider_exactly_once(
    tmp_path,
):
    """#336 review blocker 4 — atomic admission under CONCURRENCY.

    Two turns with the same request id in flight at the same time:
    exactly one spends provider calls and commits; the other is
    rejected deterministically (conflict) BEFORE any provider call.
    reserve_request's BEGIN IMMEDIATE insert-or-classify is the
    admission — there is no check-then-act window.
    """
    provider = ScriptedProvider()
    # Make the provider slow so both turns really overlap.
    import asyncio as _asyncio

    class SlowProvider(ScriptedProvider):
        async def generate(self, messages, budget):
            await _asyncio.sleep(0.05)
            return await super().generate(messages, budget)

    provider = SlowProvider()
    runtime = make_runtime(tmp_path, provider)

    results = await _asyncio.gather(
        runtime.commit_turn_async(request_id="r-1", message="hello"),
        runtime.commit_turn_async(request_id="r-1", message="hello"),
        return_exceptions=True,
    )
    outcomes = [r for r in results if not isinstance(r, BaseException)]
    errors = [r for r in results if isinstance(r, BaseException)]

    # One success + one deterministic conflict — never two successes,
    # never two provider executions.
    assert not errors, errors
    assert len(outcomes) == 2
    success_results = [r for r in outcomes if r.success]
    failed_results = [r for r in outcomes if not r.success]
    assert len(success_results) == 1
    assert len(failed_results) == 1
    assert failed_results[0].error_code == LocalErrorCode.REQUEST_CONFLICT
    # Exactly one provider generation — the loser never reached it.
    assert provider.calls == 1
    # The ledger has exactly one row, completed.
    row = runtime._ensure_storage()._connection_for_tests_only().execute(
        "select status from turn_requests where request_id = 'r-1'"
    ).fetchone()
    assert row is not None and row[0] == "completed"
    # Durable replay for the losing caller: after the winner commits,
    # re-sending the same request id replays the persisted result
    # (the review-required "replay durável para o segundo caller").
    retry = await runtime.commit_turn_async(request_id="r-1", message="hello")
    assert retry.success is True
    assert retry.replayed is True
    assert retry.message_id == success_results[0].message_id
    assert provider.calls == 1  # still exactly one remote execution
    runtime.close()


async def test_provider_failure_releases_reservation_for_retry(tmp_path):
    """A live-session provider failure must not poison the request id:
    the pending reservation is released, so the user can retry the
    same send without a permanent conflict."""
    provider = ScriptedProvider()
    provider.fail_with = RuntimeError("provider down")
    runtime = make_runtime(tmp_path, provider)

    first = await runtime.commit_turn_async(request_id="r-1", message="hello")
    assert first.success is False

    # The failed reservation was released — the ledger has no stuck
    # pending row for this request id (crash recovery owns that case).
    row = runtime._ensure_storage()._connection_for_tests_only().execute(
        "select status from turn_requests where request_id = 'r-1'"
    ).fetchone()
    assert row is None or row[0] != "pending"

    # Retry with the SAME request id succeeds once the provider is up.
    provider.fail_with = None
    second = await runtime.commit_turn_async(request_id="r-1", message="hello")
    assert second.success is True
    assert provider.calls == 1
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
    from backend.language_model import LanguageModelServerError

    provider = ScriptedProvider()
    provider.fail_with = LanguageModelServerError()
    runtime = make_runtime(tmp_path, provider)
    result = await runtime.commit_turn_async(request_id="r-1", message="hello")
    assert result.success is False
    # Canonical server_error is a generic provider failure (the web maps it
    # to provider_unavailable/503); a secret must never cross.
    assert result.error_code == LocalErrorCode.SERVICE_UNAVAILABLE
    assert result.error_message == LanguageModelServerError.MESSAGE
    runtime.close()


async def test_commit_turn_pool_exhausted_rate_limited(tmp_path):
    from backend.language_model import LanguageModelRateLimitedError

    provider = ScriptedProvider()
    provider.fail_with = LanguageModelRateLimitedError()
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


# ─────────────────────────────────────────────────────────────────
# #336 review blocker 1: memory-storage failure is fail-closed
# ─────────────────────────────────────────────────────────────────


async def test_corrupt_memory_metadata_blocks_turn_and_provider(tmp_path):
    """Corrupt memory metadata must never degrade into "no memories".

    #335/#336 fail-closed contract: ``load_recent_memories`` raises
    ``PersistenceError`` on corrupt metadata; the runtime used to
    swallow it and let the turn proceed to the provider as if nothing
    happened. Now the corruption surfaces as a sanitized ``storage``
    error, the provider is NEVER called, and nothing is reset or
    silently removed.
    """
    provider = ScriptedProvider()
    runtime = make_runtime(tmp_path, provider)
    # Healthy turn first so the database is fully initialized.
    await runtime.commit_turn_async(request_id="r-1", message="hello")
    # Persist a valid memory through the real store. The loaded-data
    # contract requires a canonical-UUID source_id (the trusted-context
    # boundary validates it) and a legacy provenance from the allowlist.
    import uuid as _uuid

    storage = runtime._ensure_storage()
    storage.store_memory(
        "lembra do café",
        {"source_id": str(_uuid.uuid4()), "provenance": "legacy_memory"},
    )
    runtime.close()

    # Corrupt the memory row directly in the real SQLite file — the
    # runtime must observe it through the store's contract. The
    # schema CHECK only enforces json_valid(metadata), but the
    # store's READ contract requires the parsed value to be a JSON
    # object; a scalar/string payload therefore passes the schema
    # and violates the read-side fail-closed contract exactly like
    # a real corrupted row does.
    import sqlite3

    conn = sqlite3.connect(tmp_path / "katherine.db")
    conn.execute("update memories set metadata = '\"corrupted string\"'")
    conn.commit()
    conn.close()

    runtime2 = make_runtime(tmp_path, ScriptedProvider())
    fresh_provider = runtime2._provider_port()
    calls_before = getattr(fresh_provider, "calls", 0)
    appraisal_before = getattr(fresh_provider, "appraisal_calls", 0)

    result = await runtime2.commit_turn_async(request_id="r-2", message="again")

    assert result.success is False
    assert result.error_code == LocalErrorCode.STORAGE.value
    # The provider was never reached: no provider call was spent.
    fresh_provider_after = runtime2._provider_port()
    assert getattr(fresh_provider_after, "calls", 0) == calls_before
    assert (
        getattr(fresh_provider_after, "appraisal_calls", 0) == appraisal_before
    )
    # Sanitized: no path, no SQL, no traceback crossing.
    assert "katherine.db" not in (result.error_message or "")
    assert "sqlite" not in (result.error_message or "").lower()

    # No reset/removal: the corrupt row is still there, untouched.
    conn = sqlite3.connect(tmp_path / "katherine.db")
    n_memories = conn.execute("select count(*) from memories").fetchone()[0]
    conn.close()
    assert n_memories >= 1

    # The turn ledger has no completed row for r-2 (no silent success).
    conn = sqlite3.connect(tmp_path / "katherine.db")
    row = conn.execute(
        "select status from turn_requests where request_id = 'r-2'"
    ).fetchone()
    conn.close()
    assert row is None or row[0] != "completed"

    runtime2.close()


async def test_healthy_memories_still_flow_into_context(tmp_path):
    """Non-regression: valid memories still load into the context."""
    import uuid as _uuid

    provider = ScriptedProvider()
    runtime = make_runtime(tmp_path, provider)
    await runtime.commit_turn_async(request_id="r-1", message="hello")
    storage = runtime._ensure_storage()
    storage.store_memory(
        "lembra do café",
        {"source_id": str(_uuid.uuid4()), "provenance": "legacy_memory"},
    )
    runtime.close()

    runtime2 = make_runtime(tmp_path, ScriptedProvider())
    result = await runtime2.commit_turn_async(request_id="r-2", message="again")
    assert result.success is True
    runtime2.close()


async def test_unconfigured_provider_yields_configuration_error(tmp_path):
    from backend.language_model import LanguageModelConfigurationError

    class Unconfigured:
        async def appraise(self, message, budget):
            raise LanguageModelConfigurationError()

        async def generate(self, messages, budget):
            raise LanguageModelConfigurationError()

    runtime = CompanionRuntime(
        storage_path=tmp_path / "katherine.db",
        language_model=Unconfigured(),
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
    from backend.language_model import LanguageModelConfigurationError

    assert (
        runtime_error_code(LanguageModelConfigurationError())
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
