"""Multi-layer configuration loading and management.

Priority (highest to lowest):
1. Environment variables (OPENCLOSE_*)
2. Project-level config (.openclose/config.toml)
3. User-level config (config.toml in ConfigPaths.config_dir() —
   Linux: ~/.config/openclose, macOS: ~/Library/Application Support/openclose,
   Windows: %APPDATA%\\openclose)
4. Defaults
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from openclose.config.paths import ConfigPaths
from openclose.config.schema import OpenCloseConfig
from openclose.log import get_logger

log = get_logger(__name__)

_config: OpenCloseConfig | None = None


def _load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file, returning empty dict if missing or invalid."""
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        log.warning("Failed to load config from %s: %s", path, e)
        return {}


def _env_overrides() -> dict[str, Any]:
    """Extract OPENCLOSE_* environment variables as config overrides."""
    prefix = "OPENCLOSE_"
    overrides: dict[str, Any] = {}
    for key, value in os.environ.items():
        if key.startswith(prefix):
            config_key = key[len(prefix):].lower()
            overrides[config_key] = value
    return overrides


def _merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge override into base."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def load_config(project_dir: Path | None = None) -> OpenCloseConfig:
    """Load configuration from all layers and return merged result."""
    global _config

    # Layer 1: defaults (handled by Pydantic)
    merged: dict[str, Any] = {}

    # Layer 2: user config
    user_data = _load_toml(ConfigPaths.user_config_path())
    merged = _merge_dicts(merged, user_data)

    # Layer 3: project config
    if project_dir is not None:
        project_data = _load_toml(ConfigPaths.project_config_path(project_dir))
        merged = _merge_dicts(merged, project_data)

    # Layer 4: environment overrides
    env_data = _env_overrides()
    merged = _merge_dicts(merged, env_data)

    # Set project_dir if provided
    if project_dir is not None:
        merged["project_dir"] = str(project_dir)

    _config = OpenCloseConfig.model_validate(merged)

    # Validate default_agent: must point at a primary agent in the
    # resolved registry. Legacy configs with default_agent="delegate"
    # would otherwise break session creation, since delegate is no
    # longer a switchable agent.
    try:
        from openclose.config.agents import reload_agents
        agents = reload_agents()
        cfg = agents.get(_config.default_agent)
        if cfg is None or cfg.mode != "primary":
            log.warning(
                "default_agent=%r is not a switchable primary agent "
                "-- falling back to 'build'.",
                _config.default_agent,
            )
            _config = _config.model_copy(update={"default_agent": "build"})
    except Exception as e:  # noqa: BLE001 — never let validation crash startup
        log.warning("Could not validate default_agent: %s", e)

    log.debug("Configuration loaded: %s", _config.model_dump())
    return _config


def get_config() -> OpenCloseConfig:
    """Get the current config, loading defaults if not yet loaded."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


class ConfigManager:
    """Manages configuration lifecycle and reloading."""

    def __init__(self, project_dir: Path | None = None) -> None:
        self._project_dir = project_dir
        self._config = load_config(project_dir)

    @property
    def config(self) -> OpenCloseConfig:
        return self._config

    def reload(self) -> OpenCloseConfig:
        """Reload configuration from all sources."""
        self._config = load_config(self._project_dir)
        return self._config
