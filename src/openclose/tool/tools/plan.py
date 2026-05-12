"""Plan tool — two-phase plan workflow.

The plan tool has two phases controlled by the required ``phase``
parameter:

* ``phase="draft"`` — spawns a read-only reviewer sub-agent that
  re-reads relevant code, critiques the plan, and returns concrete
  feedback so the agent can iterate before showing anything to the user.
  The reviewer is built on the same machinery as ``delegate``: filtered
  read-only tool sub-registry, ``AgentMode.SUBAGENT`` agent, cancel-event
  budget enforcement, soft 95% reminder, ``<report>...</report>``
  extraction, zero-tool-call rejection, fallback breadcrumb on no
  output. Its sampling temperature is set via
  ``[temperatures] plan_reviewer`` in config.

* ``phase="final"`` — returns the existing ``awaiting_plan_review``
  marker. The agent loop intercepts this case before tool execution,
  runs ``plan_broker.ask()`` to suspend on the user's response, writes
  ``plan.md`` on accept, and switches the session to the build agent.

The two phases are intentionally split: review is a tool-internal
read-only investigation, while final review is a UI-facing user
interaction handled by the loop.
"""

from __future__ import annotations

import asyncio
from typing import Any

from openclose.tool.registry import ToolRegistry
from openclose.tool.tool import Tool, ToolResult, ToolParameter
from openclose.tool.truncation import truncate_output
from openclose.tool.tools.delegate import (
    _BUDGET_REMINDER,
    _extract_report,
    _synthesise_fallback_report,
)
from openclose.log import get_logger

log = get_logger(__name__)

_ALLOWED_SUB_TOOLS = {"read", "glob", "grep", "bash", "webfetch"}

# Per-step recording caps (size control on the metadata returned to the parent,
# not the sub-agent's tool budget — that is set per _REVIEWER_MAX_TOOL_CALLS).
_MAX_RECORDED_STEPS = 100
_MAX_TOOL_RESULT_CHARS = 2000
_MAX_TEXT_CHARS = 500

_REVIEWER_PROMPT = """\
You are a read-only plan reviewer sub-agent. The proposer agent has
handed you a DRAFT implementation plan. Your job is to critique it:
- Does it fully solve the user's stated problem?
- Are there some incomplete data?
- What is missing?
Surface gaps and propose concrete edits the proposer can apply on its next iteration.
Do NOT rewrite the plan yourself.

The plan content is in your task message. The user's goal can be
inferred from the plan itself; if it is not stated clearly, flag the
ambiguity in Caveats and review against the most plausible reading.

Soft grounding: every criticism must be grounded in files you have
actually opened in this session — not prior knowledge of similar
projects. Spot-check the plan's claims against real code. If it
references files, symbols, or line numbers, open them and verify. Cite
file:line for every concrete claim. Anything that cannot be grounded
goes under Caveats as an assumption, not in Issues as a fact.

Be critical, not nice. Vague concerns ("consider X") are useless;
concrete edits ("delete step 3 — foo.py:42 already does this") are
useful. A correct plan gets a brief APPROVE; a plan with gaps gets the
gaps named with evidence.

Workflow:
- Use grep/glob to locate, read to inspect, bash for read-only commands only.
- Do NOT modify files, install packages, or change system state.
- Stay tightly focused on whether THIS draft plan, executed as written,
  solves the stated problem. Do not pivot into a generic project audit.
- You MUST make at least one tool call before emitting your report.
  Reports submitted without any tool call are REJECTED outright — a
  text-only review is treated as fabricated from prior knowledge of
  similar projects, not a grounded review of THIS plan.

Output:
- Wrap your final report between an opening `<report>` tag and a
  closing `</report>` tag. Text outside the tags is discarded as scratch
  thinking — feel free to think out loud BEFORE the opening tag.
- Inside the tags, structure the report with these sections:
  - **Verdict**: one of `APPROVE`, `APPROVE WITH MINOR EDITS`,
    `MAJOR ISSUES`, `MISALIGNED WITH GOAL` — plus one sentence saying why.
  - **Issues**: numbered list of concrete problems (gaps, wrong file
    paths, missed edge cases, contradictions). Each issue cites
    file:line evidence.
  - **Concrete edits**: numbered list of directive changes the proposer
    should apply ("Replace step 4 with …", "Add a step after step 2
    to …", "Remove step 7 — already done at foo.py:42"). Skip if
    the verdict is APPROVE.
  - **Verified**: a short list of the files/symbols you actually opened
    to ground the review.
  - **Caveats**: only if there is something material that was ambiguous
    or could not be verified. Omit otherwise.

The proposer will ITERATE the plan based on your feedback — modifying
the plan content and re-calling `plan` with `phase="draft"`, or moving
to `phase="final"` once you APPROVE. They will not resubmit the same
plan verbatim, and you should not expect them to."""

# Hard ceiling on the number of tool calls the reviewer may make. Plan
# reviews are a fixed-shape job (read plan, spot-check 2-5 claims,
# return critique), so a single hardcoded cap matches the default
# delegate budget — no `budget` parameter is exposed.
_REVIEWER_MAX_TOOL_CALLS = 30

# Empty-response retries (3) and parallel tool calls per LLM iteration mean
# step count can briefly exceed tool-call count. This factor sets the agent
# loop's max_steps generously above the tool-call cap so step exhaustion is
# never the binding constraint — the cancel_event is.
_STEP_SAFETY_MULTIPLIER = 2

# Soft-budget reminder: at >=95% of the tool-call cap, nudge the
# reviewer to wrap up and emit its <report> while it still has a turn
# or two left before the hard cancel at 100%. Reuses delegate's
# _BUDGET_REMINDER format string.
_REVIEWER_BUDGET_REMINDER_PCT = 0.95

_REPORT_MAX_LINES = 500
_REPORT_MAX_BYTES = 50_000

_SUBAGENT_LABEL = "Plan reviewer"
_MISSION_TEXT = "Reviewing draft plan"


def make_plan_tool(project_dir: str, registry: ToolRegistry) -> Tool:
    """Create the plan tool.

    Parameters
    ----------
    project_dir:
        Working directory for the reviewer sub-agent (used by its
        read-only tools).
    registry:
        The main tool registry — used to build a filtered read-only
        sub-registry for the reviewer sub-agent in ``phase="draft"``.

    Notes
    -----
    The actual user interaction for ``phase="final"`` is handled by the
    agent loop, which detects the ``awaiting_plan_review`` metadata and
    delegates to the ``PlanBroker``. The tool itself just validates and
    returns the marker so the loop knows to suspend. For ``phase="draft"``
    the loop falls through to normal tool execution and the reviewer
    runs here.
    """

    async def _run_reviewer_subagent(
        system_prompt: str,
        task_message: str,
        sub_registry: ToolRegistry,
        provider: Any,
        sink: asyncio.Queue[Any] | None,
        parent_tc_id: str,
        max_tool_calls: int,
    ) -> tuple[str, list[dict[str, Any]], str, int]:
        """Run the reviewer sub-agent and return
        (transcript_text, steps, stop_reason, tool_call_count)."""
        from openclose.agent.agent import Agent, AgentMode
        from openclose.agent.loop import AgentLoop, StreamEvent
        from openclose.config.config import get_config

        # `plan` is a tool, not a configurable agent. Build the
        # sub-agent inline: temperature comes from
        # [temperatures].plan_reviewer, model from the provider's
        # default. Tool allowlist and traits are locked here.
        config = get_config()
        model = ""
        for provider_cfg in config.providers:
            if provider_cfg.default_model:
                model = provider_cfg.default_model
                break

        agent = Agent(
            name="plan_reviewer",
            description="Read-only reviewer sub-agent spawned by the plan tool (phase=draft).",
            model=model,
            temperature=config.temperatures.plan_reviewer,
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
        reminder_threshold = int(max_tool_calls * _REVIEWER_BUDGET_REMINDER_PCT)
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
                            "subagent_label": _SUBAGENT_LABEL,
                        })
                    if sink is not None and parent_tc_id:
                        await sink.put(StreamEvent(
                            "subagent_text",
                            content=event.content,
                            parent_tool_call_id=parent_tc_id,
                            metadata={
                                "subagent_label": _SUBAGENT_LABEL,
                                "mission_text": _MISSION_TEXT,
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
                            "subagent_label": _SUBAGENT_LABEL,
                        })
                    if sink is not None and parent_tc_id:
                        await sink.put(StreamEvent(
                            "subagent_tool_call",
                            tool_call=event.tool_call,
                            parent_tool_call_id=parent_tc_id,
                            metadata={
                                "subagent_label": _SUBAGENT_LABEL,
                                "mission_text": _MISSION_TEXT,
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
                            "subagent_label": _SUBAGENT_LABEL,
                        })
                    if sink is not None and parent_tc_id:
                        await sink.put(StreamEvent(
                            "subagent_tool_result",
                            tool_call=event.tool_call,
                            tool_result=content,
                            parent_tool_call_id=parent_tc_id,
                            metadata={
                                "subagent_label": _SUBAGENT_LABEL,
                                "mission_text": _MISSION_TEXT,
                            },
                        ))
                    # Soft reminder at >=95%: tell the reviewer to stop
                    # investigating and emit its report (with the same
                    # "Stopping point" framing delegate uses) while it
                    # still has a buffer turn before the hard cancel.
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
                        "Plan reviewer error event: %s", event.error,
                    )
                    stop_reason = event.error or "reviewer error"
                    break
        except Exception as e:
            log.error("Plan reviewer exception: %s", e)
            stop_reason = f"reviewer exception: {e}"

        # Truncate text step content for size control
        for step in subagent_steps:
            if step["type"] == "text" and len(step["content"]) > _MAX_TEXT_CHARS:
                step["content"] = step["content"][:_MAX_TEXT_CHARS] + "...(truncated)"

        return all_text, subagent_steps, stop_reason, tool_call_count

    async def execute(
        content: str = "",
        phase: str = "",
        **_kwargs: object,
    ) -> ToolResult:
        # `**_kwargs` absorbs any obsolete params silently — no error.
        del _kwargs

        # Validate phase first — required, must be one of the two known
        # values. Empty/whitespace is treated as missing.
        phase = (phase or "").strip()
        if not phase:
            return ToolResult(
                error="phase parameter is required ('draft' or 'final')"
            )
        if phase not in ("draft", "final"):
            return ToolResult(
                error=f"phase must be 'draft' or 'final', got {phase!r}"
            )

        if not content or not content.strip():
            return ToolResult(error="Plan content is required")

        if phase == "final":
            # Existing marker behavior. The agent loop intercepts the
            # plan tool call BEFORE this execute runs in the normal path
            # (loop.py: `tc.name == "plan" and tc.arguments.get("phase")
            # == "final"`), so this branch is hit only if the loop
            # bypasses the intercept (e.g. tests, or a future caller).
            # Keep it as the documented contract.
            return ToolResult(
                output="",
                metadata={"plan_content": content, "awaiting_plan_review": True},
            )

        # phase == "draft": spawn the reviewer sub-agent.
        # Lazy imports to avoid circular dependency
        # (agent.loop -> tool.tools package -> plan -> agent.loop).
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
            return ToolResult(error="No tools available for plan review")

        provider = get_provider()

        task_message = (
            "Review this draft implementation plan critically. Verify it "
            "solves the user's stated problem; flag gaps with file:line "
            "evidence; propose concrete edits the proposer can apply on "
            "the next iteration.\n\n"
            "=== Draft plan to review ===\n"
            f"{content}"
        )

        transcript, steps, stop_reason, tc_count = await _run_reviewer_subagent(
            _REVIEWER_PROMPT,
            task_message,
            sub_registry,
            provider,
            sink,
            parent_tc_id,
            max_tool_calls=_REVIEWER_MAX_TOOL_CALLS,
        )

        # Hard rule (matches delegate): a sub-agent that made zero tool
        # calls cannot have grounded its review — anything in <report>
        # is fabricated from prior knowledge, not facts about THIS plan
        # against THIS repo. Discard any "report" and emit a rejection
        # notice the proposer can act on.
        if tc_count == 0:
            report_text = ""
            if not stop_reason:
                stop_reason = (
                    "zero tool calls — review rejected as ungrounded"
                )
        else:
            report_text = _extract_report(transcript)

        if report_text.strip():
            section_body = report_text
        elif tc_count == 0:
            section_body = (
                "[Plan review rejected: reviewer sub-agent made zero "
                "tool calls. A read-only reviewer must ground every "
                "criticism in files it opens during the session — "
                "text-only output is treated as fabricated from prior "
                "knowledge and not trusted. The plan was NOT reviewed; "
                "either the reviewer hit an error, or the model produced "
                "a degenerate response. Re-call `plan` with "
                "`phase=\"draft\"` after refining the plan content, or "
                "skip directly to `phase=\"final\"` if you are confident "
                "the plan is ready for the user.]"
            )
        else:
            fallback = _synthesise_fallback_report(
                steps, "plan reviewer", stop_reason=stop_reason,
            )
            if fallback:
                section_body = fallback
            else:
                err_line = "Plan review produced no output"
                if stop_reason:
                    err_line = f"{err_line} ({stop_reason})"
                section_body = f"[{err_line}]"

        truncated = truncate_output(
            section_body,
            max_lines=_REPORT_MAX_LINES,
            max_bytes=_REPORT_MAX_BYTES,
        )

        metadata: dict[str, Any] = {
            "phase": "draft",
            "subagent_steps": steps,
            "tool_call_count": tc_count,
            "stop_reason": stop_reason,
        }

        return ToolResult(output=truncated, metadata=metadata)

    return Tool(
        name="plan",
        description=(
            "USE IT FIRST TO DRAFT A PLAN THEN TO DELIVER THE FINALIZED PLAN to the user. "
            "For `phase=\"draft\"`, spawns a read-only "
            "reviewer sub-agent that re-reads relevant code, criticizes "
            "the plan against actual files, and returns concrete "
            "feedback wrapped in `<report>...</report>` (Verdict / "
            "Issues / Concrete edits / Verified / Caveats) so the agent "
            "can iterate. For `phase=\"final\"`, pauses the agent loop "
            "and presents the plan to the user (Execute / Accept & "
            "Clear / Reject / Send Feedback). "
            "Skip to phase=\"final\"` directly ONLY for trivial plans."
        ),
        parameters=[
            ToolParameter(
                name="content",
                description=(
                    "Full implementation plan in Markdown. Include "
                    "affected files, the specific change per file, "
                    "step-by-step ordering, and any verification (tests, "
                    "type checks). Use `path/to/file.py:line` citations "
                    "and code blocks where they sharpen the plan. The "
                    "reviewer reads this content directly — make the "
                    "user's goal inferable from it."
                ),
            ),
            ToolParameter(
                name="phase",
                description=(
                    "`draft` to get a critical review by a read-only "
                    "sub-agent (returns concrete feedback for "
                    "iteration), or `final` to deliver the polished "
                    "plan to the user for accept/reject/revise. Always "
                    "draft first unless the plan is trivial."
                ),
                enum=["draft", "final"],
            ),
        ],
        execute_fn=execute,
    )
