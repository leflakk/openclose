"""Pydantic v2 configuration schemas."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class ProviderConfig(BaseModel):
    """Configuration for an LLM provider.

    ``kind`` discriminates the implementation; ``"openai_compatible"`` is the
    only kind supported today. Future kinds (``"anthropic"``, ``"google"``)
    will plug in via the ``make_provider`` factory in ``provider/provider.py``
    without any schema change.
    """

    name: str = "default"
    kind: str = "openai_compatible"
    base_url: str = "http://localhost:8000/v1"
    api_key: str = ""
    # Name of the env var holding the API key. Resolved before ``api_key`` and
    # before the legacy OPENCLOSE_API_KEY / OPENAI_API_KEY chain in auth.py.
    api_key_env: str = ""
    default_model: str = ""
    # Declared models for runtime switching (UI picker). When empty, only the
    # ``default_model`` (if any) is offered.
    models: list[str] = Field(default_factory=list)


class AgentConfig(BaseModel):
    """Configuration for an agent (built-in override or custom)."""

    name: str
    description: str = ""
    model: str = ""
    temperature: float = 0.0
    max_steps: int = 100
    mode: str = "primary"
    system_prompt: str = ""
    traits: list[str] = Field(default_factory=list)  # e.g. ["readonly"]
    allowed_tools: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)

    @field_validator("temperature")
    @classmethod
    def _check_temperature(cls, v: float) -> float:
        if not (0.0 <= v <= 2.0):
            raise ValueError("temperature must be between 0.0 and 2.0")
        return v

    @field_validator("max_steps")
    @classmethod
    def _check_max_steps(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("max_steps must be positive")
        return v


class PermissionRuleConfig(BaseModel):
    """A single permission rule."""

    tool: str = "*"
    path: str = "*"
    action: Literal["allow", "deny", "ask"] = "ask"


class BrowserVisionGroundingConfig(BaseModel):
    """OpenAI-compatible endpoint serving the visual grounding model used
    by ``browser_automation`` (rich mode) to turn natural-language
    ``target`` descriptions into pixel coordinates when fuzzy
    accessibility-tree matching cannot resolve them.

    This is a separate endpoint from the main LLM provider — the
    grounding model is typically a small vision model (e.g. a Qwen2-VL
    variant) running on its own server.
    """

    base_url: str = "http://localhost:5002/v1"
    api_key: str = ""
    model: str = "local"


class TemperaturesConfig(BaseModel):
    """Sampling temperatures for non-agent LLM calls.

    Primary agents (build, plan, custom agents) get their temperature from
    the matching ``[[agents]]`` section. Everything else lives here:
    recorder annotators, browser automation planners, the cron-NL parser,
    the skill builder/runner, the read-only sub-agent spawned by the
    ``delegate`` tool, and the read-only reviewer sub-agent spawned by
    the ``plan`` tool when called with ``phase="draft"`` (both are tools,
    not configurable agents).
    """

    skills_runner: float = 0.1
    skills_builder: float = 0.1
    browser_vision_grounding: float = 0.0
    browser_vision_planner: float = 0.0
    browser_dom_planner: float = 0.0
    recorder_merger: float = 0.1
    recorder_task_builder: float = 0.1
    recorder_chunk_annotator: float = 0.2
    cron_nl: float = 0.0
    delegate: float = 0.0
    plan_reviewer: float = 0.0

    @field_validator(
        "skills_runner",
        "skills_builder",
        "browser_vision_grounding",
        "browser_vision_planner",
        "browser_dom_planner",
        "recorder_merger",
        "recorder_task_builder",
        "recorder_chunk_annotator",
        "cron_nl",
        "delegate",
        "plan_reviewer",
    )
    @classmethod
    def _check_temperature(cls, v: float) -> float:
        if not (0.0 <= v <= 2.0):
            raise ValueError("temperature must be between 0.0 and 2.0")
        return v


class OpenCloseConfig(BaseModel):
    """Root configuration model."""

    # Provider
    providers: list[ProviderConfig] = Field(
        default_factory=lambda: [ProviderConfig()]
    )
    # Which provider new sessions start with. Empty → first entry in
    # ``providers``. Sessions track their own ``provider`` once a user
    # switches via the UI.
    default_provider: str = ""

    # Agents
    agents: list[AgentConfig] = Field(default_factory=list)
    default_agent: str = "build"

    # Permissions
    permissions: list[PermissionRuleConfig] = Field(default_factory=list)

    # Temperatures for tool-internal one-shot LLM calls
    temperatures: TemperaturesConfig = Field(default_factory=TemperaturesConfig)

    # Endpoint serving the visual grounding model used by
    # browser_automation in rich mode (separate from the main LLM
    # provider). Optional: when this section is absent from
    # ~/.config/openclose/config.toml the value is None and
    # browser_automation runs in DOM-only mode.
    browser_vision_grounding: Optional[BrowserVisionGroundingConfig] = None

    # Session
    max_context_tokens: int = 128_000
    compaction_threshold: float = 0.9
    compaction_summary_max_tokens: int = 2000

    # Misc
    project_dir: str = "."

    @field_validator("compaction_threshold")
    @classmethod
    def _check_compaction_threshold(cls, v: float) -> float:
        if not (0.0 < v <= 1.0):
            raise ValueError("compaction_threshold must be in (0.0, 1.0]")
        return v

    @field_validator("max_context_tokens")
    @classmethod
    def _check_max_context_tokens(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("max_context_tokens must be positive")
        return v
