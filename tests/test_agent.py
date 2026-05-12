"""Tests for the agent system."""

from __future__ import annotations

from pathlib import Path

import pytest

from openclose.agent.agent import Agent, AgentMode, get_agent, list_agents
from openclose.agent.prompt import build_system_prompt
from openclose.config.agents import (
    load_agents,
    render_prompt_template,
)


# ── Basic Agent dataclass ────────────────────────────────────────────


def test_builtin_build_agent() -> None:
    """Build agent should exist and have full access."""
    agent = get_agent("build")
    assert agent.name == "build"
    assert agent.mode == AgentMode.PRIMARY
    assert agent.can_use_tool("bash")
    assert agent.can_use_tool("write")
    assert agent.can_use_tool("read")


def test_builtin_plan_agent() -> None:
    """Plan agent should deny mutation tools but allow bash + plan."""
    agent = get_agent("plan")
    assert agent.name == "plan"
    assert not agent.can_use_tool("write")
    assert not agent.can_use_tool("edit")
    assert agent.can_use_tool("bash")
    assert agent.can_use_tool("read")
    assert agent.can_use_tool("grep")
    assert agent.can_use_tool("glob")
    assert agent.can_use_tool("plan")


def test_build_agent_denies_plan_tool() -> None:
    """Build agent should deny the plan tool."""
    agent = get_agent("build")
    assert not agent.can_use_tool("plan")


def test_builtin_plan_agent_denies_multiedit() -> None:
    """Plan agent should deny multiedit (read-only)."""
    agent = get_agent("plan")
    assert not agent.can_use_tool("multiedit")


def test_builtin_plan_agent_has_readonly_trait() -> None:
    """Plan agent should have the 'readonly' trait."""
    agent = get_agent("plan")
    assert agent.has_trait("readonly")
    assert not agent.has_trait("nonexistent")


def test_builtin_plan_agent_has_plan_trait() -> None:
    """Plan agent should have the 'plan' trait."""
    agent = get_agent("plan")
    assert agent.has_trait("plan")


def test_unknown_agent_raises() -> None:
    """Unknown agent should raise ValueError."""
    with pytest.raises(ValueError, match="Unknown agent"):
        get_agent("nonexistent")


def test_list_agents() -> None:
    """Should list the switchable built-in agents and never expose
    `delegate` (which is a tool, not an agent)."""
    agents = list_agents()
    names = [a.name for a in agents]
    assert "build" in names
    assert "plan" in names
    assert "delegate" not in names


def test_agent_can_use_tool_allowed_list() -> None:
    """Agent with allowed_tools should only allow those tools."""
    agent = Agent(name="restricted", allowed_tools=["read", "grep"])
    assert agent.can_use_tool("read")
    assert agent.can_use_tool("grep")
    assert not agent.can_use_tool("bash")


def test_agent_can_use_tool_denied_list() -> None:
    """Agent with denied_tools should deny those tools."""
    agent = Agent(name="partial", denied_tools=["bash"])
    assert not agent.can_use_tool("bash")
    assert agent.can_use_tool("read")


# ── Traits ───────────────────────────────────────────────────────────


def test_agent_has_trait() -> None:
    agent = Agent(name="x", traits=["readonly", "strict"])
    assert agent.has_trait("readonly")
    assert agent.has_trait("strict")
    assert not agent.has_trait("other")


# ── Tool schema filtering ───────────────────────────────────────────


def _make_schema(name: str) -> dict[str, object]:
    return {"name": name, "description": f"Tool {name}", "parameters": {}}


def test_filter_tool_schemas_no_restrictions() -> None:
    """Agent with no restrictions sees all tools."""
    agent = Agent(name="build")
    schemas = [_make_schema("read"), _make_schema("write"), _make_schema("bash")]
    filtered = agent.filter_tool_schemas(schemas)
    assert len(filtered) == 3


def test_filter_tool_schemas_denied() -> None:
    """Agent with denied_tools should not see those schemas."""
    agent = Agent(name="plan", denied_tools=["write", "bash"])
    schemas = [_make_schema("read"), _make_schema("write"), _make_schema("bash")]
    filtered = agent.filter_tool_schemas(schemas)
    assert len(filtered) == 1
    assert filtered[0]["name"] == "read"


def test_filter_tool_schemas_allowed() -> None:
    """Agent with allowed_tools should only see those schemas."""
    agent = Agent(name="narrow", allowed_tools=["read", "grep"])
    schemas = [_make_schema("read"), _make_schema("write"), _make_schema("grep")]
    filtered = agent.filter_tool_schemas(schemas)
    names = [s["name"] for s in filtered]
    assert names == ["read", "grep"]


# ── Two-layer prompt building ───────────────────────────────────────


def test_two_layer_prompt_build() -> None:
    """Build agent gets common + build-specific prompt."""
    agent = get_agent("build")
    prompt = build_system_prompt(agent, project_dir="/tmp/test")
    assert "highly efficient coding assistant" in prompt  # from _COMMON_PROMPT
    assert "Verify the solution with the project's own tests" in prompt  # from _BUILD_PROMPT
    assert "/tmp/test" in prompt


def test_two_layer_prompt_plan() -> None:
    """Plan agent gets common + plan-specific prompt."""
    agent = get_agent("plan")
    prompt = build_system_prompt(agent)
    assert "highly efficient coding assistant" in prompt  # from _COMMON_PROMPT
    assert "READ-ONLY mode" in prompt  # from _PLAN_PROMPT
    assert "ready-for-implementation plan" in prompt  # from _PLAN_PROMPT


def test_two_layer_prompt_custom_agent() -> None:
    """Custom agent with system_prompt gets common + custom prompt."""
    agent = Agent(name="custom", system_prompt="You are a security auditor.")
    prompt = build_system_prompt(agent)
    assert "highly efficient coding assistant" in prompt  # from _COMMON_PROMPT
    assert "security auditor" in prompt  # from custom system_prompt


def test_two_layer_prompt_no_system_prompt() -> None:
    """Agent without system_prompt and not build/plan gets only common prompt."""
    agent = Agent(name="generic")
    prompt = build_system_prompt(agent)
    assert "highly efficient coding assistant" in prompt
    assert "Verify the solution with the project's own tests" not in prompt  # _BUILD_PROMPT absent
    assert "READ-ONLY" not in prompt


def test_builtin_custom_system_prompt_overrides_layer2() -> None:
    """User system_prompt on build agent replaces _BUILD_PROMPT but keeps common."""
    agent = Agent(name="build", system_prompt="Custom build instructions.")
    prompt = build_system_prompt(agent)
    assert "highly efficient coding assistant" in prompt  # _COMMON_PROMPT still present
    assert "Custom build instructions" in prompt
    assert "Verify with the project's own tests" not in prompt  # _BUILD_PROMPT replaced


def test_plan_system_prompt_readonly() -> None:
    """Plan agent prompt should include read-only restriction."""
    agent = get_agent("plan")
    prompt = build_system_prompt(agent)
    assert "read-only" in prompt.lower()


def test_subagent_skips_common_prompt() -> None:
    """Subagent mode should NOT include the common prompt."""
    agent = Agent(
        name="delegate",
        system_prompt="You are a delegated sub-agent.",
        mode=AgentMode.SUBAGENT,
    )
    prompt = build_system_prompt(agent)
    assert "AI coding assistant" not in prompt
    assert "delegated sub-agent" in prompt


def test_subagent_still_gets_context() -> None:
    """Subagent should still get working dir and date even without common prompt."""
    agent = Agent(
        name="helper",
        system_prompt="You are a helper sub-agent.",
        mode=AgentMode.SUBAGENT,
    )
    prompt = build_system_prompt(agent, project_dir="/tmp/test")
    assert "/tmp/test" in prompt
    assert "Current date:" in prompt


def test_subagent_prompt_no_main_agent_leakage() -> None:
    """Subagent prompt must not contain any main agent prompt fragments."""
    agent = Agent(
        name="delegate",
        system_prompt="You are a delegated sub-agent.",
        mode=AgentMode.SUBAGENT,
    )
    prompt = build_system_prompt(agent)
    # No common prompt
    assert "highly efficient coding assistant" not in prompt
    # No build prompt fragments
    assert "Verify with the project's own tests" not in prompt
    # No plan prompt fragments
    assert "READ-ONLY mode" not in prompt
    assert "ready-for-implementation plan" not in prompt
    # Own prompt present
    assert "delegated sub-agent" in prompt


def test_agent_mode_subagent() -> None:
    """SUBAGENT mode enum value works correctly."""
    agent = Agent(name="x", mode=AgentMode.SUBAGENT)
    assert agent.mode == AgentMode.SUBAGENT
    assert agent.mode.value == "subagent"


def test_system_prompt_includes_tool_names() -> None:
    """When tool_names are passed, they appear in the prompt."""
    agent = Agent(name="test")
    prompt = build_system_prompt(agent, tool_names=["read", "grep", "bash"])
    assert "Available tools: read, grep, bash" in prompt


def test_system_prompt_no_tool_names() -> None:
    """When no tool_names are passed, no tools line appears."""
    agent = Agent(name="test")
    prompt = build_system_prompt(agent)
    assert "Available tools:" not in prompt


# ── Prompt template rendering ────────────────────────────────────────


def test_render_prompt_template_basic() -> None:
    result = render_prompt_template(
        "Working in $project_dir on $date",
        {"project_dir": "/code", "date": "2026-03-18"},
    )
    assert result == "Working in /code on 2026-03-18"


def test_render_prompt_template_unknown_var() -> None:
    """Unknown variables are left as-is (safe_substitute)."""
    result = render_prompt_template("Hello $unknown", {})
    assert result == "Hello $unknown"


def test_render_prompt_template_empty() -> None:
    assert render_prompt_template("", {}) == ""
    assert render_prompt_template("no vars", {}) == "no vars"


# ── Config-based agent loading ───────────────────────────────────────


def test_load_agents_defaults() -> None:
    """When no agents are configured, built-in defaults should load."""
    agents = load_agents()
    assert "build" in agents
    assert "plan" in agents
    assert agents["plan"].denied_tools == ["write", "edit", "multiedit", "browser_automation", "deliver_message"]
    assert "readonly" in agents["plan"].traits
    assert "plan" in agents["plan"].traits


def test_load_agents_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """[[agents]] in config.toml should override customizable fields."""
    from openclose.config import config as config_mod
    from openclose.config import agents as agents_mod
    from openclose.config.schema import OpenCloseConfig, AgentConfig

    # Create a config with custom agents
    custom_config = OpenCloseConfig(
        agents=[
            AgentConfig(
                name="build",
                description="Custom build",
                model="my-model",
                temperature=0.7,
            ),
            AgentConfig(
                name="reviewer",
                description="Code reviewer",
                mode="primary",
                allowed_tools=["read", "grep"],
            ),
        ]
    )

    # Inject the custom config
    monkeypatch.setattr(config_mod, "_config", custom_config)
    agents_mod._agents_cache = None

    try:
        agents = load_agents()
        assert agents["build"].description == "Custom build"
        assert agents["build"].model == "my-model"
        assert agents["build"].temperature == 0.7
        assert "reviewer" in agents
        assert agents["reviewer"].allowed_tools == ["read", "grep"]
    finally:
        agents_mod._agents_cache = None
        config_mod._config = None


def test_locked_fields_ignored_for_builtin(monkeypatch: pytest.MonkeyPatch) -> None:
    """User config cannot override locked fields on built-in agents."""
    from openclose.config import config as config_mod
    from openclose.config import agents as agents_mod
    from openclose.config.schema import OpenCloseConfig, AgentConfig

    custom_config = OpenCloseConfig(
        agents=[
            AgentConfig(
                name="build",
                denied_tools=["bash", "write"],  # locked — should be ignored
            ),
            AgentConfig(
                name="plan",
                traits=["readonly"],  # locked — should be ignored (missing "plan")
                denied_tools=["write"],  # locked — should be ignored
            ),
        ]
    )

    monkeypatch.setattr(config_mod, "_config", custom_config)
    agents_mod._agents_cache = None

    try:
        agents = load_agents()
        # build: denied_tools should remain ["plan"] (built-in default)
        assert agents["build"].denied_tools == ["plan"]
        # plan: traits should remain ["readonly", "plan"] (built-in default)
        assert "plan" in agents["plan"].traits
        assert "readonly" in agents["plan"].traits
        # plan: denied_tools should remain locked (built-in default)
        assert agents["plan"].denied_tools == ["write", "edit", "multiedit", "browser_automation", "deliver_message"]
    finally:
        agents_mod._agents_cache = None
        config_mod._config = None
