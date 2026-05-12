"""Plan broker — manages pending plan reviews via asyncio Futures.

Mirrors the PermissionBroker pattern: the plan tool suspends on a
Future that is resolved when the user acts on the plan review dialog.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from openclose.log import get_logger

log = get_logger(__name__)


@dataclass
class PlanReply:
    """User's response to a plan review."""

    action: str  # "execute", "reject", "revise"
    feedback: str = ""


@dataclass
class PendingPlanReview:
    """A plan awaiting user review."""

    request_id: str
    plan_content: str
    session_id: str
    future: asyncio.Future[PlanReply]


class PlanBroker:
    """Manages pending plan reviews with asyncio Futures.

    When the agent invokes the plan tool, the agent loop calls ``ask()``
    which suspends until the user replies via ``reply()``.
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingPlanReview] = {}

    async def ask(
        self,
        request_id: str,
        plan_content: str,
        session_id: str = "",
    ) -> PlanReply:
        """Create a pending plan review and wait for the user's action.

        Returns a ``PlanReply`` with the user's chosen action and optional
        feedback (when action is "revise").
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future[PlanReply] = loop.create_future()

        pending = PendingPlanReview(
            request_id=request_id,
            plan_content=plan_content,
            session_id=session_id,
            future=future,
        )
        self._pending[request_id] = pending
        log.info(
            "Plan review %s: %s... (session %s)",
            request_id,
            plan_content[:80],
            session_id,
        )

        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    def reply(
        self,
        request_id: str,
        action: str,
        feedback: str = "",
    ) -> bool:
        """Resolve a pending plan review with the user's action.

        Returns True if the review was found and resolved.
        """
        pending = self._pending.get(request_id)
        if pending is None:
            log.warning("Plan reply for unknown request: %s", request_id)
            return False

        if not pending.future.done():
            pending.future.set_result(PlanReply(action=action, feedback=feedback))
            log.info("Plan reply %s: %s", request_id, action)
        return True

    def cancel_session(self, session_id: str) -> None:
        """Cancel all pending plan reviews for a session."""
        to_cancel = [
            p for p in self._pending.values() if p.session_id == session_id
        ]
        for pending in to_cancel:
            if not pending.future.done():
                pending.future.set_result(PlanReply(action="reject"))
            self._pending.pop(pending.request_id, None)
        if to_cancel:
            log.info(
                "Cancelled %d pending plan reviews for session %s",
                len(to_cancel),
                session_id,
            )

    def list_pending(self) -> list[dict[str, str]]:
        """List all pending plan reviews."""
        return [
            {
                "request_id": p.request_id,
                "plan_content": p.plan_content,
                "session_id": p.session_id,
            }
            for p in self._pending.values()
        ]


# Singleton
_plan_broker: PlanBroker | None = None


def get_plan_broker() -> PlanBroker:
    """Get the global PlanBroker singleton."""
    global _plan_broker
    if _plan_broker is None:
        _plan_broker = PlanBroker()
    return _plan_broker
