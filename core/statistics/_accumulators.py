"""Internal mutable records used while aggregating one Statistics report."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.statistics._measurements import _provider_of
from core.statistics._projection import day_key

if TYPE_CHECKING:
    from core.statistics._extensions import ExtensionSlice
    from core.statistics._units import ReportUnit


@dataclass
class _AgentAcc:
    agent_id: str
    sessions: int = 0
    runs: int = 0
    chat_messages: int = 0
    session_records: int = 0
    errors: int = 0
    last_activity: str | None = None


@dataclass
class _ModelAcc:
    provider: str
    model: str
    runs: int = 0
    assistant_messages: int = 0
    measured_input_tokens: int = 0
    measured_output_tokens: int = 0
    reasoning_tokens: int = 0
    reasoning_turns: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    estimated_turns: int = 0
    errors: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_turns: int = 0
    cache_input_tokens: int = 0
    run_duration_total_ms: int = 0
    run_duration_count: int = 0


@dataclass
class _ProviderAcc:
    provider: str
    runs: int = 0
    assistant_messages: int = 0
    measured_input_tokens: int = 0
    measured_output_tokens: int = 0
    reasoning_tokens: int = 0
    reasoning_turns: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    estimated_turns: int = 0
    errors: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_turns: int = 0
    cache_input_tokens: int = 0


@dataclass
class _DailyAcc:
    runs: int = 0
    completed: int = 0
    failed: int = 0
    cancelled: int = 0
    interrupted: int = 0
    errors: int = 0
    measured_input_tokens: int = 0
    measured_output_tokens: int = 0
    reasoning_tokens: int = 0
    reasoning_turns: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_input_tokens: int = 0


class ReportLedger:
    """Report units plus the tallies that several report sections feed.

    Units and their Extension slices are indexed by processing position.
    Agent rows keep first-registration order; Model, Provider and daily
    tallies are filled by the usage, error and Run sections alike and read
    by every section that reports them.
    """

    def __init__(self) -> None:
        self.units: list[ReportUnit] = []
        self.slices: list[ExtensionSlice | None] = []
        self.agent_order: list[str] = []
        self.agents: dict[str, _AgentAcc] = {}
        self.models: dict[str, _ModelAcc] = {}
        self.providers: dict[str, _ProviderAcc] = {}
        self.daily: dict[str, _DailyAcc] = {}

    def add_unit(self, unit: ReportUnit, unit_slice: ExtensionSlice | None) -> None:
        self.agent(unit.display_key)
        self.units.append(unit)
        self.slices.append(unit_slice)

    def agent(self, agent_id: str) -> _AgentAcc:
        accumulator = self.agents.get(agent_id)
        if accumulator is None:
            accumulator = _AgentAcc(agent_id=agent_id)
            self.agents[agent_id] = accumulator
            self.agent_order.append(agent_id)
        return accumulator

    def unit_agent(self, unit: int) -> _AgentAcc:
        """Return the Agent row of a registered unit."""
        return self.agents[self.units[unit].display_key]

    def model(self, model_key: str) -> _ModelAcc:
        accumulator = self.models.get(model_key)
        if accumulator is None:
            accumulator = _ModelAcc(provider=_provider_of(model_key), model=model_key)
            self.models[model_key] = accumulator
        return accumulator

    def provider(self, provider: str) -> _ProviderAcc:
        accumulator = self.providers.get(provider)
        if accumulator is None:
            accumulator = _ProviderAcc(provider=provider)
            self.providers[provider] = accumulator
        return accumulator

    def day(self, day: int) -> _DailyAcc:
        """Return the bucket of an indexed UTC day number."""
        key = day_key(day)
        bucket = self.daily.get(key)
        if bucket is None:
            bucket = _DailyAcc()
            self.daily[key] = bucket
        return bucket

    def sorted_daily(self) -> list[tuple[str, _DailyAcc]]:
        return sorted(self.daily.items(), key=lambda item: item[0])
