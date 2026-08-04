"""HTTP-facing conversation engine adapter with explicit budget injection."""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from .engine import ConversationEngine
from .process_turn import (
    ProcessTurnInput,
    TurnMode,
    build_process_turn,
)
from .turn_execution import (
    TurnBudget,
    TurnExecutionConfig,
    DeadlineExceeded,
    create_budget,
)


class ChatConversationEngine(ConversationEngine):
    """Conversation engine used by the active request path.

    The active path delegates the whole turn to the idempotent, transactional
    ``ProcessTurn`` use case (#272): state is loaded with its revision, the
    provider runs outside the transaction, and a single ``commit_turn`` RPC
    persists messages, snapshots, request completion and outbox atomically
    with CAS. ``save_turn`` / ``sync_state`` / ``BackgroundTasks`` are never
    used by this class.

    Internal callers may omit ``budget`` and retain the existing behavior.
    The HTTP admission path supplies the already-started budget so admission,
    lock acquisition, generation, and commit share one monotonic deadline.
    """

    def __init__(
        self,
        clock=time.time,
        archival_extraction_enabled: bool = False,
        turn_config: Optional[TurnExecutionConfig] = None,
    ):
        super().__init__(
            clock=clock,
            archival_extraction_enabled=archival_extraction_enabled,
            turn_config=turn_config,
        )
        self._process_turn = build_process_turn(self)

    async def process_turn(
        self,
        user_id: str,
        user_message: str,
        request_id: str,
        *,
        budget: Optional[TurnBudget] = None,
        mode: TurnMode = TurnMode.normal,
        correlation: str,
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
            request_id,
            active_budget,
            mode,
            correlation,
        )

    async def _run_turn_locked(self, user_id, user_message, request_id, budget, mode, correlation):
        # Only the lock acquisition is bounded by remaining_before_reserve.
        # Once acquired, the turn runs under budget checks (each stage
        # checks remaining_before_reserve).  This prevents the outer timeout
        # from firing while the commit section (protected by the write drain
        # in run_blocking_write) is executing, which would release the lock
        # prematurely.
        lock_timeout = budget.remaining_before_reserve
        ctx = self.lock_manager.lock(user_id)
        try:
            await asyncio.wait_for(ctx.__aenter__(), timeout=lock_timeout)
        except asyncio.TimeoutError:
            raise DeadlineExceeded()
        try:
            inp = ProcessTurnInput(
                authenticated_user_id=user_id,
                request_id=request_id,
                user_message=user_message,
                budget=budget,
                correlation=correlation,
                mode=mode,
            )
            return await self._process_turn.execute(inp)
        finally:
            await ctx.__aexit__(None, None, None)
