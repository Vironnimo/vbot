"""Tests for statistics projects."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import cast

from core.chat.messages import ChatMessage
from core.sessions import ChatSessionManager
from core.statistics import (
    AgentDirectory,
    ProjectDirectory,
    StatisticsService,
)
from tests.core.statistics.statistics_test_support import (
    BASE,
    _assistant,
    _FakeAgents,
    _FakeProjects,
    _run_summary,
    _write_session,
)


def _write_project_session(
    manager: ChatSessionManager,
    agent_id: str,
    project_id: str,
    messages: list[ChatMessage],
    *,
    session_id: str | None = None,
) -> str:
    session = manager.create(agent_id, session_id=session_id, project_id=project_id)
    for message in messages:
        session.append(message)
    return session.id


def test_project_session_appears_under_address_form(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    model = "openai/gpt-5"
    _write_project_session(
        manager,
        "builder",
        "vbot",
        [
            _assistant(model=model, at=BASE),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=1), duration_ms=400, run_id="p1"
            ),
        ],
    )
    service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents([])),
        cast(ProjectDirectory, _FakeProjects({"vbot": ["builder"]})),
    )

    report = service.report()

    # The project agent is keyed by its outer address form, distinct from a bare id.
    assert report.overview.total_agents == 1
    assert report.overview.agents[0].agent_id == "builder@vbot"
    assert report.overview.total_runs == 1
    assert {entry.agent_id for entry in report.runs.runs_per_agent} == {"builder@vbot"}


def test_identity_and_project_agents_coexist_distinctly(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    model = "openai/gpt-5"
    # Same bare agent id "builder" both as an identity agent and inside a project.
    _write_session(
        manager,
        "builder",
        [
            _assistant(model=model, at=BASE),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=1), duration_ms=100, run_id="i1"
            ),
        ],
    )
    _write_project_session(
        manager,
        "builder",
        "vbot",
        [
            _assistant(model=model, at=BASE + timedelta(seconds=2)),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=3), duration_ms=200, run_id="p1"
            ),
        ],
    )
    service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents(["builder"])),
        cast(ProjectDirectory, _FakeProjects({"vbot": ["builder"]})),
    )

    report = service.report()

    per_agent = {entry.agent_id: entry.runs for entry in report.runs.runs_per_agent}
    assert per_agent == {"builder": 1, "builder@vbot": 1}
    assert report.overview.total_agents == 2
    assert report.overview.total_runs == 2


def test_no_projects_report_matches_identity_only_scan(tmp_path: Path) -> None:
    # Same data dir, same files: an empty project source must not change a single
    # figure versus the identity-only service (no double counting, no new keys).
    manager = ChatSessionManager(tmp_path)
    _write_session(
        manager,
        "main",
        [
            _assistant(
                model="openai/gpt-5", at=BASE, usage={"input_tokens": 10, "output_tokens": 2}
            ),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=1), duration_ms=300, run_id="r1"
            ),
        ],
    )

    baseline = StatisticsService(manager, cast(AgentDirectory, _FakeAgents(["main"])))
    with_projects = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents(["main"])),
        cast(ProjectDirectory, _FakeProjects({})),
    )

    baseline_report = baseline.report().to_dict()
    project_report = with_projects.report().to_dict()
    # generated_at is a wall-clock field — compare everything else.
    baseline_report.pop("generated_at")
    project_report.pop("generated_at")
    assert project_report == baseline_report


def test_same_session_id_across_scopes_is_not_double_counted(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    model = "openai/gpt-5"
    shared_id = "shared-session"
    # One identity session and one project session deliberately share a session id;
    # they are different files under different anchors and must both count once.
    _write_project_session(
        manager,
        "builder",
        "vbot",
        [
            _assistant(model=model, at=BASE),
            _run_summary(
                status="completed", at=BASE + timedelta(seconds=1), duration_ms=100, run_id="p1"
            ),
        ],
        session_id=shared_id,
    )
    identity_session = manager.create("builder", session_id=shared_id)
    for message in [
        _assistant(model=model, at=BASE + timedelta(seconds=2)),
        _run_summary(
            status="completed", at=BASE + timedelta(seconds=3), duration_ms=100, run_id="i1"
        ),
    ]:
        identity_session.append(message)

    service = StatisticsService(
        manager,
        cast(AgentDirectory, _FakeAgents(["builder"])),
        cast(ProjectDirectory, _FakeProjects({"vbot": ["builder"]})),
    )

    report = service.report()

    # Two distinct files → two sessions, two runs, no collision/double count.
    assert report.overview.total_sessions == 2
    assert report.overview.total_runs == 2
    per_agent = {entry.agent_id: entry.runs for entry in report.runs.runs_per_agent}
    assert per_agent == {"builder": 1, "builder@vbot": 1}
