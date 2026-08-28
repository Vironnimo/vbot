"""Tests for direct vBot Skill authoring through ``skill_manage``."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from core.providers.tool_schema import sanitize_anthropic_tool_input_schema
from core.skills.authoring import SkillAuthoringService
from core.skills.skills import SkillRegistry
from core.tools import (
    SKILL_MANAGE_TOOL_NAME,
    SKILL_MANAGE_TOOL_PARAMETERS,
    ToolContext,
    ToolRegistry,
    register_skill_manage_tool,
    tool_failure,
)


def _skill_md(
    name: str = "demo",
    description: str = "Do a demo task.",
    body: str = "# Demo\n",
) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"


class _Harness:
    def __init__(
        self,
        tmp_path: Path,
        resolve_external_skill_scope: (Callable[[str, str, str | None], str | None] | None) = None,
    ) -> None:
        self.root = tmp_path
        self._homes = tmp_path / "agents"
        self.invalidated: list[str | None] = []
        self.tools = ToolRegistry()
        register_skill_manage_tool(
            self.tools,
            SkillAuthoringService(
                protected_roots=[tmp_path / "resources" / "skills"],
            ),
            self.home,
            self.invalidated.append,
            resolve_external_skill_scope=resolve_external_skill_scope,
        )

    def home(self, agent_id: str) -> Path:
        return self._homes / agent_id / "skills"

    def run(self, arguments: dict[str, object], agent_id: str = "main") -> dict[str, Any]:
        context = _context(agent_id, self.root)
        try:
            return cast(
                dict[str, Any],
                asyncio.run(
                    self.tools.dispatch(
                        context,
                        arguments,
                        [SKILL_MANAGE_TOOL_NAME],
                    )
                ),
            )
        except ValueError as error:
            return tool_failure("invalid_arguments", str(error), retryable=False)

    def create(
        self,
        *,
        name: str = "demo",
        content: str | None = None,
    ) -> dict[str, Any]:
        return self.run(
            {
                "action": "create",
                "name": name,
                "content": content if content is not None else _skill_md(name=name),
            }
        )


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


def test_provider_schema_is_flat_and_hermes_shaped(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    definitions = harness.tools.provider_definitions([SKILL_MANAGE_TOOL_NAME])
    parameters = cast(dict[str, Any], definitions[0]["parameters"])

    assert parameters == SKILL_MANAGE_TOOL_PARAMETERS
    assert parameters["type"] == "object"
    assert "oneOf" not in parameters
    assert "additionalProperties" not in parameters
    properties = parameters["properties"]
    assert set(properties) == {
        "action",
        "name",
        "content",
        "file_path",
        "match",
    }
    assert properties["action"]["enum"] == [
        "create",
        "edit",
        "patch",
        "write_file",
        "remove_file",
        "delete",
    ]
    assert parameters["required"] == ["action", "name"]
    assert "default" not in str(parameters)
    assert all(
        isinstance(property_schema.get("description"), str) and property_schema["description"]
        for property_schema in properties.values()
    )
    assert "draft_id" not in str(parameters)
    assert "source_path" not in str(parameters)
    assert "executable" not in str(parameters)
    assert (
        sanitize_anthropic_tool_input_schema(
            SKILL_MANAGE_TOOL_PARAMETERS,
            tool_name=SKILL_MANAGE_TOOL_NAME,
        )
        == SKILL_MANAGE_TOOL_PARAMETERS
    )


def test_create_is_immediately_live_and_invalidates(
    tmp_path: Path,
    caplog: Any,
) -> None:
    harness = _Harness(tmp_path)

    with caplog.at_level(logging.INFO, logger="vbot.tools.skill_manage"):
        result = harness.create(content=_skill_md(body="private body"))

    data = cast(dict[str, Any], result["data"])
    assert result["ok"] is True
    assert data == {
        "action": "create",
        "name": "demo",
        "scope": "own",
        "warnings": [],
        "message": "Skill 'demo' created.",
        "file_path": "SKILL.md",
    }
    assert (harness.home("main") / "demo" / "SKILL.md").is_file()
    assert SkillRegistry.load(harness.home("main")).get("demo").description == "Do a demo task."
    assert harness.invalidated == ["main"]
    assert "action=create" in caplog.text
    assert "private body" not in caplog.text
    assert str(harness.home("main")) not in str(result)


def test_create_requires_content(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)

    result = harness.run({"action": "create", "name": "demo"})

    assert result == tool_failure(
        "invalid_arguments",
        "content must be a string",
        retryable=False,
    )
    assert not harness.home("main").exists()
    assert harness.invalidated == []


def test_missing_description_uses_body_and_invalidates(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)

    result = harness.create(content="---\nname: demo\n---\n\nbody\n")

    assert result["ok"] is True
    assert cast(dict[str, Any], result["data"])["warnings"] == [
        "Skill metadata missing description; using the first body text line."
    ]
    assert SkillRegistry.load(harness.home("main")).get("demo").description == "body"
    assert harness.invalidated == ["main"]


def test_write_read_and_remove_support_file(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    assert harness.create()["ok"] is True
    harness.invalidated.clear()

    written = harness.run(
        {
            "action": "write_file",
            "name": "demo",
            "file_path": "references/notes.md",
            "content": "Useful notes\n",
        }
    )
    removed = harness.run(
        {
            "action": "remove_file",
            "name": "demo",
            "file_path": "references/notes.md",
        }
    )

    assert written["ok"] is True
    assert cast(dict[str, Any], written["data"])["file_path"] == "references/notes.md"
    assert removed["ok"] is True
    assert not (harness.home("main") / "demo" / "references").exists()
    assert harness.invalidated == ["main", "main"]


def test_write_file_rejects_removed_binary_copy_arguments(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.create()

    source = harness.run(
        {
            "action": "write_file",
            "name": "demo",
            "file_path": "assets/logo.png",
            "source_path": "logo.png",
        }
    )
    executable = harness.run(
        {
            "action": "write_file",
            "name": "demo",
            "file_path": "scripts/run.py",
            "content": "print('ok')\n",
            "executable": True,
        }
    )

    for result, field in ((source, "source_path"), (executable, "executable")):
        assert result["ok"] is False
        assert field in cast(dict[str, Any], result["error"])["message"]


def test_edit_replaces_complete_skill_document(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.create()

    result = harness.run(
        {
            "action": "edit",
            "name": "demo",
            "content": _skill_md(description="Updated.", body="New body.\n"),
        }
    )

    assert result["ok"] is True
    skill_file = harness.home("main") / "demo" / "SKILL.md"
    assert "description: Updated." in skill_file.read_text(encoding="utf-8")
    assert "New body." in skill_file.read_text(encoding="utf-8")


def test_patch_defaults_to_skill_md_and_requires_unique_match(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.create(content=_skill_md(body="old marker\nold marker\n"))

    rejected = harness.run(
        {
            "action": "patch",
            "name": "demo",
            "match": "old marker",
            "content": "new marker",
        }
    )
    replaced = harness.run(
        {
            "action": "patch",
            "name": "demo",
            "match": "old marker\nold marker",
            "content": "new marker\nnew marker",
        }
    )

    assert rejected["ok"] is False
    assert "not unique" in cast(dict[str, Any], rejected["error"])["message"]
    assert replaced["ok"] is True
    assert cast(dict[str, Any], replaced["data"])["file_path"] == "SKILL.md"
    content = (harness.home("main") / "demo" / "SKILL.md").read_text(encoding="utf-8")
    assert content.count("new marker") == 2


def test_patch_support_file_by_relative_path(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.create()
    harness.run(
        {
            "action": "write_file",
            "name": "demo",
            "file_path": "scripts/run.py",
            "content": "print('old')\n",
        }
    )

    result = harness.run(
        {
            "action": "patch",
            "name": "demo",
            "file_path": "scripts/run.py",
            "match": "old",
            "content": "new",
        }
    )

    assert result["ok"] is True
    assert (harness.home("main") / "demo" / "scripts" / "run.py").read_text(
        encoding="utf-8"
    ) == "print('new')\n"


def test_action_rejects_fields_from_another_action(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)

    result = harness.run(
        {
            "action": "delete",
            "name": "demo",
            "content": _skill_md(),
        }
    )

    assert result == tool_failure(
        "invalid_arguments",
        "Unknown delete argument(s): content",
        retryable=False,
    )


def test_non_skill_file_path_is_rejected(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.create()

    result = harness.run(
        {
            "action": "write_file",
            "name": "demo",
            "file_path": "other/data.txt",
            "content": "no",
        }
    )

    assert result["ok"] is False
    assert "[pattern]" in cast(dict[str, Any], result["error"])["message"]


def test_delete_removes_complete_skill_and_invalidates(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.create()
    harness.run(
        {
            "action": "write_file",
            "name": "demo",
            "file_path": "references/notes.md",
            "content": "notes\n",
        }
    )
    harness.invalidated.clear()

    result = harness.run({"action": "delete", "name": "demo"})

    assert result["ok"] is True
    assert cast(dict[str, Any], result["data"])["message"]
    assert not (harness.home("main") / "demo").exists()
    assert harness.invalidated == ["main"]


def test_global_scope_is_not_available_to_agent_tool(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)

    result = harness.run(
        {
            "action": "create",
            "name": "demo",
            "content": _skill_md(),
            "scope": "global",
        }
    )

    assert result == tool_failure(
        "invalid_arguments",
        "Unknown create argument(s): scope",
        retryable=False,
    )
    assert not harness.home("main").exists()
    assert harness.invalidated == []


def test_removed_draft_action_is_rejected(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)

    result = harness.run({"action": "begin", "name": "demo"})

    assert result["ok"] is False
    assert "[enum]" in cast(dict[str, Any], result["error"])["message"]


def _scope_resolver(
    mapping: dict[str, str],
) -> Callable[[str, str, str | None], str | None]:
    def resolve(agent_id: str, name: str, project_id: str | None) -> str | None:
        return mapping.get(name)

    return resolve


def test_edit_foreign_bundled_name_reports_scope_not_notfound(tmp_path: Path) -> None:
    harness = _Harness(
        tmp_path,
        resolve_external_skill_scope=_scope_resolver({"bundle-me": "bundled"}),
    )

    result = harness.run(
        {"action": "edit", "name": "bundle-me", "content": _skill_md(name="bundle-me")}
    )

    assert result["ok"] is False
    error = cast(dict[str, Any], result["error"])
    assert error["code"] == "skill_write_rejected"
    assert error["retryable"] is False
    assert "Skill 'bundle-me' is a bundled Skill — read-only here." in error["message"]
    assert "user-facing Skill controls" in error["message"]
    assert "do not edit the package with file or shell tools" in error["message"]
    assert "not found" not in error["message"]


def test_edit_foreign_project_name_reports_scope(tmp_path: Path) -> None:
    harness = _Harness(
        tmp_path,
        resolve_external_skill_scope=_scope_resolver({"project-me": "project"}),
    )

    result = harness.run({"action": "patch", "name": "project-me", "match": "x", "content": "y"})

    assert result["ok"] is False
    error = cast(dict[str, Any], result["error"])
    assert error["code"] == "skill_write_rejected"
    assert "is a Project Skill — read-only here." in error["message"]


def test_delete_shared_name_reports_owner_limited(tmp_path: Path) -> None:
    harness = _Harness(
        tmp_path,
        resolve_external_skill_scope=_scope_resolver({"shared-me": "shared"}),
    )

    result = harness.run({"action": "delete", "name": "shared-me"})

    assert result["ok"] is False
    error = cast(dict[str, Any], result["error"])
    assert error["code"] == "skill_write_rejected"
    assert "is shared with you" in error["message"]
    assert "only its owner or the user can delete it" in error["message"]


def test_unknown_name_keeps_plain_notfound(tmp_path: Path) -> None:
    harness = _Harness(
        tmp_path,
        resolve_external_skill_scope=_scope_resolver({"bundle-me": "bundled"}),
    )

    result = harness.run(
        {"action": "edit", "name": "no-such-skill", "content": _skill_md(name="no-such-skill")}
    )

    assert result["ok"] is False
    error = cast(dict[str, Any], result["error"])
    assert error["code"] == "skill_write_rejected"
    assert "Skill 'no-such-skill' not found." in error["message"]


def test_create_shadows_foreign_name_unblocked_by_scope_check(tmp_path: Path) -> None:
    harness = _Harness(
        tmp_path,
        resolve_external_skill_scope=_scope_resolver({"bundle-me": "bundled"}),
    )

    result = harness.create(name="bundle-me")

    assert result["ok"] is True
