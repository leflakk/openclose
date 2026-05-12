"""Configuration system — multi-layer config with Pydantic v2 validation."""

from openclose.config.config import load_config, get_config, ConfigManager
from openclose.config.paths import ConfigPaths
from openclose.config.schema import OpenCloseConfig, ProviderConfig, AgentConfig
from openclose.config.agents import load_agents, get_agents, reload_agents

__all__ = [
    "load_config",
    "get_config",
    "ConfigManager",
    "ConfigPaths",
    "OpenCloseConfig",
    "ProviderConfig",
    "AgentConfig",
    "load_agents",
    "get_agents",
    "reload_agents",
]
