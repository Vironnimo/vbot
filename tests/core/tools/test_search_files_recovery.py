"""First-use argv searches preserve literal payloads, targets, and constraints."""

import copy
import json

import pytest

from core.tools._search_arguments import parse_search_args
from core.tools.search_files import (
    normalize_search_arguments,
    register_search_files_tool,
    search_files_handler,
)
from core.tools.tools import ToolRegistry
from tests.core.tools.test_search_files import context


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [[], ["--files"], ["-q"]])
async def test_missing_explicit_roots_preserve_results_and_report_incomplete_scope(tmp_path, mode):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/a.py").write_text("needle\n")
    (tmp_path / "outside.py").write_text("needle\n")
    registry = ToolRegistry()
    register_search_files_tool(registry)
    patterns = [] if "--files" in mode else ["needle"]
    args = [*mode, *patterns, "missing", "src"]
    result = await registry.dispatch(context(tmp_path), {"args": args})
    assert result["ok"]
    data = result["data"]
    assert data["complete"] is False and data["warnings"]
    assert data["missing_paths"] == [(tmp_path / "missing").as_posix()]
    assert data["searched_paths"] == [(tmp_path / "src").as_posix()]
    if "-q" in mode:
        assert data["matched"] is True
        (tmp_path / "src/a.py").write_text("unrelated\n")
        result = await registry.dispatch(context(tmp_path), {"args": args})
        assert result["data"]["matched"] is None
        assert result["data"]["complete"] is False
    else:
        assert data["content"] == ("src/a.py" if mode else "src/a.py:1:needle")


@pytest.mark.asyncio
async def test_missing_path_is_not_reinterpreted_as_pattern(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/code").write_text("Alpha\nBeta\ncall(\n")
    registry = ToolRegistry()
    register_search_files_tool(registry)
    ctx = context(tmp_path)
    partial = await registry.dispatch(ctx, {"args": ["Alpha", "Beta", "src"]})
    assert partial["data"]["content"] == "src/code:1:Alpha"
    assert partial["data"]["complete"] is False
    assert any('pattern "Alpha|Beta"' in warning for warning in partial["data"]["warnings"])
    missing = await registry.dispatch(ctx, {"args": ["Alpha", "Beta"]})
    assert missing["error"]["code"] == "path_not_found"
    literal_hint = await registry.dispatch(ctx, {"args": ["-F", "Alpha", "Beta"]})
    assert '["-F", "-e", "Alpha", "-e", "Beta"]' in literal_hint["error"]["message"]
    for corrected_call in (
        {"args": ["-e", "Alpha", "-e", "Beta", "src"]},
        {"pattern": "Alpha|Beta", "path": "src"},
    ):
        corrected = await registry.dispatch(ctx, corrected_call)
        assert corrected["data"]["content"] == "src/code:1:Alpha\nsrc/code:2:Beta"
        assert corrected["data"]["complete"] is True
    literal = await registry.dispatch(ctx, {"args": ["-F", "call(", "src"]})
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
    ],
)
async def test_unbalanced_regex_characters_match_literally_with_a_note(
    tmp_path, options, pattern, searched, content
):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/code").write_text("Alpha\nBeta\ncall(\n")
    registry = ToolRegistry()
    register_search_files_tool(registry)

    result = await registry.dispatch(context(tmp_path), {"args": [*options, pattern, "src"]})

    assert result["data"]["content"] == content
    assert f'searched "{pattern}" as "{searched}"' in result["data"]["note"]
    assert "-F" in result["data"]["note"]


@pytest.mark.asyncio
async def test_look_around_runs_with_pcre2_and_says_so(tmp_path):
    (tmp_path / "code.py").write_text("price = 1\nprice_total = 2\n")
    registry = ToolRegistry()
    register_search_files_tool(registry)

    result = await registry.dispatch(context(tmp_path), {"args": ["price(?!_)"]})

    assert result["data"]["content"] == "code.py:1:price = 1"
    assert "PCRE2" in result["data"]["note"]


@pytest.mark.parametrize(
    "tokens,patterns,roots,options,kind",
    [
        (["-n", "needle", "src"], ["needle"], ["src"], ["-n"], "files"),
        (["needle", "src", "-iw"], ["needle"], ["src"], ["-i", "-w"], "files"),
        (["-g", "-F", "needle"], ["needle"], [], ["-g", "-F"], "files"),
        (["-e", "needle", "-eother", "src"], ["needle", "other"], ["src"], [], "files"),
        (["src", "--regexp=needle"], ["needle"], ["src"], [], "files"),
        (["--", "-needle", "-root"], ["-needle"], ["-root"], [], "files"),
        (["--files", "-g", "*.py", "src"], [], ["src"], ["-g", "*.py"], "files"),
        (["--dirs", "src"], [], ["src"], [], "directories"),
        (["--entries"], [], [], [], "all"),
        (["-F", "a b", r"C:\source files"], ["a b"], [r"C:\source files"], ["-F"], "files"),
        (["rg"], ["rg"], [], [], "files"),
        (["-g", "--help", "needle"], ["needle"], [], ["-g", "--help"], "files"),
        (["-e", "--files"], ["--files"], [], [], "files"),
        (["-e", "--offset"], ["--offset"], [], [], "files"),
        (["-g", "--offset", "needle"], ["needle"], [], ["-g", "--offset"], "files"),
        (["--", "--offset", "--limit"], ["--offset"], ["--limit"], [], "files"),
        (["-e", ""], [""], [], [], "files"),
    ],
)
def test_argv_boundaries_have_independently_specified_meaning(
    tokens, patterns, roots, options, kind
):
    parsed = parse_search_args(tokens)
    assert parsed["patterns"] == patterns
    assert parsed["paths"] == roots
    assert parsed["options"] == options
    assert parsed["kind"] == kind


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
    registry = ToolRegistry()
    register_search_files_tool(registry)
    before = copy.deepcopy(supplied)
    result = await registry.dispatch(context(tmp_path), supplied)
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
    registry = ToolRegistry()
    register_search_files_tool(registry)
    arguments = {"args": ["-F", "-q", "-U", "-e", pattern]}
    result = await registry.dispatch(context(tmp_path), arguments)
    assert result["ok"]
    assert result["data"]["matched"] is True
    assert normalize_search_arguments(arguments)["args"][-1] == pattern


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"args": ["--files", "--dirs"]},
        {"args": ["--files", "-e", "needle"]},
        {"args": ["--files", "-c"]},
        {"args": ["--files", "-g"]},
        {"args": ["--regexp"]},
        {"args": ["-F=true", "needle"]},
        {"args": ["--pre", "program", "needle"]},
        {"args": ["-F", "needle", ""]},
        {"args": ["needle", "-"]},
        {"args": ["needle"], "fuzzy": True},
        {"args": ["needle"], "argv": ["other"]},
        {"args": r'["-e", "\bcall\("]'},
        {"args": '["needle",]'},
    ],
)
async def test_ambiguous_or_unsupported_requests_reject_without_substitution(tmp_path, arguments):
    (tmp_path / "code").write_text("needle\n")
    registry = ToolRegistry()
    register_search_files_tool(registry)
    before = copy.deepcopy(arguments)
    try:
        result = await registry.dispatch(context(tmp_path), arguments)
    except ValueError:
        pass
    else:
        assert not result["ok"]
        assert result["data"] is None
    assert arguments == before


@pytest.mark.asyncio
async def test_path_discovery_case_scope_and_pagination(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested/EMPTY").mkdir()
    (tmp_path / "nested/a.PY").write_text("irrelevant\n")
    (tmp_path / "b.py").write_text("irrelevant\n")
    registry = ToolRegistry()
    register_search_files_tool(registry)
    ctx = context(tmp_path)
    dirs = await registry.dispatch(ctx, {"args": ["--dirs", "-i", "-g", "empty"]})
    assert dirs["data"]["content"] == "nested/EMPTY/"
    sensitive = await registry.dispatch(ctx, {"args": ["--dirs", "-s", "-g", "empty"]})
    assert sensitive["data"]["content"] == "No results."
    files = {"args": ["--files", "-i", "-g", "*.py", "--sort=path"], "limit": 1}
    first = await registry.dispatch(ctx, files)
    assert first["data"]["content"] == "b.py"
    assert first["data"]["complete"] is False
    second = await registry.dispatch(ctx, {**files, "offset": first["data"]["next_offset"]})
    assert second["data"]["content"] == "nested/a.PY"
    assert "next_offset" not in second["data"]
    assert second["data"]["complete"] is True


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
    registry = ToolRegistry()
    register_search_files_tool(registry)
    before = copy.deepcopy(arguments)
    result = await registry.dispatch(context(tmp_path), arguments)
    assert arguments == before
    assert result["ok"], result
    assert result["data"]["content"] == "a.py:2:needle second"
    assert result["data"]["next_offset"] == 2
    assert result["data"]["complete"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"args": ["needle", "--offset=1"], "offset": 2},
        {"args": ["needle", "--offset=1", "--offset=2"]},
        {"args": ["needle", "--limit=1"], "limit": 2},
        {"args": ["needle", "--offset=-1"]},
        {"args": ["needle", "--offset=1.5"]},
        {"args": ["needle", "--offset=1000001"]},
        {"args": ["needle", "--offset"]},
        {"args": ["needle", "--limit=0"]},
        {"args": ["needle", "--limit=10001"]},
        {"args": ["needle", "-q", "--offset=0"]},
    ],
)
async def test_paging_never_discards_conflicting_or_invalid_constraints(tmp_path, arguments):
    (tmp_path / "a.py").write_text("needle first\nneedle second\n")
    registry = ToolRegistry()
    register_search_files_tool(registry)
    result = await registry.dispatch(context(tmp_path), arguments)
    assert not result["ok"]
    assert result["data"] is None


@pytest.mark.asyncio
async def test_paging_option_spellings_remain_literal_payloads(tmp_path):
    (tmp_path / "--limit").write_text("--offset\n")
    registry = ToolRegistry()
    register_search_files_tool(registry)
    result = await registry.dispatch(context(tmp_path), {"args": ["--", "--offset", "--limit"]})
    assert result["ok"], result
    assert result["data"]["content"] == "--limit:1:--offset"


def test_empty_result_reports_actual_cwd_and_literal_quotes(tmp_path):
    workspace = tmp_path / "workspace"
    project = tmp_path / "project"
    workspace.mkdir()
    project.mkdir()
    (project / "events.ts").write_text("type Event = 'item:generated';\n")
    ctx = context(workspace, cwd=project)
    result = search_files_handler(ctx, {"args": ['"item:generated"']})
    assert result["data"]["searched_paths"] == [project.as_posix()]
    assert result["data"]["patterns"] == ['"item:generated"']
    found = search_files_handler(ctx, {"args": ["item:generated"]})
    assert found["data"]["content"] == "events.ts:1:type Event = 'item:generated';"


def test_help_examples_execute_the_advertised_searches(tmp_path):
    (tmp_path / "src/migrations").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/a.py").write_text("run\nrunner\n")
    (tmp_path / "tests/b.py").write_text("run\n")
    (tmp_path / "errors.log").write_text("ERROR\n")
    ctx = context(tmp_path)
    help_result = search_files_handler(ctx, {"args": ["--help"]})
    calls = [
        json.loads(line)
        for line in help_result["data"]["content"].splitlines()
        if line.startswith("{")
    ]
    results = [search_files_handler(ctx, call) for call in calls]
    assert all(result["ok"] for result in results)
    assert [result["data"]["content"] for result in results] == [
        "src/a.py:1:run\nsrc/a.py:2-runner\ntests/b.py:1:run",
        "src/migrations/",
        "errors.log",
    ]
