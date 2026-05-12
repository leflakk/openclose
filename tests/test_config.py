"""Tests for the configuration system."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from openclose.config.config import load_config, ConfigManager
from openclose.config.paths import ConfigPaths
from openclose.config.schema import OpenCloseConfig


def test_load_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Loading config with no files should return defaults."""
    # Point user config dir to empty location so real config isn't picked up
    monkeypatch.setattr(ConfigPaths, "user_config_path", classmethod(lambda cls: tmp_path / "nonexistent" / "config.toml"))  # type: ignore[arg-type,unused-ignore]
    cfg = load_config()
    assert isinstance(cfg, OpenCloseConfig)
    assert cfg.default_agent == "build"
    assert len(cfg.providers) == 1
    assert cfg.providers[0].name == "default"


def test_load_with_project_dir(tmp_path: Path) -> None:
    """Config should pick up project_dir."""
    cfg = load_config(project_dir=tmp_path)
    assert cfg.project_dir == str(tmp_path)


def test_load_toml_config(tmp_path: Path) -> None:
    """Config should load from a TOML file."""
    config_dir = tmp_path / ".openclose"
    config_dir.mkdir()
    config_file = config_dir / "config.toml"
    config_file.write_text(
        'default_agent = "plan"\n'
    )
    cfg = load_config(project_dir=tmp_path)
    assert isinstance(cfg, OpenCloseConfig)
    assert cfg.default_agent == "plan"


def test_env_overrides(tmp_path: Path, monkeypatch: object) -> None:
    """Environment variables should override config."""
    os.environ["OPENCLOSE_DEFAULT_AGENT"] = "plan"
    try:
        cfg = load_config(project_dir=tmp_path)
        assert cfg.default_agent == "plan"
    finally:
        del os.environ["OPENCLOSE_DEFAULT_AGENT"]


def test_config_manager_reload(tmp_path: Path) -> None:
    """ConfigManager should support reload."""
    mgr = ConfigManager(project_dir=tmp_path)
    cfg1 = mgr.config
    cfg2 = mgr.reload()
    assert cfg1.default_agent == cfg2.default_agent


def test_config_paths() -> None:
    """ConfigPaths should return Path objects."""
    assert isinstance(ConfigPaths.config_dir(), Path)
    assert isinstance(ConfigPaths.data_dir(), Path)
    assert isinstance(ConfigPaths.cache_dir(), Path)
    assert isinstance(ConfigPaths.db_path(), Path)
    assert ConfigPaths.db_path().name == "openclose.db"


# ── [temperatures] section ───────────────────────────────────────────


def test_temperatures_defaults_match_previous_hardcoded() -> None:
    """Defaults must match the literals these knobs replaced; this is the
    regression guard if anyone changes a default without updating docs."""
    cfg = OpenCloseConfig()
    t = cfg.temperatures
    assert t.skills_runner == 0.1
    assert t.skills_builder == 0.1
    assert t.browser_vision_grounding == 0.0
    assert t.browser_vision_planner == 0.0
    assert t.browser_dom_planner == 0.0
    assert t.recorder_merger == 0.1
    assert t.recorder_task_builder == 0.1
    assert t.recorder_chunk_annotator == 0.2
    assert t.cron_nl == 0.0
    assert t.delegate == 0.0


def test_temperatures_partial_override_via_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A [temperatures] block overrides only listed fields; rest keep defaults."""
    # Isolate from a possibly-present user-level config that would override defaults
    monkeypatch.setattr(ConfigPaths, "user_config_path", classmethod(lambda cls: tmp_path / "nonexistent" / "config.toml"))  # type: ignore[arg-type,unused-ignore]
    config_dir = tmp_path / ".openclose"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        "[temperatures]\n"
        "skills_runner = 0.5\n"
        "browser_vision_grounding = 0.3\n"
    )
    cfg = load_config(project_dir=tmp_path)
    assert cfg.temperatures.skills_runner == 0.5
    assert cfg.temperatures.browser_vision_grounding == 0.3
    # Untouched fields keep defaults
    assert cfg.temperatures.cron_nl == 0.0
    assert cfg.temperatures.recorder_chunk_annotator == 0.2


def test_temperatures_out_of_range_rejected() -> None:
    from pydantic import ValidationError
    from openclose.config.schema import TemperaturesConfig
    with pytest.raises(ValidationError):
        TemperaturesConfig(skills_runner=-0.1)
    with pytest.raises(ValidationError):
        TemperaturesConfig(cron_nl=2.1)
    with pytest.raises(ValidationError):
        TemperaturesConfig(delegate=-0.1)
    with pytest.raises(ValidationError):
        TemperaturesConfig(delegate=2.1)


def test_delegate_temperature_overridable_via_temperatures_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`delegate` is a tool, not an agent. Its sampling temperature is set
    via [temperatures] delegate = X (not [[agents]])."""
    monkeypatch.setattr(
        ConfigPaths,
        "user_config_path",
        classmethod(lambda cls: tmp_path / "nonexistent" / "config.toml"),  # type: ignore[arg-type,unused-ignore]
    )
    config_dir = tmp_path / ".openclose"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        "[temperatures]\ndelegate = 0.7\n"
    )
    cfg = load_config(project_dir=tmp_path)
    assert cfg.temperatures.delegate == 0.7


def test_legacy_agents_delegate_entry_is_warned_and_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Soft-migration path: existing user configs with
    [[agents]] name='delegate' must not break loading. Entry is dropped
    from the agent registry with a warning that points at [temperatures]."""
    import logging

    monkeypatch.setattr(
        ConfigPaths,
        "user_config_path",
        classmethod(lambda cls: tmp_path / "nonexistent" / "config.toml"),  # type: ignore[arg-type,unused-ignore]
    )
    config_dir = tmp_path / ".openclose"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        '[[agents]]\nname = "delegate"\ntemperature = 0.5\n'
    )
    load_config(project_dir=tmp_path)
    from openclose.config.agents import reload_agents
    with caplog.at_level(logging.WARNING):
        agents = reload_agents()
    assert "delegate" not in agents
    assert any(
        "delegate" in rec.getMessage() and "reserved" in rec.getMessage()
        for rec in caplog.records
    ), f"Expected reserved-name warning. Got: {[r.getMessage() for r in caplog.records]}"


def test_invalid_default_agent_falls_back_to_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Legacy `default_agent = 'delegate'` (or any non-primary agent) must
    not break session creation — config loader falls back to 'build'
    with a warning."""
    import logging

    monkeypatch.setattr(
        ConfigPaths,
        "user_config_path",
        classmethod(lambda cls: tmp_path / "nonexistent" / "config.toml"),  # type: ignore[arg-type,unused-ignore]
    )
    config_dir = tmp_path / ".openclose"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text(
        'default_agent = "delegate"\n'
    )
    with caplog.at_level(logging.WARNING):
        cfg = load_config(project_dir=tmp_path)
    assert cfg.default_agent == "build"
    assert any(
        "default_agent" in rec.getMessage()
        for rec in caplog.records
    )
