"""Message types and roles."""

from __future__ import annotations

from enum import Enum


class MessageRole(Enum):
    """Message role in a conversation."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class MessagePartType(Enum):
    """Types of message parts."""

    TEXT = "text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    REASONING = "reasoning"
    FILE = "file"
