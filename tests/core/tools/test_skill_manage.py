"""Tests for package-oriented vBot Skill authoring through ``skill_manage``."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, cast

from core.providers.tool_schema import sanitize_anthropic_tool_input_schema
from core.skills.authoring import SkillAuthoringService
from core.skills.skills import SkillRegistry
from core.storage.layout import DataDirectoryLayout
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


def _request(operation: str, **arguments: object) -> dict[str, object]:
    return {"request": {"operation": operation, **arguments}}


class _Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self._homes = tmp_path / "agents"
        self._global = tmp_path / "skills"
        self.invalidated: list[str] = []
        self.reloaded = 0
        self.tools = ToolRegistry()
        register_skill_manage_tool(
            self.tools,
            SkillAuthoringService(
                protected_roots=[tmp_path / "resources" / "skills"],
                drafts_root=DataDirectoryLayout(tmp_path).skill_drafts,
                archive_root=tmp_path / "archive",
            ),
            self.home,
            self.invalidated.append,
            lambda: self._global,
            self._on_reload,
        )

    def home(self, agent_id: str) -> Path:
        return self._homes / agent_id / "skills"

    def global_home(self) -> Path:
        return self._global

    def _on_reload(self) -> None:
        self.reloaded += 1

    def run(self, arguments: dict[str, object], agent_id: str = "main") -> dict[str, Any]:
        context = _context(agent_id, self.root)
        try:
            return cast(
                dict[str, Any],
                asyncio.run(self.tools.dispatch(context, arguments, [SKILL_MANAGE_TOOL_NAME])),
            )
        except ValueError as error:
            return tool_failure("invalid_arguments", str(error), retryable=False)

    def begin(self, *, name: str = "demo", mode: str = "create", scope: str = "own") -> str:
        result = self.run(_request("begin", name=name, mode=mode, scope=scope))
        assert result["ok"] is True
        return cast(str, cast(dict[str, Any], result["data"])["draft_id"])

    def put_skill_md(
        self,
        draft_id: str,
        *,
        content: str | None = None,
        scope: str = "own",
    ) -> None:
        result = self.run(
            _request(
                "put_file",
                draft_id=draft_id,
                path="SKILL.md",
                content=content if content is not None else _skill_md(),
                scope=scope,
            )
        )
        assert result["ok"] is True

    def commit(self, draft_id: str, *, scope: str = "own") -> dict[str, Any]:
        validated = self.run(_request("validate", draft_id=draft_id, scope=scope))
        assert validated["ok"] is True
        result = self.run(_request("commit", draft_id=draft_id, scope=scope))
        assert result["ok"] is True
        return result


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


def test_provider_schema_exposes_one_strict_object_per_operation(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    definitions = harness.tools.provider_definitions([SKILL_MANAGE_TOOL_NAME])
    parameters = cast(dict[str, Any], definitions[0]["parameters"])
    branches = cast(
        list[dict[str, Any]],
        parameters["properties"]["request"]["anyOf"],
    )
    operations = {branch["properties"]["operation"]["enum"][0]: branch for branch in branches}
    expected_fields = {
        "inspect": {"scope", "name", "draft_id", "path"},
        "begin": {"scope", "name", "mode", "source"},
        "put_file": {
            "scope",
            "draft_id",
            "path",
            "content",
            "source_path",
            "executable",
        },
        "patch": {"scope", "draft_id", "path", "old_string", "new_string"},
        "remove_file": {"scope", "draft_id", "path"},
        "validate": {"scope", "draft_id"},
        "commit": {"scope", "draft_id"},
        "abort": {"scope", "draft_id"},
        "delete": {"scope", "name"},
    }
    expected_required = {
        "inspect": set(),
        "begin": {"name", "mode"},
        "put_file": {"draft_id", "path"},
        "patch": {"draft_id", "old_string", "new_string"},
        "remove_file": {"draft_id", "path"},
        "validate": {"draft_id"},
        "commit": {"draft_id"},
        "abort": {"draft_id"},
        "delete": {"name"},
    }

    assert parameters == SKILL_MANAGE_TOOL_PARAMETERS
    assert parameters["required"] == ["request"]
    assert parameters["additionalProperties"] is False
    assert set(operations) == set(expected_fields)
    for operation, schema in operations.items():
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert set(cast(dict[str, Any], schema["properties"])) == {
            "operation",
            *expected_fields[operation],
        }
        assert set(cast(list[str], schema["required"])) == {
            "operation",
            *expected_required[operation],
        }


def test_provider_schema_structurally_models_exclusive_arguments() -> None:
    branches = SKILL_MANAGE_TOOL_PARAMETERS["properties"]["request"]["anyOf"]
    operations = {branch["properties"]["operation"]["enum"][0]: branch for branch in branches}

    assert operations["inspect"]["oneOf"] == [
        {"required": ["name"]},
        {"required": ["draft_id"]},
    ]
    assert operations["put_file"]["oneOf"] == [
        {"required": ["content"]},
        {"required": ["source_path"]},
    ]
    assert (
        sanitize_anthropic_tool_input_schema(
            SKILL_MANAGE_TOOL_PARAMETERS,
            tool_name=SKILL_MANAGE_TOOL_NAME,
        )
        == SKILL_MANAGE_TOOL_PARAMETERS
    )


def test_flat_or_multiple_operation_calls_are_rejected(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)

    flat = harness.run({"operation": "begin", "name": "demo", "mode": "create"})
    multiple = harness.run(
        {
            "begin": {"name": "demo", "mode": "create"},
            "delete": {"name": "demo"},
        }
    )

    assert flat["ok"] is False
    assert "'request' is a required property" in cast(dict[str, Any], flat["error"])["message"]
    assert multiple["ok"] is False
    assert "'request' is a required property" in cast(dict[str, Any], multiple["error"])["message"]


def test_begin_requires_name_and_mode_at_dispatch(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)

    missing_name = harness.run(_request("begin", mode="create"))
    missing_mode = harness.run(_request("begin", name="demo"))

    for result, field_name in ((missing_name, "name"), (missing_mode, "mode")):
        assert result["ok"] is False
        error = cast(dict[str, Any], result["error"])
        assert error["code"] == "invalid_arguments"
        assert error["retryable"] is False
        assert f"'{field_name}' is a required property" in error["message"]


def test_inspect_rejects_name_and_draft_id_together(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    draft_id = harness.begin()

    result = harness.run(_request("inspect", name="demo", draft_id=draft_id))

    assert result["ok"] is False
    assert "[oneOf]" in cast(dict[str, Any], result["error"])["message"]


def test_create_package_is_invisible_until_commit(
    tmp_path: Path,
    caplog: Any,
) -> None:
    harness = _Harness(tmp_path)
    draft_id = harness.begin()
    harness.put_skill_md(draft_id, content=_skill_md(body="private body"))

    assert not (harness.home("main") / "demo").exists()
    assert harness.invalidated == []

    with caplog.at_level(logging.INFO, logger="vbot.tools.skill_manage"):
        result = harness.commit(draft_id)

    assert cast(dict[str, Any], result["data"])["operation"] == "commit"
    assert (harness.home("main") / "demo" / "SKILL.md").is_file()
    assert harness.invalidated == ["main"]
    messages = [
        record.getMessage() for record in caplog.records if record.name == "vbot.tools.skill_manage"
    ]
    assert messages == [
        "Skill package mutated (skill=demo scope=own operation=commit actor_agent=main)"
    ]
    assert "private body" not in caplog.text


def test_complete_package_is_loadable_after_commit(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    draft_id = harness.begin()
    harness.put_skill_md(draft_id)
    support = harness.run(
        _request(
            "put_file",
            draft_id=draft_id,
            path="references/notes.md",
            content="Useful notes\n",
        )
    )

    harness.commit(draft_id)

    assert support["ok"] is True
    assert SkillRegistry.load(harness.home("main")).get("demo").description == "Do a demo task."
    assert (harness.home("main") / "demo" / "references" / "notes.md").read_text(
        encoding="utf-8"
    ) == "Useful notes\n"


def test_update_draft_does_not_leak_partial_changes(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    create_id = harness.begin()
    harness.put_skill_md(create_id, content=_skill_md(body="old marker"))
    harness.commit(create_id)
    harness.invalidated.clear()

    update_id = harness.begin(mode="update")
    patched = harness.run(
        _request(
            "patch",
            draft_id=update_id,
            old_string="old marker",
            new_string="new marker",
        )
    )

    assert patched["ok"] is True
    assert "old marker" in (harness.home("main") / "demo" / "SKILL.md").read_text(encoding="utf-8")
    assert harness.invalidated == []

    harness.commit(update_id)
    assert "new marker" in (harness.home("main") / "demo" / "SKILL.md").read_text(encoding="utf-8")
    assert harness.invalidated == ["main"]


def test_binary_asset_copies_from_workspace_without_utf8_conversion(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    source = tmp_path / "logo.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")
    draft_id = harness.begin()
    harness.put_skill_md(draft_id)

    result = harness.run(
        _request(
            "put_file",
            draft_id=draft_id,
            path="assets/logo.png",
            source_path=str(source),
        )
    )
    harness.commit(draft_id)

    file_data = cast(dict[str, Any], cast(dict[str, Any], result["data"])["file"])
    assert file_data["binary"] is True
    assert (
        harness.home("main") / "demo" / "assets" / "logo.png"
    ).read_bytes() == source.read_bytes()


def test_source_path_outside_workspace_is_rejected(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    draft_id = harness.begin()

    result = harness.run(
        _request(
            "put_file",
            draft_id=draft_id,
            path="references/source.py",
            source_path=str(Path(__file__).resolve()),
        )
    )

    assert result["ok"] is False
    assert "current Project or Workspace" in cast(dict[str, Any], result["error"])["message"]


def test_non_vbot_package_path_is_rejected(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    draft_id = harness.begin()

    result = harness.run(
        _request(
            "put_file",
            draft_id=draft_id,
            path="agents/openai.yaml",
            content="interface: {}\n",
        )
    )

    assert result["ok"] is False
    assert "[pattern]" in cast(dict[str, Any], result["error"])["message"]


def test_inspect_returns_manifest_and_selected_text(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    draft_id = harness.begin()
    harness.put_skill_md(draft_id)
    harness.run(
        _request(
            "put_file",
            draft_id=draft_id,
            path="scripts/run.py",
            content="print('ok')\n",
            executable=True,
        )
    )

    result = harness.run(
        _request(
            "inspect",
            draft_id=draft_id,
            path="scripts/run.py",
        )
    )

    data = cast(dict[str, Any], result["data"])
    files = cast(list[dict[str, Any]], data["files"])
    assert result["ok"] is True
    assert data["selected_content"] == "print('ok')\n"
    assert [item["path"] for item in files] == ["SKILL.md", "scripts/run.py"]
    assert files[1]["executable"] is (os.name != "nt")


def test_invalid_draft_cannot_commit(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    draft_id = harness.begin()
    harness.put_skill_md(draft_id, content="---\nname: demo\n---\n\nbody\n")

    result = harness.run(_request("commit", draft_id=draft_id))

    assert result["ok"] is False
    assert "description" in cast(dict[str, Any], result["error"])["message"]
    assert not (harness.home("main") / "demo").exists()
    assert harness.invalidated == []


def test_draft_is_bound_to_scope_and_agent(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    draft_id = harness.begin()

    wrong_scope = harness.run(_request("inspect", draft_id=draft_id, scope="global"))
    wrong_agent = harness.run(
        _request("inspect", draft_id=draft_id),
        agent_id="other",
    )

    assert wrong_scope["ok"] is False
    assert "different scope" in cast(dict[str, Any], wrong_scope["error"])["message"]
    assert wrong_agent["ok"] is False
    assert "different agent" in cast(dict[str, Any], wrong_agent["error"])["message"]


def test_abort_discards_draft_without_invalidation(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    draft_id = harness.begin()

    result = harness.run(_request("abort", draft_id=draft_id))

    assert result["ok"] is True
    assert harness.invalidated == []
    missing = harness.run(_request("inspect", draft_id=draft_id))
    assert missing["ok"] is False


def test_delete_archives_complete_skill_and_invalidates(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    draft_id = harness.begin()
    harness.put_skill_md(draft_id)
    harness.commit(draft_id)
    harness.invalidated.clear()

    result = harness.run(_request("delete", name="demo"))

    data = cast(dict[str, Any], result["data"])
    archive_path = Path(cast(str, data["archive_path"]))
    assert result["ok"] is True
    assert not (harness.home("main") / "demo").exists()
    assert (archive_path / "SKILL.md").is_file()
    assert harness.invalidated == ["main"]


def test_global_commit_reloads_global_pool(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    draft_id = harness.begin(scope="global")
    harness.put_skill_md(draft_id, scope="global")

    harness.commit(draft_id, scope="global")

    assert (harness.global_home() / "demo" / "SKILL.md").is_file()
    assert harness.reloaded == 1
    assert harness.invalidated == []


def test_removed_direct_create_operation_is_rejected(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)

    result = harness.run(_request("create", name="demo", content=_skill_md()))

    assert result["ok"] is False
    assert cast(dict[str, Any], result["error"])["code"] == "invalid_arguments"
