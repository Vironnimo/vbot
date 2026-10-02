"""Tests for direct vBot Skill authoring through ``skill_manage``."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, cast

import pytest

from core.providers.tool_schema import sanitize_anthropic_tool_input_schema
from core.runs import RunKind
from core.skills.authoring import HUMAN_WRITER, SkillAuthoringService
from core.skills.skills import SkillRegistry
from core.tools import (
    SKILL_MANAGE_TOOL_NAME,
    SKILL_MANAGE_TOOL_PARAMETERS,
    ToolContext,
    ToolRegistry,
    register_skill_manage_tool,
    tool_failure,
    tool_failure_for_exception,
)


def _skill_md(
    name: str = "demo",
    description: str = "Do a demo task.",
    body: str = "# Demo\n",
) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"


def _context(agent_id: str, root: Path, run_kind: RunKind | None = None) -> ToolContext:
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
        run_kind=run_kind,
    )


class _Harness:
    """The Tool for the caller ``main``; ``owner`` can share Skills with it."""

    def __init__(self, tmp_path: Path, scopes: dict[str, str] | None = None) -> None:
        self.root = tmp_path
        self.invalidated: list[str | None] = []
        # One entry per reported change, after its invalidation.
        self.changes: list[list[str | None]] = []
        # Names the Agent ``owner`` shares with every other Agent.
        self.shared: set[str] = set()
        self.tools = ToolRegistry()
        self.authoring = SkillAuthoringService(protected_roots=[tmp_path / "resources" / "skills"])
        register_skill_manage_tool(
            self.tools,
            self.authoring,
            self.home,
            self.invalidated.append,
            lambda agent_id, name: (
                self.home("owner") if agent_id != "owner" and name in self.shared else None
            ),
            lambda _agent_id, name, _project_id: (scopes or {}).get(name),
            on_changed=lambda: self.changes.append(list(self.invalidated)),
        )

    def home(self, agent_id: str) -> Path:
        return self.root / "agents" / agent_id / "skills"

    def document(self, name: str = "demo", agent_id: str = "main") -> Path:
        return self.home(agent_id) / name / "SKILL.md"

    def run(
        self,
        arguments: dict[str, object],
        agent_id: str = "main",
        run_kind: RunKind | None = None,
    ) -> dict[str, Any]:
        context = _context(agent_id, self.root, run_kind)
        try:
            result = cast(
                dict[str, Any],
                asyncio.run(self.tools.dispatch(context, arguments, [SKILL_MANAGE_TOOL_NAME])),
            )
        except Exception as error:
            # The executor reports a call that raised the same way.
            result = tool_failure_for_exception(SKILL_MANAGE_TOOL_NAME, error)
        # What the user sees of the latest call.
        self.details = self.tools.display_for_call(
            SKILL_MANAGE_TOOL_NAME, arguments, context=context, result=result
        )["details"]
        return result

    def changed_files(self) -> list[tuple[str, str, int, int]]:
        """The latest call's changed files: path, change, added and removed lines."""
        return [
            (change["path"], change["change"], change["added"], change["removed"])
            for block in self.details
            if block["type"] == "file_changes"
            for change in block["files"]
        ]

    def create(self, *, name: str = "demo", content: str | None = None) -> dict[str, Any]:
        return self.run(
            {
                "action": "create",
                "name": name,
                "content": content if content is not None else _skill_md(name=name),
            }
        )

    def share(self, name: str = "deploy") -> Path:
        document = self.document(name, "owner")
        document.parent.mkdir(parents=True)
        document.write_text(_skill_md(name, "Ship it.", "# Shared\n"), encoding="utf-8")
        self.shared.add(name)
        return document


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
    home = tmp_path / "skills"
    register_skill_manage_tool(registry, service, lambda _: home, invalidated.append)
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
        assert not (home / "demo").exists()
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

    assert SkillRegistry.load(home).get("demo") is not None
    assert invalidated == ["main"]


def test_provider_schema_is_flat_and_hermes_shaped(tmp_path: Path) -> None:
    definitions = _Harness(tmp_path).tools.provider_definitions([SKILL_MANAGE_TOOL_NAME])
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


def test_create_is_immediately_live_and_invalidates(tmp_path: Path, caplog: Any) -> None:
    harness = _Harness(tmp_path)

    with caplog.at_level(logging.INFO, logger="vbot.tools.skill_manage"):
        result = harness.create(content=_skill_md(body="private body"))

    assert result["ok"] is True
    assert result["data"] == {"content": "Created Skill 'demo'."}
    assert harness.document().is_file()
    assert SkillRegistry.load(harness.home("main")).get("demo").description == "Do a demo task."
    assert harness.invalidated == ["main"]
    # The change is reported once, after the caches it affects were invalidated.
    assert harness.changes == [["main"]]
    assert "action=create" in caplog.text
    assert "private body" not in caplog.text
    lines = len(harness.document().read_text(encoding="utf-8").splitlines())
    assert harness.changed_files() == [("SKILL.md", "created", lines, 0)]
    assert str(harness.home("main")) not in str(result)


_STAMPED_HEAD = (
    "---\nname: demo\ndescription: Do a demo task.\nmetadata:\n  vbot:\n    author: agent\n---\n\n"
)
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
    assert harness.document().read_bytes() == _CREATED


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


@pytest.mark.parametrize("content", ["---\nname: demo\n---\n\nbody\n", "# Demo\n\nbody\n"])
def test_missing_description_is_refused_with_the_header(tmp_path: Path, content: str) -> None:
    harness = _Harness(tmp_path)

    result = harness.create(content=content)

    assert result["ok"] is False
    message = result["error"]["message"]
    assert "---\nname: demo\ndescription: <what it covers and when to load it>\n---" in message
    assert not harness.home("main").exists()
    assert harness.invalidated == []
    assert harness.changes == []


# --- Background reviews -----------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "arguments", "code", "message"),
    [
        pytest.param(
            "user",
            {"action": "patch", "name": "demo", "old_string": "# Demo", "new_string": "# New"},
            "skill_protected",
            "Skill 'demo' was created by the user, so it cannot be changed in the background; "
            "nothing changed. Leave it as it is and name the needed change in your closing "
            "reply.",
            id="user-skill",
        ),
        pytest.param(
            "pinned",
            {"action": "edit", "name": "demo", "content": _skill_md(body="# New\n")},
            "skill_protected",
            "Skill 'demo' is pinned by the user, so it cannot be changed in the background; "
            "nothing changed. Leave it as it is and name the needed change in your closing "
            "reply.",
            id="pinned-skill",
        ),
        pytest.param(
            "shared",
            {"action": "patch", "name": "deploy", "old_string": "# Shared", "new_string": "#"},
            "skill_protected",
            "Skill 'deploy' is shared with you by another Agent, so it cannot be changed in "
            "the background; nothing changed. Leave it as it is and name the needed change in "
            "your closing reply.",
            id="shared-skill",
        ),
        pytest.param(
            "agent",
            {"action": "delete", "name": "demo"},
            "invalid_arguments",
            "In the background, delete needs absorbed_into: the name of another of your own "
            "Skills that now holds the instructions of 'demo'; nothing changed. Merge the "
            "instructions into that Skill with patch or edit first, then call delete with "
            "absorbed_into. If no other Skill holds them, leave 'demo' as it is.",
            id="delete-without-absorbed-into",
        ),
    ],
)
def test_background_reviews_refuse_skills_they_may_not_change(
    tmp_path: Path, setup: str, arguments: dict[str, object], code: str, message: str
) -> None:
    harness = _Harness(tmp_path)
    home = harness.home("main")
    if setup == "user":
        harness.authoring.create(home, "demo", _skill_md(), writer=HUMAN_WRITER)
    elif setup == "shared":
        harness.share()
    else:
        harness.create()
    if setup == "pinned":
        harness.authoring.set_pinned(home, "demo", True, writer=HUMAN_WRITER)
    before = {path: path.read_bytes() for path in tmp_path.rglob("SKILL.md")}

    result = harness.run(arguments, run_kind=RunKind.SKILL_REFLECTION)

    assert result == tool_failure(code, message, retryable=False)
    assert {path: path.read_bytes() for path in tmp_path.rglob("SKILL.md")} == before
    # An attended Run is not limited by pins or by who created the Skill.
    if setup in ("user", "pinned"):
        assert harness.run(arguments)["ok"] is True


def test_background_reviews_change_and_merge_skills_agents_created(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.create(name="old")
    review = RunKind.REFLECTION

    created = harness.run(
        {"action": "create", "name": "new", "content": _skill_md("new")}, run_kind=review
    )
    patched = harness.run(
        {"action": "patch", "name": "new", "old_string": "# Demo", "new_string": "# Merged"},
        run_kind=review,
    )
    deleted = harness.run(
        {"action": "delete", "name": "old", "absorbed_into": "new"}, run_kind=review
    )

    assert all(result["ok"] for result in (created, patched, deleted))
    home = harness.home("main")
    record = harness.authoring.record(home, "new")
    assert record is not None and record.origin == "reflection"
    [archived] = harness.authoring.archived(home)
    assert (archived.reason, archived.absorbed_into, archived.archived_by) == (
        "absorbed",
        "new",
        "reflection",
    )
    revision = harness.authoring.history(home, "new")[0]
    assert (revision.actor, revision.run_kind) == ("reflection", "reflection")


# --- Patch tolerance --------------------------------------------------------

_PATCH_BODY = (
    '# Demo\n\n1. Run `pytest`.\n2. Tag the release: "v1".\n\n## Pitfalls\n\n'
    "- Never deploy on Fridays.\n- Check the Friday calendar.\n"
)


def _patch_harness(tmp_path: Path, body: str = _PATCH_BODY) -> tuple[_Harness, Path]:
    harness = _Harness(tmp_path)
    assert harness.create(content=_skill_md(body=body))["ok"] is True
    harness.invalidated.clear()
    return harness, harness.document()


def _body(skill_file: Path) -> str:
    text = skill_file.read_bytes().decode("utf-8")
    assert text.startswith(_STAMPED_HEAD)
    return text[len(_STAMPED_HEAD) :]


_MESSAGE_HEADER = "---\nname: new\ndescription: <what it covers and when to load it>\n---"


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        pytest.param(
            {"action": "create", "name": "new"},
            "create needs content. SKILL.md starts with front matter holding name and a "
            f"description of when to load the Skill, then the instructions:\n{_MESSAGE_HEADER}"
            "\n\n<instructions>",
            id="create-without-content",
        ),
        pytest.param(
            {
                "action": "create",
                "name": "new",
                "content": _skill_md(name="new"),
                "description": "Something else.",
            },
            "description differs from the description in the front matter; give it once.",
            id="create-with-two-descriptions",
        ),
        pytest.param(
            {"action": "create", "name": "new", "content": _skill_md(name="other")},
            "The front matter names 'other' but name is 'new'; use the same name in both.",
            id="create-with-two-names",
        ),
        pytest.param(
            {
                "action": "create",
                "name": "new",
                "content": _skill_md(name="new"),
                "scope": "global",
            },
            "skill_manage writes only your own Skills; global, Project and bundled Skills are "
            "read-only here. Omit scope to write one of your own Skills.",
            id="global-scope",
        ),
        pytest.param(
            {"action": "create", "file_path": "references/notes.md", "content": "Notes."},
            "create writes SKILL.md. To write references/notes.md, use action write_file with "
            "that file_path; omit file_path to create SKILL.md.",
            id="create-support-file",
        ),
        pytest.param(
            {"action": "delete", "content": _skill_md()},
            "delete removes the whole Skill and takes no text. Use edit or patch to change it "
            "instead.",
            id="delete-with-text",
        ),
        pytest.param(
            {"action": "delete", "file_path": "references/notes.md"},
            "delete removes the whole Skill. To remove one file, use action remove_file with "
            'file_path "references/notes.md"; omit file_path to delete the Skill.',
            id="delete-one-file",
        ),
        pytest.param(
            {"action": "remove_file", "file_path": "SKILL.md"},
            "SKILL.md cannot be removed on its own; action delete removes the whole Skill.",
            id="remove-document",
        ),
        pytest.param(
            {
                "action": "write_file",
                "file_path": "references/notes.md",
                "old_string": "a",
                "content": "b",
            },
            "write_file replaces the whole file and takes no old_string. Use action patch with "
            'file_path "references/notes.md", old_string and new_string to change one passage, '
            "or omit old_string to write the complete file.",
            id="write-file-with-old-string",
        ),
        pytest.param(
            {"action": "patch", "description": "New.", "old_string": "a", "new_string": "b"},
            "description is used only by create and edit. To change an existing Skill's "
            "description, patch its description line in SKILL.md.",
            id="patch-description",
        ),
        pytest.param(
            {"action": "patch", "old_string": "- Never deploy on Fridays.", "new_string": "A"}
            | {"content": "B"},
            "Conflicting values for new_string: content and new_string differ; give one text.",
            id="patch-with-two-texts",
        ),
        pytest.param(
            {"action": "patch", "new_string": _skill_md(body="# New\n")},
            "patch needs old_string, the exact current text to replace. To replace the "
            "complete SKILL.md, use action edit with the same text as content.",
            id="patch-with-a-document",
        ),
        pytest.param(
            {"action": "patch", "new_string": "# New"},
            "patch needs old_string, the exact current text to replace, and new_string. Read "
            'the current text with skill {"name": "demo", "file_path": "SKILL.md"}.',
            id="patch-without-old-string",
        ),
        pytest.param(
            {"action": "edit", "old_string": "# Demo", "content": _skill_md(body="# New\n")},
            "edit replaces the complete SKILL.md and takes no old_string. Omit old_string to "
            "replace the whole file, or use action patch with old_string and new_string to "
            "change one passage.",
            id="edit-with-old-string",
        ),
    ],
)
def test_calls_that_cannot_apply_as_given_are_refused_before_writing(
    tmp_path: Path, arguments: dict[str, object], message: str
) -> None:
    harness, skill_file = _patch_harness(tmp_path)
    before = skill_file.read_bytes()

    result = harness.run({"name": "demo", **arguments})

    assert (result["ok"], result["error"]["code"], result["error"]["message"]) == (
        False,
        "invalid_arguments",
        message,
    )
    assert skill_file.read_bytes() == before
    assert [path.name for path in harness.home("main").iterdir()] == ["demo"]
    assert [path.name for path in skill_file.parent.iterdir()] == ["SKILL.md"]
    assert harness.invalidated == []


@pytest.mark.parametrize(
    ("arguments", "named"),
    [
        (
            {"action": "write_file", "file_path": "assets/logo.png", "source_path": "logo.png"},
            "source_path",
        ),
        (
            {
                "action": "write_file",
                "file_path": "scripts/run.py",
                "content": "print('ok')\n",
                "executable": True,
            },
            "executable",
        ),
        ({"action": "begin"}, '"action" must be one of "create", "edit"'),
    ],
    ids=["binary-copy", "executable-flag", "draft-action"],
)
def test_removed_actions_and_arguments_are_rejected_by_name(
    tmp_path: Path, arguments: dict[str, object], named: str
) -> None:
    harness, skill_file = _patch_harness(tmp_path)

    result = harness.run({"name": "demo", **arguments})

    assert result["ok"] is False
    assert named in result["error"]["message"]
    assert [path.name for path in skill_file.parent.iterdir()] == ["SKILL.md"]


@pytest.mark.parametrize("action", ["patch", "write_file"])
@pytest.mark.parametrize("extra", [{}, {"content": None}, {"content": False}])
def test_content_must_be_present_and_textual(
    tmp_path: Path, action: str, extra: dict[str, object]
) -> None:
    harness, skill_path = _patch_harness(tmp_path)
    before = skill_path.read_bytes()
    fields = {"match": "Demo"} if action == "patch" else {"file_path": "assets/empty.txt"}

    result = harness.run({"action": action, "name": "demo", **fields, **extra})

    assert result["ok"] is False
    assert skill_path.read_bytes() == before
    assert not (skill_path.parent / "assets/empty.txt").exists()


def test_write_and_remove_support_file(tmp_path: Path) -> None:
    harness, skill_file = _patch_harness(tmp_path)
    arguments = {"action": "write_file", "name": "demo", "file_path": "references/notes.md"}

    written = harness.run({**arguments, "content": "Useful notes\n"})
    written_files = harness.changed_files()
    harness.run({**arguments, "content": "Useful notes\nMore\n"})
    rewritten_files = harness.changed_files()
    removed = harness.run(
        {"action": "remove_file", "name": "demo", "file_path": "references/notes.md"}
    )

    assert written["data"] == {"content": "Wrote references/notes.md of Skill 'demo'."}
    assert removed["data"] == {"content": "Removed references/notes.md from Skill 'demo'."}
    # The user sees each change as a diff of the package file.
    assert written_files == [("references/notes.md", "created", 1, 0)]
    assert rewritten_files == [("references/notes.md", "updated", 1, 0)]
    assert harness.changed_files() == [("references/notes.md", "deleted", 0, 2)]
    assert not (skill_file.parent / "references").exists()
    assert harness.invalidated == ["main", "main", "main"]


@pytest.mark.parametrize("existing", [False, True])
def test_write_file_preserves_intentional_empty_content(tmp_path: Path, existing: bool) -> None:
    harness, skill_file = _patch_harness(tmp_path)
    arguments = {"action": "write_file", "name": "demo", "file_path": "assets/empty.txt"}
    if existing:
        assert harness.run({**arguments, "content": "old contents"})["ok"] is True

    result = harness.run({**arguments, "content": ""})

    assert result["ok"] is True
    assert (skill_file.parent / "assets/empty.txt").read_bytes() == b""


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


def test_non_skill_file_path_is_rejected(tmp_path: Path) -> None:
    harness, skill_file = _patch_harness(tmp_path)

    result = harness.run(
        {"action": "write_file", "name": "demo", "file_path": "other/data.txt", "content": "no"}
    )

    assert result["error"]["code"] == "skill_write_rejected"
    assert (
        "Support files must live under scripts/ or references/ or assets/"
        in result["error"]["message"]
    )
    assert not (skill_file.parent / "other").exists()


def test_edit_replaces_complete_skill_document(tmp_path: Path) -> None:
    harness, skill_file = _patch_harness(tmp_path)

    result = harness.run(
        {
            "action": "edit",
            "name": "demo",
            "content": _skill_md(description="Updated.", body="New body.\n"),
        }
    )

    assert result["ok"] is True
    assert harness.changed_files() == [("SKILL.md", "updated", 2, 10)]
    text = skill_file.read_text(encoding="utf-8")
    assert "description: Updated." in text
    assert "New body." in text


def test_delete_archives_complete_skill_and_invalidates(tmp_path: Path) -> None:
    harness, skill_file = _patch_harness(tmp_path)
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

    assert result["data"] == {
        "content": "Deleted Skill 'demo'. Its files are kept in the archive, where the user "
        "can restore it."
    }
    assert [(path, change) for path, change, _added, _removed in harness.changed_files()] == [
        ("SKILL.md", "deleted"),
        ("references/notes.md", "deleted"),
    ]
    assert not skill_file.parent.exists()
    assert harness.invalidated == ["main"]
    [archived] = harness.authoring.archived(harness.home("main"))
    assert (archived.name, archived.reason, archived.archived_by) == ("demo", "deleted", "agent")
    latest = harness.authoring.history(harness.home("main"), "demo")[0]
    assert (latest.kind, latest.session_id, latest.run_id) == ("archive", "session-one", "run-one")


@pytest.mark.parametrize(
    ("arguments", "code", "message"),
    [
        pytest.param(
            {"action": "delete", "name": "old", "absorbed_into": "new"},
            None,
            "Deleted Skill 'old'; its instructions now live in Skill 'new'. Its files are "
            "kept in the archive, where the user can restore it.",
            id="absorbed",
        ),
        pytest.param(
            {"action": "delete", "name": "old", "absorbed_into": "old"},
            "invalid_arguments",
            "absorbed_into names 'old' itself; nothing changed. Name the other Skill that now "
            "holds its instructions.",
            id="itself",
        ),
        pytest.param(
            {"action": "delete", "name": "old", "absorbed_into": "missing"},
            "invalid_arguments",
            "absorbed_into names 'missing', which is not one of your own Skills; nothing "
            "changed. Name one of your own Skills that now holds the instructions of 'old'.",
            id="unknown-target",
        ),
        pytest.param(
            {"action": "edit", "name": "old", "content": _skill_md("old"), "absorbed_into": "new"},
            "invalid_arguments",
            "absorbed_into is used only by delete; nothing changed. Omit absorbed_into for edit.",
            id="other-action",
        ),
    ],
)
def test_delete_names_the_skill_that_absorbed_it(
    tmp_path: Path, arguments: dict[str, object], code: str | None, message: str
) -> None:
    harness = _Harness(tmp_path)
    harness.create(name="old")
    harness.create(name="new")

    result = harness.run(arguments)

    if code is None:
        assert result["data"] == {"content": message}
        [archived] = harness.authoring.archived(harness.home("main"))
        assert (archived.reason, archived.absorbed_into) == ("absorbed", "new")
    else:
        assert result == tool_failure(code, message, retryable=False)
        assert harness.document("old").is_file()


# --- Names and scopes -------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "arguments", "fragments"),
    [
        (
            "bundled",
            {"action": "edit", "content": _skill_md(name="foreign")},
            [
                "Skill 'foreign' is a bundled Skill — read-only here.",
                "user-facing Skill controls",
                "do not edit the package with file or shell tools",
            ],
        ),
        (
            "project",
            {"action": "patch", "match": "x", "content": "y"},
            ["is a Project Skill — read-only here."],
        ),
        (
            "shared",
            {"action": "delete"},
            ["is shared with you", "only its owner or the user can delete it"],
        ),
    ],
)
def test_foreign_skill_names_report_their_scope_instead_of_not_found(
    tmp_path: Path, scope: str, arguments: dict[str, object], fragments: list[str]
) -> None:
    harness = _Harness(tmp_path, scopes={"foreign": scope})

    result = harness.run({"name": "foreign", **arguments})

    error = result["error"]
    assert (error["code"], error["retryable"]) == ("skill_write_rejected", False)
    assert all(fragment in error["message"] for fragment in fragments)
    assert "not found" not in error["message"]
    # create is not blocked: it writes the caller's own shadowing copy.
    assert harness.create(name="foreign")["ok"] is True


def test_unknown_name_keeps_plain_not_found(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, scopes={"foreign": "bundled"})

    result = harness.run(
        {"action": "edit", "name": "no-such-skill", "content": _skill_md(name="no-such-skill")}
    )

    assert result == tool_failure(
        "skill_not_found",
        "You have no Skill named 'no-such-skill'; nothing changed. You have no Skills of "
        "your own yet; use action create to add one.",
        retryable=False,
    )


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
    assert (skill_file.parent / "references" / "notes.md").read_bytes() == b"Notes.\n"


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
    harness, skill_file = _patch_harness(tmp_path)

    assert harness.run(arguments)["ok"] is True
    assert (skill_file.parent / "assets/empty.txt").read_bytes() == b""


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


# --- Skills shared with the caller -------------------------------------------


def test_receiver_changes_land_in_the_owner_package_and_invalidate_everyone(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    document = harness.share()

    patched = harness.run(
        {"action": "patch", "name": "deploy", "match": "# Shared", "content": "# Patched"}
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
        {"action": "remove_file", "name": "deploy", "file_path": "references/notes.md"}
    )
    edited = harness.run(
        {
            "action": "edit",
            "name": "deploy",
            "content": _skill_md("deploy", "Rewritten.", "# Body\n"),
        }
    )

    # The results read exactly like changes of the caller's own Skill.
    assert patched["data"] == {"content": "Patched SKILL.md of Skill 'deploy' at line 6."}
    assert all(result["ok"] for result in (written, removed, edited))
    assert "# Body" in document.read_text(encoding="utf-8")
    assert not (document.parent / "references" / "notes.md").exists()
    assert not harness.home("main").exists()
    assert harness.invalidated == [None, None, None, None]


def test_own_home_wins_over_a_shared_skill_of_the_same_name(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    shared = harness.share()
    before = shared.read_bytes()
    own = harness.document("deploy")
    own.parent.mkdir(parents=True)
    own.write_text(_skill_md("deploy", "My own copy.", "# Own\n"), encoding="utf-8")

    patched = harness.run(
        {"action": "patch", "name": "deploy", "match": "# Own", "content": "# Own patched"}
    )
    deleted = harness.run({"action": "delete", "name": "deploy"})

    assert patched["ok"] is True
    assert deleted["ok"] is True
    assert not own.exists()
    assert shared.read_bytes() == before


def test_create_and_delete_never_target_a_shared_package(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    shared = harness.share()
    before = shared.read_bytes()

    deleted = harness.run({"action": "delete", "name": "deploy"})
    created = harness.run(
        {"action": "create", "name": "deploy", "content": _skill_md("deploy", "My own copy.")}
    )

    # create writes the caller's own shadowing copy; delete refuses the shared one.
    assert deleted["error"]["code"] == "skill_not_found"
    assert created["ok"] is True
    assert harness.document("deploy").is_file()
    assert shared.read_bytes() == before


# --- Patch tolerance --------------------------------------------------------


def test_patch_by_legacy_names_changes_skill_md_or_the_named_file(tmp_path: Path) -> None:
    harness, skill_file = _patch_harness(tmp_path, body="Keep this step.\nObsolete step.\n")
    harness.run(
        {
            "action": "write_file",
            "name": "demo",
            "file_path": "scripts/run.py",
            "content": "print('old')\n",
        }
    )

    removed = harness.run(
        {"action": "patch", "name": "demo", "match": "Obsolete step.\n", "content": ""}
    )
    removed_files = harness.changed_files()
    script = harness.run(
        {
            "action": "patch",
            "name": "demo",
            "file_path": "scripts/run.py",
            "match": "old",
            "content": "new",
        }
    )

    assert (removed["ok"], script["ok"]) == (True, True)
    assert removed_files == [("SKILL.md", "updated", 0, 1)]
    assert harness.changed_files() == [("scripts/run.py", "updated", 1, 1)]
    assert _body(skill_file) == "Keep this step.\n"
    assert (skill_file.parent / "scripts" / "run.py").read_text(encoding="utf-8") == (
        "print('new')\n"
    )


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

    assert result["data"]["content"] == (
        "SKILL.md of Skill 'demo' already reads as new_string at line 9; nothing changed."
    )
    assert _body(skill_file) == "Say “hello” to users.\n"
    assert harness.details == [
        {
            "type": "notice",
            "level": "info",
            "text": "Nothing changed; the file already had this text.",
        }
    ]


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
    notes = skill_file.parent / "references" / "notes.md"
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
