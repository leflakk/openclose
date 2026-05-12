"""Tests for the PermissionBroker."""

from __future__ import annotations

import asyncio

import pytest

from openclose.permission.broker import PermissionBroker
from openclose.permission.schema import PermissionRequest


@pytest.fixture
def broker() -> PermissionBroker:
    return PermissionBroker()


@pytest.mark.asyncio
async def test_ask_and_reply_once(broker: PermissionBroker) -> None:
    """ask() suspends until reply() is called; 'once' is returned."""
    request = PermissionRequest(tool_name="bash")

    async def _reply_later() -> None:
        await asyncio.sleep(0.01)
        pending = broker.list_pending()
        assert len(pending) == 1
        broker.reply(pending[0]["request_id"], "once")

    task = asyncio.create_task(_reply_later())
    result = await broker.ask(request, session_id="s1")
    assert result == "once"
    await task


@pytest.mark.asyncio
async def test_ask_and_reply_always(broker: PermissionBroker) -> None:
    request = PermissionRequest(tool_name="write")

    async def _reply_later() -> None:
        await asyncio.sleep(0.01)
        pending = broker.list_pending()
        broker.reply(pending[0]["request_id"], "always")

    task = asyncio.create_task(_reply_later())
    result = await broker.ask(request, session_id="s1")
    assert result == "always"
    await task


@pytest.mark.asyncio
async def test_ask_and_reply_reject(broker: PermissionBroker) -> None:
    request = PermissionRequest(tool_name="bash")

    async def _reply_later() -> None:
        await asyncio.sleep(0.01)
        pending = broker.list_pending()
        broker.reply(pending[0]["request_id"], "reject")

    task = asyncio.create_task(_reply_later())
    result = await broker.ask(request, session_id="s1")
    assert result == "reject"
    await task


@pytest.mark.asyncio
async def test_cancel_session(broker: PermissionBroker) -> None:
    """cancel_session rejects all pending requests for that session."""
    request = PermissionRequest(tool_name="bash")

    async def _cancel_later() -> None:
        await asyncio.sleep(0.01)
        broker.cancel_session("s1")

    task = asyncio.create_task(_cancel_later())
    result = await broker.ask(request, session_id="s1")
    assert result == "reject"
    await task
    assert len(broker.list_pending()) == 0


@pytest.mark.asyncio
async def test_list_pending(broker: PermissionBroker) -> None:
    """list_pending shows requests that haven't been replied to."""
    request = PermissionRequest(tool_name="bash", path="/tmp/test")

    async def _check_and_reply() -> None:
        await asyncio.sleep(0.01)
        pending = broker.list_pending()
        assert len(pending) == 1
        assert pending[0]["tool_name"] == "bash"
        assert pending[0]["path"] == "/tmp/test"
        assert pending[0]["session_id"] == "s1"
        broker.reply(pending[0]["request_id"], "once")

    task = asyncio.create_task(_check_and_reply())
    await broker.ask(request, session_id="s1")
    await task


def test_reply_unknown_request(broker: PermissionBroker) -> None:
    """Replying to an unknown request returns False."""
    assert broker.reply("nonexistent", "once") is False
