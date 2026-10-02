"""Project scan, Identity-Agent resolution, and working-Project scope tests."""

import threading
from types import SimpleNamespace
from typing import Any

from core.database import DatabaseUnavailableError
from core.sessions import ChatSessionManager, SessionAddress

from .resolver_test_support import (
    AgentStore,
    ConfigAgent,
    FindingType,
    Path,
    ProjectStore,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
    _openai_configured,
    _project,
    _resolver,
    _stub_project,
    _write_agent,
    pytest,
    resolve_prompt_project,
    resolve_skill_scope,
)
from .resolver_test_support import agents as agents
from .resolver_test_support import data_dir as data_dir
from .resolver_test_support import projects as projects
from .resolver_test_support import repo as repo
from .resolver_test_support import template_dir as template_dir


@pytest.mark.parametrize(
    ("model", "default_agent", "findings"),
    [
        pytest.param("openai/gpt-5.2", "", [], id="clean"),
        pytest.param(
            "openai/ghost-model", "", [(FindingType.BAD_MODEL, "builder", True)], id="bad-model"
        ),
        # No declared Model legitimately inherits a default.
        pytest.param("", "", [], id="no-declared-model"),
        # Pointer findings carry the pointer's id and no source file.
        pytest.param(
            "openai/gpt-5.2", "ghost", [(FindingType.ORPHAN, "ghost", False)], id="orphan-default"
        ),
        pytest.param("openai/gpt-5.2", "builder", [], id="default-on-team"),
    ],
)
def test_scan_reports_model_and_default_agent_findings(
    agents: AgentStore,
    projects: ProjectStore,
    repo: Path,
    model: str,
    default_agent: str,
    findings: list[tuple[FindingType, str, bool]],
) -> None:
    _write_agent(repo, "builder.md", model=model)
    _project(projects, repo)
    project = projects.update("vbot", default_agent=default_agent)
    resolver = _resolver(agents, projects, _openai_configured())

    result = resolver.scan_project_report(project)

    assert [
        (finding.type, finding.agent_id, finding.source_path is not None)
        for finding in result.report.findings
    ] == findings
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


def test_scan_reports_orphan_session_owner(agents: AgentStore, repo: Path, data_dir: Path) -> None:
    # Arrange: sessions under the anchor for an agent the scan no longer yields
    # (renamed/deleted in the repo) — and for one still on the team (no finding).
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    from core.sessions import ChatSessionManager

    sessions = ChatSessionManager(data_dir)
    projects = ProjectStore(data_dir, sessions=sessions)
    project = _project(projects, repo)
    for owner in ("builder", "ghost"):
        sessions.create(owner, session_id="session-1", project_id="vbot")
    resolver = _resolver(agents, projects, _openai_configured())

    # Act
    result = resolver.scan_project_report(project)

    # Assert: exactly the vanished session owner is flagged.
    orphans = result.report.findings_of(FindingType.ORPHAN)
    assert [finding.agent_id for finding in orphans] == ["ghost"]
    sessions.close()


@pytest.mark.parametrize(
    "tools",
    [
        # No Subagent settings keep the wildcard: global and cross-Project reach.
        pytest.param({}, id="wildcard"),
        # Explicit targets stay verbatim, including ones that do not resolve now.
        pytest.param(
            {"subagent": {"allowed_agents": ["worker", "missing", "builder@vbot", "ghost@vbot"]}},
            id="explicit-targets",
        ),
    ],
)
def test_identity_resolution_returns_the_store_agent_unchanged(
    agents: AgentStore, projects: ProjectStore, repo: Path, tools: dict[str, Any]
) -> None:
    agents.create("worker", "Worker")
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    agents.create("orchestrator", "Orchestrator", model="openai/gpt-5.2", tools=tools)
    created = agents.update("orchestrator", root_project_id=project.project_id)
    resolver = _resolver(agents, projects, _openai_configured())

    resolved = resolver.resolve_agent(None, "orchestrator")

    assert resolved == created
    assert resolved.tools == tools
    assert resolved.root_project_id == "vbot"


@pytest.mark.asyncio
async def test_async_resolution_runs_each_agent_kind_on_its_pool(
    agents: AgentStore, projects: ProjectStore, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    created = agents.create("orchestrator", "Orchestrator", model="openai/gpt-5.2")
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())
    threads: dict[str, str] = {}
    store_get = agents.get
    read_fresh = resolver._read_agent_fresh
    apply_overrides = resolver._apply_overrides

    def recording_get(agent_id: str) -> Any:
        threads["identity"] = threading.current_thread().name
        return store_get(agent_id)

    def recording_read(*args: Any) -> Any:
        threads["project"] = threading.current_thread().name
        return read_fresh(*args)

    def recording_overrides(*args: Any) -> Any:
        threads["overrides"] = threading.current_thread().name
        return apply_overrides(*args)

    monkeypatch.setattr(agents, "get", recording_get)
    monkeypatch.setattr(resolver, "_read_agent_fresh", recording_read)
    monkeypatch.setattr(resolver, "_apply_overrides", recording_overrides)

    # Act
    resolved = await resolver.resolve_agent_async(None, "orchestrator")
    session = agents._session_manager().create("orchestrator")
    resolver.update_session_overrides(
        SessionAddress(None, "orchestrator", session.id), {"thinking_effort": "high"}
    )
    overridden = await resolver.resolve_agent_async(None, "orchestrator", session_id=session.id)
    member = await resolver.resolve_agent_async(project.project_id, "builder")

    # Assert: the ordinary results. The Identity read, which verifies the
    # current-Session pointer, runs on the Session database's pool; the Model
    # check of Session overrides and Project resolution on the resolution pool.
    assert resolved == created
    assert overridden.thinking_effort == "high"
    assert isinstance(member, ConfigAgent)
    assert threads["identity"].startswith("vbot-db-sessions")
    assert threads["overrides"].startswith("vbot-agent-resolution")
    assert threads["project"].startswith("vbot-agent-resolution")
    with pytest.raises(ResolutionAgentNotFoundError):
        await resolver.resolve_agent_async(None, "missing-agent")


@pytest.mark.asyncio
async def test_async_resolution_on_a_closed_session_database_fails_cleanly(
    data_dir: Path, template_dir: Path, projects: ProjectStore, repo: Path
) -> None:
    # Arrange: a Runtime-shaped store that uses the injected Session service.
    sessions = ChatSessionManager(data_dir)
    agents = AgentStore(data_dir, template_dir=template_dir, sessions=sessions)
    agents.create("orchestrator", "Orchestrator", model="openai/gpt-5.2")
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())
    resolver.rescan_project(project)
    sessions.close()

    # Act / Assert: the Identity read fails as unavailable; a Project Agent whose
    # Team is cached needs no Session database and still resolves.
    with pytest.raises(DatabaseUnavailableError):
        await resolver.resolve_agent_async(None, "orchestrator")
    member = await resolver.resolve_agent_async(project.project_id, "builder")
    assert isinstance(member, ConfigAgent)


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


def test_cached_member_whose_source_vanished_is_not_found(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, _openai_configured())
    resolver.rescan_project(project)

    next(repo.rglob("builder.md")).unlink()

    with pytest.raises(ResolutionAgentNotFoundError):
        resolver.resolve_agent(project.project_id, "builder")


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
    with pytest.raises(ResolutionAgentNotFoundError):
        resolver.resolve_agent(project.project_id, "planner")

    # After an explicit re-scan, the Team includes the new member.
    resolver.rescan_project(project)
    resolved = resolver.resolve_agent(project.project_id, "planner")
    assert resolved.id == "planner"


def test_case_variant_addresses_resolve_as_unknown(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Ids are exact. On a case-insensitive filesystem ``VBot`` and ``MAIN`` open the
    # stored ``vbot``/``main`` trees; resolution must still treat them as unknown
    # and name the missing resource precisely.
    from core.agents import AgentNotFoundError
    from core.projects import ProjectNotFoundError, parse_agent_address

    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    _project(projects, repo)
    agents.create("main", "Main")
    resolver = _resolver(agents, projects, _openai_configured())
    agent_id, project_id = parse_agent_address("Builder@VBot")

    with pytest.raises(ResolutionProjectNotFoundError) as project_error:
        resolver.resolve_agent(project_id, agent_id)
    with pytest.raises(ResolutionAgentNotFoundError) as identity_error:
        resolver.resolve_agent(None, "MAIN")
    with pytest.raises(ResolutionAgentNotFoundError):
        resolver.resolve_agent("vbot", "Builder")

    assert isinstance(project_error.value.__cause__, ProjectNotFoundError)
    assert isinstance(identity_error.value.__cause__, AgentNotFoundError)
    assert resolver.resolve_agent("vbot", "builder").id == "builder"


def test_resolve_prompt_project_uses_only_the_explicit_project(
    projects: ProjectStore, repo: Path
) -> None:
    projects.create("vbot", "vBot", repo)

    resolved = resolve_prompt_project(projects, "vbot")

    assert resolved is not None
    assert resolved.project_id == "vbot"
    # Workspace equality does not select a Project.
    assert resolve_prompt_project(projects, None) is None


@pytest.mark.parametrize(
    ("project_id", "prompt_project", "agent", "scope"),
    [
        # A Project Run never carries an Identity layer: a Team slug colliding with an
        # Identity Agent's id must not pull that Agent's private Skills in.
        pytest.param(
            "vbot", "vbot", SimpleNamespace(id="builder"), ("vbot", None), id="project-run"
        ),
        # A rooted Identity Run sees its home Project's Skills plus its private layer.
        pytest.param(
            None, "vbot", SimpleNamespace(id="main"), ("vbot", "main"), id="rooted-identity"
        ),
        pytest.param(None, None, SimpleNamespace(id="main"), (None, "main"), id="plain-identity"),
        # A Librarian Session works on the Skills of the Agent it is bound to.
        pytest.param(
            None,
            None,
            SimpleNamespace(id="librarian", skill_agent_id="coder"),
            (None, "coder"),
            id="librarian-session",
        ),
    ],
)
def test_resolve_skill_scope(
    project_id: str | None,
    prompt_project: str | None,
    agent: Any,
    scope: tuple[str | None, str | None],
) -> None:
    project = None if prompt_project is None else _stub_project(prompt_project)

    assert resolve_skill_scope(project_id, project, agent) == scope
