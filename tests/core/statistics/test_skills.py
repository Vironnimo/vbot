"""Unit tests for the statistics skills-usage section.

Covers the accumulator and builder in isolation from the full scan: offered vs.
activated counting, the inventory join (deleted-skill drop, never-used listing),
window semantics (offered by session ``created_at``, activated by note
timestamp, never-used window-independent), per-agent attribution, origin
collisions, and safe handling of malformed offered metadata.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.statistics.skills import (
    SkillInventorySource,
    SkillsSection,
    SkillUsageAccumulator,
    empty_inventory,
    offered_skill_names,
    resolve_inventory,
)

BASE = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _iso(offset_seconds: int = 0) -> str:
    return (BASE + timedelta(seconds=offset_seconds)).isoformat()


class _FakeInventory:
    """Minimal :class:`SkillInventorySource` for the join tests."""

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


def _accumulator(
    *, since: datetime | None = None, until: datetime | None = None
) -> SkillUsageAccumulator:
    return SkillUsageAccumulator(since=since, until=until)


def _row(section: SkillsSection, name: str):
    return next(row for row in section.skills if row.name == name)


# -- offered_skill_names ----------------------------------------------------


def test_offered_skill_names_reads_seen_skills_list() -> None:
    assert offered_skill_names({"seen_skills": ["deploy", "teach"]}) == ["deploy", "teach"]


def test_offered_skill_names_empty_when_key_absent_or_malformed() -> None:
    assert offered_skill_names({}) == []
    assert offered_skill_names({"seen_skills": "deploy"}) == []
    assert offered_skill_names({"seen_skills": [1, "", "deploy", None]}) == ["deploy"]


# -- offered vs. activated counting -----------------------------------------


def test_offered_counted_from_seen_skills() -> None:
    accumulator = _accumulator()
    accumulator.observe_session(
        display_key="main",
        created_at=_iso(),
        offered_names=["deploy", "teach"],
        activations=[],
    )
    inventory = _FakeInventory(global_skills=[("deploy", "bundled"), ("teach", "bundled")])

    section = accumulator.build(
        resolve_inventory(inventory, agent_ids=frozenset(), project_ids=frozenset())
    )

    assert _row(section, "deploy").offered_sessions == 1
    assert _row(section, "deploy").activated_sessions == 0
    assert _row(section, "deploy").activated_offered_sessions == 0
    # Offered once, activated zero → rate 0.0 (null is reserved for offered == 0).
    assert _row(section, "deploy").usage_rate == 0.0
    assert _row(section, "deploy").first_offered == _iso()
    assert _row(section, "deploy").last_offered == _iso()


def test_offered_deduplicates_within_a_session() -> None:
    accumulator = _accumulator()
    accumulator.observe_session(
        display_key="main",
        created_at=_iso(),
        offered_names=["deploy", "deploy"],
        activations=[],
    )
    inventory = _FakeInventory(global_skills=[("deploy", "global")])

    section = accumulator.build(
        resolve_inventory(inventory, agent_ids=frozenset(), project_ids=frozenset())
    )

    assert _row(section, "deploy").offered_sessions == 1


def test_activated_counted_once_per_session_from_notes() -> None:
    accumulator = _accumulator()
    # Two activation notes for the same skill in one session — one activated session.
    accumulator.observe_session(
        display_key="main",
        created_at=_iso(),
        offered_names=["deploy"],
        activations=[("deploy", _iso(1)), ("deploy", _iso(2))],
    )
    accumulator.observe_session(
        display_key="main",
        created_at=_iso(10),
        offered_names=["deploy"],
        activations=[("deploy", _iso(11))],
    )
    inventory = _FakeInventory(global_skills=[("deploy", "global")])

    section = accumulator.build(
        resolve_inventory(inventory, agent_ids=frozenset(), project_ids=frozenset())
    )
    row = _row(section, "deploy")

    assert row.offered_sessions == 2
    assert row.activated_sessions == 2
    assert row.activated_offered_sessions == 2
    assert row.usage_rate == 1.0
    assert row.first_activated == _iso(1)
    assert row.last_activated == _iso(11)


def test_usage_rate_is_activation_among_sessions_with_offer_data() -> None:
    accumulator = _accumulator()
    for index in range(4):
        accumulator.observe_session(
            display_key="main",
            created_at=_iso(index * 10),
            offered_names=["deploy"],
            activations=[("deploy", _iso(index * 10 + 1))] if index < 1 else [],
        )
    inventory = _FakeInventory(global_skills=[("deploy", "global")])

    section = accumulator.build(
        resolve_inventory(inventory, agent_ids=frozenset(), project_ids=frozenset())
    )
    row = _row(section, "deploy")

    assert row.offered_sessions == 4
    assert row.activated_sessions == 1
    assert row.activated_offered_sessions == 1
    assert row.usage_rate == 0.25


def test_usage_rate_excludes_activations_without_offer_metadata() -> None:
    accumulator = _accumulator()
    accumulator.observe_session(
        display_key="main",
        created_at=_iso(),
        offered_names=["deploy"],
        activations=[],
    )
    accumulator.observe_session(
        display_key="main",
        created_at=_iso(10),
        offered_names=[],
        activations=[("deploy", _iso(11))],
    )
    inventory = _FakeInventory(global_skills=[("deploy", "global")])

    section = accumulator.build(
        resolve_inventory(inventory, agent_ids=frozenset(), project_ids=frozenset())
    )
    row = _row(section, "deploy")

    assert row.offered_sessions == 1
    assert row.activated_sessions == 1
    assert row.activated_offered_sessions == 0
    assert row.usage_rate == 0.0
    assert section.offered_unactivated_skills == 1
    assert section.skills_without_offer_data == 0


# -- inventory join ---------------------------------------------------------


def test_usage_for_name_absent_from_inventory_is_dropped() -> None:
    accumulator = _accumulator()
    accumulator.observe_session(
        display_key="main",
        created_at=_iso(),
        offered_names=["deleted-skill"],
        activations=[("deleted-skill", _iso(1))],
    )
    inventory = _FakeInventory(global_skills=[("deploy", "global")])

    section = accumulator.build(
        resolve_inventory(inventory, agent_ids=frozenset(), project_ids=frozenset())
    )

    assert {row.name for row in section.skills} == {"deploy"}
    assert section.total_skills == 1


def test_never_used_inventory_skill_listed_with_zero_counts() -> None:
    accumulator = _accumulator()
    accumulator.observe_session(
        display_key="main",
        created_at=_iso(),
        offered_names=["deploy"],
        activations=[("deploy", _iso(1))],
    )
    inventory = _FakeInventory(global_skills=[("deploy", "global"), ("unused", "bundled")])

    section = accumulator.build(
        resolve_inventory(inventory, agent_ids=frozenset(), project_ids=frozenset())
    )
    unused = _row(section, "unused")

    assert unused.offered_sessions == 0
    assert unused.activated_sessions == 0
    assert unused.activated_offered_sessions == 0
    assert unused.usage_rate is None
    assert unused.first_offered is None
    assert unused.first_activated is None
    assert unused.by_agent == []
    assert section.total_skills == 2
    assert section.used_skills == 1
    assert section.never_used_skills == 1
    assert section.offered_unactivated_skills == 0
    assert section.skills_without_offer_data == 1


def test_rows_sorted_by_offered_desc_then_name() -> None:
    accumulator = _accumulator()
    accumulator.observe_session(
        display_key="main", created_at=_iso(), offered_names=["b", "b-more"], activations=[]
    )
    accumulator.observe_session(
        display_key="main", created_at=_iso(10), offered_names=["b-more"], activations=[]
    )
    inventory = _FakeInventory(
        global_skills=[("a", "global"), ("b", "global"), ("b-more", "global")]
    )

    section = accumulator.build(
        resolve_inventory(inventory, agent_ids=frozenset(), project_ids=frozenset())
    )

    # b-more offered twice, b once, a zero → (-offered, name).
    assert [row.name for row in section.skills] == ["b-more", "b", "a"]


# -- per-agent attribution --------------------------------------------------


def test_activations_attributed_per_agent_display_key() -> None:
    accumulator = _accumulator()
    accumulator.observe_session(
        display_key="main",
        created_at=_iso(),
        offered_names=["deploy"],
        activations=[("deploy", _iso(1))],
    )
    accumulator.observe_session(
        display_key="builder@vbot",
        created_at=_iso(10),
        offered_names=["deploy"],
        activations=[("deploy", _iso(11))],
    )
    accumulator.observe_session(
        display_key="builder@vbot",
        created_at=_iso(20),
        offered_names=["deploy"],
        activations=[("deploy", _iso(21))],
    )
    inventory = _FakeInventory(global_skills=[("deploy", "global")])

    section = accumulator.build(
        resolve_inventory(inventory, agent_ids=frozenset(), project_ids=frozenset())
    )
    by_agent = {entry.key: entry.count for entry in _row(section, "deploy").by_agent}

    assert by_agent == {"builder@vbot": 2, "main": 1}
    # Sorted by count desc then key.
    assert [entry.key for entry in _row(section, "deploy").by_agent] == ["builder@vbot", "main"]


# -- window semantics -------------------------------------------------------


def test_offered_filtered_by_session_created_at() -> None:
    accumulator = _accumulator(since=BASE + timedelta(seconds=100))
    # Offered before the window → excluded.
    accumulator.observe_session(
        display_key="main", created_at=_iso(0), offered_names=["deploy"], activations=[]
    )
    # Offered inside the window → included.
    accumulator.observe_session(
        display_key="main", created_at=_iso(200), offered_names=["deploy"], activations=[]
    )
    inventory = _FakeInventory(global_skills=[("deploy", "global")])

    section = accumulator.build(
        resolve_inventory(inventory, agent_ids=frozenset(), project_ids=frozenset())
    )

    assert _row(section, "deploy").offered_sessions == 1
    assert _row(section, "deploy").first_offered == _iso(200)


def test_activation_filtered_by_note_timestamp() -> None:
    accumulator = _accumulator(since=BASE + timedelta(seconds=100))
    # Session created before window but a note fires inside it → activation counts.
    accumulator.observe_session(
        display_key="main",
        created_at=_iso(0),
        offered_names=["deploy"],
        activations=[("deploy", _iso(50)), ("deploy", _iso(200))],
    )
    inventory = _FakeInventory(global_skills=[("deploy", "global")])

    section = accumulator.build(
        resolve_inventory(inventory, agent_ids=frozenset(), project_ids=frozenset())
    )
    row = _row(section, "deploy")

    # Offered dropped (created_at before window), but the in-window note counts.
    assert row.offered_sessions == 0
    assert row.activated_sessions == 1
    assert row.activated_offered_sessions == 0
    assert row.usage_rate is None
    assert row.first_activated == _iso(200)


def test_never_used_is_window_independent() -> None:
    accumulator = _accumulator(since=BASE + timedelta(seconds=100))
    # Skill activated only OUTSIDE the window: in-window activated is 0 (not
    # "used"), but it has activations ever (not "never used").
    accumulator.observe_session(
        display_key="main",
        created_at=_iso(0),
        offered_names=["deploy"],
        activations=[("deploy", _iso(10))],
    )
    inventory = _FakeInventory(global_skills=[("deploy", "global"), ("fresh", "bundled")])

    section = accumulator.build(
        resolve_inventory(inventory, agent_ids=frozenset(), project_ids=frozenset())
    )

    assert _row(section, "deploy").activated_sessions == 0
    # deploy: activated outside window → not used-in-window, not never-used-ever.
    # fresh: never activated at all → never used.
    assert section.used_skills == 0
    assert section.never_used_skills == 1


# -- resolve_inventory & origins -------------------------------------------


def test_resolve_inventory_merges_scopes_and_tags_agent_and_project() -> None:
    inventory = _FakeInventory(
        global_skills=[("bundled-one", "bundled"), ("global-one", "global")],
        agent_skills={"assistant": frozenset({"private"})},
        project_skills={"vbot": [("proj", "project:vBot")]},
    )

    resolved = resolve_inventory(
        inventory, agent_ids=frozenset({"assistant"}), project_ids=frozenset({"vbot"})
    )

    assert resolved.names == frozenset({"bundled-one", "global-one", "private", "proj"})
    assert resolved.origins_for("private") == ["agent:assistant"]
    assert resolved.origins_for("proj") == ["project:vBot"]
    assert resolved.origins_for("bundled-one") == ["bundled"]


def test_name_collision_across_scopes_aggregates_origins() -> None:
    accumulator = _accumulator()
    accumulator.observe_session(
        display_key="main",
        created_at=_iso(),
        offered_names=["shared"],
        activations=[("shared", _iso(1))],
    )
    inventory = _FakeInventory(
        global_skills=[("shared", "global")],
        project_skills={"vbot": [("shared", "project:vBot")]},
    )

    section = accumulator.build(
        resolve_inventory(inventory, agent_ids=frozenset(), project_ids=frozenset({"vbot"}))
    )
    row = _row(section, "shared")

    # One aggregated row, both origins visible, single usage tally.
    assert len([r for r in section.skills if r.name == "shared"]) == 1
    assert row.origins == ["global", "project:vBot"]
    assert row.activated_sessions == 1


# -- empty inventory --------------------------------------------------------


def test_empty_inventory_drops_all_usage_with_zero_counts() -> None:
    accumulator = _accumulator()
    accumulator.observe_session(
        display_key="main",
        created_at=_iso(),
        offered_names=["deploy"],
        activations=[("deploy", _iso(1))],
    )

    section = accumulator.build(empty_inventory())

    assert section.skills == []
    assert section.total_skills == 0
    assert section.used_skills == 0
    assert section.never_used_skills == 0
    assert section.offered_unactivated_skills == 0
    assert section.skills_without_offer_data == 0


def test_fake_inventory_satisfies_protocol() -> None:
    inventory: SkillInventorySource = _FakeInventory()

    assert inventory.global_skills() == []
    assert inventory.agent_skill_names("x") == frozenset()
    assert inventory.project_skills("p") == []
