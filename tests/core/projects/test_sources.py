"""Repository translation contracts at the public scan and runtime boundaries."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from core.agents import TemporaryAgentConfig, TemporaryAgentRegistry
from core.projects._resolution_values import profile_tool_access
from core.projects.scan_report import FindingType
from core.projects.sources import SourceSelection, read_profile, scan_project
from core.projects.sources.catalog import detect_sources, refresh_sources, skill_roots
from core.sessions import ChatSessionManager, SessionAddress
from core.tools.availability import ToolAccess, resolve_tool_access
from tests.core.projects.resolver_test_support import (
    AgentResolutionError,
    _openai_configured,
    _resolver,
)
from tests.core.projects.resolver_test_support import (
    agents as agents,
)
from tests.core.projects.resolver_test_support import (
    data_dir as data_dir,
)
from tests.core.projects.resolver_test_support import (
    projects as projects,
)
from tests.core.projects.resolver_test_support import (
    repo as repo,
)
from tests.core.projects.resolver_test_support import (
    template_dir as template_dir,
)


def write(root: Path, path: str, content: str) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


ALL_TOOLS = frozenset(
    {"read", "search_files", "apply_patch", "bash", "terminal", "status", "skill", "subagent"}
)
NO_SHELL = ALL_TOOLS - {"bash", "terminal"}


@pytest.mark.parametrize(
    ("source", "path", "document", "expected", "status"),
    [
        ("claude", ".claude/agents/reviewer.md", "---\ntools: []\n---\nReview.", set(), "ready"),
        (
            "codex",
            ".codex/agents/reviewer.toml",
            'name = "reviewer"\nmodel = [',
            set(),
            "needs_attention",
        ),
        (
            "claude",
            ".claude/agents/reviewer.md",
            "---\ntools: []\ntools: Read\n---\nReview.",
            set(),
            "needs_attention",
        ),
        (
            "claude",
            ".claude/agents/reviewer.md",
            "---\ntools: [Read\n---\nReview.",
            set(),
            "needs_attention",
        ),
        # A Tool the Agent may use for some commands or paths is granted whole.
        (
            "claude",
            ".claude/agents/reviewer.md",
            "---\ntools: Read(src/**), Bash(git status)\n---\nReview.",
            {"read", "bash"},
            "ready",
        ),
        (
            "claude",
            ".claude/agents/reviewer.md",
            "---\ndisallowedTools: Read(./.env), Bash\n---\nReview.",
            NO_SHELL,
            "ready",
        ),
        (
            "claude",
            ".claude/agents/reviewer.md",
            "---\npermissionMode: acceptEdits\n---\nReview.",
            ALL_TOOLS,
            "ready",
        ),
        (
            "claude",
            ".claude/agents/reviewer.md",
            "---\npermissionMode: dontAsk\n---\nReview.",
            set(),
            "ready",
        ),
        (
            "claude",
            ".claude/agents/reviewer.md",
            "---\nhooks:\n  PreToolUse:\n    - matcher: Bash\n---\nReview.",
            ALL_TOOLS,
            "limited",
        ),
        (
            "opencode",
            ".opencode/agents/reviewer.md",
            "---\npermission:\n  '*': deny\n  read: allow\n---\nReview.",
            {"read"},
            "ready",
        ),
        (
            "opencode",
            ".opencode/agents/reviewer.md",
            "---\npermission:\n  bash:\n    '*': allow\n    'git push*': deny\n"
            "  edit: ask\n  todowrite: deny\n---\nReview.",
            ALL_TOOLS,
            "ready",
        ),
        (
            "opencode",
            "opencode.json",
            '{"agent":{"reviewer":{"disable":true}}}',
            set(),
            "needs_attention",
        ),
        (
            "opencode",
            "opencode.jsonc",
            '{\n  // Shared\n  "permission": {"bash": "deny"},\n'
            '  "agent": {"reviewer": {"prompt": "Review.",},},\n}',
            NO_SHELL,
            "ready",
        ),
        (
            "codex",
            ".codex/agents/reviewer.toml",
            'name="reviewer"\ndescription="Review"\ndeveloper_instructions="Review."\n'
            'sandbox_mode="read-only"\napproval_policy="on-request"',
            ALL_TOOLS - {"apply_patch"},
            "ready",
        ),
        (
            "codex",
            ".codex/config.toml",
            '[features]\nshell_tool=false\n[agents.reviewer]\ndescription="Review"',
            NO_SHELL,
            "limited",
        ),
        (
            "copilot",
            ".github/agents/reviewer.agent.md",
            "---\ntools: [read]\n---\nReview.",
            {"read"},
            "ready",
        ),
        (
            "cursor",
            ".cursor/agents/reviewer.md",
            "---\nreadonly: true\n---\nReview.",
            {"read", "search_files", "status"},
            "ready",
        ),
        (
            "gemini",
            ".gemini/agents/reviewer.md",
            "---\ntools: [read_file]\n---\nReview.",
            {"read"},
            "ready",
        ),
    ],
)
def test_imports_grant_the_tools_an_agent_may_use(repo, source, path, document, expected, status):
    write(repo, path, document)
    sources = [SourceSelection(f"{source}.agents")]
    profile = scan_project(repo, sources=sources).team[0]
    assert read_profile(repo, sources, profile.agent_id, selected=profile) == profile
    assert profile.status == status
    policy = profile_tool_access(profile, tuple(sorted(ALL_TOOLS)))
    assert set(policy.allowed) == expected
    # A Profile selects the Agent's own Tools; the Session's grants still apply to
    # every runnable Profile, including one with an empty Tool list.
    from types import SimpleNamespace

    tools = [SimpleNamespace(name=name, activation="configurable") for name in policy.allowed]
    tools += [SimpleNamespace(name="message_parent", activation="session_grant")]
    actual = resolve_tool_access(policy, tools, "off", session_tool_grants=("message_parent",))
    assert set(actual.allowed_tools) == expected | (
        {"message_parent"} if status != "needs_attention" else set()
    )


def test_mixed_sources_priority_and_new_detection_preserve_winners(repo):
    write(repo, ".claude/agents/reviewer.md", "---\nname: reviewer\n---\nClaude.")
    write(repo, ".opencode/agents/builder.md", "Builder.")
    write(
        repo,
        ".agents/skills/review/SKILL.md",
        "---\nname: review\ndescription: Review code.\n---\nReview.",
    )
    sources = [
        SourceSelection("claude.agents"),
        SourceSelection("opencode.agents"),
        SourceSelection("shared.skills"),
    ]
    write(repo, ".opencode/agents/reviewer.md", "OpenCode.")
    # Neither documentation nor settings for OpenCode's own Agents define Team members.
    write(repo, ".claude/agents/README.md", "Our agents.")
    write(repo, "opencode.json", '{"agent":{"plan":{"model":"anthropic/x"}}}')
    result = scan_project(repo, sources=sources)
    assert [agent.agent_id for agent in result.team] == ["builder", "reviewer"]
    assert result.team[1].source == "claude"
    assert result.shadowed[0].source == "opencode"
    assert result.report.findings_of(FindingType.SLUG_COLLISION)
    write(repo, ".cursor/agents/reviewer.md", "---\nname: reviewer\n---\nCursor.")
    refreshed = refresh_sources(sources, detect_sources(repo))
    assert next(item for item in refreshed if item.id == "cursor.agents").enabled is False
    reordered = scan_project(repo, sources=[sources[1], sources[0], sources[2]])
    assert reordered.team[1].source == "opencode"
    selected = result.team[1]
    write(repo, ".claude/agents/reviewer.md", "---\nname: renamed\n---\nRenamed.")
    fallback = read_profile(repo, sources, "reviewer", selected=selected)
    assert fallback is not None and fallback.source == "opencode"
    (repo / ".claude/agents/reviewer.md").unlink()
    assert read_profile(repo, sources, "reviewer", selected=selected) == fallback


@pytest.mark.parametrize("source", ["claude", "opencode", "codex"])
def test_selected_profile_reloads_inherited_rules_and_only_its_definition(
    repo, monkeypatch, source
):
    from core.projects.sources import _reading as reading

    if source == "claude":
        config = write(repo, ".claude/settings.json", "{}")
        path = write(repo, ".claude/agents/definition.md", "---\nname: reviewer\n---\nBefore.")
        write(repo, ".claude/agents/other.md", "---\nname: other\n---\nOther.")
        changed_config = '{"permissions":{"deny":["Bash"]}}'
        changed_source = "---\nname: reviewer\n---\nAfter."
        expected_reads = {config, path}
    elif source == "opencode":
        config = write(
            repo,
            "opencode.json",
            '{"agent":{"reviewer":{"prompt":"{file:reviewer.txt}"},'
            '"other":{"prompt":"{file:other.txt}"}}}',
        )
        path = write(repo, "reviewer.txt", "Before.")
        write(repo, "other.txt", "Other.")
        changed_config = config.read_text(encoding="utf-8")[:-1] + ',"permission":{"bash":"deny"}}'
        changed_source = "After."
        expected_reads = {config, path}
    else:
        config = write(
            repo,
            ".codex/config.toml",
            "[features]\nshell_tool=true\n"
            '[agents.other]\nconfig_file="agents/role.data"\ndescription="Other role"\n'
            '[agents.reviewer]\nconfig_file="agents/role.data"\ndescription="Selected role"\n'
            '[agents.independent]\nconfig_file="independent.toml"\n',
        )
        path = write(repo, ".codex/agents/role.data", 'developer_instructions="Before."')
        independent = write(repo, ".codex/independent.toml", 'developer_instructions="Other."')
        write(repo, ".codex/agents/standalone.toml", 'name="standalone"')
        changed_config = config.read_text(encoding="utf-8").replace(
            "shell_tool=true", "shell_tool=false"
        )
        changed_source = 'name="reviewer"\ndeveloper_instructions="After."'
        expected_reads = {config, path, independent}
    sources = [SourceSelection(f"{source}.agents")]
    selected = next(
        agent for agent in scan_project(repo, sources=sources).team if agent.agent_id == "reviewer"
    )
    config.write_text(changed_config, encoding="utf-8")
    path.write_text(changed_source, encoding="utf-8")
    original_read = reading.read_text
    reads: list[Path] = []

    def read_text(source_path):
        reads.append(source_path)
        return original_read(source_path)

    monkeypatch.setattr(reading, "read_text", read_text)
    reloaded = read_profile(repo, sources, "reviewer", selected=selected)
    assert reloaded is not None and reloaded.agent_id == "reviewer"
    assert reloaded.body == "After."
    assert set(profile_tool_access(reloaded, tuple(ALL_TOOLS)).allowed) == NO_SHELL
    assert set(reads) == expected_reads
    if source == "codex":
        assert reloaded.description == "Selected role"
        # Every explicit role file still validates the inherited configuration.
        independent.write_text("broken = [", encoding="utf-8")
        invalid = read_profile(repo, sources, "reviewer", selected=selected)
        assert invalid is not None and invalid.unavailable_reason
        # Removing a role must not promote an arbitrary referenced config file
        # into a standalone .toml definition, even if it declares the same name.
        config.write_text("[features]\nshell_tool=false\n", encoding="utf-8")
        assert read_profile(repo, sources, "reviewer", selected=selected) is None


@pytest.mark.parametrize("source", ["claude", "opencode", "codex"])
@pytest.mark.parametrize("failure", ["malformed", "unreadable"])
def test_a_selected_unavailable_profile_never_uses_a_shadowed_definition(
    agents, projects, repo, deny_access, source, failure
):
    if source == "codex":
        path = write(
            repo, ".codex/agents/definition.toml", 'name="reviewer"\nmodel="openai/gpt-5.2"'
        )
        broken = "name = ["
    else:
        filename = "reviewer" if source == "opencode" else "definition"
        path = write(
            repo,
            f".{source}/agents/{filename}.md",
            "---\nname: reviewer\nmodel: openai/gpt-5.2\n---\nSelected.",
        )
        broken = "---\nname: [\n---\nBroken."
    write(repo, ".cursor/agents/reviewer.md", "---\nmodel: openai/gpt-5.2\n---\nShadowed.")
    project = projects.create(
        "repo",
        "Repo",
        repo,
        sources=[{"id": f"{source}.agents"}, {"id": "cursor.agents"}],
    )
    resolver = _resolver(agents, projects, _openai_configured())
    resolver.rescan_project(project)
    if failure == "malformed":
        path.write_text(broken, encoding="utf-8")
    else:
        deny_access(path)
    with pytest.raises(AgentResolutionError) as error:
        resolver.resolve_agent("repo", "reviewer")
    assert type(error.value) is AgentResolutionError


@pytest.mark.parametrize(
    ("files", "enabled"),
    [(("AGENTS.md", "CLAUDE.md", "GEMINI.md"), set()), (("CLAUDE.md", "GEMINI.md"), {"claude"})],
)
def test_new_projects_load_at_most_one_instruction_file(projects, repo, files, enabled):
    for name in files:
        write(repo, name, "Instructions.")
    sources = projects.create("repo", "Repo", repo).sources
    assert {
        source.id.removesuffix(".instructions")
        for source in sources
        if source.id.endswith(".instructions") and source.enabled
    } == enabled
    write(repo, ".github/copilot-instructions.md", "Instructions.")
    refreshed = projects.refresh_sources("repo").sources
    assert next(item for item in refreshed if item.id == "copilot.instructions").enabled is False


# The old readers: Claude walked its agents folder, OpenCode read only its top level.
@pytest.mark.parametrize(
    ("ecosystem", "legacy_team"), [("claude", ["new", "reviewer"]), ("opencode", ["reviewer"])]
)
def test_legacy_anchor_preserves_unknown_fields_overrides_and_sessions(
    projects, repo, data_dir, ecosystem, legacy_team
):
    write(repo, f".{ecosystem}/agents/reviewer.md", "---\nname: reviewer\n---\nReview.")
    write(repo, f".{ecosystem}/agents/nested/new.md", "---\nname: new\n---\nNew.")
    write(repo, "opencode.json", '{"agent":{"json-only":{"prompt":"JSON definition."}}}')
    other = "opencode" if ecosystem == "claude" else "claude"
    write(repo, f".{other}/agents/builder.md", "Build.")
    projects.create("repo", "Repo", repo)
    path = data_dir / "projects" / "repo" / "project.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored.pop("sources")
    stored["source_format"] = ecosystem
    stored["future_field"] = {"keep": True}
    stored["overrides"] = {"reviewer": {"model": "openai/gpt-5.2"}}
    path.write_text(json.dumps(stored), encoding="utf-8")
    sessions = ChatSessionManager(data_dir)
    try:
        session = sessions.create("reviewer", project_id="repo")
        loaded = projects.get("repo")
        assert [
            agent.agent_id for agent in scan_project(repo, sources=loaded.sources).team
        ] == legacy_team
        assert loaded.overrides == stored["overrides"]
        projects.update("repo", display_name="Renamed")
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["format_version"] == 1
        assert saved["future_field"] == {"keep": True}
        assert sessions.get(SessionAddress("repo", "reviewer", session.id)) is not None
        expanded = projects.update(
            "repo",
            sources=[
                {key: value for key, value in source.to_dict().items() if key != "agent_paths"}
                for source in loaded.sources
            ],
        )
        assert "new" in {
            agent.agent_id for agent in scan_project(repo, sources=expanded.sources).team
        }

    finally:
        sessions.close()


def test_profiles_preload_skills_map_models_and_snapshot_temporary_participants(
    agents, projects, repo, data_dir
):
    path = write(
        repo,
        ".claude/agents/reviewer.md",
        "---\nmodel: sonnet\neffort: high\ntools: Read\nskills: [review]\n---\nReview repository.",
    )
    projects.create("repo", "Repo", repo, model_mappings={"sonnet": "openai/gpt-5.2"})
    resolver = _resolver(
        agents, projects, _openai_configured(), project_skill_names={"repo": frozenset({"review"})}
    )
    resolver._profile_skill_content = lambda project_id, name, allowed: "Check regression risks."
    resolved = resolver.resolve_agent("repo", "reviewer")
    assert resolved.model == "openai/gpt-5.2"
    assert resolved.thinking_effort == "high"
    assert '<skill_content name="review">' in resolved.body
    assert "Check regression risks." in resolved.body
    sessions = ChatSessionManager(data_dir)
    try:
        registry = TemporaryAgentRegistry(
            sessions, prepare_config=resolver.prepare_temporary_config
        )
        config = TemporaryAgentConfig(
            model="openai/gpt-mini",
            cwd=repo,
            tool_access=ToolAccess(mode="selected", allowed=("read", "bash")),
            allowed_skills=["review"],
            tools={},
            name="Reviewer",
            repository_profile="reviewer",
        )
        binding = registry.create(
            owner_name="swarm",
            group_id="group",
            participant_id="peer",
            config=config,
            project_id="repo",
        )
        participant = registry.resolve(binding.address, generation_id=binding.generation_id)
        assert participant.model == "openai/gpt-5.2"
        assert participant.tool_access.allowed == ("read",)
        assert not participant.tool_access.fixed
        assert "Check regression risks." in participant.instructions
        path.write_text("---\ntools: Bash\n---\nChanged.", encoding="utf-8")
        assert (
            registry.create(
                owner_name="swarm",
                group_id="group",
                participant_id="peer",
                config=config,
                project_id="repo",
            )
            == binding
        )
        assert registry.resolve(binding.address, generation_id=binding.generation_id) == participant
        with pytest.raises(ValueError, match="differs"):
            registry.create(
                owner_name="swarm",
                group_id="group",
                participant_id="peer",
                config=replace(config, name="Other"),
                project_id="repo",
            )

        def interleaved_prepare(config, project_id):
            registry.create(
                owner_name="swarm",
                group_id="group",
                participant_id="race",
                config=config,
                project_id=project_id,
            )
            path.write_text("---\ntools: []\n---\nLater edit.", encoding="utf-8")
            return resolver.prepare_temporary_config(config, project_id)

        racing = TemporaryAgentRegistry(sessions, prepare_config=interleaved_prepare)
        winner = racing.create(
            owner_name="swarm",
            group_id="group",
            participant_id="race",
            config=config,
            project_id="repo",
        )
        assert (
            "Changed."
            in registry.resolve(winner.address, generation_id=winner.generation_id).instructions
        )
    finally:
        sessions.close()


@pytest.mark.asyncio
async def test_inherit_uses_the_delegating_model_without_requiring_a_project_default(
    agents, projects, repo
):
    write(repo, ".claude/agents/reviewer.md", "---\nmodel: inherit\n---\nReview.")
    projects.create("repo", "Repo", repo)
    resolver = _resolver(agents, projects, _openai_configured())
    agent = await resolver.resolve_delegated_agent_async(
        "repo", "reviewer", caller_model="openai/gpt-mini"
    )
    assert agent.model == "openai/gpt-mini"
    assert agent.model_inherit
    projects.set_override("repo", "reviewer", "model", "openai/gpt-5.2")
    agent = await resolver.resolve_delegated_agent_async(
        "repo", "reviewer", caller_model="openai/gpt-mini"
    )
    assert agent.model == "openai/gpt-5.2"
    assert not agent.model_inherit
    projects.clear_override("repo", "reviewer", "model")
    projects.update("repo", default_model="openai/gpt-5.2")
    write(repo, ".claude/agents/reviewer.md", "---\nmodel: unmapped-wish\n---\nReview.")
    temporary = resolver.prepare_temporary_config(
        TemporaryAgentConfig(
            model="openai/gpt-mini",
            cwd=repo,
            tool_access=ToolAccess(mode="none"),
            allowed_skills=[],
            tools={},
            name="Reviewer",
            repository_profile="reviewer",
        ),
        "repo",
    )
    assert temporary.model == "openai/gpt-5.2"
    # Without any usable default the participant keeps its own Model.
    projects.update("repo", default_model="")
    resolver._global_agent_defaults = lambda: {}
    temporary = resolver.prepare_temporary_config(
        TemporaryAgentConfig(
            model="openai/gpt-mini",
            cwd=repo,
            tool_access=ToolAccess(mode="none"),
            allowed_skills=[],
            tools={},
            name="Reviewer",
            repository_profile="reviewer",
        ),
        "repo",
    )
    assert temporary.model == "openai/gpt-mini"


@pytest.mark.parametrize(
    "owner_policy",
    [
        ToolAccess(mode="none"),
        ToolAccess(mode="selected", allowed=("read",), fixed=True),
        ToolAccess(mode="selected", allowed=("apply_patch",), denied=("write",)),
    ],
)
def test_temporary_profiles_keep_owner_tool_limits_and_materialize_skill_rules(
    agents, projects, repo, owner_policy
):
    write(
        repo,
        "opencode.json",
        json.dumps(
            {"agent": {"reviewer": {"permission": {"skill": {"*": "allow", "private": "deny"}}}}}
        ),
    )
    projects.create("repo", "Repo", repo)
    resolver = _resolver(agents, projects, _openai_configured())
    resolver._skill_pool_names = lambda project_id: frozenset({"review", "private"})
    config = TemporaryAgentConfig(
        model="openai/gpt-mini",
        cwd=repo,
        tool_access=owner_policy,
        allowed_skills=["*"],
        tools={},
        name="Reviewer",
        repository_profile="reviewer",
    )
    prepared = resolver.prepare_temporary_config(config, "repo")
    assert prepared.allowed_skills == ["review"]
    assert not set(prepared.tool_access.allowed) & set(owner_policy.denied)
    assert set(owner_policy.denied) <= set(prepared.tool_access.denied)
    if owner_policy.mode == "none":
        assert prepared.tool_access.fixed and prepared.tool_access.allowed == ()
    elif owner_policy.fixed:
        assert prepared.tool_access.fixed
        assert prepared.tool_access.allowed == owner_policy.allowed
    projects.set_override(
        "repo",
        "reviewer",
        "tool_access",
        {
            "mode": "selected",
            "allowed": ["read", "bash", "apply_patch"],
        },
    )
    overridden = resolver.prepare_temporary_config(config, "repo")
    assert set(overridden.tool_access.allowed) <= set(owner_policy.allowed)
    assert not set(overridden.tool_access.allowed) & set(owner_policy.denied)
    if owner_policy.mode == "none" or owner_policy.fixed:
        assert overridden.tool_access.fixed


def test_a_skill_folder_linked_to_another_supplies_its_skills_once(repo):
    write(repo, ".agents/skills/review/SKILL.md", "---\nname: review\ndescription: R.\n---\nR.")
    (repo / ".claude").mkdir()
    try:
        (repo / ".claude" / "skills").symlink_to(repo / ".agents" / "skills", True)
    except OSError, NotImplementedError:
        pytest.skip("symlink creation not permitted on this host")
    detected = {item.definition.id for item in detect_sources(repo)}
    assert "shared.skills" in detected
    assert "claude.skills" not in detected
    # Projects that already list the linked Source load the folder once.
    sources = [SourceSelection("claude.skills"), SourceSelection("shared.skills")]
    assert skill_roots(repo, sources) == [repo / ".agents/skills"]
