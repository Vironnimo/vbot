"""Terminal PTY input/output, rendered activity and completion delivery."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from core.runs import RunExecutionOwner
from core.tools import terminal_backend
from core.tools.process_manager import log_background_task_result

from ._terminal_events import TerminalEvents, _attention_body
from ._terminal_input import (
    _await_shell_ready,
    _input_chunks,
    _shell_command,
)
from ._terminal_state import (
    TERMINAL_INITIAL_INPUT_QUIET_SECONDS,
    TERMINAL_INITIAL_INPUT_TIMEOUT_SECONDS,
    TERMINAL_INPUT_KEY_DELAY_SECONDS,
    TERMINAL_OPERATOR_READY_TIMEOUT_SECONDS,
    TERMINAL_RESIZE_GRACE_MAX_SECONDS,
    TERMINAL_RESIZE_GRACE_SECONDS,
    AttentionKind,
    TerminalAttention,
    TerminalClosedError,
    TerminalManagerError,
    TerminalSession,
    TerminalStaleScreenError,
    _finish_files,
    _require_live,
    _utc_now,
    _validate_dimensions,
)


class TerminalSessionIO:
    """Drive PTY reads, initial input, rendered activity and Agent notifications."""

    def __init__(
        self,
        events: TerminalEvents,
        reader_executor: ThreadPoolExecutor,
        trigger_service: Any | None,
        *,
        activity_quiet_seconds: float,
        monotonic: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]],
    ) -> None:
        self._events = events
        self._reader_executor = reader_executor
        self._trigger_service = trigger_service
        self._activity_quiet_seconds = activity_quiet_seconds
        self._monotonic = monotonic
        self._sleep = sleep

    async def _send_initial_input_when_ready(
        self, session: TerminalSession, text: str, *, origin_run_id: str
    ) -> None:
        """Wait until the TUI has initialized before sending its first task."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TERMINAL_INITIAL_INPUT_TIMEOUT_SECONDS
        last_revision = -1
        quiet_since: float | None = None
        try:
            while session.state not in {"exited", "error"}:
                now = loop.time()
                if now >= deadline:
                    break
                async with session.lock:
                    revision = session.renderer.revision
                    has_screen = bool(session.renderer.screen_text())
                if has_screen and revision != last_revision:
                    last_revision = revision
                    quiet_since = now
                elif (
                    has_screen
                    and quiet_since is not None
                    and now - quiet_since >= TERMINAL_INITIAL_INPUT_QUIET_SECONDS
                ):
                    break
                session.output_event.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(session.output_event.wait(), timeout=0.1)
            if session.state in {"exited", "error"}:
                return
            attachment = session.attachment
            if attachment is None:
                return
            await self.send_input(
                session,
                data=None,
                text=text,
                key="enter",
                expected_screen_revision=None,
                origin_run_id=origin_run_id,
                execution_owner=session.execution_owner,
            )
        except asyncio.CancelledError:
            return

    async def _send_operator_command(self, session: TerminalSession) -> None:
        """Enter one operator-requested command into an interactive shell.

        The shell owns the Session and stays alive after the command ends, so
        the command is written through the exact `data` channel exactly once,
        without bracketed-paste or Enter state.
        """
        command = _shell_command(
            session.launch_command, session.launch_arguments, shell=session.command
        )
        if command is None:
            return
        if not await _await_shell_ready(session):
            return
        try:
            await asyncio.to_thread(session.adapter.write, command)
            await asyncio.to_thread(session.adapter.write, "\r")
        except (EOFError, OSError):
            return
        except asyncio.CancelledError:
            return
        async with session.lock:
            if session.state not in {"exited", "error"} and session.state != "working":
                session.state = "working"
                self._events._publish_state(session)
            session.output_event.set()

    async def _read_terminal(self, session: TerminalSession) -> None:
        error: BaseException | None = None
        try:
            while True:
                try:
                    text = await asyncio.get_running_loop().run_in_executor(
                        self._reader_executor, session.adapter.read, 4096
                    )
                except TimeoutError:
                    if not await asyncio.to_thread(session.adapter.is_alive):
                        break
                    continue
                if not text:
                    continue
                async with session.lock:
                    if session.log_handle is not None:
                        session.log_handle.write(text)
                        session.log_handle.flush()
                    previous_title = session.renderer.title
                    bracketed_paste_was_enabled = session.renderer.bracketed_paste_enabled
                    alternate_screen_exited = session.renderer.feed(text)
                    protocol_response = session.renderer.take_responses()
                    if protocol_response:
                        # These are terminal protocol replies, not human/Agent
                        # input: do not cancel queued input or start activity.
                        await asyncio.to_thread(session.adapter.write, protocol_response)
                    bracketed_paste_disabled = (
                        bracketed_paste_was_enabled and not session.renderer.bracketed_paste_enabled
                    )
                    title_changed = session.renderer.title != previous_title
                    self._events._publish_output(session, text)
                    if alternate_screen_exited:
                        self._events._publish_snapshot(session)
                    if alternate_screen_exited or bracketed_paste_disabled:
                        session.snapshot_on_settle = True
                    state_changed = False
                    if session.state != "starting":
                        state_changed = session.state != "working"
                        session.state = "working"
                        notify = session.attachment is not None and session.settled_delivery_enabled
                        now = self._monotonic()
                        if now < session.resize_grace_until:
                            session.resize_grace_until = min(
                                now + TERMINAL_RESIZE_GRACE_SECONDS,
                                session.resize_grace_deadline,
                            )
                        self._schedule_settle(session, notify=notify)
                    if state_changed or title_changed:
                        self._events._publish_state(session)
                    session.output_event.set()
        except asyncio.CancelledError:
            raise
        except (EOFError, OSError):
            pass
        except BaseException as caught:
            error = caught
        finally:
            await self._mark_finished(session, error)

    def _schedule_settle(self, session: TerminalSession, *, notify: bool) -> None:
        """Restart the generic quiet timer after PTY input or output activity."""
        attention = session.attention
        pending_agent_delivery = (
            attention is not None
            and attention.kind == "output_settled"
            and not attention.delivered
            and session.notification_task is not None
            and not session.notification_task.done()
        )
        if pending_agent_delivery:
            self._cancel_delivery(session)
            # A cancelled delivery has not established an observed baseline.
            # An identical repaint must still carry the outstanding update.
            session.settled_screen_signature = None
            notify = True
        session.activity_generation += 1
        generation = session.activity_generation
        session.notify_on_settle = session.notify_on_settle or notify
        previous = session.settle_task
        if previous is not None and not previous.done():
            previous.cancel()
        session.settle_task = asyncio.create_task(
            self._settle_after_quiet(session, generation),
            name=f"terminal:{session.terminal_id}:settle:{generation}",
        )
        session.settle_task.add_done_callback(
            lambda task: log_background_task_result(
                task,
                f"Terminal quiet detection failed for terminal={session.terminal_id} "
                f"generation={generation}",
            )
        )

    async def _settle_after_quiet(self, session: TerminalSession, generation: int) -> None:
        try:
            await self._sleep(self._activity_quiet_seconds)
            async with session.lock:
                if (
                    generation != session.activity_generation
                    or session.state in {"starting", "exited", "error"}
                    or session.finished_at is not None
                ):
                    return
                deliver = session.notify_on_settle
                session.notify_on_settle = False
                session.settled_delivery_enabled = session.attachment is not None
                session.state = "ready"
                if session.snapshot_on_settle:
                    session.snapshot_on_settle = False
                    self._events._publish_snapshot(session)
                signature = session.renderer.screen_signature()
                if deliver and session.suppress_until_activity:
                    # A text-less Agent start is silent until the first
                    # explicit input or attach: the startup screen (banner,
                    # prompt, TUI boot) is observed by the starting Agent and
                    # visible to the operator, so its settle waves must not
                    # wake the session. Mark the screen as already seen so a
                    # later identical status refresh stays silent too.
                    # Input/attach clear the flag in
                    # send_input/send_operator_input/attach.
                    session.settled_screen_signature = signature
                    deliver = False
                if deliver and signature == session.settled_screen_signature:
                    # The quiet boundary came from bytes that did not change
                    # the rendered screen (status refreshes, cursor frames,
                    # repaint echoes) or repeated the already-delivered
                    # screen. That is not work, so do not wake the agent
                    # again; the screen is already known to it.
                    deliver = False
                defer = deliver and self._monotonic() < session.resize_grace_until
                if deliver and not defer:
                    session.settled_screen_signature = signature
                self._set_attention(
                    session,
                    kind="output_settled",
                    summary=(
                        "Terminal output has been quiet after recent activity. Inspect the "
                        "current screen; this does not imply that the program finished or "
                        "requires input."
                    ),
                    details={"screen_revision": session.renderer.revision},
                    deliver=deliver and not defer,
                )
                attention = session.attention
            # Keep this task alive even when no further output arrives. New
            # activity cancels it and starts a fresh quiet boundary; reads can
            # acknowledge this exact boundary before its deferred delivery.
            while defer:
                remaining = session.resize_grace_until - self._monotonic()
                if remaining > 0:
                    await self._sleep(remaining)
                async with session.lock:
                    if (
                        generation != session.activity_generation
                        or attention is None
                        or session.attention is not attention
                        or attention.revision <= session.acknowledged_attention_revision
                        or session.attachment is None
                        or session.state in {"exited", "error"}
                    ):
                        return
                    if self._monotonic() < session.resize_grace_until:
                        continue
                    session.settled_screen_signature = signature
                    self._schedule_attention_delivery(session, attention)
                    return
        except asyncio.CancelledError:
            return

    async def _mark_finished(self, session: TerminalSession, error: BaseException | None) -> None:
        # A root process EOF cannot confirm that captured descendants exited.
        if session.termination_pending:
            return
        initial_task = session.initial_input_task
        if (
            initial_task is not None
            and initial_task is not asyncio.current_task()
            and not initial_task.done()
        ):
            initial_task.cancel()
        command_task = session.operator_command_task
        if (
            command_task is not None
            and command_task is not asyncio.current_task()
            and not command_task.done()
        ):
            command_task.cancel()
        settle_task = session.settle_task
        if (
            settle_task is not None
            and settle_task is not asyncio.current_task()
            and not settle_task.done()
        ):
            settle_task.cancel()
        async with session.lock:
            if session.state in {"exited", "error"}:
                return
            session.finished_at = session.finished_at or _utc_now()
            session.exit_code = await asyncio.to_thread(session.adapter.exit_code)
            if error is None:
                session.state = "exited"
                self._events._publish_snapshot(session)
                if not session.suppress_exit_attention:
                    self._set_attention(
                        session,
                        kind="exited",
                        summary=f"Terminal process exited with code {session.exit_code}.",
                        details={"exit_code": session.exit_code},
                    )
                else:
                    self._events._publish_state(session)
            else:
                session.state = "error"
                self._events._publish_snapshot(session)
                if not session.suppress_exit_attention:
                    self._set_attention(
                        session,
                        kind="error",
                        summary="Terminal transport or rendering failed.",
                        details={"error": str(error)},
                    )
                else:
                    self._events._publish_state(session)
            session.attention_event.set()
            session.output_event.set()
            _finish_files(session)

    def _set_attention(
        self,
        session: TerminalSession,
        *,
        kind: AttentionKind,
        summary: str,
        details: dict[str, Any],
        deliver: bool = True,
    ) -> None:
        self._cancel_delivery(session)
        session.attention_revision += 1
        revision = session.attention_revision
        attention = TerminalAttention(
            revision=revision,
            kind=kind,
            notice_id=f"terminal:{session.terminal_id}:attention:{revision}",
            summary=summary,
            details=details,
            created_at=_utc_now(),
        )
        session.attention = attention
        session.attention_event.set()
        if deliver:
            self._schedule_attention_delivery(session, attention)
        self._events._publish_state(session)

    def _schedule_attention_delivery(
        self, session: TerminalSession, attention: TerminalAttention
    ) -> None:
        if self._trigger_service is None or session.attachment is None:
            return
        revision = attention.revision
        session.notification_task = asyncio.create_task(
            self._deliver_attention(session, attention),
            name=f"terminal:{session.terminal_id}:attention:{revision}",
        )
        session.notification_task.add_done_callback(
            lambda task: log_background_task_result(
                task,
                f"Terminal attention delivery failed for terminal={session.terminal_id} "
                f"revision={revision}",
            )
        )

    async def _deliver_attention(
        self, session: TerminalSession, attention: TerminalAttention
    ) -> None:
        trigger_service = self._trigger_service
        attachment = session.attachment
        if trigger_service is None or attachment is None:
            return
        origin_run_id = session.activity_origin_run_id or session.origin_run_id
        if origin_run_id is None:
            return
        delivery = trigger_service.submit_completion(
            attachment.agent_id,
            attachment.session_id,
            notice_id=attention.notice_id,
            origin_run_id=origin_run_id,
            body=_attention_body(session, attention),
            project_id=attachment.project_id,
            execution_owner=session.activity_execution_owner,
        )
        await delivery
        attention.delivered = True

    def _cancel_delivery(self, session: TerminalSession) -> None:
        task = session.notification_task
        attention = session.attention
        attachment = session.attachment
        if task is None or task.done() or attention is None or attachment is None:
            return
        if self._trigger_service is not None:
            self._trigger_service.cancel_completion(
                attachment.agent_id,
                attachment.session_id,
                notice_id=attention.notice_id,
                project_id=attachment.project_id,
            )
        task.cancel()

    def _detach_session(self, session: TerminalSession) -> None:
        """Clear one attachment and any delivery state without touching the child."""
        self._cancel_delivery(session)
        session.attachment = None
        session.activity_origin_run_id = None
        session.activity_execution_owner = None
        session.notify_on_settle = False
        session.settled_delivery_enabled = False
        session.observed_screen = None

    async def _terminate_session(
        self, session: TerminalSession, *, suppress_attention: bool
    ) -> None:
        initial_task = session.initial_input_task
        if (
            initial_task is not None
            and initial_task is not asyncio.current_task()
            and not initial_task.done()
        ):
            initial_task.cancel()
        command_task = session.operator_command_task
        if (
            command_task is not None
            and command_task is not asyncio.current_task()
            and not command_task.done()
        ):
            command_task.cancel()
        if suppress_attention:
            session.suppress_exit_attention = True
            self._cancel_delivery(session)
        settle_task = session.settle_task
        if settle_task is not None and not settle_task.done():
            settle_task.cancel()
        if session.termination_pending or session.state not in {"exited", "error"}:
            session.termination_pending = True
            try:
                await asyncio.to_thread(
                    terminal_backend.terminate_process_tree,
                    session.adapter,
                    targets=session.termination_targets,
                )
            except OSError as error:
                raise TerminalManagerError(
                    f"Could not terminate terminal {session.terminal_id}; "
                    "its process tree may still be running. Retry the kill operation."
                ) from error
            session.termination_pending = False
        await asyncio.to_thread(session.adapter.close)
        reader = session.reader_task
        if reader is not None and reader is not asyncio.current_task() and not reader.done():
            try:
                await asyncio.wait_for(asyncio.shield(reader), timeout=5)
            except TimeoutError:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
        if session.state not in {"exited", "error"}:
            await self._mark_finished(session, None)

    async def send_operator_input(
        self, session: TerminalSession, data: str, *, expected_screen_revision: int | None = None
    ) -> None:
        """Write exact user-controlled terminal bytes through the existing PTY."""
        command_task = session.operator_command_task
        if command_task is not None and not command_task.done():
            # The launch command is typed into the shell while it is still
            # booting; user input must queue behind it instead of racing it
            # into the same line (which would corrupt the command). The task
            # always terminates: its shell-readiness wait is bounded.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(command_task),
                    timeout=TERMINAL_OPERATOR_READY_TIMEOUT_SECONDS + 1.0,
                )
        write_error: BaseException | None = None
        async with session.lock:
            _require_live(session)
            if (
                expected_screen_revision is not None
                and expected_screen_revision != session.renderer.revision
            ):
                raise TerminalStaleScreenError(
                    "Terminal screen changed; inspect status before sending this input"
                )
            initial_task = session.initial_input_task
            if initial_task is not None and not initial_task.done():
                initial_task.cancel()
            # Human input invalidates an Agent's observation even when the
            # application does not echo it (for example, a password prompt).
            session.renderer.revision += 1
            # Operator input ends the post-resize grace and the resize
            # settle gate, and counts as work against the startup
            # suppression, for the same reason as agent input.
            session.resize_grace_until = 0.0
            session.resize_grace_deadline = 0.0
            session.suppress_until_activity = False
            state_changed = session.state != "working"
            session.state = "working"
            try:
                await asyncio.to_thread(session.adapter.write, data)
            except (EOFError, OSError) as error:
                write_error = error
            else:
                self._schedule_settle(session, notify=session.attachment is not None)
                session.output_event.set()
                if state_changed:
                    self._events._publish_state(session)
                return
        await self._mark_finished(session, None)
        raise TerminalClosedError("Terminal Session is no longer running") from write_error

    async def send_input(
        self,
        session: TerminalSession,
        *,
        text: str | None,
        key: str | None,
        expected_screen_revision: int | None,
        origin_run_id: str,
        data: str | None = None,
        execution_owner: RunExecutionOwner | None = None,
    ) -> dict[str, Any]:
        """Write exact data or named terminal input and track generic PTY activity."""
        terminal_id = session.terminal_id
        initial_task = session.initial_input_task
        write_error: BaseException | None = None
        async with session.lock:
            _require_live(session)
            if (
                expected_screen_revision is not None
                and expected_screen_revision != session.renderer.revision
            ):
                raise TerminalStaleScreenError(
                    "Terminal screen changed; inspect status before sending this input"
                )
            bracketed_paste = (
                text is not None
                and ("\n" in text or "\r" in text)
                and session.renderer.bracketed_paste_enabled
            )
            chunks = _input_chunks(
                data=data,
                text=text,
                key=key,
                bracketed_paste=bracketed_paste,
            )
            if not chunks:
                return {
                    "terminal_id": terminal_id,
                    "state": session.state,
                    "characters_sent": 0,
                    "key": key,
                    "bracketed_paste": False,
                    "superseded_attention_revision": None,
                    "screen_revision": session.renderer.revision,
                }
            # Only real input ends suppression. An empty write must not
            # change activity, invalidate observations, or cancel queued input.
            session.resize_grace_until = 0.0
            session.resize_grace_deadline = 0.0
            session.suppress_until_activity = False
            session.renderer.revision += 1
            if (
                initial_task is not None
                and initial_task is not asyncio.current_task()
                and not initial_task.done()
            ):
                initial_task.cancel()
            prior_state = session.state
            prior_attention_revision = session.attention_revision
            session.activity_origin_run_id = origin_run_id
            session.activity_execution_owner = execution_owner
            session.state = "working"
            for index, chunk in enumerate(chunks):
                if index:
                    await asyncio.sleep(TERMINAL_INPUT_KEY_DELAY_SECONDS)
                try:
                    await asyncio.to_thread(session.adapter.write, chunk)
                except (EOFError, OSError) as error:
                    write_error = error
                    break
            if write_error is None:
                self._schedule_settle(session, notify=True)
                session.output_event.set()
                if prior_state != "working":
                    self._events._publish_state(session)
                return {
                    "terminal_id": terminal_id,
                    "state": session.state,
                    "characters_sent": sum(len(chunk) for chunk in chunks),
                    "key": key,
                    "bracketed_paste": bracketed_paste,
                    "superseded_attention_revision": (
                        prior_attention_revision if session.attention is not None else None
                    ),
                    "screen_revision": session.renderer.revision,
                }
        await self._mark_finished(session, None)
        raise TerminalClosedError("Terminal Session is no longer running") from write_error

    async def _resize_session(
        self,
        session: TerminalSession,
        *,
        columns: int,
        rows: int,
    ) -> dict[str, Any]:
        """Resize one already-authorized Terminal Session."""
        _validate_dimensions(columns, rows)
        async with session.lock:
            _require_live(session)
            if columns == session.renderer.columns and rows == session.renderer.rows:
                # A resize to the current dimensions is a no-op: forwarding it
                # would make the foreground program repaint and wake the agent.
                return {
                    "terminal_id": session.terminal_id,
                    "state": session.state,
                    "columns": columns,
                    "rows": rows,
                    "screen_revision": session.renderer.revision,
                }
            await asyncio.to_thread(session.adapter.resize, rows, columns)
            session.renderer.resize(columns, rows)
            session.last_resize_screen_revision = session.renderer.revision
            now = self._monotonic()
            session.resize_grace_until = now + TERMINAL_RESIZE_GRACE_SECONDS
            # A new explicit resize restarts the hard deadline; the rolling
            # extension happens on repaint output inside the base window.
            session.resize_grace_deadline = now + TERMINAL_RESIZE_GRACE_MAX_SECONDS
            self._events._publish_state(session)
            return {
                "terminal_id": session.terminal_id,
                "state": session.state,
                "columns": columns,
                "rows": rows,
                "screen_revision": session.renderer.revision,
            }
