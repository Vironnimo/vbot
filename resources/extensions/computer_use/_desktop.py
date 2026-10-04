"""One Tool call's work on the desktop: frames, access enforcement, input and images.

Every ``Desktop`` method blocks and runs on the service's desktop worker
thread. Refusals raise ``CallRefusedError`` before any input; the target raises
``TargetError`` for platform failures and ``InputInterrupted`` for a stop.
"""

from __future__ import annotations

from collections.abc import Sequence

from PIL import Image

from core.tools import ToolContext

from ._access import Access
from ._actions import CLICKS, KEYBOARD, Action, Point
from ._screens import (
    Area,
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
    zoom_fit,
)
from .target import DesktopTarget, Display, WindowInfo


class CallRefusedError(Exception):
    """A call refused before input; ``code`` and message are Model-facing."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def request_call(app: str) -> str:
    return f'computer_apps {{"action":"request","apps":["{app}"],"reason":"..."}}'


class Desktop:
    """The desktop as one call of one Session sees it."""

    def __init__(self, target: DesktopTarget, access: Access, context: ToolContext) -> None:
        self.target = target
        self.access = access
        self.state = access.state
        self.context = context
        # Set right before the first input method runs: from then on, input may have been sent.
        self.input_started = False
        # The text of the screenshot taken after a failure that followed input.
        self.failure_screen: str | None = None

    # Frames and displays

    def frame(self, action: Action) -> Frame:
        """Resolve an image reference before the call or batch sends any input."""
        if action.name in KEYBOARD:
            # Keys need no coordinates; the image only tells which app was in front.
            images = self.state.images
            found = images.get(action.screenshot_id or "") or images.get(
                self.state.latest_image or ""
            )
            return found or Frame.of(self._chosen(self.target.displays()))
        needs_image = (
            bool(action.frame_points())
            or action.region is not None
            or action.name == "cursor_position"
            or action.screenshot_id is not None
        )
        if not needs_image:
            return Frame.of(self._chosen(self.target.displays()))
        reference = action.screenshot_id or self.state.latest_image
        frame = self.state.images.get(reference or "")
        if frame is None:
            which = (
                f'Image "{reference}" is not available in this Session'
                if reference
                else ("There is no screenshot in this Session yet")
            )
            raise CallRefusedError(
                "invalid_arguments",
                f'{which}. Take a new screenshot with {{"action":"screenshot"}} and use '
                "its screenshot_id and pixel coordinates. Nothing was done.",
            )
        self.validate_frame(frame)
        return frame

    def validate_frame(self, frame: Frame) -> None:
        """Do not apply an observed mapping after its display/window geometry changes."""
        if not frame.screenshot_id:
            return
        current = {item.id: item for item in self.target.displays()}
        if current != {item.id: item for item in frame.displays}:
            raise CallRefusedError(
                "invalid_arguments",
                f'Image "{frame.screenshot_id}" no longer fits because the display layout '
                'changed. Take a new screenshot with {"action":"screenshot"} and use its '
                "screenshot_id and coordinates. Nothing was sent for this action.",
            )
        if frame.window is not None:
            observed = frame.window
            window = next(
                (item for item in self.target.windows() if item.handle == observed.handle), None
            )
            if (
                window is None
                or not window.app.matches(observed.app)
                or (window.left, window.top, window.right, window.bottom)
                != (observed.left, observed.top, observed.right, observed.bottom)
            ):
                raise CallRefusedError(
                    "invalid_arguments",
                    f'The window in image "{frame.screenshot_id}" moved, resized or closed. '
                    'Take a new screenshot with {"action":"screenshot"} and use its '
                    "screenshot_id and coordinates. Nothing was sent for this action.",
                )

    def _chosen(self, displays: Sequence[Display]) -> Display:
        foreground = self.target.foreground()
        return choose_display(displays, self.state.display, foreground, self.access.window_visible)

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
        size = f'image "{frame.screenshot_id}", which is {frame.size_text} pixels'
        named = (
            [(f"path point {number}", point) for number, point in enumerate(action.path, 1)]
            if action.path
            else [("start_coordinate", action.start), ("coordinate", action.point)]
        )
        for name, point in named:
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

    def screenshot(self, display: str | None = None, view: str | None = None) -> str:
        """Publish the foreground window by default, or an explicitly selected display."""
        displays = self.target.displays()
        if display is not None:
            self._select(displays, display)
            self.state.view = "display"
        if view is not None:
            self.state.view = view
            if view == "window":
                self.state.display = "auto"
        chosen = self._chosen(displays)
        windows = self.target.windows()
        visible = self.access.window_visible
        foreground = self.target.foreground()
        area = display_area(chosen)
        window = None
        if self.state.view == "window" and foreground is not None and visible(foreground):
            # Include visible owned menus/dialogs even when they extend past the window.
            related = [foreground]
            handles = {foreground.handle}
            while True:
                children = [
                    item
                    for item in windows
                    if item.owner in handles
                    and item.handle not in handles
                    and item.app.matches(foreground.app)
                    and visible(item)
                ]
                if not children:
                    break
                related.extend(children)
                handles.update(item.handle for item in children)
            candidate = (
                max(min(item.left for item in related), min(item.left for item in displays)),
                max(min(item.top for item in related), min(item.top for item in displays)),
                min(
                    max(item.right for item in related),
                    max(item.left + item.width for item in displays),
                ),
                min(
                    max(item.bottom for item in related),
                    max(item.top + item.height for item in displays),
                ),
            )
            if candidate[0] < candidate[2] and candidate[1] < candidate[3]:
                area, window = candidate, foreground
        number = next(index for index, item in enumerate(displays, 1) if item.id == chosen.id)
        which = f" {number} of {len(displays)}" if len(displays) > 1 else ""
        label = (
            f'Screenshot of foreground window "{window.app.name}"'
            if window is not None
            else f'Screenshot of display{which} "{chosen.name}"'
        )
        text = self._publish(area, displays, chosen, label, window)
        if self.state.display != "auto" and foreground is not None and visible(foreground):
            holding = display_of_window(displays, foreground)
            if holding is not None and holding.id != chosen.id:
                text += (
                    f' The foreground app {foreground.app.name} is on display "{holding.name}"; '
                    'show it with {"action":"screenshot","display":"auto"}.'
                )
        return text

    def _publish(
        self,
        area: Area,
        displays: Sequence[Display],
        display: Display,
        label: str,
        window: WindowInfo | None = None,
        zoom: bool = False,
    ) -> str:
        capture = Image.new("RGB", (area[2] - area[0], area[3] - area[1]))
        for item in displays:
            left, top = max(area[0], item.left), max(area[1], item.top)
            right = min(area[2], item.left + item.width)
            bottom = min(area[3], item.top + item.height)
            if left < right and top < bottom:
                image = self.target.capture(item).crop(
                    (left - item.left, top - item.top, right - item.left, bottom - item.top)
                )
                capture.paste(image, (left - area[0], top - area[1]))
        visible = self.access.window_visible
        covered, hidden = mask(capture, area, self.target.windows(), visible)
        size = zoom_fit(*covered.size) if zoom else fit(*covered.size)
        path = publish_image(self.context, resized(covered, size))
        foreground = self.target.foreground()
        front = foreground.app if foreground is not None and visible(foreground) else None
        frame = Frame(display, *size, area, path.stem, tuple(displays), window, front)
        self.state.images[frame.screenshot_id] = frame
        if not zoom:
            # A zoom is a magnifier: coordinates without screenshot_id stay on the screenshot.
            self.state.latest_image = frame.screenshot_id
        self.state.shown = display
        # Keep references bounded without retaining image bytes in Session state.
        while len(self.state.images) > 64:
            del self.state.images[next(iter(self.state.images))]
        shot = frame.screenshot_id
        text = f'{label}: {size[0]}x{size[1]} pixels, screenshot_id="{shot}". '
        if zoom:
            text += (
                f'To click something you see here, pass screenshot_id="{shot}" with its '
                "position in this image."
            )
        else:
            text += "Use positions in this image as coordinates."
        # Name no ratio: Agents used it to convert coordinates instead of clicking what they saw.
        if frame.screen_pixels >= 1.05:
            text += " It is reduced: for a small target, zoom in and click in the zoom image."
        if hidden:
            text += f" Hidden apps (gray, not approved): {', '.join(hidden)}."
        return text

    def zoom(self, frame: Frame, action: Action) -> str:
        """Publish a directly clickable fresh capture of a selected image's region."""
        assert action.region is not None
        self.validate_frame(frame)
        return self._publish(
            frame.region(*action.region),
            frame.displays,
            frame.display,
            f"Zoom of {list(action.region)} in image {frame.screenshot_id}",
            frame.window,
            zoom=True,
        )

    def cursor(self, frame: Frame) -> str:
        self.validate_frame(frame)
        x, y = self.target.cursor()
        left, top, right, bottom = frame.area
        if left <= x < right and top <= y < bottom:
            fx, fy = frame.to_frame(x, y)
            return (
                f'The pointer is at [{fx}, {fy}] in image "{frame.screenshot_id}" '
                f"({frame.size_text} pixels)."
            )
        other = next((item for item in self.target.displays() if item.contains(x, y)), None)
        where = f' on display "{other.name}"' if other is not None else ""
        return (
            f'The pointer is outside image "{frame.screenshot_id}"{where}. Move it with '
            "mouse_move to a coordinate in this image, or take a new screenshot."
        )

    # Input

    def act(self, action: Action, frame: Frame) -> str:
        """Check access for *action*, send its input and describe what was done."""
        if action.uses_pointer:
            self.validate_frame(frame)
        points = self._points(action, frame)
        self._check_access(action, frame, points)
        if action.name in KEYBOARD and not self.input_started:
            self._check_front(action, frame)
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
            target.drag(points, action.seconds, modifiers)
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
            return [at(action.start), *(at(point) for point in action.via), at(action.point)]
        return [at(action.point)]

    def _check_access(self, action: Action, frame: Frame, points: list[Point]) -> None:
        """Refuse unless the foreground window and the window at each point take input."""
        if not action.sends_input:
            return
        foreground = self.target.foreground()
        if foreground is None:
            if not points:
                raise CallRefusedError(
                    "access_required",
                    f"No app window is in the foreground, so {action.name} was not sent. Bring "
                    'an app to the front with computer_apps {"action":"open","app":"..."}.',
                )
        else:
            self._allow(action, foreground, "The foreground window")
        for point in points:
            shown = list(frame.to_frame(*point))
            window = self.target.window_at(*point)
            if window is None:
                raise CallRefusedError(
                    "access_required",
                    f"No app window is at {shown}, so {action.name} was not sent. Use "
                    "coordinates on an app window in that image.",
                )
            self._allow(action, window, f"The window at {shown}")
            if frame.window is not None and not window.app.matches(frame.window.app):
                raise CallRefusedError(
                    "invalid_arguments",
                    f'The window in image "{frame.screenshot_id}" is covered at {shown}, so '
                    f"{action.name} was not sent. Bring {frame.window.app.name} to the front "
                    "and take a new screenshot before retrying.",
                )

    def _check_front(self, action: Action, frame: Frame) -> None:
        """Refuse keys when another app came to the front since the Agent's image.

        Only the call's first input is checked: later keys may follow a window
        the call itself brought up, such as a dialog or the Start menu.
        """
        expected, current = frame.front, self.target.foreground()
        if expected is None or current is None or current.app.matches(expected):
            return
        raise CallRefusedError(
            "focus_changed",
            f'{expected.name} was in front in image "{frame.screenshot_id}", but the foreground '
            f"window now belongs to {current.app.name}, so {action.name} was not sent. Click "
            "into the window you want to type into, or take a screenshot to see what is in "
            "front.",
        )

    def _allow(self, action: Action, window: WindowInfo, where: str) -> None:
        app = window.app.name
        if not self.access.allows(window.app):
            bring = (
                ', or bring an approved app to the front with computer_apps {"action":"open",'
                '"app":"..."}'
                if where == "The foreground window"
                else ""
            )
            raise CallRefusedError(
                "access_required",
                f"{where} belongs to {app}, which the user has not approved in this Session, so "
                f"{action.name} was not sent. Ask the user for it with {request_call(app)}"
                f"{bring}.",
            )
        if window.elevated:
            raise CallRefusedError(
                "target_elevated",
                f"{app} runs as administrator, so Windows blocks input from vBot into it and "
                f"{action.name} was not sent. Ask the user to do this step, or to restart {app} "
                "without administrator rights.",
            )


__all__ = ["Desktop", "CallRefusedError", "request_call"]
