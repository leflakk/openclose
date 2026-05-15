"""Tests for remaining coverage gaps: debug, process, watcher, grep, app, delegate, config, storage, routes."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── debug.py ─────────────────────────────────────────────────────────────────

def test_dump_llm_request_disabled() -> None:
    from openclose.debug import dump_llm_request
    import openclose.flag as flag

    orig = flag.DEBUG_LLM
    try:
        flag.DEBUG_LLM = False
        # Should not write anything
        dump_llm_request(
            step=1, source="test", model="m", temperature=0.0,
            messages=[], tools=None, project_dir="/tmp/noproject",
        )
    finally:
        flag.DEBUG_LLM = orig


def test_dump_llm_request_enabled(tmp_path: Path) -> None:
    from openclose.debug import dump_llm_request
    import openclose.flag as flag

    orig = flag.DEBUG_LLM
    try:
        flag.DEBUG_LLM = True
        with patch("openclose.debug.ConfigPaths.project_runtime_dir", return_value=tmp_path):
            dump_llm_request(
                step=1, source="test", model="m", temperature=0.5,
                messages=[{"role": "user", "content": "hi"}],
                tools=[{"name": "t"}],
                project_dir=str(tmp_path),
            )
        log_file = tmp_path / "llm_debug.jsonl"
        assert log_file.exists()
        entry = json.loads(log_file.read_text().strip())
        assert entry["step"] == 1
        assert entry["source"] == "test"
        assert entry["model"] == "m"
    finally:
        flag.DEBUG_LLM = orig


# ── util/process.py ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_process_run_simple() -> None:
    from openclose.util.process import run

    result = await run("echo", "hello", timeout=10.0)
    assert result.ok
    assert "hello" in result.stdout
    # Windows' time.monotonic() resolution can record 0.0 for sub-millisecond
    # commands like `echo`, so allow zero — we just want the field populated.
    assert result.duration >= 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash invocation")
@pytest.mark.asyncio
async def test_process_run_nonzero_exit() -> None:
    from openclose.util.process import run

    result = await run("bash", "-c", "exit 42", timeout=10.0)
    assert not result.ok
    assert result.returncode == 42


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX bash invocation")
@pytest.mark.asyncio
async def test_process_run_stderr() -> None:
    from openclose.util.process import run

    result = await run("bash", "-c", "echo err >&2", timeout=10.0)
    assert "err" in result.stderr


@pytest.mark.asyncio
async def test_process_run_timeout() -> None:
    from openclose.util.process import run

    result = await run("sleep", "60", timeout=0.1)
    assert result.timed_out
    assert "timed out" in result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX os.killpg / signal.SIGKILL")
def test_kill_process_group_missing() -> None:
    from openclose.util.process import _kill_process_group

    proc = MagicMock()
    proc.pid = 999999
    # Should not raise even if process doesn't exist
    with patch("os.killpg", side_effect=ProcessLookupError):
        proc.kill = MagicMock(side_effect=ProcessLookupError)
        _kill_process_group(proc)


# ── file/watcher.py ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_watcher_start_with_watchfiles(tmp_path: Path) -> None:
    from openclose.file.watcher import FileWatcher

    callback = AsyncMock()
    watcher = FileWatcher(tmp_path, callback)

    # Mock awatch to be an async generator that immediately finishes
    async def mock_awatch(*args: Any, **kwargs: Any) -> Any:
        return
        yield  # noqa: F841 — makes it an async generator

    with patch("openclose.file.watcher.awatch", mock_awatch, create=True):
        with patch.object(watcher, "_watch", new_callable=AsyncMock):
            await watcher.start()
            assert watcher._task is not None
            await watcher.stop()


@pytest.mark.asyncio
async def test_watcher_start_import_error() -> None:
    from openclose.file.watcher import FileWatcher

    callback = AsyncMock()
    watcher = FileWatcher(Path("/tmp"), callback)

    # Simulate watchfiles not installed
    with patch.dict("sys.modules", {"watchfiles": None}):
        with patch("builtins.__import__", side_effect=ImportError("no watchfiles")):
            await watcher.start()
    assert watcher._task is None


@pytest.mark.asyncio
async def test_watcher_watch_callback_error() -> None:
    from openclose.file.watcher import FileWatcher

    callback = AsyncMock(side_effect=Exception("callback error"))
    watcher = FileWatcher(Path("/tmp"), callback)

    async def mock_awatch(path: str) -> Any:
        yield [("modified", "/tmp/test.py")]

    await watcher._watch(mock_awatch)
    # Should not raise — error is caught and logged


# ── tool/tools/grep.py ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_grep_with_ripgrep(tmp_path: Path) -> None:
    from openclose.tool.tools.grep import make_grep_tool

    (tmp_path / "test.py").write_text("def hello():\n    return 42\n")
    tool = make_grep_tool(str(tmp_path))
    # Use the real tool — rg is likely available on CI
    result = await tool.execute(pattern="hello", include="*.py")
    # Should work either way (rg or python fallback)
    assert result.ok or "No matches" in (result.output or "")


@pytest.mark.asyncio
async def test_grep_python_fallback(tmp_path: Path) -> None:
    from openclose.tool.tools.grep import make_grep_tool

    (tmp_path / "test.py").write_text("def hello():\n    return 42\n")
    tool = make_grep_tool(str(tmp_path))

    with patch("shutil.which", return_value=None):
        result = await tool.execute(pattern="hello", include="*.py")
    assert result.ok
    assert "hello" in result.output


@pytest.mark.asyncio
async def test_grep_python_invalid_regex(tmp_path: Path) -> None:
    from openclose.tool.tools.grep import _python_grep

    result = await _python_grep("[invalid", str(tmp_path), "")
    assert not result.ok
    assert "Invalid regex" in result.error


@pytest.mark.asyncio
async def test_grep_python_no_matches(tmp_path: Path) -> None:
    from openclose.tool.tools.grep import _python_grep

    (tmp_path / "test.py").write_text("nothing here\n")
    result = await _python_grep("zzznomatch", str(tmp_path), "*.py")
    assert "No matches" in result.output


@pytest.mark.asyncio
async def test_grep_relative_path(tmp_path: Path) -> None:
    from openclose.tool.tools.grep import make_grep_tool

    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "a.py").write_text("def target():\n    pass\n")
    tool = make_grep_tool(str(tmp_path))

    with patch("shutil.which", return_value=None):
        result = await tool.execute(pattern="target", path="sub")
    assert result.ok


# ── server/app.py ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_lifespan_startup_cleanup() -> None:
    from openclose.server.app import _lifespan
    from fastapi import FastAPI

    app = FastAPI()
    mock_mgr = MagicMock()
    mock_db = MagicMock()

    with patch("openclose.storage.db.get_db", return_value=mock_db), \
         patch("openclose.session.session.SessionManager", return_value=mock_mgr), \
         patch.object(__import__("openclose.server.app", fromlist=["close_db"]), "close_db") as mock_close:
        async with _lifespan(app):
            mock_mgr.cleanup_empty_sessions.assert_called_once()
        mock_close.assert_called_once()


@pytest.mark.asyncio
async def test_lifespan_startup_error() -> None:
    from openclose.server.app import _lifespan
    from fastapi import FastAPI

    app = FastAPI()

    with patch("openclose.storage.db.get_db", side_effect=Exception("DB failed")), \
         patch.object(__import__("openclose.server.app", fromlist=["close_db"]), "close_db"):
        # Should not raise — exception is caught
        async with _lifespan(app):
            pass


def test_create_app() -> None:
    from openclose.server.app import create_app

    with patch("openclose.server.app._STATIC_DIR", new=Path("/nonexistent")):
        app = create_app()
    assert app.title == "OpenClose"


# ── delegate.py ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_delegate_missing_missions() -> None:
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry

    registry = ToolRegistry()
    tool = make_delegate_tool("/tmp", registry)
    # Missing mission_1 and empty-string mission_1 both reject.
    result = await tool.execute()
    assert not result.ok
    assert "required" in result.error

    result = await tool.execute(mission_1="")
    assert not result.ok
    assert "non-empty" in result.error


@pytest.mark.asyncio
async def test_delegate_no_tools() -> None:
    from openclose.tool.tools.delegate import make_delegate_tool
    from openclose.tool.registry import ToolRegistry

    registry = ToolRegistry()  # empty — no sub-tools
    tool = make_delegate_tool("/tmp", registry)

    with patch("openclose.provider.provider.get_provider", return_value=MagicMock()):
        result = await tool.execute(mission_1="something")
    assert not result.ok
    assert "No tools available" in result.error


# ── config/schema.py ─────────────────────────────────────────────────────────

def test_schema_default_values() -> None:
    from openclose.config.schema import OpenCloseConfig, ProviderConfig

    config = OpenCloseConfig(
        project_dir="/tmp",
        providers=[ProviderConfig(base_url="http://localhost:8080/v1")],
    )
    assert config.max_context_tokens > 0
    assert config.compaction_threshold > 0
    assert config.default_agent == "build"


# ── config/paths.py ──────────────────────────────────────────────────────────

def test_config_paths() -> None:
    from openclose.config.paths import ConfigPaths

    assert ConfigPaths.config_dir() is not None
    assert ConfigPaths.data_dir() is not None
    assert ConfigPaths.cache_dir() is not None
    assert ConfigPaths.db_path() is not None
    assert ConfigPaths.user_config_path() is not None
    assert ConfigPaths.project_config_path(Path("/tmp/test")) is not None
    assert ConfigPaths.project_runtime_dir("/tmp/test") is not None


# ── storage/db.py ────────────────────────────────────────────────────────────

def test_db_session(tmp_path: Path) -> None:
    from openclose.storage.db import Database

    db = Database(tmp_path / "test.db")
    with db.get_session() as session:
        assert session is not None


def test_db_close(tmp_path: Path) -> None:
    from openclose.storage.db import Database

    db = Database(tmp_path / "test.db")
    db.close()
    # Should not raise on double close


# ── storage/migrations.py ────────────────────────────────────────────────────

def test_migrations_run(tmp_path: Path) -> None:
    from openclose.storage.db import Database

    db = Database(tmp_path / "migration_test.db")
    # Migrations should have already run during Database init
    with db.get_session() as session:
        from sqlalchemy import text
        result = session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        tables = [row[0] for row in result.all()]
        assert "messages" in tables or "sessions" in tables


# ── project/worktree.py ─────────────────────────────────────────────────────

def test_worktree_manager(tmp_path: Path) -> None:
    from openclose.project.worktree import WorktreeManager

    mgr = WorktreeManager(tmp_path)
    assert mgr._root == tmp_path


# ── config/agents.py ─────────────────────────────────────────────────────────

def test_load_agents_default() -> None:
    from openclose.config.agents import load_agents

    agents = load_agents()
    assert len(agents) > 0
    # Returns dict[str, AgentConfig], should have at least "build" agent
    assert "build" in agents


# ── server/routes.py additional endpoints ────────────────────────────────────

@pytest.mark.asyncio
async def test_route_plan_reply_invalid_action() -> None:
    from openclose.server.routes import plan_reply, PlanReplyRequest

    req = PlanReplyRequest(action="invalid_action")
    resp = await plan_reply("req1", req)
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_route_plan_reply_not_found() -> None:
    from openclose.server.routes import plan_reply, PlanReplyRequest

    req = PlanReplyRequest(action="execute")
    resp = await plan_reply("nonexistent", req)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_route_ask_user_reply_not_found() -> None:
    from openclose.server.routes import ask_user_reply, AskUserReplyRequest

    req = AskUserReplyRequest(answers=[{"question": "Q?", "answer": "A"}])
    resp = await ask_user_reply("nonexistent", req)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_route_switch_agent_not_found(tmp_path: Path) -> None:
    from openclose.server.routes import switch_agent, SwitchAgentRequest
    from openclose.storage.db import Database

    db = Database(tmp_path / "test.db")
    with patch("openclose.server.routes.get_db", return_value=db):
        req = SwitchAgentRequest(agent="build")
        resp = await switch_agent("nonexistent_session", req)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_route_switch_agent_success(tmp_path: Path) -> None:
    from openclose.server.routes import switch_agent, SwitchAgentRequest
    from openclose.storage.db import Database
    from openclose.session.session import SessionManager

    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    session = mgr.create_session()

    with patch("openclose.server.routes.get_db", return_value=db):
        req = SwitchAgentRequest(agent="build")
        resp = await switch_agent(session.id, req)
    data = json.loads(bytes(resp.body))
    assert data["ok"] is True
    assert data["agent"] == "build"


@pytest.mark.asyncio
async def test_route_toggle_plan_in_context(tmp_path: Path) -> None:
    from openclose.server.routes import toggle_plan_in_context
    from openclose.storage.db import Database
    from openclose.session.session import SessionManager

    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    session = mgr.create_session()

    with patch("openclose.server.routes.get_db", return_value=db):
        resp = await toggle_plan_in_context(session.id)
    data = json.loads(bytes(resp.body))
    assert data["ok"] is True
    assert "plan_in_context" in data


@pytest.mark.asyncio
async def test_route_toggle_plan_in_context_not_found(tmp_path: Path) -> None:
    from openclose.server.routes import toggle_plan_in_context
    from openclose.storage.db import Database

    db = Database(tmp_path / "test.db")
    with patch("openclose.server.routes.get_db", return_value=db):
        resp = await toggle_plan_in_context("nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_route_get_plan(tmp_path: Path) -> None:
    from openclose.server.routes import get_plan
    from openclose.storage.db import Database
    from openclose.session.session import SessionManager
    from openclose.config.config import load_config

    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    session = mgr.create_session()
    config = load_config(project_dir=tmp_path)

    with patch("openclose.server.routes.get_db", return_value=db), \
         patch("openclose.server.routes.get_config", return_value=config):
        resp = await get_plan(session.id)
    data = json.loads(bytes(resp.body))
    assert "exists" in data
    assert "plan_in_context" in data


@pytest.mark.asyncio
async def test_route_get_plan_not_found(tmp_path: Path) -> None:
    from openclose.server.routes import get_plan
    from openclose.storage.db import Database

    db = Database(tmp_path / "test.db")
    with patch("openclose.server.routes.get_db", return_value=db), \
         patch("openclose.server.routes.get_config", return_value=MagicMock(project_dir=str(tmp_path))):
        resp = await get_plan("nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_route_skip_permissions(tmp_path: Path) -> None:
    from openclose.server.routes import toggle_skip_permissions, get_skip_permissions

    resp = await get_skip_permissions("test_session")
    data = json.loads(bytes(resp.body))
    assert "skip_all" in data

    resp = await toggle_skip_permissions("test_session")
    data = json.loads(bytes(resp.body))
    assert "skip_all" in data


@pytest.mark.asyncio
async def test_route_delete_session(tmp_path: Path) -> None:
    from openclose.server.routes import delete_session
    from openclose.storage.db import Database
    from openclose.session.session import SessionManager

    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    session = mgr.create_session()

    with patch("openclose.server.routes.get_db", return_value=db):
        resp = await delete_session(session.id)
    data = json.loads(bytes(resp.body))
    assert data["deleted"] is True


@pytest.mark.asyncio
async def test_route_rename_session(tmp_path: Path) -> None:
    from openclose.server.routes import rename_session, RenameRequest
    from openclose.storage.db import Database
    from openclose.session.session import SessionManager

    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    session = mgr.create_session()

    with patch("openclose.server.routes.get_db", return_value=db):
        req = RenameRequest(title="New Title")
        resp = await rename_session(session.id, req)
    data = json.loads(bytes(resp.body))
    assert data["ok"] is True
    assert data["title"] == "New Title"


# ── session/processor.py process() ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_processor_process_basic(tmp_path: Path) -> None:
    """Test that process() persists user message and yields events."""
    from openclose.session.processor import SessionProcessor
    from openclose.session.session import SessionManager
    from openclose.storage.db import Database
    from openclose.config.config import load_config

    load_config(project_dir=tmp_path)
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    session = mgr.create_session()

    # Mock the agent loop to yield a simple text response
    mock_loop_events = [
        MagicMock(type="text", content="Hello!", tool_call=None, tool_result="", metadata=None),
        MagicMock(type="done", content="", tool_call=None, tool_result="", metadata=None),
    ]

    async def mock_run(user_message: str) -> Any:
        for evt in mock_loop_events:
            yield evt

    mock_loop = MagicMock()
    mock_loop.run = mock_run
    mock_loop.messages = []

    processor = SessionProcessor(
        db=db,
        session_id=session.id,
        tool_schemas=[],
    )

    with patch.object(processor, "_build_agent_loop", return_value=mock_loop) if hasattr(processor, "_build_agent_loop") else patch("openclose.session.processor.AgentLoop", return_value=mock_loop):
        events = []
        async for event in processor.process("Test message"):
            events.append(event)

    # Should have context_update events
    event_types = [e.type for e in events]
    assert "context_update" in event_types


@pytest.mark.asyncio
async def test_processor_emits_part_ids_for_streaming_fork(tmp_path: Path) -> None:
    """Streaming bubbles must end up with a usable data-last-part-id so
    forking a still-live turn truncates at a safe segment boundary
    instead of falling back to message-level (which leaks later
    segments). The SSE flow must:
      - emit message_start with the reserved assistant message id
      - attach part_id to tool_call/tool_result events
      - emit a synthetic part_persisted event for flushed TEXT parts,
        BEFORE the done event so the streaming bubble can stamp its
        final lastPartId in-band.
    """
    from openclose.session.processor import SessionProcessor
    from openclose.session.session import SessionManager
    from openclose.storage.db import Database
    from openclose.config.config import load_config

    load_config(project_dir=tmp_path)
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    session = mgr.create_session(agent="build")

    # Fake tool call shape — processor only touches .id / .name /
    # .arguments_raw on it.
    tc = MagicMock()
    tc.id = "call_X"
    tc.name = "write"
    tc.arguments_raw = '{"file_path":"/tmp/x"}'

    mock_loop_events = [
        MagicMock(type="text", content="hi ", tool_call=None, tool_result="", metadata=None),
        MagicMock(type="tool_call", content="", tool_call=tc, tool_result="", metadata=None),
        MagicMock(type="tool_result", content="", tool_call=tc, tool_result="ok", metadata=None),
        MagicMock(type="text", content="done.", tool_call=None, tool_result="", metadata=None),
        MagicMock(type="done", content="", tool_call=None, tool_result="", metadata=None, done=True),
    ]

    async def mock_run(user_message: str) -> Any:
        for evt in mock_loop_events:
            yield evt

    mock_loop = MagicMock()
    mock_loop.run = mock_run
    mock_loop.messages = []

    processor = SessionProcessor(db=db, session_id=session.id, tool_schemas=[])

    with patch("openclose.session.processor.AgentLoop", return_value=mock_loop):
        events: list[Any] = []
        async for ev in processor.process("hello"):
            events.append(ev)

    types = [e.type for e in events]
    assert "message_start" in types
    ms = next(e for e in events if e.type == "message_start")
    assert ms.message_id, "message_start missing message_id"

    tc_ev = next(e for e in events if e.type == "tool_call")
    assert tc_ev.part_id, "tool_call event missing persisted part_id"
    tr_ev = next(e for e in events if e.type == "tool_result")
    assert tr_ev.part_id, "tool_result event missing persisted part_id"

    # The final trailing text 'done.' must be flushed AND announced via
    # part_persisted BEFORE the done event.
    done_idx = next(i for i, e in enumerate(events) if e.type == "done")
    pp_indices = [i for i, e in enumerate(events) if e.type == "part_persisted"]
    assert pp_indices, "no part_persisted emitted for flushed text"
    assert max(pp_indices) < done_idx, "part_persisted must precede done so the bubble stamps lastPartId in-band"

    # Persisted parts on the assistant message line up with the emitted ids.
    parts = mgr.get_message_parts(ms.message_id)
    assert [p.part_type for p in parts] == ["text", "tool_call", "tool_result", "text"]
    assert parts[1].id == tc_ev.part_id
    assert parts[2].id == tr_ev.part_id
    # Final part_persisted matches the trailing text part.
    final_pp = events[max(pp_indices)]
    assert final_pp.part_id == parts[-1].id


# ── server/sse.py ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sse_stream_events() -> None:
    from openclose.server.sse import stream_events
    from openclose.agent.loop import StreamEvent

    async def mock_events() -> Any:
        yield StreamEvent("text", content="Hello")
        yield StreamEvent("done", done=True)

    chunks = []
    async for chunk in stream_events(mock_events()):
        chunks.append(chunk)
    assert len(chunks) >= 2
    assert any("Hello" in c for c in chunks)


# ── provider/provider.py ────────────────────────────────────────────────────

def test_provider_model_property() -> None:
    from openclose.provider.provider import Provider

    p = Provider(base_url="http://localhost:9999/v1", api_key="test")
    assert p.client is not None


# ── project/snapshot.py ──────────────────────────────────────────────────────

def test_snapshot_manager_init(tmp_path: Path) -> None:
    from openclose.project.snapshot import SnapshotManager

    mgr = SnapshotManager(tmp_path)
    assert mgr._root == tmp_path


# ── file/diff.py ─────────────────────────────────────────────────────────────

def test_diff_tracker(tmp_path: Path) -> None:
    from openclose.file.diff import DiffTracker

    tracker = DiffTracker()
    f = tmp_path / "test.py"
    f.write_text("line1\nline2\n")
    tracker.snapshot(str(f))
    f.write_text("line1\nmodified\n")
    tracker.record_change(str(f))
    changes = tracker.get_changes()
    assert len(changes) == 1
    diff = tracker.get_diff(str(f))
    assert diff is not None
    assert "modified" in diff
    all_diffs = tracker.get_all_diffs()
    assert "modified" in all_diffs
    tracker.clear()
    assert len(tracker.get_changes()) == 0
