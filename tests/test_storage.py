"""Tests for the storage layer."""

from __future__ import annotations

from pathlib import Path

from sqlmodel import select

from openclose.storage.db import Database
from openclose.storage.schema import Session, Message, MessagePart, Project
from openclose.storage.migrations import get_current_version, apply_migrations


def test_database_creation(tmp_path: Path) -> None:
    """Database should initialize and create tables."""
    db = Database(tmp_path / "test.db")
    assert db.engine is not None


def test_session_crud(tmp_path: Path) -> None:
    """Should create, read, update sessions."""
    db = Database(tmp_path / "test.db")

    # Create
    with db.get_session() as s:
        session = Session(title="Test Session", agent="build")
        s.add(session)
        s.commit()
        session_id = session.id

    # Read
    with db.get_session() as s:
        loaded = s.get(Session, session_id)
        assert loaded is not None
        assert loaded.title == "Test Session"
        assert loaded.agent == "build"
        assert loaded.archived is False

    # Update
    with db.get_session() as s:
        loaded = s.get(Session, session_id)
        assert loaded is not None
        loaded.title = "Updated"
        s.add(loaded)
        s.commit()

    with db.get_session() as s:
        loaded = s.get(Session, session_id)
        assert loaded is not None
        assert loaded.title == "Updated"


def test_message_crud(tmp_path: Path) -> None:
    """Should create and query messages."""
    db = Database(tmp_path / "test.db")

    with db.get_session() as s:
        session = Session(title="Msg Test")
        s.add(session)
        s.commit()
        sid = session.id

    with db.get_session() as s:
        msg = Message(session_id=sid, role="user", content="Hello")
        s.add(msg)
        s.commit()
        mid = msg.id

    with db.get_session() as s:
        loaded = s.get(Message, mid)
        assert loaded is not None
        assert loaded.role == "user"
        assert loaded.content == "Hello"
        assert loaded.session_id == sid


def test_message_part_crud(tmp_path: Path) -> None:
    """Should create message parts."""
    db = Database(tmp_path / "test.db")

    with db.get_session() as s:
        session = Session(title="Part Test")
        s.add(session)
        s.commit()
        msg = Message(session_id=session.id, role="assistant")
        s.add(msg)
        s.commit()
        mid = msg.id

    with db.get_session() as s:
        part = MessagePart(
            message_id=mid,
            part_type="text",
            content="Hello world",
        )
        s.add(part)
        s.commit()

    with db.get_session() as s:
        parts = s.exec(
            select(MessagePart).where(MessagePart.message_id == mid)
        ).all()
        assert len(parts) == 1
        assert parts[0].part_type == "text"
        assert parts[0].content == "Hello world"


def test_project_crud(tmp_path: Path) -> None:
    """Should create projects."""
    db = Database(tmp_path / "test.db")

    with db.get_session() as s:
        project = Project(name="my-project", directory=str(tmp_path), vcs="git")
        s.add(project)
        s.commit()
        pid = project.id

    with db.get_session() as s:
        loaded = s.get(Project, pid)
        assert loaded is not None
        assert loaded.name == "my-project"
        assert loaded.vcs == "git"


def test_migrations(tmp_path: Path) -> None:
    """Migration system should stamp version on fresh database."""
    db = Database(tmp_path / "test.db")
    version = get_current_version(db.engine)
    assert version == 5  # Stamped during init_db for new databases
    applied = apply_migrations(db.engine)
    assert applied == 0  # Already at latest version
