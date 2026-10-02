"""Skills: offered and activated Skills of the listed Sessions, joined to the current inventory."""

from __future__ import annotations

from dataclasses import asdict

from core.statistics._sections.common import JsonObject, ReportContext
from core.statistics.skills import SkillUsageAccumulator, empty_inventory


def build(context: ReportContext) -> JsonObject:
    window = context.window
    accumulator = SkillUsageAccumulator(since=window.since, until=window.until)
    # Skills apply their own windows (offers by Session start, activations by
    # record time) and count activations ever, so every activation is read.
    activations: dict[int, list[tuple[str, str | None]]] = {}
    for session_key, name, timestamp in context.query(
        """
        SELECT k.session_key, k.name, r.timestamp
        FROM stat_skills k JOIN stat_records r ON r.session_key = k.session_key AND r.seq = k.seq
        ORDER BY k.session_key, k.seq
        """
    ):
        activations.setdefault(session_key, []).append((name, timestamp))
    for session in context.by_key.values():
        if not session.skill_use:
            continue
        accumulator.observe_session(
            display_key=session.display_key,
            created_at=session.created_at,
            offered_names=list(session.offered_skills),
            activations=activations.get(session.session_key, []),
        )
    inventory = context.skill_inventory
    return asdict(accumulator.build(inventory if inventory is not None else empty_inventory()))
