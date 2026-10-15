"""When a program in a Terminal works, and when its work is done.

``TerminalActivity`` is the pure state machine behind a Terminal Session's
``state`` and its ``idle`` attention. It owns no tasks and no clock: the
Session feeds it events with the current monotonic time and carries out what
it answers (restart the quiet timer, sample processes, cancel a pending
delivery, record or deliver attention).

Work comes in cycles. Input from the Agent or the operator starts one, the
program's start-up starts the first, and output after the program went idle
starts a spontaneous one. A start-up that ends on a blank screen (a
terminal's own set-up sequences) shows no first screen: the program's next
output opens the start-up cycle again. A cycle ends at its idle boundary,
which the evidence decides, by the first reason that holds:

- ``declared``: the program declared the end of its work (a bell or
  notification, a cleared progress indicator, a shell prompt) and then
  printed nothing for the quiet period;
- ``quiet``: in a cycle input or start-up began, the program showed work
  without a pause (output, CPU time, a process the cycle started), then
  printed nothing for the quiet period, and now runs no process the cycle
  started, uses no CPU and shows no progress;
- ``fallback``: the program printed nothing for the idle period, with neither
  CPU time nor a new process in it.

A spontaneous cycle ends only as declared or by the fallback: a program that
resumed on its own can pause again without being done. Output after a ``quiet``
end, before that cycle's fallback was due, can resume the input/start-up work.
Each resumption must show fresh work: sustained output, CPU use or a process
the cycle started. Isolated redraw frames (a clock, ``top``) are not enough.
There is no limit on the number of evidenced resumptions. Only an idle
boundary wakes the attached Agent, and only with a screen the Agent has not
seen. Every output or input also starts a new generation of the quiet timer,
whose quiet boundary ends the reply to input or start.

Phases (``state`` shows ``repainting`` as ``ready``)::

    ready --output/input--> working --idle--> ready
      |
    resize, then output inside the repaint window
      v
    repainting --quiet--> ready

``exited`` and ``error`` are final.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, NamedTuple

from ._terminal_state import TerminalState
from .process_tree import IDLE_CPU_SECONDS

_Phase = Literal["ready", "repainting", "working", "exited", "error"]
_Origin = Literal["start", "input", "output"]
# What ended at a quiet boundary: activity, or only a redraw for a new size.
SettleKind = Literal["activity", "repaint"]
IdleReason = Literal["declared", "quiet", "fallback"]

# A running cycle's processes are sampled this often.
PROCESS_SAMPLE_SECONDS = 0.5
# A pause in the evidence of work longer than this share of the quiet period
# shows that the program can pause while it works.
_CONTINUITY_SHARE = 0.75
# Output spanning this share of the quiet period is a burst of work, rather
# than one redraw split into chunks by the transport. Counting chunks alone
# makes identical output behave differently across PTY reads.
_OUTPUT_BURST_SHARE = 0.5
# The tree works while it uses at least this share of one core, measured
# over the samples of the last second.
_BUSY_CPU_SHARE = 0.2
_BUSY_WINDOW_SECONDS = 1.0
# Clock readings that differ by less than this are the same moment.
_EPSILON = 1e-6


class QuietRestart(NamedTuple):
    """The activity timer must restart for ``generation``."""

    generation: int
    # A pending ``idle`` delivery describes a screen that is changing again:
    # cancel it; the next idle boundary delivers instead.
    supersede_delivery: bool


class OutputEffect(NamedTuple):
    # The output may show new content: it advances the screen revision.
    changes_screen: bool
    restart: QuietRestart | None
    state_changed: bool


class IdleEffect(NamedTuple):
    # Record a new ``idle`` attention revision: the screen is new to the Agent.
    record: bool
    # Deliver it to the attached Agent.
    deliver: bool
    reason: IdleReason


class ProcessSample(NamedTuple):
    """The program's process tree at one moment."""

    running: frozenset[int]
    cpu_seconds: float
    # Processes ever started in the tree.
    started: int


@dataclass(slots=True)
class _Cycle:
    origin: _Origin
    opened_at: float
    last_output_at: float
    last_evidence_at: float
    # No pause in the evidence of work longer than the continuity gap so far.
    continuous: bool = True
    declared_end: bool = False
    # A shell runs a command line this cycle started.
    command_running: bool = False
    # Processes running when the cycle started; None until sampled.
    baseline: frozenset[int] | None = None
    child_running: bool = False
    # A resumed quiet cycle needs fresh work, not just one redraw frame.
    resumed: bool = False
    work_observed: bool = False


class _Sample(NamedTuple):
    at: float
    cpu_seconds: float
    started: int


class TerminalActivity:
    """Activity phase, work cycles, idle boundaries and the delivered-screen baseline."""

    def __init__(
        self,
        *,
        repaint_window: float,
        quiet_seconds: float,
        idle_seconds: float,
    ) -> None:
        self._phase: _Phase = "ready"
        self._repaint_window = repaint_window
        self._quiet_seconds = quiet_seconds
        self._idle_seconds = idle_seconds
        self._continuity_gap = quiet_seconds * _CONTINUITY_SHARE
        # Set when a quiet terminal is resized: output before this monotonic
        # time redraws known content for the new size and is not activity.
        self._repaint_deadline: float | None = None
        self._cycle: _Cycle | None = None
        # Origin of the next cycle that output opens: the program's start-up first.
        self._next_origin: _Origin = "start"
        # A quiet cycle can resume before its fallback was due. Keep its
        # process baseline so a child started meanwhile still counts as work.
        self._resumable: _Cycle | None = None
        # A progress indicator (OSC 9;4) shows work; it outlives cycles.
        self._progress = False
        self._samples: deque[_Sample] = deque()
        # The current activity wakes the attached Agent at its idle boundary.
        self._wake_at_idle = False
        # Visible-cell signature of the screen the attached Agent last
        # received or saw in a result, such as the start result's first
        # screen. An idle boundary showing the same screen (status refreshes,
        # cursor frames, repaint echoes) wakes nobody.
        self._baseline: str | None = None
        # Why the last cycle ended; None while one runs and before the first.
        self.idle_reason: IdleReason | None = None
        self.generation = 0

    @property
    def state(self) -> TerminalState:
        return "ready" if self._phase == "repainting" else self._phase

    @property
    def finished(self) -> bool:
        return self._phase in {"exited", "error"}

    @property
    def cycle_open(self) -> bool:
        """A cycle of work runs: its processes are worth sampling."""
        return self._cycle is not None and not self.finished

    def output(
        self,
        now: float,
        *,
        attached: bool,
        delivery_pending: bool,
        signals: Iterable[str] = (),
    ) -> OutputEffect:
        """Program output arrived, with the *signals* it declared."""
        signals = tuple(signals)
        if self.finished:
            return OutputEffect(False, None, False)
        if not signals and self._repaint_deadline is not None and now < self._repaint_deadline:
            self._phase = "repainting"
            return OutputEffect(False, self._restart(notify=False, supersede=False), False)
        self._repaint_deadline = None
        changed = self._phase != "working"
        self._phase = "working"
        cycle = self._cycle
        if cycle is None:
            resumable = self._resumable
            if (
                resumable is not None
                and now < resumable.last_output_at + self._idle_seconds - _EPSILON
            ):
                cycle = self._open(resumable.origin, now)
                cycle.resumed = True
                cycle.baseline = resumable.baseline
            else:
                cycle = self._open(self._next_origin, now)
        else:
            self._evidence(cycle, now)
            if now - cycle.opened_at >= self._quiet_seconds * _OUTPUT_BURST_SHARE - _EPSILON:
                cycle.work_observed = True
        cycle.last_output_at = now
        self._apply_signals(cycle, signals)
        restart = self._restart(notify=attached, supersede=delivery_pending)
        return OutputEffect(True, restart, changed)

    def input(
        self,
        now: float,
        *,
        notify: bool,
        delivery_pending: bool,
        running: frozenset[int] | None = None,
    ) -> tuple[QuietRestart, bool]:
        """Agent or operator input was written, which starts a cycle; *running* are
        the processes that ran before it. Returns the restart and a state change."""
        self._repaint_deadline = None
        changed = self._phase != "working"
        self._phase = "working"
        self._open("input", now).baseline = running
        return self._restart(notify=notify, supersede=delivery_pending), changed

    def resize(self, now: float) -> None:
        """The PTY changed size. A quiet program answers by redrawing what it showed."""
        if self._phase in {"ready", "repainting"}:
            self._repaint_deadline = now + self._repaint_window

    def attach(self, *, delivery_pending: bool) -> QuietRestart | None:
        """An Agent attached: its activity, including activity in progress, wakes it."""
        if self._phase != "working":
            return None
        return self._restart(notify=True, supersede=delivery_pending)

    def detach(self) -> None:
        self._wake_at_idle = False

    def sample(self, now: float, sample: ProcessSample) -> None:
        """The program's processes at *now*, sampled while a cycle runs."""
        samples = self._samples
        samples.append(_Sample(now, sample.cpu_seconds, sample.started))
        horizon = now - self._idle_seconds - _BUSY_WINDOW_SECONDS
        while samples and samples[0].at < horizon:
            samples.popleft()
        cycle = self._cycle
        if cycle is None or self.finished:
            return
        if cycle.baseline is None:
            cycle.baseline = sample.running
        cycle.child_running = bool(sample.running - cycle.baseline)
        if cycle.child_running or self._progress or self._cpu_busy():
            cycle.work_observed = True
            self._evidence(cycle, now)

    def settle(self, generation: int, signature: str) -> SettleKind | None:
        """The quiet period of ``generation`` passed showing ``signature``.

        Returns None for a stale timer or a phase without a quiet boundary.
        A program whose work comes in cycles stays ``working`` until its idle
        boundary.
        """
        if generation != self.generation or self._phase not in {"working", "repainting"}:
            return None
        if self._phase == "repainting":
            self._phase = "ready"
            self._repaint_deadline = None
            if not self._wake_at_idle:
                # Only a redraw for a new size happened: its screen shows the
                # known content.
                self._baseline = signature
            return "repaint"
        return "activity"

    def quiet(self, now: float) -> bool:
        """A cycle runs and its program printed nothing for the quiet period."""
        cycle = self._cycle
        return (
            cycle is not None
            and not self.finished
            and now - cycle.last_output_at >= self._quiet_seconds - _EPSILON
        )

    def idle(
        self, generation: int, now: float, signature: str, *, seen: bool, blank: bool
    ) -> IdleEffect | None:
        """Whether the cycle of ``generation`` reached its idle boundary at *now*.

        *seen* tells that the attached Agent's last result showed this screen
        revision; *blank* that the screen shows nothing. Returns None for a
        stale timer and while the program works.
        """
        cycle = self._cycle
        if generation != self.generation or self._phase != "working" or cycle is None:
            return None
        reason = self._idle_reason(cycle, now)
        if reason is None:
            return None
        self._phase = "ready"
        self._cycle = None
        self.idle_reason = reason
        deliver = self._wake_at_idle
        self._wake_at_idle = False
        if blank and cycle.origin == "start":
            # Nothing shows yet: the program's own output starts it up.
            self._next_origin = "start"
            return IdleEffect(False, False, reason)
        self._next_origin = "output"
        if reason == "quiet":
            self._resumable = cycle
        if seen or signature == self._baseline:
            self._baseline = signature
            return IdleEffect(False, False, reason)
        if deliver:
            self._baseline = signature
        return IdleEffect(True, deliver, reason)

    def fallback_at(self, generation: int) -> float | None:
        """When the cycle of ``generation`` can reach its fallback idle boundary,
        or None when that time has passed or no cycle runs."""
        cycle = self._cycle
        if generation != self.generation or cycle is None or self._phase != "working":
            return None
        return cycle.last_output_at + self._idle_seconds

    def acknowledge(self, signature: str) -> None:
        """The attached Agent durably received this screen."""
        self._baseline = signature

    def finish(self, *, error: bool) -> None:
        self._phase = "error" if error else "exited"
        self._cycle = None
        self._resumable = None
        self._wake_at_idle = False
        self._repaint_deadline = None

    def _open(self, origin: _Origin, now: float) -> _Cycle:
        self._cycle = _Cycle(origin, opened_at=now, last_output_at=now, last_evidence_at=now)
        self._next_origin = "output"
        self._resumable = None
        self.idle_reason = None
        return self._cycle

    def _evidence(self, cycle: _Cycle, now: float) -> None:
        """The program showed work at *now*."""
        if now - cycle.last_evidence_at > self._continuity_gap + _EPSILON:
            cycle.continuous = False
        cycle.last_evidence_at = max(cycle.last_evidence_at, now)

    def _apply_signals(self, cycle: _Cycle | None, signals: tuple[str, ...]) -> None:
        for signal in signals:
            if signal == "progress":
                self._progress = True
            elif signal == "progress_end":
                if self._progress and cycle is not None:
                    cycle.declared_end = True
                self._progress = False
            elif cycle is None:
                continue
            elif signal == "attention":
                cycle.declared_end = True
            elif signal == "prompt":
                cycle.declared_end = True
                cycle.command_running = False
            elif signal == "command":
                # A shell runs the next command line: an earlier prompt is stale.
                cycle.declared_end = False
                cycle.command_running = True

    def _idle_reason(self, cycle: _Cycle, now: float) -> IdleReason | None:
        quiet_for = now - cycle.last_output_at
        if quiet_for < self._quiet_seconds - _EPSILON:
            return None
        if cycle.declared_end and not self._progress:
            return "declared"
        if (
            cycle.origin != "output"
            and cycle.continuous
            and (not cycle.resumed or cycle.work_observed)
            and not cycle.child_running
            and not cycle.command_running
            and not self._progress
            and not self._cpu_busy()
        ):
            return "quiet"
        if quiet_for >= self._idle_seconds - _EPSILON and self._tree_quiet(cycle, now):
            return "fallback"
        return None

    def _cpu_busy(self) -> bool:
        """The tree used at least the busy share of a core over the last second."""
        samples = self._samples
        if len(samples) < 2:
            return False
        latest = samples[-1]
        earliest = next(
            sample for sample in samples if sample.at >= latest.at - _BUSY_WINDOW_SECONDS - _EPSILON
        )
        span = latest.at - earliest.at
        if span <= _EPSILON:
            return False
        return (latest.cpu_seconds - earliest.cpu_seconds) / span >= _BUSY_CPU_SHARE

    def _tree_quiet(self, cycle: _Cycle, now: float) -> bool:
        """Neither CPU time nor a new process in the processes since the output paused.

        A change in the CPU sum either way is work: POSIX sums running
        processes only, so an exit takes CPU time out of it.
        """
        window = [
            sample
            for sample in self._samples
            if sample.at >= max(cycle.last_output_at, now - self._idle_seconds) - _EPSILON
        ]
        if len(window) < 2:
            return True
        first, last = window[0], window[-1]
        return (
            abs(last.cpu_seconds - first.cpu_seconds) <= IDLE_CPU_SECONDS
            and last.started == first.started
        )

    def _restart(self, *, notify: bool, supersede: bool) -> QuietRestart:
        if supersede:
            # A cancelled delivery has not established an observed baseline;
            # an identical screen must still carry the outstanding update.
            self._baseline = None
            notify = True
        self._wake_at_idle = self._wake_at_idle or notify
        self.generation += 1
        return QuietRestart(self.generation, supersede)
