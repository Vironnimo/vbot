"""Deliver the answers of Sub-Agent Sessions to their Parent Sessions.

Every Run that the Parent or a delivery started in a linked Sub-Agent Session is
followed: its activity reaches the Session's activity file, and when it ends its
final answer goes to the Parent Session through the completion coordinator,
with a line that names what of the Sub-Agent is still running. Runs the user
started never forward, and nothing forwards once the user took the Session over.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.projects import format_agent_address
from core.runs import (
    Run,
    RunAdmissionBlockedError,
    RunCancelledError,
    RunInterruptedError,
)
from core.sessions import SessionAddress
from core.subagents._constants import (
    FACTS_NO_SIBLINGS_PENDING_TEXT,
    FACTS_NOTHING_RUNNING_TEXT,
    FACTS_RUNNING_TEMPLATE,
    FACTS_SIBLINGS_PENDING_TEMPLATE,
    FORWARDED_FAILURE_TEXT_TEMPLATE,
    FORWARDED_NO_ANSWER_TEXT,
    FORWARDED_RUN_KINDS,
    FORWARDED_SECTION_TEMPLATE,
    TAKEN_OVER_NOTICE_TEMPLATE,
    USER_CANCEL_REASON,
)
from core.subagents.activity import SubAgentActivity
from core.subagents.links import SubAgentLink, children, read_link
from core.tools.terminal_manager import TerminalOwner
from core.utils.logging import get_logger
from core.utils.paths import model_path

if TYPE_CHECKING:
    from core.chat import ChatMessage
    from core.runs import ChatRunManager
    from core.runtime.interfaces import RuntimeServices

_LOGGER = get_logger("subagents")

# Attempts to read a Run's persisted answer that is not yet visible.
_RESULT_READ_ATTEMPTS = 3
_RESULT_READ_DELAY_SECONDS = 0.05


@dataclass(frozen=True)
class RunningEntry:
    """One piece of a Sub-Agent's work that is still running."""

    kind: str
    id: str
    label: str

    def describe(self) -> str:
        return f"{self.kind} {self.id} ({self.label})" if self.label else f"{self.kind} {self.id}"


class SubAgentActivities:
    """Own the activity file of each Sub-Agent Session in this process."""

    def __init__(self, runtime: RuntimeServices) -> None:
        self._runtime = runtime
        self._files: dict[SessionAddress, SubAgentActivity | None] = {}
        self._creating: dict[SessionAddress, asyncio.Task[SubAgentActivity | None]] = {}

    async def ensure(self, address: SessionAddress) -> SubAgentActivity | None:
        """Return the Session's activity file, creating it on first use."""
        if address in self._files:
            return self._files[address]
        task = self._creating.get(address)
        if task is None:
            task = asyncio.ensure_future(
                SubAgentActivity.create(
                    self._runtime.storage.temporary_files,
                    agent_id=address.agent_id,
                    session_id=address.session_id,
                )
            )
            self._creating[address] = task
        try:
            activity = await asyncio.shield(task)
        finally:
            if task.done():
                self._creating.pop(address, None)
        self._files[address] = activity
        return activity

    def path(self, address: SessionAddress) -> str | None:
        """Return the Session's activity file path as the Agent reads it, if one exists."""
        activity = self._files.get(address)
        return model_path(activity.path) if activity is not None else None

    async def drain(self) -> None:
        """Wait until every activity file's text so far is on disk."""
        files = [activity for activity in self._files.values() if activity is not None]
        await asyncio.gather(*(activity.drain() for activity in files))


class SubAgentForwarding:
    """Follow Runs in Sub-Agent Sessions and forward their answers to the Parent."""

    def __init__(
        self,
        runtime: RuntimeServices,
        trigger_service: Any,
        activities: SubAgentActivities,
    ) -> None:
        self._runtime = runtime
        self._trigger_service = trigger_service
        self._activities = activities
        # Followed Runs, so the tasks stay referenced and rename guards see them.
        self._followed: dict[str, tuple[Run, asyncio.Task[None]]] = {}
        # Runs whose Parent stopped them itself; their outcome is in its Tool result.
        self._silenced: set[str] = set()

    def install(self, run_manager: ChatRunManager) -> None:
        """Start following Runs as *run_manager* starts them."""
        run_manager.add_run_started_callback(self._on_run_started)

    def silence(self, run_id: str) -> None:
        """Do not forward the outcome of a Run its Parent stopped itself."""
        self._silenced.add(run_id)

    def followed_runs(self) -> list[Run]:
        """Return the Runs currently followed for forwarding."""
        return [run for run, _task in self._followed.values()]

    def taken_over(self, address: SessionAddress) -> None:
        """Tell the Parent once that the user took over its Sub-Agent's Session."""
        task = asyncio.create_task(self._notify_takeover(address))
        task.add_done_callback(_log_task_failure)

    def _on_run_started(self, run: Run) -> None:
        if run.run_kind.value not in FORWARDED_RUN_KINDS:
            return
        task = asyncio.create_task(self._follow(run), name=f"subagent-forwarding:{run.id}")
        self._followed[run.id] = (run, task)
        task.add_done_callback(lambda done: self._finish_following(run.id, done))

    def _finish_following(self, run_id: str, task: asyncio.Task[None]) -> None:
        self._followed.pop(run_id, None)
        self._silenced.discard(run_id)
        _log_task_failure(task)

    async def _follow(self, run: Run) -> None:
        sessions = self._runtime.chat_sessions
        address = _run_address(run)
        link = await sessions.run_async(read_link, sessions, address)
        if link is None or link.taken_over_at is not None:
            return
        activity = await self._activities.ensure(address)
        if activity is not None:
            activity.follow(run)
        outcome, answer = await _turn_outcome(self._runtime, run)
        link = await sessions.run_async(read_link, sessions, address)
        if link is None or link.taken_over_at is not None or run.id in self._silenced:
            return
        if not await sessions.run_async(sessions.exists, link.parent):
            _LOGGER.info(
                "Sub-Agent answer dropped: its Parent Session no longer exists "
                "(subagent=%s run=%s)",
                link.id,
                run.id,
            )
            return
        facts = facts_text(await running_entries(self._runtime, address))
        siblings = await self._siblings_text(link, run.id)
        if siblings is not None:
            facts = f"{facts}\n{siblings}"
        body = FORWARDED_SECTION_TEMPLATE.format(
            id=link.id,
            title=link.title or link.id,
            agent_id=format_agent_address(address.agent_id, address.project_id),
            session_id=address.session_id,
            outcome=outcome,
            answer=answer,
            facts=facts,
        )
        parent = link.parent
        delivery = self._trigger_service.submit_completion(
            parent.agent_id,
            parent.session_id,
            notice_id=f"subagent:{run.id}",
            origin_run_id=run.id,
            body=body,
            project_id=parent.project_id,
            execution_owner=run.execution_owner,
            on_persisted=lambda: self._mark_read(address, run.id),
        )
        delivery.add_done_callback(lambda done: _log_delivery(done, link, run.id))

    async def _siblings_text(self, link: SubAgentLink, run_id: str) -> str | None:
        """Name the Parent's other Sub-Agents whose answers are still to come, as of now.

        A sibling counts while it works or while a followed Run of it has not yet
        handed its answer to delivery. ``None`` when the Parent has no other
        Sub-Agent that is not taken over.
        """
        sessions = self._runtime.chat_sessions
        siblings = [
            sibling
            for sibling in await sessions.run_async(children, sessions, link.parent)
            if sibling.id != link.id and sibling.taken_over_at is None
        ]
        if not siblings:
            return None
        forwarding = {
            _run_address(run)
            for followed_id, (run, _task) in self._followed.items()
            if followed_id != run_id and followed_id not in self._silenced
        }
        pending = [
            f"{sibling.id} ({_single_line(sibling.title)})" if sibling.title else sibling.id
            for sibling in siblings
            if sibling.session in forwarding or is_working(self._runtime, sibling.session)
        ]
        if not pending:
            return FACTS_NO_SIBLINGS_PENDING_TEXT
        return FACTS_SIBLINGS_PENDING_TEMPLATE.format(entries="; ".join(pending))

    def _mark_read(self, address: SessionAddress, run_id: str) -> None:
        try:
            self._runtime.chat_sessions.mark_terminal_run_read(address, run_id)
        except Exception:
            _LOGGER.warning(
                "Failed to mark a forwarded Sub-Agent answer read (session=%s run=%s)",
                address.session_id,
                run_id,
                exc_info=True,
            )

    async def _notify_takeover(self, address: SessionAddress) -> None:
        sessions = self._runtime.chat_sessions
        link = await sessions.run_async(read_link, sessions, address)
        if link is None or not await sessions.run_async(sessions.exists, link.parent):
            return
        parent = link.parent
        delivery = self._trigger_service.submit_completion(
            parent.agent_id,
            parent.session_id,
            notice_id=f"subagent-takeover:{link.id}",
            origin_run_id=f"subagent-takeover:{link.id}",
            body=TAKEN_OVER_NOTICE_TEMPLATE.format(id=link.id, title=link.title or link.id),
            project_id=parent.project_id,
            wake=False,
        )
        delivery.add_done_callback(lambda done: _log_delivery(done, link, None))


async def running_entries(runtime: RuntimeServices, address: SessionAddress) -> list[RunningEntry]:
    """Return what of the Sub-Agent at *address* is still running, as of now.

    Attached terminals and background commands, and its own Sub-Agents that
    are working (a Run or queued input) and not taken over by the user.
    """
    entries: list[RunningEntry] = []
    owner = TerminalOwner(
        project_id=address.project_id, agent_id=address.agent_id, session_id=address.session_id
    )
    terminal_manager = runtime.terminal_manager
    if terminal_manager is not None:
        for info in terminal_manager.list_terminals():
            if info.attachment != owner or info.finished_at is not None:
                continue
            kind = "background command" if info.kind == "command" else "terminal"
            label = info.name or " ".join((info.command, *info.arguments)).strip()
            entries.append(RunningEntry(kind, info.terminal_id, _single_line(label)))
    sessions = runtime.chat_sessions
    for link in await sessions.run_async(children, sessions, address):
        if link.taken_over_at is None and is_working(runtime, link.session):
            entries.append(RunningEntry("Sub-Agent", link.id, _single_line(link.title)))
    return entries


def facts_text(entries: list[RunningEntry]) -> str:
    """Render the facts line that ends each forwarded answer."""
    if not entries:
        return FACTS_NOTHING_RUNNING_TEXT
    return FACTS_RUNNING_TEMPLATE.format(entries="; ".join(entry.describe() for entry in entries))


def is_working(runtime: RuntimeServices, address: SessionAddress) -> bool:
    """Return whether the Session has an active Run or queued input."""
    manager = runtime.chat_run_manager
    return manager.active_run(
        agent_id=address.agent_id, session_id=address.session_id, project_id=address.project_id
    ) is not None or bool(
        manager.list_queued(address.agent_id, address.session_id, project_id=address.project_id)
    )


async def _turn_outcome(runtime: RuntimeServices, run: Run) -> tuple[str, str]:
    """Wait for *run* to end; return how its turn ended and its final answer."""
    try:
        message = await run.wait()
    except RunCancelledError:
        cancelled = (
            "was cancelled by the user"
            if run.cancel_reason == USER_CANCEL_REASON
            else "was cancelled"
        )
        return cancelled, await _persisted_answer(runtime, run) or FORWARDED_NO_ANSWER_TEXT
    except RunInterruptedError as error:
        answer = _message_text(error.result) or await _persisted_answer(runtime, run)
        return "was interrupted", answer or FORWARDED_NO_ANSWER_TEXT
    except Exception as error:
        answer = await _persisted_answer(runtime, run)
        return "failed", answer or FORWARDED_FAILURE_TEXT_TEMPLATE.format(error=error)
    answer = _message_text(message) or await _persisted_answer(runtime, run)
    return "completed", answer or FORWARDED_NO_ANSWER_TEXT


async def _persisted_answer(runtime: RuntimeServices, run: Run) -> str | None:
    """Read the final Assistant answer the Run persisted, if it has one."""
    address = _run_address(run)
    for attempt in range(_RESULT_READ_ATTEMPTS):
        try:
            session = await runtime.chat_sessions.get_async(address)
            result = await session.load_run_result_async(run_id=run.id)
        except Exception:
            return None
        if result is not None:
            return _message_text(result.assistant)
        if attempt + 1 < _RESULT_READ_ATTEMPTS:
            await asyncio.sleep(_RESULT_READ_DELAY_SECONDS)
    return None


def _message_text(message: ChatMessage | Any) -> str | None:
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts = [
            block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
            for block in content
        ]
        text = "\n".join(part for part in parts if isinstance(part, str) and part)
        return text.strip() or None
    return None


def _run_address(run: Run) -> SessionAddress:
    return SessionAddress(
        project_id=run.project_id, agent_id=run.agent_id, session_id=run.session_id
    )


def _single_line(value: str) -> str:
    return " ".join(value.split())


def _log_delivery(delivery: asyncio.Future[None], link: SubAgentLink, run_id: str | None) -> None:
    if delivery.cancelled():
        return
    error = delivery.exception()
    if error is None:
        return
    if isinstance(error, RunAdmissionBlockedError):
        # The execution owner closed (for example its temporary group): an
        # expected end of its lifecycle, not a delivery fault.
        _LOGGER.debug(
            "Sub-Agent delivery dropped for a closed execution owner (subagent=%s run=%s)",
            link.id,
            run_id,
        )
        return
    _LOGGER.error(
        "Sub-Agent delivery to the Parent failed (subagent=%s run=%s): %s",
        link.id,
        run_id,
        error,
        exc_info=(type(error), error, error.__traceback__),
    )


def _log_task_failure(task: asyncio.Future[Any]) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        _LOGGER.error(
            "Sub-Agent forwarding failed: %s",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )


__all__ = [
    "RunningEntry",
    "SubAgentActivities",
    "SubAgentForwarding",
    "facts_text",
    "is_working",
    "running_entries",
]
