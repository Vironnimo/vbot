"""One interactive Terminal Session: process I/O, screen, activity and delivery."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncGenerator, Callable, Coroutine
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from core.runs import RunExecutionOwner
from core.storage.temp_files import TemporaryFileLease
from core.tools import terminal_backend
from core.tools._terminal_remote import RemoteTerminalAdapter
from core.tools.terminal_backend import TerminalAdapter
from core.tools.terminal_emulator import (
    EmulatorUpdate,
    ScreenObservation,
)
from core.utils.logging import get_logger

from ._terminal_activity import (
    PROCESS_SAMPLE_SECONDS,
    ProcessSample,
    QuietRestart,
    TerminalActivity,
)
from ._terminal_attention import TerminalNotices
from ._terminal_log import TerminalLog
from ._terminal_output import TerminalOutput
from ._terminal_render_host import TerminalScreen
from ._terminal_shell_state import ShellState
from ._terminal_state import (
    _CONDITIONS,
    TERMINAL_REPAINT_WINDOW_SECONDS,
    TerminalAlreadyAttachedError,
    TerminalInfo,
    TerminalLaunch,
    TerminalManagerError,
    TerminalObservation,
    TerminalOwner,
    TerminalSessionServices,
    TerminalStreamEvent,
    WaitEnded,
    _log_task_failure,
    _new_terminal_stream,
    _utc_now,
    _validate_dimensions,
)
from ._terminal_waits import TerminalWaits
from ._terminal_writing import TerminalWriting
from .process_tree import TREE_POLL_SECONDS, ProcessTree

_LOGGER = get_logger("tools.terminal_manager")
_OPERATOR_READ_HISTORY_LINES = 30


class TerminalSession:
    """Own one terminal process: its output, rendered screen, activity and attention.

    The screen revision names the content an observation saw: program output
    that may change the screen and real input advance it; a resize and the
    program's redraw for the new size do not, so input guarded with the
    revision of a delivered screen still applies after an operator resize.
    """

    def __init__(
        self,
        terminal_id: str,
        *,
        owner: TerminalOwner | None,
        adapter: TerminalAdapter,
        screen: TerminalScreen,
        launch: TerminalLaunch,
        call_name: str,
        group_id: str | None,
        origin_run_id: str | None,
        execution_owner: RunExecutionOwner | None,
        log_path: Path | None,
        log_handle: BinaryIO | None,
        log_lease: TemporaryFileLease | None,
        cleanup: Callable[[], None] | None,
        services: TerminalSessionServices,
        observer: ProcessTree | None = None,
    ) -> None:
        """*observer* watches the program's process tree, whose CPU time and new
        processes show work its output does not."""
        self.terminal_id = terminal_id
        self.owner = owner
        self.lifecycle_owner = owner
        self.attachment = owner
        self.call_name = call_name
        self.group_id = group_id
        self.origin_run_id = origin_run_id
        self.execution_owner = execution_owner
        self.started_at = _utc_now()
        self.finished_at: datetime | None = None
        self.exit_code: int | None = None
        self._launch = launch
        # The prompt and folder an integrated shell reports (Permission Rules).
        self.shell_state = ShellState(str(launch.cwd)) if launch.shell is not None else None
        self._adapter = adapter
        self._services = services
        self._lock = asyncio.Lock()
        self._observer = observer
        self._activity = TerminalActivity(
            repaint_window=TERMINAL_REPAINT_WINDOW_SECONDS,
            quiet_seconds=services.activity_quiet_seconds,
            idle_seconds=services.idle_seconds,
        )
        self._title = ""
        self._alternate_screen = False
        self._output_count = 0
        self._settles = 0
        self._shown_outputs = 0
        self._snapshot_on_settle = False
        self._suppress_exit_attention = False
        self._stopped = False
        self._termination_pending = False
        self._termination_targets: list[Any] = []
        self._output_event = asyncio.Event()
        self._reader_task: asyncio.Task[None] | None = None
        self._settle_task: asyncio.Task[None] | None = None
        self._sample_task: asyncio.Task[None] | None = None
        self._stream_sequence = 0
        self._stream = _new_terminal_stream()
        self._cleanup = cleanup
        self._tree_killed = asyncio.Event()
        self._output = TerminalOutput(
            terminal_id,
            screen,
            TerminalLog(terminal_id, log_path, log_handle, log_lease),
            lock=self._lock,
            services=services,
            finished=lambda: self.finished,
            background=self._background,
            replaced=self._screen_replaced,
        )
        self._notices = TerminalNotices(
            terminal_id,
            output=self._output,
            services=services,
            info=self.info,
            origin=lambda: (
                self._writing.origin_run_id or self.origin_run_id,
                self._writing.execution_owner,
            ),
            background=self._background,
            publish=self._publish_state,
            signal=lambda: self._waits.signal_change(),
        )
        self._waits = TerminalWaits(
            self._output, self._notices, self._activity, services, self._lock
        )
        self._writing = TerminalWriting(
            terminal_id,
            adapter=adapter,
            output=self._output,
            notices=self._notices,
            waits=self._waits,
            activity=self._activity,
            services=services,
            lock=self._lock,
            info=self.info,
            reader=lambda: self._reader_task,
            before_input=self._running_before_input,
            restart=self._restart_quiet,
            publish=self._publish_state,
            output_event=self._output_event,
            execution_owner=execution_owner,
        )

    # Facts

    @property
    def state(self) -> str:
        return self._activity.state

    @property
    def finished(self) -> bool:
        return self._activity.finished

    @property
    def pid(self) -> int:
        return self._adapter.pid

    @property
    def launch(self) -> TerminalLaunch:
        return self._launch

    def info(self) -> TerminalInfo:
        remote = self._adapter if isinstance(self._adapter, RemoteTerminalAdapter) else None
        metadata = remote.metadata if remote is not None else {}
        return TerminalInfo(
            terminal_id=self.terminal_id,
            owner=self.owner,
            lifecycle_owner=self.lifecycle_owner,
            attachment=self.attachment,
            state=self._activity.state,
            command=str(metadata.get("command", self._launch.command)),
            arguments=tuple(metadata.get("args", self._launch.arguments)),
            launch_command=self._launch.launch_command,
            launch_arguments=self._launch.launch_arguments,
            call_name=self.call_name,
            group_id=self.group_id,
            cwd=Path(str(metadata.get("workdir", self._launch.cwd))),
            pid=self._adapter.pid,
            started_at=self.started_at,
            finished_at=self.finished_at,
            exit_code=self.exit_code,
            title=self._title,
            columns=self._output.screen.columns,
            rows=self._output.screen.rows,
            alternate_screen=self._alternate_screen,
            screen_revision=self._output.revision,
            attention=self._notices.current,
            attention_revision=self._notices.revision,
            acknowledged_attention_revision=self._notices.acknowledged_revision,
            log_path=self._output.log.path,
            stopped=self._stopped,
            permission_scope=self._launch.permission_scope,
            shell=self._launch.shell,
            computer_id=self._launch.computer_id,
            computer_name=self._launch.computer_name,
            computer_online=(
                self._adapter.online if isinstance(self._adapter, RemoteTerminalAdapter) else True
            ),
            launch_pending=remote.launch_pending if remote is not None else False,
            launch_note=str(metadata.get("note", "")),
        )

    # Lifecycle

    def start(self) -> None:
        """Start reading output."""
        self._reader_task = self._background(self._read(), "reader")
        self._publish_state()

    async def terminate(self, *, suppress_attention: bool) -> None:
        """Stop the process tree; a failure leaves the Session unfinished for a retry."""
        if not self.finished:
            self._stopped = True
        if suppress_attention:
            self._suppress_exit_attention = True
            self._notices.cancel()
        self._cancel_task(self._settle_task)
        self._cancel_task(self._sample_task)
        if self._termination_pending or not self.finished:
            self._termination_pending = True
            try:
                await asyncio.to_thread(self._kill_tree)
            except OSError as error:
                raise TerminalManagerError(
                    f"Could not stop terminal {self.terminal_id}: the operating system did not "
                    "end its processes, so they can still be running"
                ) from error
            self._termination_pending = False
            self._tree_killed.set()
        await asyncio.to_thread(self._adapter.close)
        reader = self._reader_task
        if reader is not None and reader is not asyncio.current_task() and not reader.done():
            try:
                await asyncio.wait_for(asyncio.shield(reader), timeout=5)
            except TimeoutError:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
        if not self.finished:
            await self._mark_finished()

    def stop_now(self) -> bool:
        """Synchronously stop the process for Runtime shutdown; False if the tree survived."""
        self._suppress_exit_attention = True
        self._notices.cancel()
        if isinstance(self._adapter, RemoteTerminalAdapter):
            # Runtime shutdown runs synchronously on its own Event Loop. The
            # Desktop owns these processes and detects a new server instance.
            self._adapter.detach()
            for pending in self.pending_tasks():
                self._cancel_task(pending)
            self._finish_files()
            return True
        if self._termination_pending or not self.finished:
            self._termination_pending = True
            try:
                self._kill_tree()
            except OSError:
                return False
            self._termination_pending = False
        self._adapter.close()
        for task in (
            self._reader_task,
            self._settle_task,
            self._sample_task,
            self._output.redraw_task,
        ):
            self._cancel_task(task)
        self._finish_files()
        return True

    def _kill_tree(self) -> None:
        """Kill the process tree (blocking); OSError when that is not confirmed."""
        if isinstance(self._adapter, RemoteTerminalAdapter):
            self._adapter.terminate()
            return
        terminal_backend.terminate_process_tree(self._adapter, targets=self._termination_targets)

    def program_runs(self, program: str) -> bool:
        """Probe the program on the computer that owns this Terminal's PTY."""
        return self._services.program_probe(self.pid, program)

    def pending_tasks(self) -> list[asyncio.Task[Any]]:
        """Background tasks to await after ``stop_now``."""
        if self._termination_pending:
            return []
        return self._output.log.pending_tasks() + [
            task
            for task in (
                self._reader_task,
                self._settle_task,
                self._sample_task,
                self._notices.task,
                self._output.redraw_task,
                self._writing.writer_task,
            )
            if task is not None and not task.done()
        ]

    def release(self) -> None:
        """Drop a finished Session's retained resources when it leaves the catalog."""
        self._notices.cancel()
        self._cancel_task(self._output.redraw_task)
        self._finish_files()
        self._output.screen.close()
        for kind in ("render", "skipped", "end_check", "process_sample"):
            _CONDITIONS.ended((self.terminal_id, kind))

    # Waiting

    async def _await_end(self) -> None:
        """Wait until the Session's process has ended, or vBot killed it.

        Output has ended or can no longer be read, so the process is polled
        until it exits.
        """
        while not self._tree_killed.is_set():
            if await self._processes_ended():
                return
            await self._waits.wait_or_sleep(self._tree_killed, TREE_POLL_SECONDS)

    async def _processes_ended(self) -> bool:
        """Whether the Session's process has ended; False while that is unknown."""
        key = (self.terminal_id, "end_check")
        try:
            ended = not await asyncio.to_thread(self._adapter.is_alive)
        except Exception:
            if _CONDITIONS.started(key):
                _LOGGER.warning(
                    "Terminal process check failed; the Session stays live until its "
                    "processes are confirmed ended (terminal=%s)",
                    self.terminal_id,
                    exc_info=True,
                )
            return False
        if _CONDITIONS.ended(key):
            _LOGGER.info("Terminal process check recovered (terminal=%s)", self.terminal_id)
        return ended

    async def wait_for_program(
        self,
        *,
        deadline: float,
        pattern: re.Pattern[str] | None,
        after_revision: int,
        on_match: Callable[[str], None] | None = None,
    ) -> WaitEnded:
        return await self._waits.wait_for_program(
            deadline=deadline, pattern=pattern, after_revision=after_revision, on_match=on_match
        )

    async def wait_for_reply(self, *, deadline: float, after_quiet: int) -> WaitEnded:
        return await self._waits.wait_for_reply(deadline=deadline, after_quiet=after_quiet)

    async def wait_started(self, *, deadline: float) -> None:
        await self._waits.wait_started(deadline=deadline)

    def acknowledge_exit(self) -> None:
        self._notices.acknowledge_exit()

    def acknowledge_attention(self, revision: int) -> None:
        self._notices.acknowledge(revision)

    # Attachment

    def attach(
        self,
        attachment: TerminalOwner,
        *,
        origin_run_id: str,
        execution_owner: RunExecutionOwner | None,
    ) -> bool:
        """Bind this Session to one Agent Session; True when the binding is new."""
        self._writing.require_live()
        if self.attachment is not None and self.attachment != attachment:
            raise TerminalAlreadyAttachedError(
                "Terminal Session is already attached to another vBot Session."
            )
        changed = self.attachment is None
        if changed:
            self._output.observed = None
            self._output.seen_line = None
        self.attachment = attachment
        self._writing.origin_run_id = origin_run_id
        self._writing.execution_owner = execution_owner
        self._notices.acknowledged_revision = self._notices.revision
        restart = self._activity.attach(delivery_pending=self._notices.pending())
        if restart is not None:
            self._restart_quiet(restart)
        if changed:
            self._publish_state()
        return changed

    def detach(self) -> None:
        """Clear the attachment and its delivery state without touching the process."""
        self._notices.cancel()
        self.attachment = None
        self._writing.origin_run_id = None
        self._writing.execution_owner = None
        self._activity.detach()
        self._output.observed = None
        self._output.seen_line = None
        self._publish_state()

    def transfer(self, source: TerminalOwner, target: TerminalOwner) -> bool:
        """Move matching lifecycle and attachment scopes; reroute a pending delivery."""
        lifecycle_matches = self.lifecycle_owner == source
        attachment_matches = self.attachment == source
        if not lifecycle_matches and not attachment_matches:
            return False
        attention = self._notices.current
        pending = attachment_matches and attention is not None and self._notices.running()
        if pending:
            self._notices.cancel()
        if lifecycle_matches:
            self.lifecycle_owner = target
        if attachment_matches:
            self.attachment = target
        self._publish_state()
        if pending and attention is not None:
            self._notices.schedule(
                attention, self._notices.body, self._notices.seen_line, self._notices.clears
            )
        return True

    # Input and size

    async def send_input(
        self,
        *,
        text: str | None,
        key: str | None,
        expected_screen_revision: int | None,
        origin_run_id: str,
        data: str | None = None,
        execution_owner: RunExecutionOwner | None = None,
    ) -> dict[str, Any]:
        return await self._writing.send_input(
            text=text,
            key=key,
            expected_screen_revision=expected_screen_revision,
            origin_run_id=origin_run_id,
            data=data,
            execution_owner=execution_owner,
        )

    async def send_operator_input(
        self,
        data: str,
        *,
        expected_screen_revision: int | None = None,
        expected_program: str | None = None,
    ) -> None:
        await self._writing.send_operator_input(
            data,
            expected_screen_revision=expected_screen_revision,
            expected_program=expected_program,
        )

    async def release_focus(self) -> None:
        await self._writing.release_focus()

    async def resize(self, columns: int, rows: int) -> dict[str, Any]:
        """Resize the PTY and the rendered screen together."""
        _validate_dimensions(columns, rows)
        async with self._lock:
            self._writing.require_live()
            if (columns, rows) != (self._output.screen.columns, self._output.screen.rows):
                # A resize to the current dimensions would only make the
                # program repaint, so it is skipped.
                await self._resize_screen(columns, rows)
            return {
                "terminal_id": self.terminal_id,
                "state": self.state,
                "columns": columns,
                "rows": rows,
                "screen_revision": self._output.revision,
            }

    async def resize_for_agent(self, columns: int, rows: int, *, deadline: float) -> bool:
        """Resize for the attached Agent, then wait until the program redrew.

        The wait ends at the first quiet boundary after the resize, once the
        repaint window passed without output that showed something new (the
        program does not redraw), when the program ends, or at *deadline*.
        False when the terminal already had this size.
        """
        _validate_dimensions(columns, rows)
        async with self._lock:
            self._writing.require_live()
            if (columns, rows) == (self._output.screen.columns, self._output.screen.rows):
                return False
            settles, shown = self._settles, self._shown_outputs
            await self._resize_screen(columns, rows)
            if self._activity.state == "working":
                # Work in progress settles after its redraw, not on its earlier timer.
                self._restart_quiet(QuietRestart(self._activity.generation, False))
        services = self._services
        silent_until = services.monotonic() + TERMINAL_REPAINT_WINDOW_SECONDS
        while not self.finished and self._settles == settles:
            change = self._waits.change
            now = services.monotonic()
            silent = self._shown_outputs == shown
            if now >= deadline or (silent and now >= silent_until):
                break
            await self._waits.wait_or_sleep(change, (silent_until if silent else deadline) - now)
        return True

    async def _resize_screen(self, columns: int, rows: int) -> None:
        """Resize the PTY and the screen to new dimensions; the lock is held."""
        await asyncio.to_thread(self._adapter.resize, rows, columns)
        self._output.resizes.append((self._output.log.size, columns, rows))
        try:
            await self._output.screen.resize(columns, rows)
        except Exception as error:
            self._output.render_failed(error, self._output.log.size)
        self._output.resize_count += 1
        self._activity.expect_redraw(self._services.monotonic())
        self._publish_state()

    # Observation

    async def snapshot(self, *, lines: int, start_line: int | None) -> dict[str, Any]:
        async with self._lock:
            return await self._output.snapshot(
                self.info(), self._activity.idle_reason, lines=lines, start_line=start_line
            )

    def acknowledge_screen(self, observation: TerminalObservation) -> None:
        """Remember a durably delivered screen without consuming a later resize.

        The Agent has seen it: an idle boundary showing the same screen wakes
        nobody, and an idle delivery of that screen on its way is cancelled
        (its boundary can come before the result is durable).
        """
        if self._output.acknowledge(observation):
            self._activity.acknowledge(observation.signature)
        attention = self._notices.current
        if (
            attention is not None
            and attention.kind == "idle"
            and attention.details.get("screen_revision") == observation.screen_revision
        ):
            self._notices.acknowledge(attention.revision)

    async def read_for_operator(self) -> dict[str, Any]:
        """Read the screen without changing any Agent's observation or binding."""
        async with self._lock:
            screen = await self._output.screen.screen_text()
            page = await self._output.screen.page(
                start_line=None, limit=_OPERATOR_READ_HISTORY_LINES
            )
            info = self.info()
        return {
            "terminal": self._services.operator_summary(info),
            "screen": screen,
            "scrollback": page,
            "bracketed_paste": self._writing.bracketed_paste,
        }

    async def watch(self) -> AsyncGenerator[TerminalStreamEvent]:
        """Yield an authoritative VT snapshot followed by sequenced live events."""
        async with self._lock:
            after_sequence = self._stream_sequence
            ready: TerminalStreamEvent = {
                "type": "terminal_ready",
                "sequence": after_sequence,
                "terminal": self._services.operator_summary(self.info()),
                "ansi": await self._output.screen.ansi_snapshot(),
            }
        yield ready
        if self.finished:
            return
        async with contextlib.aclosing(
            self._stream.subscribe(after_sequence=after_sequence)
        ) as events:
            async for event in events:
                yield event

    # Output and activity

    async def _read(self) -> None:
        """Read output, then keep the Session live until its processes have ended.

        The end of output - EOF, a closed PTY, or a failure to read or handle
        it - is no end of the program: the Session finishes only once its
        process has ended, or once vBot killed it, which ends the wait or
        cancels this task.
        """
        ended = False
        try:
            await self._output.read(
                self._adapter, self._services.reader_executor, self._render_output
            )
            await self._await_end()
            ended = True
        except asyncio.CancelledError:
            # Only a kill or a shutdown cancels the reader, after it stopped the processes.
            ended = True
            raise
        finally:
            if ended:
                await self._mark_finished()

    async def _render_output(self, received: str) -> None:
        """Log, stream and render one chunk of output; the lock is held.

        A failing log or renderer degrades the Session instead of ending it:
        its output keeps being read, so the program neither stalls nor ends.
        """
        if self.shell_state is not None:
            self.shell_state.feed(received)
        update = await self._output.append(
            received, alternate=self._alternate_screen, publish=self._publish_output
        )
        self._writing.last_output_at = self._services.monotonic()
        facts_changed = False
        if update is not None:
            if update.cleared_line is not None:
                self._output.cleared(update.cleared_line)
            facts_changed = await self._apply_update(update)
        effect = self._activity.output(
            self._services.monotonic(),
            attached=self.attachment is not None,
            delivery_pending=self._notices.pending(),
            signals=() if update is None else update.signals,
            # A screen that missed the output cannot tell what changed.
            view="new" if update is None else update.view_change,
        )
        if effect.changes_screen:
            self._output.revision += 1
        if effect.restart is not None:
            self._shown_outputs += 1
            self._restart_quiet(effect.restart)
        if effect.state_changed or facts_changed:
            self._publish_state()
        self._output_count += 1
        self._output_event.set()
        self._waits.signal_change()

    async def _screen_replaced(
        self, update: EmulatorUpdate | None, cleared_line: int | None
    ) -> None:
        if update is not None:
            if cleared_line is not None:
                self._output.cleared(cleared_line)
            # Replies to queries the program sent long ago would arrive as input now.
            await self._apply_update(update._replace(responses="", alternate_exited=False))
        # Its content can differ from what any result showed.
        self._output.revision += 1
        if _CONDITIONS.ended((self.terminal_id, "render")):
            _LOGGER.info("Terminal screen drawn anew from its log (terminal=%s)", self.terminal_id)
        settle = self._settle_task
        if settle is None or settle.done():
            # The quiet boundary the screen missed while it was behind.
            generation = self._activity.generation
            self._settle_task = self._background(
                self._settle_after_quiet(generation), f"settle:{generation}"
            )
        self._publish_state()
        self._waits.signal_change()

    async def _apply_update(self, update: EmulatorUpdate) -> bool:
        """Apply what rendering found; True when the title or the alternate screen changed."""
        if update.responses:
            # Terminal protocol replies, not input: they start no activity. Whether
            # a program that can no longer read them still runs is checked apart.
            self._writing.queue_protocol(update.responses)
        paste_disabled = self._writing.observe_modes(update)
        facts_changed = (update.title, update.alternate_screen) != (
            self._title,
            self._alternate_screen,
        )
        self._title = update.title
        self._alternate_screen = update.alternate_screen
        if update.alternate_exited:
            try:
                await self._publish_snapshot()
            except Exception:
                # Viewers resynchronize at the next snapshot instead.
                _LOGGER.warning(
                    "Could not render the snapshot of terminal=%s", self.terminal_id, exc_info=True
                )
        if update.alternate_exited or paste_disabled:
            self._snapshot_on_settle = True
        return facts_changed

    def _restart_quiet(self, restart: QuietRestart) -> None:
        if restart.supersede_delivery:
            self._notices.cancel()
        self._cancel_task(self._settle_task)
        generation = restart.generation
        self._settle_task = self._background(
            self._settle_after_quiet(generation), f"settle:{generation}"
        )
        sampler = self._sample_task
        if (
            self._observer is not None
            and self._activity.cycle_open
            and (sampler is None or sampler.done())
        ):
            self._sample_task = self._background(self._sample_cycle(), "sample")

    async def _settle_after_quiet(self, generation: int) -> None:
        """Run the quiet boundary of one activity generation, then its idle boundary
        when the program declared or showed the end of its work, or else at the
        latest at the fallback (``TerminalActivity``)."""
        services = self._services
        try:
            await services.sleep(services.activity_quiet_seconds)
            async with self._lock:
                observation = await self._observe_unchanged(generation)
                if observation is None:
                    return
                settled = self._activity.settle(generation, observation.signature)
                if settled is None:
                    return
                self._settles += 1
                self._waits.signal_change()
                if observation.screen and not self._waits.screen_shown:
                    # The first screen also shows when the program's first
                    # output came right after a resize and counts as a redraw.
                    self._waits.screen_shown = True
                    self._waits.signal_change()
                if settled == "activity":
                    self._waits.quiet_boundaries += 1
                    self._waits.signal_change()
                if settled == "repaint" or self._settle_idle(generation, observation):
                    return
            # Process samples can end the cycle earlier; without them, nothing
            # but the fallback can.
            fallback_at = self._activity.fallback_at(generation)
            if fallback_at is None:
                return
            await services.sleep(max(0.0, fallback_at - services.monotonic()))
            async with self._lock:
                observation = await self._observe_unchanged(generation)
                if observation is not None:
                    self._settle_idle(generation, observation)
        except asyncio.CancelledError:
            return

    async def _sample_cycle(self) -> None:
        """Sample the program's processes while a cycle of work runs; once its output
        is quiet, each sample can end the cycle."""
        services = self._services
        try:
            while True:
                await services.sleep(PROCESS_SAMPLE_SECONDS)
                sample = await self._process_sample()
                async with self._lock:
                    activity = self._activity
                    if self.finished or not activity.cycle_open:
                        return
                    now = services.monotonic()
                    if sample is not None:
                        activity.sample(now, sample)
                    if not activity.quiet(now):
                        continue
                    generation = activity.generation
                    observation = await self._observe_unchanged(generation)
                    if observation is not None and self._settle_idle(generation, observation):
                        return
        except asyncio.CancelledError:
            return

    def _settle_idle(self, generation: int, observation: ScreenObservation) -> bool:
        """End the cycle of *generation* if its program is idle; the lock is held.

        Records and delivers the idle screen when the Agent has not seen it.
        """
        observed = self._output.observed
        effect = self._activity.idle(
            generation,
            self._services.monotonic(),
            observation.signature,
            seen=observed is not None and observed.screen_revision == self._output.revision,
            blank=not observation.screen,
        )
        if effect is None:
            return False
        if effect.record:
            self._notices.set(
                kind="idle",
                summary="The program is idle.",
                details={"screen_revision": self._output.revision, "reason": effect.reason},
                deliver=effect.deliver,
                observation=observation,
            )
        else:
            self._publish_state()
            self._waits.signal_change()
        return True

    async def _process_sample(self) -> ProcessSample | None:
        """The program's processes now; None without a process tree or when it failed."""
        tree = self._observer
        if tree is None:
            return None
        key = (self.terminal_id, "process_sample")
        try:
            facts = await asyncio.to_thread(tree.facts)
        except Exception:
            if _CONDITIONS.started(key):
                _LOGGER.warning(
                    "Terminal process sampling failed; its activity relies on output and "
                    "declared signals (terminal=%s)",
                    self.terminal_id,
                    exc_info=True,
                )
            return None
        if _CONDITIONS.ended(key):
            _LOGGER.info("Terminal process sampling recovered (terminal=%s)", self.terminal_id)
        return ProcessSample(
            frozenset(process.pid for process in facts.running),
            facts.cpu_seconds,
            facts.started,
        )

    async def _running_before_input(self) -> frozenset[int] | None:
        """The processes that run before input starts a cycle: a process started
        later is the cycle's work. The sample also starts the CPU measurement,
        so CPU work shows from the cycle's first periodic sample on."""
        sample = await self._process_sample()
        if sample is None:
            return None
        self._activity.sample(self._services.monotonic(), sample)
        return sample.running

    async def _observe_unchanged(self, generation: int) -> ScreenObservation | None:
        """The screen while *generation* is still the latest activity; the lock is held.

        None once newer activity, the program's end or a rendering failure
        (a screen behind its output cannot show that the output paused)
        made the boundary moot.
        """
        if generation != self._activity.generation or self.finished:
            return None
        if not self._output.current or self._writing.writes:
            return None
        try:
            if self._snapshot_on_settle:
                self._snapshot_on_settle = False
                await self._publish_snapshot()
            return await self._output.screen.observe()
        except Exception as error:
            self._output.render_failed(error, self._output.log.size)
            return None

    async def _mark_finished(self) -> None:
        """Record the end once no process of the Session runs any longer.

        A failure to read its output, if any, becomes its outcome. A screen
        that missed output is drawn anew from the log first; one that still
        misses output says so.
        """
        # A root process EOF cannot confirm that captured descendants exited.
        if self._termination_pending:
            return
        self._cancel_task(self._settle_task)
        self._cancel_task(self._sample_task)
        if self._output.failure is not None and self._output.redrawable() and not self.finished:
            await self._output.final_redraw()
        async with self._lock:
            if self.finished:
                return
            self.finished_at = self.finished_at or _utc_now()
            self.exit_code = await asyncio.to_thread(self._adapter.exit_code)
            # Release the terminal (on Windows also its console host and reader thread).
            with contextlib.suppress(OSError):
                await asyncio.to_thread(self._adapter.close)
            error = self._output.read_failure
            self._activity.finish(error=error is not None)
            try:
                await self._publish_snapshot()
            except Exception:
                # A failed renderer must not keep the Session from finishing.
                _LOGGER.warning(
                    "Could not render the final screen of terminal=%s",
                    self.terminal_id,
                    exc_info=True,
                )
            if self._suppress_exit_attention:
                self._publish_state()
            else:
                observation: ScreenObservation | None = None
                with contextlib.suppress(Exception):
                    observation = await self._output.screen.observe()
                if error is None:
                    self._notices.set(
                        kind="exited",
                        summary=f"The program exited with code {self.exit_code}.",
                        details={"exit_code": self.exit_code},
                        observation=observation,
                    )
                else:
                    self._notices.set(
                        kind="error",
                        summary="The terminal failed.",
                        details={"error": str(error)},
                        observation=observation,
                    )
            self._output_event.set()
            self._waits.signal_change()
            self._finish_files()
            await self._output.log.wait_closed()

    # Attention and delivery

    # Stream

    def _publish_output(self, text: str) -> None:
        self._stream_sequence += 1
        self._stream.publish(
            {"type": "terminal_output", "sequence": self._stream_sequence, "data": text}
        )

    async def _publish_snapshot(self) -> None:
        ansi = await self._output.screen.ansi_snapshot()
        self._stream_sequence += 1
        self._stream.publish(
            {
                "type": "terminal_snapshot",
                "sequence": self._stream_sequence,
                "terminal": self._services.operator_summary(self.info()),
                "ansi": ansi,
            }
        )

    def _publish_state(self) -> None:
        self._stream_sequence += 1
        self._stream.publish(
            {
                "type": "terminal_state",
                "sequence": self._stream_sequence,
                "terminal": self._services.operator_summary(self.info()),
            }
        )
        self._services.changed(self.terminal_id)

    # Helpers

    def _finish_files(self) -> None:
        observer, self._observer = self._observer, None
        if observer is not None:
            with contextlib.suppress(Exception):
                observer.close()
        self._output.log.close()
        cleanup, self._cleanup = self._cleanup, None
        if cleanup is not None:
            try:
                cleanup()
            except OSError:
                _LOGGER.warning(
                    "Could not remove the launch files of terminal=%s", self.terminal_id
                )

    def _background(self, coroutine: Coroutine[Any, Any, None], label: str) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine, name=f"terminal:{self.terminal_id}:{label}")
        task.add_done_callback(
            lambda done: _log_task_failure(
                done, f"Terminal {label} failed for terminal={self.terminal_id}"
            )
        )
        return task

    @staticmethod
    def _cancel_task(task: asyncio.Task[Any] | None) -> None:
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()


# A UTF-16 surrogate: a byte that was no UTF-8 (POSIX decodes such bytes as
# lone surrogates, surrogateescape), or half of a character that never came.
