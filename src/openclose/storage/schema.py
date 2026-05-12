"""SQLModel table definitions for all persistent entities."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel

from openclose.id import generate_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Session(SQLModel, table=True):
    """A conversation session."""

    __tablename__ = "sessions"

    id: str = Field(default_factory=generate_id, primary_key=True)
    title: str = ""
    agent: str = "build"
    provider: str = ""
    model: str = ""
    project_id: str = ""
    plan_in_context: bool = False
    video_compatible: bool = False
    archived: bool = False
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class Message(SQLModel, table=True):
    """A message within a session."""

    __tablename__ = "messages"

    id: str = Field(default_factory=generate_id, primary_key=True)
    session_id: str = Field(index=True)
    role: str  # "user", "assistant", "system", "tool"
    content: str = ""
    model: str = ""
    token_count: int = 0
    created_at: datetime = Field(default_factory=_now)


class MessagePart(SQLModel, table=True):
    """A structured part of a message (text, tool_call, tool_result, reasoning, file)."""

    __tablename__ = "message_parts"

    id: str = Field(default_factory=generate_id, primary_key=True)
    message_id: str = Field(index=True)
    part_type: str  # "text", "tool_call", "tool_result", "reasoning", "file"
    content: str = ""
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    metadata_json: str = "{}"
    created_at: datetime = Field(default_factory=_now)


class Project(SQLModel, table=True):
    """A tracked project/repository."""

    __tablename__ = "projects"

    id: str = Field(default_factory=generate_id, primary_key=True)
    name: str = ""
    directory: str
    vcs: str = ""  # "git", "hg", "pijul", ""
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
