"""ULID-based ID generation for all entities."""

from typing import NewType

from ulid import ULID


SessionID = NewType("SessionID", str)
MessageID = NewType("MessageID", str)
PartID = NewType("PartID", str)
ProjectID = NewType("ProjectID", str)


def generate_id() -> str:
    """Generate a new ULID string."""
    return str(ULID())


def new_session_id() -> SessionID:
    return SessionID(generate_id())


def new_message_id() -> MessageID:
    return MessageID(generate_id())


def new_part_id() -> PartID:
    return PartID(generate_id())


def new_project_id() -> ProjectID:
    return ProjectID(generate_id())
