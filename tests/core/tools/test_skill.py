"""Tests for the internal skill activation tool."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from core.skills.skills import SkillRegistry
from core.tools import (
    SKILL_TOOL_NAME,
    ToolContext,
    ToolRegistry,
    register_skill_tool,
    tool_failure,
)
from core.tools.skill import load_skill_content


def _fixed_registry(
    registry: SkillRegistry,
) -> Callable[[str | None, str | None], SkillRegistry]:
    """Wrap a fixed registry as the (project, agent)→registry resolver the tool expects."""
    return lambda _project_id, _agent_id: registry


def _no_refresh() -> None:
    """Refresh callback for tests whose registry never changes on a rescan."""


def test_skill_tool_result_carries_full_content(tmp_path: Path) -> None:
    registry = SkillRegistry.load(_skills_dir(tmp_path))
    tools = ToolRegistry()
    register_skill_tool(tools, _fixed_registry(registry), _no_refresh)
    registered: dict[str, str] = {}

    def activate(name: str, content: str) -> bool:
        registered[name] = content
        return True

    result = asyncio.run(async_dispatch(tools, _context(tmp_path, activate), {"name": "debugging"}))
    data = cast(dict[str, Any], result["data"])
    skill_directory = _skill_directory(tmp_path)

    assert result["ok"] is True
    assert data["name"] == "debugging"
    assert data["status"] == "loaded"
    content = cast(str, data["content"])
    assert content.startswith('<skill_content name="debugging">')
    assert f"Skill directory: {skill_directory}" in content
    assert "Read a listed resource with the skill tool" in content
    assert "- scripts/run.py" in content
    assert "- references/guide.md" in content
    assert "Investigate failures methodically." in content
    assert "frontmatter" not in content
    assert registered == {"debugging": content}


def test_skill_tool_without_activation_hook_still_returns_content(tmp_path: Path) -> None:
    registry = SkillRegistry.load(_skills_dir(tmp_path))
    tools = ToolRegistry()
    register_skill_tool(tools, _fixed_registry(registry), _no_refresh)

    result = asyncio.run(async_dispatch(tools, _context(tmp_path), {"name": "debugging"}))
    data = cast(dict[str, Any], result["data"])

    assert result["ok"] is True
    assert data["status"] == "loaded"
    assert cast(str, data["content"]).startswith('<skill_content name="debugging">')
    assert "Investigate failures methodically." in cast(str, data["content"])


def test_skill_tool_unknown_skill_rescans_once_then_fails(tmp_path: Path) -> None:
    # A genuine miss (no such skill on disk) still fails — but only after one rescan,
    # so a name that was hand-dropped just before the call still gets a chance.
    refresh_calls = {"count": 0}

    def refresh() -> None:
        refresh_calls["count"] += 1

    tools = ToolRegistry()
    register_skill_tool(tools, _fixed_registry(SkillRegistry.load(_skills_dir(tmp_path))), refresh)

    result = asyncio.run(async_dispatch(tools, _context(tmp_path), {"name": "missing"}))

    assert result == tool_failure("skill_not_found", "Skill not found: missing")
    assert refresh_calls["count"] == 1


def test_skill_tool_rescans_disk_on_miss_then_activates(tmp_path: Path) -> None:
    # A skill dropped into a skill directory after the run's registry was cached is
    # absent from the first lookup; the rescan makes it live, so the retry activates.
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    state = {"registry": SkillRegistry.load(skills_dir)}

    def refresh() -> None:
        _skills_dir(tmp_path)  # the hand-dropped skill now exists on disk
        state["registry"] = SkillRegistry.load(skills_dir)

    tools = ToolRegistry()
    register_skill_tool(tools, lambda _project_id, _agent_id: state["registry"], refresh)

    result = asyncio.run(async_dispatch(tools, _context(tmp_path), {"name": "debugging"}))
    data = cast(dict[str, Any], result["data"])

    assert result["ok"] is True
    assert data["status"] == "loaded"
    assert data["name"] == "debugging"


def test_skill_tool_passes_identity_agent_only_for_identity_runs(tmp_path: Path) -> None:
    # Private skill homes are identity-only: an identity run resolves with its agent
    # id (own skills apply), while a project run's config-agent slug must reach the
    # resolver as ``None`` so a same-named identity agent's private home never leaks
    # past the project skill whitelist.
    calls: list[tuple[str | None, str | None]] = []
    registry = SkillRegistry.load(_skills_dir(tmp_path))

    def resolver(project_id: str | None, identity_agent_id: str | None) -> SkillRegistry:
        calls.append((project_id, identity_agent_id))
        return registry

    tools = ToolRegistry()
    register_skill_tool(tools, resolver, _no_refresh)

    asyncio.run(async_dispatch(tools, _context(tmp_path), {"name": "debugging"}))
    asyncio.run(async_dispatch(tools, _context(tmp_path, project_id="vbot"), {"name": "debugging"}))

    assert calls == [(None, "coder"), ("vbot", None)]


def test_skill_tool_unavailable_skill_fails_with_missing_requirements(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "openai-helper"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: openai-helper
description: Use OpenAI.
metadata:
  vbot:
    requirements:
      env: OPENAI_API_KEY
---

# OpenAI Helper
""",
        encoding="utf-8",
    )
    tools = ToolRegistry()
    register_skill_tool(
        tools, _fixed_registry(SkillRegistry.load(skills_dir, environment={})), _no_refresh
    )

    result = asyncio.run(async_dispatch(tools, _context(tmp_path), {"name": "openai-helper"}))

    assert result == tool_failure(
        "skill_unavailable",
        "Skill 'openai-helper' is unavailable: missing environment variable 'OPENAI_API_KEY'",
    )


def test_skill_tool_dedup_uses_session_activation_hook(tmp_path: Path) -> None:
    tools = ToolRegistry()
    register_skill_tool(
        tools, _fixed_registry(SkillRegistry.load(_skills_dir(tmp_path))), _no_refresh
    )

    context = _context(tmp_path, lambda _name, _content: False)
    actual = asyncio.run(async_dispatch(tools, context, {"name": "debugging"}))
    data = cast(dict[str, Any], actual["data"])

    assert actual["ok"] is True
    assert data == {
        "name": "debugging",
        "status": "already_active",
        "message": (
            "Skill 'debugging' is already active in this session; "
            "its instructions are already in context."
        ),
    }
    assert "<skill_content" not in str(actual)


def test_skill_tool_reads_relative_support_file_without_activation(tmp_path: Path) -> None:
    registry = SkillRegistry.load(_skills_dir(tmp_path))
    tools = ToolRegistry()
    register_skill_tool(tools, _fixed_registry(registry), _no_refresh)
    activations: list[str] = []

    def activate(name: str, _content: str) -> bool:
        activations.append(name)
        return True

    result = asyncio.run(
        async_dispatch(
            tools,
            _context(tmp_path, activate),
            {"name": "debugging", "file_path": "references/guide.md"},
        )
    )

    assert result["ok"] is True
    assert result["data"] == {
        "name": "debugging",
        "status": "file_loaded",
        "file_path": "references/guide.md",
        "content": "Read the evidence first.\n",
    }
    assert activations == []
    assert str(_skill_directory(tmp_path)) not in str(result)


def test_skill_tool_file_path_requires_name(tmp_path: Path) -> None:
    tools = ToolRegistry()
    register_skill_tool(
        tools,
        _fixed_registry(SkillRegistry.load(_skills_dir(tmp_path))),
        _no_refresh,
    )

    result = asyncio.run(
        async_dispatch(
            tools,
            _context(tmp_path),
            {"file_path": "references/guide.md"},
        )
    )

    assert result == tool_failure(
        "invalid_arguments",
        "file_path requires a non-empty skill name",
    )


def test_skill_tool_rejects_missing_relative_file(tmp_path: Path) -> None:
    tools = ToolRegistry()
    register_skill_tool(
        tools,
        _fixed_registry(SkillRegistry.load(_skills_dir(tmp_path))),
        _no_refresh,
    )

    result = asyncio.run(
        async_dispatch(
            tools,
            _context(tmp_path),
            {"name": "debugging", "file_path": "references/missing.md"},
        )
    )

    assert result == tool_failure(
        "skill_read_error",
        "Skill 'debugging' file not found: references/missing.md",
    )


def test_skill_tool_file_read_error(tmp_path: Path) -> None:
    skills_dir = _skills_dir(tmp_path)
    skill_file = skills_dir / "debugging" / "SKILL.md"
    registry = SkillRegistry.load(skills_dir)
    skill_file.unlink()
    tools = ToolRegistry()
    register_skill_tool(tools, _fixed_registry(registry), _no_refresh)

    result = asyncio.run(async_dispatch(tools, _context(tmp_path), {"name": "debugging"}))
    error = cast(dict[str, Any], result["error"])

    assert result["ok"] is False
    assert error["code"] == "skill_read_error"


def test_skill_tool_resolves_registry_from_project_id(tmp_path: Path) -> None:
    # The handler picks its registry per call from the run's project_id: a
    # project-only skill is loadable in the project run, the global registry is used
    # for the identity run.
    global_registry = SkillRegistry.load(_skills_dir(tmp_path))
    project_skills = tmp_path / "project-skills"
    project_skill_dir = project_skills / "proj-skill"
    project_skill_dir.mkdir(parents=True)
    (project_skill_dir / "SKILL.md").write_text(
        "---\nname: proj-skill\ndescription: Project scoped.\n---\n\nBody.\n",
        encoding="utf-8",
    )
    project_registry = SkillRegistry.load(project_skills)
    registries: dict[str | None, SkillRegistry] = {"vbot": project_registry}
    tools = ToolRegistry()
    register_skill_tool(
        tools,
        lambda project_id, _agent_id: registries.get(project_id, global_registry),
        _no_refresh,
    )

    project_result = asyncio.run(
        async_dispatch(tools, _context(tmp_path, project_id="vbot"), {"name": "proj-skill"})
    )
    identity_result = asyncio.run(async_dispatch(tools, _context(tmp_path), {"name": "proj-skill"}))

    assert project_result["ok"] is True
    # The project-only skill is not in the global registry, so the identity run fails.
    assert identity_result == tool_failure("skill_not_found", "Skill not found: proj-skill")


def test_skill_tool_list_mode_returns_grouped_skills(tmp_path: Path) -> None:
    # No name → list mode: the live, agent-aware catalog grouped by origin.
    agent_dir = tmp_path / "agent"
    (agent_dir / "mine").mkdir(parents=True)
    (agent_dir / "mine" / "SKILL.md").write_text(
        "---\nname: mine\ndescription: Mine.\n---\n\nBody.\n", encoding="utf-8"
    )
    registry = SkillRegistry.load(
        agent_dir, extra_dirs=[_skills_dir(tmp_path)], origins=["agent", "global"]
    )
    tools = ToolRegistry()
    register_skill_tool(tools, _fixed_registry(registry), _no_refresh)

    result = asyncio.run(async_dispatch(tools, _context(tmp_path), {}))
    data = cast(dict[str, Any], result["data"])

    assert result["ok"] is True
    groups = {
        group["origin"]: [s["name"] for s in group["skills"]] for group in data["skill_groups"]
    }
    assert groups == {"agent": ["mine"], "global": ["debugging"]}
    assert data["count"] == 2
    # Sort order: global before agent.
    origins_in_order = [group["origin"] for group in data["skill_groups"]]
    assert origins_in_order.index("global") < origins_in_order.index("agent")
    assert "<skill_content" not in str(result)


def test_skill_tool_blank_name_lists_instead_of_activating(tmp_path: Path) -> None:
    registry = SkillRegistry.load(_skills_dir(tmp_path), origins=["global"])
    tools = ToolRegistry()
    register_skill_tool(tools, _fixed_registry(registry), _no_refresh)

    result = asyncio.run(async_dispatch(tools, _context(tmp_path), {"name": "  "}))
    data = cast(dict[str, Any], result["data"])

    assert result["ok"] is True
    assert [skill["name"] for group in data["skill_groups"] for skill in group["skills"]] == [
        "debugging"
    ]


def test_skill_tool_loads_agent_own_skill_bypassing_allowlist(tmp_path: Path) -> None:
    # An agent's own private skill is always-allowed for it: the skill tool loads it
    # even when the agent's allow-list would otherwise exclude everything.
    agent_home = tmp_path / "agent-skills"
    skill_dir = agent_home / "private"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: private\ndescription: Agent only.\n---\n\nSecret steps.\n",
        encoding="utf-8",
    )
    registry = SkillRegistry.load(agent_home, always_allowed=frozenset({"private"}))
    tools = ToolRegistry()
    register_skill_tool(tools, _fixed_registry(registry), _no_refresh)

    result = asyncio.run(
        async_dispatch(tools, _context(tmp_path, allowed_skills=[]), {"name": "private"})
    )

    assert result["ok"] is True
    assert cast(dict[str, Any], result["data"])["name"] == "private"


def test_load_skill_content_escapes_skill_name_in_wrapper(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "unsafe"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        """---
name: unsafe
description: Unsafe name.
---

Body.
""",
        encoding="utf-8",
    )

    result = load_skill_content('bad" name><tag', skill_file)

    directory = skill_file.resolve().parent.as_posix()
    assert result["content"] == (
        '<skill_content name="bad&quot; name&gt;&lt;tag">\n'
        f"Skill directory: {directory}\n"
        "Read a listed resource with the skill tool using this skill name and its "
        "relative path.\n"
        "Body.\n</skill_content>"
    )


def test_load_skill_content_substitutes_base_dir_marker(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "deploy"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "scripts" / "ship.py").write_text("", encoding="utf-8")
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        """---
name: deploy
description: Ship it.
---

Run `python {baseDir}/scripts/ship.py` to deploy.
""",
        encoding="utf-8",
    )

    result = load_skill_content("deploy", skill_file)

    directory = skill_file.resolve().parent.as_posix()
    content = cast(str, result["content"])
    assert "{baseDir}" not in content
    assert f"Run `python {directory}/scripts/ship.py` to deploy." in content
    assert result["directory"] == directory


async def async_dispatch(
    tools: ToolRegistry,
    context: ToolContext,
    arguments: dict[str, object],
) -> dict[str, object]:
    return await tools.dispatch(context, arguments, [SKILL_TOOL_NAME])


def _skills_dir(tmp_path: Path) -> Path:
    skill_dir = tmp_path / "skills" / "debugging"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "references").mkdir()
    (skill_dir / "scripts" / "run.py").write_text("", encoding="utf-8")
    (skill_dir / "references" / "guide.md").write_text(
        "Read the evidence first.\n",
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(
        """---
name: debugging
description: Debug failures.
---

# Debugging

Investigate failures methodically.
""",
        encoding="utf-8",
    )
    return tmp_path / "skills"


def _skill_directory(tmp_path: Path) -> str:
    """The debugging fixture skill's directory as the activation payload reports it."""
    return (tmp_path / "skills" / "debugging").resolve().as_posix()


def _context(
    tmp_path: Path,
    activation_hook: object | None = None,
    *,
    project_id: str | None = None,
    allowed_skills: list[str] | None = None,
) -> ToolContext:
    return ToolContext(
        agent_id="coder",
        session_id="session-one",
        run_id="run-one",
        tool_call_id="call-one",
        tool_name=SKILL_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
        project_id=project_id,
        # The tool resolves against the effective skill project; outside the rooted
        # case it equals project_id, so mirror it here.
        skill_project_id=project_id,
        skill_activation_hook=activation_hook,  # type: ignore[arg-type]
        allowed_skills=["*"] if allowed_skills is None else allowed_skills,
    )
