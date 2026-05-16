"""Tests for browser_automation helper functions — pure logic, no browser needed."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest


# ── smart_resize helpers ─────────────────────────────────────────────────────

def test_round_by_factor() -> None:
    from openclose.tool.tools.browser_automation import _round_by_factor

    assert _round_by_factor(30, 28) == 28
    assert _round_by_factor(42, 28) == 56  # 42/28=1.5 -> banker's rounds to 2 -> 2*28=56
    assert _round_by_factor(56, 28) == 56
    assert _round_by_factor(0, 28) == 0


def test_ceil_by_factor() -> None:
    from openclose.tool.tools.browser_automation import _ceil_by_factor

    assert _ceil_by_factor(29, 28) == 56
    assert _ceil_by_factor(28, 28) == 28
    assert _ceil_by_factor(1, 28) == 28


def test_floor_by_factor() -> None:
    from openclose.tool.tools.browser_automation import _floor_by_factor

    assert _floor_by_factor(55, 28) == 28
    assert _floor_by_factor(56, 28) == 56
    assert _floor_by_factor(57, 28) == 56


def test_smart_resize_normal() -> None:
    from openclose.tool.tools.browser_automation import _smart_resize

    h, w = _smart_resize(900, 1440)
    assert h % 28 == 0
    assert w % 28 == 0


def test_smart_resize_extreme_aspect_ratio() -> None:
    from openclose.tool.tools.browser_automation import _smart_resize

    with pytest.raises(ValueError, match="aspect ratio"):
        _smart_resize(1, 300)


def test_smart_resize_too_large() -> None:
    from openclose.tool.tools.browser_automation import _smart_resize

    h, w = _smart_resize(5000, 5000, max_pixels=1000000)
    assert h * w <= 1000000


def test_smart_resize_too_small() -> None:
    from openclose.tool.tools.browser_automation import _smart_resize

    h, w = _smart_resize(28, 28, min_pixels=3136)
    assert h * w >= 3136


# ── _scale_coords ────────────────────────────────────────────────────────────

def test_scale_coords() -> None:
    from openclose.tool.tools.browser_automation import _scale_coords

    result = _scale_coords([100.0, 200.0], 1440, 900, 1440, 900)
    assert result == [100.0, 200.0]


def test_scale_coords_different_sizes() -> None:
    from openclose.tool.tools.browser_automation import _scale_coords

    result = _scale_coords([720.0, 450.0], 720, 450, 1440, 900)
    assert abs(result[0] - 1440.0) < 0.01
    assert abs(result[1] - 900.0) < 0.01


# ── _build_tool_schema ───────────────────────────────────────────────────────

def test_build_tool_schema() -> None:
    from openclose.tool.tools.browser_automation import _build_tool_schema

    schema_str = _build_tool_schema(1440, 900)
    schema = json.loads(schema_str)
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "browser_automation"
    assert "1440x900" in schema["function"]["description"]
    params = schema["function"]["parameters"]["properties"]
    assert "action" in params
    assert "coordinate" in params
    # visit_url / web_search are now tool intents, not planner actions —
    # the grounding tool schema must not advertise `url` or `query`, nor
    # list those actions in the enum.
    assert "url" not in params
    assert "query" not in params
    action_enum = params["action"]["enum"]
    assert "visit_url" not in action_enum
    assert "web_search" not in action_enum
    assert "history_back" in action_enum


# ── _build_system_prompt ─────────────────────────────────────────────────────

def test_build_system_prompt() -> None:
    from openclose.tool.tools.browser_automation import _build_grounding_system_prompt as _build_system_prompt

    prompt = _build_system_prompt(1440, 900)
    assert "browser_automation" in prompt
    assert "1440x900" in prompt
    assert "tool_call" in prompt


# ── _parse_model_response ─────────────────────────────────────────────────────

def test_parse_model_response_with_tool_call() -> None:
    from openclose.tool.tools.browser_automation_shared import parse_model_response as _parse_model_response

    text = 'I see a button\n<tool_call>\n{"name": "browser_automation", "arguments": {"action": "left_click", "coordinate": [100, 200]}}\n</tool_call>'
    thinking, args = _parse_model_response(text)
    assert thinking == "I see a button"
    assert args is not None
    assert args["action"] == "left_click"
    assert args["coordinate"] == [100, 200]


def test_parse_model_response_no_tool_call() -> None:
    from openclose.tool.tools.browser_automation_shared import parse_model_response as _parse_model_response

    text = "I am thinking about this"
    thinking, args = _parse_model_response(text)
    assert thinking == "I am thinking about this"
    assert args is None


def test_parse_model_response_invalid_json() -> None:
    from openclose.tool.tools.browser_automation_shared import parse_model_response as _parse_model_response

    text = "Thinking\n<tool_call>\n{not valid json\n</tool_call>"
    thinking, args = _parse_model_response(text)
    assert thinking == "Thinking"
    assert args is None


def test_parse_model_response_direct_args() -> None:
    from openclose.tool.tools.browser_automation_shared import parse_model_response as _parse_model_response

    text = '<tool_call>\n{"action": "left_click", "coordinate": [50, 60]}\n</tool_call>'
    thinking, args = _parse_model_response(text)
    assert args is not None
    assert args["action"] == "left_click"


def test_parse_model_response_no_newline_in_tag() -> None:
    from openclose.tool.tools.browser_automation_shared import parse_model_response as _parse_model_response

    # Without \n before </tool_call>, the first split grabs the closing tag too
    # so JSON parsing fails — this is the expected behavior
    text = 'Think<tool_call>{"name": "browser_automation", "arguments": {"action": "key", "keys": ["Enter"]}}</tool_call>'
    thinking, args = _parse_model_response(text)
    assert thinking == "Think"
    assert args is None  # JSON parse fails because </tool_call> is appended


def test_parse_model_response_bad_structure() -> None:
    from openclose.tool.tools.browser_automation_shared import parse_model_response as _parse_model_response

    text = '<tool_call>\n{"not_name": 123}\n</tool_call>'
    thinking, args = _parse_model_response(text)
    assert args is None


# ── _summarise_recent_actions ────────────────────────────────────────────────

def test_summarise_recent_actions_empty() -> None:
    from openclose.tool.tools.browser_automation_shared import summarise_recent_actions as _summarise_recent_actions

    result = _summarise_recent_actions([])
    assert result == "(no actions yet)"


def test_summarise_recent_actions_with_steps() -> None:
    from openclose.tool.tools.browser_automation_shared import summarise_recent_actions as _summarise_recent_actions

    steps: list[dict[str, Any]] = [
        {
            "type": "tool_call", "subagent_label": "Planner",
            "tool_call_id": "tc1",
            "content": json.dumps({"action": "history_back"}),
        },
        {
            "type": "tool_result", "subagent_label": "Planner",
            "tool_call_id": "tc1",
            "content": "history_back()",
        },
    ]
    result = _summarise_recent_actions(steps)
    assert "history_back" in result


def test_summarise_recent_actions_various_types() -> None:
    from openclose.tool.tools.browser_automation_shared import summarise_recent_actions as _summarise_recent_actions

    steps: list[dict[str, Any]] = [
        {"type": "tool_call", "subagent_label": "Planner", "tool_call_id": "tc1",
         "content": json.dumps({"action": "history_back"})},
        {"type": "tool_result", "subagent_label": "Planner", "tool_call_id": "tc1",
         "content": "history_back()"},
        {"type": "tool_call", "subagent_label": "Planner", "tool_call_id": "tc2",
         "content": json.dumps({"action": "type", "text": "hello world"})},
        {"type": "tool_result", "subagent_label": "Planner", "tool_call_id": "tc2",
         "content": "type('hello world')"},
        {"type": "tool_call", "subagent_label": "Planner", "tool_call_id": "tc3",
         "content": json.dumps({"action": "key", "keys": ["Enter"]})},
        {"type": "tool_result", "subagent_label": "Planner", "tool_call_id": "tc3",
         "content": "key(['Enter'])"},
        {"type": "tool_call", "subagent_label": "Planner", "tool_call_id": "tc4",
         "content": json.dumps({"action": "scroll", "pixels": 300})},
        {"type": "tool_result", "subagent_label": "Planner", "tool_call_id": "tc4",
         "content": "scroll(300)"},
        {"type": "tool_call", "subagent_label": "Planner", "tool_call_id": "tc5",
         "content": json.dumps({"action": "pause_and_memorize_fact", "fact": "Important fact"})},
        {"type": "tool_result", "subagent_label": "Planner", "tool_call_id": "tc5",
         "content": "memorized"},
        {"type": "tool_call", "subagent_label": "Planner", "tool_call_id": "tc6",
         "content": json.dumps({"action": "left_click", "target": "button"})},
        {"type": "tool_result", "subagent_label": "Planner", "tool_call_id": "tc6",
         "content": "left_click(100, 200)"},
    ]
    result = _summarise_recent_actions(steps)
    assert "history_back" in result
    assert "text=" in result
    assert "keys=" in result
    assert "pixels=" in result
    assert "fact=" in result
    assert "target=" in result


def test_summarise_recent_actions_filters_non_planner() -> None:
    from openclose.tool.tools.browser_automation_shared import summarise_recent_actions as _summarise_recent_actions

    steps: list[dict[str, Any]] = [
        {"type": "tool_call", "subagent_label": "Grounding", "tool_call_id": "tc1",
         "content": json.dumps({"action": "click"})},
        {"type": "tool_result", "subagent_label": "Grounding", "tool_call_id": "tc1",
         "content": "clicked"},
    ]
    result = _summarise_recent_actions(steps)
    assert result == "(no actions yet)"


def test_summarise_recent_actions_invalid_json() -> None:
    from openclose.tool.tools.browser_automation_shared import summarise_recent_actions as _summarise_recent_actions

    steps: list[dict[str, Any]] = [
        {"type": "tool_call", "subagent_label": "Planner", "tool_call_id": "tc1",
         "content": "not json"},
        {"type": "tool_result", "subagent_label": "Planner", "tool_call_id": "tc1",
         "content": "result"},
    ]
    result = _summarise_recent_actions(steps)
    assert "?" in result


def test_summarise_recent_actions_no_desc() -> None:
    from openclose.tool.tools.browser_automation_shared import summarise_recent_actions as _summarise_recent_actions

    steps: list[dict[str, Any]] = [
        {"type": "tool_call", "subagent_label": "Planner", "tool_call_id": "tc1",
         "content": json.dumps({"action": "history_back"})},
        {"type": "tool_result", "subagent_label": "Planner", "tool_call_id": "tc1",
         "content": "back"},
    ]
    result = _summarise_recent_actions(steps)
    assert "history_back" in result


def test_summarise_truncates_long_result() -> None:
    from openclose.tool.tools.browser_automation_shared import summarise_recent_actions as _summarise_recent_actions

    steps: list[dict[str, Any]] = [
        {"type": "tool_call", "subagent_label": "Planner", "tool_call_id": "tc1",
         "content": json.dumps({"action": "visit_url", "url": "https://example.com"})},
        {"type": "tool_result", "subagent_label": "Planner", "tool_call_id": "tc1",
         "content": "x" * 200},
    ]
    result = _summarise_recent_actions(steps)
    assert "…" in result


def test_summarise_truncates_long_text() -> None:
    from openclose.tool.tools.browser_automation_shared import summarise_recent_actions as _summarise_recent_actions

    steps: list[dict[str, Any]] = [
        {"type": "tool_call", "subagent_label": "Planner", "tool_call_id": "tc1",
         "content": json.dumps({"action": "type", "text": "a" * 60})},
        {"type": "tool_result", "subagent_label": "Planner", "tool_call_id": "tc1",
         "content": "typed"},
    ]
    result = _summarise_recent_actions(steps)
    assert "…" in result


def test_summarise_truncates_long_fact() -> None:
    from openclose.tool.tools.browser_automation_shared import summarise_recent_actions as _summarise_recent_actions

    steps: list[dict[str, Any]] = [
        {"type": "tool_call", "subagent_label": "Planner", "tool_call_id": "tc1",
         "content": json.dumps({"action": "pause_and_memorize_fact", "fact": "f" * 80})},
        {"type": "tool_result", "subagent_label": "Planner", "tool_call_id": "tc1",
         "content": "memorized"},
    ]
    result = _summarise_recent_actions(steps)
    assert "…" in result


# ── _build_planner_user_turn ───────────────────────────────────────────────────

def test_build_planner_user_turn_step_zero_rich() -> None:
    from openclose.tool.tools.browser_automation import _build_planner_user_turn

    msg = _build_planner_user_turn(
        step=0, task="Find the button", memory=[],
        steps_log=[], current_url="https://example.com",
        snapshot_text='[0] button "Click me"',
        img_b64="abc123", mode="rich",
    )
    assert msg["role"] == "user"
    assert len(msg["content"]) == 2
    text_block = msg["content"][0]
    assert "Task: Find the button" in text_block["text"]
    img_block = msg["content"][1]
    assert img_block["type"] == "image_url"
    assert "abc123" in img_block["image_url"]["url"]


def test_build_planner_user_turn_later_step_rich() -> None:
    from openclose.tool.tools.browser_automation import _build_planner_user_turn

    msg = _build_planner_user_turn(
        step=3, task="Find the button", memory=["fact1", "fact2"],
        steps_log=[], current_url="https://example.com",
        snapshot_text='[0] button "Click me"',
        img_b64="abc", mode="rich",
    )
    text = msg["content"][0]["text"]
    assert "Continue the task" in text
    assert "Remembered facts:" in text
    assert "- fact1" in text


def test_build_planner_user_turn_long_url() -> None:
    from openclose.tool.tools.browser_automation import _build_planner_user_turn

    long_url = "https://example.com/" + "a" * 200
    msg = _build_planner_user_turn(
        step=0, task="task", memory=[], steps_log=[],
        current_url=long_url, snapshot_text="[0] link",
        img_b64="img", mode="rich",
    )
    text = msg["content"][0]["text"]
    assert "…" in text


# ── _grounding_instruction_text ───────────────────────────────────────────────────

def test_grounding_instruction_text() -> None:
    from openclose.tool.tools.browser_automation import _grounding_instruction_text

    result = _grounding_instruction_text("Submit button")
    assert "left_click" in result
    assert "Submit button" in result


def test_grounding_instruction_text_with_extras() -> None:
    from openclose.tool.tools.browser_automation import _grounding_instruction_text

    result = _grounding_instruction_text("search box")
    assert "left_click" in result
    assert "search box" in result


# ── _execute_action (mocked page) ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_action_left_click() -> None:
    from unittest.mock import AsyncMock, MagicMock
    from openclose.tool.tools.browser_automation_shared import execute_action as _execute_action

    page = MagicMock()
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()
    page.mouse.click = AsyncMock()
    result = await _execute_action(page, {"action": "left_click", "coordinate": [100, 200]})
    assert "left_click" in result
    page.mouse.move.assert_called_once()
    page.mouse.click.assert_called_once()


@pytest.mark.asyncio
async def test_execute_action_type() -> None:
    from unittest.mock import AsyncMock, MagicMock
    from openclose.tool.tools.browser_automation_shared import execute_action as _execute_action

    page = MagicMock()
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()
    page.mouse.click = AsyncMock()
    page.keyboard = MagicMock()
    page.keyboard.type = AsyncMock()
    page.keyboard.press = AsyncMock()
    result = await _execute_action(page, {
        "action": "type", "text": "hello",
        "coordinate": [50, 60], "press_enter": True, "delete_existing_text": True,
    })
    assert "type" in result
    assert page.keyboard.press.call_count >= 2  # ControlOrMeta+A, Backspace, Enter


@pytest.mark.asyncio
async def test_execute_action_key() -> None:
    from unittest.mock import AsyncMock, MagicMock
    from openclose.tool.tools.browser_automation_shared import execute_action as _execute_action

    page = MagicMock()
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock()
    result = await _execute_action(page, {"action": "key", "keys": ["Return", "BackSpace"]})
    assert "key" in result
    # Return -> Enter, BackSpace -> Backspace via _KEY_MAP
    page.keyboard.press.assert_any_call("Enter")
    page.keyboard.press.assert_any_call("Backspace")


@pytest.mark.asyncio
async def test_execute_action_mouse_move() -> None:
    from unittest.mock import AsyncMock, MagicMock
    from openclose.tool.tools.browser_automation_shared import execute_action as _execute_action

    page = MagicMock()
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()
    result = await _execute_action(page, {"action": "mouse_move", "coordinate": [300, 400]})
    assert "mouse_move" in result


@pytest.mark.asyncio
async def test_execute_action_scroll() -> None:
    from unittest.mock import AsyncMock, MagicMock
    from openclose.tool.tools.browser_automation_shared import execute_action as _execute_action

    page = MagicMock()
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()
    page.mouse.wheel = AsyncMock()
    result = await _execute_action(page, {
        "action": "scroll", "coordinate": [100, 100], "pixels": 300,
    })
    assert "scroll" in result
    page.mouse.wheel.assert_called_once_with(0, -300)


@pytest.mark.asyncio
async def test_execute_action_visit_url_no_longer_dispatched() -> None:
    """visit_url is now a tool intent handled by run_goto_intent, not a
    planner action. execute_action should return the unknown_action
    sentinel rather than silently navigating."""
    from unittest.mock import AsyncMock, MagicMock
    from openclose.tool.tools.browser_automation_shared import execute_action as _execute_action

    page = MagicMock()
    page.goto = AsyncMock()
    result = await _execute_action(page, {"action": "visit_url", "url": "https://test.com"})
    assert "unknown_action" in result
    page.goto.assert_not_called()


@pytest.mark.asyncio
async def test_execute_action_web_search_no_longer_dispatched() -> None:
    """web_search is now a tool intent handled by run_web_search_intent,
    not a planner action."""
    from unittest.mock import AsyncMock, MagicMock
    from openclose.tool.tools.browser_automation_shared import execute_action as _execute_action

    page = MagicMock()
    page.goto = AsyncMock()
    result = await _execute_action(page, {"action": "web_search", "query": "test query"})
    assert "unknown_action" in result
    page.goto.assert_not_called()


@pytest.mark.asyncio
async def test_run_web_search_intent_builds_bing_url() -> None:
    """run_web_search_intent must URL-encode the query and route through
    Bing — the same pipeline as run_goto_intent, but the URL is
    constructed from `query`."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from openclose.tool.tools.browser_automation_shared import (
        run_web_search_intent as _run_web_search_intent,
        EventContext,
    )

    with patch(
        "openclose.tool.tools.browser_automation_shared.run_goto_intent",
        new_callable=AsyncMock,
    ) as mock_goto:
        mock_goto.return_value = (MagicMock(), MagicMock())
        ctx = EventContext(sink=None, parent_tc_id="")
        await _run_web_search_intent(
            MagicMock(), MagicMock(), "test query",
            ctx=ctx, project_dir=".",
        )
    assert mock_goto.await_count == 1
    forwarded_url = mock_goto.await_args.args[2]
    assert "bing.com/search" in forwarded_url
    assert "test+query" in forwarded_url


@pytest.mark.asyncio
async def test_execute_action_history_back() -> None:
    from unittest.mock import AsyncMock, MagicMock
    from openclose.tool.tools.browser_automation_shared import execute_action as _execute_action

    page = MagicMock()
    page.go_back = AsyncMock()
    result = await _execute_action(page, {"action": "history_back"})
    assert "history_back" in result


@pytest.mark.asyncio
async def test_execute_action_wait() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch
    from openclose.tool.tools.browser_automation_shared import execute_action as _execute_action

    page = MagicMock()
    with patch(
        "openclose.tool.tools.browser_automation_shared.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        result = await _execute_action(page, {"action": "wait", "time": 2})
    assert "wait" in result


@pytest.mark.asyncio
async def test_execute_action_unknown() -> None:
    from unittest.mock import MagicMock
    from openclose.tool.tools.browser_automation_shared import execute_action as _execute_action

    page = MagicMock()
    result = await _execute_action(page, {"action": "fly"})
    assert "unknown_action" in result


# ── _resolve_element_index ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_element_index_success() -> None:
    from openclose.tool.tools.browser_automation_shared import resolve_element_index as _resolve_element_index

    element_map = {
        0: {"center_x": 100.5, "center_y": 200.3, "tag": "button", "role": "button", "text": "Submit"},
        1: {"center_x": 300.0, "center_y": 400.0, "tag": "a", "role": "link", "text": "Home"},
    }
    result, status = await _resolve_element_index(
        {"action": "left_click", "element_index": 0}, element_map,
    )
    assert result is not None
    assert result["action"] == "left_click"
    assert result["coordinate"] == [100.5, 200.3]
    assert "element_index" not in result
    assert "resolved" in status
    assert "button" in status


@pytest.mark.asyncio
async def test_resolve_element_index_missing() -> None:
    from openclose.tool.tools.browser_automation_shared import resolve_element_index as _resolve_element_index

    element_map = {0: {"center_x": 50, "center_y": 60, "tag": "a", "role": "link", "text": "Link"}}
    result, status = await _resolve_element_index(
        {"action": "left_click", "element_index": 5}, element_map,
    )
    assert result is None
    assert "5" in status
    assert "not in current snapshot" in status


@pytest.mark.asyncio
async def test_resolve_element_index_no_index() -> None:
    from openclose.tool.tools.browser_automation_shared import resolve_element_index as _resolve_element_index

    element_map = {0: {"center_x": 50, "center_y": 60, "tag": "a", "role": "link", "text": "Link"}}
    result, status = await _resolve_element_index(
        {"action": "left_click"}, element_map,
    )
    assert result is None
    assert "no valid element_index" in status


@pytest.mark.asyncio
async def test_resolve_element_index_preserves_extras() -> None:
    from openclose.tool.tools.browser_automation_shared import resolve_element_index as _resolve_element_index

    element_map = {
        2: {"center_x": 150, "center_y": 250, "tag": "input", "role": "textbox", "text": "Search"},
    }
    result, status = await _resolve_element_index(
        {"action": "type", "element_index": 2, "text": "hello", "press_enter": True},
        element_map,
    )
    assert result is not None
    assert result["text"] == "hello"
    assert result["press_enter"] is True
    assert result["coordinate"] == [150, 250]


@pytest.mark.asyncio
async def test_resolve_element_index_empty_map() -> None:
    from openclose.tool.tools.browser_automation_shared import resolve_element_index as _resolve_element_index

    result, status = await _resolve_element_index(
        {"action": "left_click", "element_index": 0}, {},
    )
    assert result is None


@pytest.mark.asyncio
async def test_resolve_element_index_jit_fresh_coords() -> None:
    """JIT re-resolve uses fresh coordinates from DOM.getBoxModel."""
    from unittest.mock import AsyncMock, MagicMock
    from openclose.tool.tools.browser_automation_shared import resolve_element_index as _resolve_element_index

    element_map = {
        0: {
            "center_x": 100, "center_y": 200, "tag": "", "role": "button",
            "text": "Submit", "backend_node_id": 42, "frame_id": "main",
        },
    }
    # Mock CDP session returning fresh coordinates.
    mock_session = AsyncMock()
    mock_session.send.return_value = {
        "model": {
            "content": [
                150, 250,   # top-left
                250, 250,   # top-right
                250, 350,   # bottom-right
                150, 350,   # bottom-left
            ],
            "width": 100,
            "height": 100,
        }
    }
    mock_context = MagicMock()
    mock_context.new_cdp_session = AsyncMock(return_value=mock_session)
    mock_page = MagicMock()
    mock_page.frames = []

    result, status = await _resolve_element_index(
        {"action": "left_click", "element_index": 0},
        element_map,
        page=mock_page,
        context=mock_context,
    )
    assert result is not None
    # Fresh center = average of quad: (150+250+250+150)/4=200, (250+250+350+350)/4=300
    assert result["coordinate"] == [200.0, 300.0]
    assert "JIT" in status


@pytest.mark.asyncio
async def test_resolve_element_index_jit_fallback_on_error() -> None:
    """JIT re-resolve falls back to snapshot coords on CDP error."""
    from unittest.mock import AsyncMock, MagicMock
    from openclose.tool.tools.browser_automation_shared import resolve_element_index as _resolve_element_index

    element_map = {
        0: {
            "center_x": 100, "center_y": 200, "tag": "", "role": "button",
            "text": "Submit", "backend_node_id": 42, "frame_id": "main",
        },
    }
    mock_session = AsyncMock()
    mock_session.send.side_effect = Exception("Node not found")
    mock_context = MagicMock()
    mock_context.new_cdp_session = AsyncMock(return_value=mock_session)
    mock_page = MagicMock()
    mock_page.frames = []

    result, status = await _resolve_element_index(
        {"action": "left_click", "element_index": 0},
        element_map,
        page=mock_page,
        context=mock_context,
    )
    assert result is not None
    # Falls back to snapshot-time coordinates.
    assert result["coordinate"] == [100, 200]
    assert "JIT" not in status


# ── _element_resolution_failure_message ─────────────────────────────────────


def test_element_resolution_failure_message() -> None:
    from openclose.tool.tools.browser_automation_shared import element_resolution_failure_message as _element_resolution_failure_message

    msg = _element_resolution_failure_message(
        {"action": "left_click", "element_index": 7},
        "element_index 7 not in current snapshot",
    )
    assert "7" in msg
    assert "left_click" in msg
    assert "snapshot" in msg


# ── _build_planner_user_turn (dom mode) ─────────────────────────────────────


def test_build_planner_user_turn_dom_step_zero() -> None:
    from openclose.tool.tools.browser_automation import _build_planner_user_turn

    msg = _build_planner_user_turn(
        step=0, task="Find the button", memory=[],
        steps_log=[], current_url="https://example.com",
        snapshot_text="[0] button \"Click me\"",
        img_b64=None, mode="dom",
    )
    assert msg["role"] == "user"
    # DOM mode: content is a string, not a list
    assert isinstance(msg["content"], str)
    assert "Task: Find the button" in msg["content"]
    assert "[0] button" in msg["content"]
    assert "image" not in msg["content"].lower()


def test_build_planner_user_turn_dom_later_step() -> None:
    from openclose.tool.tools.browser_automation import _build_planner_user_turn

    msg = _build_planner_user_turn(
        step=2, task="Find the button", memory=["fact1"],
        steps_log=[], current_url="https://example.com",
        snapshot_text="[0] link \"Home\"",
        img_b64=None, mode="dom",
    )
    text = msg["content"]
    assert "Continue the task" in text
    assert "Remembered facts:" in text
    assert "- fact1" in text
    assert "[0] link" in text


# ── _strip_element_indices ─────────────────────────────────────────────────


def test_strip_element_indices_removes_prefixes() -> None:
    from openclose.tool.tools.browser_automation_shared import strip_element_indices as _strip_element_indices

    text = "[0] button \"OK\"\n[12] link \"Home\"\n[999] textbox \"Search\""
    result = _strip_element_indices(text)
    assert "button \"OK\"" in result
    assert "link \"Home\"" in result
    assert "textbox \"Search\"" in result
    assert "[0]" not in result
    assert "[12]" not in result
    assert "[999]" not in result


def test_strip_element_indices_preserves_non_index_brackets() -> None:
    from openclose.tool.tools.browser_automation_shared import strip_element_indices as _strip_element_indices

    text = "some [aria-label] text\n[not-a-number] stays"
    result = _strip_element_indices(text)
    assert "[aria-label]" in result
    assert "[not-a-number]" in result


def test_strip_element_indices_no_indices() -> None:
    from openclose.tool.tools.browser_automation_shared import strip_element_indices as _strip_element_indices

    text = "button \"OK\"\nlink \"Home\""
    assert _strip_element_indices(text) == text


# ── _compute_snapshot_diff ─────────────────────────────────────────────────


def test_compute_snapshot_diff_empty_prev() -> None:
    from openclose.tool.tools.browser_automation_shared import compute_snapshot_diff as _compute_snapshot_diff

    assert _compute_snapshot_diff("", "any snapshot content") == ""


def test_compute_snapshot_diff_no_changes() -> None:
    from openclose.tool.tools.browser_automation_shared import compute_snapshot_diff as _compute_snapshot_diff

    prev = "[0] button \"Submit\"\n[1] link \"Home\""
    curr = "[5] button \"Submit\"\n[8] link \"Home\""
    assert _compute_snapshot_diff(prev, curr) == "No changes detected."


def test_compute_snapshot_diff_index_renumbering_only() -> None:
    from openclose.tool.tools.browser_automation_shared import compute_snapshot_diff as _compute_snapshot_diff

    prev = "[0] button \"A\"\n[1] button \"B\"\n[2] button \"C\""
    curr = "[10] button \"A\"\n[20] button \"B\"\n[30] button \"C\""
    assert _compute_snapshot_diff(prev, curr) == "No changes detected."


def test_compute_snapshot_diff_added_lines() -> None:
    from openclose.tool.tools.browser_automation_shared import compute_snapshot_diff as _compute_snapshot_diff

    prev = "[0] button \"Submit\"\n[1] link \"Home\""
    curr = "[0] button \"Submit\"\n[1] link \"Home\"\n[2] textbox \"Search\""
    result = _compute_snapshot_diff(prev, curr)
    assert "New:" in result
    assert "+ textbox \"Search\"" in result
    assert "Gone:" not in result


def test_compute_snapshot_diff_removed_lines() -> None:
    from openclose.tool.tools.browser_automation_shared import compute_snapshot_diff as _compute_snapshot_diff

    prev = "[0] button \"Submit\"\n[1] link \"Home\"\n[2] textbox \"Search\""
    curr = "[0] button \"Submit\"\n[1] link \"Home\""
    result = _compute_snapshot_diff(prev, curr)
    assert "Gone:" in result
    assert "- textbox \"Search\"" in result


def test_compute_snapshot_diff_new_page() -> None:
    from openclose.tool.tools.browser_automation_shared import compute_snapshot_diff as _compute_snapshot_diff

    # Build two snapshots sharing < 50% of lines → high churn
    prev_lines = [f"[{i}] element_prev_{i}" for i in range(20)]
    curr_lines = [f"[{i}] element_curr_{i}" for i in range(20)]
    prev = "\n".join(prev_lines)
    curr = "\n".join(curr_lines)
    result = _compute_snapshot_diff(prev, curr)
    assert result == "Page changed significantly (likely a new page)."


# ── _build_planner_user_turn (no diff) ─────────────────────────────────────


def test_build_planner_user_turn_dom_without_diff() -> None:
    from openclose.tool.tools.browser_automation import _build_planner_user_turn

    msg = _build_planner_user_turn(
        step=1, task="Click button", memory=[],
        steps_log=[], current_url="https://example.com",
        snapshot_text="[0] button \"OK\"",
        img_b64=None, mode="dom",
    )
    assert "Changes since last step" not in msg["content"]


# ── unified planner system prompt ───────────────────────────────────────────


def test_dom_mode_prompt_no_screenshot_references() -> None:
    from openclose.tool.tools.browser_automation import (
        UNIFIED_PLANNER_SYSTEM_PROMPT_DOM,
    )

    lower = UNIFIED_PLANNER_SYSTEM_PROMPT_DOM.lower()
    # Should not instruct the model to look at screenshots/images
    assert "image_url" not in lower
    assert "the screenshot" not in lower
    assert "current screenshot" not in lower
    # Should reference text-mode concepts
    assert "element_index" in lower
    assert "accessibility snapshot" in lower


def test_rich_mode_prompt_mentions_screenshot_and_target() -> None:
    from openclose.tool.tools.browser_automation import (
        UNIFIED_PLANNER_SYSTEM_PROMPT_RICH,
    )

    lower = UNIFIED_PLANNER_SYSTEM_PROMPT_RICH.lower()
    assert "screenshot" in lower
    # Rich mode allows both element_index and target
    assert "element_index" in lower
    assert "target" in lower
    # Includes the grounding-failure recovery guidance
    assert "tab cycling" in lower or "tab" in lower


# ── _summarise_recent_actions with element_index ────────────────────────────


def test_summarise_recent_actions_element_index() -> None:
    from openclose.tool.tools.browser_automation_shared import summarise_recent_actions as _summarise_recent_actions

    steps: list[dict[str, Any]] = [
        {
            "type": "tool_call", "subagent_label": "Planner",
            "tool_call_id": "tc1",
            "content": json.dumps({"action": "left_click", "element_index": 3}),
        },
        {
            "type": "tool_result", "subagent_label": "Planner",
            "tool_call_id": "tc1",
            "content": "left_click(100, 200)",
        },
    ]
    result = _summarise_recent_actions(steps)
    assert "element_index=3" in result
    # Should NOT fall through to target
    assert "target=" not in result


# ── _snapshot_a11y ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_snapshot_a11y_returns_correct_shape() -> None:
    """_snapshot_a11y produces the expected element_map and snapshot_text."""
    from unittest.mock import AsyncMock, MagicMock, PropertyMock
    from openclose.tool.tools.browser_automation_shared import snapshot_a11y as _snapshot_a11y

    # ── Mock CDP responses ──
    ax_tree = {
        "nodes": [
            {
                "nodeId": "1",
                "role": {"type": "role", "value": "WebArea"},
                "name": {"type": "computedString", "value": ""},
                "backendDOMNodeId": 1,
                "frameId": "main-frame",
            },
            {
                "nodeId": "2",
                "role": {"type": "role", "value": "button"},
                "name": {"type": "computedString", "value": "Submit"},
                "properties": [
                    {"name": "disabled", "value": {"type": "boolean", "value": False}},
                ],
                "backendDOMNodeId": 10,
                "frameId": "main-frame",
            },
            {
                "nodeId": "3",
                "role": {"type": "role", "value": "textbox"},
                "name": {"type": "computedString", "value": "Search"},
                "properties": [
                    {"name": "focused", "value": {"type": "boolean", "value": True}},
                ],
                "backendDOMNodeId": 11,
                "frameId": "main-frame",
            },
        ],
    }
    dom_snapshot = {
        "documents": [
            {
                "frameId": "main-frame",
                "nodes": {
                    "backendNodeId": [1, 10, 11],
                },
                "layout": {
                    "nodeIndex": [0, 1, 2],
                    "bounds": [
                        [0, 0, 1920, 1080],   # WebArea
                        [800, 500, 100, 40],   # button
                        [200, 100, 300, 30],   # textbox
                    ],
                },
            },
        ],
        "strings": [],
    }

    mock_session = AsyncMock()
    mock_session.send.side_effect = [ax_tree, dom_snapshot]
    mock_context = MagicMock()
    mock_context.new_cdp_session = AsyncMock(return_value=mock_session)

    mock_page = MagicMock()
    type(mock_page).url = PropertyMock(return_value="https://example.com")
    mock_page.title = AsyncMock(return_value="Example")
    mock_page.evaluate = AsyncMock(return_value={
        "scrollX": 0, "scrollY": 0, "innerWidth": 1920, "innerHeight": 1080,
    })
    mock_page.inner_text = AsyncMock(return_value="Welcome to example")

    snapshot_text, element_map = await _snapshot_a11y(mock_page, mock_context)

    # Should have 2 interactive elements (button + textbox), not WebArea.
    assert len(element_map) == 2
    assert element_map[0]["role"] == "button"
    assert element_map[0]["text"] == "Submit"
    assert element_map[0]["center_x"] == 850.0  # 800 + 100/2
    assert element_map[0]["center_y"] == 520.0  # 500 + 40/2
    assert element_map[1]["role"] == "textbox"
    assert element_map[1]["text"] == "Search"
    assert element_map[1].get("focused") is True

    # snapshot_text includes header and elements.
    assert "https://example.com" in snapshot_text
    assert '[0] button "Submit"' in snapshot_text
    assert '[1] textbox "Search"' in snapshot_text
    assert "[focused]" in snapshot_text


@pytest.mark.asyncio
async def test_snapshot_a11y_falls_back_on_cdp_failure() -> None:
    """If CDP fails, _snapshot_a11y falls back to _snapshot_dom_legacy."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from openclose.tool.tools.browser_automation_shared import snapshot_a11y as _snapshot_a11y

    mock_context = MagicMock()
    mock_context.new_cdp_session = AsyncMock(
        side_effect=Exception("CDP unavailable"),
    )
    mock_page = MagicMock()

    # Patch legacy fallback to return a known value.
    with patch(
        "openclose.tool.tools.browser_automation_shared.snapshot_dom_legacy",
        new_callable=AsyncMock,
        return_value=("legacy snapshot", {0: {"center_x": 50, "center_y": 60}}),
    ):
        text, emap = await _snapshot_a11y(mock_page, mock_context)

    assert text == "legacy snapshot"
    assert 0 in emap


@pytest.mark.asyncio
async def test_snapshot_a11y_sanity_check_triggers_fallback() -> None:
    """Sparse a11y tree on a content-rich page: retry once, then fall back."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from openclose.tool.tools.browser_automation_shared import snapshot_a11y as _snapshot_a11y

    ax_tree: dict[str, list[Any]] = {"nodes": []}
    dom_snapshot: dict[str, Any] = {
        "documents": [
            {
                "frameId": "f1",
                "nodes": {"backendNodeId": []},
                "layout": {"nodeIndex": [], "bounds": []},
            }
        ],
        "strings": [],
    }

    # Each impl call issues ax_tree + dom_snapshot. Retry doubles that to 4.
    mock_session = AsyncMock()
    mock_session.send.side_effect = [
        ax_tree, dom_snapshot, ax_tree, dom_snapshot,
    ]
    mock_context = MagicMock()
    mock_context.new_cdp_session = AsyncMock(return_value=mock_session)

    mock_page = MagicMock()
    mock_page.evaluate = AsyncMock(return_value={
        "scrollX": 0, "scrollY": 0, "innerWidth": 1920, "innerHeight": 1080,
    })
    mock_page.inner_text = AsyncMock(return_value="x" * 600)

    with patch(
        "openclose.tool.tools.browser_automation_shared.snapshot_dom_legacy",
        new_callable=AsyncMock,
        return_value=("legacy fallback", {0: {"center_x": 1, "center_y": 2}}),
    ) as mock_legacy, patch(
        "openclose.tool.tools.browser_automation_shared.asyncio.sleep",
        new_callable=AsyncMock,
    ) as mock_sleep:
        text, emap = await _snapshot_a11y(mock_page, mock_context)

    assert text == "legacy fallback"
    # Retry consumed a second impl call (2 send calls per impl × 2 = 4 total).
    assert mock_session.send.call_count == 4
    # Legacy fallback invoked exactly once, after both impl calls failed.
    assert mock_legacy.call_count == 1
    # Retry slept once with the 1.5s window.
    assert mock_sleep.await_args is not None
    assert mock_sleep.await_args.args == (1.5,)


@pytest.mark.asyncio
async def test_snapshot_a11y_retry_recovers() -> None:
    """Sparse first impl call, full second: returns second without fallback."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from openclose.tool.tools.browser_automation_shared import (
        snapshot_a11y as _snapshot_a11y,
    )

    call_count = 0

    async def impl_side_effect(
        page: Any, context: Any, max_elements: int, page_text_chars: int
    ) -> tuple[str, dict[int, dict[str, Any]]]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("sparse a11y tree")
        return ("full snapshot", {0: {"center_x": 10, "center_y": 20}})

    with patch(
        "openclose.tool.tools.browser_automation_shared._snapshot_a11y_impl",
        new=impl_side_effect,
    ), patch(
        "openclose.tool.tools.browser_automation_shared.snapshot_dom_legacy",
        new_callable=AsyncMock,
        return_value=("LEGACY SHOULD NOT BE CALLED", {}),
    ) as mock_legacy, patch(
        "openclose.tool.tools.browser_automation_shared.asyncio.sleep",
        new_callable=AsyncMock,
    ):
        text, emap = await _snapshot_a11y(MagicMock(), MagicMock())

    assert text == "full snapshot"
    assert 0 in emap
    assert call_count == 2
    assert mock_legacy.call_count == 0


# ── _dump_page_content with iframes ────────────────────────────────────────


@pytest.mark.asyncio
async def test_dump_page_content_with_iframes() -> None:
    """_dump_page_content extracts iframe text and links."""
    from unittest.mock import AsyncMock, MagicMock, PropertyMock
    from openclose.tool.tools.browser_automation_shared import dump_page_content as _dump_page_content

    main_frame = MagicMock()
    child_frame = MagicMock()
    child_frame.url = "https://widget.example.com"
    child_frame.evaluate = AsyncMock(return_value="Chat widget text")
    child_frame.eval_on_selector_all = AsyncMock(return_value=[
        {"text": "Help", "href": "https://widget.example.com/help"},
    ])

    mock_page = MagicMock()
    type(mock_page).url = PropertyMock(return_value="https://example.com")
    mock_page.title = AsyncMock(return_value="Example")
    # page.evaluate is called twice: walker (page text), then interactive.
    mock_page.evaluate = AsyncMock(side_effect=["Main page text", []])
    mock_page.eval_on_selector_all = AsyncMock(return_value=[])
    type(mock_page).main_frame = PropertyMock(return_value=main_frame)
    mock_page.frames = [main_frame, child_frame]

    result = await _dump_page_content(mock_page)

    assert result["url"] == "https://example.com"
    assert result["page_text"] == "Main page text"
    assert len(result["iframes"]) == 1
    assert result["iframes"][0]["url"] == "https://widget.example.com"
    assert result["iframes"][0]["text"] == "Chat widget text"
    assert len(result["iframes"][0]["links"]) == 1


@pytest.mark.asyncio
async def test_dump_page_content_interactive_elements() -> None:
    """_dump_page_content captures interactive form elements via JS evaluate."""
    from unittest.mock import AsyncMock, MagicMock, PropertyMock
    from openclose.tool.tools.browser_automation_shared import dump_page_content as _dump_page_content

    mock_page = MagicMock()
    type(mock_page).url = PropertyMock(return_value="https://example.com")
    mock_page.title = AsyncMock(return_value="Example")
    mock_page.eval_on_selector_all = AsyncMock(return_value=[])
    # First call: walker (page text). Second call: interactive elements.
    mock_page.evaluate = AsyncMock(side_effect=[
        "Main text",
        [
            'radio "Option A" [selected]',
            'radio "Option B" [unselected]',
            'button "Submit"',
        ],
    ])
    type(mock_page).main_frame = PropertyMock(return_value=MagicMock())
    mock_page.frames = [mock_page.main_frame]

    result = await _dump_page_content(mock_page)

    assert len(result["interactive_elements"]) == 3
    assert 'radio "Option A" [selected]' in result["interactive_elements"]
    assert 'button "Submit"' in result["interactive_elements"]


@pytest.mark.asyncio
async def test_dump_page_content_interactive_elements_fallback() -> None:
    """Interactive elements extraction gracefully falls back on error."""
    from unittest.mock import AsyncMock, MagicMock, PropertyMock
    from openclose.tool.tools.browser_automation_shared import dump_page_content as _dump_page_content

    mock_page = MagicMock()
    type(mock_page).url = PropertyMock(return_value="https://example.com")
    mock_page.title = AsyncMock(return_value="Example")
    mock_page.eval_on_selector_all = AsyncMock(return_value=[])
    # Walker call succeeds; interactive-elements call raises.
    mock_page.evaluate = AsyncMock(side_effect=["Main text", Exception("JS error")])
    type(mock_page).main_frame = PropertyMock(return_value=MagicMock())
    mock_page.frames = [mock_page.main_frame]

    result = await _dump_page_content(mock_page)

    assert result["interactive_elements"] == []
    assert result["url"] == "https://example.com"
    assert result["page_text"] == "Main text"


@pytest.mark.asyncio
async def test_dump_page_content_cross_origin_iframe() -> None:
    """Cross-origin iframes that throw are listed as inaccessible."""
    from unittest.mock import AsyncMock, MagicMock, PropertyMock
    from openclose.tool.tools.browser_automation_shared import dump_page_content as _dump_page_content

    main_frame = MagicMock()
    cross_origin_frame = MagicMock()
    cross_origin_frame.url = "https://ads.example.com"
    cross_origin_frame.evaluate = AsyncMock(
        side_effect=Exception("cross-origin"),
    )

    mock_page = MagicMock()
    type(mock_page).url = PropertyMock(return_value="https://example.com")
    mock_page.title = AsyncMock(return_value="Example")
    mock_page.evaluate = AsyncMock(side_effect=["Main text", []])
    mock_page.eval_on_selector_all = AsyncMock(return_value=[])
    type(mock_page).main_frame = PropertyMock(return_value=main_frame)
    mock_page.frames = [main_frame, cross_origin_frame]

    result = await _dump_page_content(mock_page)

    assert len(result["iframes"]) == 1
    assert "inaccessible" in result["iframes"][0]["text"]
    assert result["iframes"][0]["links"] == []


# ── describe_outcome ───────────────────────────────────────────────────────

def test_describe_outcome_first_step_is_empty() -> None:
    from openclose.tool.tools.browser_automation_shared import describe_outcome

    assert describe_outcome("", "any snapshot", "", "http://a") == ""


def test_describe_outcome_no_changes() -> None:
    from openclose.tool.tools.browser_automation_shared import describe_outcome

    snap = "[0] button 'Login'\n[1] link 'Help'"
    out = describe_outcome(snap, snap, "http://a", "http://a")
    assert "URL: unchanged" in out
    assert "no changes detected" in out
    assert "silently failed" not in out  # planner-facing text is in the prompt


def test_describe_outcome_url_changed() -> None:
    from openclose.tool.tools.browser_automation_shared import describe_outcome

    prev = "[0] link 'Docs'"
    curr = "[0] heading 'Docs page'\n[1] button 'Edit'"
    out = describe_outcome(prev, curr, "http://a", "http://b")
    assert "URL: changed (http://a → http://b)" in out


def test_describe_outcome_large_scale_change() -> None:
    from openclose.tool.tools.browser_automation_shared import describe_outcome

    prev = "\n".join(f"[{i}] link 'p{i}'" for i in range(20))
    curr = "\n".join(f"[{i}] heading 'h{i}'" for i in range(20))
    out = describe_outcome(prev, curr, "http://a", "http://a")
    assert "large-scale change" in out
    assert "URL: unchanged" in out


def test_describe_outcome_partial_diff() -> None:
    from openclose.tool.tools.browser_automation_shared import describe_outcome

    prev = "[0] button 'A'\n[1] button 'B'\n[2] button 'C'"
    curr = "[0] button 'A'\n[1] button 'B'\n[2] button 'C'\n[3] button 'D'"
    out = describe_outcome(prev, curr, "http://a", "http://a")
    assert "URL: unchanged" in out
    assert "DOM changes:" in out
    assert "+ button 'D'" in out


# ── wait_after_action ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_wait_after_action_nav_action_waits_for_load_and_networkidle() -> None:
    from unittest.mock import AsyncMock, MagicMock
    from openclose.tool.tools.browser_automation_shared import wait_after_action

    page = MagicMock()
    page.wait_for_load_state = AsyncMock()

    await wait_after_action(page, "history_back")

    calls = page.wait_for_load_state.await_args_list
    assert len(calls) == 2
    assert calls[0].args[0] == "load" and calls[0].kwargs["timeout"] == 10000
    assert calls[1].args[0] == "networkidle" and calls[1].kwargs["timeout"] == 3000


@pytest.mark.asyncio
async def test_wait_after_action_maybe_nav_only_networkidle() -> None:
    from unittest.mock import AsyncMock, MagicMock
    from openclose.tool.tools.browser_automation_shared import wait_after_action

    for action in ("left_click", "key"):
        page = MagicMock()
        page.wait_for_load_state = AsyncMock()

        await wait_after_action(page, action)

        calls = page.wait_for_load_state.await_args_list
        assert len(calls) == 1, f"{action}: expected 1 call, got {len(calls)}"
        assert calls[0].args[0] == "networkidle"
        assert calls[0].kwargs["timeout"] == 1500


@pytest.mark.asyncio
async def test_wait_after_action_local_action_sleeps_briefly() -> None:
    from unittest.mock import AsyncMock, MagicMock, patch
    from openclose.tool.tools.browser_automation_shared import wait_after_action

    for action in ("type", "scroll", "mouse_move", "wait", "pause_and_memorize_fact"):
        page = MagicMock()
        page.wait_for_load_state = AsyncMock()

        with patch(
            "openclose.tool.tools.browser_automation_shared.asyncio.sleep",
            new_callable=AsyncMock,
        ) as mock_sleep:
            await wait_after_action(page, action)

        assert page.wait_for_load_state.call_count == 0, f"{action}: should not touch load_state"
        assert mock_sleep.await_args is not None, f"{action}: sleep not awaited"
        assert mock_sleep.await_args.args == (0.3,), f"{action}: expected 0.3s sleep"


@pytest.mark.asyncio
async def test_wait_after_action_swallows_timeout_exceptions() -> None:
    """Long-poll / websocket sites never reach networkidle — must not stall."""
    from unittest.mock import AsyncMock, MagicMock
    from openclose.tool.tools.browser_automation_shared import wait_after_action

    page = MagicMock()
    page.wait_for_load_state = AsyncMock(side_effect=Exception("timeout"))

    # Should not raise.
    await wait_after_action(page, "history_back")
    await wait_after_action(page, "left_click")


# ── _build_planner_user_turn (dom): action_outcome block ───────────────────

def test_build_planner_user_turn_dom_injects_outcome_when_present() -> None:
    from openclose.tool.tools.browser_automation import (
        _build_planner_user_turn,
    )

    msg = _build_planner_user_turn(
        step=1, task="find stuff", memory=[], steps_log=[],
        current_url="http://a", snapshot_text="[0] button 'X'",
        img_b64=None, mode="dom",
        initial_url="",
        action_outcome="URL: unchanged\nDOM: no changes detected — silent fail",
    )
    content = msg["content"]
    assert isinstance(content, str)
    # Outcome block sits between "Recent actions" and "CURRENT page state".
    idx_recent = content.index("Recent actions")
    idx_outcome = content.index("Outcome since last snapshot:")
    idx_current = content.index("CURRENT page state")
    assert idx_recent < idx_outcome < idx_current
    assert "silent fail" in content


def test_build_planner_user_turn_dom_skips_outcome_when_empty() -> None:
    from openclose.tool.tools.browser_automation import (
        _build_planner_user_turn,
    )

    msg = _build_planner_user_turn(
        step=0, task="find stuff", memory=[], steps_log=[],
        current_url="http://a", snapshot_text="[0] button 'X'",
        img_b64=None, mode="dom",
        initial_url="",
        action_outcome="",
    )
    content = msg["content"]
    assert "Outcome since last snapshot:" not in content


# ── validate_intent ──────────────────────────────────────────────────────────

def test_validate_intent_missing() -> None:
    from openclose.tool.tools.browser_automation_shared import validate_intent
    err = validate_intent("", "", "")
    assert err is not None
    assert err.startswith("intent parameter is required")


def test_validate_intent_unknown() -> None:
    from openclose.tool.tools.browser_automation_shared import validate_intent
    err = validate_intent("teleport", "", "https://x")
    assert err is not None
    assert err.startswith("Unknown intent 'teleport'")
    # lists current valid values
    assert "visit_url" in err
    assert "act_on_page" in err
    assert "web_search" in err
    # the renamed-away intents must not be advertised
    assert "goto_page" not in err
    assert "'navigate'" not in err  # the bare intent name
    # the dropped intent must not be advertised
    assert "goto_page_and_read_body" not in err


def test_validate_intent_visit_url_requires_url() -> None:
    from openclose.tool.tools.browser_automation_shared import validate_intent
    err = validate_intent("visit_url", "", "")
    assert err is not None
    assert "requires a url" in err


def test_validate_intent_visit_url_rejects_task() -> None:
    from openclose.tool.tools.browser_automation_shared import validate_intent
    err = validate_intent("visit_url", "do stuff", "https://x")
    assert err is not None
    assert "doesn't accept a task" in err
    assert "act_on_page" in err  # points to correct intent


def test_validate_intent_visit_url_rejects_query() -> None:
    from openclose.tool.tools.browser_automation_shared import validate_intent
    err = validate_intent("visit_url", "", "https://x", "cats")
    assert err is not None
    assert "doesn't accept a query" in err
    assert "web_search" in err  # points to correct intent


def test_validate_intent_visit_url_valid() -> None:
    from openclose.tool.tools.browser_automation_shared import validate_intent
    assert validate_intent("visit_url", "", "https://x") is None


def test_validate_intent_dropped_legacy_intent_unknown() -> None:
    """The merged tool no longer accepts the old `goto_page_and_read_body`
    intent — it must now produce an Unknown-intent error like any other
    bogus value. Same applies to the renamed-away `goto_page` / `navigate`."""
    from openclose.tool.tools.browser_automation_shared import validate_intent
    err = validate_intent("goto_page_and_read_body", "", "https://x")
    assert err is not None
    assert err.startswith("Unknown intent 'goto_page_and_read_body'")
    err = validate_intent("goto_page", "", "https://x")
    assert err is not None
    assert err.startswith("Unknown intent 'goto_page'")
    err = validate_intent("navigate", "do stuff", "")
    assert err is not None
    assert err.startswith("Unknown intent 'navigate'")


def test_validate_intent_act_on_page_requires_task() -> None:
    from openclose.tool.tools.browser_automation_shared import validate_intent
    err = validate_intent("act_on_page", "", "https://x")
    assert err is not None
    assert "requires a task" in err


def test_validate_intent_act_on_page_valid_with_task_only() -> None:
    from openclose.tool.tools.browser_automation_shared import validate_intent
    assert validate_intent("act_on_page", "reach goal", "") is None


def test_validate_intent_act_on_page_valid_with_task_and_url() -> None:
    from openclose.tool.tools.browser_automation_shared import validate_intent
    assert validate_intent("act_on_page", "reach goal", "https://x") is None


def test_validate_intent_act_on_page_rejects_query() -> None:
    from openclose.tool.tools.browser_automation_shared import validate_intent
    err = validate_intent("act_on_page", "reach goal", "", "cats")
    assert err is not None
    assert "doesn't accept a query" in err
    assert "web_search" in err


def test_validate_intent_web_search_requires_query() -> None:
    from openclose.tool.tools.browser_automation_shared import validate_intent
    err = validate_intent("web_search", "", "", "")
    assert err is not None
    assert "requires a query" in err


def test_validate_intent_web_search_rejects_task() -> None:
    from openclose.tool.tools.browser_automation_shared import validate_intent
    err = validate_intent("web_search", "do stuff", "", "cats")
    assert err is not None
    assert "doesn't accept a task" in err
    assert "act_on_page" in err


def test_validate_intent_web_search_rejects_url() -> None:
    from openclose.tool.tools.browser_automation_shared import validate_intent
    err = validate_intent("web_search", "", "https://x", "cats")
    assert err is not None
    assert "doesn't accept a url" in err
    assert "visit_url" in err


def test_validate_intent_web_search_valid() -> None:
    from openclose.tool.tools.browser_automation_shared import validate_intent
    assert validate_intent("web_search", "", "", "cats") is None


# ── format_tool_output short_mode ────────────────────────────────────────────

def _make_fake_page_content() -> dict[str, Any]:
    return {
        "url": "https://example.com/page",
        "title": "Example Page",
        "page_text": "This is the body text of the page.",
        "interactive_elements": ["button 'Submit'", "input 'Email'"],
        "links": [
            {"text": "About", "href": "https://example.com/about"},
            {"text": "Contact", "href": "https://example.com/contact"},
        ],
        "iframes": [
            {
                "url": "https://example.com/embed",
                "text": "Embedded iframe text",
                "links": [{"text": "Iframe link", "href": "https://example.com/x"}],
            },
        ],
    }


def test_format_tool_output_short_mode_suppresses_content() -> None:
    from openclose.tool.tools.browser_automation_shared import format_tool_output
    out = format_tool_output(
        final_status="Task terminated with status: success",
        last_thinking="Reached the page",
        steps_log=[],
        page_content=_make_fake_page_content(),
        short_mode=True,
    )
    # Top-level fields kept.
    assert "Status: success" in out
    assert "URL: https://example.com/page" in out
    assert "Page title: Example Page" in out
    assert "Navigator observations: Reached the page" in out
    # Heavy sections suppressed.
    assert "--- Page content ---" not in out
    assert "--- Interactive elements on page ---" not in out
    assert "--- Links on page ---" not in out
    assert "--- Iframe:" not in out
    # And the actual section bodies.
    assert "This is the body text of the page." not in out
    assert "https://example.com/about" not in out


def test_format_tool_output_full_mode_includes_content() -> None:
    from openclose.tool.tools.browser_automation_shared import format_tool_output
    out = format_tool_output(
        final_status="Task terminated with status: success",
        last_thinking="Reached the page",
        steps_log=[],
        page_content=_make_fake_page_content(),
        short_mode=False,
    )
    # Top-level fields kept.
    assert "Status: success" in out
    assert "URL: https://example.com/page" in out
    # Page text is NEVER inlined — it lives in the on-disk dump only.
    assert "--- Page content ---" not in out
    assert "This is the body text of the page." not in out
    # Other sections are inlined in the report.
    assert "--- Interactive elements on page ---" in out
    assert "button 'Submit'" in out
    assert "--- Links on page ---" in out
    assert "[About](https://example.com/about)" in out
    assert "--- Iframe: https://example.com/embed ---" in out
    assert "Embedded iframe text" in out


def test_format_tool_output_short_mode_failure_includes_failure_reason() -> None:
    from openclose.tool.tools.browser_automation_shared import (
        format_tool_output, FailureReason,
    )
    out = format_tool_output(
        final_status="Task terminated with status: failure — stuck",
        last_thinking="Couldn't find it",
        steps_log=[],
        page_content={"url": "https://x", "title": "T"},
        failure_reason=FailureReason.ELEMENT_NOT_IN_TREE,
        short_mode=True,
    )
    assert "Status: failure" in out
    assert "failure_reason: element_not_in_tree" in out
    assert "Hint:" in out  # diagnostic hint still emitted
    # But still no content sections.
    assert "--- Page content ---" not in out


def test_format_tool_output_hint_does_not_name_old_tool() -> None:
    """The unified tool's failure hint must not refer to the deleted
    browser_automation_vision tool."""
    from openclose.tool.tools.browser_automation_shared import (
        format_tool_output, FailureReason,
    )
    out = format_tool_output(
        final_status="Task terminated with status: failure — stuck",
        last_thinking="Couldn't find it",
        steps_log=[],
        page_content={"url": "https://x", "title": "T"},
        failure_reason=FailureReason.ELEMENT_NOT_IN_TREE,
        short_mode=True,
    )
    assert "browser_automation_vision" not in out
    assert "browser_automation_dom" not in out


# ── fuzzy_match_a11y ────────────────────────────────────────────────────────

def _emap(*items: tuple[str, str, dict[str, Any] | None]) -> dict[int, dict[str, Any]]:
    """Tiny element_map builder for fuzzy-matcher tests.

    Each *item* is ``(role, text, extras_or_None)``. Index assigned in order.
    """
    out: dict[int, dict[str, Any]] = {}
    for idx, (role, text, extras) in enumerate(items):
        el: dict[str, Any] = {
            "role": role, "text": text,
            "in_viewport": True, "disabled": False,
        }
        if extras:
            el.update(extras)
        out[idx] = el
    return out


def test_fuzzy_match_a11y_exact_match() -> None:
    from openclose.tool.tools.browser_automation_shared import fuzzy_match_a11y

    em = _emap(
        ("button", "Submit", None),
        ("link", "Home", None),
    )
    idx, status = fuzzy_match_a11y("submit", em)
    assert idx == 0
    assert "match" in status.lower()


def test_fuzzy_match_a11y_substring_match() -> None:
    from openclose.tool.tools.browser_automation_shared import fuzzy_match_a11y

    em = _emap(
        ("button", "Sign in to your account", None),
    )
    idx, _status = fuzzy_match_a11y("sign in", em)
    assert idx == 0


def test_fuzzy_match_a11y_no_match_returns_none() -> None:
    from openclose.tool.tools.browser_automation_shared import fuzzy_match_a11y

    em = _emap(
        ("button", "Submit", None),
        ("link", "Home", None),
    )
    idx, status = fuzzy_match_a11y("settings gear icon", em)
    assert idx is None
    assert "no a11y match" in status.lower()


def test_fuzzy_match_a11y_skips_disabled() -> None:
    from openclose.tool.tools.browser_automation_shared import fuzzy_match_a11y

    em = _emap(
        ("button", "Submit", {"disabled": True}),
        ("button", "Submit", None),
    )
    idx, _status = fuzzy_match_a11y("submit", em)
    assert idx == 1


def test_fuzzy_match_a11y_in_viewport_bonus_breaks_tie() -> None:
    from openclose.tool.tools.browser_automation_shared import fuzzy_match_a11y

    em = _emap(
        ("button", "Submit", {"in_viewport": False}),
        ("button", "Submit", {"in_viewport": True}),
    )
    idx, _status = fuzzy_match_a11y("submit", em)
    assert idx == 1


def test_fuzzy_match_a11y_ambiguity_rejected() -> None:
    """Two candidates with similar weak scores should fall through to None."""
    from openclose.tool.tools.browser_automation_shared import fuzzy_match_a11y

    # Two long haystacks, both contain the target as substring → both score 60.
    em = _emap(
        ("link", "Click here to log in to the staging site", None),
        ("link", "Click here to log in to the production site", None),
    )
    idx, status = fuzzy_match_a11y("log in", em)
    # Expect ambiguity rejection (top two within 5 points and both < 80).
    assert idx is None
    assert "ambiguous" in status.lower() or "no a11y match" in status.lower()


def test_fuzzy_match_a11y_empty_target() -> None:
    from openclose.tool.tools.browser_automation_shared import fuzzy_match_a11y

    em = _emap(("button", "Submit", None))
    idx, _status = fuzzy_match_a11y("", em)
    assert idx is None


def test_fuzzy_match_a11y_empty_map() -> None:
    from openclose.tool.tools.browser_automation_shared import fuzzy_match_a11y

    idx, _status = fuzzy_match_a11y("submit", {})
    assert idx is None


# ── resolve_action_target (resolver chain) ──────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_action_target_element_index_hit() -> None:
    from openclose.tool.tools.browser_automation import resolve_action_target

    em = {
        0: {
            "role": "button", "text": "OK",
            "center_x": 100, "center_y": 200,
            "backend_node_id": None, "in_viewport": True,
        }
    }
    args = {"action": "left_click", "element_index": 0}
    result, status = await resolve_action_target(
        args, em, page=None, context=None, mode="dom",
        grounding_provider=None, img_b64=None,
        resized_w=None, resized_h=None,
        project_dir=".", step=1,
    )
    assert result is not None
    assert result["coordinate"] == [100, 200]
    assert "[0]" in status


@pytest.mark.asyncio
async def test_resolve_action_target_target_fuzzy_hit_dom() -> None:
    """In dom mode, ``target`` resolves via fuzzy a11y, no grounding call."""
    from openclose.tool.tools.browser_automation import resolve_action_target

    em = {
        0: {
            "role": "button", "text": "Sign in",
            "center_x": 250, "center_y": 50,
            "backend_node_id": None, "in_viewport": True, "disabled": False,
        },
        1: {
            "role": "link", "text": "Forgot password",
            "center_x": 300, "center_y": 80,
            "backend_node_id": None, "in_viewport": True, "disabled": False,
        },
    }
    args = {"action": "left_click", "target": "Sign in"}
    result, status = await resolve_action_target(
        args, em, page=None, context=None, mode="dom",
        grounding_provider=None, img_b64=None,
        resized_w=None, resized_h=None,
        project_dir=".", step=1,
    )
    assert result is not None
    assert result["coordinate"] == [250, 50]
    assert "fuzzy a11y match" in status


@pytest.mark.asyncio
async def test_resolve_action_target_target_miss_dom_returns_none() -> None:
    """In dom mode without grounding, an unresolvable target fails."""
    from openclose.tool.tools.browser_automation import resolve_action_target

    em = {
        0: {
            "role": "button", "text": "Submit",
            "center_x": 100, "center_y": 100,
            "backend_node_id": None, "in_viewport": True,
        },
    }
    args = {
        "action": "left_click",
        "target": "the rotating saturn icon in the canvas widget",
    }
    result, status = await resolve_action_target(
        args, em, page=None, context=None, mode="dom",
        grounding_provider=None, img_b64=None,
        resized_w=None, resized_h=None,
        project_dir=".", step=1,
    )
    assert result is None
    assert "not found" in status.lower() or "no a11y match" in status.lower()


# ── _detect_layers_batched equivalence ──────────────────────────────────────

@pytest.mark.asyncio
async def test_detect_layers_batched_marks_iframes_and_no_bid_as_main() -> None:
    """Iframe and missing-backend-node-id elements default to layer=main
    without ever touching the CDP session."""
    from unittest.mock import MagicMock
    from openclose.tool.tools.browser_automation_shared import _detect_layers_batched

    elements: list[dict[str, Any]] = [
        {"frame_id": "https://child.iframe", "backend_node_id": 99},
        {"frame_id": "main", "backend_node_id": None},
    ]
    mock_context = MagicMock()  # never used because main_only is empty
    mock_page = MagicMock()

    await _detect_layers_batched(elements, mock_context, mock_page)

    for el in elements:
        assert el["layer_id"] == "main"
        assert el["layer_z"] == 0
    # CDP session was never opened.
    mock_context.new_cdp_session.assert_not_called()


@pytest.mark.asyncio
async def test_detect_layers_batched_concurrent_resolves() -> None:
    """For main-frame elements with backend_node_ids, both CDP waves run
    via asyncio.gather. The function annotates each element with the
    layer info returned by Runtime.callFunctionOn."""
    from unittest.mock import AsyncMock, MagicMock
    from openclose.tool.tools.browser_automation_shared import _detect_layers_batched

    el_a = {"frame_id": "main", "backend_node_id": 10}
    el_b = {"frame_id": "main", "backend_node_id": 11}
    elements = [el_a, el_b]

    mock_session = AsyncMock()
    # Order is stable — gather preserves input order in result.
    mock_session.send.side_effect = [
        {"object": {"objectId": "obj-A"}},
        {"object": {"objectId": "obj-B"}},
        {"result": {"value": {"id": "main", "zIndex": 0, "label": ""}}},
        {"result": {"value": {"id": "overlay:foo", "zIndex": 50, "label": "Modal"}}},
    ]
    mock_context = MagicMock()
    mock_context.new_cdp_session = AsyncMock(return_value=mock_session)
    mock_page = MagicMock()

    await _detect_layers_batched(elements, mock_context, mock_page)

    assert el_a["layer_id"] == "main"
    assert el_a["layer_z"] == 0
    assert el_b["layer_id"] == "overlay:foo"
    assert el_b["layer_z"] == 50
    assert el_b["layer_label"] == "Modal"
    # Two waves of two CDP calls each = 4 total.
    assert mock_session.send.call_count == 4


# ── navigation dump persistence ─────────────────────────────────────────────

@pytest.mark.skipif(
    sys.platform == "win32",
    reason="str(Path('/tmp/...')) yields backslash-separated paths on Windows",
)
def test_format_tool_output_saved_line_includes_grep_read_hint() -> None:
    """`Page content saved at:` appears whenever a dump path is provided,
    with the Grep/Read recovery hint."""
    from pathlib import Path
    from openclose.tool.tools.browser_automation_shared import format_tool_output
    out = format_tool_output(
        final_status="Task terminated with status: success",
        last_thinking="Reached the page",
        steps_log=[],
        page_content=_make_fake_page_content(),
        short_mode=False,
        dump_path=Path("/tmp/openclose/navigation/2026-05-06_example.md"),
    )
    assert (
        "Page content saved at: /tmp/openclose/navigation/2026-05-06_example.md"
        in out
    )
    assert "Grep and Read" in out
    # The legacy "Full report saved:" wording is gone.
    assert "Full report saved:" not in out


def test_format_tool_output_no_saved_line_when_dump_path_missing() -> None:
    """Without a dump path the saved-line is suppressed."""
    from openclose.tool.tools.browser_automation_shared import format_tool_output
    out = format_tool_output(
        final_status="Task terminated with status: success",
        last_thinking="ok",
        steps_log=[],
        page_content=_make_fake_page_content(),
        short_mode=False,
        dump_path=None,
    )
    assert "Page content saved at:" not in out
    assert "Grep and Read" not in out


def test_format_tool_output_full_mode_caps_doubled() -> None:
    """Caps: 30 links / 100 interactive / 6000 iframe text / 20 iframe
    links. Truncation markers must still render past the cap."""
    from openclose.tool.tools.browser_automation_shared import (
        AGENT_LINKS_CAP, AGENT_INTERACTIVE_CAP,
        AGENT_IFRAME_TEXT_CAP, AGENT_IFRAME_LINKS_CAP,
        format_tool_output,
    )
    assert AGENT_LINKS_CAP == 30
    assert AGENT_INTERACTIVE_CAP == 100
    assert AGENT_IFRAME_TEXT_CAP == 6000
    assert AGENT_IFRAME_LINKS_CAP == 20

    pc = _make_fake_page_content()
    pc["links"] = [
        {"text": f"L{i}", "href": f"https://x/{i}"}
        for i in range(AGENT_LINKS_CAP + 5)
    ]
    pc["interactive_elements"] = [
        f"button 'B{i}'" for i in range(AGENT_INTERACTIVE_CAP + 7)
    ]
    pc["iframes"] = [{
        "url": "https://example.com/embed",
        "text": "z" * (AGENT_IFRAME_TEXT_CAP + 50),
        "links": [
            {"text": f"IL{i}", "href": f"https://y/{i}"}
            for i in range(AGENT_IFRAME_LINKS_CAP + 3)
        ],
    }]

    out = format_tool_output(
        final_status="Task terminated with status: success",
        last_thinking="ok",
        steps_log=[],
        page_content=pc,
        short_mode=False,
    )
    last_link = AGENT_LINKS_CAP - 1
    last_int = AGENT_INTERACTIVE_CAP - 1
    assert f"[L{last_link}](https://x/{last_link})" in out
    assert f"[L{AGENT_LINKS_CAP}](https://x/{AGENT_LINKS_CAP})" not in out
    assert f"button 'B{last_int}'" in out
    assert f"button 'B{AGENT_INTERACTIVE_CAP}'" not in out
    # Trailing truncation markers.
    assert "5 more links truncated" in out
    assert "7 more elements truncated" in out
    assert "[iframe text truncated]" in out
    assert "3 more iframe links truncated" in out


# ── format_tool_output exact-duplicate collapsing ────────────────────────────

def test_format_tool_output_dedupes_interactive_elements() -> None:
    """30 identical button rows collapse to one row with a (×30) suffix."""
    from openclose.tool.tools.browser_automation_shared import format_tool_output
    pc = _make_fake_page_content()
    pc["interactive_elements"] = ["button 'Share'"] * 30
    out = format_tool_output(
        final_status="Task terminated with status: success",
        last_thinking="ok",
        steps_log=[],
        page_content=pc,
        short_mode=False,
    )
    assert out.count("button 'Share' (×30)") == 1
    # No bare row remains alongside the deduped row.
    assert "  button 'Share'\n" not in out


def test_format_tool_output_dedup_respects_layer_separators() -> None:
    """The same row in two different layers is NOT merged across layers —
    each layer dedupes independently."""
    from openclose.tool.tools.browser_automation_shared import format_tool_output
    pc = _make_fake_page_content()
    pc["interactive_elements"] = [
        "=== ACTIVE LAYER: dialog ===",
        "button 'Share'",
        "button 'Share'",
        "=== BACKGROUND: Main page ===",
        "button 'Share'",
    ]
    out = format_tool_output(
        final_status="Task terminated with status: success",
        last_thinking="ok",
        steps_log=[],
        page_content=pc,
        short_mode=False,
    )
    # Active layer: collapsed to a single (×2) row.
    assert out.count("button 'Share' (×2)") == 1
    # Background layer: singleton row stays as-is, no count suffix.
    bg_section = out.split("=== BACKGROUND:")[1]
    assert "button 'Share'" in bg_section
    assert "(×" not in bg_section
    # And (×1) never appears anywhere.
    assert "(×1)" not in out


def test_format_tool_output_dedupes_links_by_text_and_href() -> None:
    """Three identical (text, href) link dicts collapse to one (×3) row;
    same href with a different anchor text stays as a distinct row."""
    from openclose.tool.tools.browser_automation_shared import format_tool_output
    pc = _make_fake_page_content()
    pc["links"] = [
        {"text": "Same", "href": "https://x/y"},
        {"text": "Same", "href": "https://x/y"},
        {"text": "Same", "href": "https://x/y"},
        {"text": "Different anchor", "href": "https://x/y"},
    ]
    out = format_tool_output(
        final_status="Task terminated with status: success",
        last_thinking="ok",
        steps_log=[],
        page_content=pc,
        short_mode=False,
    )
    assert out.count("[Same](https://x/y) (×3)") == 1
    assert "[Different anchor](https://x/y)" in out
    assert "[Different anchor](https://x/y) (×" not in out


def test_format_tool_output_dedupes_iframe_links() -> None:
    """Three identical iframe link dicts collapse to one (×3) row."""
    from openclose.tool.tools.browser_automation_shared import format_tool_output
    pc = _make_fake_page_content()
    pc["iframes"] = [{
        "url": "https://example.com/embed",
        "text": "",
        "links": [
            {"text": "Same iframe link", "href": "https://y/z"},
            {"text": "Same iframe link", "href": "https://y/z"},
            {"text": "Same iframe link", "href": "https://y/z"},
        ],
    }]
    out = format_tool_output(
        final_status="Task terminated with status: success",
        last_thinking="ok",
        steps_log=[],
        page_content=pc,
        short_mode=False,
    )
    assert out.count("[Same iframe link](https://y/z) (×3)") == 1


def test_format_tool_output_cap_counts_unique_entries() -> None:
    """The interactive-element cap counts unique rows after dedup, so
    99 distinct buttons + 50 duplicates of one row still fit in the cap
    with no truncation marker."""
    from openclose.tool.tools.browser_automation_shared import (
        AGENT_INTERACTIVE_CAP, format_tool_output,
    )
    assert AGENT_INTERACTIVE_CAP == 100
    pc = _make_fake_page_content()
    pc["interactive_elements"] = (
        [f"button 'B{i}'" for i in range(99)]
        + ["button 'Share'"] * 50
    )
    out = format_tool_output(
        final_status="Task terminated with status: success",
        last_thinking="ok",
        steps_log=[],
        page_content=pc,
        short_mode=False,
    )
    assert "button 'B0'" in out
    assert "button 'B98'" in out
    assert "button 'Share' (×50)" in out
    assert "more elements truncated" not in out


def test_format_tool_output_dedup_truncation_uses_unique_count() -> None:
    """When unique entries exceed the cap, the truncation count is the
    number of unique entries beyond the cap — not raw occurrences."""
    from openclose.tool.tools.browser_automation_shared import format_tool_output
    pc = _make_fake_page_content()
    # 105 distinct + 30 dups of B0. Unique = 105, raw = 135.
    # Beyond the 100 cap: 5 unique entries (not 35 raw).
    pc["interactive_elements"] = (
        [f"button 'B{i}'" for i in range(105)]
        + ["button 'B0'"] * 30
    )
    out = format_tool_output(
        final_status="Task terminated with status: success",
        last_thinking="ok",
        steps_log=[],
        page_content=pc,
        short_mode=False,
    )
    assert "5 more elements truncated" in out
    assert "35 more elements truncated" not in out


def test_format_tool_output_single_occurrence_no_count_suffix() -> None:
    """A singleton entry never gains a (×1) suffix."""
    from openclose.tool.tools.browser_automation_shared import format_tool_output
    pc = _make_fake_page_content()
    pc["interactive_elements"] = ["button 'Submit'"]
    pc["links"] = [{"text": "About", "href": "https://example.com/about"}]
    pc["iframes"] = [{
        "url": "https://example.com/embed",
        "text": "",
        "links": [{"text": "Iframe link", "href": "https://example.com/x"}],
    }]
    out = format_tool_output(
        final_status="Task terminated with status: success",
        last_thinking="ok",
        steps_log=[],
        page_content=pc,
        short_mode=False,
    )
    assert "(×1)" not in out
    assert "button 'Submit'" in out
    assert "[About](https://example.com/about)" in out
    assert "[Iframe link](https://example.com/x)" in out


def test_build_navigation_dump_only_writes_page_content() -> None:
    """The dump now contains only the URL/title/captured header and the
    `## Page content` body — interactive/links/iframe sections are
    rendered in the agent's report and not duplicated on disk."""
    from datetime import datetime, timezone
    from openclose.tool.tools.browser_automation_shared import (
        _build_navigation_dump,
    )
    pc = _make_fake_page_content()
    body = _build_navigation_dump(
        pc, datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc),
    )
    # Header intact.
    assert body.startswith("# Browser navigation dump")
    assert "URL: https://example.com/page" in body
    assert "Page title: Example Page" in body
    assert "Captured: 2026-05-06T12:00:00+00:00" in body
    # Page content is the only persisted section, with full body.
    assert "## Page content" in body
    assert pc["page_text"] in body
    # Other sections are NOT persisted.
    assert "## Interactive elements on page" not in body
    assert "## Links on page" not in body
    assert "## Iframe:" not in body
    assert "button 'Submit'" not in body
    assert "[About](https://example.com/about)" not in body
    assert "Embedded iframe text" not in body


def test_build_navigation_dump_does_not_truncate_page_text() -> None:
    """The on-disk dump never truncates — the agent recovers full
    content via Grep/Read on the file."""
    from datetime import datetime, timezone
    from openclose.tool.tools.browser_automation_shared import (
        _build_navigation_dump,
    )
    pc = _make_fake_page_content()
    pc["page_text"] = "y" * 200_000
    body = _build_navigation_dump(
        pc, datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert pc["page_text"] in body


def test_make_navigation_filename_basic() -> None:
    from datetime import datetime, timezone
    from openclose.tool.tools.browser_automation_shared import (
        _make_navigation_filename,
    )
    name = _make_navigation_filename(
        "https://example.com/path?q=1",
        datetime(2026, 5, 6, 15, 42, 11, tzinfo=timezone.utc),
    )
    # ISO timestamp prefix with dashes (cross-platform), domain, suffix, ext.
    assert name.startswith("2026-05-06T15-42-11Z_example.com_")
    assert name.endswith(".md")
    assert ":" not in name  # Windows-safe.


def test_make_navigation_filename_idn_punycode_safe() -> None:
    from datetime import datetime, timezone
    from openclose.tool.tools.browser_automation_shared import (
        _make_navigation_filename,
    )
    # IDN: urlparse returns punycode for non-ASCII netlocs, so the
    # resulting filename is ASCII-safe out of the box.
    name = _make_navigation_filename(
        "https://xn--r8jz45g.example/path",
        datetime(2026, 5, 6, 15, 42, 11, tzinfo=timezone.utc),
    )
    assert name.endswith(".md")
    # Only [a-zA-Z0-9.-_] should appear in the filename.
    import re as _re
    assert _re.fullmatch(r"[a-zA-Z0-9.\-_]+\.md", name) is not None


def test_make_navigation_filename_truncates_long_domain() -> None:
    from datetime import datetime, timezone
    from openclose.tool.tools.browser_automation_shared import (
        _make_navigation_filename,
    )
    long_domain = "a" * 200 + ".example.com"
    name = _make_navigation_filename(
        f"https://{long_domain}/x",
        datetime(2026, 5, 6, 15, 42, 11, tzinfo=timezone.utc),
    )
    # Domain segment is bounded; full name remains a sensible length.
    assert len(name) < 150


def test_prune_navigation_dir_keeps_most_recent_n(tmp_path: Path) -> None:
    from openclose.tool.tools.browser_automation_shared import (
        _prune_navigation_dir,
    )
    nav_dir = tmp_path / "navigation"
    nav_dir.mkdir()
    # Names sort lexicographically; create 205, prune to 200.
    names = [f"2026-05-06T00-00-{i:03d}Z_a_x.md" for i in range(205)]
    for n in names:
        (nav_dir / n).write_text("x")
    _prune_navigation_dir(nav_dir, keep_n=200)
    remaining = sorted(p.name for p in nav_dir.iterdir())
    assert len(remaining) == 200
    # The 5 oldest (lowest indices) are gone.
    assert remaining == sorted(names)[5:]


def test_prune_navigation_dir_missing_dir_no_raise(tmp_path: Path) -> None:
    from openclose.tool.tools.browser_automation_shared import (
        _prune_navigation_dir,
    )
    # Should not raise even though the directory does not exist.
    _prune_navigation_dir(tmp_path / "does-not-exist", keep_n=200)


def test_write_navigation_dump_writes_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openclose.config.paths import ConfigPaths
    from openclose.tool.tools.browser_automation_shared import (
        write_navigation_dump,
    )
    monkeypatch.setattr(
        ConfigPaths, "project_runtime_dir",
        classmethod(lambda cls, _pd: tmp_path),
    )
    pc = _make_fake_page_content()
    path = write_navigation_dump(".", pc)
    assert path is not None
    assert path.exists()
    assert path.parent == tmp_path / "navigation"
    body = path.read_text()
    assert body.startswith("# Browser navigation dump")
    assert "## Page content" in body
    assert pc["page_text"] in body
    # Other sections are NOT persisted on disk.
    assert "## Interactive elements on page" not in body
    assert "## Links on page" not in body
    assert "## Iframe:" not in body


def test_write_navigation_dump_returns_none_when_page_text_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty `page_text` → no dump, even when other sections have data
    (those sections live in the agent-facing report only)."""
    from openclose.config.paths import ConfigPaths
    from openclose.tool.tools.browser_automation_shared import (
        write_navigation_dump,
    )
    monkeypatch.setattr(
        ConfigPaths, "project_runtime_dir",
        classmethod(lambda cls, _pd: tmp_path),
    )
    # Links and elements present but no page text → still nothing to persist.
    result = write_navigation_dump(
        ".",
        {
            "url": "https://x", "title": "T", "page_text": "",
            "links": [{"text": "L", "href": "https://y"}],
            "interactive_elements": ["button 'X'"],
            "iframes": [],
        },
    )
    assert result is None


def test_write_navigation_dump_swallows_filesystem_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path as _Path
    from openclose.tool.tools.browser_automation_shared import (
        write_navigation_dump,
    )

    def _raise(self: _Path, *a: Any, **kw: Any) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(_Path, "mkdir", _raise)
    result = write_navigation_dump(".", _make_fake_page_content())
    assert result is None


# ── passive viewer helpers (screenshot endpoint) ─────────────────────────────

class _StubFrame:
    def __init__(self, detached: bool = False) -> None:
        self._detached = detached

    def is_detached(self) -> bool:
        return self._detached


class _StubPage:
    def __init__(self, *, closed: bool = False, detached: bool = False) -> None:
        self._closed = closed
        self.main_frame = _StubFrame(detached=detached)

    def is_closed(self) -> bool:
        return self._closed


class _StubContext:
    def __init__(self, pages: list[Any]) -> None:
        self.pages = pages
        self.new_page_calls = 0

    async def new_page(self) -> Any:
        self.new_page_calls += 1
        raise AssertionError(
            "passive viewer must never call new_page()"
        )


def test_pick_existing_page_for_view_returns_none_on_empty() -> None:
    from openclose.tool.tools.browser_automation_shared import (
        _pick_existing_page_for_view,
    )

    ctx = _StubContext(pages=[])
    assert _pick_existing_page_for_view(ctx) is None
    assert ctx.new_page_calls == 0


def test_pick_existing_page_for_view_prefers_most_recent_valid() -> None:
    from openclose.tool.tools.browser_automation_shared import (
        _pick_existing_page_for_view,
    )

    oldest = _StubPage()
    middle = _StubPage()
    newest_closed = _StubPage(closed=True)
    ctx = _StubContext(pages=[oldest, middle, newest_closed])

    picked = _pick_existing_page_for_view(ctx)
    assert picked is middle
    assert ctx.new_page_calls == 0


def test_pick_existing_page_for_view_returns_newest_when_valid() -> None:
    from openclose.tool.tools.browser_automation_shared import (
        _pick_existing_page_for_view,
    )

    oldest = _StubPage()
    newest = _StubPage()
    ctx = _StubContext(pages=[oldest, newest])

    assert _pick_existing_page_for_view(ctx) is newest


def test_pick_existing_page_for_view_returns_none_when_all_invalid() -> None:
    from openclose.tool.tools.browser_automation_shared import (
        _pick_existing_page_for_view,
    )

    ctx = _StubContext(pages=[
        _StubPage(closed=True),
        _StubPage(detached=True),
    ])
    assert _pick_existing_page_for_view(ctx) is None


async def test_acquire_singleton_browser_readonly_returns_cached_singleton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openclose.tool.tools import browser_automation_shared as shared

    sentinel_pw = object()
    sentinel_browser = object()
    sentinel_context = object()
    monkeypatch.setattr(shared, "_singleton_pw", sentinel_pw)
    monkeypatch.setattr(shared, "_singleton_browser", sentinel_browser)
    monkeypatch.setattr(shared, "_singleton_context", sentinel_context)
    monkeypatch.setattr(shared, "_connection_is_alive", lambda _b: True)

    # No playwright import should fire — sentinel object would explode
    # if Playwright tried to touch it.
    result = await shared.acquire_singleton_browser_readonly()
    assert result == (sentinel_pw, sentinel_browser, sentinel_context)
