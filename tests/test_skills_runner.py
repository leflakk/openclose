"""Tests for skills.runner — variable resolution, agent/engine construction, event serialization."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openclose.agent.loop import StreamEvent, ToolCall
from openclose.config.config import load_config
from openclose.config.paths import ConfigPaths
from openclose.permission.schema import PermissionRequest
from openclose.skills.runner import (
    _build_agent,
    _build_permission_engine,
    _resolve_variables,
    _serialize_event,
    execute_skill_to_files,
    start_run,
)
from openclose.skills.schema import Parameter, RequiredTool, Skill


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(
        ConfigPaths, "project_runtime_dir",
        classmethod(lambda cls, project_dir: tmp_path),
    )
    load_config(project_dir=tmp_path)
    yield tmp_path


def _skill(
    slug: str = "s1",
    params: list[Parameter] | None = None,
    tools: list[RequiredTool] | None = None,
) -> Skill:
    return Skill(
        name=f"skill-{slug}",
        slug=slug,
        version=1,
        parameters=params or [],
        required_tools=tools or [RequiredTool(name="read", sensitive=False)],
        goal="Do something",
        procedure="Step 1\nStep 2",
    )


# ───────────────────────── _resolve_variables ──────────────────────

def test_resolve_variables_defaults_only() -> None:
    skill = _skill(params=[
        Parameter(name="a", default="x"),
        Parameter(name="b", default="y"),
    ])
    assert _resolve_variables(skill, {}) == {"a": "x", "b": "y"}


def test_resolve_variables_overrides_defaults() -> None:
    skill = _skill(params=[
        Parameter(name="a", default="x"),
    ])
    out = _resolve_variables(skill, {"a": "override"})
    assert out == {"a": "override"}


def test_resolve_variables_passes_through_extra_inputs() -> None:
    skill = _skill(params=[Parameter(name="a", default="x")])
    out = _resolve_variables(skill, {"a": "1", "unknown": "z"})
    assert out == {"a": "1", "unknown": "z"}


# ───────────────────────── _build_agent ────────────────────────────

def test_build_agent_uses_skill_tools(runtime: Path) -> None:
    skill = _skill(tools=[
        RequiredTool(name="read", sensitive=False),
        RequiredTool(name="bash", sensitive=True),
    ])
    agent = _build_agent(skill, {})
    assert agent.allowed_tools == ["read", "bash"]
    assert agent.denied_tools == []
    assert agent.name == f"skill-{skill.slug}"


def test_build_agent_substitutes_variables(runtime: Path) -> None:
    skill = _skill(
        params=[Parameter(name="url", default="http://default")],
    )
    skill.procedure = "Visit $url"
    agent = _build_agent(skill, {"url": "http://custom"})
    assert "http://custom" in agent.system_prompt
    assert "Visit $url" not in agent.system_prompt


def test_build_agent_uses_first_provider_default_model(runtime: Path) -> None:
    fake_cfg = MagicMock()
    p1 = MagicMock()
    p1.default_model = ""
    p2 = MagicMock()
    p2.default_model = "real-model"
    fake_cfg.providers = [p1, p2]

    with patch("openclose.skills.runner.get_config", return_value=fake_cfg):
        agent = _build_agent(_skill(), {})

    assert agent.model == "real-model"


# ───────────────────────── _build_permission_engine ───────────────

def test_build_permission_engine_allows_required_tools(runtime: Path) -> None:
    skill = _skill(tools=[
        RequiredTool(name="read", sensitive=False),
        RequiredTool(name="bash", sensitive=True),
    ])
    engine = _build_permission_engine(skill)
    resp = engine.check(PermissionRequest(tool_name="read", path="/any/path"))
    assert resp.allowed is True
    resp2 = engine.check(PermissionRequest(tool_name="bash"))
    assert resp2.allowed is True


# ───────────────────────── _serialize_event ───────────────────────

def test_serialize_event_text_only() -> None:
    data = _serialize_event(StreamEvent(event_type="text", content="hi"))
    assert data["type"] == "text"
    assert data["content"] == "hi"
    assert "tool_call" not in data


def test_serialize_event_with_tool_call() -> None:
    tc = ToolCall()
    tc.id = "t1"
    tc.name = "bash"
    tc.append_arguments('{"cmd":"ls"}')
    data = _serialize_event(StreamEvent(event_type="tool_call", tool_call=tc))
    assert data["tool_call"]["id"] == "t1"
    assert data["tool_call"]["name"] == "bash"
    assert data["tool_call"]["arguments"] == '{"cmd":"ls"}'


def test_serialize_event_with_tool_result() -> None:
    data = _serialize_event(StreamEvent(event_type="tool_result", tool_result="ok"))
    assert data["tool_result"] == "ok"


def test_serialize_event_with_error() -> None:
    data = _serialize_event(StreamEvent(event_type="error", error="boom"))
    assert data["error"] == "boom"


def test_serialize_event_with_metadata() -> None:
    data = _serialize_event(StreamEvent(event_type="text", metadata={"k": "v"}))
    assert data["metadata"] == {"k": "v"}


def test_serialize_event_unserializable_metadata_flagged() -> None:
    # A set is not JSON-serializable.
    data = _serialize_event(StreamEvent(event_type="text", metadata={"k": {1, 2}}))
    assert data["metadata"] == {"_unserializable": True}


# ───────────────────────── execute_skill_to_files ─────────────────

@pytest.mark.asyncio
async def test_execute_skill_writes_artifacts_and_returns_summary(
    runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = _skill()
    jsonl = runtime / "log.jsonl"
    out = runtime / "out.md"

    # Stream: one text chunk → then loop ends normally.
    async def fake_events() -> Any:
        evt = MagicMock()
        evt.type = "text"
        evt.content = "all done"
        evt.tool_call = None
        evt.tool_result = ""
        evt.error = ""
        evt.metadata = {}
        yield evt

    fake_loop = MagicMock()
    fake_loop.run = MagicMock(return_value=fake_events())

    with patch("openclose.skills.runner.AgentLoop", return_value=fake_loop), \
         patch("openclose.skills.runner.get_provider"), \
         patch("openclose.skills.runner.register_all_tools"):
        summary = await execute_skill_to_files(skill, jsonl, out)

    assert summary["status"] == "done"
    assert "all done" in summary["final_text"]
    assert out.read_text() == "all done\n"
    # run_start + text + run_end → 3 lines
    lines = jsonl.read_text().strip().splitlines()
    assert len(lines) == 3


@pytest.mark.asyncio
async def test_execute_skill_error_event_recorded(
    runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = _skill()
    jsonl = runtime / "log.jsonl"
    out = runtime / "out.md"

    async def fake_events() -> Any:
        evt = MagicMock()
        evt.type = "error"
        evt.content = ""
        evt.tool_call = None
        evt.tool_result = ""
        evt.error = "something broke"
        evt.metadata = {}
        yield evt

    fake_loop = MagicMock()
    fake_loop.run = MagicMock(return_value=fake_events())

    with patch("openclose.skills.runner.AgentLoop", return_value=fake_loop), \
         patch("openclose.skills.runner.get_provider"), \
         patch("openclose.skills.runner.register_all_tools"):
        summary = await execute_skill_to_files(skill, jsonl, out)

    assert summary["status"] == "error"
    assert summary["error"] == "something broke"
    assert "Error: something broke" in out.read_text()


@pytest.mark.asyncio
async def test_execute_skill_crashed_loop_records_exception(
    runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = _skill()
    jsonl = runtime / "log.jsonl"
    out = runtime / "out.md"

    async def fake_events() -> Any:
        raise RuntimeError("loop kaput")
        yield  # pragma: no cover

    fake_loop = MagicMock()
    fake_loop.run = MagicMock(return_value=fake_events())

    with patch("openclose.skills.runner.AgentLoop", return_value=fake_loop), \
         patch("openclose.skills.runner.get_provider"), \
         patch("openclose.skills.runner.register_all_tools"):
        summary = await execute_skill_to_files(skill, jsonl, out)

    assert summary["status"] == "error"
    assert "loop kaput" in summary["error"]


# ───────────────────────── start_run ──────────────────────────────

@pytest.mark.asyncio
async def test_start_run_missing_skill_raises(runtime: Path) -> None:
    with pytest.raises(ValueError, match="Skill not found"):
        await start_run("ghost")


@pytest.mark.asyncio
async def test_start_run_returns_metadata(runtime: Path) -> None:
    from openclose.skills.storage import write_skill
    write_skill(_skill("probe"))

    # Stub out the background task so it doesn't actually execute.
    with patch(
        "openclose.skills.runner.execute_skill_to_files",
        new=AsyncMock(return_value={}),
    ):
        out = await start_run("probe", inputs={"x": "y"})

    assert out["status"] == "running"
    assert out["file"].endswith(".jsonl")
    assert out["run_id"]
