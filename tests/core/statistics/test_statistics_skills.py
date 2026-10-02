"""The Statistics Skills section: offered vs. activated, the inventory join and windows.

The accumulator and builder are tested directly for the counting rules; the
service tests cover the persisted inputs (seen Skills, activation notes) and scopes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import datetime, timedelta

import pytest

from core.chat.messages import ChatMessage
from core.runs import RunKind
from core.sessions import ChatSession, ChatSessionManager, SeenSkillsUpdate
from core.sessions._types import SKILL_CONTEXT_NOTE_PREFIX
from core.statistics import SkillInventorySource
from core.statistics.skills import (
    SkillsSection,
    SkillUsageAccumulator,
    SkillUsageStat,
    SkillUse,
    offered_skill_names,
    resolve_inventory,
)
from core.utils.timestamps import format_canonical_timestamp
from tests.core.statistics.statistics_test_support import BASE, StatisticsFactory


class _FakeInventory:
    """Minimal :class:`SkillInventorySource`."""

    def __init__(
        self,
        *,
        global_skills: list[tuple[str, str | None]] | None = None,
        agent_skills: dict[str, frozenset[str]] | None = None,
        project_skills: dict[str, list[tuple[str, str | None]]] | None = None,
    ) -> None:
        self._global = list(global_skills or [])
        self._agent = dict(agent_skills or {})
        self._project = dict(project_skills or {})

    def global_skills(self) -> list[tuple[str, str | None]]:
        return list(self._global)

    def agent_skill_names(self, agent_id: str) -> frozenset[str]:
        return self._agent.get(agent_id, frozenset())

    def project_skills(self, project_id: str) -> list[tuple[str, str | None]]:
        return list(self._project.get(project_id, []))


def _iso(offset_seconds: int = 0) -> str:
    return format_canonical_timestamp(BASE + timedelta(seconds=offset_seconds))


def _observe(
    accumulator: SkillUsageAccumulator,
    *,
    created: int = 0,
    offered: Sequence[str] = (),
    activated: Sequence[tuple[str, int]] = (),
    agent: str = "main",
) -> None:
    """Fold one Session started at second *created* with activations at their seconds."""
    accumulator.observe_session(
        display_key=agent,
        created_at=_iso(created),
        offered_names=list(offered),
        activations=[(name, _iso(offset)) for name, offset in activated],
    )


def _build(accumulator: SkillUsageAccumulator, *global_names: str) -> SkillsSection:
    inventory: SkillInventorySource = _FakeInventory(
        global_skills=[(name, "global") for name in global_names]
    )
    return accumulator.build(
        resolve_inventory(inventory, agent_ids=frozenset(), project_ids=frozenset())
    )


def _row(section: SkillsSection, name: str) -> SkillUsageStat:
    return next(row for row in section.skills if row.name == name)


# -- Counting rules -----------------------------------------------------------


@pytest.mark.parametrize(
    ("metadata", "names"),
    [
        ({"seen_skills": ["deploy", "teach"]}, ["deploy", "teach"]),
        ({}, []),
        ({"seen_skills": "deploy"}, []),
        ({"seen_skills": [1, "", "deploy", None]}, ["deploy"]),
    ],
    ids=["list", "absent", "not-a-list", "invalid-entries"],
)
def test_offered_skill_names_reads_only_named_entries_of_the_seen_skills_list(
    metadata: dict[str, object], names: list[str]
) -> None:
    assert offered_skill_names(metadata) == names


def test_offers_and_activations_count_once_per_session() -> None:
    accumulator = SkillUsageAccumulator(since=None, until=None)
    _observe(accumulator, created=0, offered=["deploy", "deploy"], activated=[("deploy", 1)] * 2)
    _observe(accumulator, created=10, offered=["deploy"], activated=[("deploy", 11)])

    row = _row(_build(accumulator, "deploy"), "deploy")

    assert (row.offered_sessions, row.activated_sessions, row.activated_offered_sessions) == (
        2,
        2,
        2,
    )
    assert row.usage_rate == 1.0
    assert (row.first_offered, row.last_offered) == (_iso(0), _iso(10))
    assert (row.first_activated, row.last_activated) == (_iso(1), _iso(11))


def test_usage_rate_converts_only_offers_that_the_same_session_activated() -> None:
    accumulator = SkillUsageAccumulator(since=None, until=None)
    _observe(accumulator, created=0, offered=["deploy"], activated=[("deploy", 1)])
    _observe(accumulator, created=10, offered=["deploy"])
    _observe(accumulator, created=20, offered=["deploy"])
    # An activation without offer metadata stays visible but cannot convert.
    _observe(accumulator, created=30, activated=[("deploy", 31)])
    _observe(accumulator, created=40, offered=["teach"])

    section = _build(accumulator, "deploy", "teach")
    deploy, teach = _row(section, "deploy"), _row(section, "teach")

    assert (deploy.offered_sessions, deploy.activated_sessions) == (3, 2)
    assert deploy.activated_offered_sessions == 1
    assert deploy.usage_rate == pytest.approx(1 / 3)
    # Offered but never activated is a zero rate; null is reserved for no offers.
    assert teach.usage_rate == 0.0
    assert section.offered_unactivated_skills == 1
    assert section.skills_without_offer_data == 0


def test_build_joins_the_current_inventory() -> None:
    accumulator = SkillUsageAccumulator(since=None, until=None)
    _observe(
        accumulator,
        offered=["deploy", "deleted-skill"],
        activated=[("deploy", 1), ("deleted-skill", 2)],
    )

    section = _build(accumulator, "deploy", "unused")
    unused = _row(section, "unused")

    # Usage of a name missing from the inventory is dropped; an unused
    # inventory Skill is listed with zero counts.
    assert {row.name for row in section.skills} == {"deploy", "unused"}
    assert (unused.offered_sessions, unused.activated_sessions) == (0, 0)
    assert unused.activated_offered_sessions == 0
    assert unused.usage_rate is None
    assert (unused.first_offered, unused.first_activated) == (None, None)
    assert unused.by_agent == []
    assert (section.total_skills, section.used_skills, section.never_used_skills) == (2, 1, 1)
    assert section.offered_unactivated_skills == 0
    assert section.skills_without_offer_data == 1


def test_rows_sorted_by_offered_desc_then_name() -> None:
    accumulator = SkillUsageAccumulator(since=None, until=None)
    _observe(accumulator, created=0, offered=["b", "b-more"])
    _observe(accumulator, created=10, offered=["b-more"])

    section = _build(accumulator, "a", "b", "b-more")

    assert [row.name for row in section.skills] == ["b-more", "b", "a"]


def test_activations_are_attributed_per_agent_display_key() -> None:
    accumulator = SkillUsageAccumulator(since=None, until=None)
    _observe(accumulator, created=0, offered=["deploy"], activated=[("deploy", 1)])
    for created in (10, 20):
        _observe(
            accumulator,
            created=created,
            offered=["deploy"],
            activated=[("deploy", created + 1)],
            agent="builder@vbot",
        )

    by_agent = _row(_build(accumulator, "deploy"), "deploy").by_agent

    # Sorted by count, then key.
    assert [(entry.key, entry.count) for entry in by_agent] == [("builder@vbot", 2), ("main", 1)]


def test_window_filters_offers_by_session_start_and_activations_by_note_time() -> None:
    accumulator = SkillUsageAccumulator(
        since=BASE + timedelta(seconds=100), until=BASE + timedelta(seconds=300)
    )
    # Started before the window; only the activation inside it counts.
    _observe(
        accumulator,
        created=0,
        offered=["deploy"],
        activated=[("deploy", 50), ("deploy", 200), ("deploy", 400)],
    )
    _observe(accumulator, created=200, offered=["teach"])
    # Activated only before the window: not used in it, but not "never used".
    _observe(accumulator, created=0, offered=["old"], activated=[("old", 10)])

    section = _build(accumulator, "deploy", "teach", "old", "fresh")
    deploy, teach = _row(section, "deploy"), _row(section, "teach")

    assert (deploy.offered_sessions, deploy.activated_sessions) == (0, 1)
    assert deploy.activated_offered_sessions == 0
    assert deploy.usage_rate is None
    assert (deploy.first_activated, deploy.last_activated) == (_iso(200), _iso(200))
    assert (teach.offered_sessions, teach.first_offered) == (1, _iso(200))
    assert _row(section, "old").activated_sessions == 0
    assert section.used_skills == 1
    # Never-used is window-independent: "teach" and "fresh" were never activated.
    assert section.never_used_skills == 2


def test_a_non_canonical_session_timestamp_is_bad_data() -> None:
    accumulator = SkillUsageAccumulator(since=BASE + timedelta(seconds=100), until=None)

    # Sessions store canonical timestamps only; an offset form is never guessed.
    with pytest.raises(ValueError, match="canonical"):
        accumulator.observe_session(
            display_key="main",
            created_at=BASE.isoformat(),
            offered_names=["deploy"],
            activations=[],
        )


def test_inventory_merges_scopes_and_one_row_aggregates_colliding_origins() -> None:
    inventory = _FakeInventory(
        global_skills=[("bundled-one", "bundled"), ("shared", "global")],
        agent_skills={"assistant": frozenset({"private"})},
        project_skills={"vbot": [("proj", "project:vBot"), ("shared", "project:vBot")]},
    )
    resolved = resolve_inventory(
        inventory, agent_ids=frozenset({"assistant"}), project_ids=frozenset({"vbot"})
    )
    accumulator = SkillUsageAccumulator(since=None, until=None)
    _observe(accumulator, offered=["shared"], activated=[("shared", 1)])

    section = accumulator.build(resolved)

    assert resolved.names == frozenset({"bundled-one", "shared", "private", "proj"})
    assert resolved.origins_for("private") == ["agent:assistant"]
    assert resolved.origins_for("proj") == ["project:vBot"]
    assert resolved.origins_for("bundled-one") == ["bundled"]
    [shared] = [row for row in section.skills if row.name == "shared"]
    assert shared.origins == ["global", "project:vBot"]
    assert shared.activated_sessions == 1


# -- Through the service ------------------------------------------------------


def _skill_note(name: str, at: datetime) -> ChatMessage:
    return ChatMessage.note(
        SKILL_CONTEXT_NOTE_PREFIX + json.dumps({"name": name, "content": f"{name} body"}),
        timestamp=at,
    )


def _offer_session(
    manager: ChatSessionManager,
    agent_id: str,
    offered: tuple[str, ...],
    notes: list[ChatMessage],
    *,
    project_id: str | None = None,
) -> ChatSession:
    session = manager.create(agent_id, project_id=project_id)
    for note in notes:
        session.append(note)
    manager.record_seen_skills(session.address, SeenSkillsUpdate(baseline=offered))
    return session


def _review_session(
    manager: ChatSessionManager,
    source: ChatSession,
    offered: tuple[str, ...],
    notes: list[ChatMessage],
) -> ChatSession:
    """A background reflection review: a fork whose only Run kind is unattended."""
    review = asyncio.run(manager.fork(source.address, run_kind=RunKind.SKILL_REFLECTION))
    for note in notes:
        review.append(note)
    manager.record_seen_skills(review.address, SeenSkillsUpdate(baseline=offered))
    return review


def test_report_joins_seen_skills_and_activation_notes_per_agent_key(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    _offer_session(manager, "main", ("deploy", "teach"), [_skill_note("deploy", BASE)])
    _offer_session(
        manager, "builder", ("deploy",), [_skill_note("deploy", BASE)], project_id="vbot"
    )
    # A broken activation note neither fails the report nor counts.
    broken = _offer_session(
        manager,
        "main",
        ("teach",),
        [ChatMessage.note(SKILL_CONTEXT_NOTE_PREFIX + "{broken", timestamp=BASE)],
    )
    # A background review offers and activates Skills without counting as use.
    _review_session(manager, broken, ("deploy", "teach"), [_skill_note("teach", BASE)])
    inventory = _FakeInventory(global_skills=[("deploy", "bundled"), ("teach", "global")])

    report = statistics(
        ["main"], projects={"vbot": ["builder"]}, skill_inventory=inventory
    ).report()

    skills = report.skills
    deploy = next(row for row in skills.skills if row.name == "deploy")
    teach = next(row for row in skills.skills if row.name == "teach")
    assert (deploy.offered_sessions, deploy.activated_sessions) == (2, 2)
    assert deploy.usage_rate == 1.0
    # A project Agent is keyed by its address form.
    assert [(entry.key, entry.count) for entry in deploy.by_agent] == [
        ("builder@vbot", 1),
        ("main", 1),
    ]
    assert (teach.offered_sessions, teach.activated_sessions) == (2, 0)
    assert (skills.total_skills, skills.used_skills, skills.never_used_skills) == (2, 1, 1)
    assert skills.offered_unactivated_skills == 1


def test_report_window_filters_offers_by_session_start_and_activations_by_note_time(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    # The Session starts now, after the window; its activation note lies inside it.
    _offer_session(manager, "main", ("deploy",), [_skill_note("deploy", BASE)])
    inventory = _FakeInventory(global_skills=[("deploy", "bundled")])

    report = statistics(skill_inventory=inventory).report(
        since=BASE - timedelta(hours=1), until=BASE + timedelta(hours=1)
    )

    [deploy] = report.skills.skills
    assert (deploy.offered_sessions, deploy.activated_sessions) == (0, 1)
    assert deploy.usage_rate is None


def test_skill_usage_returns_each_agents_last_activation_and_count(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    later = BASE + timedelta(hours=1)
    _offer_session(manager, "main", ("deploy",), [_skill_note("deploy", later)])
    first = _offer_session(manager, "main", (), [_skill_note("deploy", BASE)])
    _offer_session(manager, "builder", (), [_skill_note("deploy", BASE)], project_id="vbot")
    # Background reviews are not use, even with the latest activation.
    _review_session(manager, first, (), [_skill_note("deploy", later + timedelta(hours=1))])
    _review_session(manager, first, (), [_skill_note("teach", later)])
    # A review Session that also ran an attended Run counts.
    mixed = _review_session(manager, first, (), [_skill_note("review", BASE)])
    mixed.start_run("run-user")
    service = statistics(["main", "builder"], projects={"vbot": ["builder"]})

    usage = service.skill_usage()

    assert usage == {
        ("main", "deploy"): SkillUse(last_activated=format_canonical_timestamp(later), count=2),
        ("main", "review"): SkillUse(last_activated=format_canonical_timestamp(BASE), count=1),
        ("builder", "deploy"): SkillUse(last_activated=format_canonical_timestamp(BASE), count=1),
    }
    # A deleted Session no longer counts; the query refreshes the projection.
    manager.delete(first.address)
    assert asyncio.run(service.skill_usage_async())[("main", "deploy")].count == 1


def test_service_without_an_inventory_reports_an_empty_skills_section(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    _offer_session(manager, "main", ("deploy",), [_skill_note("deploy", BASE)])

    report = statistics().report()

    assert report.skills.skills == []
    assert report.skills.total_skills == 0
    assert report.skills.offered_unactivated_skills == 0
    assert report.skills.skills_without_offer_data == 0
    assert json.loads(json.dumps(report.to_dict()))["skills"]["total_skills"] == 0
