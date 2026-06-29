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
    tool_success,
)
from core.tools.skill import load_skill_content


def _fixed_registry(
    registry: SkillRegistry,
) -> Callable[[str | None, str | None], SkillRegistry]:
    """Wrap a fixed registry as the (project, agent)→registry resolver the tool expects."""
    return lambda _project_id, _agent_id: registry


def test_skill_tool_loads_body_and_resources(tmp_path: Path) -> None:
    registry = SkillRegistry.load(_skills_dir(tmp_path))
    tools = ToolRegistry()
    register_skill_tool(tools, _fixed_registry(registry))
    stored: dict[str, object] = {}

    def activate(name: str, data: dict[str, object]) -> dict[str, object]:
        stored[name] = data
        return tool_success(data)

    result = asyncio.run(async_dispatch(tools, _context(tmp_path, activate), {"name": "debugging"}))
    data = cast(dict[str, Any], result["data"])
    stored_data = cast(dict[str, Any], stored["debugging"])

    assert result["ok"] is True
    assert data == {
        "name": "debugging",
        "status": "loaded",
        "message": "Skill 'debugging' loaded into session context.",
        "resources": ["scripts/run.py", "references/guide.md"],
    }
    assert "content" not in data
    assert "<skill_content" not in str(result)
    assert "Investigate failures methodically." not in str(result)
    assert stored_data["resources"] == ["scripts/run.py", "references/guide.md"]
    assert "frontmatter" not in stored_data["content"]
    assert str(stored_data["content"]).startswith('<skill_content name="debugging">')
    assert "Investigate failures methodically." in str(stored_data["content"])


def test_skill_tool_without_activation_hook_returns_minimal_status(tmp_path: Path) -> None:
    registry = SkillRegistry.load(_skills_dir(tmp_path))
    tools = ToolRegistry()
    register_skill_tool(tools, _fixed_registry(registry))

    result = asyncio.run(async_dispatch(tools, _context(tmp_path), {"name": "debugging"}))
    data = cast(dict[str, Any], result["data"])

    assert result["ok"] is True
    assert data["resources"] == ["scripts/run.py", "references/guide.md"]
    assert data["status"] == "loaded"
    assert "content" not in data
    assert "<skill_content" not in str(result)
    assert "Investigate failures methodically." not in str(result)


def test_skill_tool_unknown_skill_fails(tmp_path: Path) -> None:
    tools = ToolRegistry()
    register_skill_tool(tools, _fixed_registry(SkillRegistry.load(_skills_dir(tmp_path))))

    result = asyncio.run(async_dispatch(tools, _context(tmp_path), {"name": "missing"}))

    assert result == tool_failure("skill_not_found", "Skill not found: missing")


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
    register_skill_tool(tools, _fixed_registry(SkillRegistry.load(skills_dir, environment={})))

    result = asyncio.run(async_dispatch(tools, _context(tmp_path), {"name": "openai-helper"}))

    assert result == tool_failure(
        "skill_unavailable",
        "Skill 'openai-helper' is unavailable: missing environment variable 'OPENAI_API_KEY'",
    )


def test_skill_tool_dedup_uses_session_activation_hook(tmp_path: Path) -> None:
    tools = ToolRegistry()
    register_skill_tool(tools, _fixed_registry(SkillRegistry.load(_skills_dir(tmp_path))))

    result = tool_success(
        {
            "content": (
                "Skill 'debugging' was already activated in this session. Skipping re-activation."
            ),
            "resources": [],
            "already_active": True,
        }
    )

    context = _context(tmp_path, lambda _name, _data: result)
    actual = asyncio.run(async_dispatch(tools, context, {"name": "debugging"}))
    data = cast(dict[str, Any], actual["data"])

    assert actual["ok"] is True
    assert data == {
        "name": "debugging",
        "status": "already_active",
        "message": "Skill 'debugging' was already active in this session.",
        "resources": ["scripts/run.py", "references/guide.md"],
    }
    assert "content" not in data
    assert "<skill_content" not in str(actual)


def test_skill_tool_file_read_error(tmp_path: Path) -> None:
    skills_dir = _skills_dir(tmp_path)
    skill_file = skills_dir / "debugging" / "SKILL.md"
    registry = SkillRegistry.load(skills_dir)
    skill_file.unlink()
    tools = ToolRegistry()
    register_skill_tool(tools, _fixed_registry(registry))

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
        tools, lambda project_id, _agent_id: registries.get(project_id, global_registry)
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
    register_skill_tool(tools, _fixed_registry(registry))

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
    register_skill_tool(tools, _fixed_registry(registry))

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
    register_skill_tool(tools, _fixed_registry(registry))

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

    assert result["content"] == (
        '<skill_content name="bad&quot; name&gt;&lt;tag">\nBody.\n</skill_content>'
    )


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
    (skill_dir / "references" / "guide.md").write_text("", encoding="utf-8")
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
        app_root=tmp_path,
        data_root=tmp_path,
        project_id=project_id,
        # The tool resolves against the effective skill project; outside the rooted
        # case it equals project_id, so mirror it here.
        skill_project_id=project_id,
        skill_activation_hook=activation_hook,  # type: ignore[arg-type]
        allowed_skills=["*"] if allowed_skills is None else allowed_skills,
    )
