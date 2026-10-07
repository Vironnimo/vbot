"""Authoring hints in successful ``skill_manage`` results."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from core.runs import RunKind
from core.skills.authoring import SkillAuthoringService
from core.tools import SKILL_MANAGE_TOOL_NAME, ToolContext, ToolRegistry, register_skill_manage_tool
from core.tools._skill_conventions import (
    SKILL_MANAGE_HINT_EMPHASIS,
    SKILL_MANAGE_HINT_HISTORY,
    SKILL_MANAGE_HINT_LONG_DESCRIPTION,
    SKILL_MANAGE_HINT_LONG_SKILL_MD,
    SKILL_MANAGE_HINT_NO_SITUATION,
    SKILL_MANAGE_HINT_UNNAMED_FILE,
)

_DESCRIPTION = "Draft release notes. Use when the user asks for release notes."


def _skill_md(
    description: str = _DESCRIPTION, body: str = "# Notes\n\n1. Collect changes.\n"
) -> str:
    return f"---\nname: notes\ndescription: {description}\n---\n\n{body}"


class _Tool:
    """``skill_manage`` for the Agent ``main``, called through registry dispatch."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.tools = ToolRegistry()
        register_skill_manage_tool(
            self.tools,
            SkillAuthoringService(),
            lambda agent_id: root / "agents" / agent_id / "skills",
            lambda _agent_id: None,
        )

    @property
    def package(self) -> Path:
        return self.root / "agents" / "main" / "skills" / "notes"

    def run(self, arguments: dict[str, object], run_kind: RunKind | None = None) -> list[str]:
        """Call the Tool on Skill ``notes``; return the result's Hint lines."""
        context = ToolContext(
            agent_id="main",
            session_id="session-one",
            run_id="run-one",
            tool_call_id="call-one",
            tool_name=SKILL_MANAGE_TOOL_NAME,
            tool_call_index=0,
            workspace=self.root,
            vbot_root=self.root,
            data_root=self.root,
            cwd=self.root,
            run_kind=run_kind,
        )
        result = cast(
            dict[str, Any],
            asyncio.run(
                self.tools.dispatch(
                    context, {"name": "notes", **arguments}, [SKILL_MANAGE_TOOL_NAME]
                )
            ),
        )
        assert result["ok"] is True, result
        lines = result["data"]["content"].split("\n")
        return [line.removeprefix("Hint: ") for line in lines if line.startswith("Hint: ")]

    def create(self, content: str = "", run_kind: RunKind | None = None) -> list[str]:
        return self.run({"action": "create", "content": content or _skill_md()}, run_kind)


_LONG = "Draft release notes from merged changes in the user's format. " * 5
_DATED = "# Notes\n\n- Collect changes, verified 2026-09-17.\n"


@pytest.mark.parametrize(
    ("content", "hints"),
    [
        pytest.param(_skill_md(), [], id="conventional"),
        pytest.param(
            _skill_md(description="Draft release notes."),
            [SKILL_MANAGE_HINT_NO_SITUATION],
            id="no-situation",
        ),
        # A situation in German, or with "Use for", is a situation.
        pytest.param(
            _skill_md(description="Entwirft Release Notes, wenn der Nutzer sie will."),
            [],
            id="german-situation",
        ),
        pytest.param(
            _skill_md(description="Draft release notes. Use for changelog requests."),
            [],
            id="use-for",
        ),
        pytest.param(
            _skill_md(description=f"{_LONG}Use when the user asks for them."),
            [SKILL_MANAGE_HINT_LONG_DESCRIPTION.format(length=len(_LONG) + 32)],
            id="long-description",
        ),
        # Beyond 1024 characters the loader's own warning covers the length.
        pytest.param(
            _skill_md(description=f"{_LONG * 4}Use when the user asks for them."),
            [],
            id="description-the-loader-warns-about",
        ),
        pytest.param(
            _skill_md(body=_DATED),
            [SKILL_MANAGE_HINT_HISTORY.format(path="SKILL.md", example="2026-09-17")],
            id="date",
        ),
        pytest.param(
            _skill_md(body="# Notes\n\nFixed in PR #482 after the outage.\n"),
            [SKILL_MANAGE_HINT_HISTORY.format(path="SKILL.md", example="PR #482")],
            id="pull-request",
        ),
        pytest.param(
            _skill_md(body="# Notes\n\nNEVER publish a draft.\n"),
            [SKILL_MANAGE_HINT_EMPHASIS.format(path="SKILL.md", example="NEVER")],
            id="capitals",
        ),
        # Code is not prose: commands and examples keep their dates and capitals.
        pytest.param(
            _skill_md(body="# Notes\n\nRun `git log --since=2026-09-17 NEVER`.\n"),
            [],
            id="inline-code",
        ),
        pytest.param(
            _skill_md(body="# Notes\n\n```\nLOG_LEVEL=CRITICAL 2026-09-17\n```\n"),
            [],
            id="code-block",
        ),
    ],
)
def test_a_whole_skill_md_reports_each_convention_it_breaks(
    tmp_path: Path, content: str, hints: list[str]
) -> None:
    assert _Tool(tmp_path).create(content) == hints


def test_a_long_skill_md_reports_its_length(tmp_path: Path) -> None:
    tool = _Tool(tmp_path)

    hints = tool.create(_skill_md(body="# Notes\n\n" + "Collect each change.\n" * 600))

    length = len((tool.package / "SKILL.md").read_text(encoding="utf-8"))
    assert length > 12000
    assert hints == [SKILL_MANAGE_HINT_LONG_SKILL_MD.format(length=f"{length:,}")]


@pytest.mark.parametrize("run_kind", [None, RunKind.SKILL_REFLECTION, RunKind.LIBRARIAN])
def test_hints_reach_every_writer_and_stop_at_three(
    tmp_path: Path, run_kind: RunKind | None
) -> None:
    content = _skill_md(
        description=f"{_LONG}Draft them.",
        body="# Notes\n\nIMPORTANT: collect changes (2026-09-17).\n",
    )

    hints = _Tool(tmp_path).create(content, run_kind)

    assert hints == [
        SKILL_MANAGE_HINT_NO_SITUATION,
        SKILL_MANAGE_HINT_LONG_DESCRIPTION.format(length=len(_LONG) + 11),
        SKILL_MANAGE_HINT_HISTORY.format(path="SKILL.md", example="2026-09-17"),
    ]


def test_a_patch_reports_only_what_it_introduced(tmp_path: Path) -> None:
    tool = _Tool(tmp_path)
    # An older Skill that already breaks conventions, written by hand.
    tool.package.mkdir(parents=True)
    (tool.package / "SKILL.md").write_text(
        _skill_md(description="Draft release notes.", body=_DATED), encoding="utf-8"
    )

    unrelated = tool.run(
        {"action": "patch", "old_string": "Collect changes", "new_string": "Collect merges"}
    )
    another_date = tool.run(
        {"action": "patch", "old_string": "# Notes", "new_string": "# Notes (2026-10-01)"}
    )
    shouted = tool.run(
        {"action": "patch", "old_string": "Collect merges", "new_string": "ALWAYS collect merges"}
    )

    assert unrelated == []
    assert another_date == []
    assert shouted == [SKILL_MANAGE_HINT_EMPHASIS.format(path="SKILL.md", example="ALWAYS")]


def test_support_files_need_a_pointer(tmp_path: Path) -> None:
    tool = _Tool(tmp_path)
    tool.create()

    def write(path: str, content: str = "Text.\n") -> list[str]:
        return tool.run({"action": "write_file", "file_path": path, "content": content})

    unnamed = write("references/export.md")
    pointed = tool.run(
        {
            "action": "patch",
            "old_string": "1. Collect changes.",
            "new_string": "1. Collect changes. For exports, read references/export.md.",
        }
    )
    script = write("scripts/collect.py", "from _helpers import load\n")
    helper = write("scripts/_helpers.py", "def load(): ...\n")
    nested = write("references/styles/plain.md")
    dated = write("references/history.md", "Changed on 2026-09-17.\n")

    assert unnamed == [
        SKILL_MANAGE_HINT_UNNAMED_FILE.format(path="references/export.md", verb="read")
    ]
    assert pointed == []
    assert script == [SKILL_MANAGE_HINT_UNNAMED_FILE.format(path="scripts/collect.py", verb="run")]
    # Helpers, nested files and files another file names need no pointer of their own.
    assert helper == []
    assert nested == []
    assert dated == [
        SKILL_MANAGE_HINT_UNNAMED_FILE.format(path="references/history.md", verb="read"),
        SKILL_MANAGE_HINT_HISTORY.format(path="references/history.md", example="2026-09-17"),
    ]
    # Rewriting SKILL.md whole is judged on every file it should name.
    assert tool.run({"action": "edit", "content": _skill_md()}) == [
        SKILL_MANAGE_HINT_UNNAMED_FILE.format(path="references/export.md", verb="read"),
        SKILL_MANAGE_HINT_UNNAMED_FILE.format(path="references/history.md", verb="read"),
        SKILL_MANAGE_HINT_UNNAMED_FILE.format(path="scripts/collect.py", verb="run"),
    ]
