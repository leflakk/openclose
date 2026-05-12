"""LLM-backed skill form generation from a session's conversation history."""

from __future__ import annotations

import json
import re
from typing import Any

from openclose.config.config import get_config
from openclose.log import get_logger
from openclose.provider.provider import get_provider
from openclose.skills.schema import Parameter, RequiredTool, SkillForm
from openclose.skills.storage import slugify
from openclose.session.session import SessionManager
from openclose.session.processor import SessionProcessor
from openclose.storage.db import get_db

log = get_logger(__name__)


# Tools whose grant we want to surface prominently in the UI.
SENSITIVE_TOOLS: set[str] = {
    "bash",
    "write",
    "edit",
    "multiedit",
    "browser_automation",
    "deliver_message",
}


class SkillBuilderError(RuntimeError):
    """Raised when the LLM output cannot be parsed into a SkillForm."""


_SYSTEM_PROMPT = """\
You distill a recorded chat-session conversation — user messages,
assistant text, and tool calls with their results — into a reusable
"skill": a definition that will run unattended on a schedule.

You will receive the full conversation as a JSON array inside the user
message below, optionally preceded by an EXTRA_INSTRUCTIONS block the
human wrote to guide you.

Separate three kinds of values:
- CONSTANTS: values baked into the skill (hardcoded email addresses,
  fixed URLs, specific prompt templates). These go in the Procedure.
- RUNTIME PARAMETERS: values that might change between runs. Expose
  these as `parameters[]` with a `default` (the value seen in the
  history) and reference them in the Procedure as $param_name.
- LIVE OBSERVATIONS: data that must be read fresh each run (today's
  news, current price, latest PRs). Describe where the skill must
  gather them in the Procedure.

Rules:
- Never invent tools, URLs, selectors, or steps absent from the
  conversation. Your `required_tools` list must be a subset of the
  tools actually called in the conversation. If a tool was never
  called, do not list it.
- Prefer the shortest reliable workflow. Drop one-off clarification
  questions and tangents.
- Flag `sensitive: true` for these tool names (they have side effects
  on the system or external world): bash, write, edit, multiedit,
  browser_automation, deliver_message.
  All others are `sensitive: false`.
- The procedure should be written so an agent with ONLY the listed
  required_tools can execute it unattended — no "ask the user", no
  "check with me first".
- Name: a short human title ("Daily PR digest"). Slug: lowercase
  kebab-case of the name.
- Goal: one active-voice sentence.

OUTPUT CONTRACT — CRITICAL:
- Reply with a SINGLE JSON OBJECT and NOTHING ELSE.
- No preamble. No trailing prose. No code fences. No ```json.
- The first character of your reply MUST be `{`.
- The object MUST match this schema exactly (types shown):

{
  "name": string,
  "slug": string,
  "goal": string,
  "parameters": [
    {"name": string, "type": "string"|"int"|"bool",
     "required": boolean, "default": string}
  ],
  "required_tools": [
    {"name": string, "sensitive": boolean}
  ],
  "required_tools_prose": string,
  "procedure": string,
  "pitfalls": string,
  "verification": string
}

All string fields must be present (use "" if empty). Lists may be empty
([]) but the keys must be present.
"""


def _serialize_conversation(
    messages: list[dict[str, Any]], max_chars: int = 80_000
) -> str:
    """Render messages as a compact JSON array string, truncated if oversized."""
    compact: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        if role == "assistant" and m.get("tool_calls"):
            tc_summary = []
            for tc in m["tool_calls"]:
                tc_summary.append({
                    "name": tc.get("name", ""),
                    "arguments": tc.get("arguments", ""),
                })
            compact.append({
                "role": "assistant",
                "content": content or "",
                "tool_calls": tc_summary,
            })
        elif role == "tool":
            compact.append({
                "role": "tool",
                "tool_call_id": m.get("tool_call_id", ""),
                "content": content,
            })
        else:
            compact.append({"role": role, "content": content})

    text = json.dumps(compact, ensure_ascii=False, indent=2)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[...truncated]"
    return text


def _extract_json_object(text: str) -> dict[str, Any]:
    """Extract the first balanced JSON object from the LLM's reply.

    The system prompt asks for a bare JSON object, but be lenient
    about minor deviations (leading prose, fenced blocks).
    """
    stripped = text.strip()

    # Strip common fence formats
    fence = re.match(r"^```(?:json)?\s*(.*?)```\s*$", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()

    # Fast path: the whole thing is a JSON object.
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # Fallback: find the first `{` and scan for a balanced closer.
    start = stripped.find("{")
    if start == -1:
        raise SkillBuilderError("No JSON object found in LLM response")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(stripped)):
        c = stripped[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(stripped[start:i + 1])
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError as e:
                    raise SkillBuilderError(
                        f"Extracted JSON is invalid: {e}"
                    ) from e
    raise SkillBuilderError("Unterminated JSON object in LLM response")


def _coerce_form(raw: dict[str, Any]) -> SkillForm:
    """Validate and normalize the LLM's raw dict into a SkillForm.

    Enforces the sensitive-tool flag regardless of what the LLM said.
    Fills a slug if the LLM omitted it.
    """
    params_raw = raw.get("parameters", []) or []
    params: list[Parameter] = []
    for p in params_raw:
        if not isinstance(p, dict):
            continue
        try:
            params.append(Parameter(
                name=str(p.get("name", "")),
                type=str(p.get("type", "string")),
                required=bool(p.get("required", False)),
                default=str(p.get("default", "")),
            ))
        except Exception as e:
            log.warning("Skipping bad parameter from LLM: %s", e)

    tools_raw = raw.get("required_tools", []) or []
    tools: list[RequiredTool] = []
    for t in tools_raw:
        if not isinstance(t, dict):
            continue
        name = str(t.get("name", "")).strip()
        if not name:
            continue
        tools.append(RequiredTool(
            name=name,
            sensitive=name in SENSITIVE_TOOLS,
        ))

    name = str(raw.get("name", "Untitled Skill")).strip() or "Untitled Skill"
    slug = str(raw.get("slug", "")).strip() or slugify(name)

    return SkillForm(
        name=name,
        slug=slug,
        goal=str(raw.get("goal", "")),
        parameters=params,
        required_tools=tools,
        required_tools_prose=str(raw.get("required_tools_prose", "")),
        procedure=str(raw.get("procedure", "")),
        pitfalls=str(raw.get("pitfalls", "")),
        verification=str(raw.get("verification", "")),
    )


async def generate_skill_form(
    session_id: str,
    user_prompt: str = "",
) -> SkillForm:
    """Build a SkillForm from the conversation in `session_id`.

    Raises:
        SkillBuilderError: if the session is missing, empty, or the
            LLM output can't be parsed.
    """
    db = get_db()
    mgr = SessionManager(db)
    session = mgr.get_session(session_id)
    if session is None:
        raise SkillBuilderError(f"Session not found: {session_id}")

    pairs = mgr.get_messages_with_parts(session_id)
    if not pairs:
        raise SkillBuilderError("Session has no messages to distill")

    messages = SessionProcessor._reconstruct_llm_messages(pairs)
    conv = _serialize_conversation(messages)

    user_parts = []
    if user_prompt.strip():
        user_parts.append(f"EXTRA_INSTRUCTIONS:\n{user_prompt.strip()}\n")
    user_parts.append(f"CONVERSATION:\n{conv}")
    user_message = "\n".join(user_parts)

    provider = get_provider()
    config = get_config()
    model = session.model or (
        config.providers[0].default_model if config.providers else ""
    )
    if not model:
        model = await provider.detect_model() or ""
    if not model:
        raise SkillBuilderError("No model configured")

    response = await provider.chat_sync(
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        model=model,
        temperature=config.temperatures.skills_builder,
        max_tokens=4096,
    )

    content = response.choices[0].message.content or ""
    if not content.strip():
        raise SkillBuilderError("LLM returned empty response")

    raw = _extract_json_object(content)
    return _coerce_form(raw)
