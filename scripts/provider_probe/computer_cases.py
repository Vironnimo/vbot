"""Provider Tool probe: computer cases."""

from __future__ import annotations

from typing import Any

COMPUTER_CASE_ARGUMENTS: dict[str, dict[str, Any]] = {
    **{action: {"action": action} for action in ("status", "apps", "windows", "close")},
    **{
        f"capture_{mode}": {
            "action": "capture",
            "pid": 101,
            "window_id": 202,
            **({"mode": mode} if mode != "default" else {}),
        }
        for mode in ("default", "som", "vision", "ax")
    },
    "click_preview": {
        "action": "click",
        "pid": 101,
        "window_id": 202,
        "element": "1",
        "apply": False,
    },
    "click_element": {
        "action": "click",
        "pid": 101,
        "window_id": 202,
        "element": "s00000001:1",
    },
    "click_coordinates": {
        "action": "click",
        "pid": 101,
        "window_id": 202,
        "view_id": "vtest",
        "coordinate": [20, 30],
    },
    "click_right_double": {
        "action": "click",
        "pid": 101,
        "window_id": 202,
        "view_id": "vtest",
        "coordinate": [20, 30],
        "button": "right",
        "count": 2,
        "foreground": False,
    },
    "click_middle": {
        "action": "click",
        "pid": 101,
        "window_id": 202,
        "view_id": "vtest",
        "coordinate": [20, 30],
        "button": "middle",
        "count": 1,
        "apply": False,
    },
    "type": {
        "action": "type",
        "pid": 101,
        "window_id": 202,
        "text": "test-owned draft",
    },
    "type_element": {
        "action": "type",
        "pid": 101,
        "window_id": 202,
        "element": "1",
        "text": "test-owned draft",
    },
    "key_single": {
        "action": "key",
        "pid": 101,
        "window_id": 202,
        "text": "enter",
    },
    "key_foreground": {
        "action": "key",
        "pid": 101,
        "window_id": 202,
        "text": "ctrl+a",
        "foreground": True,
    },
    **{
        f"scroll_{direction}": {
            "action": "scroll",
            "pid": 101,
            "window_id": 202,
            "direction": direction,
        }
        for direction in ("up", "down", "left", "right")
    },
    "scroll_element": {
        "action": "scroll",
        "pid": 101,
        "window_id": 202,
        "direction": "down",
        "element": "1",
        "amount": 5,
    },
    "invalid_target": {"action": "capture", "pid": 101},
    "invalid_field": {"action": "click", "pid": 101, "window_id": 202, "element": "1", "text": "x"},
}


_COMPUTER_WINDOW = {"pid": 101, "window_id": 202}


COMPUTER_CASE_ARGUMENTS.update(
    {
        "capture_original": {"action": "capture", **_COMPUTER_WINDOW, "resolution": "original"},
        "capture_query": {"action": "capture", **_COMPUTER_WINDOW, "query": "Draft", "limit": 25},
        "capture_desktop": {"action": "capture"},
        "zoom": {
            "action": "zoom",
            **_COMPUTER_WINDOW,
            "view_id": "vtest",
            "coordinate": [10, 10],
            "to_coordinate": [100, 100],
        },
        "drag": {
            "action": "drag",
            **_COMPUTER_WINDOW,
            "view_id": "vtest",
            "coordinate": [10, 10],
            "to_coordinate": [100, 100],
        },
        "set_value": {
            "action": "set_value",
            **_COMPUTER_WINDOW,
            "element": "1",
            "text": "draft",
        },
        "set_value_empty": {
            "action": "set_value",
            **_COMPUTER_WINDOW,
            "element": "1",
            "text": "",
        },
        "menu": {
            "action": "menu",
            **_COMPUTER_WINDOW,
            "menu_path": ["File", "Save"],
        },
        "resize": {
            "action": "resize",
            **_COMPUTER_WINDOW,
            "coordinate": [-100, 0],
            "size": [900, 700],
        },
        "launch_preview": {"action": "launch", "app": "Notepad", "apply": False},
        "launch": {"action": "launch", "app": "Notepad"},
        "verify_window": {
            "action": "verify",
            **_COMPUTER_WINDOW,
            "expect": [{"window": {"exists": True}}],
        },
        "verify_element": {
            "action": "verify",
            **_COMPUTER_WINDOW,
            "timeout_ms": 0,
            "mode": "ax",
            "expect": [
                {
                    "element": {
                        "selector": {"role": "Edit", "label_contains": "Draft"},
                        "exists": True,
                        "enabled": True,
                        "selected": False,
                        "value_equals": "draft",
                    }
                }
            ],
        },
        "sequence": {
            "action": "sequence",
            **_COMPUTER_WINDOW,
            "steps": [
                {"action": "click", "element": "1"},
                {"action": "key", "text": "ctrl+a"},
                {"action": "type", "text": "draft"},
            ],
        },
        "invalid_sequence": {
            "action": "sequence",
            **_COMPUTER_WINDOW,
            "steps": [{"action": "key", "text": "enter"}, {"action": "click", "element": "1"}],
        },
        # A window without any capture: coordinates cannot be measured in a screenshot.
        "invalid_view": {"action": "click", "pid": 303, "window_id": 404, "coordinate": [1, 1]},
    }
)


COMPUTER_CASE_ARGUMENTS.update(
    {
        "monitors": {"action": "monitors"},
        "capture_monitor": {"action": "capture", "monitor": 1},
        "move": {
            "action": "move",
            **_COMPUTER_WINDOW,
            "view_id": "vtest",
            "coordinate": [20, 30],
        },
        "hold_key": {
            "action": "key",
            **_COMPUTER_WINDOW,
            "text": "shift",
            "duration_ms": 100,
            "foreground": True,
        },
        "invalid_duration": {"action": "key", "text": "shift", "duration_ms": 3000},
        "wait_default": {"action": "wait"},
        "wait_explicit": {"action": "wait", "duration_ms": 0},
        "wait_window": {"action": "wait", **_COMPUTER_WINDOW, "duration_ms": 10},
        "type_no_capture": {
            "action": "type",
            "text": "draft",
            "capture_after": False,
        },
        "type_capture": {"action": "type", "text": "draft", "capture_after": True},
        **{
            f"modifier_{modifier}": {
                "action": "click",
                "coordinate": [20, 30],
                "view_id": "vtest",
                "modifiers": [modifier],
            }
            for modifier in ("ctrl", "shift", "alt", "win")
        },
        "drag_modifiers": {
            "action": "drag",
            "coordinate": [20, 30],
            "to_coordinate": [40, 50],
            "view_id": "vtest",
            "modifiers": ["ctrl", "shift"],
        },
        "scroll_modifiers": {
            "action": "scroll",
            "coordinate": [20, 30],
            "view_id": "vtest",
            "direction": "down",
            "modifiers": ["ctrl"],
        },
        "sequence_wait_no_capture": {
            "action": "sequence",
            "capture_after": False,
            "steps": [{"action": "wait", "duration_ms": 0}, {"action": "type", "text": "draft"}],
        },
        "invalid_wait": {"action": "wait", "duration_ms": 10001},
        "invalid_modifiers": {
            "action": "click",
            **_COMPUTER_WINDOW,
            "element": "1",
            "modifiers": ["ctrl"],
        },
    }
)


COMPUTER_CASE_ARGUMENTS.update(
    {
        **{
            f"background_{case}": {**COMPUTER_CASE_ARGUMENTS[case], "foreground": False}
            for case in (
                "capture_default",
                "capture_vision",
                "capture_ax",
                "click_coordinates",
                "move",
                "wait_window",
                "click_element",
            )
        },
        "background_set_value": {
            "action": "set_value",
            **_COMPUTER_WINDOW,
            "element": "1",
            "text": "draft",
            "foreground": False,
        },
        "background_drag_duration": {
            "action": "drag",
            **_COMPUTER_WINDOW,
            "view_id": "vtest",
            "coordinate": [20, 30],
            "to_coordinate": [40, 50],
            "duration_ms": 1800,
        },
        "invalid_background_desktop": {"action": "capture", "foreground": False},
        "invalid_background_hold": {
            "action": "key",
            **_COMPUTER_WINDOW,
            "text": "shift",
            "duration_ms": 100,
            "foreground": False,
        },
        "click_default": {
            "action": "click",
            **_COMPUTER_WINDOW,
            "element": "1",
        },
        "launch_default": {"action": "launch", "app": "Notepad"},
        "zoom_view_target": {
            "action": "zoom",
            "view_id": "vtest",
            "coordinate": [10, 10],
            "to_coordinate": [100, 100],
        },
        "click_view_target": {
            "action": "click",
            "view_id": "vtest",
            "coordinate": [20, 30],
        },
        "sequence_shared_view": {
            "action": "sequence",
            "view_id": "vtest",
            "steps": [
                {"action": "click", "coordinate": [10, 10]},
                {"action": "drag", "coordinate": [20, 30], "to_coordinate": [100, 100]},
            ],
        },
        "type_then_elements": {
            "action": "type",
            **_COMPUTER_WINDOW,
            "text": "draft",
            "mode": "som",
            "query": "Draft",
            "limit": 10,
        },
        "invalid_vision_query": {
            "action": "capture",
            **_COMPUTER_WINDOW,
            "mode": "vision",
            "query": "Draft",
        },
        "type_view": {"action": "type", "view_id": "vtest", "text": "draft"},
        "key_view": {"action": "key", "view_id": "vtest", "text": "enter"},
        "type_unicode": {
            "action": "type",
            **_COMPUTER_WINDOW,
            "text": "draft",
            "text_mode": "unicode",
        },
        "type_keyboard": {
            "action": "type",
            **_COMPUTER_WINDOW,
            "foreground": True,
            "text": "0.45",
            "text_mode": "keyboard",
        },
        "zoom_foreground": {
            "action": "zoom",
            "view_id": "vtest",
            "foreground": True,
            "coordinate": [10, 10],
            "to_coordinate": [100, 100],
        },
        "sequence_mixed_shared_view": {
            "action": "sequence",
            "view_id": "vtest",
            "foreground": True,
            "steps": [
                {"action": "click", "coordinate": [10, 10]},
                {"action": "key", "text": "g"},
                {"action": "type", "text": "0.45", "text_mode": "keyboard"},
                {"action": "key", "text": "enter"},
            ],
        },
        "invalid_keyboard_background": {
            "action": "type",
            "foreground": False,
            **_COMPUTER_WINDOW,
            "text": "draft",
            "text_mode": "keyboard",
        },
        "invalid_keyboard_element": {
            "action": "type",
            **_COMPUTER_WINDOW,
            "foreground": True,
            "element": "1",
            "text": "draft",
            "text_mode": "keyboard",
        },
    }
)


COMPUTER_CASE_ARGUMENTS.update(
    {
        "capture_view": {"action": "capture", "view_id": "vtest"},
        "capture_view_query": {"action": "capture", "view_id": "vtest", "query": "Brush"},
        "wait_view": {"action": "wait", "view_id": "vtest", "duration_ms": 0},
        "verify_view": {
            "action": "verify",
            "view_id": "vtest",
            "expect": [{"window": {"exists": True}}],
        },
        "element_target": {"action": "click", "element": "s00000001:1"},
        "sequence_element_target": {
            "action": "sequence",
            "steps": [{"action": "click", "element": "s00000001:1"}],
        },
        "capture_reset_background": {"action": "capture", "view_id": "vtest", "foreground": False},
    }
)
