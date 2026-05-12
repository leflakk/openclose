"""Main agent loop — streaming LLM interaction with tool calling."""

from __future__ import annotations

import asyncio
import contextvars
import json
import re
from collections import deque
from typing import Any, AsyncIterator, Callable, Awaitable

from openai.types.chat import ChatCompletionMessageParam

from openclose.agent.agent import Agent, AgentMode, get_agent
from openclose.config.paths import ConfigPaths
from openclose.agent.prompt import build_system_prompt
from openclose.id import generate_id
from openclose.permission.permission import PermissionEngine
from openclose.permission.broker import PermissionBroker
from openclose.permission.schema import PermissionRequest
from openclose.permission.extract import extract_path, check_path_sandbox
from openclose.tool.tool import ToolResult
from openclose.tool.tools.bash import _check_command
from openclose.tool.tools.plan_broker import PlanBroker
from openclose.tool.tools.ask_user_broker import AskUserBroker
from openclose.provider.provider import BaseProvider
from openclose.debug import LLMDebugContext, llm_debug_context
from openclose.log import get_logger

log = get_logger(__name__)

# Maximum consecutive empty LLM responses before terminating the loop.
# Local models (e.g. vLLM) occasionally return finish_reason=stop with
# no content; retrying with a nudge message usually recovers.
_MAX_EMPTY_RETRIES = 3
_EMPTY_RESPONSE_NUDGE = (
    "Continue with the task. If you need to use a tool, make a tool call. "
    "If the task is complete, provide your final response."
)

# Read-only doom-loop detection: 3 consecutive identical calls to a
# read-only tool. Edit/bash repeats are often legitimate (re-running a
# failing test, re-applying an edit after a tweak) so they are excluded
# from the consecutive-identical rule — bash gets its own windowed rule
# below, which only fires on byte-identical commands.
_READONLY_TOOLS_FOR_DOOM = {"grep", "read", "glob", "delegate"}
_DOOM_RECOVERY_NUDGE = (
    "You called the same read-only tool with identical arguments three "
    "times in a row with no progress. STOP repeating that call. Using "
    "only what you already know, make your best-effort edit to solve "
    "the task, or explain why you cannot. Do not invoke that tool again."
)

# Windowed bash detection. We track the last K bash commands and fire on
# patterns that don't have to be consecutive — the dominant model failure
# mode is "probe → mutate → probe → mutate → probe", where the same probe
# is rerun verbatim between mutations and the consecutive-streak counters
# in the read-only doom and install-burst rules both keep resetting.
_BASH_WINDOW = 10
_BASH_DOOM_THRESHOLD = 3  # same command repeated this many times in window → bash doom
_INSTALL_BURST_THRESHOLD = 3  # same install kind this many times in window → burst nudge
_BASH_DOOM_RECOVERY_NUDGE = (
    "You ran the same bash command {n} times within your last "
    f"{_BASH_WINDOW} bash calls. Re-running it is not producing new "
    "information. STOP repeating that command. Using only what you "
    "already know, make progress on the task or explain why you "
    "cannot. Do not run that exact command again."
)

# Install-burst patterns. When the LLM fires N bash calls of the same
# package-manager install kind (pip / npm / apt / …) within the bash
# window, inject a reminder asking it to step back and reconsider
# whether it's fighting the environment instead of advancing the task.
# Order matters: `uv pip install` contains `pip install` as a substring,
# so the uv pattern must be checked before the pip pattern.
_INSTALL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\buv\s+(pip\s+install|add|sync)\b"), "uv install"),
    (re.compile(r"\b(pip3?|pipx)\s+install\b"), "pip install"),
    (re.compile(r"\bpoetry\s+(add|install)\b"), "poetry install"),
    (re.compile(r"\b(npm|pnpm|yarn)\s+(install|add|i)\b"), "npm install"),
    (re.compile(r"\b(apt|apt-get)\s+install\b"), "apt install"),
    (re.compile(r"\bbrew\s+install\b"), "brew install"),
    (re.compile(r"\byum\s+install\b"), "yum install"),
    (re.compile(r"\bdnf\s+install\b"), "dnf install"),
    (re.compile(r"\bcargo\s+(install|add)\b"), "cargo install"),
    (re.compile(r"\bgem\s+install\b"), "gem install"),
    (re.compile(r"\bgo\s+(install|get)\b"), "go install"),
]
_INSTALL_BURST_NUDGE = (
    "You've made {n} bash calls matching a {label} pattern in a short "
    "window. Common pitfalls when this happens:\n"
    "  - Fighting an environment the harness is supposed to handle for you\n"
    "  - Re-running the same check instead of reading the existing output\n"
    "  - Working around a constraint you should be respecting (e.g., a "
    "\"do not run tests\" instruction in the task)\n\n"
    "Stop and consider: is this advancing the task, or are you stuck on "
    "infrastructure? Re-read the original instructions and the most recent "
    "tool output before issuing another bash call."
)


def _detect_install_pattern(command: str) -> str | None:
    """Return install pattern label (e.g. 'pip install') if `command` matches."""
    for pattern, label in _INSTALL_PATTERNS:
        if pattern.search(command):
            return label
    return None


def _split_top_level_json_objects(combined: str) -> list[str]:
    """Walk `combined` tracking JSON balance; return each top-level object."""
    objects: list[str] = []
    depth = 0
    in_str = False
    escape = False
    start: int | None = None
    for i, c in enumerate(combined):
        if escape:
            escape = False
            continue
        if in_str:
            if c == "\\":
                escape = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(combined[start:i + 1])
                start = None
    return objects


def _recover_split_tool_args(tool_calls: dict[int, "ToolCall"]) -> bool:
    """Best-effort recovery for parallel tool_calls whose JSON arguments
    were split at the wrong byte boundary by an OpenAI-compatible endpoint.

    Some local endpoints serialize parallel tool calls as
    ``{"a":1}{"b":2}`` and chunk the stream at arbitrary positions, so a
    closing ``"}`` of one object lands inside the next tool_call's args.
    We concatenate all args in index order, re-split at top-level JSON
    object boundaries, and reassign — but only when at least one
    arguments string is unparseable AND the recovered split has the same
    cardinality as the tool_call set. Returns True if anything was
    recovered. Safe no-op otherwise.
    """
    if len(tool_calls) < 2:
        return False
    # Only intervene if at least one args is unparseable JSON.
    needs_recovery = False
    for tc in tool_calls.values():
        try:
            json.loads(tc.arguments_raw or "{}")
        except json.JSONDecodeError:
            needs_recovery = True
            break
    if not needs_recovery:
        return False

    indices = sorted(tool_calls.keys())
    combined = "".join(tool_calls[i].arguments_raw or "" for i in indices)
    objects = _split_top_level_json_objects(combined)
    if len(objects) != len(indices):
        log.warning(
            "tool_args recovery: expected %d JSON objects, found %d — skipping",
            len(indices), len(objects),
        )
        return False
    # Sanity check: every recovered object must parse.
    for obj in objects:
        try:
            json.loads(obj)
        except json.JSONDecodeError:
            log.warning("tool_args recovery: split object did not parse — skipping")
            return False
    log.info(
        "tool_args recovery: re-split %d parallel tool_call args at JSON "
        "object boundaries (endpoint streamed bytes mid-object).",
        len(objects),
    )
    for idx, obj in zip(indices, objects):
        tool_calls[idx]._arguments = obj
    return True


# Context variables for real-time subagent event streaming.
# Tools (e.g. delegate) push StreamEvents to this queue during execution;
# the parent AgentLoop drains it and yields them to the SSE stream.
subagent_event_sink: contextvars.ContextVar[asyncio.Queue[StreamEvent | None] | None] = contextvars.ContextVar(
    "subagent_event_sink", default=None
)
# The tool_call_id of the currently executing tool — lets subagent tools
# tag their events so the frontend can place them in the right container.
current_tool_call_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_tool_call_id", default=""
)


class ToolCall:
    """Represents a tool call extracted from a streaming response."""

    def __init__(self) -> None:
        self.id: str = ""
        self.name: str = ""
        self._arguments: str = ""

    def append_arguments(self, chunk: str) -> None:
        self._arguments += chunk

    @property
    def arguments(self) -> dict[str, Any]:
        try:
            result = json.loads(self._arguments) if self._arguments else {}
            if isinstance(result, dict):
                return result
            return {}
        except json.JSONDecodeError:
            log.warning("Failed to parse tool arguments: %s", self._arguments)
            return {}

    @property
    def arguments_raw(self) -> str:
        return self._arguments


class StreamEvent:
    """An event from the agent loop stream."""

    def __init__(
        self,
        event_type: str,
        content: str = "",
        tool_call: ToolCall | None = None,
        tool_result: str = "",
        done: bool = False,
        error: str = "",
        context_info: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        parent_tool_call_id: str = "",
        message_id: str = "",
        part_id: str = "",
    ) -> None:
        self.type = event_type
        self.content = content
        self.tool_call = tool_call
        self.tool_result = tool_result
        self.done = done
        self.error = error
        self.context_info = context_info
        self.metadata = metadata
        self.parent_tool_call_id = parent_tool_call_id
        self.message_id = message_id
        self.part_id = part_id


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[ToolResult]]


class AgentLoop:
    """Orchestrates the agent loop: prompt -> LLM -> tool calls -> repeat."""

    def __init__(
        self,
        agent: Agent,
        provider: BaseProvider,
        tool_executor: ToolExecutor | None = None,
        tool_schemas: list[dict[str, Any]] | None = None,
        project_dir: str = ".",
        permission_engine: PermissionEngine | None = None,
        permission_broker: PermissionBroker | None = None,
        plan_broker: PlanBroker | None = None,
        ask_user_broker: AskUserBroker | None = None,
        session_id: str = "",
        cancel_event: asyncio.Event | None = None,
        extra_context: str = "",
    ) -> None:
        self._agent = agent
        self._provider = provider
        self._tool_executor = tool_executor
        self._project_dir = project_dir
        self._permission_engine = permission_engine
        self._permission_broker = permission_broker
        self._plan_broker = plan_broker
        self._ask_user_broker = ask_user_broker
        self._session_id = session_id
        self._cancel_event = cancel_event
        self._extra_context = extra_context
        self._messages: list[dict[str, Any]] = []
        self._step = 0
        self._recent_calls: list[tuple[str, str]] = []  # consecutive read-only doom
        self._doom_nudged = False  # one-shot recovery nudge before hard terminate
        # Sliding window of the last K bash calls as (command, install_label).
        # Fed by the doom-check pass; read by both the byte-identical bash
        # doom rule and the install-burst rule, so non-install probes
        # interleaved between repeated installs don't reset either.
        self._recent_bash: deque[tuple[str, str | None]] = deque(maxlen=_BASH_WINDOW)
        self._install_burst_nudged = False  # install-burst nudge fires once per run
        # Slot for an out-of-band user message queued by an external caller
        # (e.g. the delegate tool injecting a near-budget reminder). Drained
        # at the top of the next loop iteration so it lands AFTER any
        # in-flight tool_result appends and BEFORE the next LLM call.
        self._pending_nudge: str | None = None

        # Filter tool schemas so the LLM only sees tools this agent can use.
        # Keep the unfiltered list so we can re-filter on a mid-loop agent
        # swap (see _switch_agent — fired when a plan is approved).
        all_schemas = tool_schemas or []
        self._all_tool_schemas: list[dict[str, Any]] = list(all_schemas)
        self._tool_schemas = agent.filter_tool_schemas(all_schemas)
        self._tool_names: list[str] = [
            str(s["name"])
            for s in self._tool_schemas
            if isinstance(s, dict) and isinstance(s.get("name"), str)
        ]
        # Set when the user accepts a plan; drained at end-of-round so
        # in-flight tool calls finish under the old agent's permissions
        # before the new agent's tool view takes effect.
        self._pending_agent_switch: str | None = None

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self._messages

    @messages.setter
    def messages(self, value: list[dict[str, Any]]) -> None:
        self._messages = value

    def request_user_nudge(self, text: str) -> None:
        """Queue a user message to be injected before the next LLM call.

        Safe to call from within a `tool_result` event handler in the
        consumer of `run()` — the message lands at the start of the next
        loop iteration, after the loop itself has finished appending all
        tool results from the current round.
        """
        self._pending_nudge = text

    def _system_message(self) -> dict[str, str]:
        content = build_system_prompt(
            self._agent,
            self._project_dir,
            extra_context=self._extra_context,
            tool_names=self._tool_names,
        )
        return {"role": "system", "content": content}

    def _build_full_messages(self) -> list[ChatCompletionMessageParam]:
        """Build the full message list for the LLM, including system prompt."""
        all_msgs: list[dict[str, Any]] = [self._system_message(), *self._messages]
        # Cast to the expected type — the dicts match the OpenAI schema
        return all_msgs  # type: ignore[return-value]

    def _check_install_burst(
        self, sorted_tcs: list[tuple[int, ToolCall]],
    ) -> str | None:
        """Return install pattern label if any install-pattern bash call in
        this round pushes the windowed count to threshold (and we have not
        yet nudged for this run).

        Reads from ``self._recent_bash``, which the doom-check pass has
        already updated for this round. Windowed semantics mean interleaved
        non-install probes do NOT reset the count — that was the gap that
        let the test→install→test→install loop slip through.
        """
        if self._install_burst_nudged:
            return None
        for _idx, tc in sorted_tcs:
            if tc.name != "bash":
                continue
            cmd = tc.arguments.get("command", "")
            label = _detect_install_pattern(cmd) if isinstance(cmd, str) else None
            if label is None:
                continue
            count = sum(1 for _, lbl in self._recent_bash if lbl == label)
            if count >= _INSTALL_BURST_THRESHOLD:
                self._install_burst_nudged = True
                return label
        return None

    def _switch_agent(self, new_agent_name: str) -> None:
        """Swap the active agent mid-loop and re-derive its tool view.

        Fired when the user accepts a plan: the run continues in the
        build agent with the plan injected as system-prompt context.
        Per-run noise (doom counters, recent-bash window, one-shot
        nudge flags) is reset so the new agent isn't penalised by the
        old agent's history. ``_step`` is intentionally preserved —
        the step budget is a per-run guardrail, not per-agent.
        """
        new_agent = get_agent(new_agent_name)
        self._agent = new_agent
        self._tool_schemas = new_agent.filter_tool_schemas(self._all_tool_schemas)
        self._tool_names = [
            str(s["name"])
            for s in self._tool_schemas
            if isinstance(s, dict) and isinstance(s.get("name"), str)
        ]
        self._recent_calls.clear()
        self._recent_bash.clear()
        self._doom_nudged = False
        self._install_burst_nudged = False
        # Surface the just-saved plan as system-prompt extra context so
        # the new agent gets the same "## Active Plan" cue that
        # processor.py would inject for the *next* user message.
        plan_path = ConfigPaths.project_runtime_dir(self._project_dir) / "plan.md"
        if plan_path.is_file():
            plan_text = plan_path.read_text()
            self._extra_context = (
                "## Active Plan\n"
                "The following plan has been approved by the user. "
                "Follow it step by step:\n\n" + plan_text
            )

    async def run(self, user_message: str) -> AsyncIterator[StreamEvent]:
        """Run the agent loop for a user message, yielding stream events."""
        # Auto-detect model if not set
        if not self._agent.model:
            detected = await self._provider.detect_model()
            if detected:
                self._agent.model = detected
            else:
                yield StreamEvent("error", error="No model configured and auto-detection failed. Set default_model in config.toml")
                return

        self._messages.append({"role": "user", "content": user_message})
        consecutive_empty_responses = 0

        while self._step < self._agent.max_steps:
            if self._cancel_event and self._cancel_event.is_set():
                log.info("Agent loop cancelled before step %d", self._step + 1)
                yield StreamEvent("done", done=True)
                return
            self._step += 1
            log.debug("Agent loop step %d/%d", self._step, self._agent.max_steps)

            if self._pending_nudge is not None:
                self._messages.append(
                    {"role": "user", "content": self._pending_nudge}
                )
                self._pending_nudge = None

            full_messages = self._build_full_messages()
            tools = self._tool_schemas if self._tool_schemas else None

            # Tag this LLM call for the debug dumper. Provider.chat() reads
            # this contextvar and forwards the request to dump_llm_request
            # when OPENCLOSE_DEBUG_LLM is enabled.
            llm_debug_context.set(LLMDebugContext(
                source="agent_loop",
                step=self._step,
                project_dir=self._project_dir,
            ))

            # Stream from LLM
            text_content = ""
            tool_calls: dict[int, ToolCall] = {}
            has_tool_calls = False
            finish_reason: str | None = None

            # Force a tool call on a sub-agent's very first turn. The
            # delegate sub-agent's report-shaped prompt occasionally lets
            # the model write a fabricated <report> from prior knowledge
            # instead of investigating; tool_choice="required" binds the
            # first turn at the API boundary. After step 1 we revert to
            # default "auto" so the model can emit its final report (or
            # legitimately stop with "target not found").
            tool_choice = (
                "required"
                if self._agent.mode == AgentMode.SUBAGENT
                and self._step == 1
                and tools
                else None
            )

            try:
                async for chunk in self._provider.chat(
                    messages=full_messages,
                    model=self._agent.model,
                    tools=tools,
                    temperature=self._agent.temperature,
                    tool_choice=tool_choice,
                ):
                    for choice in chunk.choices:
                        delta = choice.delta
                        if choice.finish_reason:
                            finish_reason = choice.finish_reason

                        if delta.content:
                            text_content += delta.content
                            yield StreamEvent("text", content=delta.content)

                        if delta.tool_calls:
                            has_tool_calls = True
                            for tc_delta in delta.tool_calls:
                                idx = tc_delta.index
                                if idx not in tool_calls:
                                    tool_calls[idx] = ToolCall()
                                if tc_delta.id:
                                    tool_calls[idx].id = tc_delta.id
                                if tc_delta.function and tc_delta.function.name:
                                    tool_calls[idx].name = tc_delta.function.name
                                if tc_delta.function and tc_delta.function.arguments:
                                    tool_calls[idx].append_arguments(
                                        tc_delta.function.arguments
                                    )

                    if self._cancel_event and self._cancel_event.is_set():
                        log.info("Agent loop cancelled during LLM streaming")
                        break

            except Exception as e:
                log.error("LLM error: %s", e)
                yield StreamEvent("error", error=str(e))
                return

            # Check cancellation after streaming
            if self._cancel_event and self._cancel_event.is_set():
                if text_content:
                    self._messages.append(
                        {"role": "assistant", "content": text_content}
                    )
                yield StreamEvent("done", done=True)
                return

            # Add assistant message (no tool calls)
            if text_content and not has_tool_calls:
                log.info("Agent loop ending: text response without tool calls (step %d, finish_reason=%s)", self._step, finish_reason)
                self._messages.append(
                    {"role": "assistant", "content": text_content}
                )
                yield StreamEvent("done", done=True)
                return

            if has_tool_calls:
                consecutive_empty_responses = 0
                # Some OpenAI-compatible endpoints stream parallel tool
                # calls but chunk the byte stream mid-string, leaving each
                # tool_call's `arguments` malformed even though the
                # concatenation is two valid JSON objects. Recover when
                # we can; bail safely when we can't.
                _recover_split_tool_args(tool_calls)
                # Build assistant message with tool calls
                tc_list: list[dict[str, Any]] = []
                for _idx, tc in sorted(tool_calls.items()):
                    tc_list.append(
                        {
                            "id": tc.id,
                            "name": tc.name,
                            "arguments": tc.arguments_raw,
                        }
                    )

                self._messages.append(
                    {
                        "role": "assistant",
                        "content": text_content or None,
                        "tool_calls": tc_list,
                    }
                )

                # Two-phase execution: permission checks then parallel execution
                sorted_tcs = sorted(tool_calls.items())

                # First, yield all tool_call events
                for _idx, tc in sorted_tcs:
                    yield StreamEvent("tool_call", tool_call=tc)

                # Phase A: Sequential permission checks
                approved: list[tuple[int, ToolCall]] = []
                denied_results: list[tuple[ToolCall, ToolResult]] = []

                for idx, tc in sorted_tcs:
                    # 1. Agent-level check
                    if not self._agent.can_use_tool(tc.name):
                        msg = (
                            f"Error: Agent '{self._agent.name}' is not "
                            f"allowed to use tool '{tc.name}'"
                        )
                        log.warning(msg)
                        denied_results.append((tc, ToolResult(error=msg)))
                        continue

                    # 2. Pre-permission safety checks (before dialog)
                    # 2a. Path sandbox — block writes outside project dir
                    sandbox_err = check_path_sandbox(
                        tc.name, tc.arguments, self._project_dir,
                    )
                    if sandbox_err is not None:
                        denied_results.append(
                            (tc, ToolResult(error=f"{sandbox_err}. "
                             f"Do NOT retry with the same path. "
                             f"Inform the user and suggest alternatives."))
                        )
                        continue

                    # 2b. Bash heuristics — block dangerous commands
                    if tc.name == "bash":
                        cmd = tc.arguments.get("command")
                        if isinstance(cmd, str):
                            blocked = _check_command(cmd)
                            if blocked is not None:
                                denied_results.append(
                                    (tc, ToolResult(error=f"Blocked: {blocked}. "
                                     f"Do NOT retry this command. "
                                     f"Inform the user and suggest alternatives."))
                                )
                                continue

                    # 3. Permission engine check (skip for plan/ask_user tools —
                    #    they have their own user interaction)
                    if self._permission_engine is not None and tc.name not in ("plan", "ask_user"):
                        path = extract_path(tc.name, tc.arguments, self._project_dir)
                        perm_req = PermissionRequest(
                            tool_name=tc.name,
                            path=path,
                            arguments=tc.arguments,
                        )
                        perm_resp = self._permission_engine.check(perm_req)

                        if not perm_resp.allowed and not perm_resp.needs_ask:
                            # DENY — absolute block
                            denied_results.append(
                                (tc, ToolResult(error=f"{perm_resp.reason}. "
                                 f"This tool is permanently blocked by a DENY rule. "
                                 f"Do NOT retry. Inform the user and suggest alternatives."))
                            )
                            continue

                        if perm_resp.needs_ask:
                            # ASK — need user approval
                            if self._permission_broker is not None:
                                # Stamp ID before emitting so frontend gets it
                                perm_req.request_id = generate_id()
                                # Emit permission_request event for frontend
                                perm_content = json.dumps({
                                    "request_id": perm_req.request_id,
                                    "tool_name": tc.name,
                                    "path": path,
                                    "tool_args": tc.arguments,
                                })
                                yield StreamEvent("permission_request", content=perm_content)

                                reply = await self._permission_broker.ask(
                                    perm_req, session_id=self._session_id,
                                )
                                if reply == "reject":
                                    denied_results.append(
                                        (tc, ToolResult(error=f"The user has REJECTED permission to use the '{tc.name}' tool. "
                                         f"Do NOT retry this tool call. Inform the user that the action was "
                                         f"blocked and ask how they would like to proceed."))
                                    )
                                    continue
                                if reply == "always":
                                    self._permission_engine.grant_session(tc.name)
                            else:
                                # No broker — deny with reason
                                denied_results.append(
                                    (tc, ToolResult(error=f"{perm_resp.reason}. "
                                     f"No approval mechanism available. Do NOT retry. "
                                     f"Inform the user and suggest alternatives."))
                                )
                                continue

                    approved.append((idx, tc))

                # Doom-loop detection. Two rules feed into the same
                # nudge-then-terminate flow:
                #   - read-only doom: 3 consecutive identical calls to a
                #     read-only tool (grep/read/glob/delegate).
                #   - bash-windowed doom: a bash command that appears 3+
                #     times within the last _BASH_WINDOW bash calls. The
                #     window catches "probe → mutate → probe" loops where
                #     repeats are interleaved, not consecutive.
                # First trip injects a recovery nudge and continues so the
                # agent can still attempt an edit; a second trip hard-terminates.
                for _, tc in approved:
                    call_sig = (tc.name, tc.arguments_raw)
                    self._recent_calls.append(call_sig)
                    if len(self._recent_calls) > 3:
                        self._recent_calls = self._recent_calls[-3:]
                    if tc.name == "bash":
                        cmd = tc.arguments.get("command", "")
                        if isinstance(cmd, str):
                            self._recent_bash.append(
                                (cmd, _detect_install_pattern(cmd))
                            )

                readonly_doom = (
                    len(self._recent_calls) >= 3
                    and self._recent_calls[-3]
                    == self._recent_calls[-2]
                    == self._recent_calls[-1]
                    and self._recent_calls[-1][0] in _READONLY_TOOLS_FOR_DOOM
                )
                bash_doom_count = 0
                if self._recent_bash:
                    last_cmd = self._recent_bash[-1][0]
                    bash_doom_count = sum(
                        1 for c, _ in self._recent_bash if c == last_cmd
                    )
                bash_doom = bash_doom_count >= _BASH_DOOM_THRESHOLD

                if readonly_doom or bash_doom:
                    if readonly_doom:
                        repeated_tool = self._recent_calls[-1][0]
                        nudge_text = _DOOM_RECOVERY_NUDGE
                    else:
                        repeated_tool = "bash"
                        nudge_text = _BASH_DOOM_RECOVERY_NUDGE.format(
                            n=bash_doom_count,
                        )
                    if not self._doom_nudged:
                        log.warning(
                            "Doom loop on %s; injecting recovery nudge",
                            repeated_tool,
                        )
                        # Pair the already-appended assistant tool_calls
                        # message with synthetic tool_result messages so
                        # the next LLM call sees well-formed context.
                        for _, tc in sorted_tcs:
                            self._messages.append({
                                "role": "tool",
                                "content": (
                                    f"[skipped: {tc.name} would be a "
                                    f"doom-loop repeat]"
                                ),
                                "tool_call_id": tc.id,
                            })
                        self._messages.append({
                            "role": "user",
                            "content": nudge_text,
                        })
                        self._recent_calls.clear()
                        self._recent_bash.clear()
                        self._doom_nudged = True
                        continue
                    if self._permission_broker is not None:
                        doom_req = PermissionRequest(tool_name="doom_loop")
                        doom_req.request_id = generate_id()
                        doom_content = json.dumps({
                            "request_id": doom_req.request_id,
                            "tool_name": "doom_loop",
                            "path": "",
                            "tool_args": {"repeated_tool": repeated_tool},
                        })
                        yield StreamEvent("permission_request", content=doom_content)
                        doom_reply = await self._permission_broker.ask(
                            doom_req, session_id=self._session_id,
                        )
                        if doom_reply == "reject":
                            yield StreamEvent(
                                "error",
                                error=f"Doom loop detected: {repeated_tool} called 3 times with same args",
                            )
                            return
                    else:
                        log.warning("Doom loop detected: %s", repeated_tool)
                        yield StreamEvent(
                            "error",
                            error=f"Doom loop detected: {repeated_tool} called 3 times with same args",
                        )
                        return

                # Handle interactive tool calls sequentially (require user interaction)
                plan_results: list[tuple[ToolCall, ToolResult]] = []
                ask_user_results: list[tuple[ToolCall, ToolResult]] = []
                remaining_approved: list[tuple[int, ToolCall]] = []

                for idx, tc in approved:
                    if (
                        tc.name == "plan"
                        and tc.arguments.get("phase") == "final"
                        and self._plan_broker is not None
                    ):
                        plan_content = tc.arguments.get("content", "")
                        if plan_content:
                            request_id = generate_id()
                            p_content = json.dumps({
                                "request_id": request_id,
                                "plan_content": plan_content,
                            })
                            yield StreamEvent("plan_review_request", content=p_content)
                            plan_reply = await self._plan_broker.ask(
                                request_id, plan_content,
                                session_id=self._session_id,
                            )
                            if plan_reply.action in ("execute", "execute_clear"):
                                # Write plan.md to project
                                plan_path = ConfigPaths.project_runtime_dir(self._project_dir) / "plan.md"
                                plan_path.parent.mkdir(parents=True, exist_ok=True)
                                plan_path.write_text(plan_content)
                                clear_session = plan_reply.action == "execute_clear"
                                plan_results.append((
                                    tc,
                                    ToolResult(output="Plan accepted by user. Plan saved. "
                                    "Switching to build agent for execution."),
                                ))
                                yield StreamEvent(
                                    "plan_executed",
                                    content=json.dumps({
                                        "new_agent": "build",
                                        "plan_saved": True,
                                        "clear_session": clear_session,
                                    }),
                                )
                                # Defer the actual swap until the round
                                # drains so already-approved sibling tools
                                # finish under the plan agent's permissions.
                                self._pending_agent_switch = "build"
                            elif plan_reply.action == "reject":
                                plan_results.append((
                                    tc,
                                    ToolResult(output="Plan rejected by user. Ask them what they would like instead."),
                                ))
                            elif plan_reply.action == "revise":
                                plan_results.append((
                                    tc,
                                    ToolResult(output=f"User requested changes to the plan. Their feedback:\n\n"
                                    f"{plan_reply.feedback}\n\n"
                                    f"Revise the plan based on this feedback and call the plan tool again."),
                                ))
                        else:
                            plan_results.append((tc, ToolResult(error="empty plan content")))
                    elif tc.name == "ask_user" and self._ask_user_broker is not None:
                        questions = tc.arguments.get("questions", [])
                        # LLM sometimes double-encodes as a JSON string
                        if isinstance(questions, str):
                            try:
                                questions = json.loads(questions)
                            except (json.JSONDecodeError, TypeError):
                                questions = []
                        if questions:
                            request_id = generate_id()
                            au_content = json.dumps({
                                "request_id": request_id,
                                "questions": questions,
                            })
                            yield StreamEvent("ask_user_request", content=au_content)
                            au_reply = await self._ask_user_broker.ask(
                                request_id, questions,
                                session_id=self._session_id,
                            )
                            if au_reply.answers:
                                lines = ["User's answers:"]
                                for i, ans in enumerate(au_reply.answers):
                                    lines.append(
                                        f"{i + 1}. Q: {ans.get('question', '?')} "
                                        f"A: {ans.get('answer', '(no answer)')}"
                                    )
                                ask_user_results.append((
                                    tc, ToolResult(output="\n".join(lines)),
                                ))
                            else:
                                ask_user_results.append((
                                    tc, ToolResult(output="User did not provide answers (cancelled)."),
                                ))
                        else:
                            ask_user_results.append((tc, ToolResult(error="empty questions list")))
                    else:
                        remaining_approved.append((idx, tc))

                # Phase B: Parallel execution with real-time subagent event streaming
                async def execute_tool(tc: ToolCall) -> tuple[ToolCall, ToolResult]:
                    """Execute a single tool call and return the result."""
                    tc_token = current_tool_call_id.set(tc.id)
                    try:
                        if self._tool_executor is not None:
                            try:
                                result = await self._tool_executor(
                                    tc.name, tc.arguments
                                )
                            except (Exception, asyncio.CancelledError) as e:
                                result = ToolResult(error=f"Error executing tool '{tc.name}': {e}")
                                log.error(result.error)
                        else:
                            result = ToolResult(error="No tool executor configured")
                    finally:
                        current_tool_call_id.reset(tc_token)
                        # Send sentinel so the drain loop knows this tool is done
                        sink = subagent_event_sink.get(None)
                        if sink is not None:
                            await sink.put(None)
                    return tc, result

                event_queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
                sink_token = subagent_event_sink.set(event_queue)
                try:
                    tasks = [
                        asyncio.create_task(execute_tool(tc))
                        for _, tc in remaining_approved
                    ]

                    if tasks:
                        # Drain subagent events from queue while tasks run
                        active = len(tasks)
                        while active > 0:
                            try:
                                event = await asyncio.wait_for(
                                    event_queue.get(), timeout=0.05,
                                )
                                if event is None:
                                    active -= 1
                                else:
                                    yield event
                            except asyncio.TimeoutError:
                                # Check for crashed tasks
                                crashed = sum(
                                    1 for t in tasks
                                    if t.done() and not t.cancelled()
                                    and t.exception() is not None
                                )
                                if crashed:
                                    active -= crashed
                                if all(t.done() for t in tasks):
                                    break

                        # Drain any remaining events
                        while not event_queue.empty():
                            evt = event_queue.get_nowait()
                            if evt is not None:
                                yield evt

                    exec_results = [t.result() for t in tasks] if tasks else []
                finally:
                    subagent_event_sink.reset(sink_token)

                # Combine denied + plan + ask_user + executed results in original order
                all_results: dict[str, tuple[ToolCall, ToolResult]] = {}
                for tc, result in denied_results:
                    all_results[tc.id] = (tc, result)
                for tc, result in plan_results:
                    all_results[tc.id] = (tc, result)
                for tc, result in ask_user_results:
                    all_results[tc.id] = (tc, result)
                for tc, result in exec_results:
                    all_results[tc.id] = (tc, result)

                # Yield tool results and add to messages in original order
                for _idx, tc in sorted_tcs:
                    if tc.id in all_results:
                        res_tc, tool_result = all_results[tc.id]
                        result_str = tool_result.to_string()
                        yield StreamEvent(
                            "tool_result",
                            tool_result=result_str,
                            tool_call=res_tc,
                            metadata=tool_result.metadata or None,
                        )
                        self._messages.append(
                            {
                                "role": "tool",
                                "content": result_str,
                                "tool_call_id": tc.id,
                            }
                        )

                # Check cancellation after tool execution
                if self._cancel_event and self._cancel_event.is_set():
                    log.info("Agent loop cancelled after tool execution")
                    yield StreamEvent("done", done=True)
                    return

                # Install-burst nudge: if 3+ consecutive same-pattern installs,
                # remind the agent to step back instead of fighting the env.
                burst_label = self._check_install_burst(sorted_tcs)
                if burst_label is not None:
                    log.info(
                        "Install-burst nudge injected for %s pattern",
                        burst_label,
                    )
                    self._messages.append({
                        "role": "user",
                        "content": _INSTALL_BURST_NUDGE.format(
                            n=_INSTALL_BURST_THRESHOLD, label=burst_label,
                        ),
                    })

                # Apply any deferred agent swap now that the round has
                # fully drained. The next LLM call will use the new
                # agent's system prompt + filtered tool schemas.
                if self._pending_agent_switch is not None:
                    self._switch_agent(self._pending_agent_switch)
                    self._pending_agent_switch = None

                continue

            # No content and no tool calls — empty response from model
            consecutive_empty_responses += 1
            if consecutive_empty_responses >= _MAX_EMPTY_RETRIES:
                log.info(
                    "Agent loop ending: %d consecutive empty responses "
                    "(step %d, finish_reason=%s)",
                    consecutive_empty_responses, self._step, finish_reason,
                )
                yield StreamEvent("done", done=True)
                return

            log.warning(
                "Empty LLM response (attempt %d/%d, step %d, "
                "finish_reason=%s), retrying with nudge",
                consecutive_empty_responses, _MAX_EMPTY_RETRIES,
                self._step, finish_reason,
            )
            yield StreamEvent(
                "info",
                content=f"Empty response from model, retrying "
                f"({consecutive_empty_responses}/{_MAX_EMPTY_RETRIES})...",
            )
            self._messages.append({
                "role": "user",
                "content": _EMPTY_RESPONSE_NUDGE,
            })
            continue

        yield StreamEvent(
            "error", error=f"Max steps ({self._agent.max_steps}) reached"
        )
