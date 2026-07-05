"""Skill-usage aggregation for the statistics report.

This is the ``skills`` section sibling of :mod:`core.statistics.statistics`: it
turns already-persisted session data into a per-skill "offered vs. activated"
view, joined against the **current** skill inventory. It adds no persistence of
its own, mirroring the statistics domain's hard read-only constraint.

Two persisted facts feed it, both derivable without new storage:

- **Offered** — a session's ``seen_skills`` metadata (the skills whose catalog
  entry that session was shown), written by the chat loop. The list carries no
  per-skill time, so an offered skill inherits the offering session's
  ``created_at`` (session start is the honest approximation; mid-session
  additions inherit it too).
- **Activated** — the ``[skill-context] `` activation notes persisted in the
  transcript. Each note's skill name comes from the sessions domain's
  :func:`core.sessions.skill_context_note_name` helper (never re-parsed here);
  the note timestamp is the activation time. Activation is once-per-session by
  design, so a ``(session, skill)`` pair is counted at most once — factual, not
  a cap.

Usage is keyed by **bare skill name across scopes**: a name collision between,
say, a global and a project skill aggregates into one row whose ``origins`` list
makes the ambiguity visible. Per-scope attribution is deliberately not built.

**Window semantics** follow the domain convention. The inventory join and the
``total_skills`` / ``never_used_skills`` snapshots are window-independent;
``offered_sessions`` and its timestamps filter by the offering session's
``created_at``; ``activated_sessions`` and its timestamps filter by note
timestamp. Because ``never_used_skills`` means "zero activations *ever*" while
``used_skills`` means ">=1 activation *in window*", the accumulator tracks
activations both windowed and unwindowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from core.statistics.timestamps import parse_timestamp

# The session-metadata sidecar key holding the skills a session was offered
# (its ``<available_skills>`` catalog names). Owned and written by the chat loop
# (``core/chat/chat.py``); mirrored here as the persisted read contract for the
# offered-skills tally. A single source-of-truth constant so the read key can
# never silently drift from the write key.
SEEN_SKILLS_META_KEY = "seen_skills"

# A private agent skill has no scope-wide origin tag of its own (it belongs to one
# agent), so the inventory qualifies it with the owning agent's id — mirroring the
# ``agent@projekt`` display-key spirit, so a row's ``origins`` says which agent a
# private skill came from.
AGENT_SKILL_ORIGIN_TEMPLATE = "agent:{agent_id}"

# ---------------------------------------------------------------------------
# Injected inventory source (minimal Protocol — no service locator, no globals)
# ---------------------------------------------------------------------------


class SkillInventorySource(Protocol):
    """The current skill inventory the usage join runs against.

    Three name+origin views cover the scopes a report can encounter:
    ``global_skills`` for the shared bundled+global pool, ``agent_skill_names``
    for one agent's private home, and ``project_skills`` for one project's own
    skills. Names absent from every view are treated as deleted and their usage
    is dropped. Wired in the RPC layer over the runtime (see ``server/rpc``).
    """

    def global_skills(self) -> list[tuple[str, str | None]]: ...

    def agent_skill_names(self, agent_id: str) -> frozenset[str]: ...

    def project_skills(self, project_id: str) -> list[tuple[str, str | None]]: ...


# ---------------------------------------------------------------------------
# Report tree — frozen dataclasses with JSON-native field types only.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillByAgentCount:
    """Activations of one skill attributed to one agent display key."""

    key: str
    count: int


@dataclass(frozen=True)
class SkillUsageStat:
    """One current-inventory skill's offered/activated usage over the window."""

    name: str
    origins: list[str]
    offered_sessions: int
    activated_sessions: int
    usage_rate: float | None
    first_offered: str | None
    last_offered: str | None
    first_activated: str | None
    last_activated: str | None
    by_agent: list[SkillByAgentCount]


@dataclass(frozen=True)
class SkillsSection:
    """Skill-usage view joined against the current skill inventory."""

    total_skills: int
    used_skills: int
    never_used_skills: int
    skills: list[SkillUsageStat]


# ---------------------------------------------------------------------------
# Mutable per-skill accumulator used during the single scan
# ---------------------------------------------------------------------------


@dataclass
class _SkillUsageAcc:
    """Mutable per-skill-name usage tallied across every scanned session."""

    offered_sessions: int = 0
    activated_sessions: int = 0
    # Window-independent activation count, so a skill activated only outside the
    # window still reads as "used ever" (never-used flagging must not depend on
    # the window).
    activated_sessions_ever: int = 0
    first_offered: str | None = None
    last_offered: str | None = None
    first_activated: str | None = None
    last_activated: str | None = None
    by_agent: dict[str, int] = field(default_factory=dict)


class SkillUsageAccumulator:
    """Accumulate skill-usage facts over one statistics scan, then build the section.

    Fed once per session with the offered skill names (from ``seen_skills``), the
    activation-note names with their timestamps, the offering session's
    ``created_at``, and the agent display key. The window is applied here (not by
    the caller) so both the windowed figures and the window-independent
    never-used flag can be derived from the same feed.
    """

    def __init__(self, *, since: datetime | None, until: datetime | None) -> None:
        self._since = since
        self._until = until
        self._skills: dict[str, _SkillUsageAcc] = {}

    def observe_session(
        self,
        *,
        display_key: str,
        created_at: str | None,
        offered_names: list[str],
        activations: list[tuple[str, str | None]],
    ) -> None:
        """Fold one session's offered + activated skill facts into the tally.

        ``offered_names`` are the session's ``seen_skills`` (offered once per
        session, windowed by the session ``created_at``). ``activations`` are
        ``(skill_name, note_timestamp)`` pairs from the session's activation
        notes; a ``(session, skill)`` pair counts at most once, windowed by the
        note timestamp. Names are recorded raw here — the inventory join and the
        drop of deleted-skill usage happen at build time.
        """
        # Offered filters by the session's own start (``created_at``); the
        # seen_skills list carries no per-skill time.
        if self._in_window(created_at):
            for name in dict.fromkeys(offered_names):
                accumulator = self._skill(name)
                accumulator.offered_sessions += 1
                accumulator.first_offered = _min_timestamp(accumulator.first_offered, created_at)
                accumulator.last_offered = _max_timestamp(accumulator.last_offered, created_at)

        seen_in_window: set[str] = set()
        seen_ever: set[str] = set()
        for name, note_timestamp in activations:
            if name not in seen_ever:
                seen_ever.add(name)
                self._skill(name).activated_sessions_ever += 1
            # Activation filters by the note's own timestamp.
            if name in seen_in_window or not self._in_window(note_timestamp):
                continue
            seen_in_window.add(name)
            accumulator = self._skill(name)
            accumulator.activated_sessions += 1
            accumulator.by_agent[display_key] = accumulator.by_agent.get(display_key, 0) + 1
            accumulator.first_activated = _min_timestamp(
                accumulator.first_activated, note_timestamp
            )
            accumulator.last_activated = _max_timestamp(accumulator.last_activated, note_timestamp)

    def build(self, inventory: _ResolvedInventory) -> SkillsSection:
        """Join accumulated usage against the resolved inventory and build the section.

        One row per current-inventory skill (never-used inventory skills included
        with zero counts); usage observed for a name absent from the inventory is
        dropped. Rows are sorted by ``(-offered_sessions, name)``.
        """
        rows: list[SkillUsageStat] = []
        used = 0
        never_used = 0
        for name in sorted(inventory.names):
            accumulator = self._skills.get(name)
            offered = accumulator.offered_sessions if accumulator is not None else 0
            activated = accumulator.activated_sessions if accumulator is not None else 0
            activated_ever = accumulator.activated_sessions_ever if accumulator is not None else 0
            if activated:
                used += 1
            if not activated_ever:
                never_used += 1
            rows.append(
                SkillUsageStat(
                    name=name,
                    origins=inventory.origins_for(name),
                    offered_sessions=offered,
                    activated_sessions=activated,
                    usage_rate=(activated / offered) if offered else None,
                    first_offered=accumulator.first_offered if accumulator is not None else None,
                    last_offered=accumulator.last_offered if accumulator is not None else None,
                    first_activated=(
                        accumulator.first_activated if accumulator is not None else None
                    ),
                    last_activated=(
                        accumulator.last_activated if accumulator is not None else None
                    ),
                    by_agent=_by_agent_entries(
                        accumulator.by_agent if accumulator is not None else {}
                    ),
                )
            )
        rows.sort(key=lambda row: (-row.offered_sessions, row.name))
        return SkillsSection(
            total_skills=len(inventory.names),
            used_skills=used,
            never_used_skills=never_used,
            skills=rows,
        )

    def _skill(self, name: str) -> _SkillUsageAcc:
        accumulator = self._skills.get(name)
        if accumulator is None:
            accumulator = _SkillUsageAcc()
            self._skills[name] = accumulator
        return accumulator

    def _in_window(self, timestamp: str | None) -> bool:
        if self._since is None and self._until is None:
            return True
        parsed = parse_timestamp(timestamp) if timestamp is not None else None
        # An unparseable/absent timestamp cannot be excluded by a bound, matching
        # the statistics scan's lenient in-window rule.
        if parsed is None:
            return True
        if self._since is not None and parsed < self._since:
            return False
        return not (self._until is not None and parsed > self._until)


# ---------------------------------------------------------------------------
# Resolved inventory — the current skill set the usage join runs against
# ---------------------------------------------------------------------------


class _ResolvedInventory:
    """The de-duplicated current-inventory skill names with their origins.

    A skill name may be contributed by more than one scope (a global/project
    collision, or the same private skill seen across the agents that own it), so
    origins accumulate into a sorted, de-duplicated list per name — the row's
    visible signal that a name is ambiguous across scopes.
    """

    def __init__(self) -> None:
        self._origins: dict[str, set[str]] = {}

    def add(self, name: str, origin: str | None) -> None:
        origins = self._origins.setdefault(name, set())
        if origin is not None:
            origins.add(origin)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._origins)

    def origins_for(self, name: str) -> list[str]:
        return sorted(self._origins.get(name, set()))


def empty_inventory() -> _ResolvedInventory:
    """Return an inventory with no skills — every observed usage drops, zero counts.

    Used when no :class:`SkillInventorySource` is injected (existing service
    constructions and tests), so the report still carries a valid ``skills``
    section instead of failing.
    """
    return _ResolvedInventory()


def resolve_inventory(
    source: SkillInventorySource,
    *,
    agent_ids: frozenset[str],
    project_ids: frozenset[str],
) -> _ResolvedInventory:
    """Build the current-inventory name set from the injected source.

    Enumerates the global pool once, then every scanned agent's private home and
    every scanned project's own skills. ``agent_ids`` / ``project_ids`` are the
    bare scope ids the statistics scan actually saw, so the enumeration cost
    tracks the data, not the whole store. A private agent skill is tagged with
    the owning agent's id (the source reports only the bare names for a home).
    """
    inventory = _ResolvedInventory()
    for name, origin in source.global_skills():
        inventory.add(name, origin)
    for agent_id in agent_ids:
        agent_origin = AGENT_SKILL_ORIGIN_TEMPLATE.format(agent_id=agent_id)
        for name in source.agent_skill_names(agent_id):
            inventory.add(name, agent_origin)
    for project_id in project_ids:
        for name, origin in source.project_skills(project_id):
            inventory.add(name, origin)
    return inventory


def offered_skill_names(metadata: dict[str, object]) -> list[str]:
    """Return a session's offered skill names from its ``seen_skills`` metadata.

    The chat loop persists ``seen_skills`` as a sorted list of skill names.
    A session predating that metadata (or a malformed value) yields an empty
    list — such a session simply contributes no offered counts, with no legacy
    handling, matching the no-legacy-compat rule.
    """
    seen = metadata.get(SEEN_SKILLS_META_KEY)
    if not isinstance(seen, list):
        return []
    return [name for name in seen if isinstance(name, str) and name]


def _by_agent_entries(by_agent: dict[str, int]) -> list[SkillByAgentCount]:
    return [
        SkillByAgentCount(key=key, count=count)
        for key, count in sorted(by_agent.items(), key=lambda item: (-item[1], item[0]))
    ]


def _min_timestamp(current: str | None, candidate: str | None) -> str | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    current_parsed = parse_timestamp(current)
    candidate_parsed = parse_timestamp(candidate)
    if current_parsed is None:
        return candidate
    if candidate_parsed is None:
        return current
    return candidate if candidate_parsed < current_parsed else current


def _max_timestamp(current: str | None, candidate: str | None) -> str | None:
    if candidate is None:
        return current
    if current is None:
        return candidate
    current_parsed = parse_timestamp(current)
    candidate_parsed = parse_timestamp(candidate)
    if current_parsed is None:
        return candidate
    if candidate_parsed is None:
        return current
    return candidate if candidate_parsed > current_parsed else current
