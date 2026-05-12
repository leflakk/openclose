"""Delegate sub-agent tool — runs N focused missions concurrently.

The parent passes 1–3 independent missions in ``mission_1``, ``mission_2``,
``mission_3``; the tool spawns one read-only sub-agent per provided slot,
runs them in parallel, and returns a single combined report. Each
sub-agent receives the same shared ``budget`` (per-mission tool-call cap
and report-depth prompt) and frames its mission as a free-form
exploration: state a precise question, trace, call-chain, or angle, and
the sub-agent answers exactly that, shaping the report to fit the
question.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from openclose.tool.registry import ToolRegistry
from openclose.tool.tool import Tool, ToolResult, ToolParameter
from openclose.tool.truncation import truncate_output
from openclose.log import get_logger

log = get_logger(__name__)

_ALLOWED_SUB_TOOLS = {"read", "glob", "grep", "bash", "webfetch"}

# Per-step recording caps (size control on the metadata returned to the parent,
# not the sub-agent's tool budget — that is set per-budget-level, see
# _BUDGET_MAX_TOOL_CALLS).
_MAX_RECORDED_STEPS = 100
_MAX_TOOL_RESULT_CHARS = 2000
_MAX_TEXT_CHARS = 500

_SUBAGENT_PROMPT = """\
You are a read-only sub-agent. The main agent has handed you a free-form
exploration mission — a specific question, trace, call-chain, or angle to
investigate. Your job is to answer THAT question. Do NOT pivot into a
generic project map; map only what is needed to answer what was asked.

The parent is expected to have stated (a) the precise mission and (b) the
shape of answer it wants. If the mission is genuinely ambiguous, pick the
most plausible reading, answer that, and flag the ambiguity under Caveats —
do not guess widely or fan out into unrelated areas.

Soft grounding: claims must be grounded in files you have actually opened
in this session — not prior knowledge of similar projects. Every concrete
claim cites file:line. If something cannot be grounded, state it as an
assumption under Caveats rather than as a fact.

Workflow:
- Use grep/glob to locate, read to inspect, bash for read-only commands only.
- Do NOT modify files, install packages, or change system state.
- Stay tightly focused on the specific question. Stop as soon as it is
  answered defensibly; do not keep digging for unrelated observations.
- You MUST make at least one tool call before emitting your report.
  Reports submitted without any tool call are REJECTED outright — a
  text-only answer is treated as fabricated from prior knowledge of
  similar projects, not as a grounded answer about THIS repo.

Output:
- Wrap your final report between an opening `<report>` tag and a closing
  `</report>` tag. Text outside the tags is discarded as scratch thinking —
  feel free to think out loud BEFORE the opening tag. Do not emit the tags
  with no real content between them; the body must be your actual report.
- Inside the tags, structure the report to fit the question. There is no
  fixed skeleton — use whatever sections make the answer clearest (e.g. a
  trace, a table of call sites, a step-by-step walk-through). No preamble
  outside the report sections.
- End with a short Caveats block ONLY if there is anything material the
  parent should know you could not verify or that was ambiguous. Omit it
  otherwise."""

_BUDGET_PROMPTS: dict[str, str] = {
    "default": """\
Budget: default (30 tool calls).
Expected output: a focused answer to the mission. Findings (bugs, risks,
load-bearing details) are NOT required at this budget — only include them
if they fall out naturally from the work. Stop as soon as the question is
answered defensibly; do not push deeper looking for issues.""",

    "extended": """\
Budget: extended (50 tool calls).
Expected output: a focused answer to the mission, AND concrete findings
the investigation reveals — bugs, risks, load-bearing details the parent
should know. The mission answer is still the primary deliverable; findings
complement it, they do not replace it.""",
}

_VALID_BUDGETS = frozenset(_BUDGET_PROMPTS)

# Hard ceiling on the number of tool calls each sub-agent may make.
# Enforced via cancel_event in _run_subagent; the prompt does the shaping
# and the cap is the safety stop. Applied per-mission, not globally.
_BUDGET_MAX_TOOL_CALLS: dict[str, int] = {
    "default": 30,
    "extended": 50,
}

# Empty-response retries (3) and parallel tool calls per LLM iteration mean
# step count can briefly exceed tool-call count. This factor sets the agent
# loop's max_steps generously above the tool-call cap so step exhaustion is
# never the binding constraint — the cancel_event is.
_STEP_SAFETY_MULTIPLIER = 2

_DEFAULT_BUDGET = "default"

# Soft-budget reminder: at >=95% of the tool-call cap (rounded down), nudge the
# sub-agent to wrap up and emit its <report> while it still has a turn or two
# left before the hard cancel at 100%.
_BUDGET_REMINDER_PCT = 0.95
_BUDGET_REMINDER = (
    "Budget reminder: you have used at least 95% of your tool-call budget "
    "({used}/{cap}). STOP INVESTIGATING NOW AND EMIT YOUR FINAL REPORT "
    "wrapped between opening `<report>` and closing `</report>` tags. "
    "Inside the report, in addition to your "
    "usual sections, include a 'Stopping point' section that states exactly "
    "where in the mission you stopped — what you did cover, what you did NOT "
    "get to, and what the parent would need to look at next. Do not start "
    "new tool calls; the few remaining are reserved as a buffer."
)

# Single output cap applied to the COMBINED report across all missions.
# A greedy mission can crowd out siblings; the parent sees the truncation
# marker and can re-call with fewer or sharper missions.
_REPORT_MAX_LINES = 500
_REPORT_MAX_BYTES = 50_000

# Markers the sub-agent wraps its final report in. The trunk prompt instructs
# the model to put the structured report INSIDE these tags; anything outside
# is treated as scratch thinking and discarded. The fallback path (no markers
# found) returns a breadcrumb report so we never silently drop content.
_REPORT_OPEN = "<report>"
_REPORT_CLOSE = "</report>"
_REPORT_RE = re.compile(
    re.escape(_REPORT_OPEN) + r"(.*?)" + re.escape(_REPORT_CLOSE),
    re.DOTALL,
)


def _extract_report(text: str) -> str:
    """Pull the structured report out of the sub-agent's transcript.

    The trunk prompt requires the model to wrap its final report in
    <report>…</report>. We extract the LAST such block from the full
    transcript (across all turns) so a stray tool call after the report
    does not lose it. When the markers are missing entirely the text is
    treated as scratch thinking rather than a partial report — interim
    pre-tool-call text is reliably noise ("Let me try X") rather than
    usable content. Returns "" when no markers are found.
    """
    matches = _REPORT_RE.findall(text)
    if matches:
        return str(matches[-1]).strip()
    return ""


def _synthesise_fallback_report(
    subagent_steps: list[dict[str, Any]],
    budget: str,
    stop_reason: str = "",
) -> str:
    """Build a breadcrumb report from a sub-agent that produced no final text.

    Surfaces what the sub-agent did and WHY it stopped so the parent can
    either re-call delegate with a sharper mission or pick up where the
    sub-agent left off, instead of starting from zero with a bare error.
    """
    tool_calls: list[tuple[str, str]] = []
    for step in subagent_steps:
        if step["type"] == "tool_call":
            args = step.get("content", "")
            if isinstance(args, str) and len(args) > 200:
                args = args[:200] + "…"
            tool_calls.append((step.get("tool_name", "?"), args))

    if not tool_calls:
        return ""  # nothing to report — caller will surface the bare error

    headline = stop_reason.strip() or "ended without a final summary"
    lines = [
        f"[Fallback report — `{budget}` sub-agent stopped: {headline}.",
        f"Below are the {len(tool_calls)} tool calls it made before stopping. "
        "Use these as breadcrumbs; re-call `delegate` with a sharper mission "
        "if you need a structured report.]",
        "",
        "Tool calls:",
    ]
    for i, (name, args) in enumerate(tool_calls, start=1):
        lines.append(f"  {i}. {name}  {args}")
    return "\n".join(lines)


def make_delegate_tool(project_dir: str, registry: ToolRegistry) -> Tool:
    """Create the delegate tool.

    Parameters
    ----------
    project_dir:
        Working directory for the sub-agents.
    registry:
        The main tool registry — used to build a filtered read-only
        sub-registry for the delegated sub-agents.
    """

    async def _run_subagent(
        system_prompt: str,
        task_message: str,
        sub_registry: ToolRegistry,
        provider: Any,
        sink: asyncio.Queue[Any] | None,
        parent_tc_id: str,
        max_tool_calls: int,
        budget: str,
        label: str,
        mission_text: str,
    ) -> tuple[str, list[dict[str, Any]], str, int]:
        """Run a single sub-agent and return
        (transcript_text, steps, stop_reason, tool_call_count)."""
        from openclose.agent.agent import Agent, AgentMode
        from openclose.agent.loop import AgentLoop, StreamEvent
        from openclose.config.config import get_config

        # `delegate` is a tool, not a configurable agent. Build the
        # sub-agent inline: temperature comes from [temperatures].delegate,
        # model from the provider's default. Tool allowlist and traits are
        # locked here.
        config = get_config()
        model = ""
        for provider_cfg in config.providers:
            if provider_cfg.default_model:
                model = provider_cfg.default_model
                break

        agent = Agent(
            name="delegate",
            description="Read-only delegated sub-agent spawned by the delegate tool.",
            model=model,
            temperature=config.temperatures.delegate,
            mode=AgentMode.SUBAGENT,
            traits=["readonly"],
            denied_tools=[],
            max_steps=max_tool_calls * _STEP_SAFETY_MULTIPLIER + 10,
            system_prompt=system_prompt,
            allowed_tools=sorted(_ALLOWED_SUB_TOOLS),
        )

        cancel_event = asyncio.Event()

        loop = AgentLoop(
            agent=agent,
            provider=provider,
            tool_executor=sub_registry.execute,
            tool_schemas=sub_registry.get_schemas(),
            project_dir=project_dir,
            cancel_event=cancel_event,
        )

        all_text = ""  # full transcript of assistant text — searched for <report> tags at the end
        subagent_steps: list[dict[str, Any]] = []
        tool_call_count = 0
        stop_reason = ""
        reminder_sent = False
        reminder_threshold = int(max_tool_calls * _BUDGET_REMINDER_PCT)
        try:
            async for event in loop.run(task_message):
                if event.type == "text":
                    all_text += event.content
                    if (
                        subagent_steps
                        and subagent_steps[-1]["type"] == "text"
                    ):
                        subagent_steps[-1]["content"] += event.content
                    elif len(subagent_steps) < _MAX_RECORDED_STEPS:
                        subagent_steps.append({
                            "type": "text",
                            "content": event.content,
                            "subagent_label": label,
                        })
                    if sink is not None and parent_tc_id:
                        await sink.put(StreamEvent(
                            "subagent_text",
                            content=event.content,
                            parent_tool_call_id=parent_tc_id,
                            metadata={
                                "subagent_label": label,
                                "mission_text": mission_text,
                            },
                        ))
                elif event.type == "tool_call" and event.tool_call:
                    tool_call_count += 1
                    if len(subagent_steps) < _MAX_RECORDED_STEPS:
                        subagent_steps.append({
                            "type": "tool_call",
                            "tool_name": event.tool_call.name,
                            "tool_call_id": event.tool_call.id,
                            "content": event.tool_call.arguments_raw,
                            "subagent_label": label,
                        })
                    if sink is not None and parent_tc_id:
                        await sink.put(StreamEvent(
                            "subagent_tool_call",
                            tool_call=event.tool_call,
                            parent_tool_call_id=parent_tc_id,
                            metadata={
                                "subagent_label": label,
                                "mission_text": mission_text,
                            },
                        ))
                elif event.type == "tool_result" and event.tool_call:
                    content = event.tool_result
                    if len(content) > _MAX_TOOL_RESULT_CHARS:
                        content = content[:_MAX_TOOL_RESULT_CHARS] + "\n...(truncated)"
                    if len(subagent_steps) < _MAX_RECORDED_STEPS:
                        subagent_steps.append({
                            "type": "tool_result",
                            "tool_name": event.tool_call.name,
                            "tool_call_id": event.tool_call.id,
                            "content": content,
                            "subagent_label": label,
                        })
                    if sink is not None and parent_tc_id:
                        await sink.put(StreamEvent(
                            "subagent_tool_result",
                            tool_call=event.tool_call,
                            tool_result=content,
                            parent_tool_call_id=parent_tc_id,
                            metadata={
                                "subagent_label": label,
                                "mission_text": mission_text,
                            },
                        ))
                    # Soft reminder at >=95%: tell the sub-agent to stop
                    # investigating and emit its report (with a "Stopping
                    # point" section) while it still has a buffer turn
                    # before the hard cancel.
                    if (
                        not reminder_sent
                        and tool_call_count >= reminder_threshold
                        and tool_call_count < max_tool_calls
                    ):
                        loop.request_user_nudge(
                            _BUDGET_REMINDER.format(
                                used=tool_call_count, cap=max_tool_calls,
                            )
                        )
                        reminder_sent = True
                    # Enforce the tool-call budget AFTER the result is
                    # appended to the agent's messages. The cancel_event
                    # makes the loop exit cleanly with a "done" event at
                    # the start of the next iteration — no error, no
                    # half-state.
                    if (
                        tool_call_count >= max_tool_calls
                        and not cancel_event.is_set()
                    ):
                        stop_reason = (
                            f"Tool-call budget ({max_tool_calls}) reached"
                        )
                        cancel_event.set()
                elif event.type == "error":
                    log.error(
                        "Sub-agent error event (budget=%s, label=%s): %s",
                        budget, label, event.error,
                    )
                    stop_reason = event.error or "sub-agent error"
                    break
        except Exception as e:
            log.error(
                "Delegate sub-agent error (budget=%s, label=%s): %s",
                budget, label, e,
            )
            stop_reason = f"sub-agent exception: {e}"

        # Truncate text step content for size control
        for step in subagent_steps:
            if step["type"] == "text" and len(step["content"]) > _MAX_TEXT_CHARS:
                step["content"] = step["content"][:_MAX_TEXT_CHARS] + "...(truncated)"

        return all_text, subagent_steps, stop_reason, tool_call_count

    async def execute(
        mission_1: str | None = None,
        mission_2: str | None = None,
        mission_3: str | None = None,
        budget: str = _DEFAULT_BUDGET,
        **_kwargs: object,
    ) -> ToolResult:
        # `**_kwargs` absorbs any obsolete params (e.g. `mode`, or the old
        # array-shaped `missions`) the model may still pass — silently
        # ignored, no error.
        del _kwargs
        # mission_1 is required; mission_2/3 are optional. Each provided slot
        # must be a non-empty stripped string — empty entries are a parent
        # bug worth surfacing rather than silently filtering.
        if mission_1 is None:
            return ToolResult(
                error="mission_1 parameter is required (non-empty string)"
            )
        for name, value in (
            ("mission_1", mission_1),
            ("mission_2", mission_2),
            ("mission_3", mission_3),
        ):
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                return ToolResult(
                    error=f"{name} must be a non-empty string (got {value!r})"
                )

        missions: list[str] = [
            m for m in (mission_1, mission_2, mission_3) if m is not None
        ]

        # Empty/whitespace budget falls back to default. An unknown non-empty
        # value is a real error.
        budget = (budget or _DEFAULT_BUDGET).strip() or _DEFAULT_BUDGET
        if budget not in _VALID_BUDGETS:
            return ToolResult(
                error=(
                    f"budget must be one of {sorted(_VALID_BUDGETS)}, "
                    f"got {budget!r}"
                )
            )

        # Lazy imports to avoid circular dependency
        # (agent.loop -> tool.tools package -> delegate -> agent.loop)
        from openclose.agent.loop import (
            subagent_event_sink, current_tool_call_id,
        )
        from openclose.provider.provider import get_provider

        sink = subagent_event_sink.get(None)
        parent_tc_id = current_tool_call_id.get("")

        sub_registry = ToolRegistry()
        for tool in registry.list_tools():
            if tool.name in _ALLOWED_SUB_TOOLS:
                sub_registry.register(tool)

        if not sub_registry.list_tools():
            return ToolResult(error="No tools available for delegation")

        provider = get_provider()

        system_prompt = _SUBAGENT_PROMPT + "\n\n" + _BUDGET_PROMPTS[budget]
        max_tool_calls = _BUDGET_MAX_TOOL_CALLS[budget]
        total = len(missions)

        # Run all missions concurrently. No internal cap on parallelism —
        # the parent's prompt and context budget are the only throttle.
        # Each sub-agent has its own AgentLoop, cancel_event, and step log,
        # so parallel execution is safe.
        async def _run_mission(idx: int, mission: str) -> tuple[
            int, str, str, str, list[dict[str, Any]], str, int,
        ]:
            label = f"Mission {idx}/{total}"
            transcript, steps, stop_reason, tc_count = await _run_subagent(
                system_prompt,
                f"Mission: {mission}",
                sub_registry, provider, sink, parent_tc_id,
                max_tool_calls=max_tool_calls,
                budget=budget,
                label=label,
                mission_text=mission,
            )
            return idx, label, mission, transcript, steps, stop_reason, tc_count

        results = await asyncio.gather(
            *(
                _run_mission(i + 1, m)
                for i, m in enumerate(missions)
            )
        )

        # Per-mission output assembly: prefer real <report>, then fallback
        # breadcrumb, then a one-line stub.
        all_steps: list[dict[str, Any]] = []
        per_mission: list[dict[str, Any]] = []
        sections: list[str] = []
        all_fallback = True
        any_content = False
        total_tool_calls = 0

        for idx, label, mission_text, transcript, steps, stop_reason, tc_count in results:
            all_steps.extend(steps)
            total_tool_calls += tc_count

            # Hard rule: a sub-agent that made zero tool calls cannot have
            # grounded its report — anything in <report> is fabricated from
            # prior knowledge of similar projects, not facts about THIS
            # repo. Discard any "report" and reject the mission outright.
            if tc_count == 0:
                report_text = ""
                if not stop_reason:
                    stop_reason = (
                        "zero tool calls — report rejected as ungrounded"
                    )
            else:
                report_text = _extract_report(transcript)

            section_body: str
            mission_fallback = False
            if report_text.strip():
                section_body = report_text
                any_content = True
                all_fallback = False
            elif tc_count == 0:
                # Distinct from the breadcrumb path (which needs tool calls
                # to breadcrumb): emit a clear rejection notice the parent
                # can act on.
                section_body = (
                    "[Mission rejected: sub-agent made zero tool calls. A "
                    "read-only sub-agent must ground every claim in files "
                    "it opens during the session — text-only output is "
                    "treated as fabricated from prior knowledge and not "
                    "trusted. Re-call delegate with a sharper mission "
                    "that names target files/symbols, or split the "
                    "question into smaller missions.]"
                )
                mission_fallback = True
                any_content = True
            else:
                fallback = _synthesise_fallback_report(
                    steps, budget, stop_reason=stop_reason,
                )
                if fallback:
                    section_body = fallback
                    mission_fallback = True
                    any_content = True
                else:
                    err_line = "Mission produced no output"
                    if stop_reason:
                        err_line = f"{err_line} ({stop_reason})"
                    section_body = f"[{err_line}]"
                    mission_fallback = True

            sections.append(f"=== {label} ===\n{section_body}")
            per_mission.append({
                "index": idx,
                "label": label,
                "mission_text": mission_text,
                "stop_reason": stop_reason,
                "tool_call_count": tc_count,
                "fallback": mission_fallback,
            })

        if not any_content:
            reasons = "; ".join(
                f"{p['label']}: {p['stop_reason']}" if p["stop_reason"]
                else p["label"]
                for p in per_mission
            )
            return ToolResult(
                error=f"Delegation produced no output ({reasons})"
            )

        combined = "\n\n".join(sections)
        truncated = truncate_output(
            combined,
            max_lines=_REPORT_MAX_LINES,
            max_bytes=_REPORT_MAX_BYTES,
        )

        metadata: dict[str, Any] = {
            "subagent_steps": all_steps,
            "tool_call_count": total_tool_calls,
            "per_mission": per_mission,
        }
        if all_fallback:
            metadata["fallback"] = True

        return ToolResult(output=truncated, metadata=metadata)

    return Tool(
        name="delegate",
        description=(
            "USE IT FOR FOCUSED INVESTIGATIONS that would otherwise "
            "consume parent context — mapping a subsystem, tracing a call "
            "chain, surveying call sites, answering structured questions "
            "about the codebase. "
            "The tool spawns a sub-agent per `mission_N` parameter (1-3 missions), "
            "runs them in parallel, returns one combined report. "
            "For investigations that decompose into more than "
            "3 angles, pick the 3 highest-value ones for this call and "
            "queue the rest for a follow-up delegate call after you read "
            "the first batch."
        ),
        parameters=[
            ToolParameter(
                name="mission_1",
                type="string",
                description=(
                    "First investigation mission. State the goal in plain "
                    "language, include scoping/constraints inline (target "
                    "files, directories, what to exclude, shape of answer "
                    "wanted). Vague missions produce vague reports."
                ),
            ),
            ToolParameter(
                name="mission_2",
                type="string",
                required=False,
                description=(
                    "Second independent mission (runs concurrently with "
                    "the first). Omit if one mission suffices."
                ),
            ),
            ToolParameter(
                name="mission_3",
                type="string",
                required=False,
                description=(
                    "Third independent mission. Pick the 3 highest-value "
                    "angles; queue the rest for a follow-up call."
                ),
            ),
            ToolParameter(
                name="budget",
                description=(
                    "Per-mission tool-call budget and expected report "
                    "depth. Applies identically to every provided "
                    "`mission_N`. `default` (30 tool calls): focused "
                    "answer to the mission; findings optional. `extended` "
                    "(50 tool calls): focused answer + concrete findings "
                    "(bugs, risks, load-bearing details) the "
                    "investigation surfaces."
                ),
                enum=["default", "extended"],
                required=False,
                default=_DEFAULT_BUDGET,
            ),
        ],
        execute_fn=execute,
    )
