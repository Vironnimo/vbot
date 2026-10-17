"""Session goals: the user's ``/goal`` and the automatic Runs that pursue it.

A goal lives in Session metadata (``SESSION_GOAL_META_KEY``). While it is
active, the end of every completed Run of its Session starts the next
automatic Run, until the Agent marks the goal complete, blocked or paused with
``update_goal``, or the kernel pauses it (a stop, a failed Run, the turn limit,
a Runtime restart or an Agent Takeover).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from core.chat import ReplySurface, reply_surface_from_note
from core.chat.commands import CommandExecutionContext, CommandFeedback, CommandOutcome, CommandRun
from core.chat.messages import REPLY_SURFACE_NOTE_PREFIX, ChatMessage, GoalEvent, MessageSender
from core.chat.wire_shaping import quote_external_value
from core.runs import ActiveRunError, ChatRunManager, Run, RunKind, RunStatus
from core.sessions import (
    SESSION_GOAL_META_KEY,
    SESSION_GOAL_NOTE_PREFIX,
    SessionAddress,
)
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.chat import ChatLoop
    from core.sessions import ChatSessionManager

_LOGGER = get_logger("automation.goals")

GoalStatus = Literal["active", "paused", "blocked", "complete"]
# What a goal row in the conversation reports (``GoalEvent.event``).
GoalEventKind = Literal[
    "set", "resumed", "reactivated", "turn", "blocked", "complete", "paused", "cleared"
]
AgentGoalStatus = Literal["complete", "blocked", "paused"]
PauseReason = Literal["user", "stop", "limit", "run_failed", "restart", "takeover", "agent"]

DEFAULT_GOAL_TURN_LIMIT = 30
MAX_GOAL_TURN_LIMIT = 200
# An automatic Run waits for running Sub-Agents and background commands whose
# result wakes the Session. A command that never ends, such as a dev server,
# would hold the goal forever, so the goal continues after this long anyway.
GOAL_WAIT_FALLBACK_SECONDS = 10 * 60

# Agent-facing reminders (reasons: ``.vorch/domain-maps/tools/update_goal.md``).
GOAL_SET_TEXT = (
    SESSION_GOAL_NOTE_PREFIX + "\n{objective}\n"
    "When your turn ends while the goal is active, vBot starts your next turn "
    "automatically, until you mark the goal complete or blocked with update_goal."
)
GOAL_CONTINUE_TEXT = (
    "Your previous turn ended while the goal is still active. "
    "Continue with the next step toward it."
)
GOAL_LAST_TURN_TEXT = (
    "Your previous turn ended while the goal is still active. This is the last "
    "automatic turn; after it the goal pauses until the user resumes it. Continue "
    "with the next step, then end your reply with what is done and what remains."
)
GOAL_RESUMED_TEXT = "The user resumed the goal. Continue with the next step toward it."
GOAL_PAUSED_TEXT = "The goal was paused because {reason}. The user can resume it with /goal resume."
_PAUSE_NOTE_REASONS: dict[str, str] = {
    "stop": "the user stopped your turn",
    "run_failed": "your turn failed",
    "limit": "it reached its limit of {limit} automatic turns",
    "restart": "vBot restarted",
    "takeover": "the session moved to another Agent",
}
# What ``/goal`` shows the user for each pause reason.
_PAUSE_STATUS_REASONS: dict[str, str] = {
    "user": "paused with /goal pause",
    "stop": "the turn was stopped",
    "limit": "it reached its limit of automatic turns",
    "run_failed": "a turn failed",
    "restart": "vBot restarted",
    "takeover": "the session moved to another agent",
    "agent": "the agent paused it",
}
_MAX_ARGUMENT = re.compile(r"(?:^|\s)--max(?:\s+|=)(\S+)\s*$")
_USAGE = "Use /goal <objective> [--max N], or /goal pause, /goal resume, /goal clear."


@dataclass(frozen=True)
class GoalUpdate:
    """What ``update_goal`` did: ``changed`` is False when the goal already had the status."""

    status: AgentGoalStatus
    changed: bool


class NoGoalError(LookupError):
    """The Session has no goal."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _address(run: Run) -> SessionAddress:
    return SessionAddress(run.project_id, run.agent_id, run.session_id)


def _goal(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not isinstance(value.get("objective"), str):
        return None
    if value.get("status") not in ("active", "paused", "blocked", "complete"):
        return None
    return value


class GoalService:
    """Own Session goals: their state, the ``/goal`` command and automatic Runs."""

    def __init__(
        self,
        *,
        sessions: ChatSessionManager,
        run_manager: ChatRunManager,
        chat_loop: ChatLoop,
        work_pending: Callable[[SessionAddress], Awaitable[bool]] | None = None,
    ) -> None:
        self._sessions = sessions
        self._run_manager = run_manager
        self._chat_loop = chat_loop
        self._work_pending = work_pending
        self._run_relay: Callable[[Run, ReplySurface], Awaitable[None]] | None = None
        self._changed_callbacks: list[Callable[[SessionAddress, bool], None]] = []
        # Automatic Runs: only they count against the limit and must make progress.
        self._deferred: dict[SessionAddress, asyncio.Task[None]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._unobserve: Callable[[], None] | None = None
        self._closed = False

    # -- Wiring ------------------------------------------------------------------

    def install(self) -> None:
        """Observe every Run, so the end of one in a Session with a goal is decided."""
        self._unobserve = self._run_manager.add_run_started_callback(self._run_started)

    def set_run_relay(self, relay: Callable[[Run, ReplySurface], Awaitable[None]] | None) -> None:
        """Route automatic Runs of a Channel conversation back to that conversation."""
        self._run_relay = relay

    def add_changed_callback(
        self, callback: Callable[[SessionAddress, bool], None]
    ) -> Callable[[], None]:
        """Call *callback* with the Session whenever its goal changes.

        The flag is True when the change also appended a goal row to the
        Session's History outside a Run, which a reader of the History lacks.
        """
        self._changed_callbacks.append(callback)

        def remove() -> None:
            if callback in self._changed_callbacks:
                self._changed_callbacks.remove(callback)

        return remove

    def pause_after_restart(self) -> int:
        """Pause every goal a previous Runtime left active; returns how many. Blocking."""
        addresses = self._sessions.live_addresses_with_metadata_field(
            SESSION_GOAL_META_KEY, "status", "active"
        )
        paused = 0
        for address in addresses:
            try:
                if self._pause_sync(address, "restart"):
                    paused += 1
            except Exception:
                _LOGGER.warning(
                    "Could not pause a goal after restart (agent=%s session=%s)",
                    address.agent_id,
                    address.session_id,
                    exc_info=True,
                )
        return paused

    async def aclose(self) -> None:
        self._closed = True
        if self._unobserve is not None:
            self._unobserve()
            self._unobserve = None
        tasks = [*self._tasks, *self._deferred.values()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._deferred.clear()

    # -- Queries -----------------------------------------------------------------

    async def goal(self, address: SessionAddress) -> dict[str, Any] | None:
        """The Session's goal as stored, or ``None``."""
        return _goal(await self._sessions.metadata_value_async(address, SESSION_GOAL_META_KEY))

    # -- Transitions ---------------------------------------------------------------

    async def update_from_agent(
        self, address: SessionAddress, status: AgentGoalStatus, *, run_id: str
    ) -> GoalUpdate:
        """Apply the Agent's ``update_goal`` call. Raises ``NoGoalError`` without a goal."""
        outcome: dict[str, bool] = {}

        def mutate(metadata: dict[str, Any]) -> None:
            goal = _goal(metadata.get(SESSION_GOAL_META_KEY))
            if goal is None:
                outcome["missing"] = True
                return
            if goal["status"] == status:
                outcome["changed"] = False
                return
            goal = {**goal, "status": status, "status_run_id": run_id, "updated_at": _now()}
            if status == "paused":
                goal["pause_reason"] = "agent"
            else:
                goal.pop("pause_reason", None)
            metadata[SESSION_GOAL_META_KEY] = goal
            outcome["changed"] = True

        await self._sessions.run_async(self._sessions.mutate_metadata, address, mutate)
        if outcome.get("missing"):
            raise NoGoalError(address.session_id)
        changed = outcome.get("changed", False)
        if changed:
            self._notify(address)
        return GoalUpdate(status=status, changed=changed)

    async def pause_for_stop(self, address: SessionAddress) -> bool:
        """Pause an active or blocked goal because the user stopped work; True if it changed."""
        return await self._pause(address, "stop")

    async def pause_for_takeover(self, address: SessionAddress) -> bool:
        """Pause the goal of a Session that just moved to another Agent."""
        return await self._pause(address, "takeover")

    async def _pause(self, address: SessionAddress, reason: PauseReason) -> bool:
        """Pause an open goal, write its note and announce the change."""
        previous = await self._sessions.run_async(self._pause_metadata, address, reason)
        if previous is None:
            return False
        recorded = await self._record(
            address, _pause_event(reason, previous), _pause_note(reason, previous) or ""
        )
        self._paused(address, reason, recorded=recorded)
        return True

    def _pause_sync(self, address: SessionAddress, reason: PauseReason) -> bool:
        """Pause like ``_pause`` while no Run can be active (Runtime start). Blocking."""
        previous = self._pause_metadata(address, reason)
        if previous is None:
            return False
        self._sessions.get(address).add_note(
            _pause_note(reason, previous) or "", goal_event=_pause_event(reason, previous)
        )
        self._paused(address, reason, recorded=True)
        return True

    async def _record(self, address: SessionAddress, event: GoalEvent, text: str = "") -> bool:
        """Append a goal row to the History; *text*, if any, is what the Model reads.

        Returns whether the row was written; a failure is logged, since the goal
        change itself already happened.
        """
        try:
            async with self._sessions.write_lock(address):
                session = await self._sessions.get_async(address)
                await session.add_note_async(text, goal_event=event)
        except Exception:
            _LOGGER.warning(
                "Could not record a goal change (agent=%s session=%s event=%s)",
                address.agent_id,
                address.session_id,
                event.event,
                exc_info=True,
            )
            return False
        return True

    def _pause_metadata(
        self, address: SessionAddress, reason: PauseReason
    ) -> dict[str, Any] | None:
        """Pause an open goal in metadata; returns the goal before, or ``None``. Blocking."""
        previous: dict[str, Any] = {}
        # Blocked waits for the user anyway; only work in progress fails or runs out.
        pausable = ("active",) if reason in ("run_failed", "limit") else ("active", "blocked")

        def mutate(metadata: dict[str, Any]) -> None:
            goal = _goal(metadata.get(SESSION_GOAL_META_KEY))
            if goal is None or goal["status"] not in pausable:
                return
            previous.update(goal)
            metadata[SESSION_GOAL_META_KEY] = {
                **goal,
                "status": "paused",
                "pause_reason": reason,
                "updated_at": _now(),
            }

        self._sessions.mutate_metadata(address, mutate)
        return previous or None

    def _paused(self, address: SessionAddress, reason: PauseReason, *, recorded: bool) -> None:
        self._cancel_deferred(address)
        _LOGGER.info(
            "Goal paused (agent=%s session=%s reason=%s)",
            address.agent_id,
            address.session_id,
            reason,
        )
        self._notify(address, history=recorded)

    # -- /goal -------------------------------------------------------------------

    async def execute_command(
        self, context: CommandExecutionContext, argument: str | None
    ) -> CommandOutcome:
        """Run one ``/goal`` command for the context's Session."""
        address = SessionAddress(context.project_id, context.agent_id, context.session_id)
        text = (argument or "").strip()
        word = text.casefold()
        if not text:
            return _notice(_status_text(await self.goal(address)))
        if word == "pause":
            return await self._command_pause(address)
        if word == "resume":
            return await self._command_resume(address, context)
        if word == "clear":
            return await self._command_clear(address)
        return await self._command_set(address, context, text)

    async def _command_set(
        self, address: SessionAddress, context: CommandExecutionContext, text: str
    ) -> CommandOutcome:
        objective = text
        max_turns = DEFAULT_GOAL_TURN_LIMIT
        match = _MAX_ARGUMENT.search(text)
        if match is not None:
            try:
                max_turns = int(match.group(1))
            except ValueError:
                max_turns = 0
            if not 1 <= max_turns <= MAX_GOAL_TURN_LIMIT:
                return _notice(f"Use --max with a number from 1 to {MAX_GOAL_TURN_LIMIT}.")
            objective = text[: match.start()].strip()
        if not objective:
            return _notice(_USAGE)
        if self._busy(address):
            return _notice("Stop the current turn first, then set the goal.")
        now = _now()
        goal = {
            "objective": objective,
            "status": "active",
            "turns": 0,
            "max_turns": max_turns,
            "created_at": now,
            "updated_at": now,
        }

        def mutate(metadata: dict[str, Any]) -> None:
            metadata[SESSION_GOAL_META_KEY] = goal

        await self._sessions.run_async(self._sessions.mutate_metadata, address, mutate)
        self._cancel_deferred(address)
        _LOGGER.info(
            "Goal set (agent=%s session=%s max_turns=%d)",
            address.agent_id,
            address.session_id,
            max_turns,
        )
        note = GOAL_SET_TEXT.format(objective=quote_external_value(objective))
        event = GoalEvent("set", objective=objective, max_turns=max_turns)
        try:
            run = await self._start_user_run(
                address, context, ChatMessage.note(note, goal_event=event)
            )
        except ActiveRunError:
            # The Run that won the Session continues the goal when it ends.
            recorded = await self._record(address, event, note)
            self._notify(address, history=recorded)
            return _notice("Goal set. It starts after the current turn.")
        self._notify(address)
        return CommandOutcome(command="goal", runs=(CommandRun(role="primary", run=run),))

    async def _command_pause(self, address: SessionAddress) -> CommandOutcome:
        goal = await self.goal(address)
        if goal is None:
            return _notice(_status_text(None))
        if goal["status"] not in ("active", "blocked"):
            return _notice(f"The goal is already {goal['status']}.")

        def mutate(metadata: dict[str, Any]) -> None:
            current = _goal(metadata.get(SESSION_GOAL_META_KEY))
            if current is None or current["status"] not in ("active", "blocked"):
                return
            metadata[SESSION_GOAL_META_KEY] = {
                **current,
                "status": "paused",
                "pause_reason": "user",
                "updated_at": _now(),
            }

        await self._sessions.run_async(self._sessions.mutate_metadata, address, mutate)
        self._cancel_deferred(address)
        recorded = await self._record(address, GoalEvent("paused", reason="user"))
        self._notify(address, history=recorded)
        return _notice("Goal paused. Resume it with /goal resume.")

    async def _command_resume(
        self, address: SessionAddress, context: CommandExecutionContext
    ) -> CommandOutcome:
        goal = await self.goal(address)
        if goal is None:
            return _notice(_status_text(None))
        if goal["status"] == "active":
            return _notice("The goal is already active.")

        def mutate(metadata: dict[str, Any]) -> None:
            current = _goal(metadata.get(SESSION_GOAL_META_KEY))
            if current is None:
                return
            resumed = {**current, "status": "active", "turns": 0, "updated_at": _now()}
            resumed.pop("pause_reason", None)
            resumed.pop("status_run_id", None)
            metadata[SESSION_GOAL_META_KEY] = resumed

        await self._sessions.run_async(self._sessions.mutate_metadata, address, mutate)
        event = GoalEvent("resumed")
        run = None
        if not self._busy(address):
            with contextlib.suppress(ActiveRunError):
                run = await self._start_user_run(
                    address, context, ChatMessage.note(GOAL_RESUMED_TEXT, goal_event=event)
                )
        if run is None:
            # The current Run continues the goal when it ends.
            recorded = await self._record(address, event)
            self._notify(address, history=recorded)
            return _notice("Goal resumed. It continues after the current turn.")
        self._notify(address)
        return CommandOutcome(command="goal", runs=(CommandRun(role="primary", run=run),))

    async def _command_clear(self, address: SessionAddress) -> CommandOutcome:
        removed: dict[str, bool] = {}

        def mutate(metadata: dict[str, Any]) -> None:
            removed["goal"] = metadata.pop(SESSION_GOAL_META_KEY, None) is not None

        await self._sessions.run_async(self._sessions.mutate_metadata, address, mutate)
        if not removed.get("goal"):
            return _notice(_status_text(None))
        self._cancel_deferred(address)
        recorded = await self._record(address, GoalEvent("cleared"))
        self._notify(address, history=recorded)
        return _notice("Goal cleared.")

    # -- Run observation -----------------------------------------------------------

    def _run_started(self, run: Run) -> None:
        address = _address(run)
        # This Run's end decides again; a pending wait is no longer needed.
        self._cancel_deferred(address)

        async def finished(status: RunStatus) -> None:
            self._spawn(self._after_run(run, status))

        run.add_completion_observer(finished)

    async def _after_run(self, run: Run, status: RunStatus) -> None:
        address = _address(run)
        # Status and cancel_reason on the Run are the facts decided here.
        with contextlib.suppress(Exception):
            await run.wait()
        goal = await self.goal(address)
        if goal is None or self._closed:
            return
        if _set_by_agent(run, goal):
            # The Agent's update_goal in this Run; its row follows the Run.
            reason = "agent" if goal["status"] == "paused" else None
            event = GoalEvent(cast(GoalEventKind, goal["status"]), reason=reason)
            self._notify(address, history=await self._record(address, event))
        if status == RunStatus.CANCELLED:
            if run.cancel_reason != "shutdown":
                await self._pause(address, "stop")
            return
        if status != RunStatus.COMPLETED:
            await self._pause(address, "run_failed")
            return
        if goal["status"] == "blocked":
            if not _reactivates(run, goal):
                return
            if not await self._reactivate(address):
                return
            goal = {**goal, "status": "active", "turns": 0}
        if goal["status"] != "active":
            return
        if int(goal.get("turns", 0)) >= int(goal.get("max_turns", DEFAULT_GOAL_TURN_LIMIT)):
            await self._pause(address, "limit")
            return
        await self._continue(address, ended=run)

    async def _reactivate(self, address: SessionAddress) -> bool:
        """The user answered a blocked goal: it is active again with a new count."""
        changed: dict[str, bool] = {}

        def mutate(metadata: dict[str, Any]) -> None:
            current = _goal(metadata.get(SESSION_GOAL_META_KEY))
            if current is None or current["status"] != "blocked":
                return
            active = {**current, "status": "active", "turns": 0, "updated_at": _now()}
            active.pop("status_run_id", None)
            metadata[SESSION_GOAL_META_KEY] = active
            changed["goal"] = True

        await self._sessions.run_async(self._sessions.mutate_metadata, address, mutate)
        if changed:
            recorded = await self._record(address, GoalEvent("reactivated"))
            self._notify(address, history=recorded)
        return bool(changed)

    async def _continue(
        self, address: SessionAddress, *, ended: Run | None, wait_for_work: bool = True
    ) -> None:
        """Start the next automatic Run when nothing else is waiting for the Session."""
        if self._closed or self._queued(address):
            # Queued input goes first; its Run's end decides again.
            return
        if wait_for_work and self._work_pending is not None and await self._work_pending(address):
            self._defer(address)
            return
        turn: dict[str, int] = {}

        def count(metadata: dict[str, Any]) -> None:
            current = _goal(metadata.get(SESSION_GOAL_META_KEY))
            if current is None or current["status"] != "active":
                return
            turns = int(current.get("turns", 0)) + 1
            metadata[SESSION_GOAL_META_KEY] = {**current, "turns": turns, "updated_at": _now()}
            turn["turns"] = turns
            turn["max_turns"] = int(current.get("max_turns", DEFAULT_GOAL_TURN_LIMIT))

        await self._sessions.run_async(self._sessions.mutate_metadata, address, count)
        if not turn:
            return
        message = GOAL_LAST_TURN_TEXT if turn["turns"] >= turn["max_turns"] else GOAL_CONTINUE_TEXT
        row = ChatMessage.note(
            "", goal_event=GoalEvent("turn", turn=turn["turns"], max_turns=turn["max_turns"])
        )
        reply_surface = await self._latest_reply_surface(address)
        run = None
        while run is None:
            try:
                run = await self._start_run(
                    address,
                    message,
                    reply_surface,
                    run_kind=RunKind.SYSTEM,
                    internal=True,
                    context_note=row,
                )
            except ActiveRunError:
                active = self._run_manager.active_run(
                    agent_id=address.agent_id,
                    session_id=address.session_id,
                    project_id=address.project_id,
                )
                if active is not None and active is ended:
                    # The ended Run has not released the Session yet.
                    await asyncio.sleep(0)
                    continue
                # Another Run won the Session; its end decides again.
                await self._uncount(address)
                return
            except Exception:
                _LOGGER.warning(
                    "Goal Run start failed (agent=%s session=%s)",
                    address.agent_id,
                    address.session_id,
                    exc_info=True,
                )
                await self._uncount(address)
                await self._pause(address, "run_failed")
                return
        self._notify(address)
        if reply_surface is not None and reply_surface.kind == "channel" and self._run_relay:
            try:
                await self._run_relay(run, reply_surface)
            except Exception:
                _LOGGER.error(
                    "Goal Run relay failed (agent=%s session=%s)",
                    address.agent_id,
                    address.session_id,
                    exc_info=True,
                )

    async def _uncount(self, address: SessionAddress) -> None:
        def mutate(metadata: dict[str, Any]) -> None:
            current = _goal(metadata.get(SESSION_GOAL_META_KEY))
            if current is not None and int(current.get("turns", 0)) > 0:
                metadata[SESSION_GOAL_META_KEY] = {
                    **current,
                    "turns": int(current["turns"]) - 1,
                }

        await self._sessions.run_async(self._sessions.mutate_metadata, address, mutate)

    def _defer(self, address: SessionAddress) -> None:
        """Wait for background work; its result wakes the Session, or the fallback does."""
        if address in self._deferred:
            return

        async def fallback() -> None:
            try:
                await _sleep(GOAL_WAIT_FALLBACK_SECONDS)
                if self._deferred.get(address) is not asyncio.current_task():
                    return
                del self._deferred[address]
                goal = await self.goal(address)
                if goal is None or goal["status"] != "active" or self._busy(address):
                    return
                await self._continue(address, ended=None, wait_for_work=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.error(
                    "Goal wait failed (agent=%s session=%s)",
                    address.agent_id,
                    address.session_id,
                    exc_info=True,
                )

        self._deferred[address] = asyncio.get_running_loop().create_task(fallback())

    def _cancel_deferred(self, address: SessionAddress) -> None:
        task = self._deferred.pop(address, None)
        if task is not None and task is not _current_task():
            task.cancel()

    # -- Run starts ----------------------------------------------------------------

    async def _start_user_run(
        self, address: SessionAddress, context: CommandExecutionContext, note: ChatMessage
    ) -> Run:
        """A Run the user started with /goal; it does not count as an automatic turn.

        Its input is the command as the user wrote it, which the conversation
        shows as theirs; *note* tells the Model what the command did.
        """
        return await self._start_run(
            address,
            context.command_text,
            context.reply_surface,
            run_kind=RunKind.USER,
            internal=False,
            context_note=note,
            sender=context.sender,
        )

    async def _start_run(
        self,
        address: SessionAddress,
        content: str,
        reply_surface: ReplySurface | None,
        *,
        run_kind: RunKind,
        internal: bool,
        context_note: ChatMessage,
        sender: MessageSender | None = None,
    ) -> Run:
        return await self._chat_loop.start_run(
            address.agent_id,
            content,
            session_id=address.session_id,
            internal=internal,
            sender=sender,
            reply_surface=reply_surface,
            project_id=address.project_id,
            run_kind=run_kind,
            context_note=context_note,
        )

    async def _latest_reply_surface(self, address: SessionAddress) -> ReplySurface | None:
        try:
            session = await self._sessions.get_async(address)
            message = await session.latest_note_async(REPLY_SURFACE_NOTE_PREFIX)
        except Exception:
            _LOGGER.warning(
                "Cannot recover the goal reply surface (agent=%s session=%s)",
                address.agent_id,
                address.session_id,
                exc_info=True,
            )
            return None
        return None if message is None else reply_surface_from_note(message)

    # -- Helpers -------------------------------------------------------------------

    def _busy(self, address: SessionAddress) -> bool:
        return self._run_manager.has_activity_for_session(
            address.agent_id, address.session_id, project_id=address.project_id
        )

    def _queued(self, address: SessionAddress) -> bool:
        return bool(
            self._run_manager.list_queued(
                address.agent_id, address.session_id, project_id=address.project_id
            )
        )

    def _notify(self, address: SessionAddress, *, history: bool = False) -> None:
        for callback in list(self._changed_callbacks):
            try:
                callback(address, history)
            except Exception:
                _LOGGER.warning("Goal change callback failed", exc_info=True)

    def _spawn(self, coroutine: Awaitable[None]) -> None:
        if self._closed:
            if asyncio.iscoroutine(coroutine):
                coroutine.close()
            return

        async def guarded() -> None:
            try:
                await coroutine
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.error("Goal decision after a Run failed", exc_info=True)

        task = asyncio.get_running_loop().create_task(guarded())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _set_by_agent(run: Run, goal: dict[str, Any]) -> bool:
    """The goal still has the status the Agent's update_goal in *run* gave it."""
    if goal.get("status_run_id") != run.id:
        return False
    return goal["status"] in ("complete", "blocked") or (
        goal["status"] == "paused" and goal.get("pause_reason") == "agent"
    )


def _reactivates(run: Run, goal: dict[str, Any]) -> bool:
    """A user or Channel message answered the blocked goal, not the Run that blocked it."""
    return (
        run.run_kind in (RunKind.USER, RunKind.CHANNEL)
        and not run.internal
        and goal.get("status_run_id") != run.id
    )


def _pause_event(reason: PauseReason, goal: dict[str, Any]) -> GoalEvent:
    """The conversation row of a pause; a limit names the number of turns."""
    limit = goal.get("max_turns") if reason == "limit" else None
    return GoalEvent(
        "paused", reason=reason, max_turns=limit if isinstance(limit, int) and limit > 0 else None
    )


def _pause_note(reason: PauseReason, goal: dict[str, Any]) -> str | None:
    """The reminder for a pause the Agent did not cause; the user's own pause has none."""
    note = _PAUSE_NOTE_REASONS.get(reason)
    if note is None:
        return None
    limit = goal.get("max_turns", DEFAULT_GOAL_TURN_LIMIT)
    return GOAL_PAUSED_TEXT.format(reason=note.format(limit=limit))


def _status_text(goal: dict[str, Any] | None) -> str:
    if goal is None:
        return "This session has no goal. Set one with /goal <objective>."
    status = goal["status"]
    if status == "paused":
        reason = _PAUSE_STATUS_REASONS.get(str(goal.get("pause_reason")), "")
        label = f"paused ({reason})" if reason else "paused"
    elif status == "blocked":
        label = "blocked, waiting for your reply"
    else:
        label = status
    return (
        f"Goal ({label}): {goal['objective']}\n"
        f"Automatic turns: {goal.get('turns', 0)} of "
        f"{goal.get('max_turns', DEFAULT_GOAL_TURN_LIMIT)}."
    )


def _notice(text: str) -> CommandOutcome:
    return CommandOutcome(command="goal", feedback=CommandFeedback(kind="notice", text=text))


def _current_task() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


# Tests patch this seam instead of the process-wide ``asyncio.sleep``.
_sleep = asyncio.sleep
