"""Tests for skills.builder — LLM JSON parsing + form coercion."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openclose.config.config import load_config
from openclose.config.paths import ConfigPaths
from openclose.skills.builder import (
    SENSITIVE_TOOLS,
    SkillBuilderError,
    _coerce_form,
    _extract_json_object,
    _serialize_conversation,
    generate_skill_form,
)


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(
        ConfigPaths, "project_runtime_dir",
        classmethod(lambda cls, project_dir: tmp_path),
    )
    load_config(project_dir=tmp_path)
    yield tmp_path


# ───────────────────────── _serialize_conversation ────────────────

def test_serialize_conversation_passes_user_and_assistant() -> None:
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    out = _serialize_conversation(messages)
    assert "hello" in out
    assert "hi" in out


def test_serialize_conversation_assistant_with_tool_calls() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "running a tool",
            "tool_calls": [
                {"name": "bash", "arguments": '{"cmd": "ls"}'},
            ],
        },
    ]
    out = _serialize_conversation(messages)
    assert "bash" in out
    assert "running a tool" in out
    assert "tool_calls" in out


def test_serialize_conversation_tool_messages() -> None:
    messages = [
        {"role": "tool", "tool_call_id": "tc1", "content": "42"},
    ]
    out = _serialize_conversation(messages)
    assert "tc1" in out
    assert "42" in out


def test_serialize_conversation_truncates_when_huge() -> None:
    huge = [{"role": "user", "content": "x" * 100_000}]
    out = _serialize_conversation(huge, max_chars=1000)
    assert "[...truncated]" in out
    assert len(out) < 100_000


# ───────────────────────── _extract_json_object ────────────────────

def test_extract_json_object_bare() -> None:
    obj = _extract_json_object('{"a": 1}')
    assert obj == {"a": 1}


def test_extract_json_object_with_fence() -> None:
    obj = _extract_json_object('```json\n{"a": 1}\n```')
    assert obj == {"a": 1}


def test_extract_json_object_with_bare_fence() -> None:
    obj = _extract_json_object('```\n{"a": 1}\n```')
    assert obj == {"a": 1}


def test_extract_json_object_with_leading_prose() -> None:
    obj = _extract_json_object('Sure! here:\n{"a": 1}\n')
    assert obj == {"a": 1}


def test_extract_json_object_balanced_braces_in_strings() -> None:
    """Brace counting must treat `{`/`}` inside strings as literals."""
    obj = _extract_json_object('{"body": "has { and } chars"}')
    assert obj["body"] == "has { and } chars"


def test_extract_json_object_nested() -> None:
    obj = _extract_json_object('{"outer": {"inner": [1, 2]}}')
    assert obj["outer"]["inner"] == [1, 2]


def test_extract_json_object_escaped_quotes() -> None:
    obj = _extract_json_object(r'{"t": "he said \"hi\""}')
    assert obj["t"] == 'he said "hi"'


def test_extract_json_object_no_brace_raises() -> None:
    with pytest.raises(SkillBuilderError, match="No JSON object"):
        _extract_json_object("just prose here")


def test_extract_json_object_unterminated_raises() -> None:
    with pytest.raises(SkillBuilderError, match="Unterminated"):
        _extract_json_object('{"a": {')


def test_extract_json_object_invalid_json_raises() -> None:
    with pytest.raises(SkillBuilderError, match="invalid"):
        _extract_json_object('{"a": not valid}')


# ───────────────────────── _coerce_form ────────────────────────────

def test_coerce_form_minimal() -> None:
    form = _coerce_form({"name": "X"})
    assert form.name == "X"
    assert form.slug == "x"  # slugify fallback


def test_coerce_form_missing_name_uses_untitled() -> None:
    form = _coerce_form({})
    assert form.name == "Untitled Skill"


def test_coerce_form_preserves_provided_slug() -> None:
    form = _coerce_form({"name": "Hi", "slug": "custom-slug"})
    assert form.slug == "custom-slug"


def test_coerce_form_parameters() -> None:
    raw = {
        "name": "X",
        "parameters": [
            {"name": "a", "type": "string", "required": True, "default": ""},
            {"name": "b", "type": "int", "required": False, "default": "7"},
        ],
    }
    form = _coerce_form(raw)
    names = [p.name for p in form.parameters]
    assert names == ["a", "b"]
    assert form.parameters[0].required is True


def test_coerce_form_skips_non_dict_parameters() -> None:
    raw = {"name": "X", "parameters": ["not a dict", {"name": "ok"}]}
    form = _coerce_form(raw)
    assert [p.name for p in form.parameters] == ["ok"]


def test_coerce_form_skips_invalid_parameter_type() -> None:
    raw = {
        "name": "X",
        "parameters": [{"name": "bad", "type": "unknown-type"}],
    }
    form = _coerce_form(raw)
    # Pydantic rejects the literal → parameter dropped
    assert form.parameters == []


def test_coerce_form_tools_use_sensitive_set() -> None:
    """The `sensitive` flag from LLM is ignored; we derive it from SENSITIVE_TOOLS."""
    raw = {
        "name": "X",
        "required_tools": [
            {"name": "bash", "sensitive": False},   # wrong — should become True
            {"name": "read", "sensitive": True},    # wrong — should become False
        ],
    }
    form = _coerce_form(raw)
    by_name = {t.name: t.sensitive for t in form.required_tools}
    assert by_name["bash"] is True
    assert by_name["read"] is False


def test_coerce_form_skips_empty_tool_name() -> None:
    raw = {"name": "X", "required_tools": [{"name": ""}, {"name": "read"}]}
    form = _coerce_form(raw)
    assert [t.name for t in form.required_tools] == ["read"]


def test_coerce_form_skips_non_dict_tools() -> None:
    raw = {"name": "X", "required_tools": ["not a dict", {"name": "read"}]}
    form = _coerce_form(raw)
    assert [t.name for t in form.required_tools] == ["read"]


def test_coerce_form_all_string_fields_present() -> None:
    form = _coerce_form({
        "name": "X",
        "goal": "a goal",
        "required_tools_prose": "prose",
        "procedure": "proc",
        "pitfalls": "pit",
        "verification": "ver",
    })
    assert form.goal == "a goal"
    assert form.required_tools_prose == "prose"
    assert form.procedure == "proc"
    assert form.pitfalls == "pit"
    assert form.verification == "ver"


def test_sensitive_tools_constant_not_empty() -> None:
    assert "bash" in SENSITIVE_TOOLS
    assert "write" in SENSITIVE_TOOLS


# ───────────────────────── generate_skill_form ──────────────────────

@pytest.mark.asyncio
async def test_generate_skill_form_missing_session_raises(runtime: Path) -> None:
    fake_mgr = MagicMock()
    fake_mgr.get_session.return_value = None

    with patch("openclose.skills.builder.SessionManager", return_value=fake_mgr):
        with pytest.raises(SkillBuilderError, match="Session not found"):
            await generate_skill_form("missing")


@pytest.mark.asyncio
async def test_generate_skill_form_empty_session_raises(runtime: Path) -> None:
    fake_mgr = MagicMock()
    fake_mgr.get_session.return_value = MagicMock()
    fake_mgr.get_messages_with_parts.return_value = []

    with patch("openclose.skills.builder.SessionManager", return_value=fake_mgr):
        with pytest.raises(SkillBuilderError, match="no messages"):
            await generate_skill_form("empty")


@pytest.mark.asyncio
async def test_generate_skill_form_no_model_raises(runtime: Path) -> None:
    fake_session = MagicMock()
    fake_session.model = ""
    fake_mgr = MagicMock()
    fake_mgr.get_session.return_value = fake_session
    fake_mgr.get_messages_with_parts.return_value = [("msg", [])]

    fake_provider = MagicMock()
    fake_provider.detect_model = AsyncMock(return_value="")
    fake_config = MagicMock()
    fake_config.providers = []

    with patch("openclose.skills.builder.SessionManager", return_value=fake_mgr), \
         patch("openclose.skills.builder.SessionProcessor") as Proc, \
         patch("openclose.skills.builder.get_provider", return_value=fake_provider), \
         patch("openclose.skills.builder.get_config", return_value=fake_config):
        Proc._reconstruct_llm_messages.return_value = [{"role": "user", "content": "hi"}]
        with pytest.raises(SkillBuilderError, match="No model"):
            await generate_skill_form("s1")


@pytest.mark.asyncio
async def test_generate_skill_form_empty_llm_raises(runtime: Path) -> None:
    fake_session = MagicMock()
    fake_session.model = "gpt-test"
    fake_mgr = MagicMock()
    fake_mgr.get_session.return_value = fake_session
    fake_mgr.get_messages_with_parts.return_value = [("msg", [])]

    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock()]
    fake_resp.choices[0].message.content = ""
    fake_provider = MagicMock()
    fake_provider.chat_sync = AsyncMock(return_value=fake_resp)
    fake_config = MagicMock()
    fake_config.providers = []

    with patch("openclose.skills.builder.SessionManager", return_value=fake_mgr), \
         patch("openclose.skills.builder.SessionProcessor") as Proc, \
         patch("openclose.skills.builder.get_provider", return_value=fake_provider), \
         patch("openclose.skills.builder.get_config", return_value=fake_config):
        Proc._reconstruct_llm_messages.return_value = [{"role": "user", "content": "hi"}]
        with pytest.raises(SkillBuilderError, match="empty"):
            await generate_skill_form("s1")


@pytest.mark.asyncio
async def test_generate_skill_form_success(runtime: Path) -> None:
    fake_session = MagicMock()
    fake_session.model = "gpt-test"
    fake_mgr = MagicMock()
    fake_mgr.get_session.return_value = fake_session
    fake_mgr.get_messages_with_parts.return_value = [("msg", [])]

    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock()]
    fake_resp.choices[0].message.content = (
        '{"name": "Daily", "goal": "a", "parameters": [], '
        '"required_tools": [{"name": "read"}], "procedure": "do"}'
    )
    fake_provider = MagicMock()
    fake_provider.chat_sync = AsyncMock(return_value=fake_resp)
    fake_config = MagicMock()
    fake_config.providers = []

    with patch("openclose.skills.builder.SessionManager", return_value=fake_mgr), \
         patch("openclose.skills.builder.SessionProcessor") as Proc, \
         patch("openclose.skills.builder.get_provider", return_value=fake_provider), \
         patch("openclose.skills.builder.get_config", return_value=fake_config):
        Proc._reconstruct_llm_messages.return_value = [{"role": "user", "content": "hi"}]
        form = await generate_skill_form("s1", user_prompt="focus on X")

    assert form.name == "Daily"
    assert form.slug == "daily"
    assert form.procedure == "do"
    # User prompt should have been woven into the message.
    passed = fake_provider.chat_sync.call_args.kwargs["messages"]
    user_msg = passed[1]["content"]
    assert "EXTRA_INSTRUCTIONS" in user_msg
    assert "focus on X" in user_msg
