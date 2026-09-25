"""Read the computer-use call dialects Models learn elsewhere as computer arguments.

Anthropic's computer tool, OpenAI's computer actions, OpenClaw and Hermes name
the same gestures differently: ``left_click`` with ``coordinate``,
``{"type": "double_click"}`` with ``x``/``y``, ``key`` with ``text`` or ``keys``,
``scroll_direction``, ``duration`` in seconds. Each mapping here means exactly
the same call; a spelling whose meaning would change stays unmapped for
validation to explain, and conflicting spellings fail before any input.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from core.tools._call_vocabulary import SpellingAliases, is_placeholder, spelling
from core.tools.contracts import ToolContractError

_FIELD_ALIASES = SpellingAliases(
    {
        "pid": ("pid", "process_id", "process"),
        "window_id": ("window_id", "window", "hwnd", "window_handle", "window_ref"),
        "view_id": ("view_id", "view", "observation_id", "screenshot_id", "image_id", "frame_id"),
        "coordinate": (
            "coordinate",
            "coordinates",
            "coord",
            "coords",
            "position",
            "point",
            "location",
            "xy",
        ),
        "to_coordinate": (
            "to_coordinate",
            "to_coordinates",
            "end_coordinate",
            "end_coordinates",
            "target_coordinate",
            "destination",
            "to_point",
            "end_point",
            "to_position",
            "end_position",
        ),
        "start_coordinate": (
            "start_coordinate",
            "start_coordinates",
            "from_coordinate",
            "from_coordinates",
            "start_point",
            "from_point",
            "start_position",
            "from_position",
        ),
        "element": (
            "element",
            "ref",
            "element_ref",
            "element_id",
            "element_index",
            "element_token",
        ),
        "text": ("text",),
        "shortcut": (
            "shortcut",
            "keys",
            "key",
            "hotkey",
            "key_combo",
            "key_combination",
            "keystroke",
            "keystrokes",
        ),
        "direction": ("direction", "scroll_direction"),
        "amount": ("amount", "scroll_amount"),
        "count": ("count", "click_count", "num_clicks", "number_of_clicks"),
        "button": ("button", "mouse_button"),
        "foreground": ("foreground",),
        "delivery_mode": ("delivery_mode", "delivery"),
        "mode": ("mode", "capture_mode", "observation_mode"),
        "app": ("app", "application", "app_name", "application_name"),
        "size": ("size", "window_size", "dimensions"),
        "duration_ms": ("duration_ms", "ms", "milliseconds", "duration_millis"),
        "seconds": ("seconds", "secs", "duration_s", "duration_seconds"),
        "duration": ("duration",),
        "steps": ("steps", "actions"),
        "limit": ("limit", "max_elements"),
        "menu_path": ("menu_path", "menu_labels", "menu_items"),
        "monitor": ("monitor", "display", "display_id", "monitor_id", "screen_index"),
        "modifiers": ("modifiers", "modifier", "modifier_keys", "held_keys"),
        "query": ("query",),
        "resolution": ("resolution",),
        "text_mode": ("text_mode", "typing_mode"),
        "timeout_ms": ("timeout_ms",),
        "expect": ("expect", "expectations"),
        "capture_after": ("capture_after", "screenshot_after"),
        "apply": ("apply",),
        "region": ("region",),
    }
)

# Separate pixel fields; each pair becomes one [x,y] value.
_PAIRS = (
    ("x", "y", "coordinate"),
    ("from_x", "from_y", "start_coordinate"),
    ("start_x", "start_y", "start_coordinate"),
    ("to_x", "to_y", "to_coordinate"),
    ("end_x", "end_y", "to_coordinate"),
    ("x1", "y1", "coordinate"),
    ("x2", "y2", "to_coordinate"),
    ("width", "height", "size"),
)

_ACTION_NAMES: dict[str, tuple[tuple[str, ...], dict[str, Any]]] = {
    # canonical: (aliases, implied fields)
    "capture": (("screenshot", "take_screenshot", "capture_screen", "screen_capture"), {}),
    "click": (("left_click", "tap"), {}),
    "right_click": (("right_click", "context_click"), {"button": "right"}),
    "middle_click": (("middle_click", "wheel_click"), {"button": "middle"}),
    "double_click": (("double_click", "left_double_click"), {"count": 2}),
    "triple_click": (("triple_click",), {"count": 3}),
    "move": (("mouse_move", "move_mouse", "move_cursor", "hover"), {}),
    "drag": (("left_click_drag", "drag_and_drop", "mouse_drag"), {}),
    "type": (("type_text", "write", "input_text"), {}),
    "key": (("keypress", "key_press", "press", "press_key", "hotkey", "shortcut", "keys"), {}),
    "hold_key": (("hold_key",), {}),
    "scroll_up": (("scroll_up",), {"direction": "up"}),
    "scroll_down": (("scroll_down",), {"direction": "down"}),
    "scroll_left": (("scroll_left",), {"direction": "left"}),
    "scroll_right": (("scroll_right",), {"direction": "right"}),
    "windows": (("list_windows", "get_windows"), {}),
    "apps": (("list_apps", "applications", "list_applications"), {}),
    "launch": (("launch_app", "open_app", "open_application", "start_app", "open"), {}),
    "wait": (("sleep", "pause"), {}),
    "sequence": (("batch", "computer_batch"), {}),
    "menu": (("invoke_menu", "select_menu", "menu_select"), {}),
    "resize": (("set_window_frame", "resize_window", "move_window"), {}),
    "set_value": (("fill", "set_text"), {}),
    "verify": (("verify_state",), {}),
    "monitors": (("list_monitors", "displays", "list_displays"), {}),
    "ax_capture": (("get_accessibility_tree", "accessibility_tree"), {"mode": "ax"}),
}

_CANONICAL_ACTIONS = frozenset(
    {
        "status",
        "apps",
        "windows",
        "capture",
        "zoom",
        "click",
        "type",
        "key",
        "scroll",
        "drag",
        "set_value",
        "menu",
        "launch",
        "resize",
        "verify",
        "sequence",
        "close",
        "monitors",
        "move",
        "wait",
    }
)

_ACTION_TARGETS = {
    "right_click": "click",
    "middle_click": "click",
    "double_click": "click",
    "triple_click": "click",
    "hold_key": "key",
    "scroll_up": "scroll",
    "scroll_down": "scroll",
    "scroll_left": "scroll",
    "scroll_right": "scroll",
    "ax_capture": "capture",
}

_ACTION_ALIASES: dict[str, tuple[str, dict[str, Any]]] = {}
for _canonical, (_aliases, _implied) in _ACTION_NAMES.items():
    for _alias in (_canonical, *_aliases):
        _ACTION_ALIASES[spelling(_alias)] = (_ACTION_TARGETS.get(_canonical, _canonical), _implied)
for _name in _CANONICAL_ACTIONS:
    _ACTION_ALIASES.setdefault(spelling(_name), (_name, {}))

_DESKTOP_ACTIVATION = (
    "computer cannot bring a window to the front by itself. Capture the desktop with "
    '{"action":"capture"}, click the window or its taskbar entry in that screenshot, then '
    "capture the window with foreground=true. Or keep background input: capture the window "
    "with foreground=false."
)

_UNSUPPORTED_ACTIONS = {
    spelling(name): message
    for names, message in (
        (
            ("cursor_position", "get_cursor_position", "mouse_position"),
            "computer has no cursor_position action; the pointer position is not needed. "
            "Capture the target and use coordinates measured in its screenshot.",
        ),
        (
            ("left_mouse_down", "left_mouse_up", "mouse_down", "mouse_up"),
            "computer has no separate mouse down or up actions. For press, move and release "
            "use drag with coordinate (start) and to_coordinate (end).",
        ),
        (("focus_app", "bring_to_front", "activate", "focus", "focus_window"), _DESKTOP_ACTIVATION),
    )
    for name in names
}

_BUTTONS = {
    "left": "left",
    "primary": "left",
    "right": "right",
    "secondary": "right",
    "context": "right",
    "middle": "middle",
    "wheel": "middle",
}

_MODES = {
    "vision": "vision",
    "screenshot": "vision",
    "image": "vision",
    "pixels": "vision",
    "som": "som",
    "setofmarks": "som",
    "marks": "som",
    "ax": "ax",
    "accessibility": "ax",
    "elements": "ax",
    "a11y": "ax",
}

_MODIFIERS = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "shift": "shift",
    "alt": "alt",
    "option": "alt",
    "win": "win",
    "windows": "win",
    "super": "win",
}

# Key names from xdotool, browsers and other harnesses, compared by spelling.
_KEY_NAMES = {
    **_MODIFIERS,
    "opt": "alt",
    "return": "enter",
    "enter": "enter",
    "kpenter": "enter",
    "esc": "escape",
    "escape": "escape",
    "backspace": "backspace",
    "delete": "delete",
    "del": "delete",
    "insert": "insert",
    "ins": "insert",
    "pageup": "pageup",
    "pgup": "pageup",
    "prior": "pageup",
    "pagedown": "pagedown",
    "pgdn": "pagedown",
    "home": "home",
    "end": "end",
    "up": "up",
    "arrowup": "up",
    "uparrow": "up",
    "down": "down",
    "arrowdown": "down",
    "downarrow": "down",
    "left": "left",
    "arrowleft": "left",
    "leftarrow": "left",
    "right": "right",
    "arrowright": "right",
    "rightarrow": "right",
    "tab": "tab",
    "space": "space",
    "spacebar": "space",
    "capslock": "capslock",
    "pause": "pause",
    "printscreen": "printscreen",
    "prtsc": "printscreen",
    "print": "printscreen",
    "plus": "plus",
    "minus": "minus",
    **{f"f{number}": f"f{number}" for number in range(1, 25)},
}

_INTEGER_FIELDS = (
    "pid",
    "window_id",
    "monitor",
    "amount",
    "count",
    "limit",
    "duration_ms",
    "timeout_ms",
)

_POINT_FIELDS = ("coordinate", "to_coordinate", "start_coordinate", "size")

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")

# Longest pause any action accepts; a larger "duration" cannot be seconds.
_MAX_DURATION_SECONDS = 100


def normalize_computer_arguments(arguments: Any) -> Any:
    """Return computer arguments with harness dialects rewritten; see the module doc."""
    if not isinstance(arguments, dict):
        return arguments
    if "action" not in arguments and _named(arguments, "steps") is not None:
        arguments = {**arguments, "action": "sequence"}
    normalized = _normalize_call(_named_by_type(arguments), step=None)
    steps = normalized.get("steps")
    if isinstance(steps, list):
        normalized["steps"] = [
            _normalize_call(_named_by_type(item), step=index) if isinstance(item, dict) else item
            for index, item in enumerate(steps)
        ]
    return normalized


def _named_by_type(arguments: dict[str, Any]) -> dict[str, Any]:
    """Read OpenAI's ``{"type": "click", ...}`` action spelling when action is absent."""
    kind = arguments.get("type")
    if "action" in arguments or not isinstance(kind, str) or spelling(kind) not in _ACTION_ALIASES:
        return arguments
    return {("action" if key == "type" else key): value for key, value in arguments.items()}


def _named(arguments: dict[str, Any], field: str) -> Any:
    for key, value in arguments.items():
        if _FIELD_ALIASES.get(key) == field:
            return value
    return None


def _label(step: int | None, field: str) -> str:
    return f'"{field}"' if step is None else f'"steps[{step}].{field}"'


def _conflict(step: int | None, first: str, second: str) -> ToolContractError:
    return ToolContractError(
        f"computer was not run: {_label(step, first)} and {_label(step, second)} give different "
        "values for the same thing. Send one of them."
    )


def _normalize_call(arguments: dict[str, Any], *, step: int | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    origin: dict[str, str] = {}
    action_value = arguments.get("action")
    for key, value in arguments.items():
        if key == "action":
            continue
        field = _FIELD_ALIASES.get(key, key)
        if field in result and result[field] != value:
            if _empty(value):
                continue
            if not _empty(result[field]):
                raise _conflict(step, origin[field], key)
        result[field] = value
        origin[field] = key
    action, implied = _action(action_value)
    if action is not None:
        result["action"] = action
    for field, value in implied.items():
        present = result.get(field)
        if present is not None and not _empty(present) and _same(field, present) != value:
            raise ToolContractError(
                f"computer was not run: action {json.dumps(action_value)} means "
                f"{field}={json.dumps(value)}, but {_label(step, field)} is "
                f"{json.dumps(present)}. Send one intended value."
            )
        result[field] = value
    _merge_pairs(result, step)
    _drop_placeholders(result)
    for field in _POINT_FIELDS:
        if field in result:
            result[field] = _point(result[field], size=field == "size")
    _read_named_fields(result, step)
    _read_start(result, step)
    _read_region(result, step)
    _read_duration(result, step)
    _read_scroll_distance(result, step)
    _read_delivery(result, step)
    _read_keys(result, step)
    _read_enums(result)
    for field in _INTEGER_FIELDS:
        value = result.get(field)
        if isinstance(value, float) and value.is_integer():
            result[field] = int(value)
    element = result.get("element")
    if type(element) is int:
        result["element"] = str(element)
    elif isinstance(element, str) and re.fullmatch(r"#\d+", element.strip()):
        result["element"] = element.strip()[1:]
    return result


def _empty(value: Any) -> bool:
    return value is None or value in ("", [], {})


def _same(field: str, value: Any) -> Any:
    if field == "button" and isinstance(value, str):
        return _BUTTONS.get(spelling(value), value)
    if field == "direction" and isinstance(value, str):
        return value.strip().casefold()
    return value


def _action(value: Any) -> tuple[Any, dict[str, Any]]:
    if not isinstance(value, str):
        return value, {}
    name = spelling(value)
    if name in _UNSUPPORTED_ACTIONS:
        raise ToolContractError(f"computer was not run: {_UNSUPPORTED_ACTIONS[name]}")
    action, implied = _ACTION_ALIASES.get(name, (value, {}))
    return action, dict(implied)


def _merge_pairs(result: dict[str, Any], step: int | None) -> None:
    for first, second, field in _PAIRS:
        if first not in result and second not in result:
            continue
        if first not in result or second not in result:
            present, missing = (first, second) if first in result else (second, first)
            raise ToolContractError(
                f"computer was not run: {_label(step, present)} needs {_label(step, missing)} "
                f"as well. Send both, or one {_label(step, field)} as [{first},{second}]."
            )
        pair = [result.pop(first), result.pop(second)]
        if field in result and not _empty(result[field]) and _point(result[field]) != _point(pair):
            raise _conflict(step, field, first)
        result[field] = pair


def _drop_placeholders(result: dict[str, Any]) -> None:
    for field, value in list(result.items()):
        if field in {"action", "text"}:
            continue
        unset_id = field in {"pid", "window_id", "monitor"} and type(value) is int and value == 0
        placeholder = isinstance(value, str) and is_placeholder(value)
        if unset_id or placeholder or _empty(value):
            del result[field]


def _number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return round(value)
    if isinstance(value, str) and _NUMBER.fullmatch(value.strip()):
        return round(float(value))
    return None


def _point(value: Any, *, size: bool = False) -> Any:
    """Read [x,y], {"x":..,"y":..} and "x,y" as one integer pair; else leave it."""
    items: Any = value
    if isinstance(value, dict):
        keys = {spelling(key): item for key, item in value.items()}
        names = ("width", "height") if size else ("x", "y")
        short = ("w", "h") if size else names
        if len(keys) == 2 and all(name in keys for name in names):
            items = [keys[names[0]], keys[names[1]]]
        elif len(keys) == 2 and all(name in keys for name in short):
            items = [keys[short[0]], keys[short[1]]]
        else:
            return value
    elif isinstance(value, str):
        numbers = _NUMBER.findall(value)
        stripped = re.sub(r"[\s\[\](),xX;:]|-?\d+(?:\.\d+)?", "", value)
        if len(numbers) != 2 or stripped:
            return value
        items = numbers
    if not isinstance(items, (list, tuple)) or len(items) != 2:
        return value
    pair = [_number(item) for item in items]
    return pair if all(item is not None for item in pair) else value


def _read_named_fields(result: dict[str, Any], step: int | None) -> None:
    action = result.get("action")
    # Fields whose other-harness names are too generic to alias for every action.
    generic = {
        "value": ("text", {"type", "set_value"}),
        "name": ("app", {"launch", "apps", "windows"}),
        "path": ("menu_path", {"menu"}),
        "clicks": ("count" if action == "click" else "amount", {"click", "scroll"}),
    }
    for key, (field, actions) in generic.items():
        if key not in result or action not in actions:
            continue
        value = result.pop(key)
        if field in result and not _empty(result[field]) and result[field] != value:
            raise _conflict(step, field, key)
        result[field] = value
    if action == "menu" and isinstance(result.get("menu_path"), str):
        labels = [label.strip() for label in re.split(r"\s*(?:>|->|→)\s*", result["menu_path"])]
        result["menu_path"] = labels
    if action == "drag" and isinstance(result.get("path"), list):
        path = [_point(point) for point in result.pop("path")]
        if len(path) != 2:
            raise ToolContractError(
                "computer was not run: drag moves in a straight line from coordinate to "
                f"to_coordinate, so a path of {len(path)} points cannot be followed. Send the "
                "start as coordinate and the end as to_coordinate, or several drag steps in a "
                "sequence."
            )
        for field, point in zip(("start_coordinate", "to_coordinate"), path, strict=True):
            if field in result and result[field] != point:
                raise _conflict(step, field, "path")
            result[field] = point


def _read_start(result: dict[str, Any], step: int | None) -> None:
    """Map a separate drag start; Anthropic's left_click_drag names the end coordinate."""
    if "start_coordinate" not in result:
        return
    start = result.pop("start_coordinate")
    coordinate = result.get("coordinate")
    if result.get("action") == "drag" and coordinate is not None and coordinate != start:
        end = result.get("to_coordinate")
        if end is not None and end != coordinate:
            raise ToolContractError(
                f"computer was not run: {_label(step, 'start_coordinate')}, "
                f"{_label(step, 'coordinate')} and {_label(step, 'to_coordinate')} name three "
                "different points. Send coordinate (start) and to_coordinate (end)."
            )
        result["to_coordinate"] = coordinate
    elif coordinate is not None and coordinate != start:
        raise _conflict(step, "coordinate", "start_coordinate")
    result["coordinate"] = start


def _read_region(result: dict[str, Any], step: int | None) -> None:
    if "region" not in result:
        return
    region = result.pop("region")
    numbers = [_number(item) for item in region] if isinstance(region, list) else []
    if len(numbers) != 4 or any(number is None for number in numbers):
        raise ToolContractError(
            f"computer was not run: {_label(step, 'region')} must be [x1,y1,x2,y2]. Or send "
            "coordinate (top-left) and to_coordinate (bottom-right)."
        )
    for field, point in (("coordinate", numbers[:2]), ("to_coordinate", numbers[2:])):
        if field in result and result[field] != point:
            raise _conflict(step, field, "region")
        result[field] = point


def _read_duration(result: dict[str, Any], step: int | None) -> None:
    for field in ("seconds", "duration"):
        if field not in result:
            continue
        value = result.pop(field)
        seconds = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
        if seconds is None or not math.isfinite(seconds) or seconds < 0:
            raise ToolContractError(
                f"computer was not run: {_label(step, field)} must be a number of seconds. "
                "Or send duration_ms in milliseconds."
            )
        if seconds > _MAX_DURATION_SECONDS:
            raise ToolContractError(
                f"computer was not run: {_label(step, field)} is read as seconds, and {value} "
                f"seconds is longer than any pause allows. For milliseconds send "
                f'"duration_ms": {value}.'
            )
        milliseconds = round(seconds * 1000)
        if "duration_ms" in result and result["duration_ms"] != milliseconds:
            raise _conflict(step, "duration_ms", field)
        result["duration_ms"] = milliseconds


def _read_scroll_distance(result: dict[str, Any], step: int | None) -> None:
    """Explain OpenAI's pixel scroll deltas; wheel units have no exact pixel size."""
    deltas = {field: result[field] for field in ("scroll_x", "scroll_y") if field in result}
    if not deltas:
        return
    directions = {
        field: (("left", "right") if field == "scroll_x" else ("up", "down"))[number > 0]
        for field, value in deltas.items()
        if (number := _number(value)) not in (None, 0)
    }
    corrected = {key: value for key, value in result.items() if key not in deltas}
    vertical = directions.get("scroll_y")
    if directions:
        corrected["direction"] = vertical or directions["scroll_x"]
    corrected.setdefault("amount", 3)
    call = {"action": corrected.pop("action", "scroll"), **corrected}
    names = " and ".join(_label(step, field) for field in deltas)
    message = (
        f"computer was not run: {names} {'give' if len(deltas) > 1 else 'gives'} a distance in "
        "pixels, but computer scrolls in mouse-wheel units (amount 1 to 100; 3 is a few lines). "
        f"Send direction and amount instead, for example {json.dumps(call, separators=(',', ':'))}."
    )
    if vertical and "scroll_x" in directions:
        horizontal = directions["scroll_x"]
        message += f' Scroll the horizontal part in a second call with direction "{horizontal}".'
    raise ToolContractError(message)


def _read_delivery(result: dict[str, Any], step: int | None) -> None:
    if "delivery_mode" not in result:
        return
    value = result.pop("delivery_mode")
    choice = {"foreground": True, "background": False}.get(
        spelling(value) if isinstance(value, str) else ""
    )
    if choice is None:
        raise ToolContractError(
            f"computer was not run: {_label(step, 'delivery_mode')} must be foreground or "
            "background. computer also accepts foreground=true or false."
        )
    if "foreground" in result and result["foreground"] is not choice:
        raise _conflict(step, "foreground", "delivery_mode")
    result["foreground"] = choice


def _read_keys(result: dict[str, Any], step: int | None) -> None:
    action = result.get("action")
    if action == "key" and "text" in result:
        text = result.pop("text")
        if "shortcut" in result and not _empty(result["shortcut"]) and result["shortcut"] != text:
            raise _conflict(step, "text", "shortcut")
        result["shortcut"] = text
    if action in {"click", "scroll", "drag"} and isinstance(result.get("text"), str):
        names = [part for part in re.split(r"[\s+,]+", result["text"]) if part]
        held = [_MODIFIERS.get(spelling(name)) for name in names]
        if names and all(held):
            # Anthropic's click actions hold the modifier keys named in text.
            del result["text"]
            existing = result.get("modifiers")
            merged = list(existing) if isinstance(existing, list) else []
            result["modifiers"] = list(dict.fromkeys([*merged, *held]))
    shortcut = result.get("shortcut")
    if isinstance(shortcut, list) and all(isinstance(key, str) for key in shortcut):
        shortcut = "+".join(key.strip() for key in shortcut)
    if isinstance(shortcut, str):
        shortcut = _key_combination(shortcut)
        modifiers = result.get("modifiers")
        if (
            action == "key"
            and isinstance(modifiers, list)
            and all(isinstance(item, str) for item in modifiers)
        ):
            holding = [
                _MODIFIERS.get(spelling(item), item.strip().casefold()) for item in modifiers
            ]
            pressed = shortcut.split("+")
            shortcut = "+".join(dict.fromkeys([*holding, *pressed]))
            del result["modifiers"]
        result["shortcut"] = shortcut


def _key_combination(value: str) -> str:
    text = value.strip()
    if text == "+":
        return "plus"
    if text.endswith("++"):
        text = text[:-2] + "+plus"
    parts = [part.strip() for part in text.split("+")]
    if any(not part for part in parts):
        return value
    names = []
    for part in parts:
        name = spelling(part)
        if not name:
            names.append(part)
        elif name in _KEY_NAMES:
            names.append(_KEY_NAMES[name])
        else:
            names.append(name if len(part) > 1 else part.casefold())
    return "+".join(names)


def _read_enums(result: dict[str, Any]) -> None:
    button = result.get("button")
    if isinstance(button, str) and spelling(button) in _BUTTONS:
        result["button"] = _BUTTONS[spelling(button)]
    mode = result.get("mode")
    if isinstance(mode, str) and spelling(mode) in _MODES:
        result["mode"] = _MODES[spelling(mode)]
    direction = result.get("direction")
    if isinstance(direction, str) and direction.strip().casefold() in {
        "up",
        "down",
        "left",
        "right",
    }:
        result["direction"] = direction.strip().casefold()
    modifiers = result.get("modifiers")
    if isinstance(modifiers, str):
        modifiers = [part for part in re.split(r"[\s+,]+", modifiers) if part]
    if isinstance(modifiers, list):
        result["modifiers"] = [
            _MODIFIERS.get(spelling(item), item) if isinstance(item, str) else item
            for item in modifiers
        ]


__all__ = ["normalize_computer_arguments"]
