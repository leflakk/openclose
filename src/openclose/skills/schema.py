"""Pydantic models for skills: form, parameters, tool references, request bodies."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ParamType = Literal["string", "int", "bool"]


class Parameter(BaseModel):
    """A named input a skill can receive at run time."""

    name: str
    type: ParamType = "string"
    required: bool = False
    default: str = ""


class RequiredTool(BaseModel):
    """A tool the skill needs, with a sensitivity flag for UI emphasis."""

    name: str
    sensitive: bool = False


class SkillForm(BaseModel):
    """The full editable skill form — both LLM output and save input."""

    name: str
    slug: str = ""
    goal: str = ""
    parameters: list[Parameter] = Field(default_factory=list)
    required_tools: list[RequiredTool] = Field(default_factory=list)
    required_tools_prose: str = ""
    procedure: str = ""
    pitfalls: str = ""
    verification: str = ""


class Skill(BaseModel):
    """A saved skill, as read from disk."""

    name: str
    slug: str
    version: int = 1
    created_at: str = ""
    source_session: str = ""
    parameters: list[Parameter] = Field(default_factory=list)
    required_tools: list[RequiredTool] = Field(default_factory=list)
    goal: str = ""
    required_tools_prose: str = ""
    procedure: str = ""
    pitfalls: str = ""
    verification: str = ""

    def to_summary(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name": self.name,
            "goal": self.goal,
            "version": self.version,
            "created_at": self.created_at,
        }

    def body(self) -> str:
        """Reassemble the markdown body from the section fields."""
        parts = [
            f"# Goal\n{self.goal.strip()}",
            f"# Required tools\n{self.required_tools_prose.strip()}",
            f"# Procedure\n{self.procedure.strip()}",
            f"# Pitfalls\n{self.pitfalls.strip()}",
            f"# Verification\n{self.verification.strip()}",
        ]
        return "\n\n".join(parts) + "\n"


class GenerateRequest(BaseModel):
    """Payload for POST /api/skills/generate."""

    session_id: str
    user_prompt: str = ""


class SaveRequest(SkillForm):
    """Payload for POST /api/skills — a SkillForm plus the originating session."""

    source_session: str = ""


class RunRequest(BaseModel):
    """Payload for POST /api/skills/{slug}/run."""

    inputs: dict[str, str] = Field(default_factory=dict)
    trigger_message: str = ""
