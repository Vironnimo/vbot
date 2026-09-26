"""Tests for direct vBot Skill authoring through ``skill_manage``."""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

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


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_admitted_write_runs_off_loop_and_settles_before_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    service = SkillAuthoringService()
    registry = ToolRegistry()
    entered = asyncio.Event()
    release = threading.Event()
    invalidated: list[str | None] = []
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    create = service.create

    def blocked_create(*args: Any, **kwargs: Any) -> Any:
        loop.call_soon_threadsafe(entered.set)
        assert threading.get_ident() != loop_thread
        assert release.wait(5)
        return create(*args, **kwargs)

    monkeypatch.setattr(service, "create", blocked_create)
    register_skill_manage_tool(registry, service, lambda _: tmp_path, invalidated.append)
    task = asyncio.create_task(
        registry.dispatch(
            _context("main", tmp_path),
            {"action": "create", "name": "demo", "content": _skill_md()},
            [SKILL_MANAGE_TOOL_NAME],
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not task.done()
        assert not (tmp_path / "demo").exists()
        assert invalidated == []
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert (await task)["ok"] is True
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert SkillRegistry.load(tmp_path).get("demo") is not None
    assert invalidated == ["main"]


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
        "old_string",
        "new_string",
        "replace_all",
        "file_path",
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


def test_patch_empty_content_removes_only_the_selected_text(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.create(content=_skill_md(body="Keep this step.\nObsolete step.\n"))
    result = harness.run(
        {"action": "patch", "name": "demo", "match": "Obsolete step.\n", "content": ""}
    )
    assert result["ok"] is True
    text = (harness.home("main") / "demo/SKILL.md").read_text(encoding="utf-8")
    assert "Obsolete step." not in text
    assert "Keep this step." in text


@pytest.mark.parametrize("existing", [False, True])
def test_write_file_preserves_intentional_empty_content(tmp_path: Path, existing: bool) -> None:
    harness = _Harness(tmp_path)
    harness.create()
    arguments = {"action": "write_file", "name": "demo", "file_path": "assets/empty.txt"}
    if existing:
        assert harness.run({**arguments, "content": "old contents"})["ok"] is True
    result = harness.run({**arguments, "content": ""})
    assert result["ok"] is True
    assert (harness.home("main") / "demo/assets/empty.txt").read_bytes() == b""


@pytest.mark.parametrize("action", ["patch", "write_file"])
@pytest.mark.parametrize("extra", [{}, {"content": None}, {"content": False}])
def test_content_must_be_present_and_textual(
    tmp_path: Path, action: str, extra: dict[str, object]
) -> None:
    harness = _Harness(tmp_path)
    harness.create()
    skill_path = harness.home("main") / "demo/SKILL.md"
    before = skill_path.read_bytes()
    fields = {"match": "Demo"} if action == "patch" else {"file_path": "assets/empty.txt"}
    result = harness.run({"action": action, "name": "demo", **fields, **extra})
    assert result["ok"] is False
    assert skill_path.read_bytes() == before
    assert not (skill_path.parent / "assets/empty.txt").exists()


def test_create_is_immediately_live_and_invalidates(
    tmp_path: Path,
    caplog: Any,
) -> None:
    harness = _Harness(tmp_path)

    with caplog.at_level(logging.INFO, logger="vbot.tools.skill_manage"):
        result = harness.create(content=_skill_md(body="private body"))

    assert result["ok"] is True
    assert result["data"] == {"content": "Created Skill 'demo'."}
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
        "create needs content. SKILL.md starts with front matter holding name and a "
        "description of when to load the Skill, then the instructions:\n---\nname: demo\n"
        "description: <what it covers and when to load it>\n---\n\n<instructions>",
        retryable=False,
    )
    assert not harness.home("main").exists()
    assert harness.invalidated == []


@pytest.mark.parametrize("content", ["---\nname: demo\n---\n\nbody\n", "# Demo\n\nbody\n"])
def test_missing_description_is_refused_with_the_header(tmp_path: Path, content: str) -> None:
    harness = _Harness(tmp_path)

    result = harness.create(content=content)

    assert result["ok"] is False
    message = result["error"]["message"]
    assert "---\nname: demo\ndescription: <what it covers and when to load it>\n---" in message
    assert not harness.home("main").exists()
    assert harness.invalidated == []


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

    assert written["data"] == {"content": "Wrote references/notes.md of Skill 'demo'."}
    assert removed["data"] == {"content": "Removed references/notes.md from Skill 'demo'."}
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
    assert rejected["error"]["code"] == "ambiguous_match"
    assert replaced["data"] == {"content": "Patched SKILL.md of Skill 'demo' at line 9."}
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
        "delete removes the whole Skill and takes no text. Use edit or patch to change it instead.",
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
    assert result["error"]["code"] == "skill_write_rejected"
    assert (
        "Support files must live under scripts/ or references/ or assets/"
        in (result["error"]["message"])
    )
    assert not (harness.home("main") / "demo/other").exists()


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
    assert result["data"] == {"content": "Deleted Skill 'demo' and its files."}
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
        "skill_manage writes only your own Skills; global, Project and bundled Skills are "
        "read-only here. Omit scope to write one of your own Skills.",
        retryable=False,
    )
    assert not harness.home("main").exists()
    assert harness.invalidated == []


def test_removed_draft_action_is_rejected(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)

    result = harness.run({"action": "begin", "name": "demo"})

    assert result["ok"] is False
    message = cast(dict[str, Any], result["error"])["message"]
    assert '"action" must be one of "create", "edit"' in message
    assert 'received "begin"' in message


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

    assert result == tool_failure(
        "skill_not_found",
        "You have no Skill named 'no-such-skill'; nothing changed. You have no Skills of "
        "your own yet; use action create to add one.",
        retryable=False,
    )


def test_create_shadows_foreign_name_unblocked_by_scope_check(tmp_path: Path) -> None:
    harness = _Harness(
        tmp_path,
        resolve_external_skill_scope=_scope_resolver({"bundle-me": "bundled"}),
    )

    result = harness.create(name="bundle-me")

    assert result["ok"] is True


@pytest.mark.parametrize(
    "arguments",
    [
        {
            "operation": " WRITE-FILE ",
            "name": "demo",
            "filePath": "assets/empty.txt",
            "content": "",
        },
        {
            "request": {
                "action": "write_file",
                "name": "demo",
                "file_pth": "assets/empty.txt",
                "content": "",
            }
        },
        {"write_file": {"name": "demo", "file_path": "assets/empty.txt", "content": ""}},
        {
            "action": "write_file",
            "name": " demo ",
            "file_path": '"assets\\empty.txt"',
            "content": "",
        },
    ],
)
def test_recognizable_mistakes_write_real_empty_file(
    tmp_path: Path, arguments: dict[str, object]
) -> None:
    harness = _Harness(tmp_path)
    assert harness.create()["ok"] is True
    assert harness.run(arguments)["ok"] is True
    assert (harness.home("main") / "demo/assets/empty.txt").read_bytes() == b""


@pytest.mark.parametrize(
    ("file_path", "recorded"),
    [
        ('"assets\\empty.txt"', "assets/empty.txt"),
        ("./demo/references/notes.md", "references/notes.md"),
        ("other/references/notes.md", "other/references/notes.md"),
    ],
)
def test_normalized_call_records_the_package_path_it_writes(
    tmp_path: Path, file_path: str, recorded: str
) -> None:
    normalizer = _Harness(tmp_path).tools.get(SKILL_MANAGE_TOOL_NAME).argument_normalizer
    assert normalizer is not None

    normalized = normalizer(
        {"action": "write_file", "name": "demo", "file_path": file_path, "content": "x"}
    )

    assert normalized["file_path"] == recorded


# --- Patch tolerance --------------------------------------------------------

_PATCH_BODY = (
    '# Demo\n\n1. Run `pytest`.\n2. Tag the release: "v1".\n\n## Pitfalls\n\n'
    "- Never deploy on Fridays.\n- Check the Friday calendar.\n"
)
_STAMPED_HEAD = (
    "---\nname: demo\ndescription: Do a demo task.\nmetadata:\n  vbot:\n    author: agent\n---\n\n"
)


def _patch_harness(tmp_path: Path, body: str = _PATCH_BODY) -> tuple[_Harness, Path]:
    harness = _Harness(tmp_path)
    assert harness.create(content=_skill_md(body=body))["ok"] is True
    return harness, harness.home("main") / "demo" / "SKILL.md"


def _body(skill_file: Path) -> str:
    text = skill_file.read_bytes().decode("utf-8")
    assert text.startswith(_STAMPED_HEAD)
    return text[len(_STAMPED_HEAD) :]


@pytest.mark.parametrize(
    ("old_string", "new_string", "expected_line"),
    [
        (
            "## Pitfalls  \r\n\r\n- Never deploy on Fridays.",
            "## Pitfalls\r\n\r\n- Never deploy on weekends.",
            "- Never deploy on weekends.",
        ),
        (
            "2. Tag the release: “v1”.",
            "2. Tag the release: “v2”.",
            '2. Tag the release: "v2".',
        ),
    ],
)
def test_patch_applies_text_copied_with_other_line_endings_or_quotes(
    tmp_path: Path, old_string: str, new_string: str, expected_line: str
) -> None:
    harness, skill_file = _patch_harness(tmp_path)

    result = harness.run(
        {"action": "patch", "name": "demo", "old_string": old_string, "new_string": new_string}
    )

    assert result["ok"] is True
    assert "Note:" not in result["data"]["content"]
    body = _body(skill_file)
    assert expected_line in body.split("\n")
    assert "\r" not in body
    assert "“" not in body


def test_patch_that_changes_nothing_says_so(tmp_path: Path) -> None:
    harness, skill_file = _patch_harness(tmp_path, body="Say “hello” to users.\n")

    result = harness.run(
        {
            "action": "patch",
            "name": "demo",
            "old_string": 'Say "hello" to users.',
            "new_string": "Say “hello” to users.",
        }
    )

    assert result["ok"] is True
    assert result["data"]["content"] == (
        "SKILL.md of Skill 'demo' already reads as new_string at line 9; nothing changed."
    )
    assert _body(skill_file) == "Say “hello” to users.\n"


def test_patch_keeps_the_files_curly_quotes_for_a_plain_copy(tmp_path: Path) -> None:
    harness, skill_file = _patch_harness(tmp_path, body="Say “hello” to users.\n")

    result = harness.run(
        {
            "action": "patch",
            "name": "demo",
            "old_string": 'Say "hello" to users.',
            "new_string": 'Say "hi" to users.',
        }
    )

    assert result["ok"] is True
    assert _body(skill_file) == "Say “hi” to users.\n"


def test_patch_decodes_one_extra_level_of_json_escaping_with_a_note(tmp_path: Path) -> None:
    harness, skill_file = _patch_harness(tmp_path)

    result = harness.run(
        {
            "action": "patch",
            "name": "demo",
            "old_string": '2. Tag the release: \\"v1\\".\\n\\n## Pitfalls',
            "new_string": '2. Tag the release: \\"v2\\".\\n\\n## Pitfalls',
        }
    )

    assert result["data"] == {
        "content": "Patched SKILL.md of Skill 'demo' at line 12.\n"
        "Note: old_string and new_string arrived with an extra level of JSON escaping "
        '(such as \\n or \\"); they were applied unescaped.'
    }
    assert '2. Tag the release: "v2".\n\n## Pitfalls' in _body(skill_file)


def test_patch_keeps_literal_escapes_that_match_the_file(tmp_path: Path) -> None:
    harness, skill_file = _patch_harness(tmp_path, body='Run `printf "a\\n"` first.\n')

    result = harness.run(
        {
            "action": "patch",
            "name": "demo",
            "old_string": 'printf "a\\n"',
            "new_string": 'printf "b\\n"',
        }
    )

    assert result["data"] == {"content": "Patched SKILL.md of Skill 'demo' at line 9."}
    assert _body(skill_file) == 'Run `printf "b\\n"` first.\n'


def test_patch_applies_old_string_copied_with_a_misspelling_and_names_it(tmp_path: Path) -> None:
    harness, skill_file = _patch_harness(
        tmp_path, body="Always run the full test suite before merging.\n"
    )

    result = harness.run(
        {
            "action": "patch",
            "name": "demo",
            "old_string": "Always run the full test suite befor merging.",
            "new_string": "Always run the full test suite and the linter before merging.",
        }
    )

    assert result["ok"] is True
    assert _body(skill_file) == "Always run the full test suite and the linter before merging.\n"
    content = result["data"]["content"]
    assert "\nNote: Line " in content
    assert (
        "did not match your old text exactly and was edited anyway; it read: Always run the "
        "full test suite before merging." in content
    )


def test_patch_refuses_a_copy_resembling_several_places(tmp_path: Path) -> None:
    line = "Tag the release with the version number and the changelog entry."
    harness, skill_file = _patch_harness(tmp_path, body=f"{line}\n\n{line}\n")
    before = skill_file.read_bytes()

    result = harness.run(
        {
            "action": "patch",
            "name": "demo",
            "old_string": line.replace("version", "verison"),
            "new_string": "Tag the release.",
        }
    )

    assert result["error"]["code"] == "ambiguous_match"
    assert result["error"]["message"].startswith(
        "old_string does not match exactly and resembles 2 places in SKILL.md of Skill 'demo'"
    )
    assert skill_file.read_bytes() == before


def test_patch_miss_names_the_closest_text_and_the_read_call(tmp_path: Path) -> None:
    harness, skill_file = _patch_harness(tmp_path)
    before = skill_file.read_bytes()

    result = harness.run(
        {"action": "patch", "name": "demo", "old_string": "1. Run pytest.", "new_string": "x"}
    )

    assert result == tool_failure(
        "text_not_found",
        "old_string was not found in SKILL.md of Skill 'demo'; nothing changed.\n"
        "Closest text at line 11:\n1. Run `pytest`.\n"
        "Copy the exact current text into old_string. Read the current text with skill "
        '{"name": "demo", "file_path": "SKILL.md"}.',
        retryable=False,
    )
    assert skill_file.read_bytes() == before


def test_patch_miss_points_to_the_support_file_holding_the_text(tmp_path: Path) -> None:
    harness, skill_file = _patch_harness(tmp_path)
    notes = harness.home("main") / "demo" / "references" / "notes.md"
    harness.run(
        {
            "action": "write_file",
            "name": "demo",
            "file_path": "references/notes.md",
            "content": "Rollback with make rollback.\n",
        }
    )

    result = harness.run(
        {
            "action": "patch",
            "name": "demo",
            "old_string": "Rollback with make rollback.",
            "new_string": "Rollback with make undo.",
        }
    )

    assert result["error"]["code"] == "text_not_found"
    assert result["error"]["message"].endswith(
        'The text is in references/notes.md; to patch it there, add "file_path": '
        '"references/notes.md".'
    )
    assert notes.read_text(encoding="utf-8") == "Rollback with make rollback.\n"


def test_ambiguous_patch_changes_nothing_until_replace_all(tmp_path: Path) -> None:
    harness, skill_file = _patch_harness(tmp_path)
    before = skill_file.read_bytes()
    arguments: dict[str, object] = {
        "action": "patch",
        "name": "demo",
        "old_string": "Friday",
        "new_string": "weekend",
    }

    rejected = harness.run(arguments)
    unchanged = skill_file.read_bytes()
    replaced = harness.run({**arguments, "replace_all": True})

    assert rejected == tool_failure(
        "ambiguous_match",
        "old_string matches 2 places in SKILL.md of Skill 'demo' (lines 16, 17); nothing "
        "changed. Include more surrounding text so it matches once, or set replace_all to "
        "true to change every occurrence.",
        retryable=False,
    )
    assert unchanged == before
    assert replaced["data"] == {"content": "Replaced 2 occurrences in SKILL.md of Skill 'demo'."}
    assert "Friday" not in _body(skill_file)


@pytest.mark.parametrize(
    "extra",
    [
        {"file_content": "", "replace_all": False, "file_path": ""},
        {"content": ""},
        {"content": "- Never deploy after 4 pm."},
    ],
)
def test_patch_ignores_empty_placeholders_and_identical_duplicates(
    tmp_path: Path, extra: dict[str, object]
) -> None:
    harness, skill_file = _patch_harness(tmp_path)

    result = harness.run(
        {
            "action": "patch",
            "name": "demo",
            "old_string": "- Never deploy on Fridays.",
            "new_string": "- Never deploy after 4 pm.",
            **extra,
        }
    )

    assert result["ok"] is True
    assert "- Never deploy after 4 pm.\n- Check the Friday calendar.\n" in _body(skill_file)


def test_patch_with_two_different_texts_changes_nothing(tmp_path: Path) -> None:
    harness, skill_file = _patch_harness(tmp_path)
    before = skill_file.read_bytes()

    result = harness.run(
        {
            "action": "patch",
            "name": "demo",
            "old_string": "- Never deploy on Fridays.",
            "new_string": "A",
            "content": "B",
        }
    )

    assert result == tool_failure(
        "invalid_arguments",
        "Conflicting values for new_string: content and new_string differ; give one text.",
        retryable=False,
    )
    assert skill_file.read_bytes() == before


def test_edit_with_old_string_and_a_fragment_patches_only_that_text(tmp_path: Path) -> None:
    harness, skill_file = _patch_harness(tmp_path)

    result = harness.run(
        {
            "action": "edit",
            "name": "demo",
            "old_string": "- Never deploy on Fridays.",
            "content": "- Never deploy on holidays.",
        }
    )

    assert result["data"] == {
        "content": "Patched SKILL.md of Skill 'demo' at line 16.\n"
        "Note: edit with old_string changed only that text, as patch does."
    }
    assert _body(skill_file) == _PATCH_BODY.replace("on Fridays", "on holidays")


def test_edit_with_old_string_and_a_complete_document_is_refused(tmp_path: Path) -> None:
    harness, skill_file = _patch_harness(tmp_path)
    before = skill_file.read_bytes()

    result = harness.run(
        {
            "action": "edit",
            "name": "demo",
            "old_string": "# Demo",
            "content": _skill_md(body="# New\n"),
        }
    )

    assert result == tool_failure(
        "invalid_arguments",
        "edit replaces the complete SKILL.md and takes no old_string. Omit old_string to "
        "replace the whole file, or use action patch with old_string and new_string to "
        "change one passage.",
        retryable=False,
    )
    assert skill_file.read_bytes() == before


def test_patch_without_old_string_names_edit_for_a_complete_document(tmp_path: Path) -> None:
    harness, skill_file = _patch_harness(tmp_path)
    before = skill_file.read_bytes()

    complete = harness.run(
        {"action": "patch", "name": "demo", "new_string": _skill_md(body="# New\n")}
    )
    fragment = harness.run({"action": "patch", "name": "demo", "new_string": "# New"})

    assert complete["error"]["message"] == (
        "patch needs old_string, the exact current text to replace. To replace the "
        "complete SKILL.md, use action edit with the same text as content."
    )
    assert fragment["error"]["message"] == (
        "patch needs old_string, the exact current text to replace, and new_string. Read "
        'the current text with skill {"name": "demo", "file_path": "SKILL.md"}.'
    )
    assert skill_file.read_bytes() == before


# --- Documents and names ----------------------------------------------------

_CREATED = _STAMPED_HEAD.encode() + b"# Demo\n"


@pytest.mark.parametrize(
    ("content", "description"),
    [
        ("# Demo\n", "Do a demo task."),
        ("---\nname: demo\n---\n\n# Demo\n", "Do a demo task."),
        ("---\ndescription: Do a demo task.\n---\n\n# Demo\n", None),
        (
            "\n```markdown\n---\nname: demo\ndescription: Do a demo task.\n---\n\n# Demo\n```\n",
            None,
        ),
    ],
)
def test_create_completes_the_front_matter_and_writes_lf(
    tmp_path: Path, content: str, description: str | None
) -> None:
    harness = _Harness(tmp_path)
    arguments: dict[str, object] = {"action": "create", "name": "demo", "content": content}
    if description is not None:
        arguments["description"] = description

    result = harness.run(arguments)

    assert result["data"] == {"content": "Created Skill 'demo'."}
    assert (harness.home("main") / "demo" / "SKILL.md").read_bytes() == _CREATED


@pytest.mark.parametrize(
    ("content", "description", "message"),
    [
        (
            _skill_md(),
            "Something else.",
            "description differs from the description in the front matter; give it once.",
        ),
        (
            _skill_md(name="other"),
            None,
            "The front matter names 'other' but name is 'demo'; use the same name in both.",
        ),
    ],
)
def test_create_refuses_conflicting_document_fields(
    tmp_path: Path, content: str, description: str | None, message: str
) -> None:
    harness = _Harness(tmp_path)
    arguments: dict[str, object] = {"action": "create", "name": "demo", "content": content}
    if description is not None:
        arguments["description"] = description

    result = harness.run(arguments)

    assert result == tool_failure("invalid_arguments", message, retryable=False)
    assert not harness.home("main").exists()


@pytest.mark.parametrize("name", ["demos", "Demo", "xdemo"])
def test_similar_skill_name_is_suggested_and_nothing_is_written(tmp_path: Path, name: str) -> None:
    harness, skill_file = _patch_harness(tmp_path)
    before = skill_file.read_bytes()

    result = harness.run(
        {"action": "patch", "name": name, "old_string": "# Demo", "new_string": "# Other"}
    )

    assert result == tool_failure(
        "skill_not_found",
        f"You have no Skill named '{name}'; nothing changed. Did you mean 'demo'?",
        retryable=False,
    )
    assert skill_file.read_bytes() == before
    assert sorted(path.name for path in harness.home("main").iterdir()) == ["demo"]


def test_invocation_marks_and_package_paths_in_the_name(tmp_path: Path) -> None:
    harness, skill_file = _patch_harness(tmp_path)

    patched = harness.run(
        {"action": "patch", "name": "/demo", "old_string": "# Demo", "new_string": "# Demo!"}
    )
    written = harness.run(
        {"action": "write_file", "name": "demo/references/notes.md", "content": "Notes.\n"}
    )

    assert patched["ok"] is True
    assert _body(skill_file).startswith("# Demo!\n")
    assert written["data"] == {"content": "Wrote references/notes.md of Skill 'demo'."}
    notes = harness.home("main") / "demo" / "references" / "notes.md"
    assert notes.read_bytes() == b"Notes.\n"


def test_write_file_to_skill_md_replaces_the_document(tmp_path: Path) -> None:
    harness, skill_file = _patch_harness(tmp_path)

    result = harness.run(
        {
            "action": "write_file",
            "name": "demo",
            "file_path": "SKILL.md",
            "content": _skill_md(body="# Rewritten\n"),
        }
    )

    assert result["data"] == {"content": "Replaced SKILL.md of Skill 'demo'."}
    assert _body(skill_file) == "# Rewritten\n"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"action": "remove_file", "file_path": "SKILL.md"},
            "SKILL.md cannot be removed on its own; action delete removes the whole Skill.",
        ),
        (
            {"action": "delete", "file_path": "references/notes.md"},
            "delete removes the whole Skill. To remove one file, use action remove_file with "
            'file_path "references/notes.md"; omit file_path to delete the Skill.',
        ),
        (
            {
                "action": "write_file",
                "file_path": "references/notes.md",
                "old_string": "a",
                "content": "b",
            },
            "write_file replaces the whole file and takes no old_string. Use action patch with "
            'file_path "references/notes.md", old_string and new_string to change one passage, '
            "or omit old_string to write the complete file.",
        ),
        (
            {"action": "create", "file_path": "references/notes.md", "content": "Notes."},
            "create writes SKILL.md. To write references/notes.md, use action write_file with "
            "that file_path; omit file_path to create SKILL.md.",
        ),
        (
            {"action": "patch", "description": "New.", "old_string": "a", "new_string": "b"},
            "description is used only by create and edit. To change an existing Skill's "
            "description, patch its description line in SKILL.md.",
        ),
    ],
)
def test_calls_with_another_actions_effect_are_refused_before_writing(
    tmp_path: Path, arguments: dict[str, object], message: str
) -> None:
    harness, skill_file = _patch_harness(tmp_path)
    before = skill_file.read_bytes()

    result = harness.run({"name": "demo", **arguments})

    assert result == tool_failure("invalid_arguments", message, retryable=False)
    assert skill_file.read_bytes() == before
    assert sorted(path.name for path in skill_file.parent.iterdir()) == ["SKILL.md"]


def test_own_scope_is_accepted_and_category_is_noted(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)

    result = harness.run(
        {
            "action": "create",
            "name": "demo",
            "content": _skill_md(),
            "scope": "private",
            "category": "devops",
        }
    )

    assert result["data"] == {
        "content": "Created Skill 'demo'.\nNote: category is not used; Skills have no categories."
    }
