"""Ask-user broker — manages pending user questions via asyncio Futures.

Mirrors the PlanBroker pattern: the ask_user tool suspends on a
Future that is resolved when the user answers all questions via the dialog.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from openclose.log import get_logger

log = get_logger(__name__)


@dataclass
class AskUserReply:
    """User's answers to the questions."""

    answers: list[dict[str, str]] = field(default_factory=list)


@dataclass
class PendingAskUser:
    """A set of questions awaiting user answers."""

    request_id: str
    questions: list[dict[str, Any]]
    session_id: str
    future: asyncio.Future[AskUserReply]


class AskUserBroker:
    """Manages pending ask_user requests with asyncio Futures.

    When the agent invokes the ask_user tool, the agent loop calls ``ask()``
    which suspends until the user replies via ``reply()``.
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingAskUser] = {}

    async def ask(
        self,
        request_id: str,
        questions: list[dict[str, Any]],
        session_id: str = "",
    ) -> AskUserReply:
        """Create a pending ask_user request and wait for the user's answers."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[AskUserReply] = loop.create_future()

        pending = PendingAskUser(
            request_id=request_id,
            questions=questions,
            session_id=session_id,
            future=future,
        )
        self._pending[request_id] = pending
        log.info(
            "Ask user %s: %d questions (session %s)",
            request_id,
            len(questions),
            session_id,
        )

        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    def reply(
        self,
        request_id: str,
        answers: list[dict[str, str]],
    ) -> bool:
        """Resolve a pending ask_user request with the user's answers.

        Returns True if the request was found and resolved.
        """
        pending = self._pending.get(request_id)
        if pending is None:
            log.warning("Ask user reply for unknown request: %s", request_id)
            return False

        if not pending.future.done():
            pending.future.set_result(AskUserReply(answers=answers))
            log.info("Ask user reply %s: %d answers", request_id, len(answers))
        return True

    def cancel_session(self, session_id: str) -> None:
        """Cancel all pending ask_user requests for a session."""
        to_cancel = [
            p for p in self._pending.values() if p.session_id == session_id
        ]
        for pending in to_cancel:
            if not pending.future.done():
                pending.future.set_result(AskUserReply(answers=[]))
            self._pending.pop(pending.request_id, None)
        if to_cancel:
            log.info(
                "Cancelled %d pending ask_user requests for session %s",
                len(to_cancel),
                session_id,
            )


# Singleton
_ask_user_broker: AskUserBroker | None = None


def get_ask_user_broker() -> AskUserBroker:
    """Get the global AskUserBroker singleton."""
    global _ask_user_broker
    if _ask_user_broker is None:
        _ask_user_broker = AskUserBroker()
    return _ask_user_broker
