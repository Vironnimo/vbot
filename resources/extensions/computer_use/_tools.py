"""Agent-facing definitions of the Computer Use Tools and their dialect repair.

The vocabulary is Anthropic's computer tool and Claude Code's computer-use
server, which current Models are trained on. The normalizers turn other common
dialects (OpenAI's computer actions, pyautogui-style fields, string or object
coordinates) into that vocabulary when the intended effect is unambiguous, and
refuse a call whose intent is unclear before anything runs, with the corrected
call. Per-action rules that need the screen (frame bounds, access) belong to
the handler.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from typing import Any

from core.tools.call_syntax import SpellingAliases, is_placeholder, normalize_call_arguments
from core.tools.contracts import ToolContract, ToolContractError, compile_tool_contract

ACTIONS = (
    "screenshot",
    "zoom",
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "mouse_move",
    "left_click_drag",
    "left_mouse_down",
    "left_mouse_up",
    "scroll",
    "type",
    "key",
    "hold_key",
    "wait",
    "cursor_position",
)
SCROLL_DIRECTIONS = ("up", "down", "left", "right")
APPS_ACTIONS = ("list", "request", "open")
MAX_BATCH_ACTIONS = 30

COMPUTER_DESCRIPTION = (
    "Operate the desktop of the computer the vBot server runs on (Windows) with "
    "screenshots, mouse and keyboard. First get access with computer_apps: windows of "
    "apps not granted in this Session appear as gray boxes, and input into them is "
    "refused. Coordinates are pixels [x, y] in the latest screenshot of the current "
    "display; every result states that frame's size. Input actions return a new "
    "screenshot about half a second later. Use zoom to read small text, and "
    "computer_batch for several predictable steps. This is the user's real mouse and "
    "keyboard: the user can stop you with the Stop button or by pressing Esc twice."
)

COMPUTER_BATCH_DESCRIPTION = (
    "Run several computer actions in one call when you can predict the steps, such as "
    "clicking a field, typing and pressing enter. Actions run in order; the batch stops "
    "at the first one that fails, and the result says which steps ran. Each action takes "
    "the same fields as computer, except display. All coordinates refer to the "
    "screenshot taken before this call, even after a screenshot inside the batch. "
    "Screenshot and zoom actions return their images in order, and a batch that sent "
    "input ends with a fresh screenshot."
)

COMPUTER_APPS_DESCRIPTION = (
    "Get access to apps on the server's desktop for computer, and bring them to the "
    "front. The user grants each app for the current Session; computer sees and operates "
    "only granted apps. list shows the granted apps with their access, the displays and "
    "the running apps; with query it also searches the installed apps. request asks the "
    "user and waits for the answer: browsers are granted view only, terminals and code "
    "editors click only (clicks and scrolling), all other apps full control. Grants end "
    "30 minutes after the last Computer Use call in the Session. open brings a granted "
    "app to the front, starting it if needed, and returns a screenshot."
)


def _point_schema(description: str) -> dict[str, Any]:
    return {
        "type": "array",
        "items": {"type": "integer"},
        "minItems": 2,
        "maxItems": 2,
        "description": description,
    }


_ACTION_PROPERTIES: dict[str, Any] = {
    "action": {"type": "string", "enum": list(ACTIONS), "description": "The action to perform."},
    "coordinate": _point_schema(
        "[x, y] in the latest screenshot: where to click, move, scroll or end a drag. "
        "Omit on a click or scroll to use the pointer's position."
    ),
    "start_coordinate": _point_schema(
        "[x, y] where left_click_drag starts. Omit to start at the pointer's position."
    ),
    "text": {
        "type": "string",
        "description": (
            "type: the text to type; \\n presses Enter. key and hold_key: a key or chord "
            "such as enter, ctrl+s or ctrl+shift+t; several chords separated by spaces are "
            "pressed in order. Clicks, scroll and drags: modifier keys to hold, such as "
            "shift or ctrl+shift."
        ),
    },
    "scroll_direction": {
        "type": "string",
        "enum": list(SCROLL_DIRECTIONS),
        "description": "Direction to scroll.",
    },
    "scroll_amount": {
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
        "description": "Number of wheel ticks to scroll (default 3).",
    },
    "repeat": {
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
        "description": "key: how many times to press the keys (default 1).",
    },
    "duration": {
        "type": "number",
        "description": (
            "Seconds, 0-100: how long hold_key holds the keys, or how long wait waits (default 1)."
        ),
    },
    "region": {
        "type": "array",
        "items": {"type": "integer"},
        "minItems": 4,
        "maxItems": 4,
        "description": (
            "zoom: [x0, y0, x1, y1], the rectangle of the latest screenshot to show at "
            "full resolution."
        ),
    },
    "scale": {
        "type": "number",
        "minimum": 0.1,
        "maximum": 1,
        "description": (
            "screenshot and zoom: return a smaller image, 0.1-1 of the full size, to save "
            "context. Coordinates stay in the full frame."
        ),
    },
    "action_summary": {
        "type": "string",
        "description": (
            'A few words saying what the action does, shown to the user, such as "Opens '
            'the File menu". Set it on every input action; never include secrets.'
        ),
    },
}

_DISPLAY_PROPERTY = {
    "type": "string",
    "description": (
        "screenshot: the display to show from now on, by name or number (1, 2, ...), or "
        "auto for the display of the foreground app. Omit to keep the current display."
    ),
}

COMPUTER_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {**_ACTION_PROPERTIES, "display": _DISPLAY_PROPERTY},
    "required": ["action"],
    "additionalProperties": False,
}

_BATCH_ITEM: dict[str, Any] = {
    "type": "object",
    "properties": _ACTION_PROPERTIES,
    "required": ["action"],
    "additionalProperties": False,
}

COMPUTER_BATCH_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actions": {
            "type": "array",
            "items": _BATCH_ITEM,
            "minItems": 1,
            "maxItems": MAX_BATCH_ACTIONS,
            "description": (
                f"The actions to run, 1-{MAX_BATCH_ACTIONS}. Example: "
                '[{"action":"left_click","coordinate":[420,310],"action_summary":"Focuses '
                'the search box"},{"action":"type","text":"invoice"},{"action":"key",'
                '"text":"enter"}]'
            ),
        },
    },
    "required": ["actions"],
    "additionalProperties": False,
}

COMPUTER_APPS_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(APPS_ACTIONS), "description": "What to do."},
        "apps": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "request: app names as shown in the Start menu, such as Notepad or "
                "LibreOffice Calc."
            ),
        },
        "app": {"type": "string", "description": "open: the name of a granted app."},
        "reason": {
            "type": "string",
            "description": (
                "request: one sentence for the user about the task you need the apps for."
            ),
        },
        "query": {
            "type": "string",
            "description": "list: also show installed apps whose names contain this text.",
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}

RESULT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["content"],
    "properties": {"content": {"type": "string"}},
    "additionalProperties": False,
}

# Field names of other harnesses; matched without case, spaces and punctuation.
_FIELD_ALIASES = SpellingAliases(
    {
        "coordinate": (
            "coordinates",
            "position",
            "point",
            "pos",
            "xy",
            "location",
            "end_coordinate",
            "to",
            "end",
            "end_point",
            "target",
            "to_coordinate",
        ),
        "start_coordinate": (
            "start",
            "from",
            "start_position",
            "start_point",
            "from_coordinate",
            "drag_start",
        ),
        "region": ("rect", "rectangle", "area", "box", "bbox", "bounds", "zoom_region"),
        "text": (
            "key",
            "keys",
            "combo",
            "shortcut",
            "chord",
            "hotkey",
            "value",
            "content",
            "string",
            "input",
        ),
        "scroll_direction": ("direction", "scroll_dir"),
        "scroll_amount": ("amount", "ticks", "scroll_ticks", "wheel_ticks", "notches"),
        "duration": ("seconds", "secs", "duration_s", "duration_seconds"),
        "repeat": ("times", "presses", "repeat_count", "repetitions"),
        "action_summary": ("summary", "action_description"),
        "display": ("monitor", "screen", "display_name", "display_id", "display_number"),
    }
)
_KEY_FIELDS = frozenset({"key", "keys", "combo", "shortcut", "chord", "hotkey"})
_MODIFIER_FIELDS = ("modifiers", "modifier", "held_keys", "hold_keys")
_COUNT_FIELDS = ("clicks", "click_count", "num_clicks", "n_clicks", "count")
_MILLISECOND_FIELDS = ("duration_ms", "ms", "milliseconds", "time_ms", "wait_ms")
_PIXEL_SCROLL_FIELDS = ("scroll_x", "scroll_y", "delta_x", "delta_y", "dx", "dy")

# Action names of other harnesses; matched without case, spaces and punctuation.
_ACTION_ALIASES = {
    **dict.fromkeys(("click", "lclick", "leftclick"), "left_click"),
    **dict.fromkeys(("rclick", "contextclick"), "right_click"),
    **dict.fromkeys(("mclick", "wheelclick"), "middle_click"),
    **dict.fromkeys(("dblclick", "dclick", "doubleclick"), "double_click"),
    "tripleclick": "triple_click",
    **dict.fromkeys(("move", "movemouse", "hover", "moveto", "movecursor"), "mouse_move"),
    **dict.fromkeys(("drag", "leftdrag", "draganddrop", "dragto"), "left_click_drag"),
    **dict.fromkeys(("mousedown", "leftdown", "pressmouse"), "left_mouse_down"),
    **dict.fromkeys(("mouseup", "leftup", "releasemouse"), "left_mouse_up"),
    **dict.fromkeys(
        ("keypress", "presskey", "press", "hotkey", "keys", "keycombo", "shortcut"), "key"
    ),
    **dict.fromkeys(("typetext", "write", "inputtext", "entertext"), "type"),
    **dict.fromkeys(("hold", "keyhold", "holdkeys"), "hold_key"),
    **dict.fromkeys(("sleep", "pause", "delay"), "wait"),
    **dict.fromkeys(("takescreenshot", "capture", "screencapture", "snapshot"), "screenshot"),
    **dict.fromkeys(("zoomin", "inspect"), "zoom"),
    **dict.fromkeys(
        ("getcursorposition", "mouseposition", "getmouseposition", "cursor"), "cursor_position"
    ),
}
_SCROLL_ACTIONS = {f"scroll{direction}": direction for direction in SCROLL_DIRECTIONS}
_BUTTONS = {
    **dict.fromkeys(("left", "primary", "l", "1"), "left"),
    **dict.fromkeys(("right", "secondary", "r", "2"), "right"),
    **dict.fromkeys(("middle", "wheel", "m", "3"), "middle"),
}
_CLICK_COUNTS = {
    "left_click": 1,
    "right_click": 1,
    "middle_click": 1,
    "double_click": 2,
    "triple_click": 3,
}
_LEFT_CLICK_BY_COUNT = {1: "left_click", 2: "double_click", 3: "triple_click"}
_CLICK_BY_BUTTON = {"left": "left_click", "right": "right_click", "middle": "middle_click"}
_KEYBOARD_ACTIONS = frozenset({"key", "hold_key"})
_MODIFIER_ACTIONS = frozenset({*_CLICK_COUNTS, "scroll", "left_click_drag"})
_READ_ACTIONS = frozenset({"screenshot", "zoom", "wait", "cursor_position"})
_OPTIONAL = (
    "coordinate",
    "start_coordinate",
    "text",
    "scroll_direction",
    "scroll_amount",
    "repeat",
    "duration",
    "region",
    "scale",
    "display",
    "action_summary",
)


def _spelled(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold())


def _pop(arguments: dict[str, Any], names: tuple[str, ...]) -> tuple[str, Any] | None:
    """Remove and return the first field spelled like one of *names*."""
    wanted = {_spelled(name) for name in names}
    for key in tuple(arguments):
        if _spelled(key) in wanted:
            return key, arguments.pop(key)
    return None


def _number(value: Any) -> int | None:
    """Return a coordinate number as a whole pixel, or ``None``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return round(value)
    return None


def _numbers(value: Any, count: int) -> list[int] | None:
    """Read *count* pixel numbers from a list, ``"x,y"`` text or an object."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = [part for part in re.split(r"[\s,;()\[\]]+", value) if part]
    if isinstance(value, dict):
        fields = {_spelled(str(key)): item for key, item in value.items()}
        corners = {
            2: (("x", "y"),),
            4: (
                ("x0", "y0", "x1", "y1"),
                ("left", "top", "right", "bottom"),
                ("x1", "y1", "x2", "y2"),
            ),
        }[count]
        names = next((names for names in corners if all(name in fields for name in names)), None)
        if names is not None:
            value = [fields[name] for name in names]
        elif count == 4 and all(name in fields for name in ("x", "y", "width", "height")):
            box = [_number(fields[name]) for name in ("x", "y", "width", "height")]
            if any(number is None for number in box):
                return None
            x, y, width, height = (number or 0 for number in box)
            return [x, y, x + width, y + height]
        else:
            return None
    if not isinstance(value, list):
        return None
    if count == 4 and len(value) == 2:
        first, second = _numbers(value[0], 2), _numbers(value[1], 2)
        return [*first, *second] if first is not None and second is not None else None
    numbers = [_number(item) for item in value]
    if len(numbers) != count or any(number is None for number in numbers):
        return None
    return [number or 0 for number in numbers]


def _point(value: Any) -> Any:
    """``"x,y"``, ``"(x, y)"``, ``{"x": .., "y": ..}`` and fractional pairs as ``[x, y]``."""
    return _numbers(value, 2) or value if value not in (None, "", []) else None


def _region(value: Any) -> Any:
    return _numbers(value, 4) or value if value not in (None, "", []) else None


def _display(value: Any) -> Any:
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


def _chord_text(value: Any) -> Any:
    """A list of key names (``["CTRL", "S"]``) as one chord ``CTRL+S``."""
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return "+".join(item.strip() for item in value)
    return value


def _refuse(tool: str, message: str) -> ToolContractError:
    return ToolContractError(f"{tool} was not run: {message}")


def _action_name(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    spelled = _spelled(value)
    return next(
        (action for action in ACTIONS if _spelled(action) == spelled),
        _ACTION_ALIASES.get(spelled),
    )


def _argument_object(arguments: Any) -> Any:
    if isinstance(arguments, str):
        try:
            return json.loads(arguments)
        except ValueError as error:
            raise ToolContractError("Provide one JSON argument object.") from error
    return arguments


def _lift_action_type(arguments: dict[str, Any], tool: str) -> None:
    """OpenAI's computer actions name the action in ``type``."""
    found = _pop(arguments, ("type",))
    if found is None:
        return
    _, kind = found
    action = next((key for key in arguments if _spelled(key) == "action"), None)
    if action is None:
        arguments["action"] = kind
        return
    if _spelled(str(kind)) == _spelled(str(arguments[action])):
        return
    if _action_name(arguments[action]) == "type":
        raise _refuse(tool, 'send the text to type in "text": {"action":"type","text":"..."}.')
    raise _refuse(
        tool,
        f'"type" is {json.dumps(kind)} but "action" is {json.dumps(arguments[action])}. Send '
        "only action, with one of: " + ", ".join(ACTIONS) + ".",
    )


def _normalized_action(contract: ToolContract, arguments: Any, tool: str) -> Any:
    arguments = _argument_object(arguments)
    if not isinstance(arguments, dict):
        return arguments
    arguments = dict(arguments)
    _lift_action_type(arguments, tool)
    raw_action = next(
        (value for key, value in arguments.items() if _spelled(key) == "action"), None
    )
    if _action_name(raw_action) == "type" and any(
        _spelled(key) in _KEY_FIELDS for key in arguments
    ):
        raise _refuse(
            tool,
            'type types text. To press keys, send {"action":"key","text":"enter"} or a chord '
            'such as {"action":"key","text":"ctrl+s"}.',
        )
    modifiers = _pop(arguments, _MODIFIER_FIELDS)
    repaired = normalize_call_arguments(
        contract,
        arguments,
        enum_fields=("action", "scroll_direction"),
        field_aliases=_FIELD_ALIASES,
        field_normalizers=_FIELD_NORMALIZERS,
        empty_as_omitted=_OPTIONAL,
    )
    if not isinstance(repaired, dict):
        return repaired
    _repair_action(repaired, tool)
    if isinstance(repaired.get("action"), str) and repaired["action"] in ACTIONS:
        _repair_fields(repaired, tool)
        if modifiers is not None:
            _merge_modifiers(repaired, _chord_text(modifiers[1]), tool)
    return repaired


def _repair_action(arguments: dict[str, Any], tool: str) -> None:
    raw = arguments.get("action")
    if isinstance(raw, str) and _spelled(raw) in _SCROLL_ACTIONS:
        direction = _SCROLL_ACTIONS[_spelled(raw)]
        given = arguments.get("scroll_direction")
        if given not in (None, direction):
            raise _refuse(
                tool,
                f'action "{raw}" scrolls {direction}, but scroll_direction is "{given}". Send '
                f'{{"action":"scroll","scroll_direction":"{given}"}} or omit scroll_direction.',
            )
        arguments["scroll_direction"] = direction
        arguments["action"] = "scroll"
        return
    action = _action_name(raw)
    if action is not None:
        arguments["action"] = action


def _repair_fields(arguments: dict[str, Any], tool: str) -> None:
    """Fields of other harnesses whose meaning is unambiguous for this action."""
    action = arguments["action"]
    x, y = _pop(arguments, ("x",)), _pop(arguments, ("y",))
    if x is not None or y is not None:
        point = [_number(x[1] if x else None), _number(y[1] if y else None)]
        if None in point:
            raise _refuse(tool, 'send the position as "coordinate": [x, y].')
        if arguments.get("coordinate") not in (None, point):
            raise _refuse(
                tool, f"x and y say {point}, but coordinate says {arguments['coordinate']}."
            )
        arguments["coordinate"] = point
    path = _pop(arguments, ("path", "points"))
    if path is not None:
        points = [_numbers(item, 2) for item in path[1]] if isinstance(path[1], list) else []
        if action != "left_click_drag" or len(points) != 2 or None in points:
            raise _refuse(
                tool,
                "a drag takes a start and an end point: send "
                '{"action":"left_click_drag","start_coordinate":[x0, y0],"coordinate":[x1, y1]}.',
            )
        arguments.setdefault("start_coordinate", points[0])
        arguments.setdefault("coordinate", points[1])
    if _pop(arguments, _PIXEL_SCROLL_FIELDS) is not None:
        raise _refuse(
            tool,
            'scroll takes "scroll_direction" (up, down, left or right) and "scroll_amount" in '
            'wheel ticks, for example {"action":"scroll","coordinate":[x, y],'
            '"scroll_direction":"down","scroll_amount":3}.',
        )
    _repair_button(arguments, tool)
    _repair_count(arguments, tool)
    _repair_milliseconds(arguments, tool)
    action = arguments["action"]
    text = arguments.get("text")
    if isinstance(text, list) and (action in _KEYBOARD_ACTIONS or action in _MODIFIER_ACTIONS):
        arguments["text"] = _chord_text(text)
    elif isinstance(text, list) and len(text) == 1:
        arguments["text"] = text[0]
    if action in _MODIFIER_ACTIONS and is_placeholder(arguments.get("text")):
        arguments.pop("text", None)


def _repair_button(arguments: dict[str, Any], tool: str) -> None:
    found = _pop(arguments, ("button", "mouse_button"))
    if found is None or found[1] is None:
        return
    raw = found[1]
    button = _BUTTONS.get(_spelled(str(raw)))
    action = arguments["action"]
    if button == "left" and action not in {"right_click", "middle_click"}:
        return
    if button is not None and action in {"left_click", _CLICK_BY_BUTTON[button]}:
        arguments["action"] = _CLICK_BY_BUTTON[button]
        return
    raise _refuse(
        tool,
        f'"button": {json.dumps(raw)} does not fit action "{action}". Use left_click, '
        "right_click or middle_click without button. The mouse back and forward buttons "
        "are not available; press alt+left or alt+right with the key action instead.",
    )


def _repair_count(arguments: dict[str, Any], tool: str) -> None:
    found = _pop(arguments, _COUNT_FIELDS)
    if found is None:
        return
    name, value = found
    count = _number(value)
    action = arguments["action"]
    if action == "key":
        if arguments.get("repeat") not in (None, count):
            raise _refuse(tool, f"{name} and repeat disagree; send only repeat.")
        arguments["repeat"] = value
    elif action == "left_click" and count in _LEFT_CLICK_BY_COUNT:
        arguments["action"] = _LEFT_CLICK_BY_COUNT[count]
    elif count != _CLICK_COUNTS.get(action):
        raise _refuse(
            tool,
            f"{name}={json.dumps(value)} does not fit {action}. For one, two or three left "
            "clicks use left_click, double_click or triple_click; scroll takes "
            '"scroll_direction" and "scroll_amount".',
        )


def _repair_milliseconds(arguments: dict[str, Any], tool: str) -> None:
    found = _pop(arguments, _MILLISECOND_FIELDS)
    if found is None:
        return
    name, value = found
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _refuse(tool, f"{name} must be a number of milliseconds.")
    seconds = value / 1000
    if arguments.get("duration") not in (None, seconds):
        raise _refuse(
            tool,
            f"{name}={value} and duration={arguments['duration']} disagree. Send only "
            "duration, in seconds.",
        )
    arguments["duration"] = seconds


def _merge_modifiers(arguments: dict[str, Any], modifiers: Any, tool: str) -> None:
    """Fold a separate modifier list into ``text``, where computer expects it."""
    if modifiers in (None, "", []):
        return
    if not isinstance(modifiers, str):
        raise _refuse(tool, 'name modifier keys in "text", such as "ctrl+shift".')
    action = arguments["action"]
    text = arguments.get("text")
    if action in _KEYBOARD_ACTIONS:
        arguments["text"] = f"{modifiers}+{text}" if isinstance(text, str) and text else modifiers
    elif action in _MODIFIER_ACTIONS:
        if text not in (None, "") and _spelled(str(text)) != _spelled(modifiers):
            raise _refuse(
                tool,
                f'text "{text}" and modifiers "{modifiers}" disagree. On {action}, text names '
                "the modifier keys to hold; send them only there.",
            )
        arguments["text"] = modifiers
    elif action not in _READ_ACTIONS:
        raise _refuse(
            tool,
            f"{action} cannot hold modifier keys. Press a chord with "
            '{"action":"key","text":"ctrl+s"}, or hold keys while clicking with '
            '{"action":"left_click","coordinate":[x, y],"text":"shift"}.',
        )


def _contract(name: str, schema: dict[str, Any]) -> ToolContract:
    return compile_tool_contract(name=name, input_schema=schema, require_closed_input=False)


_COMPUTER_CONTRACT = _contract("computer", COMPUTER_PARAMETERS)
_ITEM_CONTRACT = _contract("computer_batch", _BATCH_ITEM)
_BATCH_CONTRACT = _contract("computer_batch", COMPUTER_BATCH_PARAMETERS)
_APPS_CONTRACT = _contract("computer_apps", COMPUTER_APPS_PARAMETERS)

_FIELD_NORMALIZERS: Mapping[str, Callable[[Any], Any]] = {
    "coordinate": _point,
    "start_coordinate": _point,
    "region": _region,
    "display": _display,
}


def normalize_computer(arguments: Any) -> Any:
    """Repair one ``computer`` call written in another harness's dialect."""
    arguments = _argument_object(arguments)
    if isinstance(arguments, dict) and "actions" in arguments and "action" not in arguments:
        raise _refuse(
            "computer",
            'computer runs one action, such as {"action":"screenshot"}. Run several '
            'actions with computer_batch {"actions":[...]}.',
        )
    return _normalized_action(_COMPUTER_CONTRACT, arguments, "computer")


def normalize_batch(arguments: Any) -> Any:
    """Repair a ``computer_batch`` call and each of its actions separately."""
    arguments = _argument_object(arguments)
    if isinstance(arguments, list):
        arguments = {"actions": arguments}
    elif isinstance(arguments, dict) and "action" in arguments and "actions" not in arguments:
        # One action sent on its own is a batch of one.
        arguments = {"actions": [arguments]}
    repaired = normalize_call_arguments(
        _BATCH_CONTRACT,
        arguments,
        field_aliases=SpellingAliases({"actions": ("steps", "commands", "sequence", "batch")}),
    )
    if not isinstance(repaired, dict) or not isinstance(repaired.get("actions"), list):
        return repaired
    items = []
    for number, item in enumerate(repaired["actions"], start=1):
        try:
            items.append(_normalized_action(_ITEM_CONTRACT, item, "computer_batch"))
        except ToolContractError as error:
            message = str(error).removeprefix("computer_batch was not run: ")
            raise _refuse("computer_batch", f"action {number}: {message}") from None
    repaired["actions"] = items
    return repaired


_APPS_ALIASES = {
    **dict.fromkeys(("launch", "start", "focus", "activate", "switch", "show"), "open"),
    **dict.fromkeys(("bringtofront", "openapp", "openapplication", "run"), "open"),
    **dict.fromkeys(("grant", "requestaccess", "access", "ask", "allow"), "request"),
    **dict.fromkeys(("ls", "status", "granted", "listapps", "search", "find"), "list"),
}


def normalize_apps(arguments: Any) -> Any:
    """Repair a ``computer_apps`` call: action and field aliases, one app or several."""
    repaired = normalize_call_arguments(
        _APPS_CONTRACT,
        _argument_object(arguments),
        enum_fields=("action",),
        field_aliases=SpellingAliases(
            {
                "app": ("name", "application", "app_name", "program"),
                "apps": ("applications", "app_names", "names", "programs"),
                "reason": ("why", "purpose", "justification", "message"),
                "query": ("filter", "search", "term", "contains"),
            }
        ),
        empty_as_omitted=("apps", "app", "reason", "query"),
    )
    if not isinstance(repaired, dict):
        return repaired
    action = repaired.get("action")
    if isinstance(action, str) and action not in APPS_ACTIONS:
        repaired["action"] = _APPS_ALIASES.get(_spelled(action), action)
    action = repaired.get("action")
    if action == "request" and "app" in repaired and "apps" not in repaired:
        repaired["apps"] = [repaired.pop("app")]
    elif action == "open" and "app" not in repaired:
        apps = repaired.get("apps")
        if isinstance(apps, list) and len(apps) == 1:
            repaired["app"] = repaired.pop("apps")[0]
    elif action == "list" and "app" in repaired and "query" not in repaired:
        repaired["query"] = repaired.pop("app")
    return repaired


__all__ = [
    "ACTIONS",
    "APPS_ACTIONS",
    "COMPUTER_APPS_DESCRIPTION",
    "COMPUTER_APPS_PARAMETERS",
    "COMPUTER_BATCH_DESCRIPTION",
    "COMPUTER_BATCH_PARAMETERS",
    "COMPUTER_DESCRIPTION",
    "COMPUTER_PARAMETERS",
    "MAX_BATCH_ACTIONS",
    "RESULT_SCHEMA",
    "SCROLL_DIRECTIONS",
    "normalize_apps",
    "normalize_batch",
    "normalize_computer",
]
