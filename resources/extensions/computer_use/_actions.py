"""One validated computer action: the fields it reads, the access it needs, its wording.

Parsing refuses a call before any input when its intent is unclear and names
the corrected call. Checks that need the screen (frame bounds, display names,
access) happen in the service.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ._keys import KeyChordError, chord_text, parse_chords, parse_modifiers
from ._tools import ACTIONS, MAX_PATH_POINTS

type Point = tuple[int, int]

READ_ACTIONS = frozenset({"screenshot", "zoom", "cursor_position", "wait"})
# Keyboard input goes to the foreground window, wherever the pointer is.
KEYBOARD = frozenset({"type", "key", "hold_key"})
CLICKS = {
    "left_click": ("left", 1),
    "right_click": ("right", 1),
    "middle_click": ("middle", 1),
    "double_click": ("left", 2),
    "triple_click": ("left", 3),
}
MAX_SECONDS = 100.0
DEFAULT_SCROLL_AMOUNT = 3
DEFAULT_WAIT_SECONDS = 1.0
# A drag without duration moves at this many image pixels per second, within these bounds.
DRAG_PIXELS_PER_SECOND = 1000
MIN_DRAG_SECONDS, MAX_DEFAULT_DRAG_SECONDS = 0.25, 5.0

_FIELDS: dict[str, frozenset[str]] = {
    "screenshot": frozenset({"display", "view"}),
    "zoom": frozenset({"region", "screenshot_id"}),
    **dict.fromkeys(CLICKS, frozenset({"coordinate", "text", "screenshot_id"})),
    "mouse_move": frozenset({"coordinate", "screenshot_id"}),
    "left_click_drag": frozenset(
        {"coordinate", "start_coordinate", "path", "duration", "text", "screenshot_id"}
    ),
    "left_mouse_down": frozenset({"coordinate", "screenshot_id"}),
    "left_mouse_up": frozenset({"coordinate", "screenshot_id"}),
    "scroll": frozenset(
        {"coordinate", "scroll_direction", "scroll_amount", "text", "screenshot_id"}
    ),
    # On keyboard actions screenshot_id names the image whose app must still be in front.
    "type": frozenset({"text", "screenshot_id"}),
    "key": frozenset({"text", "repeat", "screenshot_id"}),
    "hold_key": frozenset({"text", "duration", "screenshot_id"}),
    "wait": frozenset({"duration"}),
    "cursor_position": frozenset({"screenshot_id"}),
}


class ActionError(ValueError):
    """An action refused before any input; the message names the corrected call."""


@dataclass(frozen=True)
class Action:
    name: str
    point: Point | None = None
    start: Point | None = None
    # The points a drag passes between start and point, in order.
    via: tuple[Point, ...] = ()
    # Whether the drag's points came as one "path" rather than start and end fields.
    from_path: bool = False
    text: str = ""
    chords: tuple[tuple[str, ...], ...] = ()
    modifiers: tuple[str, ...] = ()
    direction: str = ""
    amount: int = DEFAULT_SCROLL_AMOUNT
    repeat: int = 1
    seconds: float = 0.0
    region: tuple[int, int, int, int] | None = None
    display: str | None = None
    view: str | None = None
    screenshot_id: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def sends_input(self) -> bool:
        return self.name not in READ_ACTIONS

    @property
    def uses_pointer(self) -> bool:
        """Whether the action acts at a pointer position (given or current)."""
        return self.name in _FIELDS and "coordinate" in _FIELDS[self.name]

    @property
    def path(self) -> list[Point]:
        """A drag's points in order when it came as a path, else empty."""
        if not self.from_path or self.start is None or self.point is None:
            return []
        return [self.start, *self.via, self.point]

    def frame_points(self) -> list[Point]:
        return [point for point in (self.start, *self.via, self.point) if point is not None]

    def describe(self) -> str:
        """Short wording of what the action did, without typed text."""
        name = self.name
        held = f" holding {'+'.join(self.modifiers)}" if self.modifiers else ""
        at = f"at {list(self.point)}" if self.point else "at the pointer"
        if name in CLICKS or name in {"left_mouse_down", "left_mouse_up"}:
            return f"{name} {at}{held}"
        if name == "mouse_move":
            return f"mouse_move to {list(self.point or ())}"
        if name == "left_click_drag":
            start = list(self.start) if self.start else "the pointer"
            through = f" through {len(self.via)} points" if self.via else ""
            end = list(self.point or ())
            return f"left_click_drag from {start}{through} to {end} in {self.seconds:g} s{held}"
        if name == "scroll":
            return f"scroll {self.direction} {self.amount} {at}{held}"
        if name == "type":
            return f"type ({len(self.text)} characters)"
        if name == "key":
            keys = " ".join(chord_text(list(chord)) for chord in self.chords)
            return f"key {keys}" + (f" {self.repeat} times" if self.repeat > 1 else "")
        if name == "hold_key":
            return f"hold_key {chord_text(list(self.chords[0]))} for {self.seconds:g} s"
        if name == "wait":
            return f"wait {self.seconds:g} s"
        if name == "zoom" and self.region is not None:
            return f"zoom {list(self.region)}"
        return name


def parse_action(arguments: Mapping[str, Any]) -> Action:
    """Return the validated action of one ``computer`` call or batch item."""
    name = arguments.get("action")
    if name not in ACTIONS:
        raise ActionError(f"Unknown action {name!r}. Use one of: {', '.join(ACTIONS)}.")
    fields = _FIELDS[name]
    unused = [key for key in arguments if key not in {"action", *fields}]
    _refuse_misplaced(name, unused)
    values: dict[str, Any] = {"name": name}
    notes = []
    if unused:
        notes.append(
            f"Ignored {', '.join(unused)}: {name} does not use "
            f"{'it' if len(unused) == 1 else 'them'}."
        )
    if name == "left_click_drag":
        values.update(_drag_points(arguments))
        seconds, note = _seconds(arguments.get("duration"), name, _drag_seconds(values))
        values["seconds"] = seconds
        notes.extend([note] if note else [])
    elif "coordinate" in fields and arguments.get("coordinate") is not None:
        values["point"] = _point(arguments["coordinate"], "coordinate")
    elif name == "mouse_move":
        raise ActionError(
            'mouse_move needs "coordinate": [x, y] in a screenshot or zoom image, for example '
            '{"action":"mouse_move","coordinate":[640, 360]}.'
        )
    text = arguments.get("text")
    if name in CLICKS or name in {"scroll", "left_click_drag"}:
        values["modifiers"] = tuple(_keys(parse_modifiers, text or ""))
    elif name in {"key", "hold_key"}:
        if not isinstance(text, str) or not text.strip():
            raise ActionError(
                f'{name} needs the keys in "text", for example {{"action":"{name}","text":'
                f'"{"ctrl+s" if name == "key" else "shift"}"}}.'
            )
        values["chords"] = tuple(tuple(chord) for chord in _keys(parse_chords, text))
    elif name == "type":
        if not isinstance(text, str) or not text:
            raise ActionError(
                'type needs the text to type in "text": {"action":"type","text":"..."}.'
            )
        values["text"] = text
    if name == "scroll":
        if arguments.get("scroll_direction") is None:
            raise ActionError(
                'scroll needs "scroll_direction": up, down, left or right, for example '
                '{"action":"scroll","coordinate":[640, 360],"scroll_direction":"down"}.'
            )
        values["direction"] = arguments["scroll_direction"]
        values["amount"] = arguments.get("scroll_amount") or DEFAULT_SCROLL_AMOUNT
    if name == "key":
        values["repeat"] = arguments.get("repeat") or 1
    if name in {"hold_key", "wait"}:
        default = DEFAULT_WAIT_SECONDS if name == "wait" else None
        seconds, note = _seconds(arguments.get("duration"), name, default)
        values["seconds"] = seconds
        notes.extend([note] if note else [])
    if name == "hold_key" and len(values["chords"]) != 1:
        raise ActionError(
            'hold_key holds one key or chord, such as "shift" or "ctrl+alt". To press keys in '
            'order, use {"action":"key","text":"ctrl+a delete"}.'
        )
    if name == "zoom":
        values["region"] = _region(arguments.get("region"))
    if name == "screenshot" and isinstance(arguments.get("display"), str):
        values["display"] = arguments["display"].strip() or None
    if name == "screenshot":
        values["view"] = arguments.get("view")
        if values["view"] == "window" and values.get("display") is not None:
            raise ActionError(
                'view "window" follows the foreground window. Omit display, or use '
                'view "display" to capture a selected monitor. Nothing was done.'
            )
    if "screenshot_id" in fields:
        values["screenshot_id"] = arguments.get("screenshot_id")
    return Action(**values, notes=tuple(notes))


def _refuse_misplaced(name: str, unused: list[str]) -> None:
    """Refuse fields whose presence means the Agent expects a different effect."""
    if "path" in unused:
        raise ActionError(
            f"{name} does not take a path. To hold the left button down along several points, "
            'use {"action":"left_click_drag","path":[[x0, y0],[x1, y1],[x2, y2]]}; to move '
            "without pressing, use one mouse_move per point."
        )
    if "coordinate" in unused and name in {"type", "key", "hold_key"}:
        raise ActionError(
            f"{name} acts on the focused field and does not click first. Click the field "
            f"with left_click before {name}, or in one call: computer_batch "
            f'{{"actions":[{{"action":"left_click","coordinate":[x, y]}},'
            f'{{"action":"{name}","text":"..."}}]}}.'
        )
    if "text" in unused and name in {"mouse_move", "left_mouse_down", "left_mouse_up"}:
        raise ActionError(
            f"{name} cannot hold or type keys. Hold modifier keys on a click, scroll or "
            'drag with "text", such as {"action":"left_click_drag","start_coordinate":'
            '[x0, y0],"coordinate":[x1, y1],"text":"shift"}, or press keys with the key '
            "action."
        )
    if "scroll_direction" in unused and (name in CLICKS or name == "mouse_move"):
        raise ActionError(
            f"{name} does not scroll. Scroll with "
            '{"action":"scroll","coordinate":[x, y],"scroll_direction":"down","scroll_amount":3}.'
        )


def _keys[T](parse: Callable[[str], T], text: str) -> T:
    try:
        return parse(text)
    except KeyChordError as error:
        raise ActionError(str(error)) from None


def _point(value: Any, field: str) -> Point:
    if (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    ):
        return value[0], value[1]
    raise ActionError(f'"{field}" must be [x, y]: two whole pixel numbers from the screenshot.')


def _drag_points(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """The start, intermediate and end points of a drag: from path or from the two fields."""
    path = arguments.get("path")
    point, start = arguments.get("coordinate"), arguments.get("start_coordinate")
    if path is not None:
        if point is not None or start is not None:
            raise ActionError(
                'left_click_drag takes either "path" or "start_coordinate" and "coordinate", '
                'not both. Put every point into path, for example {"action":"left_click_drag",'
                '"path":[[100, 200],[150, 240],[200, 200]]}.'
            )
        if not isinstance(path, list) or not 2 <= len(path) <= MAX_PATH_POINTS:
            raise ActionError(
                f'"path" must list 2 to {MAX_PATH_POINTS} points [x, y] in order, for example '
                "[[100, 200],[150, 240],[200, 200]]."
            )
        points = [_point(item, "path") for item in path]
        return {
            "start": points[0],
            "via": tuple(points[1:-1]),
            "point": points[-1],
            "from_path": True,
        }
    if point is None:
        raise ActionError(
            'left_click_drag needs where to drag: "start_coordinate" and "coordinate", or '
            '"path" with the points in order, for example {"action":"left_click_drag",'
            '"start_coordinate":[100, 200],"coordinate":[400, 200]}.'
        )
    values = {"point": _point(point, "coordinate")}
    if start is not None:
        values["start"] = _point(start, "start_coordinate")
    return values


def _drag_seconds(values: Mapping[str, Any]) -> float:
    """The default drag time for the given points' length, in image pixels."""
    points = [p for p in (values.get("start"), *values.get("via", ()), values["point"]) if p]
    length = sum(math.dist(a, b) for a, b in itertools.pairwise(points))
    seconds = length / DRAG_PIXELS_PER_SECOND
    return round(min(MAX_DEFAULT_DRAG_SECONDS, max(MIN_DRAG_SECONDS, seconds)), 2)


def _region(value: Any) -> tuple[int, int, int, int]:
    if (
        isinstance(value, list)
        and len(value) == 4
        and all(isinstance(item, int) and not isinstance(item, bool) for item in value)
    ):
        x0, y0, x1, y1 = value
        if x1 > x0 and y1 > y0:
            return x0, y0, x1, y1
    raise ActionError(
        'zoom needs "region": [x0, y0, x1, y1], the top-left and bottom-right corners of a '
        'rectangle in a screenshot, for example {"action":"zoom","region":'
        "[100, 200, 500, 400]}."
    )


def _seconds(value: Any, name: str, default: float | None) -> tuple[float, str | None]:
    """Return seconds and a note when a large number was read as milliseconds."""
    if value is None:
        if default is None:
            raise ActionError(
                f'{name} needs "duration" in seconds, for example '
                f'{{"action":"{name}","text":"shift","duration":1}}.'
            )
        return default, None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ActionError('"duration" must be a number of seconds from 0 to 100.')
    seconds = float(value)
    if 0 <= seconds <= MAX_SECONDS:
        return seconds, None
    if MAX_SECONDS < seconds <= MAX_SECONDS * 1000:
        # Above the limit only milliseconds make sense: 500 means half a second.
        seconds /= 1000
        return seconds, (
            f"duration {value:g} was read as milliseconds ({seconds:g} s); duration is in seconds."
        )
    raise ActionError('"duration" must be a number of seconds from 0 to 100.')


__all__ = [
    "CLICKS",
    "KEYBOARD",
    "READ_ACTIONS",
    "Action",
    "ActionError",
    "Point",
    "parse_action",
]
