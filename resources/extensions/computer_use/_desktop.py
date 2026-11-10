"""One Tool call's work on the desktop: frames, access enforcement, input and images.

Every ``Desktop`` method blocks and runs on the service's desktop worker
thread. Refusals raise ``CallRefusedError`` before any input; the target raises
``TargetError`` for platform failures and ``InputInterrupted`` for a stop.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from PIL import Image

from core.tools import ToolContext

from ._access import Access
from ._actions import CLICKS, KEYBOARD, PRESSES, Action, Point
from ._screens import (
    Area,
    Frame,
    app_at,
    choose_display,
    describe_displays,
    display_area,
    display_of_window,
    find_display,
    fit,
    mask,
    overlaps,
    publish_image,
)
from .target import (
    AppInfo,
    DesktopSnapshot,
    DesktopTarget,
    Display,
    WindowInfo,
)


class CallRefusedError(Exception):
    """A call refused before input; ``code`` and message are Model-facing."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def request_call(app: str) -> str:
    return "computer_apps " + json.dumps(
        {"action": "request", "apps": [app], "reason": "..."},
        ensure_ascii=False,
        separators=(",", ":"),
    )


class Desktop:
    """The desktop as one call of one Session sees it."""

    def __init__(self, target: DesktopTarget, access: Access, context: ToolContext) -> None:
        self.target = target
        self.access = access
        self.state = access.state
        self.context = context
        # Set right before the first input method runs: from then on, input may have been sent.
        self.input_started = False
        # A completed single action remains visible if its following screenshot fails.
        self.completed_action: str | None = None
        # The text of the screenshot taken after a failure that followed input.
        self.failure_screen: str | None = None
        # The app this call's last mouse button press went into; keys must reach it.
        self.clicked: AppInfo | None = None
        self.clicked_window: WindowInfo | None = None
        # Whether keys were sent since the last press: they can bring up another app
        # themselves, such as the Start menu or a dialog, so later keys are not checked.
        self.keyed = False

    # Frames and displays

    def frame(self, action: Action) -> Frame:
        """Resolve an image reference before the call or batch sends any input."""
        if action.window_id is not None:
            self.window(action.window_id)
        if action.element_id is not None:
            window, element = self.element_reference(action)
            if action.name != "left_click" and action.name not in element.get("actions", []):
                raise CallRefusedError(
                    "element_not_supported",
                    f"The inspected element does not support {action.name}. Nothing was done. "
                    "Use its listed actions or screenshot coordinates.",
                )
            return Frame.of(self._chosen(self.target.displays()))
        needs_image = (
            bool(action.frame_points())
            or action.region is not None
            or action.name == "cursor_position"
            or action.screenshot_id is not None
        )
        if action.name in KEYBOARD or (action.uses_pointer and not needs_image):
            # Without coordinates the image only tells which app was in front, or which
            # app lay under the pointer; without an image there is nothing to compare.
            images = self.state.images
            reference = action.screenshot_id or self.state.latest_image
            found = images.get(reference or "")
            if found is not None:
                return found
            if action.screenshot_id is None:
                return Frame.of(self._chosen(self.target.displays()))
        if not needs_image:
            displays = self.target.displays()
            if action.display is not None:
                self._selection(displays, action.display)
            return Frame.of(self._chosen(displays))
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
        if (
            action.window_id
            and action.frame_points()
            and frame.window is not None
            and not frame.window.same_window(self.window(action.window_id))
        ):
            raise CallRefusedError(
                "invalid_arguments",
                "The screenshot and window_id name different windows. Nothing was "
                "done. Take a screenshot of the intended window first.",
            )
        return frame

    def validate_frame(self, frame: Frame, snapshot: DesktopSnapshot | None = None) -> None:
        """Do not apply an observed mapping after its display/window geometry changes.

        *snapshot* is the current observation when the caller already has it.
        """
        if not frame.screenshot_id:
            return
        snapshot = snapshot or self.target.snapshot([])
        current = {item.id: item for item in snapshot.displays}
        if current != {item.id: item for item in frame.displays}:
            raise CallRefusedError(
                "invalid_arguments",
                f'Image "{frame.screenshot_id}" no longer fits because the display layout '
                'changed. Take a new screenshot with {"action":"screenshot"} and use its '
                "screenshot_id and coordinates. Nothing was sent for this action.",
            )
        if frame.window is not None:
            observed = frame.window
            stack = snapshot.windows
            window = next((item for item in stack if item.handle == observed.handle), None)
            if (
                window is None
                or not window.same_window(observed)
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

    def _selection(self, displays: Sequence[Display], value: str) -> str:
        if value.casefold() == "auto":
            return "auto"
        found = find_display(displays, value)
        if found is None:
            raise CallRefusedError(
                "invalid_arguments",
                f'There is no display "{value}". Displays:\n'
                f"{describe_displays(displays, self.state.shown)}\n"
                'Name one by number or name, or use "auto" for the display of the foreground app.',
            )
        return found.id

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

    def screenshot(
        self, display: str | None = None, view: str | None = None, window_id: str | None = None
    ) -> str:
        """Publish the foreground window by default, or an explicitly selected display."""
        if window_id is not None:
            selected = self.window(window_id)
            self._allow(Action("screenshot"), selected, "The selected window")
            self.target.activate(selected)
            view = "window"
        observed = self.target.snapshot([])
        if window_id is not None and (
            observed.foreground is None or not observed.foreground.same_window(selected)
        ):
            raise CallRefusedError(
                "screen_changed",
                (
                    "The selected window lost focus before capture. No screenshot was "
                    "returned. Select the window again."
                ),
            )
        displays = observed.displays
        if display is not None:
            self.state.display = self._selection(displays, display)
            self.state.view = "display"
        if view is not None:
            self.state.view = view
            if view == "window":
                self.state.display = "auto"
        visible = self.access.window_visible
        foreground = observed.foreground
        chosen = choose_display(
            displays,
            self.state.display,
            foreground if foreground is not None and not foreground.shell else None,
            visible,
        )
        windows = observed.windows
        area = display_area(chosen)
        window = None
        if (
            self.state.view == "window"
            and foreground is not None
            and not foreground.shell
            and visible(foreground)
        ):
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
        text = self._publish(area, observed, chosen, label, window)
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
        observed: DesktopSnapshot,
        display: Display,
        label: str,
        window: WindowInfo | None = None,
        zoom: bool = False,
    ) -> str:
        displays, windows = observed.displays, observed.windows
        visible = self.access.window_visible
        size = fit(area[2] - area[0], area[3] - area[1])
        handles = [item.handle for item in windows if visible(item)]
        covered = self.target.capture_region(area, observed, handles, size)
        # Hidden names derive from the same stable observation as the pixels.
        _, hidden = mask(Image.new("RGB", size), area, windows, visible)
        path = publish_image(self.context, covered)
        if zoom:
            # For looking only: a zoom never becomes a coordinate basis, so Agents keep
            # one coordinate system and never convert between images.
            text = f"{label}. Coordinates still refer to the latest screenshot."
            if hidden:
                text += f" Hidden apps (gray, not approved): {', '.join(hidden)}."
            return text
        foreground = observed.foreground
        front = foreground.app if foreground is not None and visible(foreground) else None
        # Which app the image shows at each point, for the check before pointer input.
        stack = tuple(item for item in windows if overlaps(item, area))
        frame = Frame(
            display,
            *size,
            area,
            path.stem,
            tuple(displays),
            window,
            front,
            stack,
            foreground if front else None,
        )
        self.state.images[frame.screenshot_id] = frame
        self.state.latest_image = frame.screenshot_id
        self.state.shown = display
        # Keep references bounded without retaining image bytes in Session state.
        while len(self.state.images) > 64:
            del self.state.images[next(iter(self.state.images))]
        # No size, ratio or reduction: Agents used each to convert coordinates by hand
        # instead of using the position where they saw the target.
        text = f'{label}, screenshot_id="{frame.screenshot_id}".'
        if window is not None:
            text += f' window_id="{self.state.remember_window(window)}".'
        if hidden:
            text += f" Hidden apps (gray, not approved): {', '.join(hidden)}."
        return text

    def zoom(self, frame: Frame, action: Action) -> str:
        """Publish a fresh capture of a screenshot's region, for looking only."""
        assert action.region is not None
        observed = self.target.snapshot([])
        self.validate_frame(frame, observed)
        return self._publish(
            frame.region(*action.region),
            observed,
            frame.display,
            f'Zoom of {list(action.region)} in screenshot "{frame.screenshot_id}"',
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

    def window(self, identifier: str) -> WindowInfo:
        previous = self.state.windows.get(identifier)
        current = next(
            (
                item
                for item in self.target.windows()
                if previous is not None and item.same_window(previous)
            ),
            None,
        )
        if current is None:
            raise CallRefusedError(
                "window_unavailable",
                f'Window "{identifier}" is unavailable in this Session or has closed. '
                "Nothing was done. Use computer_apps with action windows, "
                "or take a new screenshot.",
            )
        self._allow(Action("inspect"), current, "The selected window")
        return current

    def windows(self, query: object = None) -> str:
        folded = query.strip().casefold() if isinstance(query, str) else ""
        lines = []
        for window in self.target.windows():
            if not self.access.window_visible(window) or window.shell:
                continue
            if folded and folded not in (window.app.name + " " + window.title).casefold():
                continue
            identifier = self.state.remember_window(window)
            lines.append(
                f"{identifier}: {json.dumps(window.app.name, ensure_ascii=False)} - "
                f"{json.dumps(window.title, ensure_ascii=False)}"
            )
            if len(lines) == 64:
                lines.append("Window limit reached; narrow the list with query.")
                break
        return "Open windows (window_id):\n" + ("\n".join(lines) or "No approved windows match.")

    def element_reference(self, action: Action) -> tuple[WindowInfo, dict[str, Any]]:
        found = self.state.elements.get(action.element_id or "")
        if found is None:
            raise CallRefusedError(
                "element_unavailable",
                f'Element "{action.element_id}" is unavailable in this Session. '
                "Nothing was done. Inspect the window again.",
            )
        old, reference = found
        window_id = action.window_id or self.state.remember_window(old)
        current = self.window(window_id)
        if not current.same_window(old):
            raise CallRefusedError(
                "invalid_arguments",
                (
                    "element_id and window_id name different windows. Nothing was "
                    "done. Use the element's window or inspect the intended window."
                ),
            )
        return current, reference

    def inspect(self, action: Action) -> str:
        window: WindowInfo | None
        if action.window_id is not None:
            window = self.window(action.window_id)
        else:
            frame = self.state.images.get(self.state.latest_image or "")
            previous = frame.window if frame is not None else None
            window = (
                self.window(self.state.remember_window(previous))
                if previous
                else self.target.foreground()
            )
        if window is None:
            raise CallRefusedError(
                "window_unavailable",
                (
                    "There is no window to inspect. Nothing was done. List windows or "
                    "open an app first."
                ),
            )
        self._allow(action, window, "The selected window")
        state = self.target.inspect(window)
        identifier = self.state.remember_window(window)
        lines = [
            f'UI elements of window_id="{identifier}" '
            f"({json.dumps(window.title, ensure_ascii=False)}):"
        ]
        focused = []
        for element in state.get("elements", []):
            reference = {
                key: element[key]
                for key in (
                    "runtime_id",
                    "generation",
                    "depth",
                    "name",
                    "control_type",
                    "automation_id",
                    "password",
                    "actions",
                )
            }
            element_id = self.state.remember_element(window, reference)
            if element.get("focused"):
                focused.append(element_id)
            details = {
                key: element[key]
                for key in (
                    "role",
                    "name",
                    "enabled",
                    "password",
                    "actions",
                    "value",
                    "text",
                    "selected_text",
                )
                if key in element
                and (key in {"value", "text", "selected_text"} or element[key] not in ("", []))
            }
            lines.append(
                "  " * min(int(element.get("depth", 0)), 12)
                + element_id
                + " "
                + json.dumps(details, ensure_ascii=False)
            )
        lines.append("Focused element: " + (", ".join(focused) or "none exposed") + ".")
        if state.get("truncated"):
            lines.append(
                "The accessibility result reached its size or time limit. Use the "
                "screenshot for the remaining controls."
            )
        if not state.get("elements"):
            lines.append("This window exposes no visible UI elements. Use screenshot coordinates.")
        return "\n".join(lines)

    def _element_action(self, action: Action) -> str:
        window, reference = self.element_reference(action)
        self.target.activate(window)
        if action.name == "left_click":
            current = self.target.element(window, reference, "resolve", "")
            if current.get("password") or not current.get("enabled") or current.get("offscreen"):
                raise CallRefusedError(
                    "element_not_supported",
                    (
                        "The element is protected, disabled or hidden. Nothing was done. "
                        "Use the screenshot or ask the user to make it available."
                    ),
                )
            left, top, right, bottom = current["bounds"]
            if right <= left or bottom <= top:
                raise CallRefusedError(
                    "element_not_supported",
                    (
                        "The element has no clickable area. Nothing was done. Use an "
                        "advertised action or the screenshot."
                    ),
                )
            x, y = (left + right) // 2, (top + bottom) // 2
            receiver = self.target.window_at(x, y)
            if receiver is None or not receiver.same_window(window):
                raise CallRefusedError(
                    "screen_changed",
                    "Another window covers the element. Nothing was clicked. Take a screenshot.",
                )
            self._allow(action, receiver, "The element's window")
            self.input_started = True
            self.target.click(x, y, "left", 1, [])
            self.clicked, self.clicked_window, self.keyed = window.app, window, False
        else:
            self.input_started = True
            self.target.element(window, reference, action.name, action.text)
            if action.name == "focus":
                self.clicked, self.clicked_window, self.keyed = window.app, window, False
        return action.describe()

    def act(self, action: Action, frame: Frame) -> str:
        """Check access for *action*, send its input and describe what was done.

        First comes the check that the screen still shows what the Agent's image
        showed: the app at each pointer point, or the app in front for keys.
        """
        if action.name == "left_mouse_up" and action.point is None:
            self.input_started = True
            self.target.button("left", False)
            return action.describe()
        if action.element_id is not None:
            return self._element_action(action)
        selected = self.window(action.window_id) if action.window_id is not None else None
        if selected is not None:
            if (
                action.frame_points()
                and frame.window is not None
                and not frame.window.same_window(selected)
            ):
                raise CallRefusedError(
                    "invalid_arguments",
                    (
                        "The screenshot and window_id name different windows. Nothing was "
                        "sent. Take a screenshot of the intended window first."
                    ),
                )
            self.target.activate(selected)
        points = self._points(action, frame)
        observed = self.target.snapshot(points)
        windows = observed.windows
        if action.frame_points() or (action.uses_pointer and action.screenshot_id is not None):
            self.validate_frame(frame, observed)
        if selected is not None:
            selected_receivers = observed.receivers if points else (observed.foreground,)
            if any(item is None or not item.same_window(selected) for item in selected_receivers):
                raise CallRefusedError(
                    "focus_changed",
                    (
                        "Input would reach a different window than window_id. Nothing was "
                        "sent. Take a screenshot or select the dialog from computer_apps "
                        "windows."
                    ),
                )
        if action.name in KEYBOARD and not self.keyed and selected is None:
            self._check_front(action, frame, observed.foreground)
        for point in points:
            self._check_owner(action, frame, point, windows)
        receivers = self._check_access(action, frame, points, observed)
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
            target.start_hold(list(action.chords[0]))
        if name in PRESSES:
            # The press activated the window under it; later keys of this call must go there.
            self.clicked = receivers[0].app
            self.clicked_window = receivers[0]
            self.keyed = False
        elif name in KEYBOARD:
            self.keyed = True
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

    def _check_access(
        self, action: Action, frame: Frame, points: list[Point], observed: DesktopSnapshot
    ) -> list[WindowInfo]:
        """Refuse unless the foreground window and the window at each point take input.

        Returns the window that receives the input at each point.
        """
        if not action.sends_input:
            return []
        foreground = observed.foreground
        if foreground is None:
            if not points:
                raise CallRefusedError(
                    "access_required",
                    f"No app window is in the foreground, so {action.name} was not sent. Bring "
                    'an app to the front with computer_apps {"action":"open","app":"..."}.',
                )
        else:
            self._allow(action, foreground, "The foreground window")
        receivers = []
        for point, window in zip(points, observed.receivers, strict=True):
            shown = list(frame.to_frame(*point))
            if window is None:
                raise CallRefusedError(
                    "access_required",
                    f"No app window is at {shown}, so {action.name} was not sent. Use "
                    "coordinates on an app window in that image.",
                )
            self._allow(action, window, f"The window at {shown}")
            receivers.append(window)
        return receivers

    def _check_owner(
        self, action: Action, frame: Frame, point: Point, windows: Sequence[WindowInfo]
    ) -> None:
        """Refuse pointer input where another window lies now than in the Agent's image.

        Both sides read the topmost window of a window stack, so a new screenshot
        always resolves a refusal. An owned popup of the same app, such as a menu,
        passes. Points outside the image, such as a pointer the image does
        not show, are not checked.
        """
        expected_window = next((item for item in frame.stack if item.contains(*point)), None)
        current_window = next((item for item in windows if item.contains(*point)), None)
        expected = frame.owner(*point)
        current = app_at(windows, *point)
        related = (
            current_window is not None
            and expected_window is not None
            and current_window.owner == expected_window.handle
            and current_window.app.matches(expected_window.app)
        )
        if (
            expected is None
            or current is None
            or (
                expected_window is not None
                and current_window is not None
                and (current_window.same_window(expected_window) or related)
            )
        ):
            return
        shown = list(frame.to_frame(*point))
        receiver = (
            f"another window of {current.name}" if current.matches(expected) else current.name
        )
        raise CallRefusedError(
            "screen_changed",
            f'Image "{frame.screenshot_id}" shows {expected.name} at {shown}, but '
            f"{receiver} is there now, so {action.name} was not sent. Take a screenshot to "
            f"see what is there now; to continue in {expected.name}, bring it to the front "
            "first.",
        )

    def _check_front(self, action: Action, frame: Frame, current: WindowInfo | None) -> None:
        """Refuse keys unless the app in front is the one the Agent expects them to reach.

        That is the app this call's last mouse button press went into, else the app
        in front in the image (the latest screenshot or the named one). Keys that
        follow other keys of this call are not checked: those keys can bring up
        another app themselves, such as the Start menu after win.
        """
        expected = self.clicked or frame.front
        expected_window = self.clicked_window or frame.front_window
        matches = current is not None and (
            current.same_window(expected_window)
            if expected_window is not None
            else expected is not None and current.app.matches(expected)
        )
        if expected is None or current is None or matches:
            return
        seen = (
            f"Your last click went into {expected.name}"
            if self.clicked is not None
            else f'{expected.name} was in front in image "{frame.screenshot_id}"'
        )
        receiver = (
            f"the foreground is now another window of {current.app.name}"
            if current.app.matches(expected)
            else f"the foreground window now belongs to {current.app.name}"
        )
        raise CallRefusedError(
            "focus_changed",
            f"{seen}, but {receiver}, so "
            f"{action.name} was not sent. Click into the window you want to type into, or take "
            "a screenshot to see what is in front.",
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
