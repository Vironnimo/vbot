"""Per-Run screenshots and element refs, and how a call selects one of them."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from . import observations
from ._arguments import (
    _ELEMENT,
    _POINTER,
    _TARGET,
    _TARGETED,
    _WINDOW,
    InvalidComputerArgumentsError,
    _describe_target,
    _target,
    _target_fields,
    call_text,
    capture_call,
)
from .driver import ComputerUseError
from .observations import Observation

# Actions that only observe; with several current screenshots they follow the newest.
_READS = {"wait", "verify"}


@dataclass
class DesktopSession:
    name: str
    observations: dict[tuple[Any, ...], Observation] = field(default_factory=dict)
    views: dict[str, Observation] = field(default_factory=dict)
    # The newest view per target, which is a zoom crop when one followed the capture.
    latest: dict[tuple[Any, ...], str] = field(default_factory=dict)
    resolutions: dict[tuple[Any, ...], str] = field(default_factory=dict)
    foregrounds: dict[tuple[Any, ...], bool] = field(default_factory=dict)
    issued_elements: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    retired_views: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    observation_data: dict[tuple[Any, ...], dict[str, Any]] = field(default_factory=dict)
    # The target of the newest capture, kept after input replaces its screenshot.
    last_target: tuple[Any, ...] | None = None


def _explicit_target(arguments: dict[str, Any]) -> tuple[Any, ...] | None:
    fields = _TARGET & arguments.keys()
    if not fields or any(type(arguments[key]) is not int for key in fields):
        return None
    if fields & _WINDOW and not arguments.keys() >= _WINDOW:
        return None
    return _target(arguments)


def _uses_points(arguments: dict[str, Any], steps: list[dict[str, Any]]) -> bool:
    if arguments.get("action") in _POINTER and "coordinate" in arguments:
        return True
    return any(step.get("action") in _POINTER and "coordinate" in step for step in steps)


def with_latest_view(session: DesktopSession | None, arguments: Any) -> Any:
    """Name the screenshot or target a call leaves implicit, or explain why none fits.

    Coordinates without view_id refer to the target's only current screenshot. A call
    without pid, window_id, monitor, view_id or element continues with the Run's only
    current screenshot; with several, input must say which one, while a read (wait,
    verify) continues with the newest. Without any current screenshot such a call means
    the desktop, unless the newest capture showed a window: then input must name it
    and a read continues with it.
    """
    if not isinstance(arguments, dict):
        return arguments
    action = arguments.get("action")
    if action not in _TARGETED or action == "capture" or "view_id" in arguments:
        return arguments
    raw_steps = arguments.get("steps") if action == "sequence" else None
    steps = [step for step in raw_steps if isinstance(step, dict)] if raw_steps else []
    if any("view_id" in step for step in steps):
        return arguments
    points = _uses_points(arguments, steps)
    if _TARGET & arguments.keys():
        target = _explicit_target(arguments)
        if not points or target is None:
            return arguments
        observation = session.observations.get(target) if session else None
        if observation is None or observation.view_id is None:
            raise ComputerUseError(
                f"Coordinates need a screenshot of {_describe_target(target)}, and it has no "
                "current one (input or a newer capture replaces screenshots). No input was sent. "
                f"Capture it with {capture_call(target, arguments.get('foreground'))} and "
                "measure the coordinates in the returned image.",
                "capture_required",
            )
        _check_no_newer_crop(session, observation)
        return {**arguments, "view_id": observation.view_id}
    element = arguments.get("element", steps[0].get("element") if steps else None)
    if isinstance(element, str) and ":" in element:
        # A complete element token names its own window capture.
        return arguments
    current = list(session.observations.values()) if session else []
    last = session.last_target if session else None
    if not current and element is not None:
        return arguments
    if not current and last is not None and last[0] == "window":
        if action in _READS and not points:
            return {**arguments, **_target_fields(last)}
        foreground = session.foregrounds.get(last) if session else None
        continuation = (
            f"Capture that window with {capture_call(last, foreground)} and measure the "
            "coordinates in the new image"
            if points
            else f"Add {call_text(_target_fields(last))[1:-1]} to continue with that window"
        )
        raise ComputerUseError(
            f"The latest screenshot showed {_describe_target(last)}, and input replaced it; "
            "without pid and window_id this call would go to the desktop instead. No input "
            f"was sent. {continuation}, or capture the desktop with "
            '{"action":"capture"} first for desktop input.',
            "capture_required",
        )
    if not current:
        if points:
            raise ComputerUseError(
                "Coordinates need a screenshot, and this Run has no current one (input or a "
                "newer capture replaces screenshots). No input was sent. Capture a window "
                'with {"action":"capture","pid":<pid>,"window_id":<window_id>} using ids from '
                'windows, or the desktop with {"action":"capture"}, then measure the '
                "coordinates in the returned image.",
                "capture_required",
            )
        return arguments
    if len(current) > 1 and not points and action in _READS:
        # Reads cannot misdirect input; the newest capture is the one being worked on.
        return {**arguments, **_target_fields(current[-1].target)}
    if len(current) > 1:
        listed = "; ".join(
            f"view {observation.view_id} of {_describe_target(observation.target)}"
            if observation.view_id
            else f"elements of {_describe_target(observation.target)}"
            for observation in current
        )
        raise InvalidComputerArgumentsError(
            f"Several screenshots are current ({listed}). No input was sent. Add view_id, or "
            "pid and window_id, to say which target this call is for."
        )
    observation = current[0]
    if not points:
        return {**arguments, **_target_fields(observation.target)}
    if observation.view_id is None:
        raise ComputerUseError(
            f"The latest capture of {_describe_target(observation.target)} has element refs but "
            "no image, so coordinates cannot be measured in it. No input was sent. Use an "
            f"element ref, or capture an image with "
            f"{capture_call(observation.target, observation.foreground)}.",
            "capture_required",
        )
    _check_no_newer_crop(session, observation)
    return {**arguments, "view_id": observation.view_id}


def _check_no_newer_crop(session: DesktopSession | None, observation: Observation) -> None:
    if session is None:
        return
    latest = session.latest.get(observation.target)
    if latest and latest != observation.view_id and latest in session.views:
        raise InvalidComputerArgumentsError(
            f"The latest image of {_describe_target(observation.target)} is the zoom crop "
            f"{latest} of view {observation.view_id}, so coordinates without view_id are "
            f'ambiguous. No input was sent. Add "view_id":"{latest}" for coordinates measured '
            f'in the crop, or "view_id":"{observation.view_id}" for coordinates in the full '
            "screenshot."
        )


def stale_view_error(
    session: DesktopSession | None, view_id: str, foreground: Any = None
) -> ComputerUseError:
    """Explain an unusable view_id and name the view or capture that replaces it.

    An explicit foreground request is kept in the named capture call.
    """
    target = session.retired_views.get(view_id) if session else None
    if target is None:
        return ComputerUseError(
            f"view_id {json.dumps(view_id)} is not a screenshot from this Run. No input was "
            "sent. Capture the target (pid and window_id from windows, or the desktop without "
            "them) and use the view_id it returns.",
            "stale_view",
        )
    current = session.observations.get(target) if session else None
    if current is not None and current.view_id:
        return ComputerUseError(
            f"View {view_id} is outdated: input or a newer capture of "
            f"{_describe_target(target)} replaced it. No input was sent. Its current "
            f"screenshot is view {current.view_id}; measure the coordinates in that image and "
            f'repeat with "view_id":"{current.view_id}", or capture again.',
            "stale_view",
        )
    if not isinstance(foreground, bool):
        foreground = session.foregrounds.get(target) if session else None
    return ComputerUseError(
        f"View {view_id} is outdated: input or a newer capture replaced it, and "
        f"{_describe_target(target)} has no current screenshot. No input was sent. Capture it "
        f"with {capture_call(target, foreground)} and measure the coordinates in the new image.",
        "stale_view",
    )


def _reference(session: DesktopSession | None, arguments: dict[str, Any]) -> Observation | None:
    candidates = [arguments]
    if isinstance(arguments.get("steps"), list):
        candidates.extend(item for item in arguments["steps"] if isinstance(item, dict))
    reference = None
    for candidate in candidates:
        view_id = candidate.get("view_id")
        if isinstance(view_id, str):
            reference = session.views.get(view_id) if session else None
            if reference is None:
                raise stale_view_error(session, view_id, arguments.get("foreground"))
            break
    element = arguments.get("element")
    if element is None and arguments.get("action") == "sequence" and len(candidates) > 1:
        element = candidates[1].get("element")
    if isinstance(element, str) and _ELEMENT.fullmatch(element) and session:
        selected = reference
        if (
            selected is None
            and arguments.keys() >= _WINDOW
            and all(type(arguments[key]) is int for key in _WINDOW)
        ):
            selected = session.observations.get(_target(arguments))
        if selected is None and ":" in element:
            matches = [
                obs for obs in session.observations.values() if element in obs.elements.values()
            ]
            if len(matches) > 1:
                raise InvalidComputerArgumentsError(
                    "This element ref occurs in more than one current window. "
                    "Supply pid and window_id or that window's view_id."
                )
            selected = matches[0] if matches else None
        if selected is not None and (
            element in selected.elements or element in selected.elements.values()
        ):
            return reference or selected
        if element in session.issued_elements:
            raise ComputerUseError(
                "This element belongs to an earlier observation. Input or a new capture "
                "replaced its refs. No input was sent. Use the current observation or capture "
                "with mode=som; do not construct a replacement token.",
                "stale_element",
            )
        raise ComputerUseError(
            "This element is not in your current observations. No input was sent. Supply a "
            "complete returned element ref, or capture the intended pid and window_id with "
            "mode=som.",
            "unknown_element",
        )
    return reference


def _observation(
    session: DesktopSession, target: tuple[Any, ...], view_id: str | None = None
) -> observations.Observation:
    if view_id is not None:
        view = session.views.get(view_id)
        if view is None:
            raise stale_view_error(session, view_id)
        if view.target != target:
            raise InvalidComputerArgumentsError(
                "The sequence contains views from different targets or delivery settings. "
                "Use one target and one delivery setting per sequence."
            )
        return view
    observation = session.observations.get(target)
    if observation is None:
        raise ComputerUseError(
            f"{_describe_target(target).capitalize()} has no current capture. No input was "
            f"sent. Capture it with {capture_call(target, session.foregrounds.get(target))} "
            "before sending input.",
            "capture_required",
        )
    return observation


def _outcome(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload[key]
        for key in ("effect", "verified", "escalation", "target_became_foreground")
        if key in payload and (key != "target_became_foreground" or payload[key])
    }
