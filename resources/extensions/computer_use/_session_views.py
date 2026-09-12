"""Session views."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import observations
from ._arguments import _ELEMENT, _WINDOW, InvalidComputerArgumentsError, _target
from .driver import ComputerUseError
from .observations import Observation


@dataclass
class DesktopSession:
    name: str
    observations: dict[tuple[Any, ...], Observation] = field(default_factory=dict)
    views: dict[str, Observation] = field(default_factory=dict)
    resolutions: dict[tuple[Any, ...], str] = field(default_factory=dict)
    foregrounds: dict[tuple[Any, ...], bool] = field(default_factory=dict)
    issued_elements: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    retired_views: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    observation_data: dict[tuple[Any, ...], dict[str, Any]] = field(default_factory=dict)


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
                raise ComputerUseError(
                    "This view is stale. Capture the target again or zoom the current view.",
                    "stale_view",
                )
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
                    "Supply pid and window_id or that window's view_id.",
                    "invalid_arguments",
                )
            selected = matches[0] if matches else None
        if selected is not None and (
            element in selected.elements or element in selected.elements.values()
        ):
            return reference or selected
        if element in session.issued_elements:
            raise ComputerUseError(
                "This element belongs to an earlier observation. Input or a new capture "
                "replaced its refs. Use the current observation or capture with mode=som; "
                "do not construct a replacement token.",
                "stale_element",
            )
        raise ComputerUseError(
            "This element is not in your current observations. Supply a complete returned "
            "element ref, or capture the intended pid and window_id with mode=som.",
            "unknown_element",
        )
    return reference


def _observation(
    session: DesktopSession, target: tuple[Any, ...], view_id: str | None = None
) -> observations.Observation:
    if view_id is not None:
        view = session.views.get(view_id)
        if view is None:
            raise ComputerUseError(
                "This view is stale. Capture the target again or zoom the current view.",
                "stale_view",
            )
        if view.target != target:
            raise ComputerUseError(
                "The sequence contains views from different targets or delivery settings. "
                "Use one target and one delivery setting per sequence.",
                "invalid_arguments",
            )
        return view
    observation = session.observations.get(target)
    if observation is None:
        raise ComputerUseError(
            "Capture this target again before sending input.", "capture_required"
        )
    return observation


def _outcome(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: payload[key]
        for key in ("effect", "verified", "escalation", "target_became_foreground")
        if key in payload and (key != "target_became_foreground" or payload[key])
    }
