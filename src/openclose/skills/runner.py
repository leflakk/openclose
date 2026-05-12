"""Headless skill execution — fresh AgentLoop, pre-granted permissions, file-based logs.

A run writes two files per invocation:
- `<skill>/<iso>-<run_id>.jsonl` — one JSON event per line
- `<skill>/<iso>-<run_id>.out.md` — the final assistant text (or error)

Phase 2's cron trigger calls this exact same path.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openclose.agent.agent import Agent, AgentMode
from openclose.agent.loop import AgentLoop, StreamEvent
from openclose.config.agents import render_prompt_template
from openclose.config.config import get_config
from openclose.id import generate_id
from openclose.log import get_logger
from openclose.permission.permission import PermissionEngine
from openclose.permission.rules import PermissionAction, PermissionRule
from openclose.provider.provider import get_provider
from openclose.skills.schema import Skill
from openclose.skills.storage import read_skill, skills_dir
from openclose.tool.registry import ToolRegistry
from openclose.tool.tools import register_all_tools

log = get_logger(__name__)


_DEFAULT_TRIGGER = (
    "Execute the Procedure above exactly. Use only the tools listed in "
    "Required tools. Do not ask me questions — run unattended. When "
    "finished, write a short plain-text summary of what you did and the "
    "outcome; that will be captured as the run's output."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _resolve_variables(skill: Skill, inputs: dict[str, str]) -> dict[str, str]:
    """Merge per-run inputs over parameter defaults."""
    variables: dict[str, str] = {}
    for p in skill.parameters:
        variables[p.name] = p.default
    for k, v in inputs.items():
        variables[k] = v
    return variables


def _build_agent(skill: Skill, variables: dict[str, str]) -> Agent:
    """Synthesize a one-off Agent instance for this skill.

    The agent's allowed_tools is exactly the skill's required_tools.
    The system_prompt is the full skill body with $params substituted.
    """
    config = get_config()
    model = ""
    for provider in config.providers:
        if provider.default_model:
            model = provider.default_model
            break

    body = render_prompt_template(skill.body(), variables)
    header = (
        f"You are the skill `{skill.slug}`. Goal: {skill.goal.strip()}\n\n"
        "Follow the procedure below exactly. You are running headlessly: "
        "there is no user to ask. If something is ambiguous or blocking, "
        "end the run with a clear failure explanation.\n\n"
    )

    return Agent(
        name=f"skill-{skill.slug}",
        description=f"Headless runner for skill {skill.slug}",
        model=model,
        temperature=config.temperatures.skills_runner,
        max_steps=50,
        system_prompt=header + body,
        mode=AgentMode.PRIMARY,
        allowed_tools=[t.name for t in skill.required_tools],
        denied_tools=[],
    )


def _build_permission_engine(skill: Skill) -> PermissionEngine:
    """Fresh engine with ALLOW rules for every required tool (any path)."""
    engine = PermissionEngine.from_config()
    for t in skill.required_tools:
        engine.add_rule(PermissionRule(
            tool=t.name,
            path="*",
            action=PermissionAction.ALLOW,
        ))
    return engine


def _serialize_event(event: StreamEvent) -> dict[str, Any]:
    """Project a StreamEvent onto a JSON-safe dict for the log."""
    data: dict[str, Any] = {
        "type": event.type,
        "timestamp": _now_iso(),
    }
    if event.content:
        data["content"] = event.content
    if event.tool_call is not None:
        data["tool_call"] = {
            "id": event.tool_call.id,
            "name": event.tool_call.name,
            "arguments": event.tool_call.arguments_raw,
        }
    if event.tool_result:
        data["tool_result"] = event.tool_result
    if event.error:
        data["error"] = event.error
    if event.metadata:
        try:
            json.dumps(event.metadata)
            data["metadata"] = event.metadata
        except (TypeError, ValueError):
            data["metadata"] = {"_unserializable": True}
    return data


async def _run_loop_to_files(
    skill: Skill,
    agent: Agent,
    engine: PermissionEngine,
    registry: ToolRegistry,
    trigger: str,
    jsonl_path: Path,
    out_path: Path,
) -> dict[str, Any]:
    """Drive AgentLoop to completion, streaming events to jsonl; write output.md at end.

    Returns a summary dict:
        {status, final_text, error, started_at, finished_at, output_preview}
    """
    provider = get_provider()
    loop = AgentLoop(
        agent=agent,
        provider=provider,
        tool_executor=registry.execute,
        tool_schemas=registry.get_schemas(),
        project_dir=get_config().project_dir,
        permission_engine=engine,
        permission_broker=None,
        plan_broker=None,
        ask_user_broker=None,
        session_id=f"skill-run-{jsonl_path.stem}",
    )

    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    final_text = ""
    status = "done"
    error_reason = ""
    started_at = _now_iso()

    with jsonl_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps({
            "type": "run_start",
            "timestamp": started_at,
            "slug": skill.slug,
            "skill_name": skill.name,
        }) + "\n")
        f.flush()

        try:
            async for event in loop.run(trigger):
                try:
                    f.write(json.dumps(_serialize_event(event)) + "\n")
                    f.flush()
                except (TypeError, ValueError) as e:
                    log.warning("Failed to serialize event: %s", e)
                if event.type == "text":
                    final_text += event.content
                if event.type == "error":
                    status = "error"
                    error_reason = event.error or ""
        except Exception as e:  # noqa: BLE001
            status = "error"
            error_reason = str(e)
            f.write(json.dumps({
                "type": "exception",
                "timestamp": _now_iso(),
                "error": str(e),
            }) + "\n")
            log.exception("Skill %s run crashed", skill.slug)
        finally:
            finished_at = _now_iso()
            f.write(json.dumps({
                "type": "run_end",
                "timestamp": finished_at,
                "status": status,
                "error": error_reason,
            }) + "\n")

    body = final_text.strip() or "(no output)"
    if error_reason:
        body += f"\n\n---\nError: {error_reason}"
    out_path.write_text(body + "\n", encoding="utf-8")

    preview = final_text.strip().splitlines()[0][:200] if final_text.strip() else ""
    return {
        "status": status,
        "final_text": final_text,
        "error": error_reason,
        "started_at": started_at,
        "finished_at": finished_at,
        "output_preview": preview,
    }


async def execute_skill_to_files(
    skill: Skill,
    jsonl_path: Path,
    out_path: Path,
    *,
    inputs: dict[str, str] | None = None,
    trigger_message: str = "",
) -> dict[str, Any]:
    """Awaitable: run `skill` synchronously and write its artifacts to the given paths.

    Used by the job runner to chain skills and by `start_run` (wrapped in
    an `asyncio.create_task`) for manual one-off runs.
    """
    variables = _resolve_variables(skill, inputs or {})
    agent = _build_agent(skill, variables)
    engine = _build_permission_engine(skill)
    registry = ToolRegistry()
    register_all_tools(registry, get_config().project_dir)
    trigger = (trigger_message or "").strip() or _DEFAULT_TRIGGER
    return await _run_loop_to_files(
        skill=skill,
        agent=agent,
        engine=engine,
        registry=registry,
        trigger=trigger,
        jsonl_path=jsonl_path,
        out_path=out_path,
    )


async def start_run(
    slug: str,
    inputs: dict[str, str] | None = None,
    trigger_message: str = "",
) -> dict[str, Any]:
    """Kick off a manual skill run in the background. Returns `{run_id, ...}`.

    The background task keeps running after this function returns; use
    `list_runs(slug)` to poll status.
    """
    skill = read_skill(slug)
    if skill is None:
        raise ValueError(f"Skill not found: {slug}")

    run_id = generate_id()
    ts = _now_iso().replace(":", "-")
    base = f"{ts}-{run_id}"
    run_dir = skills_dir() / slug
    run_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = run_dir / f"{base}.jsonl"
    out_path = run_dir / f"{base}.out.md"

    asyncio.create_task(execute_skill_to_files(
        skill,
        jsonl_path,
        out_path,
        inputs=inputs,
        trigger_message=trigger_message,
    ))

    return {
        "run_id": run_id,
        "file": jsonl_path.name,
        "started_at": _now_iso(),
        "status": "running",
    }
