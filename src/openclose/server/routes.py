"""API routes — sessions, messages, tools, config."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import StreamingResponse
from pydantic import BaseModel

from openclose.server.sse import stream_events
from openclose.session.session import SessionManager
from openclose.session.processor import SessionProcessor
from openclose.session.compaction import (
    estimate_messages_tokens,
    estimate_tool_schemas_tokens,
)
from openclose.config.paths import ConfigPaths
from openclose.permission.permission import PermissionEngine
from openclose.permission.broker import get_broker
from openclose.tool.tools.plan_broker import get_plan_broker
from openclose.tool.tools.ask_user_broker import get_ask_user_broker
from openclose.storage.db import get_db
from openclose.config.config import get_config
from openclose.session.cancel import get_cancel_registry
from openclose.tool.registry import ToolRegistry
from openclose.tool.tools import register_all_tools
from openclose.skills.schema import (
    GenerateRequest as SkillsGenerateRequest,
    SaveRequest as SkillsSaveRequest,
    RunRequest as SkillsRunRequest,
)
from openclose.jobs.schema import (
    JobSaveRequest,
    JobEnableRequest,
    CronParseRequest,
)

router = APIRouter()


# Tool name → action label for the "Files Modified" panel.
# Order of strength: created > modified > read.
_FILE_TOOL_ACTIONS: dict[str, str] = {
    "write": "created",
    "edit": "modified",
    "multiedit": "modified",
    "read": "read",
}

_FILE_ACTION_RANK: dict[str, int] = {
    "created": 3,
    "modified": 2,
    "read": 1,
}


def _resolve_tool_file_path(
    tool_name: str, args: dict[str, Any], project_dir: str
) -> str | None:
    """Resolve a tool call's file argument to an absolute path inside project_dir.

    Returns ``None`` when the tool isn't tracked, args don't contain a usable
    path, or the path can't be resolved.
    """
    if tool_name not in _FILE_TOOL_ACTIONS:
        return None
    raw = args.get("file_path")
    if not raw or not isinstance(raw, str):
        raw = args.get("path")
    if not raw or not isinstance(raw, str):
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = Path(project_dir) / p
    try:
        return str(p.resolve())
    except OSError:
        return None


def _build_files_processed(
    messages: list[dict[str, Any]], project_dir: str
) -> list[dict[str, str]]:
    """Mirror the live JS tracker: ordered list of files modified or created
    by the session. Read-only operations are excluded.

    Precedence: created > modified. On every operation whose action rank is
    ≥ the existing entry's, the file is moved to the most-recent (end)
    position so the UI's reverse iteration shows most-recent first.
    """
    import json as jsonmod

    files: dict[str, dict[str, str]] = {}
    for m in messages:
        for p in m.get("parts", []):
            if p.get("part_type") != "tool_call":
                continue
            tool_name = p.get("tool_name") or ""
            action = _FILE_TOOL_ACTIONS.get(tool_name)
            if not action or action == "read":
                continue
            content = p.get("content") or ""
            try:
                args = jsonmod.loads(content) if content else {}
            except (jsonmod.JSONDecodeError, TypeError):
                continue
            if not isinstance(args, dict):
                continue
            path = _resolve_tool_file_path(tool_name, args, project_dir)
            if not path:
                continue
            existing = files.get(path)
            if existing is None or _FILE_ACTION_RANK[action] >= _FILE_ACTION_RANK[existing["action"]]:
                files.pop(path, None)
                files[path] = {"path": path, "action": action, "tool": tool_name}
    return list(files.values())


# --- File-state reconstruction & structured diffs (for the file-preview dialog) ---


def _apply_edit(
    state: str, old: str, new: str, replace_all: bool
) -> tuple[str, bool]:
    """Forward-apply an Edit. Returns (new_state, applied)."""
    if not old or old not in state:
        return state, False
    if replace_all:
        return state.replace(old, new), True
    return state.replace(old, new, 1), True


def _undo_edit(
    state: str, old: str, new: str, replace_all: bool
) -> tuple[str, bool]:
    """Reverse an Edit. Returns (prior_state, undone)."""
    if not new or new not in state:
        return state, False
    if replace_all:
        return state.replace(new, old), True
    return state.replace(new, old, 1), True


def _op_pre_from_post(
    op: dict[str, Any], post_state: str
) -> str | None:
    """Reverse an op given its post-state. None when undoing isn't possible
    (Write erases prior state; Edit whose new_string isn't in post_state)."""
    tool = op.get("tool_name") or ""
    args = op.get("args") or {}
    if tool == "read":
        return post_state
    if tool == "write":
        return None
    if tool == "edit":
        pre, ok = _undo_edit(
            post_state,
            str(args.get("old_string") or ""),
            str(args.get("new_string") or ""),
            bool(args.get("replace_all", False)),
        )
        return pre if ok else None
    if tool == "multiedit":
        cur = post_state
        for ed in reversed(args.get("edits") or []):
            if not isinstance(ed, dict):
                return None
            cur, ok = _undo_edit(
                cur,
                str(ed.get("old_string") or ""),
                str(ed.get("new_string") or ""),
                bool(ed.get("replace_all", False)),
            )
            if not ok:
                return None
        return cur
    return None


def _op_post_from_pre(op: dict[str, Any], pre_state: str) -> str:
    """Forward-apply an op given its pre-state."""
    tool = op.get("tool_name") or ""
    args = op.get("args") or {}
    if tool == "read":
        return pre_state
    if tool == "write":
        return str(args.get("content") or "")
    if tool == "edit":
        post, _ = _apply_edit(
            pre_state,
            str(args.get("old_string") or ""),
            str(args.get("new_string") or ""),
            bool(args.get("replace_all", False)),
        )
        return post
    if tool == "multiedit":
        post = pre_state
        for ed in args.get("edits") or []:
            if not isinstance(ed, dict):
                continue
            post, _ = _apply_edit(
                post,
                str(ed.get("old_string") or ""),
                str(ed.get("new_string") or ""),
                bool(ed.get("replace_all", False)),
            )
        return post
    return pre_state


def _reconstruct_states(
    operations: list[dict[str, Any]], current_content: str | None
) -> list[tuple[str | None, str | None]]:
    """Per-op (pre, post) file-state pairs in chronological order.

    Strategy: backward-replay from ``current_content`` as far back as possible,
    then forward-replay to fill any gaps (notably the ops before/at a Write
    that the backward pass couldn't undo).
    """
    n = len(operations)
    pre_states: list[str | None] = [None] * n
    post_states: list[str | None] = [None] * n

    if current_content is not None and n > 0:
        post_states[n - 1] = current_content
        state: str | None = current_content
        for i in range(n - 1, -1, -1):
            assert state is not None
            pre = _op_pre_from_post(operations[i], state)
            if pre is None:
                break
            pre_states[i] = pre
            if i > 0:
                post_states[i - 1] = pre
            state = pre

    fwd_state: str | None = pre_states[0] if (n > 0 and pre_states[0] is not None) else None
    for i in range(n):
        op = operations[i]
        if pre_states[i] is not None:
            fwd_state = pre_states[i]
        elif fwd_state is not None:
            pre_states[i] = fwd_state

        if fwd_state is None:
            # Pre unknown — Write is the only op that establishes post anyway.
            if op.get("tool_name") == "write":
                post = str((op.get("args") or {}).get("content") or "")
                if post_states[i] is None:
                    post_states[i] = post
                fwd_state = post_states[i]
        else:
            computed_post = _op_post_from_pre(op, fwd_state)
            if post_states[i] is None:
                post_states[i] = computed_post
            fwd_state = post_states[i]

    return [(pre_states[i], post_states[i]) for i in range(n)]


def _structured_diff(
    pre: str, post: str, context: int = 3
) -> list[dict[str, Any]]:
    """``difflib.SequenceMatcher.get_grouped_opcodes`` → list of unified-diff
    hunks. Each hunk: ``{old_start, old_count, new_start, new_count, lines}``.
    Each line: ``{type: ' '|'-'|'+', old: int|None, new: int|None, text}``.
    Line numbers are 1-based; for replace blocks all ``-`` lines come before
    all ``+`` (matches unified-diff convention)."""
    import difflib

    pre_lines = pre.splitlines()
    post_lines = post.splitlines()
    matcher = difflib.SequenceMatcher(a=pre_lines, b=post_lines, autojunk=False)
    hunks: list[dict[str, Any]] = []
    for group in matcher.get_grouped_opcodes(n=context):
        old_start = group[0][1] + 1
        new_start = group[0][3] + 1
        old_count = group[-1][2] - group[0][1]
        new_count = group[-1][4] - group[0][3]
        lines: list[dict[str, Any]] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for k in range(i2 - i1):
                    lines.append({
                        "type": " ",
                        "old": i1 + k + 1,
                        "new": j1 + k + 1,
                        "text": pre_lines[i1 + k],
                    })
            elif tag == "replace":
                for k in range(i2 - i1):
                    lines.append({
                        "type": "-",
                        "old": i1 + k + 1,
                        "new": None,
                        "text": pre_lines[i1 + k],
                    })
                for k in range(j2 - j1):
                    lines.append({
                        "type": "+",
                        "old": None,
                        "new": j1 + k + 1,
                        "text": post_lines[j1 + k],
                    })
            elif tag == "delete":
                for k in range(i2 - i1):
                    lines.append({
                        "type": "-",
                        "old": i1 + k + 1,
                        "new": None,
                        "text": pre_lines[i1 + k],
                    })
            elif tag == "insert":
                for k in range(j2 - j1):
                    lines.append({
                        "type": "+",
                        "old": None,
                        "new": j1 + k + 1,
                        "text": post_lines[j1 + k],
                    })
        hunks.append({
            "old_start": old_start,
            "old_count": old_count,
            "new_start": new_start,
            "new_count": new_count,
            "lines": lines,
        })
    return hunks


def _build_op_diff(
    op: dict[str, Any], pre: str | None, post: str | None
) -> dict[str, Any] | None:
    """Build the JSON diff payload for one op. Returns None for read (the
    client renders the slice against current_content)."""
    tool = op.get("tool_name") or ""
    args = op.get("args") or {}
    if tool == "read":
        return None

    if tool == "multiedit":
        edits = [e for e in (args.get("edits") or []) if isinstance(e, dict)]
        if pre is not None and post is not None:
            sub_edits: list[dict[str, Any]] = []
            cur = pre
            for idx, ed in enumerate(edits):
                next_state, _ = _apply_edit(
                    cur,
                    str(ed.get("old_string") or ""),
                    str(ed.get("new_string") or ""),
                    bool(ed.get("replace_all", False)),
                )
                sub_edits.append({
                    "label": f"edit #{idx + 1}",
                    "hunks": _structured_diff(cur, next_state),
                })
                cur = next_state
            return {"kind": "multiedit", "reconstruction": "exact", "sub_edits": sub_edits}
        # Fallback: hunk-relative line numbers per sub-edit.
        sub_edits = []
        for idx, ed in enumerate(edits):
            sub_edits.append({
                "label": f"edit #{idx + 1}",
                "hunks": _structured_diff(
                    str(ed.get("old_string") or ""),
                    str(ed.get("new_string") or ""),
                ),
            })
        return {"kind": "multiedit", "reconstruction": "fallback", "sub_edits": sub_edits}

    if pre is not None and post is not None:
        return {
            "kind": tool,
            "reconstruction": "exact",
            "hunks": _structured_diff(pre, post),
        }

    # Fallback paths for write/edit when reconstruction failed.
    if tool == "edit":
        return {
            "kind": "edit",
            "reconstruction": "fallback",
            "hunks": _structured_diff(
                str(args.get("old_string") or ""),
                str(args.get("new_string") or ""),
            ),
        }
    if tool == "write":
        return {
            "kind": "write",
            "reconstruction": "fallback",
            "hunks": _structured_diff("", str(args.get("content") or "")),
        }
    return None


# --- Static API routes (must be before any {session_id} path params) ---

@router.get("/api/files")
async def search_files(q: str = "", limit: int = 20) -> JSONResponse:
    """Search files/folders in the project directory."""
    import os
    from pathlib import Path
    from openclose.file.ignore import IgnoreManager

    config = get_config()
    root = Path(config.project_dir).resolve()
    ignore = IgnoreManager(root)
    query = q.lower()
    results: list[dict[str, str]] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames
            if not ignore.is_ignored(dp / d) and not d.startswith(".")
        ]
        rel_dir = dp.relative_to(root)

        if str(rel_dir) != ".":
            rel_str = rel_dir.as_posix()
            if query in rel_str.lower():
                results.append({"path": rel_str + "/", "type": "dir"})

        for fname in filenames:
            fp = dp / fname
            if ignore.is_ignored(fp):
                continue
            rel = fp.relative_to(root).as_posix()
            if query in rel.lower():
                results.append({"path": rel, "type": "file"})
            if len(results) >= limit:
                break
        if len(results) >= limit:
            break

    return JSONResponse(results)


@router.get("/api/files/resolve")
async def resolve_file(name: str = "") -> JSONResponse:
    """Resolve a filename to its full path inside the project directory."""
    import os

    config = get_config()
    root = Path(config.project_dir).resolve()
    target = name.strip()
    if not target:
        return JSONResponse({"path": ""})
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            if fname == target:
                return JSONResponse({"path": (Path(dirpath) / fname).as_posix()})
    return JSONResponse({"path": ""})


@router.get("/api/files/tree")
async def list_tree(path: str = "") -> JSONResponse:
    """List one directory level under ``path`` (relative to project_dir).

    Empty ``path`` lists the project root. Used by the Explore Files UI
    panel for lazy-loading the file tree. Sandboxed to project_dir.
    """
    from openclose.file.ignore import IgnoreManager

    config = get_config()
    root = Path(config.project_dir).resolve()

    target = root if not path else (root / path)
    try:
        resolved = target.resolve()
    except OSError:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    try:
        resolved.relative_to(root)
    except ValueError:
        return JSONResponse({"error": "path outside project"}, status_code=403)
    if not resolved.is_dir():
        return JSONResponse({"error": "not a directory"}, status_code=404)

    ignore = IgnoreManager(root)
    dirs: list[dict[str, str]] = []
    files: list[dict[str, str]] = []
    try:
        for entry in resolved.iterdir():
            if entry.name.startswith("."):
                continue
            if ignore.is_ignored(entry):
                continue
            rel = entry.relative_to(root).as_posix()
            item = {"name": entry.name, "path": rel}
            if entry.is_dir():
                item["type"] = "dir"
                dirs.append(item)
            elif entry.is_file():
                item["type"] = "file"
                files.append(item)
    except OSError:
        return JSONResponse({"error": "cannot read directory"}, status_code=500)

    dirs.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())
    return JSONResponse(dirs + files)


@router.get("/api/files/content")
async def file_content(path: str = "") -> JSONResponse:
    """Return the text content of a file inside project_dir for the
    Explore Files viewer. Binary files return ``binary=true`` with no
    content. Capped at 200KB; larger files set ``truncated=true``.
    """
    from openclose.file.binary import is_binary

    if not path:
        return JSONResponse({"error": "path is required"}, status_code=400)

    config = get_config()
    root = Path(config.project_dir).resolve()

    p = Path(path)
    if not p.is_absolute():
        p = root / p
    try:
        resolved = p.resolve()
    except OSError:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    try:
        resolved.relative_to(root)
    except ValueError:
        return JSONResponse({"error": "path outside project"}, status_code=403)

    if not resolved.is_file():
        return JSONResponse({
            "path": resolved.relative_to(root).as_posix(),
            "exists": False,
            "binary": False,
            "content": None,
            "total_lines": 0,
            "truncated": False,
        })

    is_bin = is_binary(resolved)
    if is_bin:
        return JSONResponse({
            "path": resolved.relative_to(root).as_posix(),
            "exists": True,
            "binary": True,
            "content": None,
            "total_lines": 0,
            "truncated": False,
        })

    MAX_BYTES = 200 * 1024
    truncated = False
    content = ""
    try:
        size = resolved.stat().st_size
        if size > MAX_BYTES:
            truncated = True
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(MAX_BYTES)
        else:
            content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return JSONResponse({"error": "cannot read file"}, status_code=500)

    total_lines = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
    return JSONResponse({
        "path": resolved.relative_to(root).as_posix(),
        "exists": True,
        "binary": False,
        "content": content,
        "total_lines": total_lines,
        "truncated": truncated,
    })


class BashRequest(BaseModel):
    command: str


@router.post("/api/bash")
async def run_bash(req: BashRequest) -> JSONResponse:
    """Execute a bash command in the project directory."""
    from openclose.util.process import find_bash, run

    bash_path = find_bash()
    if bash_path is None:
        return JSONResponse({
            "stdout": "",
            "stderr": (
                "bash not found. Install Git Bash or WSL on Windows, "
                "or ensure `bash` is available on macOS/Linux."
            ),
            "returncode": 127,
        })

    config = get_config()
    result = await run(bash_path, "-c", req.command, cwd=config.project_dir, timeout=120.0)
    return JSONResponse({
        "stdout": result.stdout,
        "stderr": result.stderr,
        "returncode": result.returncode,
    })


def _get_templates() -> Any:
    from openclose.server.app import templates
    return templates


# --- HTML pages ---

@router.get("/")
async def index() -> RedirectResponse:
    """Resume the most recent session, or create a new one if none exist."""
    db = get_db()
    mgr = SessionManager(db)
    sessions = mgr.list_sessions()
    config = get_config()
    if sessions:
        session = sessions[0]
        # Clean up stale empty sessions from previous runs
        mgr.cleanup_empty_sessions(keep_session_id=session.id)
    else:
        # Reuse an existing empty session if available
        agent = config.default_agent
        empty = mgr.get_empty_session(agent=agent)
        session = empty if empty else mgr.create_session(title="", agent=agent)
    return RedirectResponse(url=f"/session/{session.id}", status_code=302)



def _segment_parts(parts: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split an ordered part list into visual segments.

    Mirrors live streaming: a new bubble starts at the first text/tool_call
    that follows one or more tool_results. Consecutive tool_results from
    parallel tool calls stay in the same segment as their tool_calls (in
    live, parallel results just attach to existing <details> elements
    without spawning new divs).
    """
    segments: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    prev_was_result = False
    for p in parts:
        is_result = p["part_type"] == "tool_result"
        if prev_was_result and not is_result and current:
            segments.append(current)
            current = []
        current.append(p)
        prev_was_result = is_result
    if current:
        segments.append(current)
    return segments


@router.get("/session/{session_id}", response_class=HTMLResponse)
async def session_page(request: Request, session_id: str) -> HTMLResponse:
    """Chat session page."""
    db = get_db()
    mgr = SessionManager(db)
    session = mgr.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    raw_messages = mgr.get_messages(session_id)
    # Attach parts to each message for template rendering
    messages = []
    for m in raw_messages:
        parts = mgr.get_message_parts(m.id)
        parts_dicts = [
            {"id": p.id, "part_type": p.part_type, "content": p.content,
             "tool_name": p.tool_name, "tool_call_id": p.tool_call_id,
             "metadata_json": p.metadata_json}
            for p in parts
        ]
        messages.append({
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "created_at": m.created_at,
            "parts": parts_dicts,
            "segments": _segment_parts(parts_dicts) or [[]],
        })
    # Compute initial context usage for the context bar
    config = get_config()
    if session and raw_messages:
        messages_with_parts = mgr.get_messages_with_parts(session_id)
        llm_messages = SessionProcessor._reconstruct_llm_messages(
            messages_with_parts
        )
        registry = ToolRegistry()
        register_all_tools(registry, config.project_dir)
        tool_schemas = registry.get_schemas()
        msg_tokens = estimate_messages_tokens(llm_messages)
        tools_tokens = estimate_tool_schemas_tokens(tool_schemas)
        context_info: dict[str, object] = {
            "used": msg_tokens + tools_tokens,
            "max": config.max_context_tokens,
            "messages_tokens": msg_tokens,
            "tools_tokens": tools_tokens,
        }
    else:
        context_info = {
            "used": 0,
            "max": config.max_context_tokens,
            "messages_tokens": 0,
            "tools_tokens": 0,
        }

    project_dir_resolved = str(Path(config.project_dir).resolve())
    files_processed = _build_files_processed(messages, project_dir_resolved)

    resp: HTMLResponse = _get_templates().TemplateResponse(
        request, "session.html",
        {"session": session, "messages": messages, "context_info": context_info,
         "project_dir": project_dir_resolved, "files_processed": files_processed},
    )
    return resp


# --- API endpoints ---

class CreateSessionRequest(BaseModel):
    title: str = ""
    agent: str = ""


class Attachment(BaseModel):
    path: str
    start_line: int
    end_line: int
    text: str


class SendMessageRequest(BaseModel):
    content: str
    attachments: list[Attachment] = []


@router.post("/api/sessions")
async def create_session(req: CreateSessionRequest) -> JSONResponse:
    """Create a new session, reusing an existing empty one if available."""
    db = get_db()
    mgr = SessionManager(db)
    if req.agent:
        err = _validate_primary_agent(req.agent)
        if err:
            return JSONResponse({"error": err}, status_code=400)
    agent = req.agent or get_config().default_agent
    existing = mgr.get_empty_session(agent=agent)
    if existing:
        if req.title and req.title != existing.title:
            mgr.update_title(existing.id, req.title)
            existing.title = req.title
        session = existing
    else:
        session = mgr.create_session(title=req.title, agent=agent)
    return JSONResponse({"id": session.id, "title": session.title})


class ForkSessionRequest(BaseModel):
    agent: str = ""
    up_to_message_id: str = ""
    up_to_part_id: str = ""


@router.post("/api/sessions/{session_id}/fork")
async def fork_session(session_id: str, req: ForkSessionRequest) -> JSONResponse:
    """Fork a session, preserving message history.

    With ``agent`` set, switches the forked session's agent; otherwise the
    source's agent is inherited. With ``up_to_message_id`` set, only
    history up to and including that message is copied. With
    ``up_to_part_id`` additionally set, parts within the target message
    are also truncated — required for forking from a specific UI bubble
    when a single DB message renders as multiple visual segments.
    """
    db = get_db()
    mgr = SessionManager(db)
    if req.agent:
        err = _validate_primary_agent(req.agent)
        if err:
            return JSONResponse({"error": err}, status_code=400)
    agent = req.agent or None
    new_session = mgr.fork_session(
        session_id,
        agent=agent,
        up_to_message_id=req.up_to_message_id,
        up_to_part_id=req.up_to_part_id,
    )
    if new_session is None:
        return JSONResponse(
            {"error": "Source session or target message/part not found"},
            status_code=404,
        )
    return JSONResponse({"id": new_session.id, "title": new_session.title})


@router.get("/api/sessions")
async def list_sessions(q: str = "") -> JSONResponse:
    """List sessions, optionally filtering by a content-search query."""
    db = get_db()
    mgr = SessionManager(db)
    sessions = mgr.search_sessions(q) if q.strip() else mgr.list_sessions()
    return JSONResponse([
        {"id": s.id, "title": s.title, "agent": s.agent, "updated_at": str(s.updated_at)}
        for s in sessions
    ])


@router.get("/api/sessions/{session_id}/exists")
async def session_exists(session_id: str) -> JSONResponse:
    """Lightweight existence check for a session id."""
    db = get_db()
    mgr = SessionManager(db)
    sess = mgr.get_session(session_id)
    return JSONResponse({"exists": sess is not None})


@router.get("/api/sessions/{session_id}/messages")
async def get_messages(session_id: str) -> JSONResponse:
    """Get messages for a session."""
    db = get_db()
    mgr = SessionManager(db)
    messages = mgr.get_messages(session_id)
    result = []
    for m in messages:
        parts = mgr.get_message_parts(m.id)
        result.append({
            "id": m.id, "role": m.role, "content": m.content,
            "created_at": str(m.created_at),
            "parts": [
                {"id": p.id, "part_type": p.part_type, "content": p.content,
                 "tool_name": p.tool_name, "tool_call_id": p.tool_call_id,
                 "metadata_json": p.metadata_json}
                for p in parts
            ],
        })
    return JSONResponse(result)


@router.get("/api/sessions/{session_id}/file-events")
async def session_file_events(session_id: str, path: str = "") -> JSONResponse:
    """Return the read/write/edit/multiedit operations performed on a single
    file in this session, plus the file's current on-disk contents.

    Drives the file-preview dialog opened by clicking a row in the
    "Files Modified" sidebar panel.
    """
    import json as jsonmod
    from openclose.file.binary import is_binary

    if not path:
        return JSONResponse({"error": "path is required"}, status_code=400)

    db = get_db()
    mgr = SessionManager(db)
    session = mgr.get_session(session_id)
    if session is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    config = get_config()
    project_dir = str(Path(config.project_dir).resolve())

    # Resolve the requested path
    p = Path(path)
    if not p.is_absolute():
        p = Path(project_dir) / p
    try:
        resolved = p.resolve()
    except OSError:
        return JSONResponse({"error": "invalid path"}, status_code=400)
    target = str(resolved)

    # Sandbox: paths inside project_dir are always allowed (covers the live
    # case — the in-progress turn's tool_calls aren't persisted yet, so they
    # wouldn't pass a "must appear in session ops" gate). Paths *outside*
    # project_dir (legitimate via the read tool, which has no sandbox) must
    # appear in the session's persisted ops to be returned, which prevents
    # arbitrary remote-host file enumeration via the API.
    try:
        resolved.relative_to(Path(project_dir))
        inside_project = True
    except ValueError:
        inside_project = False

    # Walk message parts in chronological order; collect ops on this file.
    operations: list[dict[str, Any]] = []
    target_in_session = False
    for m in mgr.get_messages(session_id):
        for part in mgr.get_message_parts(m.id):
            if part.part_type != "tool_call":
                continue
            tool_name = part.tool_name or ""
            if tool_name not in _FILE_TOOL_ACTIONS:
                continue
            try:
                args = jsonmod.loads(part.content) if part.content else {}
            except (jsonmod.JSONDecodeError, TypeError):
                continue
            if not isinstance(args, dict):
                continue
            arg_path = _resolve_tool_file_path(tool_name, args, project_dir)
            if arg_path != target:
                continue
            target_in_session = True
            # Reads are tracked for the sandbox check above but excluded from
            # the dialog: it only shows mutations (write/edit/multiedit).
            if tool_name == "read":
                continue
            operations.append({
                "tool_name": tool_name,
                "args": args,
                "tool_call_id": part.tool_call_id or "",
            })

    if not inside_project and not target_in_session:
        return JSONResponse({"error": "path not in this session"}, status_code=403)

    # Read current file content (cap at 200KB)
    MAX_BYTES = 200 * 1024
    current_content: str | None = None
    too_large = False
    is_bin = False
    exists = resolved.is_file()
    if exists:
        is_bin = is_binary(resolved)
        if not is_bin:
            try:
                size = resolved.stat().st_size
                if size > MAX_BYTES:
                    too_large = True
                    with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                        current_content = f.read(MAX_BYTES)
                else:
                    current_content = resolved.read_text(encoding="utf-8", errors="replace")
            except OSError:
                current_content = None

    # Reconstruct (pre, post) state per op and attach a structured diff.
    state_pairs = _reconstruct_states(operations, current_content)
    for op, (pre, post) in zip(operations, state_pairs):
        diff = _build_op_diff(op, pre, post)
        if diff is not None:
            op["diff"] = diff

    return JSONResponse({
        "path": target,
        "exists": exists,
        "binary": is_bin,
        "current_content": current_content,
        "current_too_large": too_large,
        "operations": operations,
    })


@router.post("/api/sessions/{session_id}/messages")
async def send_message(session_id: str, req: SendMessageRequest) -> StreamingResponse:
    """Send a message and stream the response via SSE."""
    db = get_db()
    config = get_config()

    # Set up tool registry
    registry = ToolRegistry()
    register_all_tools(registry, config.project_dir)

    # Look up the session's agent
    mgr = SessionManager(db)
    session = mgr.get_session(session_id)
    agent_name = session.agent if session else config.default_agent

    permission_engine = PermissionEngine.for_session(session_id)
    permission_broker = get_broker()
    plan_broker = get_plan_broker()
    ask_user_broker = get_ask_user_broker()

    cancel_registry = get_cancel_registry()
    cancel_event = cancel_registry.register(session_id)

    processor = SessionProcessor(
        db=db,
        session_id=session_id,
        agent_name=agent_name,
        tool_executor=registry.execute,
        tool_schemas=registry.get_schemas(),
        project_dir=config.project_dir,
        permission_engine=permission_engine,
        permission_broker=permission_broker,
        plan_broker=plan_broker,
        ask_user_broker=ask_user_broker,
        cancel_event=cancel_event,
    )

    attachments = [a.model_dump() for a in req.attachments]

    async def _stream_with_cleanup() -> AsyncIterator[str]:
        try:
            async for chunk in stream_events(
                processor.process(req.content, attachments=attachments)
            ):
                yield chunk
        finally:
            cancel_registry.unregister(session_id)

    return StreamingResponse(
        _stream_with_cleanup(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/sessions/{session_id}/interrupt")
async def interrupt_session(session_id: str) -> JSONResponse:
    """Interrupt an active LLM generation for a session."""
    cancel_registry = get_cancel_registry()
    permission_broker = get_broker()
    permission_broker.cancel_session(session_id)
    plan_broker = get_plan_broker()
    plan_broker.cancel_session(session_id)
    ask_user_broker = get_ask_user_broker()
    ask_user_broker.cancel_session(session_id)
    found = cancel_registry.cancel(session_id)
    return JSONResponse({"ok": found, "interrupted": found})


class PermissionReplyRequest(BaseModel):
    reply: str  # "once", "always", "reject"


@router.post("/api/permissions/{request_id}/reply")
async def permission_reply(request_id: str, req: PermissionReplyRequest) -> JSONResponse:
    """Reply to a pending permission request."""
    if req.reply not in ("once", "always", "reject"):
        return JSONResponse({"error": "Invalid reply; must be once, always, or reject"}, status_code=400)
    broker = get_broker()
    found = broker.reply(request_id, req.reply)  # type: ignore[arg-type]
    if not found:
        return JSONResponse({"error": "Permission request not found or already resolved"}, status_code=404)
    return JSONResponse({"ok": True})


@router.post("/api/sessions/{session_id}/skip-permissions")
async def toggle_skip_permissions(session_id: str) -> JSONResponse:
    """Toggle skip-all permissions mode for a session."""
    engine = PermissionEngine.for_session(session_id)
    engine.set_skip_all(not engine.skip_all)
    return JSONResponse({"skip_all": engine.skip_all})


@router.get("/api/sessions/{session_id}/skip-permissions")
async def get_skip_permissions(session_id: str) -> JSONResponse:
    """Get current skip-all permissions status."""
    engine = PermissionEngine.for_session(session_id)
    return JSONResponse({"skip_all": engine.skip_all})


class PlanReplyRequest(BaseModel):
    action: str  # "execute", "execute_clear", "reject", "revise"
    feedback: str = ""


@router.post("/api/plan/{request_id}/reply")
async def plan_reply(request_id: str, req: PlanReplyRequest) -> JSONResponse:
    """Reply to a pending plan review request."""
    if req.action not in ("execute", "execute_clear", "reject", "revise"):
        return JSONResponse(
            {"error": "Invalid action; must be execute, execute_clear, reject, or revise"},
            status_code=400,
        )
    broker = get_plan_broker()
    found = broker.reply(request_id, req.action, req.feedback)
    if not found:
        return JSONResponse({"error": "Plan review not found or already resolved"}, status_code=404)
    return JSONResponse({"ok": True})


class AskUserReplyRequest(BaseModel):
    answers: list[dict[str, str]]


@router.post("/api/ask-user/{request_id}/reply")
async def ask_user_reply(request_id: str, req: AskUserReplyRequest) -> JSONResponse:
    """Reply to a pending ask_user request with the user's answers."""
    broker = get_ask_user_broker()
    found = broker.reply(request_id, req.answers)
    if not found:
        return JSONResponse(
            {"error": "Ask user request not found or already resolved"},
            status_code=404,
        )
    return JSONResponse({"ok": True})


class SwitchAgentRequest(BaseModel):
    agent: str


@router.post("/api/sessions/{session_id}/agent")
async def switch_agent(session_id: str, req: SwitchAgentRequest) -> JSONResponse:
    """Switch a session's agent in-place."""
    err = _validate_primary_agent(req.agent)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    db = get_db()
    mgr = SessionManager(db)
    success = mgr.update_agent(session_id, req.agent)
    if not success:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return JSONResponse({"ok": True, "agent": req.agent})


class SwitchModelRequest(BaseModel):
    provider: str
    model: str


@router.post("/api/sessions/{session_id}/model")
async def switch_model(
    session_id: str, req: SwitchModelRequest,
) -> JSONResponse:
    """Switch a session's provider and model in-place."""
    config = get_config()
    if not any(p.name == req.provider for p in config.providers):
        return JSONResponse(
            {"error": f"Unknown provider: {req.provider!r}"},
            status_code=400,
        )
    # Model is intentionally free-form: some endpoints (vLLM, llama.cpp,
    # OpenRouter aliases) accept arbitrary strings, and the declared
    # ``models`` list is only a UI hint.
    db = get_db()
    mgr = SessionManager(db)
    success = mgr.update_provider_and_model(
        session_id, req.provider, req.model,
    )
    if not success:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return JSONResponse({
        "ok": True, "provider": req.provider, "model": req.model,
    })


@router.get("/api/sessions/{session_id}/model")
async def get_session_model(session_id: str) -> JSONResponse:
    """Return the active (provider, model) for a session.

    Empty fields mean "use the configured default" — the picker resolves
    those itself, so the client just gets the raw session state plus the
    config defaults for rendering.
    """
    db = get_db()
    mgr = SessionManager(db)
    s = mgr.get_session(session_id)
    if s is None:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    config = get_config()
    effective_provider = (
        s.provider
        or config.default_provider
        or (config.providers[0].name if config.providers else "")
    )
    effective_model = s.model
    if not effective_model:
        pcfg = next(
            (p for p in config.providers if p.name == effective_provider),
            None,
        )
        if pcfg:
            effective_model = pcfg.default_model
    return JSONResponse({
        "provider": s.provider,
        "model": s.model,
        "effective_provider": effective_provider,
        "effective_model": effective_model,
    })


@router.get("/api/models")
async def list_models() -> JSONResponse:
    """Flat list of (provider, model) entries declared in config.toml."""
    config = get_config()
    items: list[dict[str, str]] = []
    for p in config.providers:
        # Prefer declared list; fall back to default_model only if non-empty.
        names = list(p.models) if p.models else (
            [p.default_model] if p.default_model else []
        )
        for m in names:
            items.append({
                "provider": p.name,
                "model": m,
                "label": f"{p.name} / {m}",
            })
    return JSONResponse(items)


@router.post("/api/sessions/{session_id}/plan-in-context")
async def toggle_plan_in_context(session_id: str) -> JSONResponse:
    """Toggle plan_in_context for a session."""
    db = get_db()
    mgr = SessionManager(db)
    session = mgr.get_session(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    new_val = not session.plan_in_context
    mgr.update_plan_in_context(session_id, new_val)
    return JSONResponse({"ok": True, "plan_in_context": new_val})


@router.get("/api/sessions/{session_id}/video-compatible")
async def get_video_compatible(session_id: str) -> JSONResponse:
    """Get current video_compatible status."""
    db = get_db()
    mgr = SessionManager(db)
    session = mgr.get_session(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return JSONResponse({"video_compatible": session.video_compatible})


@router.post("/api/sessions/{session_id}/video-compatible")
async def toggle_video_compatible(session_id: str) -> JSONResponse:
    """Toggle video_compatible for a session."""
    db = get_db()
    mgr = SessionManager(db)
    session = mgr.get_session(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    new_val = not session.video_compatible
    mgr.update_video_compatible(session_id, new_val)
    return JSONResponse({"ok": True, "video_compatible": new_val})


@router.get("/api/browser/screenshot")
async def browser_screenshot() -> Response:
    """Return the current browser page as a PNG, or 503 if unavailable.

    On a lifecycle error (detached frame, closed target, crashed page,
    disconnected browser) we drop the singleton so the next poll reconnects
    from scratch — otherwise a transient crash leaves the cache wedged and
    every subsequent poll returns 503 against the same dead handle.
    """
    from openclose.tool.tools.browser_automation_shared import (
        acquire_singleton_browser,
        reset_singleton_browser,
        _pick_or_create_page,
        _is_page_lifecycle_error,
    )
    try:
        _pw, _browser, context = await acquire_singleton_browser()
        page = await _pick_or_create_page(context)
        png = await page.screenshot(type="png", full_page=False)
    except Exception as e:
        if _is_page_lifecycle_error(e):
            await reset_singleton_browser()
        return Response(status_code=503, content=b"screenshot unavailable")
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/sessions/{session_id}/plan")
async def get_plan(session_id: str) -> JSONResponse:
    """Get plan status and content for a session."""
    db = get_db()
    config = get_config()
    mgr = SessionManager(db)
    session = mgr.get_session(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    plan_path = ConfigPaths.project_runtime_dir(config.project_dir) / "plan.md"
    exists = plan_path.is_file()
    content = plan_path.read_text() if exists else ""
    return JSONResponse({
        "exists": exists,
        "plan_in_context": session.plan_in_context,
        "content": content,
    })


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> JSONResponse:
    """Delete a session."""
    db = get_db()
    mgr = SessionManager(db)
    success = mgr.delete_session(session_id)
    PermissionEngine.remove_session(session_id)
    return JSONResponse({"deleted": success})


class RenameRequest(BaseModel):
    title: str


@router.patch("/api/sessions/{session_id}/rename")
async def rename_session(session_id: str, req: RenameRequest) -> JSONResponse:
    """Rename a session."""
    db = get_db()
    mgr = SessionManager(db)
    success = mgr.update_title(session_id, req.title)
    return JSONResponse({"ok": success, "title": req.title})


@router.post("/api/sessions/{session_id}/undo")
async def undo_message(session_id: str) -> JSONResponse:
    """Remove the last user+assistant message pair."""
    db = get_db()
    mgr = SessionManager(db)
    messages = mgr.get_messages(session_id)
    removed = 0
    # Remove from the end: assistant then user
    for msg in reversed(messages):
        if removed >= 2:
            break
        with db.get_session() as s:
            from openclose.storage.schema import Message, MessagePart
            from sqlmodel import select
            parts = s.exec(select(MessagePart).where(MessagePart.message_id == msg.id)).all()
            for p in parts:
                s.delete(p)
            loaded = s.get(Message, msg.id)
            if loaded:
                s.delete(loaded)
            s.commit()
        removed += 1
    return JSONResponse({"ok": True, "removed": removed})


@router.get("/api/sessions/{session_id}/export")
async def export_session(session_id: str) -> JSONResponse:
    """Export session as structured data."""
    db = get_db()
    mgr = SessionManager(db)
    session = mgr.get_session(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    messages = mgr.get_messages(session_id)
    return JSONResponse({
        "session": {"id": session.id, "title": session.title, "agent": session.agent},
        "messages": [
            {"role": m.role, "content": m.content, "created_at": str(m.created_at)}
            for m in messages
        ],
    })


@router.post("/api/sessions/{session_id}/compact")
async def compact_session(session_id: str) -> JSONResponse:
    """Trigger context compaction on a session."""
    db = get_db()
    mgr = SessionManager(db)
    messages = mgr.get_messages(session_id)
    from openclose.session.compaction import compact_messages
    msg_dicts = [{"role": m.role, "content": m.content} for m in messages]
    config = get_config()
    _, compacted, _ = compact_messages(
        msg_dicts,
        max_tokens=config.max_context_tokens,
    )
    return JSONResponse({"ok": True, "compacted": compacted, "message_count": len(messages)})


@router.get("/api/agents")
async def list_agents() -> JSONResponse:
    """List switchable (primary) agents."""
    from openclose.agent.agent import AgentMode
    from openclose.agent.agent import list_agents as _list_agents
    agents = _list_agents()
    return JSONResponse([
        {"name": a.name, "description": a.description}
        for a in agents
        if a.mode == AgentMode.PRIMARY
    ])


def _validate_primary_agent(name: str) -> str | None:
    """Return an error message if ``name`` is not a switchable primary
    agent, otherwise None."""
    from openclose.agent.agent import AgentMode
    from openclose.agent.agent import list_agents as _list_agents

    primary = {a.name: a for a in _list_agents() if a.mode == AgentMode.PRIMARY}
    if name not in primary:
        return f"Unknown or non-switchable agent: {name!r}"
    return None


@router.get("/api/config")
async def get_config_endpoint() -> JSONResponse:
    """Get current configuration."""
    config = get_config()
    return JSONResponse(config.model_dump())


# ---------------------------------------------------------------------------
# Recorder + tasks
# ---------------------------------------------------------------------------


class AnnotateRequest(BaseModel):
    recording_id: str
    name: str
    description: str = ""


@router.get("/api/recorder/status")
async def recorder_status() -> JSONResponse:
    from openclose.recorder import get_active_recording
    return JSONResponse({"active": get_active_recording()})


@router.post("/api/recorder/start")
async def recorder_start() -> JSONResponse:
    from openclose.recorder import start_recording, RecorderError
    try:
        return JSONResponse(await start_recording())
    except RecorderError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/recorder/stop")
async def recorder_stop() -> JSONResponse:
    from openclose.recorder import stop_recording, RecorderError
    try:
        return JSONResponse(await stop_recording())
    except RecorderError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/recorder/cancel")
async def recorder_cancel() -> JSONResponse:
    from openclose.recorder.recorder import cancel_recording
    await cancel_recording()
    return JSONResponse({"ok": True})


@router.post("/api/recorder/annotate")
async def recorder_annotate(req: AnnotateRequest) -> JSONResponse:
    from openclose.recorder import annotate_recording, RecorderError
    try:
        task = await annotate_recording(req.recording_id, req.name, req.description)
    except RecorderError as e:
        return JSONResponse({"error": str(e)}, status_code=409)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({
        "ok": True,
        **task.to_summary(),
        "path": str(task.path),
    })


@router.get("/api/tasks")
async def tasks_list() -> JSONResponse:
    from openclose.recorder import list_tasks
    return JSONResponse([t.to_summary() for t in list_tasks()])


@router.get("/api/tasks/{slug}")
async def tasks_get(slug: str) -> JSONResponse:
    from openclose.recorder import read_task
    task = read_task(slug)
    if task is None:
        return JSONResponse({"error": "Task not found"}, status_code=404)
    return JSONResponse({**task.to_summary(), "body": task.body})


@router.delete("/api/tasks/{slug}")
async def tasks_delete(slug: str) -> JSONResponse:
    from openclose.recorder import delete_task
    return JSONResponse({"deleted": delete_task(slug)})


# ---------------------------------------------------------------------------
# Skills — distilled from chat history, executed headlessly (phase 2: cron)
# ---------------------------------------------------------------------------


@router.get("/api/skills/tools")
async def skills_tools() -> JSONResponse:
    """Enumerate tools the form's required_tools picker can offer."""
    from openclose.skills.builder import SENSITIVE_TOOLS
    config = get_config()
    registry = ToolRegistry()
    register_all_tools(registry, config.project_dir)
    out = [
        {"name": t.name, "sensitive": t.name in SENSITIVE_TOOLS}
        for t in registry.list_tools()
    ]
    out.sort(key=lambda d: str(d["name"]))
    return JSONResponse(out)


@router.post("/api/skills/generate")
async def skills_generate(req: SkillsGenerateRequest) -> JSONResponse:
    from openclose.skills.builder import generate_skill_form, SkillBuilderError
    try:
        form = await generate_skill_form(req.session_id, req.user_prompt)
    except SkillBuilderError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse({"form": form.model_dump()})


@router.post("/api/skills")
async def skills_create(req: SkillsSaveRequest) -> JSONResponse:
    from datetime import datetime, timezone
    from openclose.skills.schema import Skill
    from openclose.skills.storage import reserve_skill_slug, write_skill, read_skill
    slug = (req.slug or "").strip() or reserve_skill_slug(req.name)
    if read_skill(slug) is not None:
        slug = reserve_skill_slug(req.name)
    skill = Skill(
        name=req.name,
        slug=slug,
        version=1,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source_session=req.source_session or "",
        parameters=list(req.parameters),
        required_tools=list(req.required_tools),
        goal=req.goal,
        required_tools_prose=req.required_tools_prose,
        procedure=req.procedure,
        pitfalls=req.pitfalls,
        verification=req.verification,
    )
    path = write_skill(skill)
    return JSONResponse({"ok": True, "slug": slug, "path": str(path)})


@router.put("/api/skills/{slug}")
async def skills_update(slug: str, req: SkillsSaveRequest) -> JSONResponse:
    from openclose.skills.schema import Skill
    from openclose.skills.storage import read_skill, write_skill
    existing = read_skill(slug)
    if existing is None:
        return JSONResponse({"error": "Skill not found"}, status_code=404)
    skill = Skill(
        name=req.name,
        slug=slug,
        version=existing.version + 1,
        created_at=existing.created_at,
        source_session=existing.source_session or (req.source_session or ""),
        parameters=list(req.parameters),
        required_tools=list(req.required_tools),
        goal=req.goal,
        required_tools_prose=req.required_tools_prose,
        procedure=req.procedure,
        pitfalls=req.pitfalls,
        verification=req.verification,
    )
    path = write_skill(skill)
    return JSONResponse({"ok": True, "slug": slug, "path": str(path), "version": skill.version})


@router.get("/api/skills")
async def skills_list() -> JSONResponse:
    from openclose.skills.storage import list_skills
    return JSONResponse([s.to_summary() for s in list_skills()])


@router.get("/api/skills/{slug}")
async def skills_get(slug: str) -> JSONResponse:
    from openclose.skills.storage import read_skill
    skill = read_skill(slug)
    if skill is None:
        return JSONResponse({"error": "Skill not found"}, status_code=404)
    return JSONResponse(skill.model_dump())


@router.delete("/api/skills/{slug}")
async def skills_delete(slug: str) -> JSONResponse:
    from openclose.skills.storage import delete_skill
    return JSONResponse({"deleted": delete_skill(slug)})


@router.post("/api/skills/{slug}/duplicate")
async def skills_duplicate(slug: str) -> JSONResponse:
    from datetime import datetime, timezone
    from openclose.skills.schema import Skill
    from openclose.skills.storage import (
        list_skills, read_skill, reserve_skill_slug, write_skill,
    )
    src = read_skill(slug)
    if src is None:
        return JSONResponse({"error": "Skill not found"}, status_code=404)
    new_name = _next_duplicate_name(src.name, {s.name for s in list_skills()})
    new_slug = reserve_skill_slug(new_name)
    copy = Skill(
        name=new_name,
        slug=new_slug,
        version=1,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source_session="",
        parameters=list(src.parameters),
        required_tools=list(src.required_tools),
        goal=src.goal,
        required_tools_prose=src.required_tools_prose,
        procedure=src.procedure,
        pitfalls=src.pitfalls,
        verification=src.verification,
    )
    write_skill(copy)
    return JSONResponse({"ok": True, "slug": new_slug, "name": new_name})


@router.post("/api/skills/{slug}/run")
async def skills_run(slug: str, req: SkillsRunRequest) -> JSONResponse:
    from openclose.skills.runner import start_run
    try:
        info = await start_run(
            slug,
            inputs=dict(req.inputs),
            trigger_message=req.trigger_message,
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse(info)


@router.get("/api/skills/{slug}/runs")
async def skills_runs(slug: str, limit: int = 10) -> JSONResponse:
    from openclose.skills.storage import list_runs
    return JSONResponse(list_runs(slug, limit=limit))


# ---------------------------------------------------------------------------
# Jobs — scheduled triggers that chain skills in series
# ---------------------------------------------------------------------------


@router.get("/api/jobs/channels")
async def jobs_channels() -> JSONResponse:
    """Enumerate configured deliver_message channel aliases for the UI dropdown."""
    from openclose.jobs.notify import list_channel_aliases
    return JSONResponse(list_channel_aliases())


@router.post("/api/jobs/cron/parse")
async def jobs_cron_parse(req: CronParseRequest) -> JSONResponse:
    """Translate natural-language / literal cron input into a validated schedule preview."""
    from openclose.jobs.cron_nl import (
        CronTranslateError, translate_cron, next_occurrences,
    )
    try:
        tr = await translate_cron(req.text, req.timezone)
    except CronTranslateError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": str(e)}, status_code=500)
    try:
        upcoming = next_occurrences(tr.cron, req.timezone, count=5)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"Valid cron but next-fire compute failed: {e}"}, status_code=500)
    return JSONResponse({
        "cron": tr.cron,
        "description": tr.description,
        "next_occurrences": upcoming,
        "timezone": req.timezone,
    })


def _new_job_id() -> str:
    from openclose.id import generate_id
    return generate_id()


def _next_duplicate_name(base: str, existing: set[str]) -> str:
    """Return "<base>_duplicated" or "<base>_duplicated_N" to avoid name collision."""
    candidate = f"{base}_duplicated"
    if candidate not in existing:
        return candidate
    i = 2
    while f"{candidate}_{i}" in existing:
        i += 1
    return f"{candidate}_{i}"


@router.post("/api/jobs")
async def jobs_create(req: JobSaveRequest) -> JSONResponse:
    from datetime import datetime, timezone
    from openclose.jobs.schema import JobConfig
    from openclose.jobs.storage import write_job
    job = JobConfig(
        id=_new_job_id(),
        name=req.name,
        skills=list(req.skills),
        skill_parameters=dict(req.skill_parameters),
        timing=req.timing,
        on_failure=req.on_failure,
        notification=req.notification,
        enabled=req.enabled,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        version=1,
    )
    write_job(job)
    from openclose.jobs.scheduler import get_scheduler
    get_scheduler().invalidate(job.id)
    return JSONResponse({"ok": True, "id": job.id})


@router.put("/api/jobs/{job_id}")
async def jobs_update(job_id: str, req: JobSaveRequest) -> JSONResponse:
    from openclose.jobs.storage import read_job, write_job
    from openclose.jobs.scheduler import get_scheduler
    existing = read_job(job_id)
    if existing is None:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    existing.name = req.name
    existing.skills = list(req.skills)
    existing.skill_parameters = dict(req.skill_parameters)
    existing.timing = req.timing
    existing.on_failure = req.on_failure
    existing.notification = req.notification
    existing.enabled = req.enabled
    existing.version += 1
    write_job(existing)
    get_scheduler().invalidate(job_id)
    return JSONResponse({"ok": True, "id": job_id, "version": existing.version})


@router.get("/api/jobs")
async def jobs_list() -> JSONResponse:
    from openclose.jobs.storage import list_jobs
    from openclose.jobs.cron_nl import next_fire_time
    out: list[dict[str, Any]] = []
    for j in list_jobs():
        entry: dict[str, Any] = {
            "id": j.id,
            "name": j.name,
            "skills": list(j.skills),
            "enabled": j.enabled,
            "timing": j.timing.model_dump(),
            "on_failure": j.on_failure,
            "notification": j.notification.model_dump(),
            "next_run": "",
        }
        if j.enabled:
            if j.timing.mode == "one_shot" and not j.timing.executed:
                entry["next_run"] = j.timing.run_at
            elif j.timing.mode == "recurring" and j.timing.cron:
                try:
                    entry["next_run"] = next_fire_time(
                        j.timing.cron, j.timing.timezone,
                    ).isoformat(timespec="seconds")
                except Exception:  # noqa: BLE001
                    entry["next_run"] = ""
        out.append(entry)
    return JSONResponse(out)


@router.get("/api/jobs/{job_id}")
async def jobs_get(job_id: str) -> JSONResponse:
    from openclose.jobs.storage import read_job
    job = read_job(job_id)
    if job is None:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return JSONResponse(job.model_dump())


@router.delete("/api/jobs/{job_id}")
async def jobs_delete(job_id: str) -> JSONResponse:
    from openclose.jobs.storage import delete_job
    from openclose.jobs.scheduler import get_scheduler
    ok = delete_job(job_id)
    get_scheduler().invalidate(job_id)
    return JSONResponse({"deleted": ok})


@router.post("/api/jobs/{job_id}/duplicate")
async def jobs_duplicate(job_id: str) -> JSONResponse:
    from datetime import datetime, timezone
    from openclose.jobs.schema import JobConfig, JobNotification, JobTiming
    from openclose.jobs.storage import list_jobs, read_job, write_job
    from openclose.jobs.scheduler import get_scheduler
    src = read_job(job_id)
    if src is None:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    new_name = _next_duplicate_name(src.name, {j.name for j in list_jobs()})
    timing = JobTiming(
        mode=src.timing.mode,
        cron=src.timing.cron,
        run_at=src.timing.run_at,
        executed=False,  # reset so a one-shot duplicate can still fire
        timezone=src.timing.timezone,
    )
    dup = JobConfig(
        id=_new_job_id(),
        name=new_name,
        skills=list(src.skills),
        skill_parameters={k: dict(v) for k, v in src.skill_parameters.items()},
        timing=timing,
        on_failure=src.on_failure,
        notification=JobNotification(
            channel=src.notification.channel,
            notify_on=src.notification.notify_on,
            include_output=src.notification.include_output,
        ),
        enabled=False,  # duplicates start disabled to avoid surprise auto-fire
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        version=1,
    )
    write_job(dup)
    get_scheduler().invalidate(dup.id)
    return JSONResponse({"ok": True, "id": dup.id, "name": new_name})


@router.post("/api/jobs/{job_id}/enable")
async def jobs_enable(job_id: str, req: JobEnableRequest) -> JSONResponse:
    from openclose.jobs.storage import read_job, write_job
    from openclose.jobs.scheduler import get_scheduler
    job = read_job(job_id)
    if job is None:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    job.enabled = req.enabled
    write_job(job)
    get_scheduler().invalidate(job_id)
    return JSONResponse({"ok": True, "enabled": job.enabled})


@router.post("/api/jobs/{job_id}/run")
async def jobs_run_now(job_id: str) -> JSONResponse:
    from openclose.jobs.scheduler import get_scheduler
    result = await get_scheduler().trigger_now(job_id)
    if not result.get("ok"):
        return JSONResponse(result, status_code=409)
    return JSONResponse(result)


@router.get("/api/jobs/{job_id}/runs")
async def jobs_runs(job_id: str, limit: int = 20) -> JSONResponse:
    from openclose.jobs.storage import list_job_runs
    return JSONResponse(list_job_runs(job_id, limit=limit))


@router.get("/api/jobs/{job_id}/runs/{run_folder}")
async def jobs_run_detail(job_id: str, run_folder: str) -> JSONResponse:
    from openclose.jobs.storage import read_summary
    summary = read_summary(job_id, run_folder)
    if summary is None:
        return JSONResponse({"error": "Run not found"}, status_code=404)
    return JSONResponse(summary.model_dump())
