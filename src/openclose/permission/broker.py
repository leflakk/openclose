"""Permission broker — manages pending permission requests via asyncio Futures."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Literal

from openclose.permission.schema import PermissionRequest
from openclose.id import generate_id
from openclose.log import get_logger

log = get_logger(__name__)

PermissionReply = Literal["once", "always", "reject"]


@dataclass
class PendingRequest:
    """A permission request awaiting user reply."""

    request_id: str
    request: PermissionRequest
    session_id: str
    future: asyncio.Future[PermissionReply]


class PermissionBroker:
    """Manages pending permission requests with asyncio Futures.

    When the agent loop encounters a tool call that needs user approval,
    it calls ``ask()`` which suspends until the user replies via ``reply()``.
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingRequest] = {}

    async def ask(
        self,
        request: PermissionRequest,
        session_id: str = "",
    ) -> PermissionReply:
        """Create a pending permission request and wait for a reply.

        Returns the user's reply: "once", "always", or "reject".
        """
        # Use pre-stamped ID if set (e.g. by the agent loop before
        # emitting the SSE event), otherwise generate a new one.
        request_id = request.request_id or generate_id()
        request.request_id = request_id

        loop = asyncio.get_running_loop()
        future: asyncio.Future[PermissionReply] = loop.create_future()

        pending = PendingRequest(
            request_id=request_id,
            request=request,
            session_id=session_id,
            future=future,
        )
        self._pending[request_id] = pending
        log.info("Permission request %s: %s (session %s)", request_id, request.tool_name, session_id)

        try:
            return await future
        finally:
            self._pending.pop(request_id, None)

    def reply(self, request_id: str, action: PermissionReply) -> bool:
        """Resolve a pending permission request.

        Returns True if the request was found and resolved.
        """
        pending = self._pending.get(request_id)
        if pending is None:
            log.warning("Permission reply for unknown request: %s", request_id)
            return False

        if not pending.future.done():
            pending.future.set_result(action)
            log.info("Permission reply %s: %s", request_id, action)
        return True

    def cancel_session(self, session_id: str) -> None:
        """Reject all pending requests for a session."""
        to_cancel = [
            p for p in self._pending.values() if p.session_id == session_id
        ]
        for pending in to_cancel:
            if not pending.future.done():
                pending.future.set_result("reject")
            self._pending.pop(pending.request_id, None)
        if to_cancel:
            log.info("Cancelled %d pending requests for session %s", len(to_cancel), session_id)

    def list_pending(self) -> list[dict[str, str]]:
        """List all pending permission requests."""
        return [
            {
                "request_id": p.request_id,
                "tool_name": p.request.tool_name,
                "path": p.request.path,
                "session_id": p.session_id,
            }
            for p in self._pending.values()
        ]


# Singleton
_broker: PermissionBroker | None = None


def get_broker() -> PermissionBroker:
    """Get the global PermissionBroker singleton."""
    global _broker
    if _broker is None:
        _broker = PermissionBroker()
    return _broker
