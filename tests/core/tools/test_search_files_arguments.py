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
            "src/app.py-1-import os\nsrc/app.py:2:def load():\nsrc/app.py-3-    return os.name",
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
        {"pattern": "app*.py"},
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
            "src/app.py-1-import os\nsrc/app.py:2:def load():\nsrc/app.py-3-    return os.name",
        ),
        (
            {"pattern": "def", "-A": 1, "type": "py"},
            "src/app.py:2:def load():\nsrc/app.py-3-    return os.name",
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
        ({"recursive": False, "args": ["--dirs"]}, "src/\ndocs/"),
        # Lists sent as JSON text.
        (
            {"pattern": "load", "path": '["docs", "src/view.ts"]'},
            "docs/guide.md:1:Call load() first.\nsrc/view.ts:1:export const load = 1;",
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
        (["-h", "def", "src"], "src/app.py:2:def load():"),
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
    assert result["data"]["content"] == "src/app.py:2:def load():"
    assert message in result["data"]["note"]


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

    unknown = await dispatch(project, {"pattern": "load", "path": "nothing/like/this"})
    assert unknown["error"]["message"] == (
        "Path not found: nothing/like/this (relative to the working directory "
        f"{project.as_posix()}). Nothing was searched. Correct path, or omit it to search "
        "the working directory."
    )

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
    assert data["searched_paths"] == [(tmp_path / "src").as_posix()]
    assert data["content"] == ("src/a.py" if mode else "src/a.py:1:needle")


@pytest.mark.asyncio
async def test_missing_path_is_not_reinterpreted_as_pattern(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/code").write_text("Alpha\nBeta\ncall(\n")
    partial = await dispatch(tmp_path, {"args": ["Alpha", "Beta", "Gamma", "lib/a.py", "src"]})
    assert partial["data"]["content"] == "src/code:1:Alpha"
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
        assert corrected["data"]["content"] == "src/code:1:Alpha\nsrc/code:2:Beta"
        assert "warnings" not in corrected["data"]
    literal = await dispatch(tmp_path, {"args": ["-F", "call(", "src"]})
    assert literal["data"]["content"] == "src/code:3:call("


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "pattern", "searched", "content"),
    [
        *[
            (options, pattern, searched, content)
            for options in ([], ["-P"])
            for pattern, searched, content in (
                ("call(", r"call\(", "src/code:3:call("),
                ("Beta|call(", r"Beta|call\(", "src/code:2:Beta\nsrc/code:3:call("),
                ("x)|Alpha", r"x\)|Alpha", "src/code:1:Alpha"),
            )
        ],
        # PCRE2 already reads a leading brace literally; the default engine rejects it.
        ([], "{Alpha|Beta", r"\{Alpha|Beta", "src/code:2:Beta"),
        ([], "Beta{2", r"Beta\{2", ""),
    ],
)
async def test_unbalanced_regex_characters_match_literally_with_a_note(
    tmp_path, options, pattern, searched, content
):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/code").write_text("Alpha\nBeta\ncall(\n")

    result = await dispatch(tmp_path, {"args": [*options, pattern, "src"]})

    assert result["data"]["content"] == content
    assert f'searched "{pattern}" as "{searched}"' in result["data"]["note"]
    assert "-F" in result["data"]["note"]


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
    assert result["data"]["content"] == "a.txt:1:old"
    assert result["data"]["note"] == (
        f"--replace was ignored: results show the original lines{advice}."
    )


@pytest.mark.asyncio
async def test_look_around_runs_with_pcre2_and_says_so(tmp_path):
    (tmp_path / "code.py").write_text("price = 1\nprice_total = 2\n")

    result = await dispatch(tmp_path, {"args": ["price(?!_)"]})

    assert result["data"]["content"] == "code.py:1:price = 1"
    assert "PCRE2" in result["data"]["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        (r"Alpha|16 \·", "code:1:Alpha\ncode:2:16 · cases"),
        (r"[\·—]", "code:2:16 · cases\ncode:3:long—dash\ncode:4:call(·"),
        (r"long\—dash|Beta", "code:3:long—dash"),
        (r"Alpha|call(\·", "code:1:Alpha\ncode:4:call(·"),
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

    assert result["data"]["content"] == "code:1:16 \\·"
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
    assert result["data"]["content"] == "src/code.py:1:def add_recipe():"


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
            {"args": ["--files", "-e", "needle"]},
            "invalid_arguments",
            "--files lists files by name and does not search their contents, so it cannot be "
            'combined with -e. To select names, pass glob (such as "*.py"); to search '
            "contents, remove --files.",
        ),
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
        ({"args": ["needle", "--offset=-1"]}, "invalid_arguments", "between 0 and 1000000"),
        ({"args": ["needle", "--offset=1.5"]}, "invalid_arguments", "between 0 and 1000000"),
        ({"args": ["needle", "--offset=1000001"]}, "invalid_arguments", "between 0 and 1000000"),
        ({"args": ["needle", "--offset"]}, "invalid_arguments", "--offset in args needs a value"),
        ({"args": ["needle", "--limit=0"]}, "invalid_arguments", "between 1 and 10000"),
        ({"args": ["needle", "--limit=10001"]}, "invalid_arguments", "between 1 and 10000"),
        # Rejected before the search runs.
        ({"args": ["--files"], "limit": 0}, None, '"limit" must be at least 1'),
        ({"args": ["--files"], "offset": -1}, None, '"offset" must be at least 0'),
        ({"args": ["needle"], "fuzzy": True}, None, '"fuzzy" is not a parameter'),
        ({"args": ["needle"], "argv": ["other"]}, None, "Conflicting values for args"),
        ({"args": r'["-e", "\bcall\("]'}, None, _LIST),
        ({"args": '["needle",]'}, None, _LIST),
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
    assert sensitive["data"]["content"] == ""
    assert sensitive["data"]["summary"] == "No directories found."
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
    ],
)
async def test_paging_flags_select_same_exact_results_as_fields(tmp_path, arguments):
    (tmp_path / "a.py").write_text("needle first\nneedle second\nneedle third\n")
    before = copy.deepcopy(arguments)
    result = await dispatch(tmp_path, arguments)
    assert arguments == before
    assert result["ok"], result
    assert result["data"]["content"] == "a.py:2:needle second"
    assert result["data"]["next_offset"] == 2


@pytest.mark.asyncio
async def test_paging_option_spellings_remain_literal_payloads(tmp_path):
    (tmp_path / "--limit").write_text("--offset\n")
    result = await dispatch(tmp_path, {"args": ["--", "--offset", "--limit"]})
    assert result["ok"], result
    assert result["data"]["content"] == "--limit:1:--offset"


@pytest.mark.asyncio
async def test_empty_result_reports_actual_cwd_and_literal_quotes(tmp_path):
    workspace = tmp_path / "workspace"
    project = tmp_path / "project"
    workspace.mkdir()
    project.mkdir()
    (project / "events.ts").write_text("type Event = 'item:generated';\n")
    result = await dispatch(workspace, {"args": ['"item:generated"']}, cwd=project)
    assert result["data"]["searched_paths"] == [project.as_posix()]
    assert result["data"]["patterns"] == ['"item:generated"']
    assert result["data"]["summary"] == "No matches in 1 searched file."
    found = await dispatch(workspace, {"args": ["item:generated"]}, cwd=project)
    assert found["data"]["content"] == "events.ts:1:type Event = 'item:generated';"


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
        "src/a.py-1-x = 1\nsrc/a.py:2:def load_config():\nsrc/a.py-3-    connect(x)\n"
        "src/a.py-4-# TODO\n--\ntests/b.py:1:def load_config():",
        "src/a.py:1",
        "src/a.py:3:    connect(x)",
        "errors.log",
        "src/db/migrations/",
    ]
