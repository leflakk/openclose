"""Tests for the per-provider API key resolution chain."""

from __future__ import annotations

import pytest

from openclose.config.schema import OpenCloseConfig, ProviderConfig
from openclose.provider.auth import load_api_key


def _set_config(monkeypatch: pytest.MonkeyPatch, providers: list[ProviderConfig]) -> None:
    fake = OpenCloseConfig(providers=providers)
    monkeypatch.setattr(
        "openclose.provider.auth.get_config", lambda: fake,
    )


def test_api_key_env_takes_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch, [
        ProviderConfig(
            name="openrouter",
            api_key_env="OPENROUTER_API_KEY",
            api_key="inline-fallback",
        ),
    ])
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    monkeypatch.setenv("OPENCLOSE_API_KEY", "wrong-legacy")
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-openai")

    assert load_api_key("openrouter") == "from-env"


def test_inline_api_key_used_when_env_var_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_config(monkeypatch, [
        ProviderConfig(
            name="openrouter",
            api_key_env="OPENROUTER_API_KEY",   # name set but env not exported
            api_key="inline-key",
        ),
    ])
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENCLOSE_API_KEY", "legacy")  # should NOT win

    assert load_api_key("openrouter") == "inline-key"


def test_legacy_openclose_env_used_when_provider_keys_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_config(monkeypatch, [ProviderConfig(name="default")])
    monkeypatch.setenv("OPENCLOSE_API_KEY", "legacy-oc")
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-oa")

    assert load_api_key("default") == "legacy-oc"


def test_legacy_openai_env_used_as_last_resort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_config(monkeypatch, [ProviderConfig(name="default")])
    monkeypatch.delenv("OPENCLOSE_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")

    assert load_api_key("default") == "openai-key"


def test_returns_empty_when_nothing_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_config(monkeypatch, [ProviderConfig(name="default")])
    monkeypatch.delenv("OPENCLOSE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    assert load_api_key("default") == ""


def test_unknown_provider_falls_through_to_legacy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling load_api_key with a name not in config still tries legacy env."""
    _set_config(monkeypatch, [ProviderConfig(name="default")])
    monkeypatch.setenv("OPENCLOSE_API_KEY", "legacy")

    assert load_api_key("unknown") == "legacy"
