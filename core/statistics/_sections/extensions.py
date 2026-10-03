"""Extensions: in-window activity of every listed Extension participant Session, by group.

Runs, Tool calls and errors are the Session's own; requests are the ledger's
at the Session's address, in the report ``Totals`` shape. ``last_activity`` is
the latest in-window record or request. Owners are sorted by name; groups
most recently active first, capped at ``TOP_EXTENSION_GROUPS``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.statistics._extensions import (
    EXTENSION_ACTOR_PREFIX,
    ExtensionSliceKey,
    extension_actor_key,
)
from core.statistics._sections.common import (
    RUN_STATUSES,
    JsonObject,
    ReportContext,
    Totals,
    totals_sql,
)
from core.statistics._sections.window import instant_timestamp

# Groups per owner, most recently active first; bounds the serialized report.
TOP_EXTENSION_GROUPS = 25


@dataclass
class _Activity:
    """In-window activity of one participant Session, or a rollup of several."""

    sessions: int = 0
    statuses: dict[str, int] = field(default_factory=lambda: dict.fromkeys(RUN_STATUSES, 0))
    errors: int = 0
    tool_calls: int = 0
    totals: Totals = field(default_factory=Totals)
    # Latest in-window instant, microseconds.
    last_instant: int | None = None

    @property
    def active(self) -> bool:
        return bool(sum(self.statuses.values()) or self.tool_calls or self.totals["calls"])

    def merge(self, other: _Activity) -> None:
        self.sessions += other.sessions
        for status, count in other.statuses.items():
            self.statuses[status] = self.statuses.get(status, 0) + count
        self.errors += other.errors
        self.tool_calls += other.tool_calls
        self.totals.merge(other.totals)
        self.seen(other.last_instant)

    def seen(self, instant: int | None) -> None:
        if instant is not None and (self.last_instant is None or instant > self.last_instant):
            self.last_instant = instant

    def json(self) -> JsonObject:
        return {
            "sessions": self.sessions,
            "runs": {"total": sum(self.statuses.values()), **self.statuses},
            "errors": self.errors,
            "tool_calls": self.tool_calls,
            "totals": self.totals.json(),
            "last_activity": (
                None if self.last_instant is None else instant_timestamp(self.last_instant)
            ),
        }


def build(context: ReportContext) -> JsonObject:
    participants: dict[int, tuple[ExtensionSliceKey, _Activity]] = {
        session.session_key: (session.extension, _Activity(sessions=1))
        for session in context.by_key.values()
        if session.extension is not None
    }
    if participants:
        _fill(context, {key: activity for key, (_label, activity) in participants.items()})
    return {"extensions": _owners(context, list(participants.values()))}


def _owners(
    context: ReportContext, participants: list[tuple[ExtensionSliceKey, _Activity]]
) -> list[JsonObject]:
    owners: dict[str, dict[str, list[tuple[ExtensionSliceKey, _Activity]]]] = {}
    for label, activity in participants:
        owners.setdefault(label.owner_name, {}).setdefault(label.group_id, []).append(
            (label, activity)
        )
    result: list[JsonObject] = []
    for owner_name in sorted(owners):
        owner = _Activity()
        groups: list[tuple[tuple[str, str], JsonObject]] = []
        for group_id, members in owners[owner_name].items():
            group = _Activity()
            for _label, activity in members:
                group.merge(activity)
            # A window hides groups without in-window Runs, Tool calls,
            # Model calls or errors; all-time reporting keeps every retained
            # group, including idle ones.
            if context.window.windowed and not (group.active or group.errors):
                continue
            owner.merge(group)
            title = next((label.group_title for label, _ in members if label.group_title), None)
            started_at = min(
                (label.created_at for label, _ in members if label.created_at), default=None
            )
            last = group.last_instant
            recency = instant_timestamp(last) if last is not None else started_at or ""
            groups.append(
                (
                    (recency, group_id),
                    {
                        "group_id": group_id,
                        "title": title,
                        "started_at": started_at,
                        "activity": group.json(),
                        "participants": [
                            {
                                "participant_id": label.participant_id,
                                "name": label.participant_name,
                                "model": label.model,
                                "session_id": label.session_id,
                                "activity": activity.json(),
                            }
                            for label, activity in members
                        ],
                    },
                )
            )
        if not groups:
            continue
        groups.sort(key=lambda item: item[0], reverse=True)
        result.append(
            {
                "name": owner_name,
                "actor_key": extension_actor_key(owner_name),
                "total_groups": len(groups),
                "groups_truncated": len(groups) > TOP_EXTENSION_GROUPS,
                "activity": owner.json(),
                "groups": [group for _order, group in groups[:TOP_EXTENSION_GROUPS]],
            }
        )
    return result


def _fill(context: ReportContext, participants: dict[int, _Activity]) -> None:
    context.sessions_table  # noqa: B018 - loads the Session address ids.
    by_address = {context.session_addresses[key]: key for key in participants}
    in_window = context.instant_condition
    for session_key, status, count in context.query(
        f"""
        SELECT r.session_key, r.status, COUNT(*) FROM agg_runs r
        WHERE r.origin = 'extension' AND {in_window("r.start_instant")}
        GROUP BY r.session_key, r.status
        """
    ):
        target = participants.get(session_key)
        if target is not None:
            target.statuses[status] = target.statuses.get(status, 0) + count
    for session_key, calls in context.query(
        f"""
        SELECT t.session_key, SUM(t.calls) FROM agg_tools t
        WHERE t.origin = 'extension' AND {context.hour_condition("t")}
        GROUP BY t.session_key
        """
    ):
        if session_key in participants:
            participants[session_key].tool_calls += calls
    for session_key, count in context.query(
        f"SELECT e.session_key, COUNT(*) FROM stat_errors e WHERE {in_window('e.instant')} "
        "GROUP BY e.session_key"
    ):
        if session_key in participants:
            participants[session_key].errors += count
    for address, *measures in context.query(
        f"""
        SELECT u.address, {totals_sql()}
        FROM agg_usage a JOIN {context.units_table} u ON u.unit_key = a.unit_key
        WHERE {context.hour_condition("a")} AND u.actor LIKE ?
        GROUP BY u.address
        """,
        (EXTENSION_ACTOR_PREFIX + "%",),
    ):
        if address in by_address:
            participants[by_address[address]].totals.add(measures)
    _last_activity(context, participants, by_address)


def _last_activity(
    context: ReportContext, participants: dict[int, _Activity], by_address: dict[int, int]
) -> None:
    low, high = context.window.instants
    for session_key, max_instant in context.query(
        "SELECT session_key, max_instant FROM stat_sessions WHERE max_instant IS NOT NULL"
    ):
        if session_key not in participants or max_instant < low:
            continue
        if max_instant >= high:
            # Later records exist; find the latest one inside the window.
            max_instant = context.scalar(
                "SELECT MAX(instant) FROM stat_records "
                "WHERE session_key = ? AND instant >= ? AND instant < ?",
                (session_key, low, high),
            )
        participants[session_key].seen(max_instant)
    for address, max_instant in context.query(
        f"""
        SELECT u.address, MAX((
            SELECT MAX(c.instant) FROM stat_usage_calls c
            WHERE c.session_key = u.unit_key AND {context.instant_condition("c.instant")}
        ))
        FROM {context.units_table} u WHERE u.actor LIKE ? GROUP BY u.address
        """,
        (EXTENSION_ACTOR_PREFIX + "%",),
    ):
        session_key = by_address.get(address)
        if session_key is not None:
            participants[session_key].seen(max_instant)
