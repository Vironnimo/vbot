"""Extension-owned Session activity: attribution keys, slices and report records.

Extension participant Sessions feed every ordinary report section under one
reserved actor key per owner (``extension:<name>``) instead of their synthetic
participant Agent ids. The same aggregation pass also fills one slice per
participant Session; this module rolls those slices up into groups and owners
for the ``extensions`` report section, so its figures always agree with the
report totals they are part of.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from core.statistics._costs import CostTotals
from core.statistics._measurements import _max_timestamp
from core.statistics.report import RunStatusCounts
from core.utils.timestamps import parse_canonical_timestamp

# Agent ids and ``agent@project`` addresses never contain ``:``, so this actor
# key cannot collide with an Agent in per-agent report rows.
EXTENSION_ACTOR_PREFIX = "extension:"

# Groups per owner, most recently active first; bounds the serialized report.
TOP_EXTENSION_GROUPS = 100


def extension_actor_key(owner_name: str) -> str:
    """Return the per-agent report key attributing one owner's Sessions."""
    return f"{EXTENSION_ACTOR_PREFIX}{owner_name}"


@dataclass(frozen=True)
class ExtensionSliceKey:
    """One owner-managed participant Session and its display labels."""

    owner_name: str
    group_id: str
    group_title: str | None
    participant_id: str
    participant_name: str | None
    model: str | None
    session_id: str
    created_at: str | None


@dataclass
class ExtensionSlice:
    """Mutable in-window activity of one participant Session."""

    records: int = 0
    runs: int = 0
    status: Counter[str] = field(default_factory=Counter)
    errors: int = 0
    tool_calls: int = 0
    model_calls: int = 0
    measured_input_tokens: int = 0
    measured_output_tokens: int = 0
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    costs: CostTotals = field(default_factory=CostTotals)
    last_activity: str | None = None

    def merge(self, other: ExtensionSlice) -> None:
        self.records += other.records
        self.runs += other.runs
        self.status.update(other.status)
        self.errors += other.errors
        self.tool_calls += other.tool_calls
        self.model_calls += other.model_calls
        self.measured_input_tokens += other.measured_input_tokens
        self.measured_output_tokens += other.measured_output_tokens
        self.estimated_input_tokens += other.estimated_input_tokens
        self.estimated_output_tokens += other.estimated_output_tokens
        self.costs.merge(other.costs)
        if other.last_activity is not None:
            self.last_activity = _max_timestamp(self.last_activity, other.last_activity)


@dataclass(frozen=True)
class ExtensionActivity:
    """In-window activity; measured and estimated tokens stay separate."""

    sessions: int
    runs: int
    run_status: RunStatusCounts
    errors: int
    tool_calls: int
    model_calls: int
    measured_input_tokens: int
    measured_output_tokens: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    costs: CostTotals
    last_activity: str | None


@dataclass(frozen=True)
class ExtensionParticipantUsage:
    participant_id: str
    name: str | None
    model: str | None
    session_id: str
    activity: ExtensionActivity


@dataclass(frozen=True)
class ExtensionGroupUsage:
    group_id: str
    title: str | None
    started_at: str | None
    activity: ExtensionActivity
    participants: list[ExtensionParticipantUsage]


@dataclass(frozen=True)
class ExtensionUsage:
    name: str
    actor_key: str
    total_groups: int
    groups_truncated: bool
    activity: ExtensionActivity
    groups: list[ExtensionGroupUsage]


@dataclass(frozen=True)
class ExtensionsSection:
    extensions: list[ExtensionUsage]


@dataclass
class _GroupRollup:
    group_id: str
    title: str | None = None
    started_at: str | None = None
    participants: list[tuple[ExtensionSliceKey, ExtensionSlice]] = field(default_factory=list)


class ExtensionUsageAccumulator:
    """Own participant slices for one report and roll them up at build time."""

    def __init__(self, *, windowed: bool) -> None:
        self._windowed = windowed
        self._owners: dict[str, dict[str, _GroupRollup]] = {}

    def slice(self, key: ExtensionSliceKey) -> ExtensionSlice:
        groups = self._owners.setdefault(key.owner_name, {})
        group = groups.setdefault(key.group_id, _GroupRollup(key.group_id))
        group.title = group.title or key.group_title
        group.started_at = _min_timestamp(group.started_at, key.created_at)
        participant = ExtensionSlice()
        group.participants.append((key, participant))
        return participant

    def build(self) -> ExtensionsSection:
        extensions: list[ExtensionUsage] = []
        for owner_name in sorted(self._owners):
            owner_total = ExtensionSlice()
            owner_sessions = 0
            groups: list[ExtensionGroupUsage] = []
            for group in self._owners[owner_name].values():
                group_total = ExtensionSlice()
                participants: list[ExtensionParticipantUsage] = []
                for key, participant in group.participants:
                    group_total.merge(participant)
                    participants.append(
                        ExtensionParticipantUsage(
                            participant_id=key.participant_id,
                            name=key.participant_name,
                            model=key.model,
                            session_id=key.session_id,
                            activity=_activity(participant, sessions=1),
                        )
                    )
                # A window hides groups without in-window activity; all-time
                # reporting keeps every retained group, including idle ones.
                if self._windowed and group_total.records == 0 and group_total.model_calls == 0:
                    continue
                owner_total.merge(group_total)
                owner_sessions += len(participants)
                groups.append(
                    ExtensionGroupUsage(
                        group_id=group.group_id,
                        title=group.title,
                        started_at=group.started_at,
                        activity=_activity(group_total, sessions=len(participants)),
                        participants=participants,
                    )
                )
            if not groups:
                continue
            groups.sort(key=_recency, reverse=True)
            extensions.append(
                ExtensionUsage(
                    name=owner_name,
                    actor_key=extension_actor_key(owner_name),
                    total_groups=len(groups),
                    groups_truncated=len(groups) > TOP_EXTENSION_GROUPS,
                    activity=_activity(owner_total, sessions=owner_sessions),
                    groups=groups[:TOP_EXTENSION_GROUPS],
                )
            )
        return ExtensionsSection(extensions=extensions)


def _activity(value: ExtensionSlice, *, sessions: int) -> ExtensionActivity:
    return ExtensionActivity(
        sessions=sessions,
        runs=value.runs,
        run_status=RunStatusCounts(
            completed=value.status.get("completed", 0),
            failed=value.status.get("failed", 0),
            cancelled=value.status.get("cancelled", 0),
            interrupted=value.status.get("interrupted", 0),
        ),
        errors=value.errors,
        tool_calls=value.tool_calls,
        model_calls=value.model_calls,
        measured_input_tokens=value.measured_input_tokens,
        measured_output_tokens=value.measured_output_tokens,
        estimated_input_tokens=value.estimated_input_tokens,
        estimated_output_tokens=value.estimated_output_tokens,
        costs=CostTotals(**vars(value.costs)),
        last_activity=value.last_activity,
    )


def _recency(group: ExtensionGroupUsage) -> datetime:
    moment = group.activity.last_activity or group.started_at
    return datetime.min.replace(tzinfo=UTC) if moment is None else parse_canonical_timestamp(moment)


def _min_timestamp(current: str | None, candidate: str | None) -> str | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    earlier = parse_canonical_timestamp(candidate) < parse_canonical_timestamp(current)
    return candidate if earlier else current
