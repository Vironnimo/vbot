"""Computer Use: operate the server host's desktop with screenshots, mouse and keyboard.

``ComputerUseService`` owns the lifecycle, the Agent permission check, the
access mode with its per-Session approvals and requests, call serialization on
one desktop worker thread, stop control and result assembly. Platform work happens behind
``DesktopTarget``; ``Desktop`` runs one call's work on the worker thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, Protocol

from core.extensions import ExtensionAPI
from core.extensions.operations import PENDING_INPUTS_RESOURCE, ExtensionHost
from core.tools import ToolContext, ToolDisplay, tool_failure, tool_success
from core.tools.availability import resolve_tool_access
from core.utils.ids import new_id

from ._access import (
    Access,
    AccessRequests,
    Sessions,
    SessionState,
    request_message,
    resolve_app,
    resolve_apps,
)
from ._actions import Action, ActionError, parse_action
from ._desktop import CallRefusedError, Desktop, request_call
from ._screens import describe_displays
from ._tools import (
    COMPUTER_APPS_DESCRIPTION,
    COMPUTER_APPS_PARAMETERS,
    COMPUTER_BATCH_DESCRIPTION,
    COMPUTER_BATCH_PARAMETERS,
    COMPUTER_DESCRIPTION,
    COMPUTER_PARAMETERS,
    RESULT_SCHEMA,
    normalize_apps,
    normalize_batch,
    normalize_computer,
)
from .target import AppInfo, DesktopTarget, InputInterrupted, TargetError

UNSUPPORTED = "Computer Use currently supports Windows server hosts."
READINESS_HINT = (
    "Computer Use works when the vBot server runs on Windows in a signed-in desktop session."
)
TOOL_FAMILY = "computer_use"
# After input, the screen gets this long to react before the result's screenshot.
SETTLE_SECONDS = 0.5
# Between two input actions of a batch, so menus and focus can follow.
STEP_SETTLE_SECONDS = 0.2
# After bringing an app to the front, which may launch it.
OPEN_SETTLE_SECONDS = 1.5
# A cached "not ready" is checked again at most this often.
READINESS_RECHECK_SECONDS = 30.0
# The activity sign stays up between the calls of a Run; without a call for this long
# (a Run that waits on something else, or one whose end was missed) it goes away.
ACTIVITY_IDLE_SECONDS = 120.0
_LIST_LIMIT = 25
ASK_SETTING = "ask_per_app"
_STOPPED_BY = {
    "control": "The user stopped Computer Use",
    "double_escape": "The user stopped Computer Use by pressing Esc twice",
    "run_cancel": "The Run was cancelled",
    "shutdown": "vBot is shutting down, which stopped Computer Use",
}


class Hotkey(Protocol):
    """The global double-Esc stop, armed only while a call runs."""

    available: bool

    def start(self) -> None: ...

    def set_armed(self, owner: object | None) -> None: ...

    def close(self) -> None: ...


async def _sleep(seconds: float, stop: asyncio.Event) -> bool:
    """Wait *seconds* unless *stop* is set first; return whether it was set."""
    try:
        async with asyncio.timeout(seconds):
            await stop.wait()
    except TimeoutError:
        return stop.is_set()
    return True


def _new_target() -> DesktopTarget:
    """Create the platform's desktop target; runs on the desktop worker thread."""
    if sys.platform == "win32":
        from .windows_target import WindowsTarget

        return WindowsTarget()
    raise TargetError(UNSUPPORTED, "computer_use_unavailable")


def _new_hotkey(callback: Callable[[object], None]) -> Hotkey:
    from .hotkey import EmergencyHotkey

    hotkey: Hotkey = EmergencyHotkey(callback)
    return hotkey


def _summary(arguments: dict[str, Any]) -> str | None:
    actions = arguments.get("actions")
    if not isinstance(actions, list):
        return None
    words = [
        str(item.get("action_summary") or item.get("action") or "?")
        for item in actions
        if isinstance(item, dict)
    ]
    return f"{len(actions)} actions: " + "; ".join(words)


def _apps_summary(arguments: dict[str, Any]) -> str | None:
    apps = arguments.get("apps")
    subject = arguments.get("app") or arguments.get("query")
    if isinstance(apps, list):
        subject = ", ".join(str(app) for app in apps)
    action = arguments.get("action")
    return f"{action} · {subject}" if action and subject else None


class ComputerUseService:
    """Desktop access for Agents: permission, approvals, serialization and stop control."""

    def __init__(self, api: ExtensionAPI) -> None:
        self.api = api
        self.host: ExtensionHost | None = None
        self.sessions = Sessions()
        self.access = AccessRequests(self._inputs_changed)
        self._target: DesktopTarget | None = None
        self._unready: str | None = "Computer Use is starting."
        self._checked = 0.0
        self._rechecking = False
        self._tasks: set[asyncio.Task[None]] = set()
        self._executor: ThreadPoolExecutor | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._hotkey: Hotkey | None = None
        self._closed = False
        # Serializes whole Tool calls; a batch is atomic.
        self._lock = asyncio.Lock()
        # The target checks this between input events; ``_stopped`` wakes pauses.
        self._stop = threading.Event()
        self._stopped = asyncio.Event()
        self._control_lock = threading.RLock()
        self._active: str | None = None
        self._stop_source: str | None = None
        self._control_revision = 0
        self._inputs_revision = 0
        # The Run whose left_mouse_down still holds the left button.
        self._held_run: str | None = None
        # Runs that took the desktop since the activity sign went up.
        self._activity_runs: set[str] = set()
        self._activity_shown = False
        self._activity_timer: asyncio.TimerHandle | None = None

    # Lifecycle

    async def start(self, host: ExtensionHost) -> None:
        self.host = host
        self._loop = asyncio.get_running_loop()
        self._executor = ThreadPoolExecutor(1, thread_name_prefix="computer-use")
        target: DesktopTarget | None = None
        try:
            target = await self._on_worker(_new_target)
            await self._on_worker(target.set_stop_event, self._stop)
            reason = await self._on_worker(target.readiness)
        except TargetError as error:
            reason = str(error)
        except Exception:
            self.api.logger.exception("Computer Use could not start its desktop target")
            target, reason = None, "Computer Use could not start on this host."
        self._target, self._unready, self._checked = target, reason, time.monotonic()
        if reason is not None:
            self.api.logger.info("Computer Use is not ready: %s", reason)

    async def close(self) -> None:
        self._closed = True
        self.stop(source="shutdown")
        self.access.cancel_all()
        self._end_activity()
        if self._hotkey is not None:
            self._hotkey.close()
        executor, target = self._executor, self._target
        if executor is not None:
            if target is not None:
                try:
                    release = asyncio.get_running_loop().run_in_executor(
                        executor, target.release_all
                    )
                    await asyncio.wait_for(release, 2)
                except Exception:
                    self.api.logger.warning("Computer Use could not release input", exc_info=True)
                try:
                    target.close()
                except Exception:
                    self.api.logger.warning(
                        "Computer Use could not close its target", exc_info=True
                    )
            executor.shutdown(wait=False, cancel_futures=True)
        self.sessions.clear()

    async def run_end(self, context: Any, **_: Any) -> None:
        """Release a mouse button the ending Run left pressed; end its activity sign."""
        run_id = getattr(context, "run_id", None)
        if run_id in self._activity_runs:
            self._activity_runs.discard(run_id)
            if not self._activity_runs:
                self._end_activity()
        if self._held_run is None or self._held_run != run_id or self._target is None:
            return
        async with self._lock:
            if self._held_run == run_id and not self._closed:
                self._held_run = None
                try:
                    await self._on_worker(self._target.release_all)
                except Exception:
                    self.api.logger.warning("Computer Use could not release input", exc_info=True)

    def ready(self) -> bool:
        if self._closed or self._target is None:
            return False
        if self._unready is not None:
            self._recheck_soon()
        return self._unready is None

    def _recheck_soon(self) -> None:
        """Schedule a readiness check of the target; never blocks the caller."""
        loop = self._loop
        if loop is None or self._rechecking:
            return
        if time.monotonic() - self._checked < READINESS_RECHECK_SECONDS:
            return
        self._rechecking = True
        try:
            loop.call_soon_threadsafe(self._spawn_recheck)
        except RuntimeError:
            self._rechecking = False

    def _spawn_recheck(self) -> None:
        task = asyncio.ensure_future(self._recheck())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _recheck(self) -> None:
        try:
            if self._target is not None and not self._closed:
                self._readiness(await self._on_worker(self._target.readiness))
        except Exception:
            self.api.logger.warning("Computer Use readiness check failed", exc_info=True)
        finally:
            self._checked = time.monotonic()
            self._rechecking = False

    def _readiness(self, reason: str | None) -> None:
        if reason != self._unready:
            self.api.logger.info("Computer Use readiness changed: %s", reason or "ready")
        self._unready = reason
        self._checked = time.monotonic()

    async def _on_worker[T](self, function: Callable[..., T], *args: Any) -> T:
        """Run *function* on the desktop worker thread."""
        executor = self._executor
        if executor is None or self._closed:
            raise TargetError("Computer Use has stopped.", "computer_use_unavailable")
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, functools.partial(function, *args))

    # Stop control

    def stop(self, owner: object | None = None, *, source: str = "control") -> None:
        """Stop the active call (only *owner*'s, when given); safe from any thread."""
        with self._control_lock:
            active = self._active
            if active is None or self._stop_source is not None:
                return
            if owner is not None and owner != active:
                return
            self._stop_source = source
            self._stop.set()
            if self._hotkey is not None:
                self._hotkey.set_armed(None)
            loop, stopped = self._loop, self._stopped
            if loop is not None:
                # A closed loop has nothing waiting any more.
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(stopped.set)
            self.api.logger.debug("Computer Use call stopped (source=%s)", source)
            self._control_changed(active)
            if loop is not None:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._end_activity)

    async def control(self, arguments: dict[str, Any]) -> dict[str, Any]:
        action = arguments.get("action", "status")
        with self._control_lock:
            if (
                action == "stop"
                and self._active is not None
                and arguments.get("call_id") == self._active
            ):
                self.stop(source="control")
            return {
                "available": self.ready(),
                "active": self._active is not None,
                "stopping": self._active is not None and self._stop_source is not None,
                "hotkey_available": self._hotkey is not None and self._hotkey.available,
                **({"call_id": self._active} if self._active is not None else {}),
            }

    def _control_changed(self, call_id: str) -> None:
        """Tell accessors that the ``control`` status changed; ``_control_lock`` is held."""
        host = self.host
        if self._closed or host is None or host.publish_change is None:
            return
        self._control_revision += 1
        try:
            host.publish_change("control", [call_id], self._control_revision)
        except ValueError:
            # A retired registration invalidates every Extension surface itself.
            self.api.logger.debug("Computer Use change not published: registration retired")

    def _inputs_changed(self, request_id: str) -> None:
        """Tell accessors that the pending inputs gained or lost *request_id*."""
        host = self.host
        if self._closed or host is None or host.publish_change is None:
            return
        self._inputs_revision += 1
        try:
            host.publish_change(PENDING_INPUTS_RESOURCE, [request_id], self._inputs_revision)
        except ValueError:
            self.api.logger.debug("Pending input change not published: registration retired")

    async def respond(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.access.respond(arguments.get("request_id"), arguments.get("response"))

    # Activity sign

    def _begin_activity(self, run_id: str | None) -> None:
        """Show the sign for a call that takes the desktop; it stays until the Run ends."""
        if self._activity_timer is not None:
            self._activity_timer.cancel()
            self._activity_timer = None
        self._activity_runs.add(run_id or "")
        if not self._activity_shown and self._target is not None:
            self._activity_shown = True
            self._target.set_activity(True)

    def _call_done(self) -> None:
        if self._activity_shown and self._loop is not None:
            self._activity_timer = self._loop.call_later(ACTIVITY_IDLE_SECONDS, self._end_activity)

    def _end_activity(self) -> None:
        if self._activity_timer is not None:
            self._activity_timer.cancel()
            self._activity_timer = None
        self._activity_runs.clear()
        if self._activity_shown and self._target is not None:
            self._activity_shown = False
            self._target.set_activity(False)

    @asynccontextmanager
    async def _active_call(self, context: ToolContext) -> AsyncIterator[None]:
        """Hold the desktop for one call: stoppable, with the hotkey armed."""
        async with self._lock:
            owner = new_id("ctl")
            self._loop = asyncio.get_running_loop()
            if self._hotkey is None:
                # The global keyboard hook starts with the first call that needs it.
                hotkey = _new_hotkey(lambda armed: self.stop(armed, source="double_escape"))
                await asyncio.to_thread(hotkey.start)
                self._hotkey = hotkey
            with self._control_lock:
                self._active, self._stop_source = owner, None
                self._stop.clear()
                self._stopped = asyncio.Event()
                if self._hotkey is not None:
                    self._hotkey.set_armed(owner)
                self._control_changed(owner)
            context.on_cancel(lambda: self.stop(owner, source="run_cancel"))
            try:
                if context.is_cancelled() or context.was_cancelled_by_user():
                    self.stop(owner, source="run_cancel")
                else:
                    self._begin_activity(context.run_id)
                yield
            finally:
                self._call_done()
                with self._control_lock:
                    if self._active == owner:
                        if self._hotkey is not None:
                            self._hotkey.set_armed(None)
                        self._active = None
                        self._control_changed(owner)

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise InputInterrupted()

    async def _pause(self, seconds: float) -> None:
        """Wait *seconds*, ending early with ``InputInterrupted`` on a stop."""
        self._check_stop()
        if seconds > 0:
            await _sleep(seconds, self._stopped)
        self._check_stop()

    def _stopped_text(self) -> str:
        return _STOPPED_BY.get(self._stop_source or "control", _STOPPED_BY["control"])

    # Calls

    async def _handle(
        self,
        context: ToolContext,
        tool: str,
        work: Callable[[Access, Any, Desktop], Awaitable[str]],
    ) -> dict[str, Any]:
        """Run one Tool call: permission and Session checks, then *work*, as an envelope."""
        desktop: Desktop | None = None
        try:
            state, agent = await self._begin(context, tool)
            assert self._target is not None
            access = Access(state, ask=self.asks_per_app())
            desktop = Desktop(self._target, access, context)
            content = await work(access, agent, desktop)
        except ActionError as error:
            return self._failed(tool, "invalid_arguments", str(error))
        except CallRefusedError as refusal:
            return self._failed(tool, refusal.code, str(refusal))
        except InputInterrupted:
            return self._failed(
                tool,
                "computer_use_interrupted",
                f"{self._stopped_text()}; held keys and buttons were released. Do not continue "
                "operating the computer unless the user asks you to.",
            )
        except TargetError as error:
            return self._failed(tool, error.code, _target_text(error, desktop))
        except Exception as error:
            self.api.logger.exception("Computer Use %s call failed", tool)
            return self._failed(
                tool,
                "computer_use_failed",
                f"Computer Use failed unexpectedly ({type(error).__name__}). "
                + _sent_text(desktop),
            )
        self.api.logger.debug("Computer Use %s call succeeded", tool)
        return tool_success({"content": content})

    def asks_per_app(self) -> bool:
        """Whether the user approves each app per Session; read live from the settings."""
        return self.api.get_config().get(ASK_SETTING) is True

    def _failed(self, tool: str, code: str, message: str) -> dict[str, Any]:
        self.api.logger.debug("Computer Use %s call failed (code=%s)", tool, code)
        return tool_failure(code, message, retryable=False)

    async def _begin(self, context: ToolContext, tool: str) -> tuple[SessionState, Any]:
        """Refuse the call unless Computer Use is ready and this Agent may use *tool*."""
        registry = self.api.operations.tool_registry
        stopped = "Computer Use has stopped. Retry after Extensions have reloaded."
        if self._closed:
            raise CallRefusedError("computer_use_unavailable", stopped)
        if self._target is None or self._unready is not None:
            raise CallRefusedError("computer_use_unavailable", self._unready or UNSUPPORTED)
        if self.host is None or registry is None:
            raise CallRefusedError("computer_use_unavailable", stopped)
        if not context.session_id:
            raise CallRefusedError(
                "computer_use_unavailable",
                "Computer Use works only inside a Session. Nothing was done.",
            )
        try:
            agent = self.host.resolve_tool_agent(context)
        except ValueError as error:
            raise CallRefusedError(
                "computer_use_unavailable",
                f"Computer Use cannot identify the calling Agent ({error}), so it cannot check "
                "that this Agent may use the computer. Nothing was done. Tell the user that "
                "Computer Use is unavailable for this Agent.",
            ) from error
        allowed = resolve_tool_access(
            agent.tool_access,
            registry.list_tools(),
            agent.memory_prompt_mode,
            workspace=str(agent.workspace or ""),
        ).allowed_tools
        if tool not in allowed:
            raise CallRefusedError(
                "tool_not_allowed",
                f"This Agent may not use the {tool} Tool. Nothing was done. Ask the user to "
                f"allow {tool} for this Agent.",
            )
        if context.is_cancelled() or context.was_cancelled_by_user():
            raise CallRefusedError(
                "computer_use_interrupted", "The Run was cancelled. Nothing was done."
            )
        reason = await self._on_worker(self._target.readiness)
        self._readiness(reason)
        if reason is not None:
            raise CallRefusedError("computer_use_unavailable", reason)
        key = (context.project_id, context.agent_id, context.session_id)
        return self.sessions.use(key), agent

    async def computer(self, context: ToolContext, arguments: dict[str, Any]) -> dict[str, Any]:
        async def work(access: Access, agent: Any, desktop: Desktop) -> str:
            action = parse_action(arguments)
            async with self._active_call(context):
                return await self._computer(context, desktop, action)

        return await self._handle(context, "computer", work)

    async def _computer(self, context: ToolContext, desktop: Desktop, action: Action) -> str:
        exact = bool(action.frame_points()) or action.region is not None
        frame = await self._on_worker(desktop.frame, exact)
        desktop.check_bounds(action, frame)
        lines: list[str] = []
        try:
            if action.name == "screenshot":
                lines.append(
                    await self._on_worker(desktop.screenshot, action.scale, action.display)
                )
            elif action.name == "zoom":
                lines.append(await self._on_worker(desktop.zoom, frame, action))
            elif action.name == "cursor_position":
                lines.append(await self._on_worker(desktop.cursor, frame))
            elif action.name == "wait":
                await self._pause(action.seconds)
                lines.append(f"Waited {action.seconds:g} s.")
                lines.append(await self._on_worker(desktop.screenshot))
            else:
                self._check_stop()
                lines.append(f"Done: {await self._act(context, desktop, action, frame)}.")
                if action.name != "left_mouse_down":
                    await self._pause(SETTLE_SECONDS)
                    lines.append(await self._on_worker(desktop.screenshot))
        except InputInterrupted:
            sent = desktop.input_started and action.sends_input
            raise TargetError(
                f"{self._stopped_text()} during {action.describe()}"
                + ("; it may have been partly sent" if sent else "")
                + ". Held keys and buttons were released. Do not continue operating the "
                "computer unless the user asks you to.",
                "computer_use_interrupted",
            ) from None
        return "\n".join([*lines, *action.notes])

    async def _act(self, context: ToolContext, desktop: Desktop, action: Action, frame: Any) -> str:
        done = await self._on_worker(desktop.act, action, frame)
        if action.name == "left_mouse_down":
            self._held_run = context.run_id
        elif action.name == "left_mouse_up":
            self._held_run = None
        return done

    async def computer_batch(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        async def work(access: Access, agent: Any, desktop: Desktop) -> str:
            actions = []
            for number, item in enumerate(arguments["actions"], start=1):
                try:
                    actions.append(parse_action(item))
                except ActionError as error:
                    raise ActionError(f"Action {number}: {error} Nothing was run.") from None
            async with self._active_call(context):
                return await self._batch(context, desktop, actions)

        return await self._handle(context, "computer_batch", work)

    async def _batch(self, context: ToolContext, desktop: Desktop, actions: list[Action]) -> str:
        exact = any(action.frame_points() or action.region is not None for action in actions)
        frame = await self._on_worker(desktop.frame, exact)
        for number, action in enumerate(actions, start=1):
            try:
                desktop.check_bounds(action, frame)
            except CallRefusedError as refusal:
                raise CallRefusedError(
                    refusal.code, f"Action {number}: {refusal} Nothing was run."
                ) from None
        lines: list[str] = []
        sent = False
        number = 0
        try:
            for number, action in enumerate(actions, start=1):
                if action.sends_input and sent:
                    await self._pause(STEP_SETTLE_SECONDS)
                self._check_stop()
                lines.append(f"{number}. {await self._step(context, desktop, action, frame)}")
                lines.extend(f"   {note}" for note in action.notes)
                sent = sent or action.sends_input
        except (CallRefusedError, TargetError) as error:
            raise _batch_error(error, actions, number, lines, self._stopped_text()) from None
        if sent and actions[-1].name != "screenshot":
            try:
                await self._pause(SETTLE_SECONDS)
            except InputInterrupted:
                raise TargetError(
                    f"{self._stopped_text()} after all {len(actions)} actions ran, before the "
                    "final screenshot. Do not continue operating the computer unless the user "
                    "asks you to.\n" + "\n".join(lines),
                    "computer_use_interrupted",
                ) from None
            lines.append(await self._on_worker(desktop.screenshot))
        return f"Ran {len(actions)} actions:\n" + "\n".join(lines)

    async def _step(
        self, context: ToolContext, desktop: Desktop, action: Action, frame: Any
    ) -> str:
        if action.name == "screenshot":
            return await self._on_worker(desktop.screenshot, action.scale)
        if action.name == "zoom":
            return await self._on_worker(desktop.zoom, frame, action)
        if action.name == "cursor_position":
            return await self._on_worker(desktop.cursor, frame)
        if action.name == "wait":
            await self._pause(action.seconds)
            return action.describe()
        return await self._act(context, desktop, action, frame)

    async def computer_apps(
        self, context: ToolContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        async def work(access: Access, agent: Any, desktop: Desktop) -> str:
            action = arguments.get("action")
            if action == "list":
                return await self._on_worker(_listing, desktop, arguments.get("query"))
            if action == "request":
                return await self._request(context, access, agent, arguments)
            return await self._open(context, access, desktop, arguments.get("app"))

        return await self._handle(context, "computer_apps", work)

    async def _request(
        self, context: ToolContext, access: Access, agent: Any, arguments: dict[str, Any]
    ) -> str:
        names = [name for name in arguments.get("apps") or [] if isinstance(name, str)]
        if not names:
            raise ActionError(
                'request needs "apps", for example {"action":"request","apps":["Notepad"],'
                '"reason":"Write the meeting notes"}.'
            )
        if not access.ask:
            return (
                "No approval is needed: you may operate every app. Bring one to the front with "
                f'computer_apps {{"action":"open","app":"{names[0]}"}}.'
            )
        assert self._target is not None
        resolution = resolve_apps(names, await self._on_worker(self._target.apps))
        notes = " ".join(resolution.problems)
        if not resolution.apps:
            raise ActionError(f"{notes} Nothing was requested.")
        new = [app for app in resolution.apps if not access.state.granted(app)]
        if not new:
            return f"Already approved in this Session: {access.state.describe()}. {notes}".strip()
        message = request_message(str(agent.name), new, arguments.get("reason"))
        answer = await self._ask(context, message)
        self.api.logger.info(
            "Computer Use access %s (apps=%d)",
            "granted" if answer == "accept" else answer,
            len(new),
        )
        listed = ", ".join(app.name for app in new)
        if answer == "timeout":
            raise CallRefusedError(
                "access_declined",
                f"Nobody answered the access request for {listed} within 5 minutes, so it counts "
                "as declined. Ask the user in chat before requesting these apps again.",
            )
        if answer != "accept":
            raise CallRefusedError(
                "access_declined",
                f"The user declined access to {listed}. Continue without these apps or ask the "
                "user how to proceed; do not request them again unless the user asks you to.",
            )
        state = self.sessions.use((context.project_id, context.agent_id, context.session_id))
        for app in new:
            state.add(app)
        return (
            f"The user approved {listed} in this Session.\n"
            + (f"{notes}\n" if notes else "")
            + f'Next: bring the app to the front with computer_apps {{"action":"open","app":'
            f'"{new[0].name}"}}, or take a screenshot with computer {{"action":"screenshot"}}.'
        )

    async def _ask(self, context: ToolContext, message: str) -> str:
        """Wait for the user's answer; a cancelled Run withdraws the request."""
        waiter = asyncio.ensure_future(self.access.ask(context.session_id, message))
        loop = asyncio.get_running_loop()

        def withdraw() -> None:
            # A closed loop has ended the request already.
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(waiter.cancel)

        context.on_cancel(withdraw)
        if context.is_cancelled() or context.was_cancelled_by_user():
            waiter.cancel()
        try:
            return await waiter
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            raise CallRefusedError(
                "computer_use_interrupted",
                "The Run was cancelled, so the access request was withdrawn. Nothing was granted.",
            ) from None

    async def _open(self, context: ToolContext, access: Access, desktop: Desktop, name: Any) -> str:
        if not isinstance(name, str) or not name.strip():
            raise ActionError('open needs "app", for example {"action":"open","app":"Notepad"}.')
        assert self._target is not None
        match = resolve_app(name, await self._on_worker(self._target.apps))
        if isinstance(match, str):
            raise ActionError(match)
        if not access.allows(match):
            raise CallRefusedError(
                "access_required",
                f"The user has not approved {match.name} in this Session. Ask for it first "
                f"with {request_call(match.name)}.",
            )
        async with self._active_call(context):
            try:
                desktop.input_started = True
                await self._on_worker(self._target.open, match)
                await self._pause(OPEN_SETTLE_SECONDS)
                shot = await self._on_worker(desktop.screenshot)
            except InputInterrupted:
                raise TargetError(
                    f"{self._stopped_text()} while {match.name} was opening.",
                    "computer_use_interrupted",
                ) from None
        return f"Opened {match.name}.\n{shot}"


def _listing(desktop: Desktop, query: Any) -> str:
    """The text of ``computer_apps list``; runs on the desktop worker thread."""
    target, access = desktop.target, desktop.access
    apps = target.apps()
    if not access.ask:
        lines = ["Access: you may operate every app; no approval is needed."]
    elif access.state.grants:
        lines = [
            "Access: the user approves each app for this Session. Approved: "
            f"{access.state.describe()}."
        ]
    else:
        lines = [
            "Access: the user approves each app for this Session, and none is approved yet. "
            'Ask with computer_apps {"action":"request","apps":["..."],"reason":"..."}.'
        ]
    lines.append("Displays:\n" + describe_displays(target.displays(), access.state.shown))

    def described(app: AppInfo) -> str:
        return app.name if access.allows(app) else f"{app.name} (not approved)"

    running = sorted({app.name: app for app in apps if app.running}.values(), key=_name)
    lines.append("Running apps: " + (", ".join(described(app) for app in running) or "none") + ".")
    if isinstance(query, str) and query.strip():
        text = query.strip().casefold()
        found = sorted(
            {app.name: app for app in apps if text in app.name.casefold()}.values(), key=_name
        )
        shown = ", ".join(described(app) for app in found[:_LIST_LIMIT])
        more = (
            f", and {len(found) - _LIST_LIMIT} more; use a longer query"
            if len(found) > _LIST_LIMIT
            else ""
        )
        lines.append(f'Installed apps matching "{query.strip()}": {shown or "none"}{more}.')
    else:
        lines.append('Search installed apps with {"action":"list","query":"..."}.')
    return "\n".join(lines)


def _name(app: AppInfo) -> str:
    return app.name.casefold()


def _sent_text(desktop: Desktop | None) -> str:
    if desktop is not None and desktop.input_started:
        return "Input may have been sent; take a screenshot before repeating it."
    return "No input was sent."


def _target_text(error: TargetError, desktop: Desktop | None) -> str:
    message = str(error).rstrip()
    if error.code == "computer_use_failed":
        return f"{message} {_sent_text(desktop)}"
    return message


def _batch_error(
    error: CallRefusedError | TargetError,
    actions: list[Action],
    number: int,
    lines: list[str],
    stopped: str,
) -> Exception:
    """The error of a batch stopped at action *number*, naming the steps that ran."""
    action = actions[number - 1]
    ran = (
        "Actions that ran (take a screenshot to see the current state):\n" + "\n".join(lines)
        if lines
        else "No action ran before it."
    )
    rest = len(actions) - number
    skipped = f" The remaining {rest} did not run." if rest else ""
    if isinstance(error, InputInterrupted):
        message = (
            f"{stopped} during action {number} of {len(actions)} ({action.describe()}).{skipped} "
            "Held keys and buttons were released. Do not continue operating the computer "
            f"unless the user asks you to.\n{ran}"
        )
        return TargetError(message, "computer_use_interrupted")
    detail = str(error).rstrip()
    if isinstance(error, TargetError) and error.code == "computer_use_failed":
        detail += " Input may have been sent." if action.sends_input else " No input was sent."
    message = f"Action {number} of {len(actions)} ({action.name}) failed: {detail}{skipped}\n{ran}"
    if isinstance(error, CallRefusedError):
        return CallRefusedError(error.code, message)
    return TargetError(message, error.code)


def register(api: ExtensionAPI) -> None:
    service = ComputerUseService(api)
    api.register_settings(
        [
            {
                "key": ASK_SETTING,
                "type": "toggle",
                "label": "Ask before each app",
                "description": (
                    "When on, an Agent asks you in the WebUI for each app it wants to use in a "
                    "Session, and apps you have not approved are hidden from it. When off, every "
                    "Agent allowed to use Computer Use operates all apps without asking."
                ),
                "default": False,
            }
        ]
    )
    api.operations.startup.append(service.start)
    api.operations.pending_inputs = service.access.list
    api.operations.input_response_operation = "respond"
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
    api.operations.register(
        "respond",
        "Answer one pending Computer Use access request with accept, decline or cancel.",
        {
            "type": "object",
            "properties": {"request_id": {"type": "string"}, "response": {"type": "object"}},
            "required": ["request_id", "response"],
            "additionalProperties": False,
        },
        service.respond,
        secret=True,
    )
    api.on_shutdown(service.close)
    api.on("run_end", service.run_end)
    api.register_tool_family(TOOL_FAMILY, "Computer Use")
    shared: dict[str, Any] = {
        "requires_opt_in": True,
        "parallel_safe": False,
        "ready": service.ready,
        "readiness_hint": READINESS_HINT,
        "result_schema": RESULT_SCHEMA,
        "family": TOOL_FAMILY,
    }
    api.register_tool(
        "computer",
        COMPUTER_DESCRIPTION,
        COMPUTER_PARAMETERS,
        service.computer,
        display=ToolDisplay(summary_fields=("action", "action_summary")),
        argument_normalizer=normalize_computer,
        **shared,
    )
    api.register_tool(
        "computer_batch",
        COMPUTER_BATCH_DESCRIPTION,
        COMPUTER_BATCH_PARAMETERS,
        service.computer_batch,
        display=ToolDisplay(summary_builder=_summary),
        argument_normalizer=normalize_batch,
        **shared,
    )
    api.register_tool(
        "computer_apps",
        COMPUTER_APPS_DESCRIPTION,
        COMPUTER_APPS_PARAMETERS,
        service.computer_apps,
        display=ToolDisplay(summary_fields=("action",), summary_builder=_apps_summary),
        argument_normalizer=normalize_apps,
        **shared,
    )


__all__ = ["ComputerUseService", "register"]
