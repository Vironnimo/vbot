"""Project scan and Identity-Agent resolution tests."""

from .resolver_test_support import (
    AgentResolutionError,
    AgentStore,
    ConfigAgent,
    FindingType,
    Path,
    ProjectStore,
    _openai_configured,
    _project,
    _resolver,
    _write_agent,
    pytest,
)
from .resolver_test_support import agents as agents
from .resolver_test_support import data_dir as data_dir
from .resolver_test_support import projects as projects
from .resolver_test_support import repo as repo
from .resolver_test_support import template_dir as template_dir


def test_scan_reports_unconfigured_model_as_bad_model_finding(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange
    _write_agent(repo, "builder.md", model="openai/ghost-model")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    # Act
    result = resolver.scan_project_report(project)

    # Assert
    bad = result.report.findings_of(FindingType.BAD_MODEL)
    assert len(bad) == 1
    assert bad[0].agent_id == "builder"
    assert bad[0].source_path is not None


def test_scan_does_not_flag_agent_without_declared_model(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange: no declared model legitimately inherits a default — not a finding.
    _write_agent(repo, "writer.md", model="")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    # Act
    result = resolver.scan_project_report(project)

    # Assert
    assert result.report.findings_of(FindingType.BAD_MODEL) == ()


def test_scan_reports_configured_model_clean(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    # Act
    result = resolver.scan_project_report(project)

    # Assert
    assert result.report.is_clean
    assert [member.agent_id for member in result.team] == ["builder"]


def test_scan_honors_project_source_format(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange: agents in both formats; the project declares claude as its format.
    _write_agent(repo, "builder.md")
    claude_dir = repo / ".claude" / "agents"
    claude_dir.mkdir(parents=True)
    (claude_dir / "reviewer.md").write_text(
        "---\nname: reviewer\ndescription: Reviews.\n---\nBody.\n", encoding="utf-8"
    )
    project = projects.create("vbot", "vBot", repo, source_format="claude")
    resolver = _resolver(agents, projects, _openai_configured())

    # Act
    result = resolver.scan_project_report(project)

    # Assert: only the claude member is on the team — no mixing.
    assert [member.agent_id for member in result.team] == ["reviewer"]
    assert result.team[0].source_format == "claude"


def test_scan_reports_orphan_default_agent(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange: the anchor points at a default agent the scan does not produce.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    project = projects.update("vbot", default_agent="ghost")
    resolver = _resolver(agents, projects, _openai_configured())

    # Act
    result = resolver.scan_project_report(project)

    # Assert: pointer-origin findings carry the pointer's id and no source file.
    orphans = result.report.findings_of(FindingType.ORPHAN)
    assert len(orphans) == 1
    assert orphans[0].agent_id == "ghost"
    assert orphans[0].source_path is None


def test_scan_default_agent_on_team_is_not_orphan(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    project = projects.update("vbot", default_agent="builder")
    resolver = _resolver(agents, projects, _openai_configured())

    # Act
    result = resolver.scan_project_report(project)

    # Assert
    assert result.report.findings_of(FindingType.ORPHAN) == ()


def test_scan_reports_orphan_session_owner(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange: sessions under the anchor for an agent the scan no longer yields
    # (renamed/deleted in the repo) — and for one still on the team (no finding).
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    for owner in ("builder", "ghost"):
        sessions_dir = projects.sessions_dir("vbot", owner)
        sessions_dir.mkdir(parents=True)
        (sessions_dir / "session-1.jsonl").write_text("{}\n", encoding="utf-8")
    resolver = _resolver(agents, projects, _openai_configured())

    # Act
    result = resolver.scan_project_report(project)

    # Assert: exactly the vanished session owner is flagged.
    orphans = result.report.findings_of(FindingType.ORPHAN)
    assert [finding.agent_id for finding in orphans] == ["ghost"]


def test_identity_resolution_returns_store_agent_unchanged(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange
    created = agents.create("orchestrator", "Orchestrator", model="openai/gpt-5.2")
    resolver = _resolver(agents, projects, _openai_configured())

    # Act
    resolved = resolver.resolve_agent(None, "orchestrator")

    # Assert: byte-for-byte the store agent (same object contract as today).
    assert resolved == created
    assert resolved.workspace == created.workspace
    assert resolved.model == "openai/gpt-5.2"


def test_identity_resolution_unknown_agent_raises(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    resolver = _resolver(agents, projects, _openai_configured())

    with pytest.raises(AgentResolutionError):
        resolver.resolve_agent(None, "missing-agent")


def test_identity_wildcard_keeps_global_and_cross_project_reach(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    created = agents.create("orchestrator", "Orchestrator", allowed_agents=["*"])
    agents.create("worker", "Worker")
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    resolved = resolver.resolve_agent(None, "orchestrator")

    assert resolved == created
    assert resolved.allowed_agents == ["*"]


def test_identity_explicit_targets_filter_missing_addresses_live(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    agents.create("worker", "Worker")
    agents.create(
        "orchestrator",
        "Orchestrator",
        allowed_agents=["worker", "missing", "builder@vbot", "ghost@vbot", "bad@address@x"],
    )
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    resolved = resolver.resolve_agent(None, "orchestrator")

    assert resolved.allowed_agents == ["worker", "builder@vbot"]


def test_single_agent_config_is_read_fresh_per_resolve(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange: open-time scan caches the Team; then the repo file changes model.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2", body="v1")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())
    resolver.rescan_project(project)  # caches the Team at open time

    # Mutate the repo file after the Team scan.
    _write_agent(repo, "builder.md", model="openai/gpt-mini", body="v2")

    # Act
    runtime_agent = resolver.resolve_agent(project.project_id, "builder")

    # Assert: config (model + body) reflects the live file, not the cached scan.
    assert isinstance(runtime_agent, ConfigAgent)
    assert runtime_agent.model == "openai/gpt-mini"
    assert runtime_agent.body == "v2\n"


def test_team_membership_uses_cache_not_live_new_file(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange: Team is scanned/cached with one agent.
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())
    resolver.rescan_project(project)

    # A new agent file appears in the repo *after* the open-time scan.
    _write_agent(repo, "planner.md", model="openai/gpt-5.2")

    # Act / Assert: the new agent is not on the cached Team until a re-scan.
    with pytest.raises(AgentResolutionError):
        resolver.resolve_agent(project.project_id, "planner")

    # After an explicit re-scan, the Team includes the new member.
    resolver.rescan_project(project)
    resolved = resolver.resolve_agent(project.project_id, "planner")
    assert resolved.id == "planner"


def test_resolve_unknown_project_agent_raises(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())

    with pytest.raises(AgentResolutionError):
        resolver.resolve_agent(project.project_id, "ghost")
