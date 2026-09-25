"""Project canonical Session messages into typed Statistics index rows.

Only the facts that report sections aggregate enter the index: roles,
timestamps, Run ids, normalized Usage and cost accounting, Tool outcomes,
error kinds, Compaction measurements, Run terminal facts and Skill activation
names. Message text, Reasoning, Tool arguments and results, and Skill content
never do. Every derived value mirrors the normalization the report applies, so
SQL aggregation over these rows reproduces the canonical interpretation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from core.chat.messages import ChatMessage, usage_token_is_estimated
from core.models.pricing import nonnegative_amount, price_usage, project_cost
from core.sessions import skill_context_note_name, skill_tool_activation_name
from core.statistics._measurements import (
    UNKNOWN_MODEL_KEY,
    _duration_ms,
    _parse_envelope,
    _provider_model_key,
    _timing_field,
    _usage_nonnegative_int,
)
from core.utils.timestamps import parse_canonical_timestamp

JsonObject = dict[str, Any]

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_EPOCH_DATE = date(1970, 1, 1)
_MICROSECOND = timedelta(microseconds=1)
MICROSECONDS_PER_DAY = 86_400_000_000
MICROSECONDS_PER_HOUR = 3_600_000_000

CALL_KIND_CHAT = 0
CALL_KIND_COMPACTION = 1
CALL_KIND_AUXILIARY = 2

CALL_COLUMNS = (
    "session_key, seq, kind, instant, day, model_key, has_model, visible, has_usage, "
    "input_tokens, output_tokens, reasoning_tokens, cache_read_tokens, cache_write_tokens, "
    "input_estimated, output_estimated, has_cache, reasoning_present, cache_read_present, "
    "cache_write_present, price_estimated, reported_cost_usd, retrospective, priced, "
    "cost_usd, cost_source, cost_json, purpose"
)

COST_UNPRICED = 0
COST_PROVIDER = 1
COST_CATALOG = 2
UNKNOWN_COST: JsonObject = {"amount_usd": None, "source": "unknown"}


def datetime_instant(value: datetime) -> int:
    """Return exact UTC microseconds since the epoch for an aware datetime."""
    return (value - _EPOCH) // _MICROSECOND


def timestamp_instant(value: str) -> int:
    """Return the instant of a stored canonical Session timestamp.

    Sessions store every timestamp in the canonical form, so any other value is
    bad data and raises ``ValueError``.
    """
    return datetime_instant(parse_canonical_timestamp(value))


def day_key(day: int) -> str:
    """Return the ISO date for a day number derived from an instant."""
    return (_EPOCH_DATE + timedelta(days=day)).isoformat()


def cost_source_class(cost: JsonObject) -> tuple[int, float | None]:
    """Classify one projected call cost the way cost totals count it."""
    amount = nonnegative_amount(cost.get("amount_usd"))
    source = cost.get("source")
    if amount is None or source not in {"provider", "catalog"}:
        return COST_UNPRICED, None
    return (COST_PROVIDER if source == "provider" else COST_CATALOG), amount


@dataclass(frozen=True)
class PricingInputs:
    """The Usage facts retrospective catalog pricing reads, in typed form."""

    reported_cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_read_present: bool
    cache_write_tokens: int | None
    cache_write_present: bool
    reasoning_tokens: int | None
    reasoning_present: bool
    estimated: bool

    def usage(self) -> JsonObject:
        """Rebuild a Usage mapping that prices exactly like the canonical one.

        A present but invalid count becomes ``-1`` so pricing rejects it the same
        way; absent keys stay absent so their defaults still apply.
        """
        usage: JsonObject = {"estimated": self.estimated}
        if self.reported_cost_usd is not None:
            usage["reported_cost_usd"] = self.reported_cost_usd
        usage["input_tokens"] = _or_invalid(self.input_tokens)
        usage["output_tokens"] = _or_invalid(self.output_tokens)
        if self.cache_read_present:
            usage["cache_read_tokens"] = _or_invalid(self.cache_read_tokens)
        if self.cache_write_present:
            usage["cache_write_tokens"] = _or_invalid(self.cache_write_tokens)
        if self.reasoning_present:
            usage["reasoning_tokens"] = _or_invalid(self.reasoning_tokens)
        return usage

    def price(self, pricing: Any) -> JsonObject:
        """Return the projected retrospective cost under the current pricing."""
        return project_cost(price_usage(self.usage(), pricing)) or dict(UNKNOWN_COST)


def _or_invalid(value: int | None) -> int:
    return -1 if value is None else value


@dataclass
class ProjectedRows:
    """Typed rows for one ingested batch, ready for ``executemany``."""

    session_key: int
    records: list[tuple[Any, ...]] = field(default_factory=list)
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    tools: list[tuple[Any, ...]] = field(default_factory=list)
    errors: list[tuple[Any, ...]] = field(default_factory=list)
    checkpoints: list[tuple[Any, ...]] = field(default_factory=list)
    runs: list[tuple[Any, ...]] = field(default_factory=list)
    skills: list[tuple[Any, ...]] = field(default_factory=list)
    min_instant: int | None = None
    max_instant: int | None = None

    def add(self, seq: int, message: ChatMessage) -> None:
        """Project one canonical message at its Session sequence number."""
        key = self.session_key
        instant = timestamp_instant(message.timestamp)
        day = instant // MICROSECONDS_PER_DAY
        self._observe_instant(instant)
        self.records.append((key, seq, message.role, message.timestamp, instant, message.run_id))
        role = message.role
        if role == "assistant":
            self._add_call(
                seq,
                CALL_KIND_CHAT,
                instant,
                day,
                message.model,
                message.usage,
                visible=isinstance(message.content, str) and bool(message.content.strip()),
            )
        elif role == "tool":
            self._add_tool(seq, instant, message)
        elif role == "note":
            skill_name = skill_context_note_name(message)
            if skill_name is not None:
                self.skills.append((key, seq, skill_name))
        elif role == "error":
            self.errors.append((key, seq, instant, day, message.error_kind or UNKNOWN_MODEL_KEY))
        elif role == "compaction_checkpoint":
            self._add_checkpoint(seq, instant, day, message)
        elif role == "run_summary":
            self._add_run(seq, instant, day, message)

    def _observe_instant(self, instant: int) -> None:
        if self.min_instant is None or instant < self.min_instant:
            self.min_instant = instant
        if self.max_instant is None or instant > self.max_instant:
            self.max_instant = instant

    def _add_tool(self, seq: int, instant: int, message: ChatMessage) -> None:
        activation = skill_tool_activation_name(message)
        outcome: int | None
        error_code: str | None = None
        if activation is not None:
            # A loaded Skill activation is a successful Tool call by definition.
            outcome = 1
            self.skills.append((self.session_key, seq, activation))
        else:
            envelope = _parse_envelope(message.content)
            if envelope is None:
                outcome = None
            elif envelope["ok"]:
                outcome = 1
            else:
                outcome = 0
                error_code = envelope["error"]["code"]
        self.tools.append(
            (
                self.session_key,
                seq,
                instant,
                message.name or UNKNOWN_MODEL_KEY,
                outcome,
                error_code,
                _duration_ms(message.timing),
            )
        )

    def _add_checkpoint(self, seq: int, instant: int, day: int, message: ChatMessage) -> None:
        usage = message.usage or {}
        self.checkpoints.append(
            (
                self.session_key,
                seq,
                instant,
                message.compaction_strategy or "unknown",
                _usage_nonnegative_int(usage, "context_tokens_before"),
                _usage_nonnegative_int(usage, "context_tokens_after"),
                _usage_nonnegative_int(usage, "compaction_duration_ms"),
            )
        )
        call = usage.get("model_call")
        if isinstance(call, dict) and isinstance(call.get("usage"), dict):
            self._add_call(
                seq,
                CALL_KIND_COMPACTION,
                instant,
                day,
                call.get("model"),
                call["usage"],
                visible=False,
            )

    def _add_run(self, seq: int, instant: int, day: int, message: ChatMessage) -> None:
        timing_started = _timing_field(message.timing, "started_at")
        timing_completed = _timing_field(message.timing, "completed_at")
        self.runs.append(
            (
                self.session_key,
                seq,
                instant,
                day,
                message.run_id,
                message.status or "completed",
                _duration_ms(message.timing),
                timing_started,
                timing_completed,
                timestamp_instant(timing_started or message.timestamp),
                timestamp_instant(timing_completed or message.timestamp),
            )
        )

    def _add_call(
        self,
        seq: int,
        kind: int,
        instant: int,
        day: int,
        model: Any,
        usage: Any,
        *,
        visible: bool,
        purpose: str | None = None,
    ) -> None:
        model_key = _provider_model_key(model) if isinstance(model, str) else UNKNOWN_MODEL_KEY
        values: JsonObject = usage if isinstance(usage, dict) else {}
        input_estimated = usage_token_is_estimated(values, "input_tokens")
        output_estimated = usage_token_is_estimated(values, "output_tokens")
        pricing = PricingInputs(
            reported_cost_usd=nonnegative_amount(values.get("reported_cost_usd")),
            input_tokens=_usage_nonnegative_int(values, "input_tokens"),
            output_tokens=_usage_nonnegative_int(values, "output_tokens"),
            cache_read_tokens=_usage_nonnegative_int(values, "cache_read_tokens"),
            cache_read_present="cache_read_tokens" in values,
            cache_write_tokens=_usage_nonnegative_int(values, "cache_write_tokens"),
            cache_write_present="cache_write_tokens" in values,
            reasoning_tokens=_usage_nonnegative_int(values, "reasoning_tokens"),
            reasoning_present="reasoning_tokens" in values,
            estimated=input_estimated or output_estimated,
        )
        snapshot = project_cost(values["cost"]) if "cost" in values else None
        retrospective = not isinstance(snapshot, dict)
        serialized_cost: str | None = None
        cost_class = COST_UNPRICED
        cost_amount: float | None = None
        if not retrospective:
            assert isinstance(snapshot, dict)
            cost = project_cost(snapshot) or dict(UNKNOWN_COST)
            cost_class, cost_amount = cost_source_class(cost)
            serialized_cost = cost_json(cost)
        self.calls.append(
            (
                self.session_key,
                seq,
                kind,
                instant,
                day,
                model_key,
                int(bool(model)),
                int(visible),
                int(usage is not None),
                pricing.input_tokens,
                pricing.output_tokens,
                pricing.reasoning_tokens,
                pricing.cache_read_tokens,
                pricing.cache_write_tokens,
                int(input_estimated),
                int(output_estimated),
                int(pricing.cache_read_present or pricing.cache_write_present),
                int(pricing.reasoning_present),
                int(pricing.cache_read_present),
                int(pricing.cache_write_present),
                int(pricing.estimated),
                pricing.reported_cost_usd,
                int(retrospective),
                int(not retrospective),
                cost_amount,
                cost_class,
                serialized_cost,
                purpose or ("compaction" if kind == CALL_KIND_COMPACTION else "chat"),
            )
        )

    def add_usage_call(
        self,
        seq: int,
        *,
        timestamp: str,
        model: str,
        kind: str,
        usage: JsonObject,
    ) -> None:
        """Project a durable request without creating a Session record."""
        instant = timestamp_instant(timestamp)
        self._add_call(
            seq,
            {"chat": CALL_KIND_CHAT, "compaction": CALL_KIND_COMPACTION}.get(
                kind, CALL_KIND_AUXILIARY
            ),
            instant,
            instant // MICROSECONDS_PER_DAY,
            model,
            usage,
            visible=False,
            purpose=kind,
        )


def compact_json(value: JsonObject) -> str:
    """Serialize a small accounting object deterministically, for comparison."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def cost_json(cost: JsonObject) -> str:
    """Serialize one projected call cost, keeping its key order and exact amounts."""
    return json.dumps(cost, ensure_ascii=False, separators=(",", ":"))
