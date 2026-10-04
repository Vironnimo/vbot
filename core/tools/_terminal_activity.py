"""When Terminal I/O counts as work, and when its quiet boundary wakes the Agent.

``TerminalActivity`` is the pure state machine behind a Terminal Session's
``state`` and its ``output_settled`` attention. It owns no tasks and no
clock: the Session feeds it events with the current monotonic time and
carries out what it answers (restart the quiet timer, cancel a pending
delivery, record or deliver attention).

Phases (``state`` shows ``repainting`` as ``ready``)::

    starting --input--> working --quiet--> ready --output/input--> working
                                             |
                                          resize, then output inside the
                                          repaint window
                                             v
                                         repainting --quiet--> ready (baseline)

``exited`` and ``error`` are final.
"""

from __future__ import annotations

from typing import Literal, NamedTuple

from ._terminal_state import TerminalState

_Phase = Literal["starting", "ready", "repainting", "working", "exited", "error"]


class QuietRestart(NamedTuple):
    """The quiet timer must restart for ``generation``."""

    generation: int
    # A pending ``output_settled`` delivery describes a screen that is
    # changing again: cancel it; the next quiet boundary delivers instead.
    supersede_delivery: bool


class OutputEffect(NamedTuple):
    # The output may show new content: it advances the screen revision.
    changes_screen: bool
    restart: QuietRestart | None
    state_changed: bool


class SettleEffect(NamedTuple):
    # Record a new ``output_settled`` attention revision.
    record: bool
    # Deliver it to the attached Agent.
    deliver: bool
    state_changed: bool


class TerminalActivity:
    """Activity phase, quiet-boundary wakeups and the delivered-screen baseline."""

    def __init__(
        self,
        *,
        awaiting_initial_input: bool,
        startup_silence: bool,
        repaint_window: float,
        output_wakes: bool = True,
    ) -> None:
        self._phase: _Phase = "starting" if awaiting_initial_input else "ready"
        # False: only input wakes the attached Agent, never spontaneous output
        # (a command's output is its result, delivered when it ends).
        self._spontaneous_wakes = output_wakes
        self._repaint_window = repaint_window
        # Set when a quiet terminal is resized: output before this monotonic
        # time redraws known content for the new size and is not activity.
        self._repaint_deadline: float | None = None
        # The current cycle wakes the attached Agent at its quiet boundary.
        self._wake_at_quiet = False
        # Spontaneous output (not caused by input) wakes the attached Agent.
        # Armed by attach and by the first quiet boundary while attached, so
        # the start-up output of an Agent-started terminal wakes nobody.
        self._output_wakes = False
        # An Agent start without initial text wakes nobody until the first
        # input or attach: the starting Agent sees the start-up screen in the
        # start result and the operator sees it live.
        self._startup_silence = startup_silence
        # Visible-cell signature of the screen the attached Agent last
        # received or acknowledged. A quiet boundary showing the same screen
        # (status refreshes, cursor frames, repaint echoes) wakes nobody.
        self._baseline: str | None = None
        self.generation = 0

    @property
    def state(self) -> TerminalState:
        return "ready" if self._phase == "repainting" else self._phase

    @property
    def finished(self) -> bool:
        return self._phase in {"exited", "error"}

    def output(self, now: float, *, attached: bool, delivery_pending: bool) -> OutputEffect:
        """Program output arrived."""
        if self.finished:
            return OutputEffect(False, None, False)
        if self._phase == "starting":
            # The initial input is still waiting for a stable screen.
            return OutputEffect(True, None, False)
        if self._repaint_deadline is not None and now < self._repaint_deadline:
            self._phase = "repainting"
            return OutputEffect(False, self._restart(notify=False, supersede=False), False)
        self._repaint_deadline = None
        changed = self._phase != "working"
        self._phase = "working"
        restart = self._restart(
            notify=attached and self._output_wakes and self._spontaneous_wakes,
            supersede=delivery_pending,
        )
        return OutputEffect(True, restart, changed)

    def input(self, *, notify: bool, delivery_pending: bool) -> tuple[QuietRestart, bool]:
        """Agent or operator input was written; returns the restart and a state change."""
        self._repaint_deadline = None
        self._startup_silence = False
        changed = self._phase != "working"
        self._phase = "working"
        return self._restart(notify=notify, supersede=delivery_pending), changed

    def resize(self, now: float) -> None:
        """The PTY changed size. A quiet program answers by redrawing what it showed."""
        if self._phase in {"ready", "repainting"}:
            self._repaint_deadline = now + self._repaint_window

    def attach(self, *, delivery_pending: bool) -> QuietRestart | None:
        """An Agent attached: its activity, including a cycle in progress, wakes it."""
        self._output_wakes = True
        self._startup_silence = False
        if self._phase != "working":
            return None
        return self._restart(notify=True, supersede=delivery_pending)

    def detach(self) -> None:
        self._wake_at_quiet = False
        self._output_wakes = False

    def settle(self, generation: int, signature: str, *, attached: bool) -> SettleEffect | None:
        """The quiet timer of ``generation`` expired showing ``signature``.

        Returns None for a stale timer or a phase without a quiet boundary.
        """
        if generation != self.generation or self._phase not in {"working", "repainting"}:
            return None
        if self._phase == "repainting":
            # Only a redraw for a new size happened. Its screen shows the
            # known content and becomes the baseline without waking anyone or
            # recording attention.
            self._phase = "ready"
            self._repaint_deadline = None
            self._baseline = signature
            return SettleEffect(False, False, False)
        deliver = self._wake_at_quiet
        self._wake_at_quiet = False
        self._output_wakes = attached
        self._phase = "ready"
        if deliver and self._startup_silence:
            # Remember the start-up screen as seen, so an identical later
            # refresh stays silent too.
            self._baseline = signature
            deliver = False
        if deliver and signature == self._baseline:
            deliver = False
        if deliver:
            self._baseline = signature
        return SettleEffect(True, deliver, True)

    def acknowledge(self, signature: str) -> None:
        """The attached Agent durably received this screen."""
        self._baseline = signature

    def finish(self, *, error: bool) -> None:
        self._phase = "error" if error else "exited"
        self._wake_at_quiet = False
        self._repaint_deadline = None

    def _restart(self, *, notify: bool, supersede: bool) -> QuietRestart:
        if supersede:
            # A cancelled delivery has not established an observed baseline;
            # an identical screen must still carry the outstanding update.
            self._baseline = None
            notify = True
        self._wake_at_quiet = self._wake_at_quiet or notify
        self.generation += 1
        return QuietRestart(self.generation, supersede)
