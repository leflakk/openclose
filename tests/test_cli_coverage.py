"""Tests for CLI coverage."""

from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest


# ── parser ──────────────────────────────────────────────────────────────────

def test_build_parser() -> None:
    from openclose.cli.cli import _build_parser

    parser = _build_parser()
    assert parser.prog == "openclose"

    # Test serve subcommand
    args = parser.parse_args(["serve", "--host", "0.0.0.0", "--port", "8080", "--no-browser"])
    assert args.command == "serve"
    assert args.host == "0.0.0.0"
    assert args.port == 8080
    assert args.no_browser is True

    # Test run subcommand
    args = parser.parse_args(["run", "-p", "hello", "--agent", "code", "--json"])
    assert args.command == "run"
    assert args.prompt == "hello"
    assert args.agent == "code"
    assert args.json is True

    # Test sessions subcommand
    args = parser.parse_args(["sessions"])
    assert args.command == "sessions"


def test_build_parser_defaults() -> None:
    from openclose.cli.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["serve"])
    assert args.host == "127.0.0.1"
    assert args.port == 9876
    assert args.no_browser is False
    assert args.project_dir == "."


# ── main dispatch ───────────────────────────────────────────────────────────

def test_main_no_command() -> None:
    from openclose.cli.cli import main

    with patch("sys.argv", ["openclose"]):
        with patch("openclose.cli.cli._build_parser") as mock_parser:
            ns = MagicMock()
            ns.command = None
            parser_instance = MagicMock()
            parser_instance.parse_args.return_value = ns
            mock_parser.return_value = parser_instance
            main()
            parser_instance.print_help.assert_called_once()


def test_main_serve() -> None:
    from openclose.cli.cli import main

    with patch("sys.argv", ["openclose", "serve"]):
        with patch("openclose.cli.cli._cmd_serve") as mock_serve:
            with patch("openclose.cli.cli.setup_logging"):
                with patch("openclose.cli.cli.ConfigPaths.ensure_dirs"):
                    main()
            mock_serve.assert_called_once()


def test_main_sessions() -> None:
    from openclose.cli.cli import main

    with patch("sys.argv", ["openclose", "sessions"]):
        with patch("openclose.cli.cli._cmd_sessions") as mock_sessions:
            with patch("openclose.cli.cli.setup_logging"):
                with patch("openclose.cli.cli.ConfigPaths.ensure_dirs"):
                    main()
            mock_sessions.assert_called_once()


def test_main_run() -> None:
    from openclose.cli.cli import main
    import asyncio as _asyncio

    # Closing the coroutine in the mock's side_effect silences
    # "coroutine '_cmd_run' was never awaited" without actually running it.
    def _consume(coro: object) -> None:
        if _asyncio.iscoroutine(coro):
            coro.close()

    with patch("sys.argv", ["openclose", "run", "-p", "hello"]):
        with patch("openclose.cli.cli.asyncio.run", side_effect=_consume) as mock_run:
            with patch("openclose.cli.cli.setup_logging"):
                with patch("openclose.cli.cli.ConfigPaths.ensure_dirs"):
                    main()
            mock_run.assert_called_once()


# ── _cmd_serve ──────────────────────────────────────────────────────────────

def test_cmd_serve_no_browser() -> None:
    from openclose.cli.cli import _cmd_serve
    import argparse

    args = argparse.Namespace(
        host="127.0.0.1", port=9876, no_browser=True, project_dir="."
    )
    with patch("openclose.cli.cli.load_config"):
        with patch("uvicorn.run") as mock_run:
            with patch("openclose.server.app.create_app", return_value=MagicMock()):
                with patch("openclose.cli.cli.webbrowser.open") as mock_open:
                    _cmd_serve(args)
                    mock_open.assert_not_called()
                    mock_run.assert_called_once()


def test_cmd_serve_with_browser() -> None:
    from openclose.cli.cli import _cmd_serve
    import argparse

    args = argparse.Namespace(
        host="127.0.0.1", port=9876, no_browser=False, project_dir="."
    )
    with patch("openclose.cli.cli.load_config"):
        with patch("uvicorn.run"):
            with patch("openclose.server.app.create_app", return_value=MagicMock()):
                with patch("openclose.cli.cli.webbrowser.open") as mock_open:
                    _cmd_serve(args)
                    mock_open.assert_called_once_with("http://127.0.0.1:9876")


# ── _cmd_sessions ───────────────────────────────────────────────────────────

def test_cmd_sessions_empty(capsys: pytest.CaptureFixture[str]) -> None:
    from openclose.cli.cli import _cmd_sessions

    with patch("openclose.cli.cli.load_config"):
        with patch("openclose.cli.cli.get_db"):
            mock_mgr = MagicMock()
            mock_mgr.list_sessions.return_value = []
            with patch("openclose.session.session.SessionManager", return_value=mock_mgr):
                _cmd_sessions()
    captured = capsys.readouterr()
    assert "No sessions" in captured.out


def test_cmd_sessions_with_sessions(capsys: pytest.CaptureFixture[str]) -> None:
    from openclose.cli.cli import _cmd_sessions

    mock_session = MagicMock()
    mock_session.id = "abc123"
    mock_session.title = "Test Session"
    mock_session.agent = "build"
    mock_session.updated_at = "2024-01-01"

    with patch("openclose.cli.cli.load_config"):
        with patch("openclose.cli.cli.get_db"):
            with patch("openclose.session.session.SessionManager") as MockMgr:
                MockMgr.return_value.list_sessions.return_value = [mock_session]
                _cmd_sessions()
    captured = capsys.readouterr()
    assert "abc123" in captured.out
    assert "Test Session" in captured.out


# ── _cmd_run ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cmd_run_text_output(capsys: pytest.CaptureFixture[str]) -> None:
    from openclose.cli.cli import _cmd_run
    from openclose.agent.loop import StreamEvent
    import argparse

    args = argparse.Namespace(prompt="hello", agent="build", json=False, project_dir=".")

    async def mock_process(msg: str):  # type: ignore[no-untyped-def]
        yield StreamEvent("text", content="Hello back!")
        yield StreamEvent("done")

    with patch("openclose.cli.cli.load_config"):
        with patch("openclose.cli.cli.get_db"):
            with patch("openclose.session.session.SessionManager") as MockMgr:
                mock_session = MagicMock()
                mock_session.id = "s1"
                MockMgr.return_value.create_session.return_value = mock_session
                with patch("openclose.tool.registry.ToolRegistry"):
                    with patch("openclose.tool.tools.register_all_tools"):
                        with patch("openclose.session.processor.SessionProcessor") as MockProc:
                            MockProc.return_value.process = mock_process
                            await _cmd_run(args)

    captured = capsys.readouterr()
    assert "Hello back!" in captured.out


@pytest.mark.asyncio
async def test_cmd_run_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    from openclose.cli.cli import _cmd_run
    from openclose.agent.loop import StreamEvent
    import argparse
    import json

    args = argparse.Namespace(prompt="hello", agent="build", json=True, project_dir=".")

    async def mock_process(msg: str):  # type: ignore[no-untyped-def]
        yield StreamEvent("text", content="Response text")
        yield StreamEvent("done")

    with patch("openclose.cli.cli.load_config"):
        with patch("openclose.cli.cli.get_db"):
            with patch("openclose.session.session.SessionManager") as MockMgr:
                mock_session = MagicMock()
                mock_session.id = "s1"
                MockMgr.return_value.create_session.return_value = mock_session
                with patch("openclose.tool.registry.ToolRegistry"):
                    with patch("openclose.tool.tools.register_all_tools"):
                        with patch("openclose.session.processor.SessionProcessor") as MockProc:
                            MockProc.return_value.process = mock_process
                            await _cmd_run(args)

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["session_id"] == "s1"
    assert output["response"] == "Response text"


@pytest.mark.asyncio
async def test_cmd_run_error_output(capsys: pytest.CaptureFixture[str]) -> None:
    from openclose.cli.cli import _cmd_run
    from openclose.agent.loop import StreamEvent
    import argparse

    args = argparse.Namespace(prompt="hello", agent="build", json=False, project_dir=".")

    async def mock_process(msg: str):  # type: ignore[no-untyped-def]
        yield StreamEvent("error", error="Something went wrong")

    with patch("openclose.cli.cli.load_config"):
        with patch("openclose.cli.cli.get_db"):
            with patch("openclose.session.session.SessionManager") as MockMgr:
                mock_session = MagicMock()
                mock_session.id = "s1"
                MockMgr.return_value.create_session.return_value = mock_session
                with patch("openclose.tool.registry.ToolRegistry"):
                    with patch("openclose.tool.tools.register_all_tools"):
                        with patch("openclose.session.processor.SessionProcessor") as MockProc:
                            MockProc.return_value.process = mock_process
                            await _cmd_run(args)

    captured = capsys.readouterr()
    assert "Something went wrong" in captured.err
