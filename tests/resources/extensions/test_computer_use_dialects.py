"""Computer use: calls in other harnesses' vocabulary run through production dispatch."""

from __future__ import annotations

import pytest

from tests.resources.extensions.computer_use_helpers import (
    capture,
    dispatch,
    model_text,
)
from tests.resources.extensions.computer_use_helpers import (
    computer as computer,
)


def _sent(client, name):
    return [args for called, args in client.calls if called == name]


@pytest.mark.parametrize(
    "arguments,tool,expected",
    [
        # Anthropic computer tool
        ({"action": "left_click", "coordinate": [10, 20]}, "click", {"x": 10, "y": 20}),
        ({"action": "right_click", "coordinate": [10, 20]}, "click", {"button": "right"}),
        ({"action": "middle_click", "coordinate": [10, 20]}, "click", {"button": "middle"}),
        ({"action": "double_click", "coordinate": [10, 20]}, "click", {"count": 2}),
        ({"action": "triple_click", "coordinate": [10, 20]}, "click", {"count": 3}),
        ({"action": "mouse_move", "coordinate": [5, 6]}, "move_cursor", {"x": 5, "y": 6}),
        (
            {"action": "left_click_drag", "start_coordinate": [1, 2], "coordinate": [30, 40]},
            "drag",
            {"from_x": 1, "from_y": 2, "to_x": 30, "to_y": 40},
        ),
        (
            {
                "action": "scroll",
                "coordinate": [10, 20],
                "scroll_direction": "down",
                "scroll_amount": 5,
            },
            "scroll",
            {"direction": "down", "amount": 5, "x": 10},
        ),
        ({"action": "key", "text": "ctrl+s"}, "hotkey", {"keys": ["ctrl", "s"]}),
        ({"action": "key", "text": "Return"}, "press_key", {"key": "enter"}),
        ({"action": "type", "text": "draft"}, "type_text", {"text": "draft"}),
        # OpenAI computer actions
        ({"type": "click", "x": 10, "y": 20, "button": "right"}, "click", {"button": "right"}),
        ({"type": "double_click", "x": 10, "y": 20}, "click", {"count": 2, "x": 10}),
        (
            {"type": "keypress", "keys": ["CTRL", "SHIFT", "T"]},
            "hotkey",
            {"keys": ["ctrl", "shift", "t"]},
        ),
        (
            {"type": "drag", "path": [{"x": 1, "y": 2}, {"x": 30, "y": 40}]},
            "drag",
            {"from_x": 1, "to_y": 40},
        ),
        # Other shapes with one exact reading
        ({"action": "click", "x": 10.4, "y": 19.6}, "click", {"x": 10, "y": 20}),
        ({"action": "click", "coordinate": "10, 20"}, "click", {"x": 10, "y": 20}),
        ({"action": "click", "position": {"x": 10, "y": 20}}, "click", {"x": 10, "y": 20}),
        ({"action": "tap", "ref": 1}, "click", {"element_token": "s00000001:1"}),
        ({"action": "click", "element": "#1"}, "click", {"element_token": "s00000001:1"}),
        ({"action": "press", "keys": ["ctrl", "plus"]}, "hotkey", {"keys": ["ctrl", "plus"]}),
        ({"action": "key", "shortcut": "ctrl++"}, "hotkey", {"keys": ["ctrl", "plus"]}),
        (
            {"action": "key", "key": "s", "modifiers": ["control"]},
            "hotkey",
            {"keys": ["ctrl", "s"]},
        ),
        ({"action": "scroll_up", "coordinate": [10, 20]}, "scroll", {"direction": "up"}),
        (
            {"action": "click", "coordinate": [10, 20], "delivery_mode": "background"},
            "click",
            {"delivery_mode": "background"},
        ),
        (
            {"action": "click", "coordinate": [10, 20], "mouse_button": "secondary"},
            "click",
            {"button": "right"},
        ),
    ],
)
def test_harness_vocabulary_runs_the_same_gesture(computer, arguments, tool, expected):
    _, _, client, _ = computer
    capture(computer)
    result = dispatch(computer, arguments)
    assert result["ok"], result["error"]
    sent = _sent(client, tool)
    assert len(sent) == 1 and client.inputs == 1
    # The only current screenshot names the target: the captured window, not the desktop.
    assert sent[0]["pid"] == 1 and sent[0]["window_id"] == 2
    assert {key: sent[0].get(key) for key in expected} == expected


@pytest.mark.parametrize(
    "arguments,tool,expected",
    [
        (
            {"action": "left_click", "coordinate": [10, 20], "text": "shift"},
            "click",
            {"modifiers": ["shift"], "x": 10},
        ),
        (
            {"action": "hold_key", "text": "shift", "duration": 0.5},
            "press_key",
            {"key": "shift", "duration_ms": 500},
        ),
    ],
)
def test_anthropic_held_keys_run_as_foreground_input(computer, arguments, tool, expected):
    _, _, client, _ = computer
    capture(computer, foreground=True)
    result = dispatch(computer, arguments)
    assert result["ok"], result["error"]
    sent = _sent(client, tool)
    assert len(sent) == 1 and sent[0]["delivery_mode"] == "foreground"
    assert {key: sent[0].get(key) for key in expected} == expected


@pytest.mark.parametrize(
    "arguments,fragment",
    [
        ({"action": "click", "x": 10, "y": 20, "coordinate": [30, 40]}, "different values"),
        ({"action": "right_click", "coordinate": [1, 2], "button": "left"}, 'button="right"'),
        ({"action": "click", "x": 10}, '"x" needs "y" as well'),
        ({"action": "key", "text": "enter", "keys": "escape"}, "different values"),
        ({"action": "cursor_position"}, "Capture the target and use coordinates"),
        ({"action": "left_mouse_down"}, "use drag with coordinate"),
        ({"action": "focus_app", "app": "Editor"}, "click the window or its taskbar entry"),
        (
            {"type": "scroll", "x": 1, "y": 2, "scroll_x": 0, "scroll_y": 300},
            '{"action":"scroll","coordinate":[1,2],"direction":"down","amount":3}',
        ),
        ({"action": "wait", "duration": 500}, '"duration_ms": 500'),
        ({"action": "drag", "path": [[1, 2], [3, 4], [5, 6]]}, "a path of 3 points"),
        ({"action": "zoom", "region": [1, 2, 3]}, "[x1,y1,x2,y2]"),
    ],
)
def test_ambiguous_or_conflicting_spellings_fail_with_the_corrected_call(
    computer, arguments, fragment
):
    _, _, client, _ = computer
    capture(computer)
    result = dispatch(computer, arguments)
    assert result["error"]["code"] == "invalid_arguments"
    assert fragment in result["error"]["message"]
    assert result["error"]["message"].startswith("computer was not run")
    assert client.inputs == 0


def test_placeholders_for_optional_fields_are_ignored(computer):
    _, _, client, _ = computer
    result = dispatch(
        computer,
        {"action": "windows", "pid": 0, "window_id": 0, "view_id": "", "app": None, "query": ""},
    )
    assert result["ok"] and _sent(client, "list_windows")


def test_unused_fields_are_named_and_effects_that_would_be_dropped_refuse(computer):
    _, _, client, _ = computer
    capture(computer)
    typed = dispatch(computer, {"action": "type", "text": "draft", "direction": "down"})
    assert typed["ok"] and typed["data"]["note"] == "Ignored direction: type does not use it."
    assert "note: Ignored direction" in model_text(typed)
    # A coordinate on key would silently drop the click the caller asked for.
    refused = dispatch(computer, {"action": "key", "text": "enter", "coordinate": [1, 2]})
    assert refused["error"]["code"] == "invalid_arguments"
    assert "key does not use coordinate" in refused["error"]["message"]
    assert "No input was sent" in refused["error"]["message"]
    assert client.inputs == 1
    # An application name on a window action points to the window list instead of guessing.
    named = dispatch(computer, {"action": "capture", "app": "Editor"})
    assert '{"action":"windows","app":"Editor"}' in named["error"]["message"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "windows", "session": "foreign"},
        {"action": "capture", "scope": "desktop", "pid": 1, "window_id": 2},
    ],
)
def test_unknown_root_fields_fail_at_dispatch(computer, arguments):
    _, _, client, _ = computer
    result = dispatch(computer, arguments)
    assert result["error"]["code"] == "invalid_arguments"
    assert not client.calls


def test_window_list_is_one_line_per_window_with_filters(computer):
    _, _, client, _ = computer
    windows = [
        {"pid": 7, "window_id": 70, "title": "Draft - Editor", "app_name": "Editor"},
        {"pid": 7, "window_id": 71, "title": "Find", "app_name": "Editor", "minimized": True},
        {"pid": 9, "window_id": 90, "title": "Inbox", "app_name": "Mail", "is_on_screen": False},
    ]
    original = client.call
    client.call = lambda name, args: (
        {"windows": windows} if name == "list_windows" else original(name, args)
    )
    listed = dispatch(computer, {"action": "windows"})["data"]
    assert listed["count"] == 3
    assert listed["content"].splitlines() == [
        "pid 7 window_id 70: Draft - Editor (Editor)",
        "pid 7 window_id 71: Find (Editor) [minimized]",
        "pid 9 window_id 90: Inbox (Mail) [off-screen]",
    ]
    assert (
        dispatch(computer, {"action": "list_windows", "application": "mail"})["data"]["content"]
        == "pid 9 window_id 90: Inbox (Mail) [off-screen]"
    )
    assert dispatch(computer, {"action": "windows", "pid": 7})["data"]["count"] == 2
    empty = dispatch(computer, {"action": "windows", "app": "Browser"})["data"]
    assert empty["count"] == 0 and empty["note"] == (
        'Nothing matches. List all with {"action":"windows"}.'
    )
