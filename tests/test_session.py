"""Tests for session management."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openclose.storage.db import Database
from openclose.session.session import SessionManager
from openclose.session.message import MessageRole, MessagePartType
from openclose.session.compaction import (
    estimate_tokens,
    estimate_messages_tokens,
    compact_messages,
    estimate_tool_schemas_tokens,
    _get_content_str,
    summarize_for_compaction,
)
from openclose.session.processor import SessionProcessor


def test_session_create_and_get(tmp_path: Path) -> None:
    """Should create and retrieve a session."""
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    session = mgr.create_session(title="Test", agent="build")
    assert session.title == "Test"

    loaded = mgr.get_session(session.id)
    assert loaded is not None
    assert loaded.title == "Test"


def test_session_list(tmp_path: Path) -> None:
    """Should list sessions."""
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    mgr.create_session(title="S1")
    mgr.create_session(title="S2")
    sessions = mgr.list_sessions()
    assert len(sessions) == 2


def test_session_archive(tmp_path: Path) -> None:
    """Archived sessions should be excluded from default list."""
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    s = mgr.create_session(title="Archive Me")
    mgr.archive_session(s.id)
    assert len(mgr.list_sessions()) == 0
    assert len(mgr.list_sessions(include_archived=True)) == 1


def test_session_update_title(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    s = mgr.create_session(title="Old")
    mgr.update_title(s.id, "New")
    loaded = mgr.get_session(s.id)
    assert loaded is not None
    assert loaded.title == "New"


def test_add_and_get_messages(tmp_path: Path) -> None:
    """Should add and retrieve messages."""
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    s = mgr.create_session()
    mgr.add_message(s.id, MessageRole.USER, content="Hello")
    mgr.add_message(s.id, MessageRole.ASSISTANT, content="Hi there!")

    messages = mgr.get_messages(s.id)
    assert len(messages) == 2
    assert messages[0].role == "user"
    assert messages[1].role == "assistant"


def test_add_message_parts(tmp_path: Path) -> None:
    """Should add and retrieve message parts."""
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    s = mgr.create_session()
    msg = mgr.add_message(s.id, MessageRole.ASSISTANT)
    mgr.add_message_part(msg.id, MessagePartType.TEXT, content="Hello")
    mgr.add_message_part(
        msg.id,
        MessagePartType.TOOL_CALL,
        content='{"name": "read"}',
        tool_name="read",
        tool_call_id="tc_123",
    )
    parts = mgr.get_message_parts(msg.id)
    assert len(parts) == 2
    assert parts[0].part_type == "text"
    assert parts[1].tool_name == "read"


def test_delete_session(tmp_path: Path) -> None:
    """Should delete session and its messages."""
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    s = mgr.create_session()
    mgr.add_message(s.id, MessageRole.USER, content="test")
    assert mgr.delete_session(s.id)
    assert mgr.get_session(s.id) is None
    assert len(mgr.get_messages(s.id)) == 0


def test_get_empty_session_returns_empty(tmp_path: Path) -> None:
    """Should find a session with zero messages."""
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    s = mgr.create_session(title="Empty", agent="build")
    found = mgr.get_empty_session(agent="build")
    assert found is not None
    assert found.id == s.id


def test_get_empty_session_skips_non_empty(tmp_path: Path) -> None:
    """Should not return sessions that have messages."""
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    s = mgr.create_session(title="Has msgs", agent="build")
    mgr.add_message(s.id, MessageRole.USER, content="hi")
    assert mgr.get_empty_session(agent="build") is None


def test_get_empty_session_filters_by_agent(tmp_path: Path) -> None:
    """Should only return empty sessions matching the requested agent."""
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    mgr.create_session(title="Plan session", agent="plan")
    build = mgr.create_session(title="Build session", agent="build")
    found = mgr.get_empty_session(agent="build")
    assert found is not None
    assert found.id == build.id
    assert mgr.get_empty_session(agent="unknown") is None


def test_cleanup_empty_sessions(tmp_path: Path) -> None:
    """Should delete empty sessions but preserve sessions with messages."""
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    mgr.create_session(title="Empty1")
    mgr.create_session(title="Empty2")
    has_msg = mgr.create_session(title="HasMsg")
    mgr.add_message(has_msg.id, MessageRole.USER, content="hello")

    deleted = mgr.cleanup_empty_sessions()
    assert deleted == 2
    sessions = mgr.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].id == has_msg.id


def test_cleanup_empty_sessions_keeps_specified(tmp_path: Path) -> None:
    """Should keep the specified session even if it's empty."""
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    keep = mgr.create_session(title="Keep")
    mgr.create_session(title="Delete")

    deleted = mgr.cleanup_empty_sessions(keep_session_id=keep.id)
    assert deleted == 1
    sessions = mgr.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].id == keep.id


# --- Fix 1: Token estimation tests ---


def test_estimate_tokens() -> None:
    """Token estimation should return positive values."""
    assert estimate_tokens("hello") > 0
    assert estimate_tokens("a" * 400) > 0


def test_estimate_tokens_tiktoken() -> None:
    """With tiktoken installed, estimation should be accurate."""
    # tiktoken encodes "a" * 400 as 50 tokens (cl100k_base)
    result = estimate_tokens("a" * 400)
    assert result == 50


def test_estimate_tokens_short_text() -> None:
    """Should handle short text correctly."""
    assert estimate_tokens("") == 0
    assert estimate_tokens("hi") > 0


def test_estimate_messages_tokens() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "hello world"},
        {"role": "assistant", "content": "hi there"},
    ]
    tokens = estimate_messages_tokens(messages)
    assert tokens > 0


def test_get_content_str_with_tool_calls() -> None:
    """Should extract content from tool_calls arguments."""
    msg: dict[str, Any] = {
        "role": "assistant",
        "content": "Let me check.",
        "tool_calls": [
            {
                "id": "tc_1",
                "name": "read_file",
                "arguments": '{"path": "/tmp/test.py"}',
            }
        ],
    }
    text = _get_content_str(msg)
    assert "Let me check." in text
    assert "read_file" in text
    assert '{"path": "/tmp/test.py"}' in text


def test_get_content_str_plain_message() -> None:
    """Plain messages without tool_calls should still work."""
    msg: dict[str, Any] = {"role": "user", "content": "hello"}
    assert _get_content_str(msg) == "hello"


def test_get_content_str_empty() -> None:
    """Empty message should return empty string."""
    assert _get_content_str({}) == ""
    assert _get_content_str({"content": ""}) == ""


def test_estimate_tool_schemas_tokens() -> None:
    """Should estimate tokens for tool schemas."""
    schemas: list[dict[str, Any]] = [
        {
            "name": "read_file",
            "description": "Read a file from disk",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        }
    ]
    tokens = estimate_tool_schemas_tokens(schemas)
    assert tokens > 0


def test_estimate_tool_schemas_tokens_empty() -> None:
    """Empty schemas should return 0."""
    assert estimate_tool_schemas_tokens([]) == 0


# --- Fix 1: Compaction return value tests ---


def test_compact_messages_no_compaction_needed() -> None:
    """Should not compact if under limit."""
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    result, compacted, pruned = compact_messages(messages, max_tokens=100_000)
    assert not compacted
    assert len(result) == 2
    assert pruned == []


def test_compact_messages_over_limit() -> None:
    """Should compact when over limit."""
    messages: list[dict[str, Any]] = []
    for i in range(50):
        messages.append({"role": "user", "content": "x" * 5000})
        messages.append({"role": "assistant", "content": "y" * 5000})

    result, compacted, pruned = compact_messages(
        messages,
        max_tokens=1000,
        keep_recent_tokens=500,
    )
    assert compacted
    assert len(result) < len(messages)


def test_compact_messages_returns_pruned() -> None:
    """Compaction should return the pruned messages as the third element."""
    messages: list[dict[str, Any]] = []
    for i in range(50):
        messages.append({"role": "user", "content": "x" * 5000})
        messages.append({"role": "assistant", "content": "y" * 5000})

    result, compacted, pruned = compact_messages(
        messages,
        max_tokens=1000,
        keep_recent_tokens=500,
    )
    assert compacted
    assert len(pruned) > 0
    # pruned + kept (only original messages) should equal the original messages
    original_in_result = [m for m in result if m in messages]
    assert len(pruned) + len(original_in_result) == len(messages)


def test_compact_messages_placeholder_marker() -> None:
    """Compacted messages should contain a placeholder with the marker."""
    messages: list[dict[str, Any]] = []
    for i in range(50):
        messages.append({"role": "user", "content": "x" * 5000})
        messages.append({"role": "assistant", "content": "y" * 5000})

    result, compacted, pruned = compact_messages(
        messages,
        max_tokens=1000,
        keep_recent_tokens=500,
    )
    assert compacted
    placeholders = [m for m in result if m.get("_compaction_placeholder")]
    assert len(placeholders) == 1


def test_compact_messages_strips_orphaned_tool_messages() -> None:
    """Tool messages at the start of kept should be moved to pruned."""
    # Build a conversation: user, assistant(tool_calls), tool, tool, user, assistant
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "x" * 5000},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "tc1", "name": "bash", "arguments": "x" * 5000},
            ],
        },
        {"role": "tool", "content": "y" * 5000, "tool_call_id": "tc1"},
        {"role": "user", "content": "z" * 5000},
        {"role": "assistant", "content": "w" * 5000},
    ]
    # Set keep_recent_tokens so the cut falls between assistant(tool_calls) and tool
    result, compacted, pruned = compact_messages(
        messages,
        max_tokens=100,
        keep_recent_tokens=3500,
    )
    assert compacted
    # The kept portion should NOT start with a tool message
    non_system = [m for m in result if m.get("role") != "system"]
    assert non_system[0]["role"] != "tool"
    # The orphaned tool message should have been moved to pruned
    tool_in_pruned = [m for m in pruned if m.get("role") == "tool"]
    assert len(tool_in_pruned) >= 1


def test_compact_messages_tool_tokens_counted() -> None:
    """Passing tool_tokens should trigger compaction even if messages alone fit."""
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "x" * 4000},
        {"role": "assistant", "content": "y" * 4000},
    ]
    # Without tool_tokens, messages fit within max_tokens
    _, compacted_without, _ = compact_messages(
        messages, max_tokens=5000, keep_recent_tokens=1000, tool_tokens=0,
    )
    assert not compacted_without
    # With tool_tokens, total exceeds max_tokens → compaction triggers
    _, compacted_with, _ = compact_messages(
        messages, max_tokens=5000, keep_recent_tokens=1000, tool_tokens=4000,
    )
    assert compacted_with


# --- Fix 2: get_messages_with_parts tests ---


def test_get_messages_with_parts(tmp_path: Path) -> None:
    """Bulk query should return correct message-to-parts grouping."""
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    s = mgr.create_session()

    mgr.add_message(s.id, MessageRole.USER, content="Hello")
    msg2 = mgr.add_message(s.id, MessageRole.ASSISTANT, content="Hi")
    mgr.add_message_part(msg2.id, MessagePartType.TOOL_CALL, content='{}', tool_name="bash", tool_call_id="tc_1")
    mgr.add_message_part(msg2.id, MessagePartType.TOOL_RESULT, content="ok", tool_name="bash", tool_call_id="tc_1")

    result = mgr.get_messages_with_parts(s.id)
    assert len(result) == 2
    # First message (user) has no parts
    assert result[0][0].role == "user"
    assert result[0][1] == []
    # Second message (assistant) has 2 parts
    assert result[1][0].role == "assistant"
    assert len(result[1][1]) == 2


def test_get_messages_with_parts_empty_session(tmp_path: Path) -> None:
    """Should return empty list for session with no messages."""
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    s = mgr.create_session()
    assert mgr.get_messages_with_parts(s.id) == []


# --- Fix 2: Reconstruct LLM messages tests ---


def test_reconstruct_plain_messages(tmp_path: Path) -> None:
    """Plain user+assistant messages should reconstruct without tool_calls."""
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    s = mgr.create_session()
    mgr.add_message(s.id, MessageRole.USER, content="Hello")
    mgr.add_message(s.id, MessageRole.ASSISTANT, content="Hi there!")

    pairs = mgr.get_messages_with_parts(s.id)
    result = SessionProcessor._reconstruct_llm_messages(pairs)

    assert len(result) == 2
    assert result[0] == {"role": "user", "content": "Hello"}
    assert result[1] == {"role": "assistant", "content": "Hi there!"}


def test_reconstruct_with_tool_calls(tmp_path: Path) -> None:
    """Should reconstruct assistant + tool_calls + tool results."""
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    s = mgr.create_session()
    mgr.add_message(s.id, MessageRole.USER, content="Read file.txt")
    msg = mgr.add_message(s.id, MessageRole.ASSISTANT, content="Sure, let me read that.")
    mgr.add_message_part(
        msg.id, MessagePartType.TOOL_CALL,
        content='{"path": "file.txt"}', tool_name="read_file", tool_call_id="tc_42",
    )
    mgr.add_message_part(
        msg.id, MessagePartType.TOOL_RESULT,
        content="file contents here", tool_name="read_file", tool_call_id="tc_42",
    )

    pairs = mgr.get_messages_with_parts(s.id)
    result = SessionProcessor._reconstruct_llm_messages(pairs)

    assert len(result) == 3  # user, assistant+tool_calls, tool result
    assert result[0] == {"role": "user", "content": "Read file.txt"}
    # Assistant message should have tool_calls
    assert result[1]["role"] == "assistant"
    assert len(result[1]["tool_calls"]) == 1
    tc = result[1]["tool_calls"][0]
    assert tc["id"] == "tc_42"
    assert tc["name"] == "read_file"
    assert tc["arguments"] == '{"path": "file.txt"}'
    # Tool result message
    assert result[2]["role"] == "tool"
    assert result[2]["content"] == "file contents here"
    assert result[2]["tool_call_id"] == "tc_42"


def test_reconstruct_interrupted_session(tmp_path: Path) -> None:
    """Tool call without a result should get a synthetic error message."""
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    s = mgr.create_session()
    msg = mgr.add_message(s.id, MessageRole.ASSISTANT, content="")
    mgr.add_message_part(
        msg.id, MessagePartType.TOOL_CALL,
        content='{"cmd": "ls"}', tool_name="bash", tool_call_id="tc_99",
    )
    # No TOOL_RESULT — simulates interrupted session

    pairs = mgr.get_messages_with_parts(s.id)
    result = SessionProcessor._reconstruct_llm_messages(pairs)

    assert len(result) == 2  # assistant + synthetic tool result
    assert result[1]["role"] == "tool"
    assert "interrupted" in result[1]["content"].lower()
    assert result[1]["tool_call_id"] == "tc_99"


# --- Fix 3: Summarization tests ---


@pytest.mark.asyncio
async def test_summarize_for_compaction() -> None:
    """Should call LLM with pruned messages and return summary."""
    pruned: list[dict[str, Any]] = [
        {"role": "user", "content": "What is 2+2?"},
        {"role": "assistant", "content": "2+2 equals 4."},
    ]
    mock_llm = AsyncMock(return_value="User asked a math question. Answer: 4.")
    summary = await summarize_for_compaction(pruned, mock_llm, max_summary_tokens=500)

    assert summary == "User asked a math question. Answer: 4."
    mock_llm.assert_called_once()
    call_args = mock_llm.call_args
    # First arg is the messages list, second is max_tokens
    assert call_args[0][1] == 500
    prompt_msgs = call_args[0][0]
    assert len(prompt_msgs) == 2
    assert prompt_msgs[0]["role"] == "system"
    assert "summarize" in prompt_msgs[0]["content"].lower()


@pytest.mark.asyncio
async def test_summarize_for_compaction_empty() -> None:
    """Empty pruned messages should return empty string."""
    mock_llm = AsyncMock()
    summary = await summarize_for_compaction([], mock_llm)
    assert summary == ""
    mock_llm.assert_not_called()


@pytest.mark.asyncio
async def test_compaction_with_summary_fallback() -> None:
    """If LLM call fails, original placeholder should remain."""
    pruned: list[dict[str, Any]] = [
        {"role": "user", "content": "hello"},
    ]
    mock_llm = AsyncMock(side_effect=RuntimeError("LLM down"))
    with pytest.raises(RuntimeError):
        await summarize_for_compaction(pruned, mock_llm)
