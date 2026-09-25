"""Named search fields, other search interfaces' spellings, and path recovery."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.tools.contracts import ToolContractError
from core.tools.search_files import register_search_files_tool
from core.tools.tools import ToolRegistry
from tests.core.tools.test_search_files import context


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("import os\ndef load():\n    return os.name\n")
    (tmp_path / "src/view.ts").write_text("export const load = 1;\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/guide.md").write_text("Call load() first.\n")
    os.utime(tmp_path / "docs/guide.md", (1_000_000_000, 1_000_000_000))
    os.utime(tmp_path / "src/view.ts", (1_100_000_000, 1_100_000_000))
    os.utime(tmp_path / "src/app.py", (1_200_000_000, 1_200_000_000))
    return tmp_path


async def dispatch(root: Path, arguments: dict, **kwargs) -> dict:
    registry = ToolRegistry()
    register_search_files_tool(registry)
    return await registry.dispatch(context(root, **kwargs), arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "content"),
    [
        (
            {"pattern": "load"},
            "docs/guide.md:1:Call load() first.\nsrc/app.py:2:def load():\n"
            "src/view.ts:1:export const load = 1;",
        ),
        ({"pattern": "load", "path": "src", "glob": "*.py"}, "src/app.py:2:def load():"),
        (
            {"pattern": "load", "path": ["docs", "src/view.ts"]},
            "docs/guide.md:1:Call load() first.\nsrc/view.ts:1:export const load = 1;",
        ),
        (
            {"pattern": "load", "glob": ["*.py", "*.md"], "output": "files"},
            "docs/guide.md\nsrc/app.py",
        ),
        ({"pattern": "o", "path": "src/app.py", "output": "count"}, "src/app.py:3"),
        (
            {"pattern": "def", "context": 1},
            "src/app.py:1-import os\nsrc/app.py:2:def load():\nsrc/app.py:3-    return os.name",
        ),
        (
            {"pattern": "LOAD", "args": ["-i"], "glob": "*.ts"},
            "src/view.ts:1:export const load = 1;",
        ),
        (
            {"pattern": ["import", "export"]},
            "src/app.py:1:import os\nsrc/view.ts:1:export const load = 1;",
        ),
    ],
)
async def test_named_fields_search_contents(project: Path, arguments: dict, content: str) -> None:
    result = await dispatch(project, arguments)
    assert result["ok"], result
    assert result["data"]["content"] == content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "content"),
    [
        ({}, "src/app.py\nsrc/view.ts\ndocs/guide.md"),
        ({"path": "src"}, "src/app.py\nsrc/view.ts"),
        ({"glob": "*.md"}, "docs/guide.md"),
        ({"glob": "*.py", "output": "files"}, "src/app.py"),
        ({"args": ["--dirs"]}, "src/\ndocs/"),
    ],
)
async def test_omitting_the_pattern_lists_files(project: Path, arguments: dict, content: str):
    os.utime(project / "docs", (1_000_000_000, 1_000_000_000))
    result = await dispatch(project, arguments)
    assert result["ok"], result
    assert result["data"]["content"] == content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"output": "count"}, 'output "count" counts matching lines per file and needs a pattern'),
        ({"context": 2, "path": "src"}, "context shows lines around content matches"),
    ],
)
async def test_content_only_fields_without_a_pattern_explain_the_fix(
    project: Path, arguments: dict, message: str
) -> None:
    result = await dispatch(project, arguments)
    assert result["error"]["code"] == "invalid_arguments"
    assert message in result["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"pattern": "*.py"},
        {"pattern": "**/*.py", "path": "src"},
        {"args": ["*.py"]},
        {"pattern": "*.py", "target": "files"},
        {"pattern": "*.py", "output": "files"},
    ],
)
async def test_a_file_name_glob_in_pattern_lists_matching_files(project: Path, arguments: dict):
    result = await dispatch(project, arguments)
    assert result["data"]["content"] == "src/app.py"
    if "target" not in arguments:
        assert "is a file name glob" in result["data"]["note"]


@pytest.mark.asyncio
async def test_a_glob_shaped_literal_search_stays_a_content_search(project: Path) -> None:
    (project / "notes.txt").write_text("match *.py files\n")
    result = await dispatch(project, {"pattern": "*.py", "args": ["-F"]})
    assert result["data"]["content"] == "notes.txt:1:match *.py files"
    assert "note" not in result["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "content"),
    [
        # Claude Code's Grep.
        (
            {"pattern": "LOAD", "-i": True, "-n": True, "glob": "*.py", "output_mode": "content"},
            "src/app.py:2:def load():",
        ),
        (
            {"pattern": "load", "output_mode": "files_with_matches", "head_limit": 1},
            "docs/guide.md",
        ),
        ({"pattern": "os", "output_mode": "count", "path": "src"}, "src/app.py:2"),
        (
            {"pattern": "def", "-C": 1, "path": "src/app.py"},
            "src/app.py:1-import os\nsrc/app.py:2:def load():\nsrc/app.py:3-    return os.name",
        ),
        (
            {"pattern": "def", "-A": 1, "type": "py"},
            "src/app.py:2:def load():\nsrc/app.py:3-    return os.name",
        ),
        # Hermes and opencode.
        ({"pattern": "*.ts", "target": "files", "path": "src"}, "src/view.ts"),
        (
            {"pattern": "load", "target": "content", "file_glob": "*.md"},
            "docs/guide.md:1:Call load() first.",
        ),
        ({"pattern": "load", "include": "*.ts"}, "src/view.ts:1:export const load = 1;"),
        # Common spellings.
        (
            {"query": "load()", "literal": True, "directory": "docs"},
            "docs/guide.md:1:Call load() first.",
        ),
        (
            {"regex": "LOAD", "ignoreCase": "true", "exclude": ["*.md", "*.ts"]},
            "src/app.py:2:def load():",
        ),
        (
            {"pattern": "load(", "regex": False, "path": "docs"},
            "docs/guide.md:1:Call load() first.",
        ),
        (
            {"pattern": "Load", "case_sensitive": False, "file_type": "ts"},
            "src/view.ts:1:export const load = 1;",
        ),
        ({"pattern": "o", "-c": True, "path": "src/app.py"}, "src/app.py:3"),
        ({"pattern": "load", "-l": True, "glob": "*.md"}, "docs/guide.md"),
        ({"recursive": False, "args": ["--entries"]}, "src/\ndocs/"),
    ],
)
async def test_other_search_interfaces_spellings_run_as_intended(
    project: Path, arguments: dict, content: str
) -> None:
    os.utime(project / "docs", (1_000_000_000, 1_000_000_000))
    result = await dispatch(project, arguments)
    assert result["ok"], result
    assert result["data"]["content"] == content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "content"),
    [
        (["-rn", "def", "."], "src/app.py:2:def load():"),
        (
            ["-E", "import|export", "src"],
            "src/app.py:1:import os\nsrc/view.ts:1:export const load = 1;",
        ),
        (["--include=*.md", "load"], "docs/guide.md:1:Call load() first."),
        (
            ["--exclude", "*.py", "--exclude-dir=docs", "load"],
            "src/view.ts:1:export const load = 1;",
        ),
        (["-E", "utf-8", "def"], "src/app.py:2:def load():"),
    ],
)
async def test_grep_habits_in_args_keep_their_meaning(project: Path, args: list[str], content: str):
    result = await dispatch(project, {"args": args})
    assert result["ok"], result
    assert result["data"]["content"] == content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"pattern": "load", "query": "other"},
        {"pattern": "load", "-l": True, "output": "count"},
        {"pattern": "load", "ignore_case": "maybe"},
    ],
)
async def test_unclear_or_conflicting_fields_are_rejected(project: Path, arguments: dict):
    with pytest.raises(ToolContractError):
        await dispatch(project, arguments)


@pytest.mark.asyncio
async def test_missing_path_suggests_similar_existing_paths(project: Path) -> None:
    (project / "src/utils").mkdir()
    (project / "src/utils/io.py").write_text("load\n")

    misspelled = await dispatch(project, {"pattern": "load", "path": "scr/utlis"})
    assert misspelled["error"]["code"] == "path_not_found"
    assert "(similar: src/utils)" in misspelled["error"]["message"]
    assert "Nothing was searched." in misspelled["error"]["message"]

    repeated = await dispatch(project, {"pattern": "load", "path": f"{project.name}/src/utils"})
    assert "(similar: src/utils)" in repeated["error"]["message"]

    partial = await dispatch(project, {"pattern": "load", "path": ["src/utils", "docs/guid.md"]})
    assert partial["data"]["content"] == "src/utils/io.py:1:load"
    assert "(similar: docs/guide.md)" in partial["data"]["warnings"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["-F load() docs", '-F "Call load" docs', "  -F  load(  docs "])
async def test_a_command_line_string_in_args_splits_into_arguments(project: Path, args: str):
    result = await dispatch(project, {"args": args})
    assert result["data"]["content"] == "docs/guide.md:1:Call load() first."


@pytest.mark.asyncio
async def test_a_command_line_string_with_backslashes_asks_for_one_argument_per_item(
    project: Path,
) -> None:
    result = await dispatch(project, {"args": r"-e \bload\b docs"})
    assert result["error"]["code"] == "invalid_arguments"
    assert (
        r'pass one per item, for example ["-e", "\\bload\\b", "docs"]' in result["error"]["message"]
    )
