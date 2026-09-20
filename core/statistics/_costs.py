"""Per-call cost projection over canonical Usage, with explicit coverage."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core.chat.messages import ChatMessage
from core.models.pricing import TokenPricing, nonnegative_amount, price_usage, project_cost
from core.statistics._measurements import (
    _date_key,
    _provider_model_key,
    _read_usage,
    _usage_nonnegative_int,
)
from core.statistics.timestamps import parse_timestamp


@dataclass
class CostTotals:
    calls: int = 0
    reported_calls: int = 0
    estimated_calls: int = 0
    unpriced_calls: int = 0
    retrospective_calls: int = 0
    reported_usd: float | None = None
    estimated_usd: float | None = None

    @property
    def known_total(self) -> float:
        return (self.reported_usd or 0) + (self.estimated_usd or 0)

    def add(self, cost: dict[str, Any], *, retrospective: bool) -> None:
        self.calls += 1
        amount = nonnegative_amount(cost.get("amount_usd"))
        if amount is None or cost.get("source") not in {"provider", "catalog"}:
            self.unpriced_calls += 1
        elif cost["source"] == "provider":
            self.reported_calls += 1
            self.reported_usd = (self.reported_usd or 0) + amount
        else:
            self.estimated_calls += 1
            self.estimated_usd = (self.estimated_usd or 0) + amount
            self.retrospective_calls += int(retrospective)


@dataclass(frozen=True)
class ModelCosts:
    model: str
    totals: CostTotals


@dataclass(frozen=True)
class DailyCosts:
    date: str
    totals: CostTotals


@dataclass(frozen=True)
class SessionCosts:
    agent_id: str
    session_id: str
    session_title: str | None
    totals: CostTotals


@dataclass(frozen=True)
class CallCost:
    timestamp: str
    agent_id: str
    session_id: str
    session_title: str | None
    run_id: str | None
    model: str
    kind: str
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    estimated_tokens: bool
    cost: dict[str, Any]
    retrospective: bool


@dataclass(frozen=True)
class CostsSection:
    totals: CostTotals
    models: list[ModelCosts]
    daily: list[DailyCosts]
    top_sessions: list[SessionCosts]
    recent_calls: list[CallCost]
    recent_calls_truncated: bool
    compactions: CostTotals


@dataclass
class CostAccumulator:
    pricing_lookup: Callable[[str], TokenPricing | None] | None = None
    totals: CostTotals = field(default_factory=CostTotals)
    compactions: CostTotals = field(default_factory=CostTotals)
    models: dict[str, CostTotals] = field(default_factory=dict)
    daily: dict[str, CostTotals] = field(default_factory=dict)
    sessions: dict[tuple[str, str], SessionCosts] = field(default_factory=dict)
    recent: list[CallCost] = field(default_factory=list)

    def observe(
        self,
        message: ChatMessage,
        *,
        agent_id: str,
        session_id: str,
        session_title: str | None,
        kind: str = "chat",
    ) -> None:
        model = _provider_model_key(message.model)
        usage = message.usage or {}
        cost = usage.get("cost")
        retrospective = not isinstance(cost, dict)
        if retrospective:
            pricing = self.pricing_lookup(model) if self.pricing_lookup else None
            cost = price_usage(usage, pricing)
        assert isinstance(cost, dict)
        cost = project_cost(cost) or {"amount_usd": None, "source": "unknown"}
        session = self.sessions.setdefault(
            (agent_id, session_id), SessionCosts(agent_id, session_id, session_title, CostTotals())
        )
        buckets = [self.totals, self.models.setdefault(model, CostTotals()), session.totals]
        day = _date_key(message.timestamp)
        if day:
            buckets.append(self.daily.setdefault(day, CostTotals()))
        if kind == "compaction":
            buckets.append(self.compactions)
        for bucket in buckets:
            bucket.add(cost, retrospective=retrospective)
        facts = _read_usage(message.usage)
        self.recent.append(
            CallCost(
                message.timestamp,
                agent_id,
                session_id,
                session_title,
                message.run_id,
                model,
                kind,
                _usage_nonnegative_int(usage, "input_tokens"),
                _usage_nonnegative_int(usage, "output_tokens"),
                _usage_nonnegative_int(usage, "cache_read_tokens"),
                facts.estimated,
                cost,
                retrospective,
            )
        )
        # Bound report memory as well as its serialized size.
        if len(self.recent) > 100:
            self.recent = sorted(
                self.recent,
                key=lambda call: (
                    parse_timestamp(call.timestamp) or datetime.min.replace(tzinfo=UTC)
                ),
                reverse=True,
            )[:50]

    def build(self) -> CostsSection:
        return CostsSection(
            totals=self.totals,
            models=[
                ModelCosts(model, totals)
                for model, totals in sorted(
                    self.models.items(), key=lambda pair: (-pair[1].known_total, pair[0])
                )
            ],
            daily=[DailyCosts(day, totals) for day, totals in sorted(self.daily.items())],
            top_sessions=sorted(
                self.sessions.values(),
                key=lambda row: (-row.totals.known_total, row.agent_id, row.session_id),
            )[:20],
            recent_calls=sorted(
                self.recent,
                key=lambda call: (
                    parse_timestamp(call.timestamp) or datetime.min.replace(tzinfo=UTC)
                ),
                reverse=True,
            )[:50],
            recent_calls_truncated=self.totals.calls > 50,
            compactions=self.compactions,
        )
