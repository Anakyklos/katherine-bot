"""Shared fakes for the #326 account deletion gate/API unit tests.

The account deletion HTTP gate runs on every normal route (/chat, /history
and the four #315 privacy actions). Existing unit tests inject
``ApplicationDependencies`` without a real Supabase client; these fakes
provide a no-tombstone gate so those tests keep exercising their own
scenario, and a configurable fake service for the account deletion API
tests themselves. No Supabase/Groq/embeddings client is ever constructed.
"""

from __future__ import annotations

from typing import Any, Optional

from backend.account_deletion_service import (
    AccountDeletionBlocked,
    AccountDeletionUnavailable,
)


class NoTombstoneGate:
    """Minimal fake gate: no tombstone ever; every normal route passes.

    ``assert_active`` never raises, so the route continues exactly as before
    the #326 gate existed.
    """

    def __init__(self, blocked: bool = False, unavailable: bool = False) -> None:
        self.blocked = blocked
        self.unavailable = unavailable
        self.checks: list[str] = []

    async def assert_active(self, authenticated_user_id: str) -> None:
        self.checks.append(authenticated_user_id)
        if self.unavailable:
            raise AccountDeletionUnavailable()
        if self.blocked:
            raise AccountDeletionBlocked()


class FakeAccountDeletionService:
    """Configurable fake for the account deletion API boundary.

    ``request`` records ``(user_id, operation_id)`` calls and returns a
    canned response (default ``accepted``) or raises a configured error.
    ``assert_active`` delegates to a ``NoTombstoneGate`` instance so the
    gate scenarios share one small fake.
    """

    def __init__(
        self,
        *,
        status: str = "accepted",
        error: Optional[BaseException] = None,
        blocked: bool = False,
        unavailable: bool = False,
    ) -> None:
        self._status = status
        self._error = error
        self.gate = NoTombstoneGate(blocked=blocked, unavailable=unavailable)
        self.requests: list[tuple[str, str]] = []

    async def request(self, authenticated_user_id: str, operation_id: str) -> Any:
        self.requests.append((authenticated_user_id, operation_id))
        if self._error is not None:
            raise self._error
        from backend.account_deletion_service import AccountDeletionRequestResponse

        return AccountDeletionRequestResponse(status=self._status)

    async def assert_active(self, authenticated_user_id: str) -> None:
        await self.gate.assert_active(authenticated_user_id)
