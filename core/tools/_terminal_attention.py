"""Terminal attention and completion delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import replace
from typing import Any

from core.runs import RunAdmissionBlockedError, RunExecutionOwner
from core.tools.terminal_emulator import (
    ScreenObservation,
)
from core.utils.logging import get_logger

from ._terminal_notices import (
    _attention_body,
    _seen_through,
    screen_behind_note,
    unseen_lines_note,
)
from ._terminal_output import TerminalOutput
from ._terminal_state import (
    AttentionKind,
    TerminalAttention,
    TerminalInfo,
    TerminalSessionServices,
    _utc_now,
)

_NOTICE_LABEL_CHARS = 60
_LOGGER = get_logger("tools.terminal_manager")


class TerminalNotices:
    """Own attention identity, acknowledgement and cancellable completion delivery."""

    def __init__(
        self,
        terminal_id: str,
        *,
        output: TerminalOutput,
        services: TerminalSessionServices,
        info: Callable[[], TerminalInfo],
        origin: Callable[[], tuple[str | None, RunExecutionOwner | None]],
        background: Callable[[Coroutine[Any, Any, None], str], asyncio.Task[None]],
        publish: Callable[[], None],
        signal: Callable[[], None],
    ) -> None:
        self.terminal_id = terminal_id
        self._output = output
        self._services = services
        self._info = info
        self._origin = origin
        self._background = background
        self._publish_state = publish
        self._signal_change = signal
        self.current: TerminalAttention | None = None
        self.body = ""
        self.seen_line = 0
        self.clears = 0
        self.revision = 0
        self.acknowledged_revision = 0
        self.task: asyncio.Task[None] | None = None
        self.exit_acknowledged = False

    def set(
        self,
        *,
        kind: AttentionKind,
        summary: str,
        details: dict[str, Any],
        deliver: bool = True,
        observation: ScreenObservation | None,
    ) -> None:
        """Record attention, with the message that brings it and the screen it shows."""
        self.cancel()
        self.revision += 1
        revision = self.revision
        attention = TerminalAttention(
            revision=revision,
            kind=kind,
            notice_id=f"terminal:{self.terminal_id}:attention:{revision}",
            summary=summary,
            details=details,
            created_at=_utc_now(),
        )
        self.current = attention
        behind = None
        if self._output.failure is not None:
            behind = screen_behind_note(
                str(self._output.failure),
                self._output.log_file(),
                complete=not self._output.log.failed,
            )
        unseen = None
        if observation is not None:
            unseen = unseen_lines_note(
                self.terminal_id,
                unseen_from=self._output.seen_line or 0,
                shown_from=observation.screen_start_line,
                first_line=observation.first_line,
                log=self._output.log_file(),
                log_complete=not self._output.log.failed,
                log_end_line=self._output.log.end_line,
                seen=True,
            )
        self.body = _attention_body(
            self.terminal_id,
            self._notice_label(),
            attention,
            observation=observation,
            unseen=unseen,
            behind=behind,
            idle_seconds=self._services.idle_seconds,
        )
        self.seen_line = (
            _seen_through(observation, alternate=self._info().alternate_screen)
            if observation is not None
            else 0
        )
        self.clears = self._output.clear_count
        if kind in {"exited", "error"} and self.exit_acknowledged:
            # A result already showed the end.
            deliver = False
        if deliver:
            self.schedule(attention, self.body, self.seen_line, self.clears)
        self._publish_state()
        self._signal_change()

    def _notice_label(self) -> str:
        info = self._info()
        label = " ".join(info.program.split())
        if len(label) > _NOTICE_LABEL_CHARS:
            label = label[: _NOTICE_LABEL_CHARS - 3] + "..."
        return f"{info.call_name}, {label}" if info.call_name else label

    def schedule(
        self, attention: TerminalAttention, body: str, seen_line: int, clears: int
    ) -> None:
        """Deliver *body*, which shows the screen down to history line *seen_line*
        as it was after *clears* clears."""
        if self._services.trigger_service is None or self._info().attachment is None:
            return
        self.task = self._background(
            self._deliver(attention, body, seen_line, clears), f"attention:{attention.revision}"
        )

    async def _deliver(
        self, attention: TerminalAttention, body: str, seen_line: int, clears: int
    ) -> None:
        trigger_service = self._services.trigger_service
        attachment = self._info().attachment
        origin_run_id, execution_owner = self._origin()
        if trigger_service is None or attachment is None or origin_run_id is None:
            return
        delivery = trigger_service.submit_completion(
            attachment.agent_id,
            attachment.session_id,
            notice_id=attention.notice_id,
            origin_run_id=origin_run_id,
            body=body,
            project_id=attachment.project_id,
            execution_owner=execution_owner,
        )
        try:
            await delivery
        except RunAdmissionBlockedError:
            # The activity's execution owner closed (for example its temporary
            # group): an expected end of its lifecycle, not a delivery fault.
            _LOGGER.debug(
                "Terminal attention dropped for a closed execution owner (terminal=%s revision=%s)",
                self.terminal_id,
                attention.revision,
            )
            return
        # The Agent received it: a wait ends only at a later attention.
        self.acknowledged_revision = max(self.acknowledged_revision, attention.revision)
        if self._info().attachment == attachment:
            self._output.saw_lines(seen_line, clears)
        if self.current is attention:
            self.current = replace(attention, delivered=True)

    def running(self) -> bool:
        task = self.task
        return task is not None and not task.done()

    def pending(self) -> bool:
        """An ``idle`` delivery is on its way and not yet received."""
        attention = self.current
        return (
            attention is not None
            and attention.kind == "idle"
            and not attention.delivered
            and self.running()
        )

    def cancel(self) -> None:
        task = self.task
        attention = self.current
        attachment = self._info().attachment
        if task is None or task.done() or attention is None or attachment is None:
            return
        if self._services.trigger_service is not None:
            self._services.trigger_service.cancel_completion(
                attachment.agent_id,
                attachment.session_id,
                notice_id=attention.notice_id,
                project_id=attachment.project_id,
            )
        task.cancel()

    def acknowledge(self, revision: int) -> None:
        """Cancel the equivalent automatic delivery after a manual result is durable."""
        attention = self.current
        if (
            attention is None
            or attention.revision != revision
            or revision <= self.acknowledged_revision
        ):
            return
        self.acknowledged_revision = max(self.acknowledged_revision, revision)
        self.cancel()

    def acknowledge_exit(self) -> None:
        """A result durably showed the program's end, so its exit is not delivered."""
        self.exit_acknowledged = True
        attention = self.current
        if attention is not None and attention.kind in {"exited", "error"}:
            self.acknowledged_revision = max(self.acknowledged_revision, attention.revision)
            self.cancel()
