"""One Tool call's work on the desktop: frames, access enforcement, input and images.

Every ``Desktop`` method blocks and runs on the service's desktop worker
thread. Refusals raise ``CallRefusedError`` before any input; the target raises
``TargetError`` for platform failures and ``InputInterrupted`` for a stop.
"""

from __future__ import annotations

from collections.abc import Sequence

from core.tools import ToolContext

from ._access import TIER_WORDS, SessionState, tier_allows, window_visible
from ._actions import CLICKS, Action, Point
from ._screens import (
    Frame,
    choose_display,
    describe_displays,
    display_area,
    display_of_window,
    find_display,
    fit,
    mask,
    publish_image,
    resized,
    scaled,
)
from .target import DesktopTarget, Display, WindowInfo

_TIER_RULES = {
    "read": "screenshots and zoom work, but input is refused",
    "click": (
        "left clicks, double and triple clicks, scrolling and mouse moves without modifier "
        "keys work; typing, keys, right and middle clicks, drags and modifier keys are refused"
    ),
}


class CallRefusedError(Exception):
    """A call refused before input; ``code`` and message are Model-facing."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def request_call(app: str) -> str:
    return f'computer_apps {{"action":"request","apps":["{app}"],"reason":"..."}}'


class Desktop:
    """The desktop as one call of one Session sees it."""

    def __init__(self, target: DesktopTarget, state: SessionState, context: ToolContext) -> None:
        self.target = target
        self.state = state
        self.context = context
        # Set right before the first input method runs: from then on, input may have been sent.
        self.input_started = False

    # Frames and displays

    def frame(self, exact: bool) -> Frame:
        """Return the frame coordinates refer to: the latest screenshot's display.

        With *exact*, coordinates are about to be used, so a display that changed
        size or disappeared since that screenshot refuses the call.
        """
        displays = self.target.displays()
        shown = self.state.shown
        if shown is not None:
            current = next((display for display in displays if display.id == shown.id), None)
            if current is not None and display_area(current) == display_area(shown):
                return Frame.of(current)
            if exact:
                raise CallRefusedError(
                    "invalid_arguments",
                    "The display of the latest screenshot changed size or was disconnected, so "
                    'its coordinates no longer fit. Take a new screenshot with {"action":'
                    '"screenshot"} and use its coordinates.',
                )
        return Frame.of(self._chosen(displays))

    def _chosen(self, displays: Sequence[Display]) -> Display:
        visible = window_visible(self.state, self.target.own_app_keys())
        return choose_display(displays, self.state.display, self.target.foreground(), visible)

    def _select(self, displays: Sequence[Display], value: str) -> None:
        if value.casefold() == "auto":
            self.state.display = "auto"
            return
        found = find_display(displays, value)
        if found is None:
            raise CallRefusedError(
                "invalid_arguments",
                f'There is no display "{value}". Displays:\n'
                f"{describe_displays(displays, self.state.shown)}\n"
                'Name one by number or name, or use "auto" for the display of the foreground app.',
            )
        self.state.display = found.id

    @staticmethod
    def check_bounds(action: Action, frame: Frame) -> None:
        """Refuse coordinates outside *frame* before anything runs."""
        size = (
            f'the latest screenshot of display "{frame.display.name}", which is {frame.size_text}'
        )
        for name, point in (("start_coordinate", action.start), ("coordinate", action.point)):
            if point is not None and not frame.contains(*point):
                raise CallRefusedError(
                    "invalid_arguments",
                    f"{name} {list(point)} is outside {size}. Use coordinates from that "
                    "screenshot.",
                )
        if action.region is not None:
            x0, y0, x1, y1 = action.region
            if x0 < 0 or y0 < 0 or x1 > frame.width or y1 > frame.height:
                raise CallRefusedError(
                    "invalid_arguments",
                    f"region {list(action.region)} is outside {size}. Use corners inside it.",
                )

    # Images

    def screenshot(self, scale: float = 1.0, display: str | None = None) -> str:
        """Capture, mask and publish the current display; return its description."""
        displays = self.target.displays()
        if display is not None:
            self._select(displays, display)
        chosen = self._chosen(displays)
        windows = self.target.windows()
        visible = window_visible(self.state, self.target.own_app_keys())
        covered, hidden = mask(self.target.capture(chosen), display_area(chosen), windows, visible)
        frame = Frame.of(chosen)
        size = (frame.width, frame.height)
        image = resized(covered, size)
        if scale < 1:
            size = scaled(size, scale)
            image = resized(image, size)
        publish_image(self.context, image)
        self.state.shown = chosen
        number = next(index for index, item in enumerate(displays, 1) if item.id == chosen.id)
        which = f" {number} of {len(displays)}" if len(displays) > 1 else ""
        text = f'Screenshot of display{which} "{chosen.name}": {frame.size_text} frame'
        if size != (frame.width, frame.height):
            text += (
                f", image scaled to {size[0]}x{size[1]}; coordinates use the "
                f"{frame.size_text} frame"
            )
        text += "."
        if hidden:
            text += f" Hidden apps (gray, not granted): {', '.join(hidden)}."
        foreground = self.target.foreground()
        if self.state.display != "auto" and foreground is not None and visible(foreground):
            holding = display_of_window(displays, foreground)
            if holding is not None and holding.id != chosen.id:
                text += (
                    f' The foreground app {foreground.app.name} is on display "{holding.name}"; '
                    'show it with {"action":"screenshot","display":"auto"}.'
                )
        return text

    def zoom(self, frame: Frame, action: Action) -> str:
        """Publish a fresh, masked capture of a frame region at physical resolution."""
        assert action.region is not None
        area = frame.region(*action.region)
        display = frame.display
        capture = self.target.capture(display)
        crop = capture.crop(
            (
                area[0] - display.left,
                area[1] - display.top,
                area[2] - display.left,
                area[3] - display.top,
            )
        )
        visible = window_visible(self.state, self.target.own_app_keys())
        covered, hidden = mask(crop, area, self.target.windows(), visible)
        size = fit(covered.width, covered.height)
        if action.scale < 1:
            size = scaled(size, action.scale)
        publish_image(self.context, resized(covered, size))
        factor = size[0] / (action.region[2] - action.region[0])
        text = (
            f"Zoom of {list(action.region)}: image {size[0]}x{size[1]}, {factor:.1f}x the "
            f"screenshot. Coordinates still use the {frame.size_text} frame of display "
            f'"{display.name}".'
        )
        if hidden:
            text += f" Hidden apps (gray, not granted): {', '.join(hidden)}."
        return text

    def cursor(self, frame: Frame) -> str:
        x, y = self.target.cursor()
        if frame.display.contains(x, y):
            fx, fy = frame.to_frame(x, y)
            return (
                f"The pointer is at [{fx}, {fy}] in the {frame.size_text} frame of display "
                f'"{frame.display.name}".'
            )
        other = next((item for item in self.target.displays() if item.contains(x, y)), None)
        where = f' on display "{other.name}"' if other is not None else ""
        return (
            f"The pointer is outside the current display{where}. Move it with mouse_move to a "
            "coordinate in the latest screenshot."
        )

    # Input

    def act(self, action: Action, frame: Frame) -> str:
        """Check access for *action*, send its input and describe what was done."""
        points = self._points(action, frame)
        self._check_access(action, frame, points)
        self.input_started = True
        target = self.target
        modifiers = list(action.modifiers)
        name = action.name
        if name in CLICKS:
            button, count = CLICKS[name]
            target.click(*points[0], button, count, modifiers)
        elif name == "mouse_move":
            target.move(*points[0])
        elif name == "left_click_drag":
            target.drag(points[0], points[1], modifiers)
        elif name in {"left_mouse_down", "left_mouse_up"}:
            if action.point is not None:
                target.move(*points[0])
            target.button("left", name == "left_mouse_down")
        elif name == "scroll":
            target.scroll(*points[0], action.direction, action.amount, modifiers)
        elif name == "type":
            target.type_text(action.text)
        elif name == "key":
            if len(action.chords) == 1:
                target.keys(list(action.chords[0]), action.repeat)
            else:
                for _ in range(action.repeat):
                    for chord in action.chords:
                        target.keys(list(chord), 1)
        elif name == "hold_key":
            target.hold(list(action.chords[0]), action.seconds)
        return action.describe()

    def _points(self, action: Action, frame: Frame) -> list[Point]:
        """Physical points the action acts at, in order; omitted ones are the pointer's."""
        if not action.uses_pointer:
            return []
        cursor: Point | None = None

        def at(point: Point | None) -> Point:
            nonlocal cursor
            if point is not None:
                return frame.to_physical(*point)
            if cursor is None:
                cursor = self.target.cursor()
            return cursor

        if action.name == "left_click_drag":
            return [at(action.start), at(action.point)]
        return [at(action.point)]

    def _check_access(self, action: Action, frame: Frame, points: list[Point]) -> None:
        """Refuse unless the foreground app and the app at each point allow *action*."""
        if action.tier == "read":
            return
        own = self.target.own_app_keys()
        foreground = self.target.foreground()
        if foreground is None:
            if not points:
                raise CallRefusedError(
                    "access_required",
                    f"No app window is in the foreground, so {action.name} was not sent. Bring a "
                    'granted app to the front with computer_apps {"action":"open","app":"..."}.',
                )
        else:
            self._allow(action, foreground, "The foreground window", own)
        for point in points:
            shown = list(frame.to_frame(*point))
            window = self.target.window_at(*point)
            if window is None:
                raise CallRefusedError(
                    "access_required",
                    f"No app this Session may use is at {shown}, so {action.name} was not sent. "
                    "Use coordinates on a granted app in the latest screenshot.",
                )
            self._allow(action, window, f"The window at {shown}", own)

    def _allow(self, action: Action, window: WindowInfo, where: str, own: frozenset[str]) -> None:
        app = window.app.name
        foreground = where == "The foreground window"
        bring = 'bring a granted app to the front with computer_apps {"action":"open","app":"..."}'
        if window.app.keys & own:
            raise CallRefusedError(
                "access_required",
                f"{where} belongs to vBot itself, which Computer Use never operates, so "
                f"{action.name} was not sent." + (f" First {bring}." if foreground else ""),
            )
        grant = self.state.grant_for(window.app)
        if grant is None:
            bring = f", or {bring}" if foreground else ""
            raise CallRefusedError(
                "access_required",
                f"{where} belongs to {app}, which is not granted in this Session, so "
                f"{action.name} was not sent. Ask the user for it with {request_call(app)}"
                f"{bring}.",
            )
        if not tier_allows(grant.tier, action.tier):
            held = " with modifier keys" if action.modifiers else ""
            raise CallRefusedError(
                "access_tier",
                f"{where} belongs to {app}, which is {TIER_WORDS[grant.tier]} in this Session: "
                f"{_TIER_RULES[grant.tier]}. {action.name}{held} was not sent. Do this step "
                "another way, for example with other Tools, or ask the user to do it.",
            )
        if window.elevated:
            raise CallRefusedError(
                "target_elevated",
                f"{app} runs as administrator, so Windows blocks input from vBot into it and "
                f"{action.name} was not sent. Ask the user to do this step, or to restart {app} "
                "without administrator rights.",
            )


__all__ = ["Desktop", "CallRefusedError", "request_call"]
