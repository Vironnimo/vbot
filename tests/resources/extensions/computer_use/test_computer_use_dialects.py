"""Calls written in other harnesses' dialects: repaired when unambiguous, else refused."""

from __future__ import annotations

import json

import pytest

from tests.resources.extensions.computer_use.computer_use_test_support import Harness

pytestmark = pytest.mark.asyncio

POINT = [200, 150]  # inside Notepad on the unscaled primary display
CLICK: tuple = ("click", 200, 150, "left", 1, [])


@pytest.mark.parametrize(
    ("tool", "arguments", "inputs"),
    [
        ("computer", {"action": "click", "coordinate": "200, 150"}, [CLICK]),
        ("computer", {"action": "Left-Click", "coordinate": {"x": 200.4, "y": 149.6}}, [CLICK]),
        (
            "computer",
            {"type": "click", "x": 200, "y": 150, "button": "right"},
            [("click", 200, 150, "right", 1, [])],
        ),
        (
            "computer",
            {"action": "click", "coordinates": POINT, "clicks": 3},
            [("click", 200, 150, "left", 3, [])],
        ),
        (
            "computer",
            {"action": "double_click", "x": "200", "y": "150"},
            [("click", 200, 150, "left", 2, [])],
        ),
        ("computer", {"type": "keypress", "keys": ["CTRL", "S"]}, [("keys", ["ctrl", "s"], 1)]),
        (
            "computer",
            {"action": "hotkey", "key": "ctrl+s", "count": 2},
            [("keys", ["ctrl", "s"], 2)],
        ),
        (
            "computer",
            {"action": "key", "text": "s", "modifiers": ["ctrl"]},
            [("keys", ["ctrl", "s"], 1)],
        ),
        (
            "computer",
            {"action": "left_click", "coordinate": POINT, "modifiers": "shift"},
            [("click", 200, 150, "left", 1, ["shift"])],
        ),
        (
            "computer",
            {"action": "scroll_down", "coordinate": POINT, "amount": 2},
            [("scroll", 200, 150, "down", 2, [])],
        ),
        (
            "computer",
            {"action": "scroll", "position": POINT, "direction": "up"},
            [("scroll", 200, 150, "up", 3, [])],
        ),
        (
            "computer",
            {"type": "drag", "path": [{"x": 200, "y": 150}, {"x": 300, "y": 250}]},
            [("drag", (200, 150), (300, 250), [])],
        ),
        (
            "computer",
            {"action": "hold_key", "text": "shift", "duration_ms": 500},
            [("hold", ["shift"], 0.5)],
        ),
        ("computer", json.dumps({"action": "type", "text": "hi"}), [("type", "hi")]),
        (
            "computer",
            {"action": "type", "text": "hi", "coordinate": None, "scroll_amount": ""},
            [("type", "hi")],
        ),
        (
            "computer_batch",
            [{"type": "click", "x": 200, "y": 150}, {"type": "type", "text": "hi"}],
            [CLICK, ("type", "hi")],
        ),
        (
            "computer_batch",
            {"steps": json.dumps([{"action": "type", "text": "hi"}])},
            [("type", "hi")],
        ),
        ("computer_batch", {"action": "type", "text": "hi"}, [("type", "hi")]),
    ],
)
async def test_unambiguous_dialects_run_as_the_canonical_call(
    computer: Harness, tool: str, arguments, inputs: list
) -> None:
    computer.target.inputs.clear()
    result = await computer.call(tool, arguments)
    assert result["ok"], result
    assert computer.target.inputs == inputs


@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        ("computer", {"type": "scroll", "x": 200, "y": 150, "scroll_y": 300}, '"scroll_direction"'),
        ("computer", {"action": "type", "key": "enter"}, '{"action":"key","text":"enter"}'),
        ("computer", {"action": "click", "coordinate": POINT, "button": "back"}, "alt+left"),
        (
            "computer",
            {"action": "double_click", "coordinate": POINT, "button": "right"},
            "does not fit",
        ),
        (
            "computer",
            {"action": "click", "x": 200, "y": 150, "coordinate": [10, 10]},
            "x and y say",
        ),
        (
            "computer",
            {"action": "mouse_move", "coordinate": POINT, "modifiers": "shift"},
            "cannot hold",
        ),
        ("computer", {"actions": [{"action": "screenshot"}]}, "computer_batch"),
        (
            "computer",
            {"action": "scroll_up", "coordinate": POINT, "scroll_direction": "down"},
            "scrolls up",
        ),
        (
            "computer_batch",
            {"actions": [{"action": "screenshot"}, {"action": "type", "key": "a"}]},
            "action 2",
        ),
        (
            "computer_apps",
            {"action": "request", "applications": "Notepad", "apps": ["Paint"]},
            "Conflicting",
        ),
    ],
)
async def test_ambiguous_dialects_are_refused_with_the_corrected_call(
    computer: Harness, tool: str, arguments, message: str
) -> None:
    computer.target.inputs.clear()
    result = await computer.call(tool, arguments)
    assert result["error"]["code"] == "invalid_arguments"
    assert message in result["error"]["message"]
    assert computer.target.inputs == []


@pytest.mark.parametrize(
    ("arguments", "opened"),
    [
        ({"action": "launch", "name": "Notepad"}, ["Notepad"]),
        ({"action": "open", "apps": ["notepad"]}, ["Notepad"]),
    ],
)
async def test_apps_calls_accept_common_spellings(
    computer: Harness, arguments: dict, opened: list[str]
) -> None:
    result = await computer.call("computer_apps", arguments)
    assert result["ok"], result
    assert computer.target.opened == opened
