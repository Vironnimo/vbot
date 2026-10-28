"""Deliver the answers of Sub-Agent Sessions to their Parent Sessions.

Follow Runs in linked Sub-Agent Sessions and deliver only answers whose persisted
recipient is the Parent. Steering changes future answers without redirecting old
ones or changing the durable tree.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from core.runs import (
    Run,
    RunAdmissionBlockedError,
    RunCancelledError,
    RunInterruptedError,
    RunStatus,
    is_silent_answer,
)
from core.sessions import SessionAddress
from core.storage.temp_files import artifact_owner
from core.subagents._constants import (
    FACTS_NO_SIBLINGS_PENDING_TEXT,
    FACTS_NOTHING_RUNNING_TEXT,
    FACTS_RUNNING_TEMPLATE,
    FACTS_SIBLINGS_PENDING_TEMPLATE,
    FORWARDED_ANSWER_TEMPLATE,
    FORWARDED_FAILURE_TEXT_TEMPLATE,
    FORWARDED_NO_ANSWER_TEXT,
    FORWARDED_RUN_KINDS,
    FORWARDED_SECTION_TEMPLATE,
    REPLY_TO_USER_NOTICE_TEMPLATE,
    USER_CANCEL_REASON,
)
from core.subagents.activity import SubAgentActivity
from core.subagents.links import SubAgentLink, children, read_link
from core.tools.terminal_manager import TerminalOwner
from core.utils.logging import get_logger
from core.utils.paths import model_path
from core.utils.retry import sleep_for_retry

if TYPE_CHECKING:
    from core.chat import ChatMessage
    from core.runs import ChatRunManager
    from core.runtime.interfaces import RuntimeServices


_LOGGER = get_logger("subagents")


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

    async def ensure(
        self, address: SessionAddress, *, parent: SessionAddress
    ) -> SubAgentActivity | None:
        """Return the Session's activity file, creating it on first use.

        The file lies in the temporary-file folder of *parent*, which reads it.
        """
        if address in self._files:
            return self._files[address]
        task = self._creating.get(address)
        if task is None:
            task = asyncio.ensure_future(
                SubAgentActivity.create(
                    self._runtime.storage.temporary_files,
                    agent_id=address.agent_id,
                    session_id=address.session_id,
                    owner=artifact_owner(parent.project_id, parent.agent_id, parent.session_id),
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
        self._forwarded_callbacks: list[Callable[[SessionAddress], None]] = []

    def install(self, run_manager: ChatRunManager) -> None:
        """Start following Runs as *run_manager* starts them."""
        run_manager.add_run_started_callback(self._on_run_started)

    def silence(self, run_id: str) -> None:
        """Do not forward the outcome of a Run its Parent stopped itself."""
        self._silenced.add(run_id)

    def followed_runs(self) -> list[Run]:
        """Return the Runs currently followed for forwarding."""
        return [run for run, _task in self._followed.values()]

    def pending(self, address: SessionAddress) -> bool:
        """Whether the Session has followed work not yet handed to delivery."""
        return bool(self._pending_runs(address))

    def _pending_runs(self, address: SessionAddress) -> list[Run]:
        return [
            run
            for run_id, (run, task) in self._followed.items()
            if run_id not in self._silenced and not task.done() and _run_address(run) == address
        ]

    async def parent_answer_pending(self, address: SessionAddress) -> bool:
        """Read the actual Parent-directed answers of still-followed Runs.

        The current reply target governs future output, but cannot redirect a
        complete answer committed before user steering. Terminal work uses the
        very same selection as delivery, including failures without an answer.
        """
        for run in self._pending_runs(address):
            if run.status != RunStatus.RUNNING:
                directed = await _turn_outcome(self._runtime, run) is not None
            else:
                session = await self._runtime.chat_sessions.get_async(address)
                messages = await session.load_run_messages_async(run.id)
                directed = bool(_parent_answers(messages))
            if directed and run in self._pending_runs(address):
                return True
        return False

    def add_forwarded_callback(
        self, callback: Callable[[SessionAddress], None]
    ) -> Callable[[], None]:
        """Call *callback* with a Session's address once a followed Run of it is
        done forwarding, delivered or not. Returns the unsubscribe function."""
        self._forwarded_callbacks.append(callback)

        def remove() -> None:
            if callback in self._forwarded_callbacks:
                self._forwarded_callbacks.remove(callback)

        return remove

    def reply_to_user(self, address: SessionAddress, message_id: str) -> None:
        """Tell the Parent when a user input redirects the Sub-Agent's answers."""
        task = asyncio.create_task(self._notify_user_reply(address, message_id))
        task.add_done_callback(_log_task_failure)

    def _on_run_started(self, run: Run) -> None:
        if run.run_kind.value not in FORWARDED_RUN_KINDS:
            return
        task = asyncio.create_task(self._follow(run), name=f"subagent-forwarding:{run.id}")
        self._followed[run.id] = (run, task)
        task.add_done_callback(lambda done: self._finish_following(run.id, done))

    def _finish_following(self, run_id: str, task: asyncio.Task[None]) -> None:
        followed = self._followed.pop(run_id, None)
        self._silenced.discard(run_id)
        _log_task_failure(task)
        if followed is None:
            return
        address = _run_address(followed[0])
        for callback in list(self._forwarded_callbacks):
            try:
                callback(address)
            except Exception:
                _LOGGER.warning("A Sub-Agent forwarding callback failed", exc_info=True)

    async def _follow(self, run: Run) -> None:
        sessions = self._runtime.chat_sessions
        address = _run_address(run)
        link = await self._read_link(address)
        if link is None:
            return
        activity = await self._activities.ensure(address, parent=link.parent)
        if activity is not None:
            activity.follow(run)
        result = await _turn_outcome(self._runtime, run)
        if result is None:
            return
        outcome, answer, answer_ids = result
        link = await self._read_link(address)
        if link is None or run.id in self._silenced:
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
            run_id=run.id,
            outcome=outcome,
            answer=answer,
            facts=facts,
        )
        parent = link.parent
        delivery = self._trigger_service.submit_completion(
            parent,
            notice_id=f"subagent:{run.id}",
            origin_run_id=run.id,
            continuation_source=run.capture_continuation_source(),
            expected_session_generation_id=link.parent_generation_id,
            body=body,
            execution_owner=run.execution_owner,
        )
        delivery.add_done_callback(lambda done: _log_delivery(done, link, run.id))
        if activity is not None:
            activity.track_delivery(
                delivery,
                delivery_id=f"subagent:{run.id}",
                run_id=run.id,
                parent=parent,
                answer_ids=answer_ids,
            )

    async def _read_link(self, address: SessionAddress) -> SubAgentLink | None:
        """A missing link is terminal; unavailable metadata gets bounded retries."""
        sessions = self._runtime.chat_sessions
        for attempt in range(3):
            try:
                link = await sessions.run_async(read_link, sessions, address, strict=True)
            except Exception:
                if attempt == 2:
                    # The watcher callback reports the terminal failure with its
                    # traceback instead of silently treating this as a root Session.
                    raise
                _LOGGER.log(
                    logging.WARNING if attempt == 0 else logging.DEBUG,
                    "Sub-Agent forwarding metadata unavailable; retrying "
                    "(agent=%s session=%s attempt=%d)",
                    address.agent_id,
                    address.session_id,
                    attempt + 1,
                    exc_info=True,
                )
                await sleep_for_retry(attempt)
            else:
                if attempt:
                    _LOGGER.info(
                        "Sub-Agent forwarding metadata recovered (agent=%s session=%s)",
                        address.agent_id,
                        address.session_id,
                    )
                return link
        raise AssertionError("forwarding metadata retry did not settle")

    async def _siblings_text(self, link: SubAgentLink, run_id: str) -> str | None:
        """Name the Parent's other Sub-Agents whose answers are still to come, as of now.

        A sibling counts while it works or while a followed Run of it has not yet
        handed its answer to delivery. ``None`` when the Parent has no other
        Sub-Agent whose answers currently go to it.
        """
        sessions = self._runtime.chat_sessions
        siblings = [
            sibling
            for sibling in await sessions.run_async(children, sessions, link.parent)
            if sibling.id != link.id and sibling.reply_target == "parent"
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

    async def _notify_user_reply(self, address: SessionAddress, message_id: str) -> None:
        sessions = self._runtime.chat_sessions
        link = await self._read_link(address)
        if (
            link is None
            or link.reply_target != "user"
            or not await sessions.run_async(sessions.exists, link.parent)
        ):
            return
        parent = link.parent
        delivery = self._trigger_service.submit_completion(
            parent,
            notice_id=f"subagent-user-reply:{message_id}",
            origin_run_id=f"subagent-user-reply:{message_id}",
            expected_session_generation_id=link.parent_generation_id,
            body=REPLY_TO_USER_NOTICE_TEMPLATE.format(id=link.id, title=link.title or link.id),
            wake=False,
        )
        delivery.add_done_callback(lambda done: _log_delivery(done, link, None))


async def running_entries(runtime: RuntimeServices, address: SessionAddress) -> list[RunningEntry]:
    """Return what of the Sub-Agent at *address* is still running, as of now.

    Its unfinished background commands, attached terminals, and its own
    Sub-Agents that are working (a Run or queued input).
    """
    entries: list[RunningEntry] = []
    command_manager = runtime.command_manager
    if command_manager is not None:
        for report in command_manager.commands([address]):
            if not report.finished:
                entries.append(RunningEntry("background command", report.command_id, report.label))
    owner = TerminalOwner(
        project_id=address.project_id, agent_id=address.agent_id, session_id=address.session_id
    )
    terminal_manager = runtime.terminal_manager
    if terminal_manager is not None:
        for info in terminal_manager.list_terminals():
            if info.attachment != owner or info.finished_at is not None:
                continue
            command = " ".join((info.command, *info.arguments)).strip()
            label = f"{info.call_name}, {command}" if info.call_name else command
            entries.append(RunningEntry("terminal", info.terminal_id, _single_line(label)))
    sessions = runtime.chat_sessions
    for link in await sessions.run_async(children, sessions, address):
        if is_working(runtime, link.session):
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


async def _turn_outcome(
    runtime: RuntimeServices, run: Run
) -> tuple[str, str, tuple[str, ...]] | None:
    """Return the Run's outcome and every complete answer, in persistence order.

    Steering can continue a Run after a complete answer. Its return value is
    only the last answer, so the Session is the source for the whole report.
    A stopped or failed Run also retains its latest partial text. Silent
    answers are left out; None when nothing is directed to the Parent.
    """
    result = None
    outcome = "completed"
    fallback = FORWARDED_NO_ANSWER_TEXT
    try:
        result = await run.wait()
    except RunCancelledError:
        outcome = (
            "was cancelled by the user"
            if run.cancel_reason == USER_CANCEL_REASON
            else "was cancelled"
        )
    except RunInterruptedError as error:
        outcome, result = "was interrupted", error.result
    except Exception as error:
        outcome, fallback = "failed", FORWARDED_FAILURE_TEXT_TEMPLATE.format(error=error)

    session = await runtime.chat_sessions.get_async(_run_address(run))
    messages = await session.load_run_messages_async(run.id)
    progression = [m for m in messages if m.role in {"user", "assistant", "tool", "error"}]
    ended_silent = bool(progression) and _is_silent_answer(progression[-1])
    if outcome.startswith("was cancelled") and (
        ended_silent or (progression and _is_complete_answer(progression[-1]))
    ):
        # Stop during Compaction after the final answer does not make it partial.
        outcome = "completed"
    # Each entry keeps the destination at its own commit. A later Run or a later
    # steering message cannot redirect earlier answers. The footer snapshots the
    # terminal destination even when a Run failed without Assistant output.
    final_target = next(
        (
            m.subagent_reply_target
            for m in reversed(messages)
            if m.subagent_reply_target is not None
        ),
        "parent" if run.run_kind.value in {"subagent", "system"} else "user",
    )
    answers = _parent_answers(messages)
    latest = next(
        (m for m in reversed(messages) if m.role == "assistant" and _message_text(m)), None
    )
    # Normal Runs persist their return value. Keep the returned text as a
    # fallback for executors that return an answer without persisting it.
    if result is not None and not any(m.id == result.id for m in messages):
        if _message_text(result):
            latest = replace(result, subagent_reply_target=final_target)
        elif _is_silent_answer(result):
            ended_silent = True
    if (
        latest is not None
        and latest.subagent_reply_target == "parent"
        and all(m.id != latest.id for m in answers)
        and (_is_complete_answer(latest) or outcome != "completed")
    ):
        answers.append(latest)
    # The silent answer reports nothing: the Parent is not woken for it.
    if not answers and (final_target != "parent" or (outcome == "completed" and ended_silent)):
        return None
    sections = [
        FORWARDED_ANSWER_TEMPLATE.format(
            kind="Answer" if _is_complete_answer(m) else "Partial answer",
            message_id=m.id,
            content=_message_text(m),
        )
        for m in answers
    ]
    return (
        outcome,
        "\n\n".join(sections) or fallback,
        tuple(m.id for m in answers),
    )


def _parent_answers(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [m for m in messages if _is_complete_answer(m) and m.subagent_reply_target == "parent"]


def _is_complete_answer(message: ChatMessage) -> bool:
    return (
        message.role == "assistant"
        and bool(_message_text(message))
        and not message.interrupted
        and not message.tool_calls
        and message.phase != "commentary"
    )


def _is_silent_answer(message: ChatMessage | Any) -> bool:
    return (
        getattr(message, "role", None) == "assistant"
        and not getattr(message, "interrupted", False)
        and not getattr(message, "tool_calls", None)
        and is_silent_answer(_raw_text(message))
    )


def _message_text(message: ChatMessage | Any) -> str | None:
    """The message's text, or None when it has none or is the silent answer."""
    text = _raw_text(message)
    return None if is_silent_answer(text) else text


def _raw_text(message: ChatMessage | Any) -> str | None:
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
