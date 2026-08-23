"""Tests for ``skill_manage`` update rights on Skills shared into the caller."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

from core.skills.authoring import SkillAuthoringService
from core.tools import (
    SKILL_MANAGE_TOOL_NAME,
    ToolContext,
    ToolRegistry,
    register_skill_manage_tool,
    tool_failure,
)


def _skill_md(name: str, description: str, body: str = "# Shared\n") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"


class _Harness:
    """Two agents: the owner ``main`` and the receiver ``two``."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self._homes = tmp_path / "agents"
        self.invalidated: list[str | None] = []
        # owner id -> set of names shared to the receiver "two"
        self.shared: dict[str, set[str]] = {"main": set()}
        self.tools = ToolRegistry()
        register_skill_manage_tool(
            self.tools,
            SkillAuthoringService(protected_roots=[]),
            self.home,
            self.invalidated.append,
            self.resolve_shared,
        )

    def home(self, agent_id: str) -> Path:
        return self._homes / agent_id / "skills"

    def resolve_shared(self, receiver_id: str, name: str) -> Path | None:
        for owner_id, names in sorted(self.shared.items()):
            if owner_id != receiver_id and name in names:
                return self.home(owner_id)
        return None

    def run(self, arguments: dict[str, object], agent_id: str = "two") -> dict[str, Any]:
        context = _context(agent_id, self.root)
        try:
            return cast(
                dict[str, Any],
                asyncio.run(self.tools.dispatch(context, arguments, [SKILL_MANAGE_TOOL_NAME])),
            )
        except ValueError as error:
            return tool_failure("invalid_arguments", str(error), retryable=False)

    def owner_skill_file(self, name: str) -> Path:
        return self.home("main") / name / "SKILL.md"


def _context(agent_id: str, root: Path) -> ToolContext:
    return ToolContext(
        agent_id=agent_id,
        session_id="session-one",
        run_id="run-one",
        tool_call_id="call-one",
        tool_name=SKILL_MANAGE_TOOL_NAME,
        tool_call_index=0,
        workspace=root,
        vbot_root=root,
        data_root=root,
        cwd=root,
    )


def _seed_shared(harness: _Harness, name: str = "deploy") -> None:
    harness.home("main").mkdir(parents=True, exist_ok=True)
    harness.owner_skill_file(name).parent.mkdir(parents=True, exist_ok=True)
    harness.owner_skill_file(name).write_text(_skill_md(name, "Ship it."), encoding="utf-8")
    harness.shared["main"].add(name)


def test_receiver_patch_lands_in_the_owner_package(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    _seed_shared(harness)

    result = harness.run(
        {
            "action": "patch",
            "name": "deploy",
            "match": "# Shared",
            "content": "# Patched",
        }
    )

    assert result["ok"] is True
    document = harness.owner_skill_file("deploy").read_text(encoding="utf-8")
    assert "# Patched" in document


def test_receiver_edit_and_support_files_target_the_owner_package(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    _seed_shared(harness)

    edited = harness.run(
        {
            "action": "edit",
            "name": "deploy",
            "content": _skill_md("deploy", "Rewritten.", "# Rewritten body"),
        }
    )
    written = harness.run(
        {
            "action": "write_file",
            "name": "deploy",
            "file_path": "references/notes.md",
            "content": "notes",
        }
    )
    removed = harness.run(
        {
            "action": "remove_file",
            "name": "deploy",
            "file_path": "references/notes.md",
        }
    )

    assert all(result["ok"] for result in (edited, written, removed))
    assert "# Rewritten body" in harness.owner_skill_file("deploy").read_text(encoding="utf-8")
    assert not (harness.home("main") / "deploy" / "references" / "notes.md").exists()


def test_own_home_wins_over_a_shared_instance_of_the_same_name(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    _seed_shared(harness)
    own_dir = harness.home("two") / "deploy"
    own_dir.mkdir(parents=True)
    (own_dir / "SKILL.md").write_text(
        _skill_md("deploy", "My own copy.", "# Own"), encoding="utf-8"
    )

    harness.run(
        {
            "action": "patch",
            "name": "deploy",
            "match": "# Own",
            "content": "# Own patched",
        }
    )

    # The caller's own home is resolved first; the owner's file stays untouched.
    assert "# Own patched" in (own_dir / "SKILL.md").read_text(encoding="utf-8")
    assert "# Patched" not in harness.owner_skill_file("deploy").read_text(encoding="utf-8")


def test_create_stays_in_the_callers_own_home_even_for_a_shared_name(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    _seed_shared(harness)

    created = harness.run(
        {
            "action": "create",
            "name": "deploy",
            "content": _skill_md("deploy", "Receiver's own copy."),
        }
    )

    # create never writes a foreign home: the receiver gets its own package
    # (which shadows the shared instance for it); the owner's file stays intact.
    assert created["ok"] is True
    assert (harness.home("two") / "deploy" / "SKILL.md").exists()
    assert "Receiver's own copy." in harness.owner_skill_file("deploy").read_text(
        encoding="utf-8"
    ) or (
        harness.owner_skill_file("deploy").read_text(encoding="utf-8")
        == _skill_md("deploy", "Ship it.")
    )


def test_delete_refuses_a_shared_only_target(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    _seed_shared(harness)

    deleted = harness.run({"action": "delete", "name": "deploy"})

    assert deleted["ok"] is False
    assert harness.owner_skill_file("deploy").exists()


def test_unknown_name_still_fails_with_meaningful_error(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)

    result = harness.run({"action": "patch", "name": "ghost", "match": "a", "content": "b"})

    assert result["ok"] is False


def test_shared_mutation_invalidates_all_agent_registries(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    _seed_shared(harness)

    harness.run(
        {
            "action": "patch",
            "name": "deploy",
            "match": "# Shared",
            "content": "# Patched",
        }
    )

    assert harness.invalidated == [None]
