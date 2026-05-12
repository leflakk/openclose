"""LLM request debug dumper — writes every LLM payload to <ConfigPaths.config_dir()>/<project>/llm_debug.jsonl.

Activated by setting OPENCLOSE_DEBUG_LLM=1.  To remove this feature:
1. Delete this file.
2. Remove the DEBUG_LLM line from flag.py.
3. Remove the dump_llm_request() calls (grep for 'dump_llm_request').
"""

from __future__ import annotations

import contextvars
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from openclose import flag
from openclose.config.paths import ConfigPaths


@dataclass
class LLMDebugContext:
    """Per-call metadata read by Provider.chat / chat_sync when logging.

    Callers (agent loop, compaction, browser_automation sub-agent, …) set this
    contextvar just before invoking the Provider; the Provider reads it
    inside its own _maybe_dump() helper so every LLM request — regardless
    of caller — flows through the same debug dump. If the contextvar is
    unset, no log line is written (opt-in per call chain).
    """

    source: str
    step: int
    project_dir: str


llm_debug_context: contextvars.ContextVar[LLMDebugContext | None] = contextvars.ContextVar(
    "llm_debug_context", default=None
)


def dump_llm_request(
    *,
    step: int,
    source: str,
    model: str,
    temperature: float,
    messages: list[Any],
    tools: list[Any] | None,
    project_dir: str,
) -> None:
    """Append one JSON line to the per-project runtime dir ``llm_debug.jsonl``.

    No-op when ``flag.DEBUG_LLM`` is ``False``.
    """
    if not flag.DEBUG_LLM:
        return

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "step": step,
        "source": source,
        "model": model,
        "temperature": temperature,
        "messages": messages,
        "tools": tools,
    }

    path = ConfigPaths.project_runtime_dir(project_dir) / "llm_debug.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
