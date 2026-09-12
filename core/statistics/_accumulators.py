"""Internal mutable records used while aggregating one Statistics report."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


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


@dataclass
class _ToolAcc:
    name: str
    calls: int = 0
    successes: int = 0
    failures: int = 0
    duration_total_ms: int = 0
    duration_samples: list[int] = field(default_factory=list)
    error_codes: Counter[str] = field(default_factory=Counter)


@dataclass
class _CompactionSessionAcc:
    agent_id: str
    session_id: str
    compactions: int = 0
    estimated_reclaimed_tokens: int = 0
    last_compaction: str | None = None
