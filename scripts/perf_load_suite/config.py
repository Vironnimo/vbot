"""Validated load-scenario configuration and its persisted result projection."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from core.performance import MAX_RECORDING_SECONDS
from scripts.perf_load_suite.directive import DEFAULT_TOOLS, PerfDirective
from scripts.perf_load_suite.stack import PROJECT_ROOT
from scripts.perf_load_suite.swarm_script import DEFAULT_SWARM_TOOLS
from scripts.perf_load_suite.timeline import DEFAULT_SNAPSHOT_INTERVAL_SECONDS

DEFAULT_LEVELS: tuple[int, ...] = (1, 10, 20, 30)
QUICK_LEVELS: tuple[int, ...] = (1, 5)
SCENARIOS: tuple[str, ...] = ("sessions", "swarm")
WARMUP_CHUNK_TOKENS = 20_000
# Shape of one seeded turn in the watched Session's History (ui_history_turns):
# three Tool rounds and a Markdown answer, streamed unpaced.
UI_HISTORY_STEPS = 4
UI_HISTORY_TOKENS = 300
PROBE_MARGIN_SECONDS = 300
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "perf-results"


@dataclass(frozen=True)
class LoadConfig:
    """Everything that shapes one harness invocation (stored in ``result.json``).

    Empty ``tools`` selects the scenario's default Tools. With
    ``duration_minutes`` turns keep coming until the duration elapsed and
    ``turns`` is not used.
    """

    levels: tuple[int, ...] = DEFAULT_LEVELS
    scenario: str = "sessions"
    turns: int = 3
    duration_minutes: float | None = None
    identity_agents: int = 3
    steps: int = 4
    tokens: int = 400
    rate: float = 80.0
    think_ms: int = 600
    tools: tuple[str, ...] = ()
    calls: int = 1
    history_tokens: int = 0
    run_timeout_seconds: float = 300.0
    recording_max_seconds: int = 1800
    snapshot_interval_seconds: float = DEFAULT_SNAPSHOT_INTERVAL_SECONDS
    profile: bool = False
    profile_gil: bool = False
    ui: bool = False
    ui_profile: bool = False
    ui_history_turns: int | None = None
    keep: bool = False
    output_root: Path = field(default=DEFAULT_OUTPUT_ROOT)

    def __post_init__(self) -> None:
        if self.scenario not in SCENARIOS:
            raise ValueError(f"scenario must be one of {', '.join(SCENARIOS)}")
        if not self.tools:
            default = DEFAULT_SWARM_TOOLS if self.scenario == "swarm" else DEFAULT_TOOLS
            object.__setattr__(self, "tools", default)
        if not self.levels or any(level < 1 for level in self.levels):
            raise ValueError("levels must be positive Session counts")
        if len(set(self.levels)) != len(self.levels):
            raise ValueError("levels must be distinct")
        if self.turns < 1 or self.identity_agents < 1:
            raise ValueError("turns and identity agents must be at least 1")
        if self.duration_minutes is not None and self.duration_minutes <= 0:
            raise ValueError("duration must be positive")
        if self.snapshot_interval_seconds <= 0:
            raise ValueError("snapshot interval must be positive")
        if self.run_timeout_seconds <= 0:
            raise ValueError("run timeout must be positive")
        if not 1 <= self.recording_max_seconds <= MAX_RECORDING_SECONDS:
            raise ValueError(f"recording limit must be from 1 to {MAX_RECORDING_SECONDS} seconds")
        if self.history_tokens < 0:
            raise ValueError("history tokens must not be negative")
        if self.scenario == "swarm" and self.history_tokens:
            raise ValueError("history tokens apply to the sessions scenario only")
        if self.ui_history_turns is not None:
            if self.ui_history_turns < 0:
                raise ValueError("UI history turns must not be negative")
            if self.scenario == "swarm":
                raise ValueError("UI history turns apply to the sessions scenario only")
            if not self.ui:
                raise ValueError("UI history turns need the UI probe")
        # Validates steps/tokens/rate/think_ms/tools/calls once, up front.
        self.turn_directive(self.levels[0], 0, 0)
        self.swarm_directive(self.levels[0])

    @property
    def duration_seconds(self) -> float | None:
        return None if self.duration_minutes is None else self.duration_minutes * 60.0

    def turn_directive(self, level: int, session_index: int, turn_index: int) -> PerfDirective:
        return PerfDirective(
            tag=f"L{level}-s{session_index:03d}-t{turn_index + 1}",
            steps=self.steps,
            tokens=self.tokens,
            rate=self.rate,
            think_ms=self.think_ms,
            tools=self.tools,
            calls=self.calls,
        )

    def swarm_directive(self, level: int) -> PerfDirective:
        """The directive in a Swarm goal; unbounded turns in a duration run."""
        return PerfDirective(
            tag=f"L{level}-swarm",
            steps=self.steps,
            tokens=self.tokens,
            rate=self.rate,
            think_ms=self.think_ms,
            tools=self.tools,
            calls=self.calls,
            turns=0 if self.duration_minutes is not None else self.turns,
        )

    def warmup_turns(self) -> int:
        return math.ceil(self.history_tokens / WARMUP_CHUNK_TOKENS) if self.history_tokens else 0

    def warmup_directive(self, level: int, session_index: int, warmup_index: int) -> PerfDirective:
        remaining = self.history_tokens - warmup_index * WARMUP_CHUNK_TOKENS
        return PerfDirective(
            tag=f"L{level}-s{session_index:03d}-w{warmup_index + 1}",
            warmup_tokens=min(WARMUP_CHUNK_TOKENS, remaining),
            rate=0,
        )

    def ui_history_directive(self, level: int, turn_index: int) -> PerfDirective:
        """One seeded turn of the watched Session's History (``ui_history_turns``)."""
        return PerfDirective(
            tag=f"L{level}-ui-h{turn_index + 1:04d}",
            steps=UI_HISTORY_STEPS,
            tokens=UI_HISTORY_TOKENS,
            rate=0,
            tools=DEFAULT_TOOLS,
            markdown=True,
        )

    def recording_seconds(self) -> tuple[int, bool]:
        """Recording limit of one load phase and whether the server cap shortened it.

        A duration run records the duration plus one Run timeout for the
        wind-down; the server keeps a recording at most an hour.
        """
        duration = self.duration_seconds
        if duration is None:
            return self.recording_max_seconds, False
        wanted = max(self.recording_max_seconds, math.ceil(duration + self.run_timeout_seconds))
        return min(wanted, MAX_RECORDING_SECONDS), wanted > MAX_RECORDING_SECONDS

    def probe_seconds(self) -> int:
        """How long the UI probe measures at most before it stops on its own."""
        duration = self.duration_seconds
        if duration is None:
            return self.recording_max_seconds
        return math.ceil(duration + self.run_timeout_seconds + PROBE_MARGIN_SECONDS)

    @property
    def profiled_level(self) -> int | None:
        return max(self.levels) if self.profile or self.profile_gil else None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["levels"] = list(self.levels)
        data["tools"] = list(self.tools)
        data["output_root"] = str(self.output_root)
        sample = (
            self.swarm_directive(self.levels[0])
            if self.scenario == "swarm"
            else self.turn_directive(self.levels[0], 0, 0)
        )
        data["sample_directive"] = sample.render()
        return data
