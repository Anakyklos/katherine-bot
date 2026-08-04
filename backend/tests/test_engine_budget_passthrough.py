from __future__ import annotations

import asyncio

from backend.chat_engine import ChatConversationEngine
from backend.process_turn import TurnMode
from backend.turn_execution import TurnBudget, TurnExecutionConfig

UUID = "550e8400-e29b-41d4-a716-446655440000"
CORRELATION = "c" * 64


def test_process_turn_uses_explicit_budget_object():
    engine = object.__new__(ChatConversationEngine)
    provided = TurnBudget(deadline=100.0, reserve=10.0, now_provider=lambda: 0.0)
    captured = {}

    async def fake_run(user_id, message, request_id, budget, mode, correlation):
        captured.update(
            user_id=user_id,
            message=message,
            request_id=request_id,
            budget=budget,
            mode=mode,
            correlation=correlation,
        )
        return "ok"

    engine._run_turn_locked = fake_run
    result = asyncio.run(
        engine.process_turn(
            "user-a",
            "hello",
            UUID,
            budget=provided,
            mode=TurnMode.normal,
            correlation=CORRELATION,
        )
    )
    assert result == "ok"
    assert captured["budget"] is provided
    assert captured["request_id"] == UUID
    assert captured["mode"] is TurnMode.normal
    assert captured["correlation"] == CORRELATION


def test_process_turn_still_creates_budget_for_internal_callers():
    engine = object.__new__(ChatConversationEngine)
    engine._turn_config = TurnExecutionConfig.defaults()
    engine._monotonic = lambda: 10.0
    captured = {}

    async def fake_run(_user_id, _message, _request_id, budget, _mode, correlation):
        captured["budget"] = budget
        captured["correlation"] = correlation
        return "ok"

    engine._run_turn_locked = fake_run
    assert (
        asyncio.run(
            engine.process_turn(
                "user-a", "hello", UUID, correlation=CORRELATION
            )
        )
        == "ok"
    )
    budget = captured["budget"]
    assert isinstance(budget, TurnBudget)
    assert budget.deadline == 55.0
    assert budget.reserve == 10.0
    assert captured["correlation"] == CORRELATION
