"""Tests targeting specific coverage gaps in smaller modules."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openclose.config.config import load_config


# ── multiedit tool ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_multiedit_list_edits(tmp_path: Path) -> None:
    from openclose.tool.tools.multiedit import make_multiedit_tool

    f = tmp_path / "test.py"
    f.write_text("foo\nbar\nbaz\n")
    tool = make_multiedit_tool(str(tmp_path))
    result = await tool.execute(
        file_path=str(f),
        edits=[
            {"old_string": "foo", "new_string": "FOO"},
            {"old_string": "bar", "new_string": "BAR"},
        ],
    )
    assert result.ok
    assert "Applied 2 edits" in result.output
    assert f.read_text() == "FOO\nBAR\nbaz\n"


@pytest.mark.asyncio
async def test_multiedit_json_string_edits(tmp_path: Path) -> None:
    from openclose.tool.tools.multiedit import make_multiedit_tool

    f = tmp_path / "test.py"
    f.write_text("alpha\nbeta\n")
    tool = make_multiedit_tool(str(tmp_path))
    edits_json = json.dumps([{"old_string": "alpha", "new_string": "ALPHA"}])
    result = await tool.execute(file_path=str(f), edits=edits_json)
    assert result.ok
    assert "Applied 1 edit" in result.output


@pytest.mark.asyncio
async def test_multiedit_invalid_json(tmp_path: Path) -> None:
    from openclose.tool.tools.multiedit import make_multiedit_tool

    f = tmp_path / "test.py"
    f.write_text("content\n")
    tool = make_multiedit_tool(str(tmp_path))
    result = await tool.execute(file_path=str(f), edits="not json{{{")
    assert not result.ok
    assert "Invalid edits JSON" in result.error


@pytest.mark.asyncio
async def test_multiedit_non_list_edits(tmp_path: Path) -> None:
    from openclose.tool.tools.multiedit import make_multiedit_tool

    f = tmp_path / "test.py"
    f.write_text("content\n")
    tool = make_multiedit_tool(str(tmp_path))
    result = await tool.execute(file_path=str(f), edits=42)
    assert not result.ok
    assert "edits must be an array" in result.error


@pytest.mark.asyncio
async def test_multiedit_empty_list(tmp_path: Path) -> None:
    from openclose.tool.tools.multiedit import make_multiedit_tool

    f = tmp_path / "test.py"
    f.write_text("content\n")
    tool = make_multiedit_tool(str(tmp_path))
    result = await tool.execute(file_path=str(f), edits=[])
    assert not result.ok
    assert "non-empty" in result.error


@pytest.mark.asyncio
async def test_multiedit_outside_project(tmp_path: Path) -> None:
    from openclose.tool.tools.multiedit import make_multiedit_tool

    tool = make_multiedit_tool(str(tmp_path / "project"))
    result = await tool.execute(file_path="/etc/passwd", edits=[{"old_string": "a", "new_string": "b"}])
    assert not result.ok
    assert "Cannot edit outside" in result.error


@pytest.mark.asyncio
async def test_multiedit_file_not_found(tmp_path: Path) -> None:
    from openclose.tool.tools.multiedit import make_multiedit_tool

    tool = make_multiedit_tool(str(tmp_path))
    result = await tool.execute(
        file_path=str(tmp_path / "missing.py"),
        edits=[{"old_string": "a", "new_string": "b"}],
    )
    assert not result.ok
    assert "File not found" in result.error


@pytest.mark.asyncio
async def test_multiedit_old_string_not_found(tmp_path: Path) -> None:
    from openclose.tool.tools.multiedit import make_multiedit_tool

    f = tmp_path / "test.py"
    f.write_text("hello\n")
    tool = make_multiedit_tool(str(tmp_path))
    result = await tool.execute(
        file_path=str(f),
        edits=[{"old_string": "nonexistent", "new_string": "x"}],
    )
    assert not result.ok
    assert "not found" in result.error


@pytest.mark.asyncio
async def test_multiedit_old_string_multiple(tmp_path: Path) -> None:
    from openclose.tool.tools.multiedit import make_multiedit_tool

    f = tmp_path / "test.py"
    f.write_text("dup\ndup\n")
    tool = make_multiedit_tool(str(tmp_path))
    result = await tool.execute(
        file_path=str(f),
        edits=[{"old_string": "dup", "new_string": "x"}],
    )
    assert not result.ok
    assert "found 2 times" in result.error


@pytest.mark.asyncio
async def test_multiedit_non_dict_edit(tmp_path: Path) -> None:
    from openclose.tool.tools.multiedit import make_multiedit_tool

    f = tmp_path / "test.py"
    f.write_text("content\n")
    tool = make_multiedit_tool(str(tmp_path))
    result = await tool.execute(file_path=str(f), edits=["not a dict"])
    assert not result.ok
    assert "must be an object" in result.error


@pytest.mark.asyncio
async def test_multiedit_missing_old_string(tmp_path: Path) -> None:
    from openclose.tool.tools.multiedit import make_multiedit_tool

    f = tmp_path / "test.py"
    f.write_text("content\n")
    tool = make_multiedit_tool(str(tmp_path))
    result = await tool.execute(file_path=str(f), edits=[{"new_string": "x"}])
    assert not result.ok
    assert "old_string is required" in result.error


@pytest.mark.asyncio
async def test_multiedit_relative_path(tmp_path: Path) -> None:
    from openclose.tool.tools.multiedit import make_multiedit_tool

    f = tmp_path / "sub" / "test.py"
    f.parent.mkdir()
    f.write_text("hello\n")
    tool = make_multiedit_tool(str(tmp_path))
    result = await tool.execute(
        file_path="sub/test.py",
        edits=[{"old_string": "hello", "new_string": "hi"}],
    )
    assert result.ok
    assert f.read_text() == "hi\n"


# ── plan_broker ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_plan_broker_execute() -> None:
    from openclose.tool.tools.plan_broker import PlanBroker

    broker = PlanBroker()

    async def reply_later() -> None:
        await asyncio.sleep(0.01)
        pending = broker.list_pending()
        assert len(pending) == 1
        broker.reply(pending[0]["request_id"], "execute")

    task = asyncio.create_task(reply_later())
    reply = await broker.ask("req1", "The plan", session_id="s1")
    assert reply.action == "execute"
    assert reply.feedback == ""
    await task


@pytest.mark.asyncio
async def test_plan_broker_reject() -> None:
    from openclose.tool.tools.plan_broker import PlanBroker

    broker = PlanBroker()

    async def reply_later() -> None:
        await asyncio.sleep(0.01)
        pending = broker.list_pending()
        broker.reply(pending[0]["request_id"], "reject")

    task = asyncio.create_task(reply_later())
    reply = await broker.ask("req2", "The plan", session_id="s1")
    assert reply.action == "reject"
    await task


@pytest.mark.asyncio
async def test_plan_broker_revise() -> None:
    from openclose.tool.tools.plan_broker import PlanBroker

    broker = PlanBroker()

    async def reply_later() -> None:
        await asyncio.sleep(0.01)
        pending = broker.list_pending()
        broker.reply(pending[0]["request_id"], "revise", "Add more detail")

    task = asyncio.create_task(reply_later())
    reply = await broker.ask("req3", "The plan", session_id="s1")
    assert reply.action == "revise"
    assert reply.feedback == "Add more detail"
    await task


@pytest.mark.asyncio
async def test_plan_broker_reply_unknown() -> None:
    from openclose.tool.tools.plan_broker import PlanBroker

    broker = PlanBroker()
    assert broker.reply("nonexistent", "execute") is False


@pytest.mark.asyncio
async def test_plan_broker_cancel_session() -> None:
    from openclose.tool.tools.plan_broker import PlanBroker

    broker = PlanBroker()

    async def ask_and_get_cancelled() -> str:
        reply = await broker.ask("req4", "The plan", session_id="s1")
        return reply.action

    task = asyncio.create_task(ask_and_get_cancelled())
    await asyncio.sleep(0.01)
    broker.cancel_session("s1")
    action = await task
    assert action == "reject"


@pytest.mark.asyncio
async def test_plan_broker_list_pending() -> None:
    from openclose.tool.tools.plan_broker import PlanBroker

    broker = PlanBroker()
    assert broker.list_pending() == []

    async def ask_task() -> None:
        await broker.ask("req5", "The plan", session_id="s1")

    task = asyncio.create_task(ask_task())
    await asyncio.sleep(0.01)
    pending = broker.list_pending()
    assert len(pending) == 1
    assert pending[0]["plan_content"] == "The plan"
    assert pending[0]["session_id"] == "s1"
    # Clean up
    broker.reply(pending[0]["request_id"], "execute")
    await task


# ── cancel registry ─────────────────────────────────────────────────────────

def test_cancel_registry_lifecycle() -> None:
    from openclose.session.cancel import CancelRegistry

    reg = CancelRegistry()
    event = reg.register("s1")
    assert not reg.is_cancelled("s1")

    assert reg.cancel("s1")
    assert reg.is_cancelled("s1")
    assert event.is_set()

    reg.unregister("s1")
    assert not reg.is_cancelled("s1")


def test_cancel_registry_unknown() -> None:
    from openclose.session.cancel import CancelRegistry

    reg = CancelRegistry()
    assert not reg.cancel("nonexistent")
    assert not reg.is_cancelled("nonexistent")


def test_cancel_registry_singleton() -> None:
    from openclose.session.cancel import get_cancel_registry

    r1 = get_cancel_registry()
    r2 = get_cancel_registry()
    assert r1 is r2


# ── permission extract ──────────────────────────────────────────────────────

def test_extract_path_known_tool(tmp_path: Path) -> None:
    from openclose.permission.extract import extract_path

    result = extract_path("read", {"file_path": str(tmp_path / "x.py")}, str(tmp_path))
    assert str(tmp_path) in result


def test_extract_path_relative(tmp_path: Path) -> None:
    from openclose.permission.extract import extract_path

    result = extract_path("read", {"file_path": "sub/x.py"}, str(tmp_path))
    assert str(tmp_path) in result


def test_extract_path_unknown_tool() -> None:
    from openclose.permission.extract import extract_path

    assert extract_path("bash", {"command": "ls"}) == "*"


def test_extract_path_missing_arg() -> None:
    from openclose.permission.extract import extract_path

    assert extract_path("read", {}) == "*"


def test_extract_path_non_string_arg() -> None:
    from openclose.permission.extract import extract_path

    assert extract_path("read", {"file_path": 123}) == "*"


def test_check_path_sandbox_non_write() -> None:
    from openclose.permission.extract import check_path_sandbox

    assert check_path_sandbox("read", {"file_path": "/etc/passwd"}) is None


def test_check_path_sandbox_inside_project(tmp_path: Path) -> None:
    from openclose.permission.extract import check_path_sandbox

    result = check_path_sandbox(
        "write", {"file_path": str(tmp_path / "ok.py")}, str(tmp_path)
    )
    assert result is None


def test_check_path_sandbox_outside_project(tmp_path: Path) -> None:
    from openclose.permission.extract import check_path_sandbox

    result = check_path_sandbox(
        "write", {"file_path": "/etc/passwd"}, str(tmp_path)
    )
    assert result is not None
    assert "Cannot" in result


def test_check_path_sandbox_missing_arg() -> None:
    from openclose.permission.extract import check_path_sandbox

    assert check_path_sandbox("write", {}) is None


def test_check_path_sandbox_relative(tmp_path: Path) -> None:
    from openclose.permission.extract import check_path_sandbox

    result = check_path_sandbox("edit", {"file_path": "sub/x.py"}, str(tmp_path))
    assert result is None


# ── webfetch tool ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_webfetch_empty_url() -> None:
    from openclose.tool.tools.webfetch import make_webfetch_tool

    tool = make_webfetch_tool()
    result = await tool.execute(url="")
    assert not result.ok
    assert "required" in result.error


@pytest.mark.asyncio
async def test_webfetch_success() -> None:
    from openclose.tool.tools.webfetch import make_webfetch_tool

    tool = make_webfetch_tool()
    mock_response = MagicMock()
    mock_response.headers = {"content-type": "text/plain"}
    mock_response.text = "Hello, world!"
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("openclose.tool.tools.webfetch.httpx.AsyncClient", return_value=mock_client):
        result = await tool.execute(url="https://example.com")
    assert result.ok
    assert "Hello, world!" in result.output


@pytest.mark.asyncio
async def test_webfetch_html_conversion() -> None:
    from openclose.tool.tools.webfetch import make_webfetch_tool

    tool = make_webfetch_tool()
    mock_response = MagicMock()
    mock_response.headers = {"content-type": "text/html"}
    mock_response.text = "<html><body><p>Hello</p><script>evil()</script></body></html>"
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("openclose.tool.tools.webfetch.httpx.AsyncClient", return_value=mock_client):
        result = await tool.execute(url="https://example.com")
    assert result.ok
    assert "Hello" in result.output
    # Script tags should be removed
    assert "evil" not in result.output


@pytest.mark.asyncio
async def test_webfetch_http_error() -> None:
    from openclose.tool.tools.webfetch import make_webfetch_tool
    import httpx

    tool = make_webfetch_tool()
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "Not found", request=MagicMock(), response=mock_response
        )
    )

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("openclose.tool.tools.webfetch.httpx.AsyncClient", return_value=mock_client):
        result = await tool.execute(url="https://example.com/missing")
    assert not result.ok
    assert "HTTP 404" in result.error


@pytest.mark.asyncio
async def test_webfetch_network_error() -> None:
    from openclose.tool.tools.webfetch import make_webfetch_tool

    tool = make_webfetch_tool()
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=Exception("Connection refused"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("openclose.tool.tools.webfetch.httpx.AsyncClient", return_value=mock_client):
        result = await tool.execute(url="https://unreachable.test")
    assert not result.ok
    assert "Fetch error" in result.error


# ── provider ────────────────────────────────────────────────────────────────

def test_provider_init() -> None:
    from openclose.provider.provider import Provider

    p = Provider(base_url="http://localhost:9999/v1", api_key="test-key")
    assert p.client is not None


def test_provider_wrap_tools() -> None:
    from openclose.provider.provider import Provider

    tools = [{"name": "test", "parameters": {}}]
    wrapped = Provider._wrap_tools(tools)
    assert len(wrapped) == 1
    assert wrapped[0]["type"] == "function"
    assert wrapped[0]["function"]["name"] == "test"


@pytest.mark.asyncio
async def test_provider_detect_model_success() -> None:
    from openclose.provider.provider import Provider

    p = Provider(base_url="http://localhost:9999/v1")
    mock_model = MagicMock()
    mock_model.id = "test-model"
    mock_models = MagicMock()
    mock_models.data = [mock_model]
    mock_models_ns = MagicMock()
    mock_models_ns.list = AsyncMock(return_value=mock_models)
    object.__setattr__(p._client, "models", mock_models_ns)
    result = await p.detect_model()
    assert result == "test-model"


@pytest.mark.asyncio
async def test_provider_detect_model_failure() -> None:
    from openclose.provider.provider import Provider

    p = Provider(base_url="http://localhost:9999/v1")
    mock_models_ns = MagicMock()
    mock_models_ns.list = AsyncMock(side_effect=Exception("Connection refused"))
    object.__setattr__(p._client, "models", mock_models_ns)
    result = await p.detect_model()
    assert result is None


@pytest.mark.asyncio
async def test_provider_chat_sync() -> None:
    from openclose.provider.provider import Provider

    p = Provider(base_url="http://localhost:9999/v1")
    mock_resp = MagicMock()
    mock_chat = MagicMock()
    mock_chat.completions = MagicMock()
    mock_chat.completions.create = AsyncMock(return_value=mock_resp)
    object.__setattr__(p._client, "chat", mock_chat)

    result = await p.chat_sync(
        messages=[{"role": "user", "content": "hi"}],
        model="test-model",
        tools=[{"name": "t", "parameters": {}}],
        max_tokens=100,
    )
    assert result is mock_resp


@pytest.mark.asyncio
async def test_provider_chat_stream() -> None:
    from openclose.provider.provider import Provider

    p = Provider(base_url="http://localhost:9999/v1")

    chunk1 = MagicMock()
    chunk2 = MagicMock()

    async def mock_stream() -> Any:
        yield chunk1
        yield chunk2

    mock_chat = MagicMock()
    mock_chat.completions = MagicMock()
    mock_chat.completions.create = AsyncMock(return_value=mock_stream())
    object.__setattr__(p._client, "chat", mock_chat)

    chunks = []
    async for chunk in p.chat(
        messages=[{"role": "user", "content": "hi"}],
        model="test-model",
        tools=[{"name": "t", "parameters": {}}],
        max_tokens=100,
    ):
        chunks.append(chunk)
    assert len(chunks) == 2


# ── file watcher ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_watcher_stop_no_task() -> None:
    from openclose.file.watcher import FileWatcher

    watcher = FileWatcher(Path("/tmp"), AsyncMock())
    await watcher.stop()  # Should not raise


@pytest.mark.asyncio
async def test_watcher_start_no_watchfiles() -> None:
    from openclose.file.watcher import FileWatcher

    watcher = FileWatcher(Path("/tmp"), AsyncMock())
    with patch.dict("sys.modules", {"watchfiles": None}):
        with patch("openclose.file.watcher.FileWatcher.start") as mock_start:
            # Simulate ImportError path
            mock_start.return_value = None
            await watcher.start()


@pytest.mark.asyncio
async def test_watcher_stop_with_task() -> None:
    from openclose.file.watcher import FileWatcher

    watcher = FileWatcher(Path("/tmp"), AsyncMock())

    async def fake_watch() -> None:
        await asyncio.sleep(100)

    watcher._task = asyncio.create_task(fake_watch())
    await watcher.stop()
    assert watcher._task.cancelled() or watcher._task.done()


# ── session processor helpers ───────────────────────────────────────────────

def test_derive_title_short() -> None:
    from openclose.session.processor import _derive_title

    assert _derive_title("Hello world") == "Hello world"


def test_derive_title_long() -> None:
    from openclose.session.processor import _derive_title

    long_text = "This is a very long prompt that exceeds the maximum title length limit for sessions"
    title = _derive_title(long_text, max_len=30)
    assert len(title) <= 31  # max_len + ellipsis char
    assert title.endswith("\u2026")


def test_derive_title_whitespace() -> None:
    from openclose.session.processor import _derive_title

    assert _derive_title("  hello   world  ") == "hello world"


def test_reconstruct_llm_messages_plain() -> None:
    from openclose.session.processor import SessionProcessor
    from openclose.storage.schema import Message, MessagePart

    msg = Message(id="m1", session_id="s1", role="user", content="Hello")
    parts: list[MessagePart] = []
    result = SessionProcessor._reconstruct_llm_messages([(msg, parts)])
    assert len(result) == 1
    assert result[0] == {"role": "user", "content": "Hello"}


def test_reconstruct_llm_messages_with_tool_calls() -> None:
    from openclose.session.processor import SessionProcessor
    from openclose.storage.schema import Message, MessagePart
    from openclose.session.message import MessagePartType

    msg = Message(id="m1", session_id="s1", role="assistant", content="Using tool")
    tc = MessagePart(
        id="p1", message_id="m1",
        part_type=MessagePartType.TOOL_CALL.value,
        content='{"arg": 1}',
        tool_name="read",
        tool_call_id="tc1",
    )
    tr = MessagePart(
        id="p2", message_id="m1",
        part_type=MessagePartType.TOOL_RESULT.value,
        content="file contents",
        tool_name="read",
        tool_call_id="tc1",
    )
    result = SessionProcessor._reconstruct_llm_messages([(msg, [tc, tr])])
    assert len(result) == 2  # assistant + tool result
    assert result[0]["role"] == "assistant"
    assert len(result[0]["tool_calls"]) == 1
    assert result[1]["role"] == "tool"
    assert result[1]["content"] == "file contents"


def test_reconstruct_llm_messages_interrupted() -> None:
    from openclose.session.processor import SessionProcessor
    from openclose.storage.schema import Message, MessagePart
    from openclose.session.message import MessagePartType

    msg = Message(id="m1", session_id="s1", role="assistant", content="")
    tc = MessagePart(
        id="p1", message_id="m1",
        part_type=MessagePartType.TOOL_CALL.value,
        content='{}',
        tool_name="bash",
        tool_call_id="tc1",
    )
    # No corresponding tool result — simulates interruption
    result = SessionProcessor._reconstruct_llm_messages([(msg, [tc])])
    assert len(result) == 2
    assert "interrupted" in result[1]["content"]


def test_build_context_info(tmp_path: Path) -> None:
    from openclose.session.processor import SessionProcessor
    from openclose.storage.db import Database

    load_config()

    db = Database(tmp_path / "test.db")
    from openclose.session.session import SessionManager

    mgr = SessionManager(db)
    s = mgr.create_session()

    processor = SessionProcessor(
        db=db,
        session_id=s.id,
        tool_schemas=[{"name": "test", "parameters": {"properties": {}}}],
    )
    info = processor._build_context_info([{"role": "user", "content": "hi"}])
    assert "used" in info
    assert "max" in info
    assert "messages_tokens" in info
    assert "tools_tokens" in info
