"""Context compaction — summarize old messages when context is full."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from openclose.log import get_logger

log = get_logger(__name__)

# Try to load tiktoken for accurate token counting
_tiktoken_encoding: Any = None
try:
    import tiktoken

    _tiktoken_encoding = tiktoken.get_encoding("cl100k_base")
except Exception:
    pass


def estimate_tokens(text: str) -> int:
    """Estimate token count using tiktoken when available, heuristic fallback."""
    if _tiktoken_encoding is not None:
        return len(_tiktoken_encoding.encode(text))
    # Heuristic fallback — lower ratio for non-ASCII heavy text
    non_ascii = sum(1 for c in text[:500] if ord(c) > 127)
    if len(text[:500]) > 0 and non_ascii / len(text[:500]) > 0.1:
        return int(len(text) / 1.5) + 1
    return len(text) // 4 + 1


def _get_content_str(msg: dict[str, Any]) -> str:
    """Extract string content from a message, including tool call arguments."""
    parts: list[str] = []
    content = msg.get("content", "")
    if isinstance(content, str) and content:
        parts.append(content)
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            if isinstance(tc, dict):
                parts.append(tc.get("name", ""))
                parts.append(tc.get("arguments", ""))
    return " ".join(parts)


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """Estimate total tokens across all messages."""
    total = 0
    for msg in messages:
        total += estimate_tokens(_get_content_str(msg))
        # Overhead for role, delimiters, tool call structure, etc.
        total += 7
    return total


def estimate_tool_schemas_tokens(schemas: list[dict[str, Any]]) -> int:
    """Estimate tokens consumed by tool/function schemas in the request."""
    if not schemas:
        return 0
    try:
        return estimate_tokens(json.dumps(schemas))
    except (TypeError, ValueError):
        return len(schemas) * 200


def compact_messages(
    messages: list[dict[str, Any]],
    max_tokens: int,
    keep_recent_tokens: int = 40_000,
    tool_tokens: int = 0,
) -> tuple[list[dict[str, Any]], bool, list[dict[str, Any]]]:
    """Compact messages if they exceed the token limit.

    Strategy:
    1. Keep system messages intact.
    2. Keep the most recent messages (up to keep_recent_tokens).
    3. Replace older messages with a summary placeholder.

    Args:
        messages: The conversation messages.
        max_tokens: Token budget (should already factor in the compaction threshold).
        keep_recent_tokens: How many tokens of recent messages to preserve.
        tool_tokens: Estimated tokens consumed by tool schemas (counted toward the
            total but not part of the messages themselves).

    Returns (compacted_messages, was_compacted, pruned_messages).
    """
    total = estimate_messages_tokens(messages) + tool_tokens
    if total <= max_tokens:
        return messages, False, []

    log.info(
        "Compacting messages: %d estimated tokens > %d max", total, max_tokens
    )

    # Separate system messages from the rest
    system_msgs: list[dict[str, Any]] = []
    other_msgs: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "system":
            system_msgs.append(msg)
        else:
            other_msgs.append(msg)

    # Keep recent messages from the end
    kept: list[dict[str, Any]] = []
    kept_tokens = 0
    for msg in reversed(other_msgs):
        msg_tokens = estimate_tokens(_get_content_str(msg))
        if kept_tokens + msg_tokens > keep_recent_tokens:
            break
        kept.insert(0, msg)
        kept_tokens += msg_tokens

    # Strip orphaned tool-result messages from the front of kept.
    # If the cut point fell between an assistant(tool_calls) message and its
    # tool results, the kept list starts with tool messages whose parent
    # assistant message was pruned — the LLM will reject these.
    while kept and kept[0].get("role") == "tool":
        kept.pop(0)

    # Build compacted message list
    pruned_count = len(other_msgs) - len(kept)
    pruned: list[dict[str, Any]] = other_msgs[:pruned_count]
    if pruned_count > 0:
        # Use a user/assistant pair instead of a system message so it works
        # with strict chat templates (Jinja, etc.) that only allow a single
        # system message at the start.
        summary_user: dict[str, Any] = {
            "role": "user",
            "content": (
                f"[Context compacted: {pruned_count} earlier messages were removed "
                f"to stay within the context window. Please use the summary below "
                f"to maintain continuity.]"
            ),
            "_compaction_placeholder": True,
        }
        summary_assistant: dict[str, Any] = {
            "role": "assistant",
            "content": (
                "Understood. I have the context from our earlier conversation "
                "and will continue seamlessly."
            ),
        }
        # Only include the assistant acknowledgment if the first kept message
        # isn't also assistant (strict templates reject consecutive same-role).
        if kept and kept[0].get("role") == "assistant":
            result = system_msgs + [summary_user] + kept
        else:
            result = system_msgs + [summary_user, summary_assistant] + kept
    else:
        result = system_msgs + kept

    log.info(
        "Compaction: %d -> %d messages, ~%d -> ~%d tokens",
        len(messages),
        len(result),
        total,
        estimate_messages_tokens(result),
    )
    return result, True, pruned


async def summarize_for_compaction(
    pruned_messages: list[dict[str, Any]],
    llm_call: Callable[[list[dict[str, Any]], int], Awaitable[str]],
    max_summary_tokens: int = 2000,
) -> str:
    """Summarize pruned messages using an LLM call.

    Args:
        pruned_messages: Messages that were removed during compaction.
        llm_call: Async callback ``(messages, max_tokens) -> str`` that calls the LLM.
        max_summary_tokens: Maximum tokens for the summary response.

    Returns:
        A concise summary of the pruned conversation.
    """
    if not pruned_messages:
        return ""

    # Build a transcript from pruned messages, capped to ~30K tokens
    lines: list[str] = []
    budget = 30_000
    for msg in pruned_messages:
        text = _get_content_str(msg)
        role = msg.get("role", "unknown")
        line = f"{role}: {text}"
        budget -= estimate_tokens(line)
        if budget < 0:
            lines.append("... (earlier messages truncated)")
            break
        lines.append(line)

    transcript = "\n".join(lines)
    prompt_messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are a conversation summarizer. Produce a concise summary of "
                "the following conversation excerpt. Focus on: key decisions made, "
                "tool actions taken and their results, important code changes, and "
                "any open questions or tasks. Be factual and brief."
            ),
        },
        {
            "role": "user",
            "content": f"Summarize this conversation:\n\n{transcript}",
        },
    ]

    return await llm_call(prompt_messages, max_summary_tokens)
