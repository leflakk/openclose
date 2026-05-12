"""Skill file storage — per-project markdown files with YAML frontmatter.

Layout under `<ConfigPaths.config_dir()>/<project>/skills/`:
- `<slug>.md`                                 → skill definition
- `<slug>/<iso>-<run_id>.jsonl`               → per-run event log
- `<slug>/<iso>-<run_id>.out.md`              → per-run final text output

The frontmatter uses block-style YAML for the `parameters` and
`required_tools` lists. Written and parsed by hand — PyYAML isn't a
project dependency and the schema is small enough to ship without.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openclose.config.config import get_config
from openclose.config.paths import ConfigPaths
from openclose.skills.schema import Parameter, RequiredTool, Skill


_SLUG_RE = re.compile(r"[^a-z0-9]+")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
_SECTION_RE = re.compile(r"^# (.+?)\n(.*?)(?=^# |\Z)", re.DOTALL | re.MULTILINE)


def skills_dir() -> Path:
    """`<ConfigPaths.config_dir()>/<project>/skills/` — holds skill `.md` files + run logs."""
    config = get_config()
    d = ConfigPaths.project_runtime_dir(config.project_dir) / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def slugify(name: str) -> str:
    s = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return s or "skill"


def reserve_skill_slug(name: str) -> str:
    """Return an available (non-colliding) skill slug for `name`."""
    base = slugify(name)
    d = skills_dir()
    slug = base
    i = 2
    while (d / f"{slug}.md").exists():
        slug = f"{base}-{i}"
        i += 1
    return slug


# ── Frontmatter serialization ────────────────────────────────────────


def _yaml_scalar(v: Any) -> str:
    """Render a scalar value as a YAML scalar (quoted if needed)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = "" if v is None else str(v)
    if s == "":
        return '""'
    # Quote if it contains characters that would confuse a YAML parser
    if re.search(r"[:#\[\]{}&*!|>'\"%@`\n\r\t]", s) or s[0] in "-?,":
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if s.strip() != s:
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return s


def _format_frontmatter(
    *,
    name: str,
    slug: str,
    version: int,
    created_at: str,
    source_session: str,
    parameters: list[Parameter],
    required_tools: list[RequiredTool],
) -> str:
    lines = ["---"]
    lines.append(f"name: {_yaml_scalar(name)}")
    lines.append(f"slug: {_yaml_scalar(slug)}")
    lines.append(f"version: {version}")
    lines.append(f"created_at: {_yaml_scalar(created_at)}")
    lines.append(f"source_session: {_yaml_scalar(source_session)}")

    if parameters:
        lines.append("parameters:")
        for p in parameters:
            lines.append(f"  - name: {_yaml_scalar(p.name)}")
            lines.append(f"    type: {_yaml_scalar(p.type)}")
            lines.append(f"    required: {_yaml_scalar(p.required)}")
            lines.append(f"    default: {_yaml_scalar(p.default)}")
    else:
        lines.append("parameters: []")

    if required_tools:
        lines.append("required_tools:")
        for t in required_tools:
            lines.append(f"  - name: {_yaml_scalar(t.name)}")
            lines.append(f"    sensitive: {_yaml_scalar(t.sensitive)}")
    else:
        lines.append("required_tools: []")

    lines.append("---")
    return "\n".join(lines)


# ── Frontmatter parsing ──────────────────────────────────────────────


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
        inner = v[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\")
    return v


def _parse_bool(v: str) -> bool:
    return v.strip().lower() in ("true", "yes", "1")


def _parse_int(v: str, fallback: int = 0) -> int:
    try:
        return int(v.strip())
    except ValueError:
        return fallback


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Parse our hand-authored YAML frontmatter.

    Supports flat scalars plus two block-style lists of objects
    (``parameters`` and ``required_tools``). Any other structure is
    ignored — this is deliberately not a general YAML parser.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text

    fm_text = m.group(1)
    body = m.group(2)

    fm: dict[str, Any] = {}
    current_list_key: str | None = None
    current_list: list[dict[str, Any]] | None = None
    current_obj: dict[str, Any] | None = None

    for raw in fm_text.splitlines():
        if not raw.strip():
            continue

        # Top-level key: "key: value" or "key:" (starting a block list)
        if re.match(r"^[a-zA-Z_][\w]*:", raw):
            # close any open list (flushing the in-progress object first)
            if current_list_key is not None and current_list is not None:
                if current_obj is not None:
                    current_list.append(current_obj)
                fm[current_list_key] = current_list
                current_list_key = None
                current_list = None
                current_obj = None

            key, _, value = raw.partition(":")
            key = key.strip()
            value = value.strip()

            if value == "" or value == "[]":
                if value == "[]":
                    fm[key] = []
                else:
                    current_list_key = key
                    current_list = []
                    current_obj = None
                continue

            fm[key] = _unquote(value)
            continue

        # List item: "  - name: value" → starts a new object in the list
        m_item = re.match(r"^\s*-\s+([a-zA-Z_][\w]*)\s*:\s*(.*)$", raw)
        if m_item and current_list is not None:
            if current_obj is not None:
                current_list.append(current_obj)
            current_obj = {m_item.group(1): _unquote(m_item.group(2))}
            continue

        # Continuation key inside current list object: "    key: value"
        m_cont = re.match(r"^\s+([a-zA-Z_][\w]*)\s*:\s*(.*)$", raw)
        if m_cont and current_obj is not None:
            current_obj[m_cont.group(1)] = _unquote(m_cont.group(2))
            continue

    # flush trailing list / object
    if current_obj is not None and current_list is not None:
        current_list.append(current_obj)
    if current_list_key is not None and current_list is not None:
        fm[current_list_key] = current_list

    return fm, body


def _parse_sections(body: str) -> dict[str, str]:
    """Split markdown body into `{heading: content}` using `^# Heading` as delimiters."""
    sections: dict[str, str] = {}
    for m in _SECTION_RE.finditer(body):
        heading = m.group(1).strip()
        content = m.group(2).strip()
        sections[heading.lower()] = content
    return sections


# ── Read / write / list / delete ─────────────────────────────────────


@dataclass
class _SkillIO:
    """Intermediate representation for writing — accepts the form body sections."""

    name: str
    slug: str
    version: int
    created_at: str
    source_session: str
    parameters: list[Parameter]
    required_tools: list[RequiredTool]
    goal: str
    required_tools_prose: str
    procedure: str
    pitfalls: str
    verification: str

    def to_body(self) -> str:
        return (
            f"# Goal\n{self.goal.strip()}\n\n"
            f"# Required tools\n{self.required_tools_prose.strip()}\n\n"
            f"# Procedure\n{self.procedure.strip()}\n\n"
            f"# Pitfalls\n{self.pitfalls.strip()}\n\n"
            f"# Verification\n{self.verification.strip()}\n"
        )


def write_skill(skill: Skill) -> Path:
    """Write a skill to disk at `skills_dir() / <slug>.md`.

    Also creates the per-skill runs folder `skills_dir() / <slug>/`.
    """
    d = skills_dir()
    path = d / f"{skill.slug}.md"
    io = _SkillIO(
        name=skill.name,
        slug=skill.slug,
        version=skill.version,
        created_at=skill.created_at,
        source_session=skill.source_session,
        parameters=list(skill.parameters),
        required_tools=list(skill.required_tools),
        goal=skill.goal,
        required_tools_prose=skill.required_tools_prose,
        procedure=skill.procedure,
        pitfalls=skill.pitfalls,
        verification=skill.verification,
    )
    fm = _format_frontmatter(
        name=io.name,
        slug=io.slug,
        version=io.version,
        created_at=io.created_at,
        source_session=io.source_session,
        parameters=io.parameters,
        required_tools=io.required_tools,
    )
    path.write_text(f"{fm}\n\n{io.to_body()}", encoding="utf-8")
    (d / skill.slug).mkdir(parents=True, exist_ok=True)
    return path


def _skill_from_file(slug: str, path: Path) -> Skill | None:
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    fm, body = _parse_frontmatter(text)
    sections = _parse_sections(body)

    params: list[Parameter] = []
    for raw in fm.get("parameters", []) or []:
        if not isinstance(raw, dict):
            continue
        try:
            params.append(Parameter(
                name=str(raw.get("name", "")),
                type=str(raw.get("type", "string")),
                required=_parse_bool(str(raw.get("required", "false"))),
                default=str(raw.get("default", "")),
            ))
        except Exception:
            continue

    tools: list[RequiredTool] = []
    for raw in fm.get("required_tools", []) or []:
        if not isinstance(raw, dict):
            continue
        try:
            tools.append(RequiredTool(
                name=str(raw.get("name", "")),
                sensitive=_parse_bool(str(raw.get("sensitive", "false"))),
            ))
        except Exception:
            continue

    return Skill(
        name=str(fm.get("name", slug)),
        slug=str(fm.get("slug", slug)),
        version=_parse_int(str(fm.get("version", "1")), 1),
        created_at=str(fm.get("created_at", "")),
        source_session=str(fm.get("source_session", "")),
        parameters=params,
        required_tools=tools,
        goal=sections.get("goal", ""),
        required_tools_prose=sections.get("required tools", ""),
        procedure=sections.get("procedure", ""),
        pitfalls=sections.get("pitfalls", ""),
        verification=sections.get("verification", ""),
    )


def read_skill(slug: str) -> Skill | None:
    """Read a skill by slug, or return None if the file doesn't exist."""
    return _skill_from_file(slug, skills_dir() / f"{slug}.md")


def list_skills() -> list[Skill]:
    """List all skills, newest mtime first."""
    d = skills_dir()
    out: list[Skill] = []
    for path in sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True):
        skill = _skill_from_file(path.stem, path)
        if skill is not None:
            out.append(skill)
    return out


def delete_skill(slug: str) -> bool:
    """Delete a skill .md and its runs folder. Returns True if the .md existed."""
    d = skills_dir()
    path = d / f"{slug}.md"
    runs_dir = d / slug
    existed = path.is_file()
    if existed:
        path.unlink()
    if runs_dir.is_dir():
        shutil.rmtree(runs_dir, ignore_errors=True)
    return existed


# ── Runs listing ─────────────────────────────────────────────────────


def list_runs(slug: str, limit: int = 10) -> list[dict[str, Any]]:
    """List run metadata for a skill, newest first.

    Reads the first and last line of each `.jsonl` to derive
    start/end/status without loading the whole file; also reads the
    matching `.out.md` for an output preview if present.
    """
    d = skills_dir() / slug
    if not d.is_dir():
        return []

    entries = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []

    for jsonl in entries[:limit]:
        # Filename shape: `<iso_ts_with_dashes>-<ulid26>.jsonl`.
        stem = jsonl.stem
        run_id = stem[-26:] if len(stem) >= 26 else stem
        started_at = ""
        finished_at = ""
        status = "running"
        try:
            with jsonl.open("r", encoding="utf-8") as f:
                first_line = f.readline()
                if first_line:
                    try:
                        first = json.loads(first_line)
                        started_at = str(first.get("timestamp", ""))
                    except json.JSONDecodeError:
                        pass
                # Efficient-enough last line via a read-all on small files.
                content = first_line + f.read()
            lines = [ln for ln in content.splitlines() if ln.strip()]
            if lines:
                try:
                    last = json.loads(lines[-1])
                    if last.get("type") == "run_end":
                        status = str(last.get("status", "done"))
                        finished_at = str(last.get("timestamp", ""))
                except json.JSONDecodeError:
                    pass
        except OSError:
            continue

        out_path = jsonl.with_suffix(".out.md")
        preview = ""
        if out_path.is_file():
            try:
                text = out_path.read_text(encoding="utf-8")
                preview = text.strip().splitlines()[0][:200] if text.strip() else ""
            except OSError:
                pass

        out.append({
            "run_id": run_id,
            "file": jsonl.name,
            "started_at": started_at,
            "finished_at": finished_at,
            "status": status,
            "output_preview": preview,
        })

    return out
