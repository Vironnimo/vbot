"""Repository translation contracts at the public scan and runtime boundaries."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from core.agents import TemporaryAgentConfig, TemporaryAgentRegistry
from core.projects._resolution_values import profile_tool_access
from core.projects.scan_report import FindingType
from core.projects.sources import SourceSelection, scan_project
from core.projects.sources.catalog import detect_sources, refresh_sources
from core.sessions import ChatSessionManager, SessionAddress
from core.tools.availability import ToolAccess, resolve_tool_access
from tests.core.projects.resolver_test_support import (
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
            "---\ntools: Read(src/**)\n---\nReview.",
            set(),
            "limited",
        ),
        (
            "claude",
            ".claude/agents/reviewer.md",
            "---\ntools: Read, Bash(git status)\n---\nReview.",
            {"read"},
            "limited",
        ),
        (
            "claude",
            ".claude/agents/reviewer.md",
            "---\ntools: [Read\n---\nReview.",
            set(),
            "needs_attention",
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
            "---\npermission:\n  bash:\n    '*': allow\n    'git push*': deny\n---\nReview.",
            {"read", "search_files", "apply_patch", "status", "skill", "subagent"},
            "limited",
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
            "opencode.json",
            '{"permission":{"*":"deny"},"agent":{"reviewer":{"permission":{"read":"allow"},"prompt":"Review."}}}',
            {"read"},
            "ready",
        ),
        (
            "codex",
            ".codex/agents/reviewer.toml",
            'name="reviewer"\ndescription="Review"\ndeveloper_instructions="Review."\nsandbox_mode="read-only"',
            {"status", "skill", "subagent"},
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
def test_imports_never_widen_the_tool_selection(repo, source, path, document, expected, status):
    write(repo, path, document)
    profile = scan_project(repo, sources=[SourceSelection(f"{source}.agents")]).team[0]
    assert profile.status == status
    policy = profile_tool_access(
        profile,
        ("read", "search_files", "apply_patch", "bash", "terminal", "status", "skill", "subagent"),
    )
    assert set(policy.allowed) == expected
    # The actual Tool resolver must not add followers or Session grants to an
    # imported exact list, including the empty list.
    from types import SimpleNamespace

    tools = [SimpleNamespace(name=name, activation="configurable") for name in policy.allowed]
    tools += [SimpleNamespace(name="message_parent", activation="session_grant")]
    actual = resolve_tool_access(policy, tools, "off", session_tool_grants=("message_parent",))
    assert set(actual.allowed_tools) == expected | (
        {"message_parent"} if not policy.fixed else set()
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
    result = scan_project(repo, sources=sources)
    assert [agent.agent_id for agent in result.team] == ["builder", "reviewer"]
    assert result.team[1].source == "claude"
    assert result.shadowed[0].source == "opencode"
    assert result.report.findings_of(FindingType.SLUG_COLLISION)
    write(repo, ".cursor/agents/reviewer.md", "Cursor.")
    refreshed = refresh_sources(sources, detect_sources(repo))
    assert next(item for item in refreshed if item.id == "cursor.agents").enabled is False
    reordered = scan_project(repo, sources=[sources[1], sources[0], sources[2]])
    assert reordered.team[1].source == "opencode"


@pytest.mark.parametrize("ecosystem", ["claude", "opencode"])
def test_legacy_anchor_preserves_unknown_fields_overrides_and_sessions(
    projects, repo, data_dir, ecosystem
):
    write(repo, f".{ecosystem}/agents/reviewer.md", "---\nname: reviewer\n---\nReview.")
    write(repo, f".{ecosystem}/agents/nested/new.md", "New nested definition.")
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
        assert [agent.agent_id for agent in scan_project(repo, sources=loaded.sources).team] == [
            "reviewer"
        ]
        assert loaded.overrides == stored["overrides"]
        projects.update("repo", display_name="Renamed")
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["format_version"] == 1
        assert saved["future_field"] == {"keep": True}
        assert sessions.get(SessionAddress("repo", "reviewer", session.id)) is not None
        assert saved["sources"][0]["agent_paths"] == [f".{ecosystem}/agents/*.md"]
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
        assert participant.tool_access.fixed
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
