"""HTTP-facing conversation engine adapter with explicit budget injection."""

from __future__ import annotations

from typing import Optional

from fastapi import BackgroundTasks

from .engine import ConversationEngine
from .turn_execution import TurnBudget, create_budget


class ChatConversationEngine(ConversationEngine):
    """Conversation engine used by the active request path.

    Internal callers may omit ``budget`` and retain the existing behavior.
    The HTTP admission path supplies the already-started budget so admission,
    lock acquisition, generation, and commit share one monotonic deadline.
    """

    async def process_turn(
        self,
        user_id: str,
        user_message: str,
        background_tasks: Optional[BackgroundTasks] = None,
        *,
        budget: Optional[TurnBudget] = None,
    ):
        active_budget = (
            budget
            if budget is not None
            else create_budget(self._turn_config, now_provider=self._monotonic)
        )
        if not isinstance(active_budget, TurnBudget):
            raise TypeError("budget must be a TurnBudget")
        return await self._run_turn_locked(
            user_id,
            user_message,
            background_tasks,
            active_budget,
        )
