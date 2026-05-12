"""Agent definitions and registry.

Agents are loaded from ``[[agents]]`` sections in ``config.toml`` via
:func:`openclose.config.agents.get_agents`.  Built-in defaults are used
when no agents are configured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from openclose.config.agents import get_agents
from openclose.config.config import get_config


class AgentMode(Enum):
    """Agent execution mode."""

    PRIMARY = "primary"
    SUBAGENT = "subagent"


@dataclass
class Agent:
    """An agent definition with its capabilities and constraints."""

    name: str
    description: str = ""
    model: str = ""
    temperature: float = 0.0
    max_steps: int = 100
    system_prompt: str = ""
    mode: AgentMode = AgentMode.PRIMARY
    traits: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)

    def can_use_tool(self, tool_name: str) -> bool:
        """Check if this agent is allowed to use a tool."""
        if self.denied_tools and tool_name in self.denied_tools:
            return False
        if self.allowed_tools and tool_name not in self.allowed_tools:
            return False
        return True

    def has_trait(self, trait: str) -> bool:
        """Check if this agent has a semantic trait."""
        return trait in self.traits

    def filter_tool_schemas(
        self, schemas: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        """Return only the tool schemas this agent is allowed to use.

        This is the key fix: the LLM should never *see* tools that the
        agent cannot invoke, so it won't waste a step trying.
        """
        filtered: list[dict[str, object]] = []
        for schema in schemas:
            name = schema.get("name", "")
            if isinstance(name, str) and self.can_use_tool(name):
                filtered.append(schema)
        return filtered


def _agent_from_config(name: str) -> Agent | None:
    """Try to build an Agent from the resolved agents config."""
    agents = get_agents()

    agent_cfg = agents.get(name)
    if agent_cfg is None:
        return None

    mode = AgentMode(agent_cfg.mode)

    agent = Agent(
        name=agent_cfg.name,
        description=agent_cfg.description,
        model=agent_cfg.model,
        temperature=agent_cfg.temperature,
        max_steps=agent_cfg.max_steps,
        system_prompt=agent_cfg.system_prompt,
        mode=mode,
        traits=list(agent_cfg.traits),
        allowed_tools=list(agent_cfg.allowed_tools),
        denied_tools=list(agent_cfg.denied_tools),
    )

    # Fall back to provider's default model if agent has none
    if not agent.model:
        config = get_config()
        for provider in config.providers:
            if provider.default_model:
                agent.model = provider.default_model
                break

    return agent


def get_agent(name: str) -> Agent:
    """Get an agent by name.

    Looks up the resolved agents config (config.toml + built-in defaults).
    Raises ``ValueError`` if the agent is not found.
    """
    agent = _agent_from_config(name)
    if agent is not None:
        return agent

    raise ValueError(f"Unknown agent: {name!r}")


def list_agents() -> list[Agent]:
    """List all available agents."""
    agents_cfg = get_agents()

    agents: list[Agent] = []
    for name, agent_cfg in agents_cfg.items():
        mode = AgentMode(agent_cfg.mode)
        agents.append(
            Agent(
                name=agent_cfg.name,
                description=agent_cfg.description,
                model=agent_cfg.model,
                temperature=agent_cfg.temperature,
                max_steps=agent_cfg.max_steps,
                system_prompt=agent_cfg.system_prompt,
                mode=mode,
                traits=list(agent_cfg.traits),
                allowed_tools=list(agent_cfg.allowed_tools),
                denied_tools=list(agent_cfg.denied_tools),
            )
        )

    return agents
