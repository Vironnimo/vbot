"""File-search contracts exercised against the pinned engine and real files."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from core.tools.contracts import ToolContractError
from core.tools.search_files import register_search_files_tool, search_files_handler
from core.tools.tools import ToolContext, ToolRegistry


def context(root: Path, **kwargs) -> ToolContext:
    return ToolContext(
        agent_id="a",
        session_id="s",
        run_id="r",
        tool_call_id="c",
        tool_name="search_files",
        tool_call_index=0,
        workspace=root,
        vbot_root=root,
        data_root=root,
        **kwargs,
    )


def search(root: Path, **arguments):
    # Fixture notation keeps the engine cases readable; each runs the public argv Tool.
    action = arguments.pop("action", "content")
    kind = arguments.pop("kind", "all")
    patterns = arguments.pop("patterns", [])
    options = arguments.pop("options", [])
    paths = arguments.pop("paths", [])
    tokens = list(options)
    if "--type-list" not in tokens:
        if action == "paths":
            tokens.append({"all": "--entries", "files": "--files", "directories": "--dirs"}[kind])
            for pattern in patterns:
                tokens.extend(["-g", "./" + pattern])
        else:
            for pattern in patterns:
                tokens.extend(["-e", pattern])
    if paths:
        tokens.extend(["--", *paths])
    result = search_files_handler(context(root), {"args": tokens, **arguments})
    assert result["ok"], result
    return result["data"]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "empty").mkdir()
    (tmp_path / "src" / "a.py").write_text(
        "before\nrun run\nafter\nRUN\nrunner\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "b.PY").write_text("run\n", encoding="utf-8", newline="\n")
    (tmp_path / "plain.txt").write_text("nothing\n", encoding="utf-8", newline="\n")
    return tmp_path


@pytest.mark.parametrize(
    "options, expected",
    [
        ([], "src/a.py:2:run run\nsrc/a.py:5:runner\ntests/b.PY:1:run"),
        (["-w"], "src/a.py:2:run run\ntests/b.PY:1:run"),
        (["-iw"], "src/a.py:2:run run\nsrc/a.py:4:RUN\ntests/b.PY:1:run"),
        (["-i", "-s", "-w"], "src/a.py:2:run run\ntests/b.PY:1:run"),
        (["-x"], "tests/b.PY:1:run"),
        (["-l"], "src/a.py\ntests/b.PY"),
        (["--files-without-match"], "plain.txt"),
        (["-c"], "src/a.py:2\ntests/b.PY:1"),
        (["--count-matches"], "src/a.py:3\ntests/b.PY:1"),
        (["-c", "--include-zero"], "plain.txt:0\nsrc/a.py:2\ntests/b.PY:1"),
        (["-o", "-w"], "src/a.py:2:1:run\nsrc/a.py:2:5:run\ntests/b.PY:1:1:run"),
    ],
)
def test_content_modes(tree: Path, options: list[str], expected: str) -> None:
    data = search(tree, action="content", patterns=["run"], options=options)
    assert data["content"] == expected
    assert data["complete"] is True


def test_multiple_roots_filters_and_overlap(tree: Path) -> None:
    data = search(
        tree,
        action="content",
        patterns=["run", "RUN"],
        paths=["src", "tests", "src/a.py"],
        options=["-g", "*.py", "-g", "!b.PY", "-w"],
    )
    assert data["content"] == "src/a.py:2:run run\nsrc/a.py:4:RUN"


@pytest.mark.parametrize(
    "patterns,kind,expected",
    [
        (["*.py"], "files", "No results."),
        (["**/*.{py,js}"], "files", "src/a.py\ntests/b.PY"),
        (["**/empty"], "directories", "src/empty/"),
        (None, "all", "plain.txt\nsrc/\nsrc/a.py\nsrc/empty/\ntests/\ntests/b.PY"),
    ],
)
def test_paths(tree: Path, patterns, kind: str, expected: str) -> None:
    args = {} if patterns is None else {"patterns": patterns}
    assert (
        search(tree, action="paths", kind=kind, options=["--sort=path"], **args)["content"]
        == expected
    )


def test_literal_patterns_and_pcre(tree: Path) -> None:
    assert (
        "No results"
        in search(tree, action="content", patterns=["run|RUN"], options=["-F"])["content"]
    )
    assert (
        search(tree, action="content", patterns=[r"run(?= run)"], options=["-P", "-o"])["content"]
        == "src/a.py:2:1:run"
    )
    assert (
        search(tree, action="content", patterns=[r"run(?= run)"], options=["--engine=auto", "-o"])[
            "content"
        ]
        == "src/a.py:2:1:run"
    )


def test_context_and_paging(tree: Path) -> None:
    args = {"action": "content", "patterns": ["run"], "options": ["-C1", "-w"], "limit": 1}
    first = search(tree, **args)
    assert first["content"] == "src/a.py:1-before\nsrc/a.py:2:run run\nsrc/a.py:3-after"
    assert first["next_offset"] == 1
    assert first["complete"] is False
    second = search(tree, **args, offset=first["next_offset"])
    assert second["content"] == "tests/b.PY:1:run"
    assert "next_offset" not in second
    assert second["complete"] is True
    beyond = search(tree, **args, offset=50)
    assert "offset 50" in beyond["content"]


def test_page_boundary_context_stops_before_the_next_pages_match(tmp_path: Path) -> None:
    (tmp_path / "g.txt").write_text("m1\nc2\nm3\nc4\nc5\nc6\nc7\n")
    args = {"action": "content", "patterns": ["^m"], "options": ["-A", "4"], "limit": 1}

    first = search(tmp_path, **args)
    second = search(tmp_path, **args, offset=first["next_offset"])

    assert first["content"] == "g.txt:1:m1\ng.txt:2-c2"
    assert first["next_offset"] == 1
    assert second["content"] == "g.txt:3:m3\ng.txt:4-c4\ng.txt:5-c5\ng.txt:6-c6\ng.txt:7-c7"


def test_context_overlap_and_line_coordinates(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("before\nrun\nrun\nafter\n")
    data = search(tmp_path, action="content", patterns=["run"], options=["-C", "1"])
    assert data["content"] == "a:1-before\na:2:run\na:3:run\na:4-after"


def test_multiline_count_semantics(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("first\nsecond\nfirst\nsecond\n", newline="\n")
    for mode in ("-c", "--count-matches"):
        assert (
            search(tmp_path, action="content", patterns=["first\\nsecond"], options=["-U", mode])[
                "content"
            ]
            == "a:2"
        )
    assert (
        "second"
        in search(
            tmp_path,
            action="content",
            patterns=["first.*second"],
            options=["-U", "--multiline-dotall"],
        )["content"]
    )


@pytest.mark.parametrize("action", ["content", "paths"])
def test_ignores_are_shared_and_positive_filters_never_reinclude(
    tmp_path: Path, action: str
) -> None:
    (tmp_path / ".gitignore").write_text("ignored/\n*.log\n")
    (tmp_path / "ignored").mkdir()
    (tmp_path / "ignored" / "a.py").write_text("needle")
    (tmp_path / "a.py").write_text("needle")
    (tmp_path / "a.log").write_text("needle")
    args = {
        "action": action,
        "patterns": ["needle"] if action == "content" else ["**/*"],
        "options": ["-g", "*.py"],
    }
    data = search(tmp_path, **args)
    assert "ignored/a.py" not in data["content"]
    assert "a.py" in data["content"]
    assert "ignored/a.py" in search(tmp_path, **{**args, "paths": ["ignored"]})["content"]
    assert (
        "ignored/a.py" in search(tmp_path, **{**args, "options": ["-u", "-g", "*.py"]})["content"]
    )
    assert (
        "ignored/a.py"
        not in search(tmp_path, **{**args, "options": ["-u", "--ignore", "-g", "*.py"]})["content"]
    )


def test_ignore_source_precedence_and_negation(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("*.py\n")
    (tmp_path / ".ignore").write_text("!a.py\n")
    (tmp_path / ".rgignore").write_text("a.py\n")
    (tmp_path / "a.py").write_text("needle")
    (tmp_path / "b.py").write_text("needle")
    assert search(tmp_path, action="content", patterns=["needle"])["content"] == "No results."
    (tmp_path / ".rgignore").unlink()
    assert search(tmp_path, action="content", patterns=["needle"])["content"] == "a.py:1:needle"
    assert (
        "b.py"
        in search(tmp_path, action="content", patterns=["needle"], options=["--no-ignore-vcs"])[
            "content"
        ]
    )


def test_hidden_git_and_depth(tree: Path) -> None:
    (tree / ".secret").write_text("run")
    (tree / ".git").write_text("run")
    assert ".secret" in search(tree, action="paths")["content"]
    assert ".secret" not in search(tree, action="paths", options=["--no-hidden"])["content"]
    assert ".git" not in search(tree, action="paths", options=["-uuu"])["content"]
    assert "src/a.py" not in search(tree, action="paths", options=["--max-depth=1"])["content"]
    assert (
        "No results"
        in search(tree, action="content", patterns=["run"], paths=[".git"], options=["-u"])[
            "content"
        ]
    )


def test_types_and_size(tree: Path) -> None:
    assert (
        "plain.txt" not in search(tree, action="paths", kind="files", options=["-tpy"])["content"]
    )
    assert (
        search(tree, action="paths", kind="files", options=["--type-add", "tiny:*.txt", "-ttiny"])[
            "content"
        ]
        == "plain.txt"
    )
    assert (
        search(tree, action="paths", kind="files", options=["--max-filesize", "5"])["content"]
        == "tests/b.PY"
    )
    assert "py:" in search(tree, action="paths", options=["--type-list"])["content"]


def test_long_line_retains_actual_hit(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("x" * 100000 + "NEEDLE" + "y" * 100000)
    data = search(tmp_path, action="content", patterns=["NEEDLE"])
    assert "NEEDLE" in data["content"]
    assert "source bytes omitted" in data["content"]
    assert len(data["content"].encode()) < 50000


def test_binary_encodings_and_existence(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"run\x00tail\n")
    (tmp_path / "b").write_bytes("run\n".encode("utf-16"))
    assert search(tmp_path, action="content", patterns=["run"])["content"] == "b:1:run"
    assert "a:1:" in search(tmp_path, action="content", patterns=["run"], options=["-a"])["content"]
    assert search(tmp_path, action="content", patterns=["run"], options=["-q"])["matched"] is True
    assert (
        search(tmp_path, action="content", patterns=["missing"], options=["-q"])["matched"] is False
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"args": []},
        {"args": ["--files", "missing"]},
        {"args": ["--dirs", "-tpy"]},
        {"args": ["--files", "--dirs"]},
        {"args": ["--dirs", "-e", "run"]},
        {"args": ["run", "-C", "2", "-l"]},
        {"args": ["--files", "--pre", "anything"]},
        {"args": ["--help", "missing"]},
        {"args": ["--files"], "limit": 0},
        {"args": ["--files"], "offset": -1},
    ],
)
def test_invalid_calls_preserve_constraints(tree: Path, arguments) -> None:
    result = search_files_handler(context(tree), arguments)
    assert result["ok"] is False
    assert result["data"] is None


def test_unknown_argument_is_rejected_before_the_search_runs(tree: Path) -> None:
    registry = ToolRegistry()
    register_search_files_tool(registry)

    with pytest.raises(ToolContractError, match='"unknown_feature" is not a parameter'):
        asyncio.run(registry.dispatch(context(tree), {"args": ["run"], "unknown_feature": True}))


@pytest.mark.parametrize("candidates", [False, True])
@pytest.mark.parametrize("args", [["["], ["-P", "(?<"]])
def test_native_validation_with_and_without_candidates(
    tmp_path: Path, args: list[str], candidates: bool
) -> None:
    if candidates:
        (tmp_path / "a.txt").write_text("[(?<")
    result = search_files_handler(context(tmp_path), {"args": args})
    assert result["ok"] is False
    message = result["error"]["message"]
    assert "regex parse error" in message or "PCRE2: error compiling pattern" in message
    assert "If you meant literal text, add -F to args." in message


def test_timeout_and_user_cancel(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import core.tools.search as shared

    result = search_files_handler(context(tree, cancel_check_hook=lambda: True), {"args": ["run"]})
    assert result["error"]["code"] == "cancelled_by_user"
    monkeypatch.setattr(shared, "SEARCH_TIMEOUT_SECONDS", -1)
    assert search(tree, action="paths")["complete"] is False


def test_schema_and_registry_repairs(tree: Path) -> None:
    registry = ToolRegistry()
    register_search_files_tool(registry)
    definition = registry.provider_definitions(["search_files"])[0]
    assert len(definition["parameters"]["properties"]) == 3
    assert definition["parameters"]["required"] == ["args"]
    assert "additionalProperties" not in definition["parameters"]
    result = asyncio.run(
        registry.dispatch(
            context(tree),
            {"argv": ["-n", "run", "tests"], "limit": "1"},
        )
    )
    assert result["data"]["content"] == "tests/b.PY:1:run"


def test_live_path_pagination_newest_and_ties(tree: Path) -> None:
    os.utime(tree / "plain.txt", (2000000000, 2000000000))
    page = search(tree, action="paths", kind="files", limit=1)
    assert page["content"] == "plain.txt"
    assert page["next_offset"] == 1
    assert "plain.txt" not in search(tree, action="paths", kind="files", offset=1)["content"]
