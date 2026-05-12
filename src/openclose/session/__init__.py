"""Session management — CRUD, processing, compaction."""

from openclose.session.session import SessionManager
from openclose.session.message import MessageRole, MessagePartType
from openclose.session.processor import SessionProcessor
from openclose.session.compaction import (
    compact_messages,
    estimate_messages_tokens,
    estimate_tool_schemas_tokens,
    summarize_for_compaction,
)

__all__ = [
    "SessionManager",
    "MessageRole",
    "MessagePartType",
    "SessionProcessor",
    "compact_messages",
    "estimate_messages_tokens",
    "estimate_tool_schemas_tokens",
    "summarize_for_compaction",
]
