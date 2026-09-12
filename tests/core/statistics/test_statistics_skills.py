"""Tests for statistics skills."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

from core.chat.messages import ChatMessage
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions._types import SKILL_CONTEXT_NOTE_PREFIX
from core.statistics import (
    AgentDirectory,
    ProjectDirectory,
    SkillInventorySource,
    StatisticsReport,
    StatisticsService,
)
from tests.core.statistics.statistics_test_support import (
    BASE,
    _FakeAgents,
    _FakeProjects,
)


# ---------------------------------------------------------------------------
# Skills section — end-to-end through the service (offered from seen_skills
# metadata, activated from persisted notes, joined against an injected inventory).
# ---------------------------------------------------------------------------
class _FakeInventory:
    """Minimal :class:`SkillInventorySource` for the service-level skills tests."""

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


def _skill_note(name: str, at: datetime) -> ChatMessage:
    return ChatMessage.note(
        SKILL_CONTEXT_NOTE_PREFIX + json.dumps({"name": name, "content": f"{name} body"}),
        timestamp=at,
    )


def _skills_row(report: StatisticsReport, name: str):
    return next(row for row in report.skills.skills if row.name == name)


def test_skills_offered_from_metadata_and_activated_from_notes(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    inventory = _FakeInventory(global_skills=[("deploy", "bundled"), ("teach", "global")])
    service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents(["main"])),
        skill_inventory=cast(SkillInventorySource, inventory),
    )
    session = manager.create("main")
    session.append(ChatMessage.user("hi", timestamp=BASE))
    session.append(_skill_note("deploy", BASE + timedelta(seconds=1)))
    manager.set_metadata(
        SessionAddress(project_id=None, agent_id="main", session_id=session.id),
        {"seen_skills": ["deploy", "teach"]},
    )

    report = service.report()

    deploy = _skills_row(report, "deploy")
    teach = _skills_row(report, "teach")
    assert deploy.offered_sessions == 1
    assert deploy.activated_sessions == 1
    assert deploy.activated_offered_sessions == 1
    assert deploy.usage_rate == 1.0
    assert deploy.by_agent == [type(deploy.by_agent[0])(key="main", count=1)]
    # teach was offered but never activated.
    assert teach.offered_sessions == 1
    assert teach.activated_sessions == 0
    assert report.skills.total_skills == 2
    assert report.skills.used_skills == 1
    assert report.skills.never_used_skills == 1
    assert report.skills.offered_unactivated_skills == 1
    assert report.skills.skills_without_offer_data == 0


def test_skills_usage_for_deleted_name_is_dropped(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    # Inventory holds only "deploy"; the session used a now-deleted "legacy" skill.
    inventory = _FakeInventory(global_skills=[("deploy", "bundled")])
    service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents(["main"])),
        skill_inventory=cast(SkillInventorySource, inventory),
    )
    session = manager.create("main")
    session.append(_skill_note("legacy", BASE + timedelta(seconds=1)))
    manager.set_metadata(
        SessionAddress(project_id=None, agent_id="main", session_id=session.id),
        {"seen_skills": ["legacy", "deploy"]},
    )

    report = service.report()

    assert {row.name for row in report.skills.skills} == {"deploy"}
    assert _skills_row(report, "deploy").offered_sessions == 1
    assert _skills_row(report, "deploy").activated_sessions == 0


def test_skills_default_service_has_empty_section(tmp_path: Path) -> None:
    # No injected inventory → every usage drops, zero counts, valid section.
    manager = ChatSessionManager(tmp_path)
    service = StatisticsService(manager, cast(AgentDirectory, _FakeAgents(["main"])))
    session = manager.create("main")
    session.append(_skill_note("deploy", BASE))
    manager.set_metadata(
        SessionAddress(project_id=None, agent_id="main", session_id=session.id),
        {"seen_skills": ["deploy"]},
    )

    report = service.report()

    assert report.skills.skills == []
    assert report.skills.total_skills == 0
    assert report.skills.offered_unactivated_skills == 0
    assert report.skills.skills_without_offer_data == 0
    # Fully JSON-serializable with the skills section present.
    assert json.loads(json.dumps(report.to_dict()))["skills"]["total_skills"] == 0


def test_skills_project_agent_keyed_by_address_form(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    inventory = _FakeInventory(global_skills=[("deploy", "bundled")])
    service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents([])),
        cast(ProjectDirectory, _FakeProjects({"vbot": ["builder"]})),
        skill_inventory=cast(SkillInventorySource, inventory),
    )
    session = manager.create("builder", project_id="vbot")
    session.append(_skill_note("deploy", BASE + timedelta(seconds=1)))
    manager.set_metadata(
        SessionAddress(project_id="vbot", agent_id="builder", session_id=session.id),
        {"seen_skills": ["deploy"]},
    )

    report = service.report()
    deploy = _skills_row(report, "deploy")

    assert [entry.key for entry in deploy.by_agent] == ["builder@vbot"]
    assert deploy.by_agent[0].count == 1


def test_skills_window_filters_offered_and_activated(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    inventory = _FakeInventory(global_skills=[("deploy", "bundled")])
    service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents(["main"])),
        skill_inventory=cast(SkillInventorySource, inventory),
    )
    # Session created (first message) before the window; an activation note fires
    # inside it. Offered filters by created_at (excluded); activated by note
    # timestamp (included) — and never-used is window-independent.
    address = SessionAddress(project_id=None, agent_id="main", session_id="windowed")
    manager._store.create(address, created_at=BASE.isoformat())
    session = manager.get(address)
    session.append(ChatMessage.user("hi", timestamp=BASE))
    session.append(_skill_note("deploy", BASE + timedelta(hours=2)))
    manager.set_metadata(
        SessionAddress(project_id=None, agent_id="main", session_id=session.id),
        {"seen_skills": ["deploy"]},
    )

    report = service.report(since=BASE + timedelta(hours=1))
    deploy = _skills_row(report, "deploy")

    assert deploy.offered_sessions == 0
    assert deploy.activated_sessions == 1
    assert deploy.activated_offered_sessions == 0
    assert deploy.usage_rate is None
    assert report.skills.never_used_skills == 0


def test_skills_malformed_skill_context_note_is_ignored(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    inventory = _FakeInventory(global_skills=[("deploy", "bundled")])
    service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents(["main"])),
        skill_inventory=cast(SkillInventorySource, inventory),
    )
    session = manager.create("main")
    # A [skill-context] note with a broken JSON payload must not crash the scan
    # nor count as an activation.
    session.append(ChatMessage.note(SKILL_CONTEXT_NOTE_PREFIX + "{broken", timestamp=BASE))
    manager.set_metadata(
        SessionAddress(project_id=None, agent_id="main", session_id=session.id),
        {"seen_skills": ["deploy"]},
    )

    report = service.report()

    assert _skills_row(report, "deploy").activated_sessions == 0
    assert _skills_row(report, "deploy").offered_sessions == 1
