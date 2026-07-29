from __future__ import annotations

import asyncio

from backend.chat_engine import ChatConversationEngine
from backend.turn_execution import TurnBudget, TurnExecutionConfig


def test_process_turn_uses_explicit_budget_object():
    engine = object.__new__(ChatConversationEngine)
    provided = TurnBudget(deadline=100.0, reserve=10.0, now_provider=lambda: 0.0)
    captured = {}

    async def fake_run(user_id, message, background_tasks, budget):
        captured.update(
            user_id=user_id,
            message=message,
            background_tasks=background_tasks,
            budget=budget,
        )
        return "ok"

    engine._run_turn_locked = fake_run
    result = asyncio.run(
        engine.process_turn("user-a", "hello", None, budget=provided)
    )
    assert result == "ok"
    assert captured["budget"] is provided


def test_process_turn_still_creates_budget_for_internal_callers():
    engine = object.__new__(ChatConversationEngine)
    engine._turn_config = TurnExecutionConfig.defaults()
    engine._monotonic = lambda: 10.0
    captured = {}

    async def fake_run(_user_id, _message, _background_tasks, budget):
        captured["budget"] = budget
        return "ok"

    engine._run_turn_locked = fake_run
    assert asyncio.run(engine.process_turn("user-a", "hello")) == "ok"
    budget = captured["budget"]
    assert isinstance(budget, TurnBudget)
    assert budget.deadline == 55.0
    assert budget.reserve == 10.0
