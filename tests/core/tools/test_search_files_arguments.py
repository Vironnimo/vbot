"""search_files arguments: named fields, argv spellings, repairs, and rejections."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from core.tools.contracts import ToolContractError
from core.tools.search_files import interpret_search_call, normalize_search_arguments
from tests.core.tools.search_files_test_support import dispatch


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "content"),
    [
        (
            {"pattern": "load"},
            "docs/guide.md\n1:Call load() first.\n\n"
            "src/app.py\n2:def load():\n\n"
            "src/view.ts\n1:export const load = 1;",
        ),
        ({"pattern": "load", "path": "src", "glob": "*.py"}, "src/app.py\n2:def load():"),
        (
            {"pattern": "load", "path": ["docs", "src/view.ts"]},
            "docs/guide.md\n1:Call load() first.\n\nsrc/view.ts\n1:export const load = 1;",
        ),
        (
            {"pattern": "load", "glob": ["*.py", "*.md"], "output": "files"},
            "docs/guide.md\nsrc/app.py",
        ),
        ({"pattern": "o", "path": "src/app.py", "output": "count"}, "src/app.py:3"),
        (
            {"pattern": "def", "context": 1},
            "src/app.py\n1-import os\n2:def load():\n3-    return os.name",
        ),
        (
            {"pattern": "LOAD", "args": ["-i"], "glob": "*.ts"},
            "src/view.ts\n1:export const load = 1;",
        ),
        (
            {"pattern": ["import", "export"]},
            "src/app.py\n1:import os\n\nsrc/view.ts\n1:export const load = 1;",
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
        ({"pattern": "", "path": "", "glob": [""]}, "src/app.py\nsrc/view.ts\ndocs/guide.md"),
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
        {"pattern": "app*.py"},
    ],
)
async def test_a_file_name_glob_in_pattern_lists_matching_files(project: Path, arguments: dict):
    result = await dispatch(project, arguments)
    assert result["data"]["content"] == "src/app.py"
    if "target" not in arguments:
        assert "is a file name glob" in result["data"]["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "listed"),
    [
        ({"pattern": r".*\.[pP][yY]$", "output": "files"}, "src/app.py"),
        ({"pattern": r"\.(py|ts)$", "output": "files"}, "src/app.py\nsrc/view.ts"),
        # A bare file name, as DeepSeek sent it to find a file (agent-eval 2026-10-10).
        ({"pattern": "app.py", "output": "files"}, "src/app.py"),
        ({"pattern": "src/view"}, "src/view.ts"),
    ],
)
async def test_a_content_search_without_matches_lists_files_whose_names_match(
    project: Path, arguments: dict, listed: str
):
    result = await dispatch(project, arguments)
    assert result["data"]["content"] == listed
    assert result["data"]["note"] == (
        "No file contents match pattern, so the files whose names match it were listed."
    )
    # Content matches keep the search a content search.
    contents = await dispatch(project, {**arguments, "pattern": arguments["pattern"] + "|load"})
    assert "matching line" in contents["data"]["summary"]
    assert contents["data"].get("note") != result["data"]["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "glob"),
    [
        # Swarm run 2026-10-12: names of files that did not exist, searched again and again.
        ({"pattern": r"load\.ts$", "output": "files"}, "*.ts"),
        ({"pattern": r"src/load_(a|b)\.py"}, "src/load_*.py"),
        ({"pattern": "load_a|load_b", "output": "files"}, "load_*"),
        # Text to find: one identifier, alternatives in content output, an extension no
        # searched file has, alternatives no file name starts like.
        ({"pattern": "load_a"}, None),
        ({"pattern": "load_a|load_b"}, None),
        ({"pattern": r"load\.rs$", "output": "files"}, None),
        ({"pattern": "save_a|save_b", "output": "files"}, None),
    ],
)
async def test_a_file_name_pattern_without_matches_says_no_name_matches_either(
    project: Path, arguments: dict, glob: str | None
):
    (project / "src/load_x.py").write_text("x = 1\n")
    result = await dispatch(project, arguments)
    assert "content" not in result["data"]
    if glob is None:
        assert "note" not in result["data"]
        return
    # The note's example glob lists the files with similar names.
    assert json.dumps(glob) in result["data"]["note"]
    listing = await dispatch(project, {"glob": glob})
    assert listing["data"]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "corrected", "files"),
    [
        (
            {"pattern": "*.py|*.ts", "output": "files"},
            {"glob": ["*.py", "*.ts"], "output": "files"},
            {"src/app.py", "src/view.ts"},
        ),
        (
            {"pattern": "*.py", "glob": ["*.py", "*.md", "!guide.md"]},
            {"glob": ["*.py", "*.md", "!guide.md"]},
            {"src/app.py"},
        ),
        (
            {"pattern": "*.py|*.md", "args": ["--iglob=*.py"]},
            {"args": ["--iglob=*.py"]},
            {"src/app.py"},
        ),
    ],
)
async def test_an_empty_repaired_glob_pattern_offers_a_name_search_without_changing_it(
    project: Path, arguments: dict, corrected: dict, files: set[str]
) -> None:
    original = copy.deepcopy(arguments)
    result = await dispatch(project, arguments)
    assert result["ok"], result
    assert "content" not in result["data"]
    assert arguments == original
    note = result["data"]["note"]
    assert "glob" in note and "pattern" in note
    if "glob" not in arguments and "args" not in arguments:
        assert json.dumps(corrected["glob"]) in note
    listing = await dispatch(project, corrected)
    assert set(listing["data"]["content"].splitlines()) == files
    # The same syntax can really be text to find. A match keeps its content result.
    (project / "src/app.py").write_text("a literal *.py example\n")
    matched = await dispatch(project, arguments)
    assert "src/app.py" in matched["data"]["content"]
    assert matched["data"]["note"] != note


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pattern", "noted"),
    [
        # Swarm run 2026-10-12: the "?" of a URL made the regex miss the URL.
        ("page.pl?id=7", True),
        ("load(x)", True),
        # The literal text is absent as well.
        ("page.pl?id=8", False),
    ],
)
async def test_a_regex_without_matches_names_the_literal_search_when_its_text_occurs(
    project: Path, pattern: str, noted: bool
):
    (project / "links.txt").write_text("see page.pl?id=7 and load(x)\n")
    result = await dispatch(project, {"pattern": pattern})
    assert "content" not in result["data"]
    assert ("note" in result["data"]) is noted
    literal = await dispatch(project, {"pattern": pattern, "args": ["-F"]})
    assert ("links.txt" in literal["data"].get("content", "")) is noted
    assert "note" not in literal["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pattern", "noted"),
    [
        (r'"uses": \[[\s\S]*?"note"', True),
        (r'(?s)"uses".*"note"', True),
        # Across lines it matches nothing either.
        (r'"uses": \[[\s\S]\{', False),
    ],
)
async def test_a_regex_without_matches_names_the_multiline_search_when_it_spans_lines(
    project: Path, pattern: str, noted: bool
):
    (project / "uses.json").write_text('{\n  "uses": [\n    {"note": 1}\n  ]\n}\n')
    result = await dispatch(project, {"pattern": pattern})
    assert "content" not in result["data"]
    assert ("-U" in result["data"].get("note", "")) is noted
    spanning = await dispatch(project, {"pattern": pattern, "args": ["-U"]})
    assert ("uses.json" in spanning["data"].get("content", "")) is noted
    assert "note" not in spanning["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "suggested", "occurrences"),
    [
        # Swarm run 2026-10-20: a phrase searched to confirm it was gone had wrapped in HTML.
        ({"pattern": "and the feed-in"}, r"and\s+the\s+feed-in", 1),
        # Matches within lines stay; the note counts the ones a line break hides.
        ({"pattern": "feed-in tariff"}, r"feed-in\s+tariff", 3),
        ({"pattern": "feed-in tariff|meter", "output": "files"}, r"feed-in\s+tariff|meter", 4),
        ({"pattern": "a (b) c", "args": ["-F"], "output": "count"}, r"a\s+\(b\)\s+c", 1),
        # No line break inside any occurrence, or a search that cannot span lines.
        ({"pattern": "meter reading"}, None, 0),
        ({"pattern": "feed-in tariff", "args": ["-v"], "glob": "*.html"}, None, 0),
        ({"pattern": "feed-in tariff", "args": ["-x"]}, None, 0),
    ],
)
async def test_a_phrase_search_counts_the_matches_a_line_break_hides(
    project: Path, arguments: dict, suggested: str | None, occurrences: int
):
    (project / "wrapped.html").write_text(
        "<p>The meter reading and the\n   feed-in tariff differ.</p>\n"
        "<p>Each feed-in\ntariff applies once.</p>\n"
        "<p>One feed-in tariff is shown.</p>\n"
        "<p>Keep a (b)\nc here.</p>\n"
    )
    result = await dispatch(project, arguments)
    assert result["ok"], result
    if suggested is None:
        assert "note" not in result["data"]
        return
    assert f'"{suggested}"' in result["data"]["note"]
    # The call the note names finds every occurrence, those within lines included.
    args = [arg for arg in arguments.get("args", []) if arg != "-F"]
    spanning = await dispatch(
        project, {"pattern": suggested, "args": [*args, "-U"], "output": "count"}
    )
    assert spanning["data"]["content"] == f"wrapped.html:{occurrences}"
    assert "note" not in spanning["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "noted"),
    [
        # Swarm run 2026-10-12: one counted line held the text three times.
        ({"pattern": "zz", "output": "count"}, True),
        ({"pattern": "zz", "args": ["-c"]}, True),
        # Each counted line holds one match, the call counts matches already, or the
        # pattern matches a shape whose matches nobody counts.
        ({"pattern": "yy", "output": "count"}, False),
        ({"pattern": "zz", "output": "count", "args": ["--count-matches"]}, False),
        ({"pattern": r"\w+", "output": "count", "glob": "counts.txt"}, False),
    ],
)
async def test_a_line_count_says_how_many_matches_the_lines_hold(
    project: Path, arguments: dict, noted: bool
):
    (project / "counts.txt").write_text("zz zz zz\nzz\nyy\n")
    result = await dispatch(project, arguments)
    assert result["data"]["content"].startswith("counts.txt:")
    assert ("note" in result["data"]) is noted
    if noted:
        assert "4" in result["data"]["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "named"),
    [
        # File names as a pattern find the files mentioning them; the note says which
        # files have those names (Swarm Sessions, 2026-10).
        ({"pattern": "app.py|verify.py", "output": "files"}, ["src/app.py"]),
        ({"pattern": "(app|verify)", "output": "files"}, ["src/app.py"]),
        ({"pattern": r"verify\.py", "output": "files"}, []),
        # No claim about names: an identifier, an extension no searched file has, content output.
        ({"pattern": "verify", "output": "files"}, None),
        ({"pattern": "verify.py", "output": "files", "glob": "*.md"}, None),
        ({"pattern": "notes.get|verify", "output": "files"}, None),
        ({"pattern": "verify.py"}, None),
    ],
)
async def test_a_file_list_search_for_file_names_names_the_files_with_those_names(
    project: Path, arguments: dict, named: list[str] | None
):
    (project / "notes.md").write_text("Run app.py, then verify.py.\n")
    result = await dispatch(project, arguments)
    assert "notes.md" in result["data"]["content"]
    if named is None:
        assert "note" not in result["data"]
        return
    note = result["data"]["note"]
    labels = ["src/app.py", "src/view.ts", "docs/guide.md", "notes.md"]
    assert [label for label in labels if label in note] == named


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "listed", "noted"),
    [
        ({"args": ["--files"], "pattern": "app"}, "src/app.py", True),
        ({"args": ["--files", "-e", "view"]}, "src/view.ts", True),
        ({"args": ["--files"], "pattern": "^src/v"}, "src/view.ts", True),
        ({"args": ["--files"], "pattern": "*.ts"}, "src/view.ts", True),
        ({"args": ["--dirs"], "pattern": "^do"}, "docs/", True),
        # A pattern that matches every name lists everything.
        ({"args": ["--files"], "pattern": "."}, "src/app.py\nsrc/view.ts\ndocs/guide.md", False),
        ({"args": ["--dirs"], "pattern": "*"}, "src/\ndocs/", False),
        # Under -v or -F the same pattern matches no name.
        ({"args": ["--files", "-v"], "pattern": "."}, "", True),
        ({"args": ["--dirs", "-F"], "pattern": "*"}, "", True),
        # Names stay separate records; a newline does not join two candidates.
        ({"args": ["--files"], "pattern": r"app\.py\nsrc/view\.ts"}, "", True),
    ],
)
async def test_a_pattern_beside_a_listing_matches_names(
    project: Path, arguments: dict, listed: str, noted: bool
):
    os.utime(project / "docs", (1_000_000_000, 1_000_000_000))
    result = await dispatch(project, arguments)
    assert result["ok"], result
    assert result["data"].get("content", "") == listed
    assert ("note" in result["data"]) is noted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "listed"),
    [
        ({"pattern": ".", "output": "files"}, True),
        ({"pattern": "^(.*)$", "args": ["-l"]}, True),
        # Under -x the pattern matches one-character lines only; count output counts lines.
        ({"pattern": ".", "output": "files", "args": ["-x"]}, False),
        ({"pattern": ".", "output": "count"}, False),
    ],
)
async def test_file_list_output_for_a_pattern_matching_every_line_lists_every_file(
    project: Path, arguments: dict, listed: bool
):
    (project / "empty.txt").write_text("")
    result = await dispatch(project, arguments)
    assert result["ok"], result
    if listed:
        assert sorted(result["data"]["content"].splitlines()) == [
            "docs/guide.md",
            "empty.txt",
            "src/app.py",
            "src/view.ts",
        ]
        assert result["data"]["note"]
    else:
        assert "empty.txt" not in result["data"].get("content", "")
        assert "note" not in result["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "content", "noted"),
    [
        ({"glob": "src"}, "src/app.py\nsrc/view.ts", True),
        ({"glob": ["src/", "!*.ts"]}, "src/app.py", True),
        ({"glob": "./docs", "pattern": "load"}, "docs/guide.md\n1:Call load() first.", True),
        ({"glob": "src", "path": "src"}, "src/app.py\nsrc/view.ts", True),
        # A name that is no directory, an excluding glob and a directory listing stay as sent.
        ({"glob": "missing"}, "", False),
        ({"glob": "!src"}, "docs/guide.md", False),
        ({"glob": "src", "args": ["--dirs"]}, "src/", False),
    ],
)
async def test_a_glob_naming_a_directory_matches_the_files_below_it(
    project: Path, arguments: dict, content: str, noted: bool
):
    result = await dispatch(project, arguments)
    assert result["ok"], result
    assert result["data"].get("content", "") == content
    assert ("note" in result["data"]) is noted


@pytest.mark.asyncio
async def test_a_glob_shaped_literal_search_stays_a_content_search(project: Path) -> None:
    (project / "notes.txt").write_text("match *.py files\n")
    result = await dispatch(project, {"pattern": "*.py", "args": ["-F"]})
    assert result["data"]["content"] == "notes.txt\n1:match *.py files"
    assert "note" not in result["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "content"),
    [
        # Claude Code's Grep.
        (
            {"pattern": "LOAD", "-i": True, "-n": True, "glob": "*.py", "output_mode": "content"},
            "src/app.py\n2:def load():",
        ),
        (
            {"pattern": "load", "output_mode": "files_with_matches", "head_limit": 1},
            "docs/guide.md",
        ),
        ({"pattern": "os", "output_mode": "count", "path": "src"}, "src/app.py:2"),
        (
            {"pattern": "load", "output": "output>content", "glob": "*.py"},
            "src/app.py\n2:def load():",
        ),
        ({"pattern": "load", "output": "output>files", "glob": "*.py"}, "src/app.py"),
        ({"pattern": "os", "output": "output>count", "path": "src"}, "src/app.py:2"),
        (
            {"pattern": "def", "-C": 1, "path": "src/app.py"},
            "src/app.py\n1-import os\n2:def load():\n3-    return os.name",
        ),
        (
            {"pattern": "def", "-A": 1, "type": "py"},
            "src/app.py\n2:def load():\n3-    return os.name",
        ),
        # Hermes and opencode.
        ({"pattern": "*.ts", "target": "files", "path": "src"}, "src/view.ts"),
        (
            {"pattern": "load", "target": "content", "file_glob": "*.md"},
            "docs/guide.md\n1:Call load() first.",
        ),
        ({"pattern": "load", "include": "*.ts"}, "src/view.ts\n1:export const load = 1;"),
        # Common spellings.
        (
            {"query": "load()", "literal": True, "directory": "docs"},
            "docs/guide.md\n1:Call load() first.",
        ),
        (
            {"regex": "LOAD", "ignoreCase": "true", "exclude": ["*.md", "*.ts"]},
            "src/app.py\n2:def load():",
        ),
        (
            {"pattern": "load(", "regex": False, "path": "docs"},
            "docs/guide.md\n1:Call load() first.",
        ),
        (
            {"pattern": "Load", "case_sensitive": False, "file_type": "ts"},
            "src/view.ts\n1:export const load = 1;",
        ),
        ({"pattern": "o", "-c": True, "path": "src/app.py"}, "src/app.py:3"),
        ({"pattern": "load", "-l": True, "glob": "*.md"}, "docs/guide.md"),
        ({"recursive": False, "args": ["--dirs"]}, "src/\ndocs/"),
        # Lists sent as JSON text.
        (
            {"pattern": "load", "path": '["docs", "src/view.ts"]'},
            "docs/guide.md\n1:Call load() first.\n\nsrc/view.ts\n1:export const load = 1;",
        ),
        (
            {"pattern": "load", "glob": '[ "*.py", "*.md" ]', "output": "files"},
            "docs/guide.md\nsrc/app.py",
        ),
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
        (["-rn", "def", "."], "src/app.py\n2:def load():"),
        (
            ["-E", "import|export", "src"],
            "src/app.py\n1:import os\n\nsrc/view.ts\n1:export const load = 1;",
        ),
        (["--include=*.md", "load"], "docs/guide.md\n1:Call load() first."),
        (
            ["--exclude", "*.py", "--exclude-dir=docs", "load"],
            "src/view.ts\n1:export const load = 1;",
        ),
        (["-E", "utf-8", "def"], "src/app.py\n2:def load():"),
        (["-h", "def", "src"], "src/app.py\n2:def load():"),
    ],
)
async def test_grep_habits_in_args_keep_their_meaning(project: Path, args: list[str], content: str):
    result = await dispatch(project, {"args": args})
    assert result["ok"], result
    assert result["data"]["content"] == content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"pattern": "*.ts", "path": "src/*.py"}, ""),
        ({"pattern": "def", "path": "src/*.py"}, 'so src was searched with glob "/src/*.py"'),
        ({"pattern": "def", "path": "{src,docs}"}, "names several paths, so src, docs"),
        ({"pattern": "def", "path": "src,docs"}, "names several paths, so src, docs"),
        # A missing path starting with ! is an excluding glob; alone it searches the cwd.
        (
            {"args": ["load", "!docs/**", "!*.ts"]},
            'path "!docs/**" starts with !, so it was applied as an excluding glob.',
        ),
    ],
)
async def test_a_path_written_as_a_glob_or_list_searches_what_it_names(
    project: Path, arguments: dict, message: str
) -> None:
    result = await dispatch(project, arguments)
    if not message:
        # Another field already selects names, so the path stays a missing path.
        assert result["error"]["code"] == "path_not_found"
        return
    assert result["data"]["content"] == "src/app.py\n2:def load():"
    assert message in result["data"]["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("glob", "workdir", "content", "note"),
    [
        pytest.param(
            "src\\*.py",
            ".",
            "src/app.py\n2:def load():",
            None,
            id="backslashes",
            marks=pytest.mark.skipif(os.name != "nt", reason="Windows separators"),
        ),
        pytest.param("{root}{sep}src{sep}*.py", ".", "src/app.py\n2:def load():", None, id="abs"),
        pytest.param(
            "{root}/docs",
            ".",
            "docs/guide.md\n1:Call load() first.",
            'glob "/docs" names a directory',
            id="absolute-directory",
        ),
        # Outside the working directory, the glob's fixed directory is searched.
        pytest.param(
            "{root}/docs/*.md",
            "src",
            "{root}/docs/guide.md\n1:Call load() first.",
            'is an absolute path, so {root}/docs was searched with glob "/*.md".',
            id="outside",
        ),
    ],
)
async def test_a_glob_written_as_a_windows_or_absolute_path_selects_what_it_names(
    project: Path, glob: str, workdir: str, content: str, note: str | None
) -> None:
    # Both matched nothing: ripgrep reads \ as an escape and matches globs below the root.
    root = project.as_posix()
    arguments = {"pattern": "load", "glob": glob.format(root=root, sep=os.sep)}
    result = await dispatch(project / workdir, arguments)
    assert result["data"]["content"] == content.format(root=root)
    if note is None:
        assert "note" not in result["data"]
    else:
        assert note.format(root=root) in result["data"]["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"pattern": "load", "query": "other"},
        {"pattern": "load", "-l": True, "output": "count"},
        {"pattern": "load", "ignore_case": "maybe"},
        {"pattern": "load", "output": "output>content>files"},
        {"pattern": "load", "output": "target>content"},
        {"pattern": "load", "output": "output>contents"},
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

    unknown = await dispatch(project, {"pattern": "load", "path": "nothing/like/this"})
    assert unknown["error"]["message"] == (
        "Path not found: nothing/like/this (relative to the working directory "
        f"{project.as_posix()}). Nothing was searched. Correct path, or omit it to search "
        "the working directory."
    )

    repeated = await dispatch(project, {"pattern": "load", "path": f"{project.name}/src/utils"})
    assert "(similar: src/utils)" in repeated["error"]["message"]

    partial = await dispatch(project, {"pattern": "load", "path": ["src/utils", "docs/guid.md"]})
    assert partial["data"]["content"] == "src/utils/io.py\n1:load"
    assert "(similar: docs/guide.md)" in partial["data"]["warnings"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("args", ["-F load() docs", '-F "Call load" docs', "  -F  load(  docs "])
async def test_a_command_line_string_in_args_splits_into_arguments(project: Path, args: str):
    result = await dispatch(project, {"args": args})
    assert result["data"]["content"] == "docs/guide.md\n1:Call load() first."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "content"),
    [
        ({"args": '--files"]', "pattern": "."}, "src/app.py\nsrc/view.ts\ndocs/guide.md"),
        ({"args": "['--files']", "path": "src"}, "src/app.py\nsrc/view.ts"),
        ({"args": "[--dirs]", "pattern": "do"}, "docs/"),
        ({"args": '["-i", "-t", "ts"', "pattern": "LOAD"}, "src/view.ts\n1:export const load = 1;"),
        # Text that is not only flags stays what it was: here a regex class, the pattern.
        ({"args": "[u]", "path": "src"}, "src/app.py\n3:    return os.name"),
    ],
)
async def test_flags_wrapped_in_stray_list_syntax_are_those_flags(
    project: Path, arguments: dict, content: str
):
    os.utime(project / "docs", (1_000_000_000, 1_000_000_000))
    result = await dispatch(project, arguments)
    assert result["ok"], result
    assert result["data"]["content"] == content


@pytest.mark.asyncio
async def test_a_command_line_string_with_backslashes_asks_for_one_argument_per_item(
    project: Path,
) -> None:
    result = await dispatch(project, {"args": r"-e \bload\b docs"})
    assert result["error"]["code"] == "invalid_arguments"
    assert (
        r'pass one per item, for example ["-e", "\\bload\\b", "docs"]' in result["error"]["message"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [[], ["--files"], ["-l"]])
async def test_missing_explicit_roots_preserve_results_and_name_the_missing_path(tmp_path, mode):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("needle\n")
    (tmp_path / "outside.py").write_text("needle\n")
    patterns = [] if "--files" in mode else ["needle"]
    args = [*mode, *patterns, "missing", "src"]
    result = await dispatch(tmp_path, {"args": args})
    assert result["ok"]
    data = result["data"]
    assert data["warnings"][0] == (
        f"Path not found: missing (relative to the working directory {tmp_path.as_posix()})."
    )
    assert data["searched_paths"] == ["src"]
    assert data["content"] == ("src/a.py" if mode else "src/a.py\n1:needle")


@pytest.mark.asyncio
async def test_missing_path_is_not_reinterpreted_as_pattern(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/code").write_text("Alpha\nBeta\ncall(\n")
    partial = await dispatch(tmp_path, {"args": ["Alpha", "Beta", "Gamma", "lib/a.py", "src"]})
    assert partial["data"]["content"] == "src/code\n1:Alpha"
    assert any('pattern "Alpha|Beta|Gamma".' in warning for warning in partial["data"]["warnings"])
    missing = await dispatch(tmp_path, {"args": ["Alpha", "Beta"]})
    assert missing["error"]["code"] == "path_not_found"
    literal_hint = await dispatch(tmp_path, {"args": ["-F", "Alpha", "Beta"]})
    assert '["-F", "-e", "Alpha", "-e", "Beta"]' in literal_hint["error"]["message"]
    for corrected_call in (
        {"args": ["-e", "Alpha", "-e", "Beta", "src"]},
        {"pattern": "Alpha|Beta", "path": "src"},
    ):
        corrected = await dispatch(tmp_path, corrected_call)
        assert corrected["data"]["content"] == "src/code\n1:Alpha\n2:Beta"
        assert "warnings" not in corrected["data"]
    literal = await dispatch(tmp_path, {"args": ["-F", "call(", "src"]})
    assert literal["data"]["content"] == "src/code\n3:call("
    # With pattern set, every args item without a leading - is a path: the error
    # names the -e form, which runs as suggested and keeps the other items.
    words = await dispatch(tmp_path, {"pattern": "Alpha", "args": ["beta", "-i"]})
    message = words["error"]["message"]
    assert words["error"]["code"] == "path_not_found"
    assert "Correct path, or omit it" not in message
    suggested = json.loads(message.split("args ")[-1].split("]")[0] + "]")
    corrected = await dispatch(tmp_path, {"pattern": "Alpha", "args": suggested})
    assert corrected["data"]["content"] == "src/code\n1:Alpha\n2:Beta"
    # A path with a similar existing one was most likely mistyped.
    mistyped = await dispatch(tmp_path, {"pattern": "Alpha", "args": ["scr"]})
    assert '"-e"' not in mistyped["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "pattern", "searched", "content"),
    [
        *[
            (options, pattern, searched, content)
            for options in ([], ["-P"])
            for pattern, searched, content in (
                ("call(", r"call\(", "src/code\n3:call("),
                ("Beta|call(", r"Beta|call\(", "src/code\n2:Beta\n3:call("),
                ("x)|Alpha", r"x\)|Alpha", "src/code\n1:Alpha"),
            )
        ],
        # PCRE2 already reads a leading brace literally; the default engine rejects it.
        ([], "{Alpha|Beta", r"\{Alpha|Beta", "src/code\n2:Beta"),
        ([], "Beta{2", r"Beta\{2", ""),
    ],
)
async def test_unbalanced_regex_characters_match_literally_with_a_note(
    tmp_path, options, pattern, searched, content
):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/code").write_text("Alpha\nBeta\ncall(\n")

    result = await dispatch(tmp_path, {"args": [*options, pattern, "src"]})

    assert result["data"].get("content", "") == content
    assert f'searched "{pattern}" as "{searched}"' in result["data"]["note"]
    assert "-F" in result["data"]["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pattern", "options", "content", "noted"),
    [
        (r"Alpha\nBeta", {}, "code\n1:Alpha\n2:Beta", True),
        ("Alpha\nBeta", {}, "code\n1:Alpha\n2:Beta", True),
        # Adding multiline does not make . match a line break.
        (r"Alpha.*\nBeta", {}, "code\n1:Alpha\n2:Beta", True),
        (
            r"Alpha\nBeta",
            {"args": ["--no-multiline-dotall"]},
            "code\n1:Alpha\n2:Beta",
            True,
        ),
        # Literal escapes and an explicit literal mode keep their meaning.
        (r"Alpha\\nBeta", {}, "code\n6:Alpha\\nBeta", False),
        (r"Alpha\nBeta", {"args": ["-F"]}, "code\n6:Alpha\\nBeta", False),
        (r"Alpha\nBeta", {"literal": True}, "code\n6:Alpha\\nBeta", False),
        (r"Alpha\nBeta", {"args": ["--no-multiline"]}, None, False),
        (r"Alpha\nBeta", {"multiline": False}, None, False),
        (r"Alpha\nBeta", {"-U": False}, None, False),
        (r"Alpha\nBeta", {"args": ["-U", "--no-multiline"]}, None, False),
        (
            r"Alpha\nBeta",
            {"args": ["--no-multiline", "-U"]},
            "code\n1:Alpha\n2:Beta",
            False,
        ),
    ],
)
async def test_rejected_regex_line_breaks_run_multiline_without_overriding_explicit_modes(
    tmp_path, pattern, options, content, noted
):
    (tmp_path / "code").write_text(
        "Alpha\nBeta\nAlpha\nbetween\nBeta\nAlpha\\nBeta\n", newline="\n"
    )

    result = await dispatch(tmp_path, {"pattern": pattern, "path": "code", **options})

    if content is None:
        assert result["ok"] is False
        assert result["error"]["code"] == "search_error"
        return
    assert result["ok"], result
    assert result["data"]["content"] == content
    assert ("note" in result["data"]) is noted


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["--no-filename", "-N", "--no-line-number"])
async def test_display_flags_run_the_search_with_a_note(tmp_path, flag):
    (tmp_path / "cases").mkdir()
    (tmp_path / "cases/a.sql").write_text('-- naming="first"\nSELECT 1;\n')

    result = await dispatch(
        tmp_path,
        {"args": ["-o", flag], "path": "cases", "pattern": 'naming="([^"]+)"', "limit": 45},
    )

    assert result["ok"], result
    assert 'naming="first"' in result["data"]["content"]
    assert result["data"]["note"] == (
        f"{flag} was ignored: results always name each match's file and line."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("offered", "advice"),
    [
        (None, "; use apply_patch to change files"),
        (("search_files", "edit", "write"), "; use edit to change files"),
        (("search_files",), ""),
    ],
)
async def test_replace_shows_the_original_lines_and_names_the_offered_edit_tool(
    tmp_path, offered, advice
):
    (tmp_path / "a.txt").write_text("old\n")

    result = await dispatch(
        tmp_path, {"pattern": "old", "args": ["--replace", "new"]}, offered_tools=offered
    )

    assert result["ok"], result
    assert result["data"]["content"] == "a.txt\n1:old"
    assert result["data"]["note"] == (
        f"--replace was ignored: results show the original lines{advice}."
    )


@pytest.mark.asyncio
async def test_look_around_runs_with_pcre2_and_says_so(tmp_path):
    (tmp_path / "code.py").write_text("price = 1\nprice_total = 2\n")

    result = await dispatch(tmp_path, {"args": ["price(?!_)"]})

    assert result["data"]["content"] == "code.py\n1:price = 1"
    assert "PCRE2" in result["data"]["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        (r"Alpha|16 \·", "code\n1:Alpha\n2:16 · cases"),
        (r"[\·—]", "code\n2:16 · cases\n3:long—dash\n4:call(·"),
        (r"long\—dash|Beta", "code\n3:long—dash"),
        (r"Alpha|call(\·", "code\n1:Alpha\n4:call(·"),
    ],
)
async def test_escaped_unicode_punctuation_preserves_regex_meaning(tmp_path, pattern, expected):
    (tmp_path / "code").write_text("Alpha\n16 · cases\nlong—dash\ncall(·\n", encoding="utf-8")

    result = await dispatch(tmp_path, {"pattern": pattern, "path": "code"})

    assert result["ok"], result
    assert result["data"]["content"] == expected
    assert "note" in result["data"]
    if pattern == r"Alpha|call(\·":
        assert "unbalanced regex characters" in result["data"]["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [["-F"], []])
async def test_literal_backslashes_before_unicode_punctuation_are_not_repaired(tmp_path, args):
    (tmp_path / "code").write_text("16 \\·\n16 ·\n", encoding="utf-8")
    pattern = r"16 \·" if args else r"16 \\·"

    result = await dispatch(tmp_path, {"args": [*args, pattern, "code"]})

    assert result["data"]["content"] == "code\n1:16 \\·"
    assert "note" not in result["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", [r"\q|Alpha", r"\é|Alpha", r"[\·|Alpha"])
async def test_unicode_escape_repair_does_not_hide_unrelated_invalid_regex(tmp_path, pattern):
    (tmp_path / "code").write_text("Alpha\n", encoding="utf-8")

    result = await dispatch(tmp_path, {"pattern": pattern, "path": "code"})

    assert result["ok"] is False
    assert result["error"]["code"] == "search_error"
    assert "Nothing was searched." in result["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "shown", "column", "width"),
    [
        ({"pattern": r"tools\(\corpus|fuzz"}, r"tools\(\corpus|fuzz", 7, 2),
        ({"pattern": "ok", "args": ["-e", r"y\q"]}, r"y\q", 1, 2),
        ({"pattern": "[z-a]x", "args": ["--dirs"]}, "[z-a]x", 1, 3),
        # A PCRE2 error has no caret to place: ripgrep's message stays.
        ({"pattern": r"x(?<=\c", "args": ["-P"]}, None, 0, 0),
    ],
)
async def test_a_regex_error_points_at_the_pattern_as_sent(
    tmp_path, arguments, shown, column, width
):
    # ripgrep reports errors in its combined (?:p1)|(?:p2) form, with the caret placed there.
    (tmp_path / "code").write_text("ok\n", encoding="utf-8")
    result = await dispatch(tmp_path, arguments)

    message = result["error"]["message"]
    assert result["error"]["code"] == "search_error"
    if shown is None:
        assert "PCRE2" in message
        return
    assert "(?:" not in message
    lines = message.splitlines()
    caret = lines[lines.index(f"    {shown}") + 1]
    assert caret == "    " + " " * column + "^" * width


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected", "output"),
    [
        ({"output": "count", "context": 2}, "code:2", "count"),
        ({"args": ["-c", "-C", "2"]}, "code:2", "count"),
        ({"args": ["-C2", "--count-matches"]}, "code:3", "count"),
        ({"output": "files", "context": 2, "args": ["-c"]}, "code:2", "count"),
        ({"output": "count", "context": 2, "args": ["--count-matches"]}, "code:3", "count"),
        ({"output": "files", "context": 3}, "code", "file-list"),
        ({"args": ["-c", "-C", "2", "-l"]}, "code", "file-list"),
        ({"args": ["-q", "-C", "2"]}, "code", "file-list"),
        ({"path": ".", "args": ["-A1", "--files-without-match"]}, "other", "file-list"),
    ],
)
async def test_count_and_file_list_output_ignore_context_after_resolving_option_precedence(
    tmp_path, arguments, expected, output
):
    (tmp_path / "code").write_text("before\nneedle needle\nneedle\nafter\n")
    (tmp_path / "other").write_text("nothing\n")
    supplied = {"pattern": "needle", "path": "code", **arguments}
    original = copy.deepcopy(supplied)

    result = await dispatch(tmp_path, supplied)

    assert supplied == original
    assert result["ok"], result
    assert result["data"]["content"] == expected
    assert f"Context was ignored because {output} output" in result["data"]["note"]


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (["-n", "needle", "src"], {"patterns": ["needle"], "roots": ["src"]}),
        (
            ["needle", "src", "-iw"],
            {
                "patterns": ["needle"],
                "roots": ["src"],
                "rg_args": ["--ignore-case", "--word-regexp"],
            },
        ),
        (["-g", "-F", "needle"], {"patterns": ["needle"], "globs": ["-F"]}),
        (["-e", "needle", "-eother", "src"], {"patterns": ["needle", "other"], "roots": ["src"]}),
        (["src", "--regexp=needle"], {"patterns": ["needle"], "roots": ["src"]}),
        (["--", "-needle", "-root"], {"patterns": ["-needle"], "roots": ["-root"]}),
        (
            ["--files", "-g", "*.py", "src"],
            {"mode": "list_files", "globs": ["*.py"], "roots": ["src"]},
        ),
        (["--dirs", "src"], {"mode": "list_dirs", "roots": ["src"]}),
        (
            ["-F", "a b", r"C:\source files"],
            {"patterns": ["a b"], "roots": [r"C:\source files"], "rg_args": ["--fixed-strings"]},
        ),
        (["rg"], {"patterns": ["rg"]}),
        (["-g", "--help", "needle"], {"patterns": ["needle"], "globs": ["--help"]}),
        (["-e", "--files"], {"patterns": ["--files"]}),
        (["-e", "--offset"], {"patterns": ["--offset"]}),
        (["-g", "--offset", "needle"], {"patterns": ["needle"], "globs": ["--offset"]}),
        (["--", "--offset", "--limit"], {"patterns": ["--offset"], "roots": ["--limit"]}),
        (["-e", ""], {"patterns": [""]}),
        (["-m=2", "-tpy", "x"], {"patterns": ["x"], "rg_args": ["--max-count=2", "--type=py"]}),
    ],
)
def test_argv_boundaries_have_independently_specified_meaning(tokens, expected):
    query = interpret_search_call({"args": tokens})
    defaults = {"mode": "content", "patterns": [], "roots": [], "globs": [], "rg_args": []}
    for name, value in {**defaults, **expected}.items():
        assert getattr(query, name) == value, name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "supplied",
    [
        {"args": ["-F", "add_recipe(", "src"]},
        {"argv": ["-F", "add_recipe(", "src"]},
        {"request": {"args": '["-F", "add_recipe(", "src"]'}},
        {"args": ["-e", r"add_recipe\(", "src"]},
        {"args": r'["-e", "add_recipe\(", "src"]'},
    ],
)
async def test_first_call_finds_only_intended_content(tmp_path, supplied):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/code.py").write_text("def add_recipe():\n    pass\n")
    (tmp_path / "other.py").write_text("def add_recipe():\n")
    before = copy.deepcopy(supplied)
    result = await dispatch(tmp_path, supplied)
    assert supplied == before
    assert result["ok"]
    assert result["data"]["content"] == "src/code.py\n1:def add_recipe():"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pattern",
    [
        r'["call\("]',
        r"[abc]",
        '"item:generated"',
        r"\bcall\(",
        "a\nb",
        r"\u0041",
        "--help",
        "-F",
        "$(anything)",
        "a; b | c",
    ],
)
async def test_literal_payloads_are_never_shell_or_repair_syntax(tmp_path, pattern):
    (tmp_path / "text").write_text(pattern, newline="\n")
    arguments = {"args": ["-F", "-l", "-U", "-e", pattern]}
    result = await dispatch(tmp_path, arguments)
    assert result["ok"]
    assert result["data"]["content"] == "text"
    assert normalize_search_arguments(arguments)["args"][-1] == pattern


_LIST = "args contains a malformed encoded list"
_PATHS = "path must name a file or directory"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "code", "message"),
    [
        # Contradictory modes.
        ({"args": ["--files", "--dirs"]}, "invalid_arguments", "Choose one of --files or --dirs"),
        (
            {"pattern": "a{2,1}", "args": ["--dirs"]},
            "search_error",
            "The pattern is not a valid regular expression (ripgrep syntax)",
        ),
        ({"args": ["--help", "missing"]}, "invalid_arguments", "--help shows a reference"),
        # Incomplete or unavailable options, and roots that are not paths.
        ({"args": ["--files", "-g"]}, "invalid_arguments", "-g in args needs a value"),
        ({"args": ["--regexp"]}, "invalid_arguments", "--regexp in args needs a value"),
        ({"args": ["-F=true", "needle"]}, "invalid_arguments", "-F takes no value"),
        ({"args": ["--pre", "program", "needle"]}, "invalid_arguments", "not available"),
        ({"args": ["--foo", "needle"]}, "search_error", 'ripgrep has no flag "--foo"'),
        ({"args": ["-t", "nosuchtype", "needle"]}, "search_error", "--type-list"),
        ({"args": ["-g", "{a", "needle"]}, "search_error", 'The glob "{a" is invalid'),
        # Another refusal keeps ripgrep's message and points to the flag reference.
        (
            {"pattern": r"needle\w+", "args": ["--regex-size-limit=10"]},
            "search_error",
            "exceeds size limit",
        ),
        ({"args": ["-F", "needle", ""]}, "invalid_arguments", _PATHS),
        ({"args": ["needle", "-"]}, "invalid_arguments", _PATHS),
        ({"args": ["--files", "missing"]}, "path_not_found", "Nothing was searched."),
        ({"args": ["needle", "--files"]}, "path_not_found", "replace --files with -l"),
        # Paging flags never discard a conflicting or invalid page.
        (
            {"args": ["needle", "--offset=1"], "offset": 2},
            "invalid_arguments",
            "offset was given different values",
        ),
        (
            {"args": ["needle", "--offset=1", "--offset=2"]},
            "invalid_arguments",
            "offset was given different values",
        ),
        (
            {"args": ["needle", "--limit=1"], "limit": 2},
            "invalid_arguments",
            "limit was given different values",
        ),
        (
            {"args": ["needle", "--head=1"], "limit": 2},
            "invalid_arguments",
            "limit was given different values",
        ),
        ({"args": ["needle", "--head=0"]}, "invalid_arguments", "between 1 and 10000"),
        ({"args": ["needle", "--head=1.5"]}, "invalid_arguments", "between 1 and 10000"),
        ({"args": ["needle", "--head"]}, "invalid_arguments", "--head in args needs a value"),
        ({"args": ["needle", "--offset=-1"]}, "invalid_arguments", "between 0 and 1000000"),
        ({"args": ["needle", "--offset=1.5"]}, "invalid_arguments", "between 0 and 1000000"),
        ({"args": ["needle", "--offset=1000001"]}, "invalid_arguments", "between 0 and 1000000"),
        ({"args": ["needle", "--offset"]}, "invalid_arguments", "--offset in args needs a value"),
        ({"args": ["needle", "--limit=0"]}, "invalid_arguments", "between 1 and 10000"),
        ({"args": ["needle", "--limit=10001"]}, "invalid_arguments", "between 1 and 10000"),
        # Rejected before the search runs.
        (
            {"args": ["--files"], "limit": 0},
            "invalid_arguments",
            "limit must be between 1 and 10000; received 0",
        ),
        (
            {"args": ["--files"], "offset": -1},
            "invalid_arguments",
            "offset must be between 0 and 1000000; received -1",
        ),
        ({"args": ["needle"], "argv": ["other"]}, None, "Conflicting values for args"),
        ({"args": r'["-e", "\bcall\("]'}, None, _LIST),
        ({"args": '["needle",]'}, None, _LIST),
        # A value next to list syntax could hold a regex class: its brackets stay.
        ({"args": '["-e", "[ab]"'}, None, _LIST),
    ],
)
async def test_ambiguous_or_unsupported_requests_reject_without_substitution(
    tmp_path, arguments, code, message
):
    (tmp_path / "code").write_text("needle\n")
    before = copy.deepcopy(arguments)
    if code is None:
        with pytest.raises(ValueError, match=message):
            await dispatch(tmp_path, arguments)
    else:
        result = await dispatch(tmp_path, arguments)
        assert result["ok"] is False
        assert result["data"] is None
        assert result["error"]["code"] == code
        assert message in result["error"]["message"]
    assert arguments == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "field", "content"),
    [
        # Swarm run 2026-10-12: a bare --glob beside the glob that gives its value.
        ({"args": ["--glob"], "glob": "*.py", "output": "files"}, "glob", "src/app.py"),
        (
            {"args": ["-i", "-e"], "pattern": "LOAD", "glob": "*.ts"},
            "pattern",
            "src/view.ts\n1:export const load = 1;",
        ),
    ],
)
async def test_a_flag_without_its_value_beside_the_field_that_gives_it_is_ignored(
    project: Path, arguments: dict, field: str, content: str
):
    result = await dispatch(project, arguments)

    assert result["data"]["content"] == content
    assert result["data"]["note"]
    # Without that field the flag still needs its value.
    alone = await dispatch(
        project, {key: value for key, value in arguments.items() if key != field}
    )
    assert alone["error"]["code"] == "invalid_arguments"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "note"),
    [
        (["--files", "-c"], "-c was ignored because --files lists files"),
        (["--dirs", "-l"], "-l was ignored because --dirs lists directories"),
    ],
)
async def test_output_flags_in_a_listing_are_ignored_with_a_note(tmp_path, args, note):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/code").write_text("needle\n")
    result = await dispatch(tmp_path, {"args": args})
    assert result["data"]["content"] in {"src/code", "src/"}
    assert note in result["data"]["note"]


@pytest.mark.asyncio
async def test_path_discovery_case_scope_and_pagination(tmp_path):
    (tmp_path / "nested/EMPTY").mkdir(parents=True)
    (tmp_path / "nested/EMPTY/keep").write_text("")
    (tmp_path / "nested/a.PY").write_text("irrelevant\n")
    (tmp_path / "b.py").write_text("irrelevant\n")
    dirs = await dispatch(tmp_path, {"args": ["--dirs", "-g", "empty"]})
    assert dirs["data"]["content"] == "nested/EMPTY/"
    sensitive = await dispatch(
        tmp_path, {"args": ["--dirs", "--no-glob-case-insensitive", "-g", "empty"]}
    )
    assert "content" not in sensitive["data"]
    shallow = await dispatch(tmp_path, {"args": ["--dirs", "-d", "1"]})
    assert shallow["data"]["content"] == "nested/"
    files = {"args": ["--files", "-g", "*.py", "--sort=path"], "limit": 1}
    first = await dispatch(tmp_path, files)
    assert first["data"]["content"] == "b.py"
    assert first["data"]["next_offset"] == 1
    second = await dispatch(tmp_path, {**files, "offset": first["data"]["next_offset"]})
    assert second["data"]["content"] == "nested/a.PY"
    assert "next_offset" not in second["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"args": ["needle", "--offset", "1", "--limit=1"]},
        {"args": ["--offset=1", "needle"], "limit": 1},
        {"args": ["needle", "--offset=01", "--limit", "1"], "offset": 1, "limit": 1},
        {"args": ["needle", "--head", "1"], "offset": 1},
        {"args": ["--head=1", "needle"], "offset": 1, "limit": 1},
    ],
)
async def test_paging_flags_select_same_exact_results_as_fields(tmp_path, arguments):
    (tmp_path / "a.py").write_text("needle first\nneedle second\nneedle third\n")
    before = copy.deepcopy(arguments)
    result = await dispatch(tmp_path, arguments)
    assert arguments == before
    assert result["ok"], result
    assert result["data"]["content"] == "a.py\n2:needle second"
    assert result["data"]["next_offset"] == 2


@pytest.mark.asyncio
async def test_paging_option_spellings_remain_literal_payloads(tmp_path):
    (tmp_path / "--limit").write_text("--offset\n")
    result = await dispatch(tmp_path, {"args": ["--", "--offset", "--limit"]})
    assert result["ok"], result
    assert result["data"]["content"] == "--limit\n1:--offset"


@pytest.mark.asyncio
async def test_quotes_in_a_pattern_are_searched_literally_in_the_actual_cwd(tmp_path):
    workspace = tmp_path / "workspace"
    project = tmp_path / "project"
    workspace.mkdir()
    project.mkdir()
    (project / "events.ts").write_text("type Event = 'item:generated';\n")
    result = await dispatch(workspace, {"args": ['"item:generated"']}, cwd=project)
    assert result["data"] == {"summary": "No matches in 1 searched file."}
    found = await dispatch(workspace, {"args": ["item:generated"]}, cwd=project)
    assert found["data"]["content"] == "events.ts\n1:type Event = 'item:generated';"


@pytest.mark.asyncio
async def test_help_examples_execute_the_advertised_searches(tmp_path):
    (tmp_path / "src/db/migrations").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / ".gitignore").write_text("*.log\n")
    (tmp_path / "src/a.py").write_text("x = 1\ndef load_config():\n    connect(x)\n# TODO\n")
    (tmp_path / "src/db/migrations/001.sql").write_text("-- todo\n")
    (tmp_path / "tests/b.py").write_text("def load_config():\n")
    (tmp_path / "errors.log").write_text("ERROR\n")
    help_result = await dispatch(tmp_path, {"args": ["--help"]})
    calls = [
        json.loads(line)
        for line in help_result["data"]["content"].splitlines()
        if line.strip().startswith("{")
    ]
    results = [await dispatch(tmp_path, call) for call in calls]
    assert all(result["ok"] for result in results), results
    assert [result["data"]["content"] for result in results] == [
        "src/a.py\n1-x = 1\n2:def load_config():\n3-    connect(x)\n4-# TODO\n\n"
        "tests/b.py\n1:def load_config():",
        "src/a.py:1",
        "src/a.py\n3:    connect(x)",
        "errors.log",
        "src/db/migrations/",
    ]
