"""Tests for the internal agent skill-authoring tool (``skill_manage``)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

from core.skills.authoring import SkillAuthoringService
from core.skills.skills import SkillRegistry
from core.tools import (
    SKILL_MANAGE_TOOL_NAME,
    ToolContext,
    ToolRegistry,
    register_skill_manage_tool,
)


def _skill_md(name: str = "demo", description: str = "Do a demo task.", body: str = "# Demo\n") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        self._homes = tmp_path / "agents"
        self.invalidated: list[str] = []
        self.tools = ToolRegistry()
        register_skill_manage_tool(
            self.tools,
            SkillAuthoringService(protected_roots=[tmp_path / "resources" / "skills"]),
            self.home,
            self.invalidated.append,
        )

    def home(self, agent_id: str) -> Path:
        return self._homes / agent_id / "skills"

    def run(self, arguments: dict[str, object], agent_id: str = "main") -> dict[str, Any]:
        context = _context(agent_id)
        return cast(
            dict[str, Any],
            asyncio.run(self.tools.dispatch(context, arguments, [SKILL_MANAGE_TOOL_NAME])),
        )


def _context(agent_id: str) -> ToolContext:
    here = Path(".")
    return ToolContext(
        agent_id=agent_id,
        session_id="session-one",
        run_id="run-one",
        tool_call_id="call-one",
        tool_name=SKILL_MANAGE_TOOL_NAME,
        tool_call_index=0,
        workspace=here,
        app_root=here,
        data_root=here,
    )


def test_create_in_empty_home_and_invalidates(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)

    result = harness.run({"operation": "create", "name": "demo", "content": _skill_md()})

    assert result["ok"] is True
    assert cast(dict[str, Any], result["data"])["operation"] == "create"
    assert (harness.home("main") / "demo" / "SKILL.md").is_file()
    assert harness.invalidated == ["main"]


def test_created_skill_is_loadable_in_same_session(tmp_path: Path) -> None:
    # The "live registry" contract: after the write, a fresh scan of the agent's
    # home (what the next resolve does) finds the new skill by name.
    harness = _Harness(tmp_path)

    harness.run({"operation": "create", "name": "demo", "content": _skill_md()})

    registry = SkillRegistry.load(harness.home("main"))
    assert registry.get("demo").description == "Do a demo task."


def test_edit_rewrites_skill(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.run({"operation": "create", "name": "demo", "content": _skill_md()})

    result = harness.run(
        {"operation": "edit", "name": "demo", "content": _skill_md(description="Updated.")}
    )

    assert result["ok"] is True
    assert SkillRegistry.load(harness.home("main")).get("demo").description == "Updated."


def test_patch_applies_unique_replacement(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.run({"operation": "create", "name": "demo", "content": _skill_md(body="old line\n")})

    result = harness.run(
        {"operation": "patch", "name": "demo", "old_string": "old line", "new_string": "new line"}
    )

    assert result["ok"] is True
    assert "new line" in (harness.home("main") / "demo" / "SKILL.md").read_text(encoding="utf-8")


def test_patch_empty_new_string_deletes_text(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.run({"operation": "create", "name": "demo", "content": _skill_md(body="drop me\nkeep\n")})

    result = harness.run(
        {"operation": "patch", "name": "demo", "old_string": "drop me\n", "new_string": ""}
    )

    assert result["ok"] is True
    body = (harness.home("main") / "demo" / "SKILL.md").read_text(encoding="utf-8")
    assert "drop me" not in body
    assert "keep" in body


def test_delete_removes_skill(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.run({"operation": "create", "name": "demo", "content": _skill_md()})

    result = harness.run({"operation": "delete", "name": "demo"})

    assert result["ok"] is True
    assert not (harness.home("main") / "demo").exists()


def test_write_and_remove_support_file(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.run({"operation": "create", "name": "demo", "content": _skill_md()})

    write_result = harness.run(
        {"operation": "write_file", "name": "demo", "path": "scripts/run.py", "content": "x = 1\n"}
    )
    assert write_result["ok"] is True
    assert (harness.home("main") / "demo" / "scripts" / "run.py").is_file()

    remove_result = harness.run({"operation": "remove_file", "name": "demo", "path": "scripts/run.py"})
    assert remove_result["ok"] is True
    assert not (harness.home("main") / "demo" / "scripts" / "run.py").exists()


def test_invalid_skill_returns_diagnostics(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)

    result = harness.run(
        {"operation": "create", "name": "demo", "content": "---\nname: demo\n---\n\nbody\n"}
    )

    assert result["ok"] is False
    error = cast(dict[str, Any], result["error"])
    assert error["code"] == "skill_write_rejected"
    assert "description" in error["message"]
    # A rejected write does not invalidate anything (nothing changed).
    assert harness.invalidated == []


def test_path_traversal_rejected(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)

    result = harness.run({"operation": "create", "name": "../escape", "content": _skill_md()})

    assert result["ok"] is False
    assert cast(dict[str, Any], result["error"])["code"] == "skill_write_rejected"


def test_unknown_operation_is_invalid_arguments(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)

    result = harness.run({"operation": "promote", "name": "demo"})

    assert result["ok"] is False
    assert cast(dict[str, Any], result["error"])["code"] == "invalid_arguments"


def test_missing_content_is_invalid_arguments(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)

    result = harness.run({"operation": "create", "name": "demo"})

    assert result["ok"] is False
    assert cast(dict[str, Any], result["error"])["code"] == "invalid_arguments"


def test_unknown_argument_rejected(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)

    result = harness.run({"operation": "create", "name": "demo", "content": _skill_md(), "scope": "global"})

    assert result["ok"] is False
    assert cast(dict[str, Any], result["error"])["code"] == "invalid_arguments"


def test_writes_target_only_calling_agents_home(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)

    harness.run({"operation": "create", "name": "demo", "content": _skill_md()}, agent_id="alice")

    assert (harness.home("alice") / "demo" / "SKILL.md").is_file()
    assert not harness.home("bob").exists()
    assert harness.invalidated == ["alice"]
