"""One running or retained command: output, end detection, stop and delivery.

The record owns its process from launch to release. Two tasks drive it: the
reader feeds the output pipe into ``CommandOutput``; the supervisor waits for
the shell's exit and then for the processes it left running. A command has no
time limit of its own: it runs until it ends or something stops it. Every
decision runs on the Event Loop; process queries and kills run through the
blocking runner, and output cleaning on the output host's worker.

End of a command: the shell exited, output after the exit was read for at most
``COMMAND_DRAIN_SECONDS``, and either no process of the command runs, or the
processes still running were quiet for ``COMMAND_LEFTOVER_QUIET_SECONDS`` (no
output, no new process, at most ``IDLE_CPU_SECONDS`` of CPU time). An end that
something outside vBot caused, such as ``taskkill /T`` on the shell, is an
ordinary end. Processes still running at the end keep the record live until
they end or are stopped, by vBot or from outside; the record is finished once
none runs.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core.runs import RunAdmissionBlockedError, RunExecutionOwner
from core.storage.temp_files import TemporaryFileLease
from core.utils.logging import get_logger

from ._command_output import CommandOutput
from ._command_types import (
    COMMAND_DRAIN_SECONDS,
    COMMAND_LEFTOVER_QUIET_SECONDS,
    COMMAND_LOG_REFRESH_SECONDS,
    COMMAND_PROGRESS_LINES,
    COMMAND_PROGRESS_SECONDS,
    COMMAND_STOP_GRACE_SECONDS,
    CommandOwner,
    CommandProcess,
    CommandReport,
    CommandReportFormatter,
    CommandStatus,
    CommandStopError,
    CommandWaitOutcome,
    CompletionSink,
    OutputCallback,
    StopReason,
    command_status,
)
from .process_tree import IDLE_CPU_SECONDS, TREE_POLL_SECONDS, ProcessTreeFacts, RunningProcess

_LOGGER = get_logger("tools.command_manager")
# The console host of a windowless console is a member of the command's Job; it
# exits with the last process attached to that console and is no command work.
_CONSOLE_HOST = "conhost.exe"

BlockingRunner = Callable[..., Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class CommandServices:
    """What every command record of one manager shares."""

    trigger_service: CompletionSink | None
    monotonic: Callable[[], float]
    sleep: Callable[[float], Awaitable[None]]
    # Runs a blocking callable off the Event Loop (``asyncio.to_thread``).
    run_blocking: BlockingRunner
    # A background command's status may have changed.
    changed: Callable[[str], None]


@dataclass(slots=True)
class _QuietWatch:
    """The baseline of the current quiet period of processes left running."""

    activity: int = -1
    since: float = 0.0
    cpu_seconds: float = 0.0
    started: int = 0


class CommandRecord:
    """One command from its launch until the manager forgets it."""

    def __init__(
        self,
        command_id: str,
        *,
        owner: CommandOwner,
        process: CommandProcess,
        command: str,
        description: str | None,
        workdir: Path,
        origin_run_id: str,
        execution_owner: RunExecutionOwner | None,
        formatter: CommandReportFormatter,
        on_output: OutputCallback | None,
        output: CommandOutput,
        log_path: Path | None,
        log_lease: TemporaryFileLease | None,
        services: CommandServices,
    ) -> None:
        self.command_id = command_id
        self.owner = owner
        self.origin_run_id = origin_run_id
        self.execution_owner = execution_owner
        self._process = process
        self._pid = process.pid
        self._tree = process.tree
        self._command = command
        self._description = description
        self._workdir = workdir
        self._formatter = formatter
        self._on_output = on_output
        self._output = output
        self._log_path = log_path
        self._log_lease = log_lease
        self._services = services
        self.started_at = services.monotonic()
        self._background = False
        self._deliver = False
        self._exit_code: int | None = None
        self._stop_reason: StopReason | None = None
        # Failed children recorded before vBot stopped the command; later ones
        # ended because of the stop and are no failures of the command.
        self._exits_before_stop: int | None = None
        self._facts: ProcessTreeFacts | None = None
        self._facts_failing = False
        self._ended_at: float | None = None
        self._finished_at: float | None = None
        self._activity = 0
        self._eof = asyncio.Event()
        self._ended = asyncio.Event()
        self._finished = asyncio.Event()
        # vBot confirmed the kill of every process of the command.
        self._killed = asyncio.Event()
        self._interrupted = False
        self._stop_lock = asyncio.Lock()
        self._progress = asyncio.Event()
        self._reader_task: asyncio.Task[None] | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._progress_task: asyncio.Task[None] | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._delivery_task: asyncio.Task[None] | None = None
        # The Session the result was submitted to, and its body: a transfer
        # submits a result not yet delivered again, to the new Session.
        self._delivered_to = owner
        self._delivered_body: str | None = None

    # Facts

    @property
    def ended(self) -> bool:
        return self._ended.is_set()

    @property
    def finished(self) -> bool:
        return self._finished.is_set()

    @property
    def finished_at(self) -> float | None:
        """When the last process of the command ended; None while one runs."""
        return self._finished_at

    @property
    def background(self) -> bool:
        return self._background

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def status(self) -> CommandStatus:
        return command_status(
            ended=self.ended, stop_reason=self._stop_reason, exit_code=self._exit_code
        )

    @property
    def _delivering(self) -> bool:
        """Whether the end delivers the result: armed, and the Agent did not
        stop the command, since its stop call shows the outcome."""
        return self._deliver and self._stop_reason != "agent"

    @property
    def notice_id(self) -> str:
        return f"command:{self.command_id}"

    def report(self) -> CommandReport:
        now = self._services.monotonic()
        facts = self._facts
        exits = facts.nonzero_exits if facts is not None else ()
        if self._exits_before_stop is not None:
            exits = exits[: self._exits_before_stop]
        ended_at = self._ended_at if self._ended_at is not None else now
        return CommandReport(
            command_id=self.command_id,
            owner=self.owner,
            pid=self._pid,
            command=self._command,
            description=self._description,
            workdir=self._workdir,
            ended=self.ended,
            finished=self.finished,
            exit_code=self._exit_code,
            stop_reason=self._stop_reason,
            transcript=self._output.transcript(),
            log_path=self._log_path,
            failed_programs=tuple(program.describe() for program in exits),
            still_running=(
                facts.running if facts is not None and self.ended and not self.finished else ()
            ),
            background=self._background,
            delivers_result=self._delivering,
            elapsed_seconds=max(0.0, ended_at - self.started_at),
        )

    def latest(self, lines: int) -> str:
        return self._output.latest(lines)

    # Lifecycle

    def start(self) -> None:
        self._reader_task = self._task(self._read(), "reader")
        self._supervisor_task = self._task(self._supervise(), "supervisor")
        if self._on_output is not None:
            self._progress_task = self._task(self._publish_progress(), "progress")

    async def wait(self, seconds: float | None) -> CommandWaitOutcome:
        if seconds is None:
            await self._ended.wait()
        else:
            await self._wait_or_sleep(self._ended, seconds)
        return "ended" if self.ended else "deadline"

    async def wait_finished(self) -> None:
        await self._finished.wait()

    def to_background(self, *, deliver: bool) -> None:
        if not self._background:
            self._background = True
            self._cancel(self._progress_task)
        if deliver and not self.ended:
            self._deliver = True
        self._changed()

    def transfer(self, source: CommandOwner, target: CommandOwner) -> bool:
        if self.owner != source:
            return False
        self.owner = target
        sink = self._services.trigger_service
        body = self._delivered_body
        if (
            sink is not None
            and body is not None
            and self._delivered_to != target
            and self._withdraw_submitted()
        ):
            self._submit(sink, body)
        return True

    async def stop(self, reason: StopReason) -> None:
        """Stop the command and every process it started; returns once none runs.

        The first reason wins. ``scope`` withdraws the result delivery; ``agent``
        delivers nothing more, since the kill's own result shows the outcome.
        Raises ``CommandStopError`` when the kill is not confirmed. Unless the
        command ended meanwhile (an interrupt sent before the kill can end
        it), it then runs on as if this stop had not happened: an end it
        reaches by itself reports its exit code, and its result is delivered
        unless a ``scope`` stop withdrew the delivery.
        """
        claimed = self._request_stop(reason)
        if reason == "scope":
            self.withdraw_delivery()
        froze = False
        try:
            async with self._stop_lock:
                # A stop before this one whose kill failed gave its reason back.
                claimed = self._request_stop(reason) or claimed
                if not self.finished and not self._killed.is_set():
                    froze = self._freeze_exits()
                    if not self._interrupted:
                        self._interrupted = True
                        if self._process.interrupt():
                            await self._wait_or_sleep(self._finished, COMMAND_STOP_GRACE_SECONDS)
                    if not self.finished:
                        await self._kill()
        except CommandStopError:
            if not self.ended:
                # The command runs on; what it does from now on is its own.
                if claimed:
                    self._stop_reason = None
                if froze:
                    self._exits_before_stop = None
            raise
        await self._finished.wait()

    def acknowledge(self) -> None:
        """A durable result showed the command's end: deliver nothing more.

        A delivery ``TriggerService`` has not handed over yet is withdrawn; one
        it handed over stays. Before the end, nothing changes.
        """
        if self.ended:
            self.withdraw_delivery()

    def withdraw_delivery(self) -> None:
        """Deliver no result: disarm it, and withdraw one already submitted.

        A submitted result whose delivery already claimed it stays: it is
        delivered once, as submitted.
        """
        self._deliver = False
        self._withdraw_submitted()

    async def kill_and_release(self) -> bool:
        """Kill every process off the Event Loop, withdraw the delivery and release
        the record (Runtime stop).

        Returns whether the kill was confirmed. The record is released either
        way, so Runtime stop never waits for processes that may still run; an
        unconfirmed kill logs them.
        """
        self._request_stop("shutdown")
        self.withdraw_delivery()
        try:
            if self.finished:
                return True
            self._freeze_exits()
            try:
                await self._services.run_blocking(self._tree.terminate)
            except OSError:
                self._log_abandoned(await self._services.run_blocking(self._running_now))
                return False
            self._killed.set()
            return True
        finally:
            self._abandon()

    def kill_now(self) -> bool:
        """``kill_and_release`` with the kill on the calling thread, also without an
        Event Loop."""
        self._request_stop("shutdown")
        self.withdraw_delivery()
        try:
            if self.finished:
                return True
            self._freeze_exits()
            try:
                self._tree.terminate()
            except OSError:
                self._log_abandoned(self._running_now())
                return False
            self._killed.set()
            return True
        finally:
            self._abandon()

    def _log_abandoned(self, survivors: tuple[RunningProcess, ...] | None) -> None:
        _LOGGER.warning(
            "The kill of command %s at shutdown was not confirmed; vBot no longer follows "
            "its processes, which may still run: %s",
            self.command_id,
            "unknown"
            if survivors is None
            else ", ".join(f"{process.name} (pid {process.pid})" for process in survivors),
        )

    def _abandon(self) -> None:
        """Stop following the command and release it: no process runs any
        longer, or vBot gives up on them (Runtime stop)."""
        for task in (
            self._reader_task,
            self._supervisor_task,
            self._progress_task,
            self._flush_task,
            self._delivery_task,
        ):
            if task is not None and not task.done():
                # Without a running Event Loop, nothing is left to cancel.
                with contextlib.suppress(RuntimeError):
                    task.cancel()
        self._release()
        # No process runs any longer: a call waiting for the end, or a handoff
        # waiting for the finish, returns.
        if self._ended_at is None:
            self._ended_at = self._services.monotonic()
        if self._finished_at is None:
            self._finished_at = self._services.monotonic()
        for event in (self._ended, self._finished):
            # The flag is set first; waking a waiter fails only on a closed Event Loop.
            with contextlib.suppress(RuntimeError):
                event.set()

    def pending_tasks(self) -> list[asyncio.Task[Any]]:
        return [
            task
            for task in (
                self._reader_task,
                self._supervisor_task,
                self._progress_task,
                self._flush_task,
                self._delivery_task,
            )
            if task is not None and not task.done()
        ]

    # Output

    async def _read(self) -> None:
        try:
            try:
                # The next chunk is read once the previous one is cleaned: a
                # command that prints faster waits for its pipe.
                while data := await self._process.read():
                    self._activity += 1
                    if not self._output.failed:
                        await self._feed(data)
            except Exception:
                _LOGGER.error(
                    "Reading the output of command %s failed; it keeps running until it "
                    "ends or is stopped",
                    self.command_id,
                    exc_info=True,
                )
            await self._output.end()
        except Exception:
            _LOGGER.error(
                "Completing the output of command %s failed", self.command_id, exc_info=True
            )
        finally:
            self._eof.set()
            self._output_changed()

    async def _feed(self, data: bytes) -> None:
        try:
            await self._output.feed(data)
        except Exception:
            # The pipe is still read, so the command never waits on a full pipe.
            _LOGGER.error(
                "Cleaning the output of command %s failed; its later output is read and discarded",
                self.command_id,
                exc_info=True,
            )
        self._output_changed()

    def _output_changed(self) -> None:
        if self._output.log_pending and (self._flush_task is None or self._flush_task.done()):
            self._flush_task = self._task(self._flush_later(), "log")
        if not self._background:
            self._progress.set()

    async def _flush_later(self) -> None:
        await self._services.sleep(COMMAND_LOG_REFRESH_SECONDS)
        await self._output.flush_log()

    async def _publish_progress(self) -> None:
        callback = self._on_output
        if callback is None:
            return
        try:
            while not self.ended:
                await self._progress.wait()
                self._progress.clear()
                if self.ended or self._background:
                    return
                await callback(self._output.latest(COMMAND_PROGRESS_LINES))
                await self._services.sleep(COMMAND_PROGRESS_SECONDS)
        except Exception:
            _LOGGER.warning(
                "Publishing the output of command %s failed; its later output is not published",
                self.command_id,
                exc_info=True,
            )

    # End detection

    async def _supervise(self) -> None:
        try:
            exit_code = await self._process.wait()
            await self._drain()
            facts = await self._tree_facts()
            self._record_exit(exit_code, facts)
            quiet = _QuietWatch()
            while not self._killed.is_set():
                if facts is not None:
                    if not facts.running:
                        break
                    if self.ended:
                        self._follow_leftovers(facts)
                    elif self._leftovers_quiet(quiet, facts):
                        self._record_end(facts)
                await self._wait_or_sleep(self._killed, TREE_POLL_SECONDS)
                if self._killed.is_set():
                    break
                facts = await self._tree_facts()
            await self._drain()
            self._process.close_output()
            await self._eof.wait()
            if facts is not None:
                facts = replace(facts, running=())
            self._finish(facts)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.error(
                "Following command %s failed; its processes are stopped",
                self.command_id,
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                await self._services.run_blocking(self._tree.terminate)
            self._finish(None)

    def _record_exit(self, exit_code: int, facts: ProcessTreeFacts | None) -> None:
        """The shell exited: fix its exit code; the command ends unless processes run.

        Processes that cannot be inspected (*facts* None) may still run: the
        command keeps running, so its call moves it to the background as any
        running command and it stays listed and stoppable, until a later poll
        shows what runs.
        """
        self._exit_code = exit_code
        if facts is None:
            return
        self._facts = facts
        if not facts.running:
            self._record_end(facts)

    def _leftovers_quiet(self, quiet: _QuietWatch, facts: ProcessTreeFacts) -> bool:
        """Whether the processes the exited shell left running were quiet long enough.

        Output, a new process or CPU time beyond a trace restarts the quiet
        period; so does an exit that took its CPU time out of the sum (POSIX
        counts running processes only).
        """
        now = self._services.monotonic()
        if (
            quiet.activity != self._activity
            or abs(facts.cpu_seconds - quiet.cpu_seconds) > IDLE_CPU_SECONDS
            or facts.started != quiet.started
        ):
            quiet.activity = self._activity
            quiet.since = now
            quiet.cpu_seconds = facts.cpu_seconds
            quiet.started = facts.started
            return False
        return now - quiet.since >= COMMAND_LEFTOVER_QUIET_SECONDS

    def _follow_leftovers(self, facts: ProcessTreeFacts) -> None:
        """After the end: keep the processes still running current.

        Listeners hear when one of them ended, so a view of what still runs
        follows; the command's status stays as it ended.
        """
        before = {process.pid for process in self._facts.running} if self._facts else set()
        self._facts = facts
        if before - {process.pid for process in facts.running}:
            self._changed()

    def _record_end(self, facts: ProcessTreeFacts | None) -> None:
        if self.ended:
            return
        if facts is not None:
            self._facts = facts
        self._ended_at = self._services.monotonic()
        self._ended.set()
        self._cancel(self._progress_task)
        if self._background and self._delivering and self._services.trigger_service is not None:
            self._submit_result(self._services.trigger_service)
        self._changed()

    def _finish(self, facts: ProcessTreeFacts | None) -> None:
        """No process of the command runs any longer: fix the outcome and release."""
        if self.finished:
            return
        self._record_end(facts)
        if self._facts is not None and self._facts.running:
            self._facts = replace(self._facts, running=())
        self._cancel(self._flush_task)
        self._release()
        self._finished_at = self._services.monotonic()
        self._finished.set()
        self._changed()

    def _release(self) -> None:
        self._output.close()
        if self._log_lease is not None:
            self._log_lease.finish()
            self._log_lease = None
        with contextlib.suppress(Exception):
            self._tree.close()
        with contextlib.suppress(Exception):
            self._process.close()

    async def _tree_facts(self) -> ProcessTreeFacts | None:
        """The command's processes now, without the console host; None when unknown."""
        try:
            facts: ProcessTreeFacts = await self._services.run_blocking(self._tree.facts)
        except Exception:
            if not self._facts_failing:
                self._facts_failing = True
                _LOGGER.warning(
                    "Could not inspect the processes of command %s; it stays live until "
                    "they are confirmed ended",
                    self.command_id,
                    exc_info=True,
                )
            return None
        if self._facts_failing:
            self._facts_failing = False
            _LOGGER.info("Inspecting the processes of command %s recovered", self.command_id)
        running = tuple(
            process for process in facts.running if process.name.lower() != _CONSOLE_HOST
        )
        return replace(facts, running=running)

    async def _drain(self) -> None:
        await self._wait_or_sleep(self._eof, COMMAND_DRAIN_SECONDS)

    # Stopping

    def _request_stop(self, reason: StopReason) -> bool:
        """Claim *reason* for a command that runs and has none yet; whether it did."""
        if self._stop_reason is not None or self.ended:
            return False
        self._stop_reason = reason
        return True

    def _freeze_exits(self) -> bool:
        """Before vBot interrupts or kills: count the failed child exits so far.

        Exits recorded later were caused by the stop and are no failures of
        the command, also for processes left running after its end. Returns
        whether this call fixed the count.
        """
        if self._exits_before_stop is not None:
            return False
        try:
            self._exits_before_stop = self._tree.exit_count()
        except Exception:
            return False
        return True

    def _running_now(self) -> tuple[RunningProcess, ...] | None:
        """The command's processes still running, without the console host; None
        when unknown. Blocks."""
        try:
            running = self._tree.facts().running
        except Exception:
            return None
        return tuple(process for process in running if process.name.lower() != _CONSOLE_HOST)

    async def _kill(self) -> None:
        try:
            await self._services.run_blocking(self._tree.terminate)
        except OSError as error:
            facts = await self._tree_facts()
            survivors = facts.running if facts is not None else ()
            raise CommandStopError(
                f"Could not stop command {self.command_id}: the operating system did not "
                "confirm that its processes ended",
                survivors=survivors,
            ) from error
        self._killed.set()

    # Delivery

    def _submit_result(self, sink: CompletionSink) -> None:
        try:
            body = self._formatter(self.report())
        except Exception:
            _LOGGER.error(
                "Could not deliver the result of command %s", self.command_id, exc_info=True
            )
            return
        self._submit(sink, body)

    def _submit(self, sink: CompletionSink, body: str) -> None:
        """Submit the result *body* to the command's Session."""
        self._delivered_to = self.owner
        self._delivered_body = body
        try:
            delivery = asyncio.ensure_future(
                sink.submit_completion(
                    self.owner.agent_id,
                    self.owner.session_id,
                    notice_id=self.notice_id,
                    origin_run_id=self.origin_run_id,
                    body=body,
                    project_id=self.owner.project_id,
                    execution_owner=self.execution_owner,
                    wake=True,
                )
            )
        except Exception:
            _LOGGER.error(
                "Could not deliver the result of command %s", self.command_id, exc_info=True
            )
            return
        self._delivery_task = self._task(self._await_delivery(delivery), "delivery")

    def _withdraw_submitted(self) -> bool:
        """Withdraw a submitted result not yet delivered; whether one was withdrawn.

        A result whose delivery the sink already claimed stays: it is delivered
        once, as submitted.
        """
        task = self._delivery_task
        if task is None or task.done() or not self._withdraw_submission():
            return False
        # Without a running Event Loop (Runtime stop), nothing is left to cancel.
        with contextlib.suppress(RuntimeError):
            task.cancel()
        return True

    def _withdraw_submission(self) -> bool:
        """Withdraw the submitted result from the sink; whether it was withdrawn."""
        sink = self._services.trigger_service
        if sink is None:
            return True
        target = self._delivered_to
        try:
            return sink.cancel_completion(
                target.agent_id,
                target.session_id,
                notice_id=self.notice_id,
                project_id=target.project_id,
            )
        except Exception:
            _LOGGER.warning(
                "Could not withdraw the result of command %s", self.command_id, exc_info=True
            )
            return False

    async def _await_delivery(self, delivery: Awaitable[None]) -> None:
        try:
            # Only the sink withdraws a submitted result; stopping to follow
            # the delivery leaves it alone.
            await asyncio.shield(delivery)
        except RunAdmissionBlockedError:
            # The command's execution owner closed (for example its temporary
            # group): an expected end of its lifecycle, not a delivery fault.
            _LOGGER.debug(
                "Command result dropped for a closed execution owner (command=%s)",
                self.command_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.warning(
                "Delivering the result of command %s failed", self.command_id, exc_info=True
            )

    # Helpers

    def _changed(self) -> None:
        if self._background:
            self._services.changed(self.command_id)

    async def _wait_or_sleep(self, event: asyncio.Event, seconds: float) -> None:
        """Wait until *event* is set or *seconds* pass on the manager's clock."""
        if event.is_set():
            return
        happened = asyncio.ensure_future(event.wait())
        pause = asyncio.ensure_future(self._services.sleep(seconds))
        try:
            await asyncio.wait({happened, pause}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in (happened, pause):
                waiter.cancel()
            await asyncio.gather(happened, pause, return_exceptions=True)

    def _task(self, coroutine: Coroutine[Any, Any, None], label: str) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine, name=f"command-{label}:{self.command_id}")
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error("Command task %s failed", task.get_name(), exc_info=task.exception())

    @staticmethod
    def _cancel(task: asyncio.Task[Any] | None) -> None:
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()


__all__ = ["BlockingRunner", "CommandRecord", "CommandServices"]
