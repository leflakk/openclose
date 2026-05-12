"""CLI entry point — argparse-based."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import webbrowser

from openclose.config.config import load_config
from openclose.config.paths import ConfigPaths
from openclose.storage.db import get_db
from openclose.log import setup_logging, get_logger

log = get_logger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openclose",
        description="OpenClose — A local-first AI coding assistant",
    )
    sub = parser.add_subparsers(dest="command")

    # serve
    serve = sub.add_parser("serve", help="Start the web UI server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=9876)
    serve.add_argument("--no-browser", action="store_true")
    serve.add_argument("--project-dir", default=".")

    # run (non-interactive)
    run = sub.add_parser("run", help="Run a single prompt (non-interactive)")
    run.add_argument("-p", "--prompt", required=True, help="The prompt to send")
    run.add_argument("--agent", default="build")
    run.add_argument("--json", action="store_true", help="Output as JSON")
    run.add_argument("--project-dir", default=".")

    # sessions
    sub.add_parser("sessions", help="List sessions")

    return parser


def main() -> None:
    """Main CLI entry point."""
    parser = _build_parser()
    args = parser.parse_args()

    setup_logging()
    ConfigPaths.ensure_dirs()

    if args.command == "serve":
        _cmd_serve(args)
    elif args.command == "run":
        asyncio.run(_cmd_run(args))
    elif args.command == "sessions":
        _cmd_sessions()
    else:
        parser.print_help()


def _cmd_serve(args: argparse.Namespace) -> None:
    """Start the web server."""
    import uvicorn
    from openclose.server.app import create_app

    from pathlib import Path
    project = Path(args.project_dir).resolve()
    load_config(project_dir=project)

    app = create_app()
    url = f"http://{args.host}:{args.port}"
    log.info("Starting OpenClose at %s", url)

    if not args.no_browser:
        webbrowser.open(url)

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


async def _cmd_run(args: argparse.Namespace) -> None:
    """Non-interactive mode: send a prompt, print the response."""
    from pathlib import Path

    load_config(project_dir=Path(args.project_dir).resolve())
    db = get_db()

    from openclose.session.session import SessionManager
    from openclose.session.processor import SessionProcessor
    from openclose.tool.registry import ToolRegistry
    from openclose.tool.tools import register_all_tools

    mgr = SessionManager(db)
    session = mgr.create_session(title="CLI Run", agent=args.agent)

    registry = ToolRegistry()
    register_all_tools(registry, args.project_dir)

    processor = SessionProcessor(
        db=db,
        session_id=session.id,
        agent_name=args.agent,
        tool_executor=registry.execute,
        tool_schemas=registry.get_schemas(),
        project_dir=args.project_dir,
    )

    response_text = ""
    async for event in processor.process(args.prompt):
        if event.type == "text":
            response_text += event.content
            if not args.json:
                print(event.content, end="", flush=True)
        elif event.type == "error":
            print(f"\nError: {event.error}", file=sys.stderr)

    if args.json:
        output = {
            "session_id": session.id,
            "response": response_text,
        }
        print(json.dumps(output, indent=2))
    elif response_text:
        print()  # Final newline


def _cmd_sessions() -> None:
    """List all sessions."""
    load_config()
    db = get_db()

    from openclose.session.session import SessionManager

    mgr = SessionManager(db)
    sessions = mgr.list_sessions()
    if not sessions:
        print("No sessions.")
        return
    for s in sessions:
        print(f"  {s.id}  {s.title or '(untitled)'}  [{s.agent}]  {s.updated_at}")
