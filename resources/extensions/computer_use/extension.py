"""Native window and desktop control with owned observations and bounded execution."""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from typing import Any

from core.extensions import ExtensionAPI
from core.extensions.operations import ExtensionHost
from core.tools import ToolContext, ToolDisplay, tool_failure, tool_success
from core.tools.availability import resolve_tool_access
from core.utils.ids import new_id

from . import observations
from ._arguments import (
    _BACKGROUND_FOCUS_HINT,
    _FIELDS,
    _MUTATIONS,
    _NO_EFFECT_HINT,
    _POST_INPUT_OBSERVATION_MS,
    _READINESS_HINT,
    _TARGET,
    _WINDOW,
    COMPUTER_DESCRIPTION,
    COMPUTER_PARAMETERS,
    InvalidComputerArgumentsError,
    _invalid,
    _target,
    _target_fields,
    _validate_arguments,
)
from ._session_views import (
    DesktopSession,
    _observation,
    _outcome,
    _reference,
)
from .driver import ComputerUseError, ComputerUseInterruptedError, CuaDriver, EmergencyHotkey

__all__ = [
    "COMPUTER_DESCRIPTION",
    "COMPUTER_PARAMETERS",
    "ComputerUseService",
    "DesktopSession",
    "InvalidComputerArgumentsError",
    "register",
]


class ComputerUseService:
    "Own authority, connection lifetime, observations, and ordered input end to end."

    def __init__(self, api: ExtensionAPI) -> None:
        self.api = api
        self.host: ExtensionHost | None = None
        self.executable = shutil.which("cua-driver")
        self._lock = threading.RLock()
        self._sessions: dict[tuple[str | None, str, str, str], DesktopSession] = {}
        self._driver: CuaDriver | None = None
        self._closed = False
        self._control_lock = threading.RLock()
        self._wake = threading.Event()
        self._active: str | None = None
        self._interrupted: object | None = None
        self._active_driver: CuaDriver | None = None
        self._active_context: ToolContext | None = None
        self._hotkey = EmergencyHotkey(lambda owner: self.stop(owner, source="double_escape"))

    async def start(self, host: ExtensionHost) -> None:
        self.host = host
        if self.executable:
            self._hotkey.start()

    def stop(self, owner: object | None = None, *, source: str = "control") -> None:
        # This lock is never held while waiting for a Driver call or the service lock.
        with self._control_lock:
            if self._active is None or self._interrupted is self._active:
                return
            if owner is not None and self._active != owner:
                return
            self._interrupted = self._active
            self._hotkey.set_armed(None)
            self._wake.set()
            driver = self._active_driver
            if driver is not None:
                try:
                    driver.interrupt()
                except OSError:
                    # Further steps still stop even if the OS refuses termination.
                    driver.broken = True
                    self.api.logger.exception("Could not interrupt the Computer Use worker")
            context = self._active_context
            self.api.logger.info(
                "Computer Use call interrupted (source=%s run=%s tool_call=%s)",
                source,
                context.run_id if context is not None else None,
                context.tool_call_id if context is not None else None,
            )

    async def control(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = arguments.get("action", "status")
        with self._control_lock:
            context = self._active_context
            if (
                action == "stop"
                and context is not None
                and arguments.get("call_id") == self._active
            ):
                self.stop()
            return {
                "available": self.ready(),
                "active": self._active is not None,
                "stopping": self._active is not None and self._interrupted is self._active,
                "hotkey_available": self._hotkey.available,
                **({"call_id": self._active} if context is not None else {}),
            }

    def ready(self) -> bool:
        return bool(self.executable) and not self._closed

    def _client(self) -> CuaDriver:
        if not self.executable:
            raise ComputerUseError(
                "Computer Use is unavailable. Install cua-driver on the server host "
                "and reload Extensions."
            )
        if self._driver is not None and self._driver.broken:
            self._driver.close()
            self._driver = None
            self._sessions.clear()
        if self._driver is None:
            self._driver = CuaDriver(self.executable)
        return self._driver

    def _check_access(self, context: ToolContext) -> None:
        if self._active is not None and (
            self._interrupted is self._active or self._hotkey.pending_owner is self._active
        ):
            raise ComputerUseInterruptedError()
        if self._closed or self.host is None or self.api.operations.tool_registry is None:
            raise ComputerUseError(
                "Computer Use has stopped. Retry after Extensions have reloaded."
            )
        if not context.session_id or not context.run_id:
            raise ComputerUseError(
                "Computer Use requires a Session. Start a Session before calling this Tool."
            )
        agent = self.host.resolve_agent(context.project_id, context.agent_id)
        allowed = resolve_tool_access(
            agent.tool_access,
            self.api.operations.tool_registry.list_tools(),
            agent.memory_prompt_mode,
            workspace=str(agent.workspace or ""),
        ).allowed_tools
        if "computer" not in allowed:
            raise ComputerUseError(
                "Computer Use is not permitted for this Agent. Ask the user to grant "
                "the computer Tool."
            )
        if context.is_cancelled() or context.was_cancelled_by_user():
            raise ComputerUseInterruptedError()

    def _call(
        self, context: ToolContext, session: DesktopSession, name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        self._check_access(context)
        client = self._client()
        client.connect()
        self._check_access(context)
        payload = dict(args)
        if "session" in client.schemas.get(name, {}).get("properties", {}):
            payload["session"] = session.name
        try:
            result = client.call(name, payload)
        except ComputerUseError:
            self._check_access(context)
            raise
        return result

    def _invalidate(self, target: tuple[Any, ...] | None = None) -> None:
        for session in self._sessions.values():
            session.retired_views.update(
                (key, view.target)
                for key, view in session.views.items()
                if target is None or view.target == target
            )
            while len(session.retired_views) > 32:
                del session.retired_views[next(iter(session.retired_views))]
            if target is None:
                session.observations.clear()
                session.views.clear()
                session.observation_data.clear()
            else:
                session.observations.pop(target, None)
                session.observation_data.pop(target, None)
                session.views = {
                    key: view for key, view in session.views.items() if view.target != target
                }

    @staticmethod
    def _remember(
        session: DesktopSession, observation: observations.Observation, *, crop: bool = False
    ) -> None:
        if not crop:
            session.observations[observation.target] = observation
            session.issued_elements.update(
                (token, observation.target) for token in observation.elements.values()
            )
            while len(session.issued_elements) > 2000:
                del session.issued_elements[next(iter(session.issued_elements))]
        if observation.view_id:
            session.views[observation.view_id] = observation
        # Cropping is read-only: retain the parent and recent sibling crops.
        while len(session.views) > 16:
            del session.views[next(iter(session.views))]

    def _observe(
        self,
        context: ToolContext,
        session: DesktopSession,
        target: tuple[Any, ...],
        args: dict[str, Any],
    ) -> dict[str, Any]:
        # A replacement snapshot also invalidates coordinates held by another Run.
        self._invalidate(target)
        mode = args.get("mode", "vision")
        requested_target = target
        if target[0] == "window":
            resolved = self._call(context, session, "resolve_window", _target_fields(target))
            target = _target(resolved)
            self._invalidate(target)
        payload: dict[str, Any]
        if target[0] == "window":
            request = {
                **_target_fields(target),
                "include_screenshot": mode != "ax",
                "max_elements": args.get("limit", 200),
            }
            if args.get("query"):
                request["query"] = args["query"]
            if not args["foreground"]:
                request["_background_capture"] = True
            name = "capture_pixels" if mode == "vision" else "get_window_state"
            payload = self._call(context, session, name, request)
        else:
            payload = self._call(context, session, "get_desktop_state", _target_fields(target))
        observation, result = observations.capture(
            context, target, payload, mode=mode, resolution=args.get("resolution", "auto")
        )
        observation.foreground = args["foreground"]
        session.resolutions[target] = observation.resolution
        session.foregrounds[target] = observation.foreground
        self._remember(session, observation)
        result.update(target=_target_fields(target), foreground=observation.foreground, mode=mode)
        if requested_target != target:
            result["requested_target"] = _target_fields(requested_target)
        session.observation_data[target] = result
        return result

    def _reference_failure(
        self,
        context: ToolContext,
        session: DesktopSession | None,
        arguments: dict[str, Any],
        error: ComputerUseError,
    ) -> dict[str, Any]:
        # Only the requesting Run's retained observations can supply recovery context.
        self._check_access(context)
        result = tool_failure(error.code, str(error), retryable=False)
        if session is None:
            return result
        target = _target(arguments) if _TARGET & arguments.keys() else None
        steps = arguments.get("steps")
        candidates = [arguments, *(steps if isinstance(steps, list) else [])]
        # Conflicting explicit targets must not repurpose an owned stale reference.
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            view_id = candidate.get("view_id")
            view = session.views.get(view_id) if isinstance(view_id, str) else None
            owned_target = (
                view.target
                if view
                else session.retired_views.get(view_id)
                if isinstance(view_id, str)
                else None
            )
            if owned_target is None and isinstance(candidate.get("element"), str):
                owned_target = session.issued_elements.get(candidate["element"])
            if owned_target is not None:
                if target is not None and target != owned_target:
                    return result
                target = owned_target
        retained = session.observation_data.get(target) if target is not None else None
        if retained is not None:
            result["artifacts"].append(
                {
                    "kind": "computer_observation",
                    "applied": False,
                    "observation": retained,
                    "observation_note": (
                        "No input was sent. This is the retained observation, not a new capture. "
                        "Capture its target again for fresh state, or use its returned refs "
                        "if the application has not changed."
                    ),
                }
            )
        elif target is not None:
            # A retired reference can explain recovery, but never authorize input.
            recovery_args: dict[str, Any] = {
                "action": "capture",
                **_target_fields(target),
                "foreground": arguments.get(
                    "foreground", session.foregrounds.get(target, target[0] != "window")
                ),
                "resolution": arguments.get("resolution", session.resolutions.get(target, "auto")),
                "mode": arguments.get(
                    "mode",
                    "som"
                    if "query" in arguments
                    or "limit" in arguments
                    or any("element" in candidate for candidate in candidates)
                    else "vision",
                ),
                **{key: arguments[key] for key in ("query", "limit") if key in arguments},
            }
            result["artifacts"].append(
                {
                    "kind": "computer_observation",
                    "applied": False,
                    **self._recovery(context, recovery_args, error),
                }
            )
        return result

    def _recovery(
        self,
        context: ToolContext,
        args: dict[str, Any],
        error: Exception | None = None,
    ) -> dict[str, Any]:
        """Return a read-only next call without reviving an observation or sending input."""
        if args["action"] in {"status", "close", "launch", "apps", "windows", "monitors"}:
            return {}
        try:
            self._check_access(context)
        except ComputerUseError:
            return {}
        target = _target_fields(_target(args))
        code = error.code if isinstance(error, ComputerUseError) else None
        recovery: dict[str, Any] = {"action": "capture", **target, "foreground": args["foreground"]}
        if args.get("mode", "vision") != "vision":
            recovery["mode"] = args["mode"]
            for field in ("query", "limit"):
                if field in args:
                    recovery[field] = args[field]
        if args.get("resolution", "auto") != "auto":
            recovery["resolution"] = args["resolution"]
        if code == "stale_window":
            recovery = {"action": "windows"}
        elif code in {"target_not_foreground", "focus_refused", "window_not_visible"}:
            return {
                "target": target,
                "foreground": args["foreground"],
                "recovery": {"action": "capture"},
                "recovery_note": (
                    "The next observation is the desktop. Call computer with recovery as the "
                    "complete arguments, without adding the window target or an old view_id. "
                    "Select the intended window there before capturing it with foreground=true."
                ),
            }
        return {"target": target, "foreground": args["foreground"], "recovery": recovery}

    def _mutation(
        self,
        context: ToolContext,
        session: DesktopSession,
        args: dict[str, Any],
        observation: observations.Observation | None,
    ) -> dict[str, Any]:
        action = args["action"]
        if action == "launch":
            return self._call(context, session, "launch_app", {"name": args["app"]})
        target = _target(args)
        payload = _target_fields(target)
        payload["delivery_mode"] = "foreground" if args["foreground"] else "background"
        if "duration_ms" in args:
            payload["duration_ms"] = args["duration_ms"]
        if "modifiers" in args:
            payload["modifiers"] = args["modifiers"]
        if "element" in args:
            assert observation is not None
            payload["element_token"] = observation.token(args["element"])
        if "coordinate" in args and "view_id" in args:
            assert observation is not None
            if observation.foreground != args["foreground"]:
                raise ComputerUseError(
                    "Capture this target with the requested foreground setting before "
                    "coordinate input.",
                    "capture_required",
                )
            x, y = observation.point(args["view_id"], *args["coordinate"])
            if action == "drag":
                x2, y2 = observation.point(args["view_id"], *args["to_coordinate"])
                payload.update(from_x=x, from_y=y, to_x=x2, to_y=y2)
            else:
                payload.update(x=x, y=y)
        if action == "move":
            name = "move_cursor"
        elif action == "click":
            name = "click"
            fields = self._client().schemas.get(name, {}).get("properties", {})
            if "button" in fields:
                payload["button"] = args["button"]
                payload["count"] = args["count"]
            elif args["button"] == "right" and args["count"] == 1:
                name = "right_click"
            elif args["button"] == "left" and args["count"] == 2:
                name = "double_click"
            elif args["button"] != "left":
                _invalid("button")
        elif action in {"type", "set_value"}:
            name = "type_text" if action == "type" else "set_value"
            payload["text" if action == "type" else "value"] = args["text"]
            if "text_mode" in args:
                payload["text_mode"] = args["text_mode"]
        elif action == "key":
            keys = [key.strip().lower() for key in args["shortcut"].split("+") if key.strip()]
            name = "press_key" if len(keys) == 1 else "hotkey"
            payload["key" if len(keys) == 1 else "keys"] = keys[0] if len(keys) == 1 else keys
        elif action == "scroll":
            name = "scroll"
            payload.update(direction=args["direction"], amount=args["amount"])
        elif action == "drag":
            name = "drag"
            payload["button"] = args["button"]
            if not args["foreground"]:
                payload.setdefault("duration_ms", 250)
        elif action == "menu":
            name = "invoke_menu"
            payload["path"] = args["menu_path"]
        elif action == "resize":
            name = "set_window_frame"
            payload.update(
                zip(
                    ("x", "y", "width", "height"), (*args["coordinate"], *args["size"]), strict=True
                )
            )
        else:
            _invalid("action")
        if (
            name in {"set_value", "invoke_menu", "set_window_frame"}
            and "delivery_mode" not in self._client().schemas.get(name, {}).get("properties", {})
            and payload.pop("delivery_mode", None) == "background"
        ):
            payload["_background_input"] = True
        return self._call(context, session, name, payload)

    def _after_input(
        self,
        context: ToolContext,
        session: DesktopSession,
        target: tuple[Any, ...],
        args: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        session.resolutions[target] = args["resolution"]
        if result.get("applied"):
            session.foregrounds[target] = args["foreground"]
        if not args["capture_after"] and not result.get("partial"):
            try:
                self._check_access(context)
            except ComputerUseInterruptedError as error:
                return {
                    **result,
                    "partial": True,
                    "error": {"code": error.code, "message": str(error)},
                    "next_action": str(error),
                }
            return {
                **result,
                **self._recovery(context, args),
                "next_action": result.get(
                    "next_action",
                    "Input was dispatched without a new observation. Use recovery to observe "
                    "before further input.",
                ),
            }
        try:
            # SendInput and UIA acknowledgements do not await application rendering.
            # Do not infer completion from changing or quiet pixels (animations/carets).
            self._wait(context, {"duration_ms": _POST_INPUT_OBSERVATION_MS})
            result["observation"] = self._observe(context, session, target, args)
            result["observation_delay_ms"] = _POST_INPUT_OBSERVATION_MS
            if result.get("applied"):
                result["observation_note"] = (
                    "Input was dispatched. This observation does not confirm that application "
                    "work has finished. If the expected result is missing, use wait or verify "
                    "before repeating input."
                )
        except Exception as error:
            if not isinstance(error, (ComputerUseError, OSError)):
                self.api.logger.exception("Computer Use observation failed")
                error = ComputerUseError(
                    "The observation failed unexpectedly. Capture the target before further input.",
                    "observation_failed",
                )
            result.update(
                observation_error={
                    "code": error.code
                    if isinstance(error, ComputerUseError)
                    else "observation_failed",
                    "message": str(error),
                },
                **self._recovery(context, args, error),
            )
            if result.get("applied"):
                result["next_action"] = (
                    str(error)
                    if "recovery" not in result
                    else "Input was dispatched but its result could not be observed. Use "
                    "recovery to inspect the current state before deciding what remains."
                )
        return result

    def _sequence(
        self, context: ToolContext, session: DesktopSession, args: dict[str, Any]
    ) -> dict[str, Any]:
        target = _target(args)
        initial = _observation(session, target, args.get("view_id"))
        step_observations = []
        for step in args["steps"]:
            observation = (
                _observation(session, target, step["view_id"]) if "view_id" in step else initial
            )
            step_observations.append(observation)
            if "view_id" in step and observation.foreground != args["foreground"]:
                raise ComputerUseError(
                    "Capture this target with the requested foreground setting before "
                    "coordinate input.",
                    "capture_required",
                )
            if "coordinate" in step:
                observation.point(step["view_id"], *step["coordinate"])
                if step["action"] == "drag":
                    observation.point(step["view_id"], *step["to_coordinate"])
            if "element" in step:
                observation.token(step["element"])
        completed = 0
        result: dict[str, Any] = {
            "action": "sequence",
            "applied": False,
            "completed_steps": 0,
            "total_steps": len(args["steps"]),
            "step_results": [],
        }
        for index, (step, observation) in enumerate(
            zip(args["steps"], step_observations, strict=True)
        ):
            try:
                self._check_access(context)
                step_args = {
                    **{key: value for key, value in args.items() if key != "view_id"},
                    **step,
                }
                step_args.setdefault("button", "left")
                step_args.setdefault("count", 1)
                self._invalidate()
                if step["action"] == "wait":
                    self._wait(context, step_args)
                    outcome = {"effect": "waited"}
                else:
                    outcome = self._mutation(context, session, step_args, observation)
                completed += 1
                result["step_results"].append(
                    {
                        "step": index + 1,
                        "action": step["action"],
                        **_outcome(outcome),
                    }
                )
                if outcome.get("target_became_foreground"):
                    raise ComputerUseError(_BACKGROUND_FOCUS_HINT, "background_focus_changed")
                if outcome.get("effect") in {"suspected_noop", "partial"}:
                    raise ComputerUseError(_NO_EFFECT_HINT, "effect_uncertain")
            except Exception as error:
                if not isinstance(error, ComputerUseError):
                    self.api.logger.exception("Computer Use input step failed")
                    error = ComputerUseError(
                        (
                            "Input stopped unexpectedly and may have partial effects. Inspect"
                            " the observation before deciding what remains; do not replay "
                            "completed steps."
                        ),
                        "computer_use_failed",
                    )
                result.update(
                    partial=True,
                    stopped_step=index + 1,
                    error={"code": error.code, "message": str(error)},
                    next_action=(
                        "The sequence stopped. Inspect the completed step count and fresh "
                        "observation before continuing."
                    ),
                )
                break
        result.update(applied=completed > 0, completed_steps=completed)
        return self._after_input(context, session, target, args, result)

    def _wait(self, context: ToolContext, args: dict[str, Any]) -> None:
        deadline = time.monotonic() + args.get("duration_ms", 1000) / 1000
        while True:
            self._check_access(context)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            self._wake.wait(min(remaining, 0.05))

    def _verify(
        self, context: ToolContext, session: DesktopSession, args: dict[str, Any]
    ) -> dict[str, Any]:
        target = _target(args)
        deadline = time.monotonic() + args["timeout_ms"] / 1000
        result: dict[str, Any] = {}
        # Short bounded driver waits let cancellation/revocation interrupt a long verification.
        while True:
            remaining = max(0, round((deadline - time.monotonic()) * 1000))
            result = self._call(
                context,
                session,
                "verify_state",
                {
                    **_target_fields(target),
                    "expect": args["expect"],
                    "timeout_ms": min(remaining, 250),
                    "include_screenshot": False,
                },
            )
            if (
                result.get("status") in {"satisfied", "verified"}
                or result.get("satisfied") is True
                or result.get("verified") is True
            ):
                break
            if time.monotonic() >= deadline:
                result["next_action"] = (
                    "The requested state was not verified before the timeout. Inspect "
                    "the observation before continuing."
                )
                break
        verification = {"action": "verify", "verification": observations.bounded(context, result)}
        try:
            verification["observation"] = self._observe(context, session, target, args)
        except ComputerUseError as error:
            # A verified closed window cannot supply another window screenshot.
            verification["observation_error"] = {"code": error.code, "message": str(error)}
            verification.update(self._recovery(context, args, error))
        return verification

    def _execute(
        self, context: ToolContext, session: DesktopSession, args: dict[str, Any]
    ) -> dict[str, Any]:
        action = args["action"]
        if action in {"apps", "windows"}:
            payload = self._call(context, session, "list_" + action, {})
            fields = (
                ("name", "pid", "running", "active")
                if action == "apps"
                else ("pid", "window_id", "title", "app_name", "minimized", "is_on_screen")
            )
            items = [
                {key: item[key] for key in fields if key in item and item[key] is not None}
                for item in payload.get(action, [])
                if isinstance(item, dict)
            ]
            return {"action": action, **observations.bounded(context, {action: items})}
        if action == "capture":
            return {"action": action, **self._observe(context, session, _target(args), args)}
        if action == "monitors":
            return {"action": action, **self._call(context, session, "list_monitors", {})}
        if action == "zoom":
            target = _target(args)
            current = _observation(session, target, args["view_id"])
            zoomed, result = observations.zoom(
                context, current, args["view_id"], *args["coordinate"], *args["to_coordinate"]
            )
            self._remember(session, zoomed, crop=True)
            return {
                "action": action,
                **result,
                "target": _target_fields(target),
                "foreground": zoomed.foreground,
                "mode": "vision",
            }
        if action == "wait":
            self._invalidate()
            self._wait(context, args)
            return {"action": action, **self._observe(context, session, _target(args), args)}
        if action == "verify":
            return self._verify(context, session, args)
        if action in _MUTATIONS and not args["apply"]:
            # A preview never sends input and does not echo text or file contents.
            return {
                "action": action,
                "applied": False,
                "preview": True,
                "next_action": (
                    "Preview only; no input was sent. Repeat with apply=true to execute."
                ),
            }
        if action == "sequence":
            return self._sequence(context, session, args)
        if action == "launch":
            try:
                payload = self._mutation(context, session, args, None)
            finally:
                self._invalidate()
            return {"action": action, "applied": True, **observations.bounded(context, payload)}
        target = _target(args)
        observation = _observation(session, target, args.get("view_id"))
        # Resolve references before invalidating, including on uncertain input.
        if "element" in args:
            observation.token(args["element"])
        if "view_id" in args:
            if observation.foreground != args["foreground"]:
                raise ComputerUseError(
                    "Capture this target with the requested foreground setting before "
                    "coordinate input.",
                    "capture_required",
                )
            if "coordinate" in args:
                observation.point(args["view_id"], *args["coordinate"])
                if args["action"] == "drag":
                    observation.point(args["view_id"], *args["to_coordinate"])
        failure = None
        try:
            payload = self._mutation(context, session, args, observation)
        except Exception as error:
            if not isinstance(error, ComputerUseError):
                self.api.logger.exception("Computer Use input failed")
                error = ComputerUseError(
                    "Input stopped unexpectedly and may have partial effects. Inspect the "
                    "observation before deciding what remains; do not replay completed steps.",
                    "computer_use_failed",
                )
            failure = {
                "action": action,
                "applied": False,
                "partial": True,
                "error": {"code": error.code, "message": str(error)},
            }
            payload = {}
        finally:
            self._invalidate()
        if failure is not None:
            return self._after_input(context, session, target, args, failure)
        result = {"action": action, "applied": True, **_outcome(payload)}
        if payload.get("target_became_foreground"):
            result["next_action"] = _BACKGROUND_FOCUS_HINT
        elif payload.get("effect") in {"suspected_noop", "partial"}:
            result["next_action"] = _NO_EFFECT_HINT
        return self._after_input(context, session, target, args, result)

    def handle(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            key = (context.project_id, context.agent_id, context.session_id, context.run_id)
            session = self._sessions.get(key)
            try:
                reference = None
                reference_error = None
                if isinstance(arguments, dict):
                    try:
                        reference = _reference(session, arguments)
                    except ComputerUseError as error:
                        reference_error = error
                foreground = (
                    session.foregrounds.get(_target(arguments))
                    if session is not None
                    and isinstance(arguments, dict)
                    and ("pid" not in arguments or arguments.keys() >= _WINDOW)
                    and all(type(arguments[k]) is int for k in _TARGET & arguments.keys())
                    else None
                )
                args = _validate_arguments(
                    arguments,
                    reference,
                    foreground,
                    unresolved_reference=reference_error is not None,
                )
                if reference_error is not None:
                    raise reference_error
            except InvalidComputerArgumentsError as error:
                return tool_failure("invalid_arguments", str(error))
            except ComputerUseError as error:
                try:
                    return self._reference_failure(context, session, arguments, error)
                except ComputerUseError as denied:
                    return tool_failure(denied.code, str(denied), retryable=False)
            if "resolution" not in arguments and reference is None and session is not None:
                args["resolution"] = session.resolutions.get(_target(args), args["resolution"])
            owner = new_id("ctl")
            try:
                self._check_access(context)
                client = self._client()
                with self._control_lock:
                    self._active = owner
                    self._interrupted = None
                    self._active_driver = client
                    self._active_context = context
                    self._wake.clear()
                    if args["action"] not in {"status", "close"}:
                        self._hotkey.set_armed(owner)
                context.on_cancel(lambda: self.stop(owner, source="tool_cancel"))
                self._check_access(context)
                client.connect()
                self._check_access(context)
                # Selecting the client may retire a broken worker and its cached sessions.
                session = self._sessions.get(key)
                if args["action"] == "status":
                    return tool_success(
                        {
                            "action": "status",
                            "version": client.version,
                            "host": "server",
                            "actions": sorted(_FIELDS),
                        }
                    )
                if args["action"] == "close":
                    if session is not None:
                        self._call(context, session, "end_session", {})
                        del self._sessions[key]
                    return tool_success({"action": "close", "closed": True})
                if session is None:
                    session = DesktopSession("vbot-" + uuid.uuid4().hex)
                    self._call(context, session, "start_session", {})
                    self._sessions[key] = session
                self._check_access(context)
                result = self._execute(context, session, args)
                if args["action"] not in _MUTATIONS:
                    self._check_access(context)
                if result.get("error") and not result["applied"]:
                    failure = tool_failure(
                        result["error"]["code"],
                        (
                            "No sequence step completed successfully. Inspect the observation "
                            "before deciding whether to repeat input. "
                            if args["action"] == "sequence"
                            else ""
                        )
                        + result["error"]["message"],
                        retryable=False,
                    )
                    failure["artifacts"].append(
                        {
                            "kind": "computer_observation",
                            **{key: value for key, value in result.items() if key != "error"},
                        }
                    )
                    return failure
                return tool_success(result)
            except ComputerUseError as error:
                if self._driver is not None and self._driver.broken:
                    self._sessions.clear()
                failure = tool_failure(error.code, str(error), retryable=False)
                if recovery := self._recovery(context, args, error):
                    failure["artifacts"].append({"kind": "computer_observation", **recovery})
                return failure
            except Exception:
                self.api.logger.exception("Computer Use request failed")
                failure = tool_failure(
                    "computer_use_failed",
                    "Computer Use could not complete the request. Check the Extension "
                    "diagnostics and capture the window before repeating input.",
                    retryable=False,
                )
                if recovery := self._recovery(context, args):
                    failure["artifacts"].append({"kind": "computer_observation", **recovery})
                return failure
            finally:
                try:
                    if self._driver is not None and (self._driver.broken or not self._sessions):
                        if self._driver.broken:
                            self._sessions.clear()
                        driver, self._driver = self._driver, None
                        driver.close()
                finally:
                    with self._control_lock:
                        if self._active is owner:
                            self._hotkey.set_armed(None)
                            self._active = None
                            self._interrupted = None
                            self._active_driver = None
                            self._active_context = None

    def _close_sessions(self, run_id: str | None = None) -> None:
        if self._driver is not None and self._driver.broken:
            # Its owned worker has already stopped. Never reconnect during cleanup.
            self._sessions.clear()
        else:
            for key, session in list(self._sessions.items()):
                if run_id is not None and key[3] != run_id:
                    continue
                try:
                    self._client().call("end_session", {"session": session.name})
                except ComputerUseError:
                    self.api.logger.warning("Computer Use session cleanup failed", exc_info=True)
                    if self._driver is not None and self._driver.broken:
                        self._sessions.clear()
                        break
                else:
                    self._sessions.pop(key, None)
        # Cua also owns an implicit transport session used by discovery tools.
        # Retire it with the last Run instead of retaining idle/ended state.
        if not self._sessions and self._driver is not None:
            driver, self._driver = self._driver, None
            driver.close()

    def run_end(self, context: Any, **kwargs: Any) -> None:
        with self._lock:
            self._close_sessions(context.run_id)

    def close(self) -> None:
        self._closed = True
        self._hotkey.close()
        self.stop(source="shutdown")
        with self._lock:
            self._close_sessions()
            if self._driver is not None:
                self._driver.close()


def register(api: ExtensionAPI) -> None:
    service = ComputerUseService(api)
    api.operations.startup.append(service.start)
    api.operations.register(
        "control",
        "Inspect or interrupt the active Computer Use call on the server host.",
        {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["status", "stop"]},
                "call_id": {"type": "string", "minLength": 1},
            },
            "allOf": [
                {
                    "if": {"properties": {"action": {"const": "stop"}}, "required": ["action"]},
                    "then": {"required": ["call_id"]},
                }
            ],
            "additionalProperties": False,
        },
        service.control,
    )
    api.on_shutdown(service.close)
    api.on("run_end", service.run_end)
    api.register_tool(
        "computer",
        COMPUTER_DESCRIPTION,
        COMPUTER_PARAMETERS,
        service.handle,
        requires_opt_in=True,
        parallel_safe=False,
        open_input_schema=True,
        ready=service.ready,
        readiness_hint=_READINESS_HINT,
        display=ToolDisplay(summary_fields=("action", "pid", "window_id")),
        result_schema={"type": "object", "required": ["action"]},
    )
