"""Tests for ID generation."""

from openclose.id import (
    generate_id,
    new_session_id,
    new_message_id,
    new_part_id,
    new_project_id,
)


def test_generate_id_is_string() -> None:
    id_ = generate_id()
    assert isinstance(id_, str)
    assert len(id_) == 26  # ULID length


def test_generate_id_unique() -> None:
    ids = {generate_id() for _ in range(100)}
    assert len(ids) == 100


def test_typed_ids() -> None:
    s = new_session_id()
    m = new_message_id()
    p = new_part_id()
    pr = new_project_id()
    assert len(s) == 26
    assert len(m) == 26
    assert len(p) == 26
    assert len(pr) == 26
    # All unique
    assert len({s, m, p, pr}) == 4
