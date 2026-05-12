"""Agents configuration loader.

Loads agent definitions from built-in defaults and ``[[agents]]`` sections
in ``config.toml``, and returns ready-to-use ``AgentConfig`` instances.

Built-in agents (``build``, ``plan``) have locked tool restrictions that
cannot be overridden by user config.  Custom agents must be fully
self-contained (no inheritance).

The name ``delegate`` is reserved: it is a tool, not an agent. Any
``[[agents]] name = "delegate"`` block is ignored with a warning — its
sampling temperature is set via ``[temperatures] delegate = X``.

Priority (highest wins):
1. ``[[agents]]`` in config.toml (user-level and project-level, merged)
2. Built-in defaults (hardcoded below)
"""

from __future__ import annotations

from string import Template
from typing import Any

from openclose.config.schema import AgentConfig
from openclose.log import get_logger

log = get_logger(__name__)

# ── Built-in defaults ────────────────────────────────────────────────

_BUILTIN_AGENT_DEFAULTS: dict[str, dict[str, Any]] = {
    "build": {
        "name": "build",
        "description": "Primary agent with full tool access for code writing and execution.",
        "mode": "primary",
        "denied_tools": ["plan"],
    },
    "plan": {
        "name": "plan",
        "description": "Read-only analysis agent. Cannot modify files.",
        "mode": "primary",
        "traits": ["readonly", "plan"],
        "denied_tools": ["write", "edit", "multiedit", "browser_automation", "deliver_message"],
    },
}

# Names that cannot be used as user-defined agents. ``delegate`` is a
# tool that internally spawns a read-only sub-agent — its sampling
# temperature is configured via ``[temperatures] delegate = X``, not via
# an ``[[agents]]`` block.
_RESERVED_AGENT_NAMES = {"delegate"}

# Fields that user config CANNOT override for built-in agents.
_LOCKED_FIELDS = {"traits", "allowed_tools", "denied_tools"}

# Fields that user config CAN override for built-in agents.
_CUSTOMIZABLE_FIELDS = {"model", "temperature", "max_steps", "description", "system_prompt"}


# ── Template variable substitution ───────────────────────────────────

def render_prompt_template(
    prompt: str,
    variables: dict[str, str],
) -> str:
    """Substitute ``$var`` or ``${var}`` placeholders using `string.Template`.

    Unknown placeholders are left as-is (safe_substitute).
    """
    if not prompt or "$" not in prompt:
        return prompt
    return Template(prompt).safe_substitute(variables)


# ── Public API ───────────────────────────────────────────────────────

def load_agents() -> dict[str, AgentConfig]:
    """Load and resolve all agent definitions.

    Returns a dict of ``name → AgentConfig``, ready for consumption by
    ``agent.agent.get_agent()``.
    """
    # Avoid circular import — config imports schema, we import config here
    from openclose.config.config import get_config

    # Collect raw agent dicts — start with built-in defaults
    agents_raw: dict[str, dict[str, Any]] = {
        name: dict(data) for name, data in _BUILTIN_AGENT_DEFAULTS.items()
    }

    # Overlay agents from config.toml [[agents]] sections.
    config = get_config()
    for agent_cfg in config.agents:
        dumped = agent_cfg.model_dump(exclude_unset=True)
        name = agent_cfg.name

        if name in _RESERVED_AGENT_NAMES:
            log.warning(
                "[[agents]] name=%r is reserved (it is a tool, not an agent) "
                "-- entry ignored. To override its sampling temperature, "
                "set [temperatures] %s = X in config.toml.",
                name, name,
            )
            continue

        if name in _BUILTIN_AGENT_DEFAULTS:
            # Built-in agent: only allow customizable fields
            for field in _CUSTOMIZABLE_FIELDS:
                if field in dumped:
                    agents_raw[name][field] = dumped[field]
            # Warn about ignored locked fields
            for field in _LOCKED_FIELDS:
                if field in dumped:
                    log.warning(
                        "Field '%s' is locked for built-in agent '%s' -- ignored",
                        field, name,
                    )
        else:
            # Custom agent: accept as-is (must be fully self-contained)
            agents_raw[name] = dumped

    # Build AgentConfig instances
    result: dict[str, AgentConfig] = {}
    for name, data in agents_raw.items():
        try:
            result[name] = AgentConfig.model_validate(data)
        except Exception as e:
            log.error("Invalid agent config for '%s': %s", name, e)

    return result


# ── Singleton cache ──────────────────────────────────────────────────

_agents_cache: dict[str, AgentConfig] | None = None


def get_agents() -> dict[str, AgentConfig]:
    """Get the cached agents dict, loading on first call."""
    global _agents_cache
    if _agents_cache is None:
        _agents_cache = load_agents()
    return _agents_cache


def reload_agents() -> dict[str, AgentConfig]:
    """Force-reload agents from disk."""
    global _agents_cache
    _agents_cache = load_agents()
    return _agents_cache
