"""Session processor — orchestrates agent loop + tools for a session."""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from openclose.agent.agent import get_agent
from openclose.config.paths import ConfigPaths
from openclose.agent.loop import AgentLoop, StreamEvent, ToolExecutor
from openclose.permission.permission import PermissionEngine
from openclose.permission.broker import PermissionBroker
from openclose.tool.tools.plan_broker import PlanBroker
from openclose.tool.tools.ask_user_broker import AskUserBroker
from openclose.provider.provider import Provider, get_provider
from openclose.session.session import SessionManager
from openclose.session.message import MessagePartType, MessageRole
from openclose.session.compaction import (
    compact_messages,
    estimate_messages_tokens,
    estimate_tool_schemas_tokens,
    summarize_for_compaction,
)
from openclose.storage.schema import Message, MessagePart
from openclose.config.config import get_config
from openclose.storage.db import Database
from openclose.debug import LLMDebugContext, llm_debug_context
from openclose.log import get_logger

log = get_logger(__name__)


def _derive_title(text: str, max_len: int = 50) -> str:
    """Derive a short session title from user text."""
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_len:
        return cleaned
    truncated = cleaned[:max_len].rsplit(" ", 1)[0]
    return truncated + "\u2026"


class SessionProcessor:
    """Processes user messages within a session context.

    Ties together: session persistence, agent loop, tool execution, compaction.
    """

    def __init__(
        self,
        db: Database,
        session_id: str,
        agent_name: str = "build",
        provider: Provider | None = None,
        tool_executor: ToolExecutor | None = None,
        tool_schemas: list[dict[str, Any]] | None = None,
        project_dir: str = ".",
        permission_engine: PermissionEngine | None = None,
        permission_broker: PermissionBroker | None = None,
        plan_broker: PlanBroker | None = None,
        ask_user_broker: AskUserBroker | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        self._session_mgr = SessionManager(db)
        self._session_id = session_id
        self._agent = get_agent(agent_name)

        # Resolve provider + model from session state. New sessions have
        # empty fields → get_provider() falls back to config.default_provider
        # (then to the first declared provider). Once the user picks a
        # different (provider, model) via /model in the UI, the SessionManager
        # writes those fields and this resolution honors them on the next
        # message.
        session_row = self._session_mgr.get_session(session_id)
        session_provider = session_row.provider if session_row else ""
        session_model = session_row.model if session_row else ""
        self._provider = provider or get_provider(session_provider)

        # Model priority: session.model > agent_cfg.model (already in
        # self._agent.model when set there) > session's resolved provider's
        # default_model. The last branch fixes a multi-provider bug where
        # agent.py:90-96 always picked the *first* provider's default_model.
        if session_model:
            self._agent.model = session_model
        elif session_provider:
            cfg = get_config()
            pcfg = next(
                (p for p in cfg.providers if p.name == session_provider), None,
            )
            if pcfg and pcfg.default_model:
                self._agent.model = pcfg.default_model
        self._tool_executor = tool_executor
        self._tool_schemas = tool_schemas or []
        self._project_dir = project_dir
        self._permission_engine = permission_engine
        self._permission_broker = permission_broker
        self._plan_broker = plan_broker
        self._ask_user_broker = ask_user_broker
        self._cancel_event = cancel_event

    @staticmethod
    def _reconstruct_llm_messages(
        messages_with_parts: list[tuple[Message, list[MessagePart]]],
    ) -> list[dict[str, Any]]:
        """Convert stored messages + parts into LLM-ready dicts with tool context."""
        result: list[dict[str, Any]] = []

        for msg, parts in messages_with_parts:
            tool_call_parts = [
                p for p in parts if p.part_type == MessagePartType.TOOL_CALL.value
            ]
            tool_result_parts = [
                p for p in parts if p.part_type == MessagePartType.TOOL_RESULT.value
            ]

            if msg.role == "assistant" and tool_call_parts:
                # Build assistant message with tool_calls
                tool_calls: list[dict[str, Any]] = []
                for tc in tool_call_parts:
                    tool_calls.append({
                        "id": tc.tool_call_id or "",
                        "name": tc.tool_name or "",
                        "arguments": tc.content,
                    })
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": msg.content or None,
                    "tool_calls": tool_calls,
                }
                result.append(assistant_msg)

                # Emit tool result messages
                result_by_id = {
                    p.tool_call_id: p for p in tool_result_parts
                }
                for tc in tool_call_parts:
                    tr = result_by_id.get(tc.tool_call_id)
                    if tr is not None:
                        result.append({
                            "role": "tool",
                            "content": tr.content,
                            "tool_call_id": tr.tool_call_id,
                        })
                    else:
                        # Interrupted session — no result stored for this call
                        result.append({
                            "role": "tool",
                            "content": "[Error: tool call was interrupted and produced no result]",
                            "tool_call_id": tc.tool_call_id or "",
                        })
            else:
                # Plain user/system/assistant message
                result.append({"role": msg.role, "content": msg.content})

        return result

    def _build_context_info(
        self, messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build context usage info for the client."""
        config = get_config()
        messages_tokens = estimate_messages_tokens(messages)
        tools_tokens = estimate_tool_schemas_tokens(self._tool_schemas)
        return {
            "used": messages_tokens + tools_tokens,
            "max": config.max_context_tokens,
            "messages_tokens": messages_tokens,
            "tools_tokens": tools_tokens,
        }

    async def process(
        self,
        user_message: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Process a user message and yield stream events.

        ``attachments`` carry file excerpts the user highlighted in the
        Explore Files UI; they are injected into the system prompt for
        this turn only and are never persisted.
        """
        # Persist user message
        self._session_mgr.add_message(
            session_id=self._session_id,
            role=MessageRole.USER,
            content=user_message,
        )

        # Reserve the assistant message now so its created_at reflects when
        # the response started, not when streaming finished.  Content and
        # parts are filled in the finally block below.
        assistant_msg = self._session_mgr.add_message(
            session_id=self._session_id,
            role=MessageRole.ASSISTANT,
            content="",
        )

        # Surface the reserved message id so the streaming UI can attach
        # per-message actions (copy/fork) before the turn finishes.
        yield StreamEvent("message_start", message_id=assistant_msg.id)

        # Auto-name untitled sessions from the first user message
        session = self._session_mgr.get_session(self._session_id)
        if session and not session.title:
            title = _derive_title(user_message)
            if title:
                self._session_mgr.update_title(self._session_id, title)

        # Build extra context (inject plan if enabled)
        extra_context = ""
        if session and session.plan_in_context:
            plan_path = ConfigPaths.project_runtime_dir(self._project_dir) / "plan.md"
            if plan_path.is_file():
                plan_text = plan_path.read_text()
                extra_context = (
                    "## Active Plan\n"
                    "The following plan has been approved by the user. "
                    "Follow it step by step:\n\n" + plan_text
                )

        # Append user-attached file excerpts (this turn only, not persisted).
        if attachments:
            excerpt_blocks: list[str] = []
            for att in attachments:
                path = str(att.get("path", "")).strip()
                start = int(att.get("start_line", 0) or 0)
                end = int(att.get("end_line", 0) or 0)
                text = str(att.get("text", ""))
                if not path or not text:
                    continue
                lines_label = (
                    f"lines {start}-{end}" if end > start else f"line {start}"
                )
                excerpt_blocks.append(
                    f"### {path}  ({lines_label})\n```\n{text}\n```"
                )
            if excerpt_blocks:
                excerpts_section = (
                    "## User-attached file excerpts (this turn only)\n"
                    "The user highlighted the following ranges in the Explore "
                    "Files panel for this reply:\n\n" + "\n\n".join(excerpt_blocks)
                )
                extra_context = (
                    excerpts_section if not extra_context
                    else extra_context + "\n\n" + excerpts_section
                )

        tool_schemas = self._tool_schemas

        # Build agent loop
        loop = AgentLoop(
            agent=self._agent,
            provider=self._provider,
            tool_executor=self._tool_executor,
            tool_schemas=tool_schemas,
            project_dir=self._project_dir,
            permission_engine=self._permission_engine,
            permission_broker=self._permission_broker,
            plan_broker=self._plan_broker,
            ask_user_broker=self._ask_user_broker,
            session_id=self._session_id,
            cancel_event=self._cancel_event,
            extra_context=extra_context,
        )

        # Load existing messages with parts (for tool context reconstruction)
        messages_with_parts = self._session_mgr.get_messages_with_parts(
            self._session_id
        )
        # Exclude the just-added user message and the empty assistant placeholder
        messages_for_llm = self._reconstruct_llm_messages(
            messages_with_parts[:-2]
        )
        loop.messages = messages_for_llm

        # Check if compaction is needed
        config = get_config()
        threshold = int(config.max_context_tokens * config.compaction_threshold)
        tool_overhead = estimate_tool_schemas_tokens(self._tool_schemas)
        estimated = estimate_messages_tokens(loop.messages) + tool_overhead
        if estimated > threshold:
            compacted, was_compacted, pruned = compact_messages(
                loop.messages,
                max_tokens=threshold,
                keep_recent_tokens=min(40_000, threshold // 2),
                tool_tokens=tool_overhead,
            )
            if was_compacted:
                log.info("Context compacted for session %s", self._session_id)
                # Try to replace the placeholder with an LLM summary
                if pruned:
                    try:
                        async def _llm_call(
                            msgs: list[dict[str, Any]], max_tokens: int
                        ) -> str:
                            # Tag this LLM call for the debug dumper; the
                            # Provider reads llm_debug_context and forwards
                            # the payload to dump_llm_request.
                            llm_debug_context.set(LLMDebugContext(
                                source="compaction",
                                step=0,
                                project_dir=self._project_dir,
                            ))
                            resp = await self._provider.chat_sync(
                                messages=msgs,  # type: ignore[arg-type]
                                model=self._agent.model or config.providers[0].default_model,
                                max_tokens=max_tokens,
                            )
                            return str(resp.choices[0].message.content or "")

                        summary = await summarize_for_compaction(
                            pruned,
                            _llm_call,
                            max_summary_tokens=config.compaction_summary_max_tokens,
                        )
                        if summary:
                            for m in compacted:
                                if m.get("_compaction_placeholder"):
                                    m["content"] = (
                                        f"[Summary of earlier conversation — use this "
                                        f"to maintain continuity]\n\n{summary}"
                                    )
                                    m.pop("_compaction_placeholder", None)
                                    break
                    except Exception:
                        log.warning(
                            "Compaction summary failed, using placeholder",
                            exc_info=True,
                        )
                        # Clean up marker from placeholder
                        for m in compacted:
                            m.pop("_compaction_placeholder", None)
                # Clean up any remaining markers
                for m in compacted:
                    m.pop("_compaction_placeholder", None)
                loop.messages = compacted

        # Emit initial context usage
        yield StreamEvent(
            "context_update",
            context_info=self._build_context_info(loop.messages),
        )

        # Run agent loop — persist tool_call/tool_result parts as soon as
        # they stream so the file-events endpoint can serve real diffs
        # (with proper line numbers and 3 lines of context) for in-flight
        # turns, instead of forcing the dialog to a hunk-relative client
        # fallback. Text chunks are still buffered between non-text events
        # to avoid one DB row per token. The finally below preserves the
        # existing safety net for partial responses on client abort.
        full_response = ""
        pending_text = ""
        wrote_any_part = False

        def _flush_pending_text() -> str:
            """Persist any buffered text as a TEXT part and return its id.

            Returns the persisted MessagePart id, or "" if nothing was
            flushed. The id is surfaced via a ``part_persisted`` SSE event
            so the live streaming bubble can stamp ``data-last-part-id``
            in real time — required for fork-from-bubble on a turn that's
            still in progress."""
            nonlocal pending_text, wrote_any_part
            if not pending_text:
                return ""
            part = self._session_mgr.add_message_part(
                assistant_msg.id,
                MessagePartType.TEXT,
                content=pending_text,
            )
            pending_text = ""
            wrote_any_part = True
            return part.id

        try:
            async for event in loop.run(user_message):
                flushed_text_id = ""
                if event.type == "text":
                    full_response += event.content
                    pending_text += event.content
                elif event.type == "tool_call" and event.tool_call:
                    flushed_text_id = _flush_pending_text()
                    tc_part = self._session_mgr.add_message_part(
                        assistant_msg.id,
                        MessagePartType.TOOL_CALL,
                        content=event.tool_call.arguments_raw,
                        tool_name=event.tool_call.name,
                        tool_call_id=event.tool_call.id,
                    )
                    event.part_id = tc_part.id
                    wrote_any_part = True
                elif event.type == "tool_result" and event.tool_call:
                    flushed_text_id = _flush_pending_text()
                    metadata_json = json.dumps(event.metadata) if event.metadata else "{}"
                    tr_part = self._session_mgr.add_message_part(
                        assistant_msg.id,
                        MessagePartType.TOOL_RESULT,
                        content=event.tool_result,
                        tool_name=event.tool_call.name,
                        tool_call_id=event.tool_call.id,
                        metadata_json=metadata_json,
                    )
                    event.part_id = tr_part.id
                    wrote_any_part = True
                elif event.type in ("done", "error"):
                    # Flush trailing text now so the final part_persisted
                    # arrives BEFORE the bubble finalizes on the client.
                    flushed_text_id = _flush_pending_text()
                # subagent_* events are streamed to the frontend in real-time
                # but don't need separate persistence — the delegate tool_result's
                # metadata["subagent_steps"] already covers page-reload rendering.

                if flushed_text_id:
                    yield StreamEvent(
                        "part_persisted",
                        message_id=assistant_msg.id,
                        part_id=flushed_text_id,
                    )
                yield event

                # Emit context update after tool_result, done, or error
                if event.type in ("tool_result", "done", "error"):
                    yield StreamEvent(
                        "context_update",
                        context_info=self._build_context_info(loop.messages),
                    )
        finally:
            # Flush any trailing text and update the pre-reserved assistant
            # message's content. finally also runs on client abort /
            # GeneratorExit, so partial responses still land in the DB.
            _flush_pending_text()
            if full_response or wrote_any_part:
                self._session_mgr.update_message_content(
                    assistant_msg.id, full_response
                )
            else:
                # No response generated — remove the empty placeholder
                self._session_mgr.delete_message(assistant_msg.id)
