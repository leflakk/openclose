"""Tests for the multi-provider factory + per-name cache."""

from __future__ import annotations

from typing import Iterator

import pytest

from openclose.config.schema import OpenCloseConfig, ProviderConfig
from openclose.provider.provider import (
    Provider,
    get_provider,
    make_provider,
    reset_provider_cache,
)


@pytest.fixture(autouse=True)
def _reset_provider_cache() -> Iterator[None]:
    """Ensure every test starts with a clean provider cache."""
    reset_provider_cache()
    yield
    reset_provider_cache()


def test_make_provider_openai_compatible_returns_provider() -> None:
    cfg = ProviderConfig(
        name="x",
        kind="openai_compatible",
        base_url="http://localhost:1234/v1",
        api_key="k",
    )
    inst = make_provider(cfg)
    assert isinstance(inst, Provider)


def test_make_provider_unknown_kind_raises() -> None:
    cfg = ProviderConfig(name="x", kind="anthropic")
    with pytest.raises(NotImplementedError) as excinfo:
        make_provider(cfg)
    assert "anthropic" in str(excinfo.value)


def test_get_provider_caches_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Distinct names return distinct instances; same name returns the cached one."""
    cfg_a = ProviderConfig(name="a", base_url="http://a/v1")
    cfg_b = ProviderConfig(name="b", base_url="http://b/v1")
    fake_config = OpenCloseConfig(providers=[cfg_a, cfg_b])
    monkeypatch.setattr(
        "openclose.provider.provider.get_config", lambda: fake_config,
    )

    pa1 = get_provider("a")
    pa2 = get_provider("a")
    pb = get_provider("b")

    assert pa1 is pa2          # cached
    assert pa1 is not pb       # distinct per-name


def test_get_provider_empty_name_uses_default_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_a = ProviderConfig(name="a", base_url="http://a/v1")
    cfg_b = ProviderConfig(name="b", base_url="http://b/v1")
    fake_config = OpenCloseConfig(providers=[cfg_a, cfg_b], default_provider="b")
    monkeypatch.setattr(
        "openclose.provider.provider.get_config", lambda: fake_config,
    )

    p = get_provider("")
    p_b = get_provider("b")
    assert p is p_b


def test_get_provider_empty_name_falls_back_to_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_a = ProviderConfig(name="a", base_url="http://a/v1")
    cfg_b = ProviderConfig(name="b", base_url="http://b/v1")
    fake_config = OpenCloseConfig(providers=[cfg_a, cfg_b], default_provider="")
    monkeypatch.setattr(
        "openclose.provider.provider.get_config", lambda: fake_config,
    )

    p = get_provider("")
    p_a = get_provider("a")
    assert p is p_a


def test_get_provider_unknown_name_falls_back_to_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_a = ProviderConfig(name="a", base_url="http://a/v1")
    fake_config = OpenCloseConfig(providers=[cfg_a])
    monkeypatch.setattr(
        "openclose.provider.provider.get_config", lambda: fake_config,
    )

    # Unknown name resolves to first provider's instance.
    p_unknown = get_provider("nonexistent")
    p_a = get_provider("a")
    assert p_unknown is p_a


def test_reset_provider_cache_drops_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = ProviderConfig(name="a", base_url="http://a/v1")
    fake_config = OpenCloseConfig(providers=[cfg])
    monkeypatch.setattr(
        "openclose.provider.provider.get_config", lambda: fake_config,
    )

    first = get_provider("a")
    reset_provider_cache()
    second = get_provider("a")
    assert first is not second
