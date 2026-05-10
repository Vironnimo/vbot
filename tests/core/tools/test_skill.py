"""Tests for the internal skill activation tool."""

import asyncio
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


def test_skill_tool_loads_body_and_resources(tmp_path: Path) -> None:
    registry = SkillRegistry.load(_skills_dir(tmp_path))
    tools = ToolRegistry()
    register_skill_tool(tools, registry)
    stored: dict[str, object] = {}

    def activate(name: str, data: dict[str, object]) -> dict[str, object]:
        stored[name] = data
        return tool_success(data)

    result = asyncio.run(async_dispatch(tools, _context(tmp_path, activate), {"name": "debugging"}))
    data = cast(dict[str, Any], result["data"])

    assert result["ok"] is True
    assert data["resources"] == ["scripts/run.py", "references/guide.md"]
    assert "frontmatter" not in data["content"]
    assert str(data["content"]).startswith('<skill_content name="debugging">')
    assert "Investigate failures methodically." in str(data["content"])
    assert stored["debugging"] == data


def test_skill_tool_unknown_skill_fails(tmp_path: Path) -> None:
    tools = ToolRegistry()
    register_skill_tool(tools, SkillRegistry.load(_skills_dir(tmp_path)))

    result = asyncio.run(async_dispatch(tools, _context(tmp_path), {"name": "missing"}))

    assert result == tool_failure(
        "skill_not_found",
        "Skill not found or not allowed for this agent: missing",
    )


def test_skill_tool_dedup_uses_session_activation_hook(tmp_path: Path) -> None:
    tools = ToolRegistry()
    register_skill_tool(tools, SkillRegistry.load(_skills_dir(tmp_path)))

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

    assert asyncio.run(async_dispatch(tools, context, {"name": "debugging"})) == result


def test_skill_tool_file_read_error(tmp_path: Path) -> None:
    skills_dir = _skills_dir(tmp_path)
    skill_file = skills_dir / "debugging" / "SKILL.md"
    registry = SkillRegistry.load(skills_dir)
    skill_file.unlink()
    tools = ToolRegistry()
    register_skill_tool(tools, registry)

    result = asyncio.run(async_dispatch(tools, _context(tmp_path), {"name": "debugging"}))
    error = cast(dict[str, Any], result["error"])

    assert result["ok"] is False
    assert error["code"] == "skill_read_error"


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
        skill_activation_hook=activation_hook,  # type: ignore[arg-type]
        allowed_skills=["*"],
    )
