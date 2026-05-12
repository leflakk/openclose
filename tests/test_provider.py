"""Tests for the provider system."""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from openai import BadRequestError

from openclose.provider.auth import load_api_key
from openclose.provider.models import ModelInfo, ModelRegistry
from openclose.provider.provider import Provider


def test_load_api_key_from_env() -> None:
    """Should load API key from environment."""
    os.environ["OPENCLOSE_API_KEY"] = "test-key-123"
    try:
        key = load_api_key()
        assert key == "test-key-123"
    finally:
        del os.environ["OPENCLOSE_API_KEY"]


def test_load_api_key_fallback_openai() -> None:
    """Should fall back to OPENAI_API_KEY."""
    # Clear OPENCLOSE_API_KEY if set
    os.environ.pop("OPENCLOSE_API_KEY", None)
    os.environ["OPENAI_API_KEY"] = "openai-key-456"
    try:
        key = load_api_key()
        assert key == "openai-key-456"
    finally:
        del os.environ["OPENAI_API_KEY"]


def test_provider_creation() -> None:
    """Provider should initialize with given params."""
    provider = Provider(
        base_url="http://localhost:8080/v1",
        api_key="test-key",
        provider_name="test",
    )
    assert provider.client is not None
    assert provider.client.base_url == "http://localhost:8080/v1/"


def test_model_registry() -> None:
    """ModelRegistry should register and retrieve models."""
    registry = ModelRegistry()
    model = ModelInfo(id="test-model", name="Test Model", context_window=32_000)
    registry.register(model)

    assert registry.get("test-model") is model
    assert registry.get("nonexistent") is None
    assert len(registry.list_models()) == 1


def test_model_registry_get_or_default() -> None:
    """get_or_default should create a default entry for unknown models."""
    registry = ModelRegistry()
    model = registry.get_or_default("unknown-model")
    assert model.id == "unknown-model"
    assert model.context_window == 128_000  # default


# ── tool_choice forwarding ───────────────────────────────────────────────────

def _make_bad_request_error(message: str) -> BadRequestError:
    """Build a BadRequestError without hitting the real API."""
    request = httpx.Request("POST", "http://test/v1/chat/completions")
    response = httpx.Response(400, request=request)
    return BadRequestError(message=message, response=response, body={"message": message})


def _build_chat_provider_with_capture() -> tuple[Provider, list[dict[str, Any]]]:
    """Make a Provider whose `chat.completions.create` records its kwargs."""
    captured: list[dict[str, Any]] = []
    provider = Provider(base_url="http://test/v1", api_key="k", provider_name="t")
    fake_create = AsyncMock()

    async def _stream() -> Any:
        # Empty async iterator — no chunks needed; the caller exits cleanly.
        if False:
            yield None  # pragma: no cover

    fake_create.return_value = _stream()

    async def capturing_create(**kwargs: Any) -> Any:
        captured.append(kwargs)
        return _stream()

    fake = MagicMock()
    fake.chat = MagicMock()
    fake.chat.completions = MagicMock()
    fake.chat.completions.create = AsyncMock(side_effect=capturing_create)
    provider._client = fake
    return provider, captured


@pytest.mark.asyncio
async def test_chat_forwards_tool_choice_when_set() -> None:
    """`tool_choice` is added to kwargs when both tools and tool_choice are set."""
    provider, captured = _build_chat_provider_with_capture()
    tools = [{"name": "grep", "description": "x", "parameters": {"type": "object"}}]
    async for _ in provider.chat(
        messages=[{"role": "user", "content": "hi"}],
        model="m",
        tools=tools,
        tool_choice="required",
    ):
        pass
    assert len(captured) == 1
    assert captured[0].get("tool_choice") == "required"


@pytest.mark.asyncio
async def test_chat_omits_tool_choice_when_none() -> None:
    """`tool_choice=None` (default) means the kwarg is not sent."""
    provider, captured = _build_chat_provider_with_capture()
    tools = [{"name": "grep", "description": "x", "parameters": {"type": "object"}}]
    async for _ in provider.chat(
        messages=[{"role": "user", "content": "hi"}],
        model="m",
        tools=tools,
    ):
        pass
    assert "tool_choice" not in captured[0]


@pytest.mark.asyncio
async def test_chat_omits_tool_choice_when_no_tools() -> None:
    """No tools means no tool_choice — even if caller passed one."""
    provider, captured = _build_chat_provider_with_capture()
    async for _ in provider.chat(
        messages=[{"role": "user", "content": "hi"}],
        model="m",
        tools=None,
        tool_choice="required",
    ):
        pass
    assert "tool_choice" not in captured[0]
    assert "tools" not in captured[0]


@pytest.mark.asyncio
async def test_chat_retries_without_tool_choice_on_bad_request() -> None:
    """Endpoint rejects `tool_choice` → retry once without it."""
    provider = Provider(base_url="http://test/v1", api_key="k", provider_name="t")
    captured: list[dict[str, Any]] = []
    call_count = {"n": 0}

    async def _stream() -> Any:
        if False:
            yield None  # pragma: no cover

    async def flaky_create(**kwargs: Any) -> Any:
        captured.append(kwargs)
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _make_bad_request_error("Unknown parameter: tool_choice")
        return _stream()

    fake = MagicMock()
    fake.chat = MagicMock()
    fake.chat.completions = MagicMock()
    fake.chat.completions.create = AsyncMock(side_effect=flaky_create)
    provider._client = fake

    tools = [{"name": "grep", "description": "x", "parameters": {"type": "object"}}]
    async for _ in provider.chat(
        messages=[{"role": "user", "content": "hi"}],
        model="m",
        tools=tools,
        tool_choice="required",
    ):
        pass

    assert call_count["n"] == 2
    assert captured[0].get("tool_choice") == "required"
    assert "tool_choice" not in captured[1]


def test_wrap_tool_calls_in_messages_normalizes_invalid_json() -> None:
    """Invalid JSON arguments should be normalized to '{}' to prevent API 400."""
    msgs = [{
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c0", "name": "glob", "arguments": '{"x": '},  # broken
            {"id": "c1", "name": "read", "arguments": '{"file_path": "x.py"}'},
        ],
    }]
    out = Provider._wrap_tool_calls_in_messages(msgs)
    assert out[0]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert out[0]["tool_calls"][1]["function"]["arguments"] == '{"file_path": "x.py"}'
    # Other tool_call fields preserved
    assert out[0]["tool_calls"][0]["function"]["name"] == "glob"
    assert out[0]["tool_calls"][0]["id"] == "c0"
    assert out[0]["tool_calls"][0]["type"] == "function"


def test_wrap_tool_calls_in_messages_passes_empty_arguments() -> None:
    """Empty arguments string should pass through unchanged (model intent)."""
    msgs = [{
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "c0", "name": "glob", "arguments": ""},
        ],
    }]
    out = Provider._wrap_tool_calls_in_messages(msgs)
    assert out[0]["tool_calls"][0]["function"]["arguments"] == ""


@pytest.mark.asyncio
async def test_chat_does_not_retry_on_unrelated_bad_request() -> None:
    """A 400 unrelated to `tool_choice` propagates without retry."""
    provider = Provider(base_url="http://test/v1", api_key="k", provider_name="t")
    call_count = {"n": 0}

    async def angry_create(**kwargs: Any) -> Any:
        call_count["n"] += 1
        raise _make_bad_request_error("Model not found")

    fake = MagicMock()
    fake.chat = MagicMock()
    fake.chat.completions = MagicMock()
    fake.chat.completions.create = AsyncMock(side_effect=angry_create)
    provider._client = fake

    tools = [{"name": "grep", "description": "x", "parameters": {"type": "object"}}]
    with pytest.raises(BadRequestError):
        async for _ in provider.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="m",
            tools=tools,
            tool_choice="required",
        ):
            pass

    assert call_count["n"] == 1  # no retry
