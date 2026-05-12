"""Additional tests for coverage gaps."""

from __future__ import annotations

from pathlib import Path
from typing import Any


from openclose.agent.agent import Agent, get_agent
from openclose.agent.prompt import build_system_prompt
from openclose.config.config import load_config, _env_overrides, _merge_dicts
from openclose.config.paths import ConfigPaths
from openclose.file.binary import is_binary
from openclose.file.diff import DiffTracker
from openclose.file.ignore import IgnoreManager
from openclose.flag import _bool_flag, _int_flag
from openclose.log import setup_logging
from openclose.permission.permission import PermissionEngine
from openclose.provider.auth import load_api_key
from openclose.provider.models import ModelRegistry, get_model_registry
from openclose.session.compaction import compact_messages
from openclose.session.prompt import build_message_history
from openclose.session.session import SessionManager
from openclose.session.message import MessageRole
from openclose.storage.db import Database
from openclose.storage.migrations import apply_migrations, get_current_version


# --- flag ---

def test_bool_flag_true() -> None:
    import os
    os.environ["TEST_FLAG_BOOL"] = "true"
    try:
        assert _bool_flag("TEST_FLAG_BOOL")
    finally:
        del os.environ["TEST_FLAG_BOOL"]


def test_bool_flag_false() -> None:
    import os
    os.environ["TEST_FLAG_BOOL2"] = "false"
    try:
        assert not _bool_flag("TEST_FLAG_BOOL2")
    finally:
        del os.environ["TEST_FLAG_BOOL2"]


def test_bool_flag_default() -> None:
    assert not _bool_flag("NONEXISTENT_FLAG")
    assert _bool_flag("NONEXISTENT_FLAG", default=True)


def test_int_flag() -> None:
    import os
    os.environ["TEST_INT"] = "42"
    try:
        assert _int_flag("TEST_INT", 0) == 42
    finally:
        del os.environ["TEST_INT"]


def test_int_flag_default() -> None:
    assert _int_flag("NONEXISTENT_INT", 99) == 99


def test_int_flag_invalid() -> None:
    import os
    os.environ["TEST_INT_BAD"] = "not_a_number"
    try:
        assert _int_flag("TEST_INT_BAD", 10) == 10
    finally:
        del os.environ["TEST_INT_BAD"]


# --- log ---

def test_setup_logging() -> None:
    setup_logging()  # Should not raise


# --- config ---

def test_merge_dicts() -> None:
    base = {"a": 1, "b": {"c": 2}}
    override = {"b": {"d": 3}, "e": 4}
    result = _merge_dicts(base, override)
    assert result["a"] == 1
    assert result["b"]["c"] == 2
    assert result["b"]["d"] == 3
    assert result["e"] == 4


def test_env_overrides() -> None:
    import os
    os.environ["OPENCLOSE_TEST_KEY"] = "val"
    try:
        overrides = _env_overrides()
        assert "test_key" in overrides
    finally:
        del os.environ["OPENCLOSE_TEST_KEY"]


def test_config_paths_ensure_dirs() -> None:
    ConfigPaths.ensure_dirs()  # Should not raise
    assert ConfigPaths.config_dir().is_dir()


# --- provider ---

def test_load_api_key_from_config() -> None:
    import os
    os.environ.pop("OPENCLOSE_API_KEY", None)
    os.environ.pop("OPENAI_API_KEY", None)
    key = load_api_key("nonexistent_provider")
    assert key == ""  # No key found


def test_model_registry_global() -> None:
    reg = get_model_registry()
    assert isinstance(reg, ModelRegistry)


# --- permission ---

def test_permission_from_config() -> None:
    load_config()
    engine = PermissionEngine.from_config()
    assert isinstance(engine, PermissionEngine)


# --- agent ---

def test_agent_custom_prompt() -> None:
    agent = Agent(name="custom", system_prompt="You are a custom agent.")
    prompt = build_system_prompt(agent)
    assert "custom agent" in prompt


def test_agent_extra_context() -> None:
    agent = get_agent("build")
    prompt = build_system_prompt(agent, extra_context="Extra info here")
    assert "Extra info here" in prompt


# --- session prompt ---

def test_build_message_history(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    mgr = SessionManager(db)
    s = mgr.create_session()
    mgr.add_message(s.id, MessageRole.USER, content="Hello")
    mgr.add_message(s.id, MessageRole.ASSISTANT, content="Hi!")
    history = build_message_history(mgr, s.id)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[1]["content"] == "Hi!"


# --- diff tracker extended ---

def test_diff_tracker_get_all_diffs(tmp_path: Path) -> None:
    f1 = tmp_path / "a.txt"
    f2 = tmp_path / "b.txt"
    f1.write_text("old1\n")
    f2.write_text("old2\n")

    tracker = DiffTracker()
    tracker.snapshot(str(f1))
    tracker.snapshot(str(f2))

    f1.write_text("new1\n")
    f2.write_text("new2\n")
    tracker.record_change(str(f1))
    tracker.record_change(str(f2))

    diffs = tracker.get_all_diffs()
    assert "+new1" in diffs
    assert "+new2" in diffs


def test_diff_tracker_clear(tmp_path: Path) -> None:
    tracker = DiffTracker()
    tracker.snapshot(str(tmp_path / "x"))
    tracker.clear()
    assert tracker.get_changes() == []


def test_diff_tracker_get_nonexistent() -> None:
    tracker = DiffTracker()
    assert tracker.get_diff("nonexistent") is None


# --- ignore reload ---

def test_ignore_reload(tmp_path: Path) -> None:
    mgr = IgnoreManager(tmp_path)
    (tmp_path / ".gitignore").write_text("*.tmp\n")
    mgr.reload()
    assert mgr.is_ignored(tmp_path / "test.tmp")


# --- binary null bytes fallback ---

def test_not_binary_text_file(tmp_path: Path) -> None:
    f = tmp_path / "text.txt"
    f.write_text("just text no nulls")
    assert not is_binary(f)


# --- compaction edge cases ---

def test_compact_messages_system_preserved() -> None:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "You are helpful."},
    ]
    for i in range(50):
        messages.append({"role": "user", "content": "x" * 5000})
        messages.append({"role": "assistant", "content": "y" * 5000})

    result, compacted, _ = compact_messages(messages, max_tokens=1000, keep_recent_tokens=500)
    assert compacted
    # System message should be first
    assert result[0]["role"] == "system"
    assert result[0]["content"] == "You are helpful."


# --- migrations ---

def test_migrations_version_tracking(tmp_path: Path) -> None:
    db = Database(tmp_path / "mig.db")
    v = get_current_version(db.engine)
    assert v == 5  # Stamped during init_db for new databases
    applied = apply_migrations(db.engine)
    assert applied == 0  # Already at latest version
