"""Search boundaries: ignores, exact coordinates, bounded paging and child cleanup."""

import contextlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from core.tools._search_execution import native_lines
from core.tools._search_options import parse_options
from core.tools._search_selection import Glob
from core.tools.search import SearchBudget
from core.tools.search_files import search_files_handler
from tests.core.tools.test_search_files import context, search


def test_worktree_bounds_parent_ignores(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("work/\n*.py\n")
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").write_text("gitdir: ../.git/worktrees/work")
    (work / "a.py").write_text("needle")
    assert (
        search(tmp_path, action="content", patterns=["needle"], paths=["work"])["content"]
        == "work/a.py:1:needle"
    )


def test_global_and_repository_excludes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "exclude").write_text("global.py\n")
    (home / ".gitconfig").write_text(
        '[core]\n excludesFile = "' + (home / "exclude").as_posix() + '"\n'
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    root = tmp_path / "repo"
    (root / ".git/info").mkdir(parents=True)
    (root / ".git/info/exclude").write_text("local.py\n")
    for name in ("global.py", "local.py", "keep.py"):
        (root / name).write_text("needle")
    assert search(root, action="content", patterns=["needle"])["content"] == "keep.py:1:needle"
    data = search(
        root,
        action="content",
        patterns=["needle"],
        options=["--no-ignore-global", "--no-ignore-exclude"],
    )
    assert len(data["content"].splitlines()) == 3


def test_extra_ignore_case_and_explicit_ignored_file(tmp_path):
    (tmp_path / "rules").write_text("HIDDEN.PY\n")
    (tmp_path / "hidden.py").write_text("needle")
    args = {
        "action": "content",
        "patterns": ["needle"],
        "options": ["--ignore-file", "rules", "--ignore-file-case-insensitive"],
    }
    assert search(tmp_path, **args)["content"] == "No results."
    assert search(tmp_path, **args, paths=["hidden.py"])["content"] == "hidden.py:1:needle"


def test_crlf_encoding_null_records_and_unicode(tmp_path):
    (tmp_path / "crlf").write_bytes(b"needle\r\n")
    assert (
        search(
            tmp_path,
            action="content",
            patterns=["needle"],
            paths=["crlf"],
            options=["-x", "--crlf"],
        )["content"]
        == "crlf:1:needle"
    )
    (tmp_path / "latin").write_bytes(b"caf\xe9\n")
    assert (
        "café"
        in search(
            tmp_path, action="content", patterns=["café"], paths=["latin"], options=["-Elatin1"]
        )["content"]
    )
    (tmp_path / "null").write_bytes(b"first\0needle\0")
    assert (
        search(
            tmp_path,
            action="content",
            patterns=["needle"],
            paths=["null"],
            options=["--null-data", "-o"],
        )["content"]
        == "null:2:1:needle"
    )


def test_byte_pagination_makes_progress(tmp_path):
    (tmp_path / "a").write_text("".join(f"needle {i} " + "x" * 1000 + "\n" for i in range(150)))
    offset = 0
    seen = []
    while True:
        page = search(tmp_path, action="content", patterns=["needle"], limit=1000, offset=offset)
        seen.extend(
            line.split(":", 2)[1] for line in page["content"].splitlines() if line.startswith("a:")
        )
        assert len(page["content"].encode()) <= 51200
        if "next_offset" not in page:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert seen == list(map(str, range(1, 151)))


def test_large_context_cannot_hide_match_or_stall_paging(tmp_path):
    (tmp_path / "a").write_text(("context " + "x" * 3000 + "\n") * 100 + "NEEDLE\n")
    data = search(tmp_path, action="content", patterns=["NEEDLE"], options=["-B100"])
    assert "a:101:NEEDLE" in data["content"]
    assert data.get("next_offset") != 0


def test_native_context_precedence():
    options = parse_options(["-A1", "-C3", "-B2", "-C4"], action="content", kind="files")
    assert options.context == (2, 1)


@pytest.mark.parametrize(
    "pattern,path,matched",
    [
        ("**/*.{py,js}", "src/file.py", True),
        ("**/{src,{lib,test}}/*.py", "lib/file.py", True),
        ("**/[{}].py", "src/{.py", True),
        ("**/[]].py", "src/].py", True),
        ("*.py", "src/file.py", False),
        ("**/*", "~lock", True),
        ("**/*", "..data", True),
    ],
)
def test_glob_grammar(pattern, path, matched):
    assert Glob(pattern).matches(path, False) is matched


def test_follow_preserves_spelling_and_stops_loops(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "real/a").write_text("needle")
    try:
        (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
        (tmp_path / "real/loop").symlink_to(tmp_path / "real", target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit creating symbolic links")
    data = search(tmp_path, action="content", patterns=["needle"], paths=["link"], options=["-L"])
    assert data["content"] == "link/a:1:needle"
    assert data["complete"] is False
    assert any("loop" in warning for warning in data["warnings"])


def test_cancellation_kills_a_silent_child(tmp_path, monkeypatch):
    original = subprocess.Popen
    children = []
    hooks = []

    def launch(_command, **kwargs):
        child = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr("core.tools._search_execution.subprocess.Popen", launch)
    cancelled = threading.Event()
    ctx = context(
        tmp_path, cancel_registration_hook=hooks.append, cancel_check_hook=cancelled.is_set
    )
    timer = threading.Timer(0.2, cancelled.set)
    timer.start()
    try:
        started = time.monotonic()
        with contextlib.closing(
            native_lines(Path(sys.executable), [], ctx, SearchBudget(ctx))
        ) as lines:
            assert list(lines) == []
        assert time.monotonic() - started < 3
        assert len(hooks) == 1
        assert children[0].poll() is not None
    finally:
        timer.cancel()


@pytest.mark.parametrize("pattern", ["true", "false"])
def test_boolean_words_are_search_text_not_flag_values(tmp_path, pattern):
    (tmp_path / "a").write_text("true false\n")
    result = search_files_handler(context(tmp_path), {"args": ["-n", pattern]})
    assert result["data"]["content"] == "a:1:true false"


def test_display_preserves_search_arguments():
    from core.tools.search_files import register_search_files_tool
    from core.tools.tools import ToolRegistry

    registry = ToolRegistry()
    register_search_files_tool(registry)
    display = registry.get("search_files").display.to_payload(
        {"args": ["-e", "alpha", "-e", "beta", "src", "tests"]}
    )
    assert [part["value"] for part in display["primary"]] == ["-e alpha -e beta src tests"]


def test_nested_repository_uses_its_own_git_ignores(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("a.py\n")
    nested = tmp_path / "nested"
    (nested / ".git/info").mkdir(parents=True)
    (nested / ".git/info/exclude").write_text("b.py\n")
    for path in (tmp_path / "a.py", nested / "a.py", nested / "b.py"):
        path.write_text("needle\n")
    assert (
        search(tmp_path, action="content", patterns=["needle"])["content"] == "nested/a.py:1:needle"
    )


def test_explicit_nested_repository_keeps_its_internal_ignores(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".gitignore").write_text("nested/\n")
    nested = tmp_path / "nested"
    (nested / ".git").mkdir(parents=True)
    (nested / ".gitignore").write_text("ignored.py\n")
    (nested / "ignored.py").write_text("needle\n")
    (nested / "keep.py").write_text("needle\n")
    result = search(tmp_path, action="content", paths=["nested"], patterns=["needle"])
    assert result["content"] == "nested/keep.py:1:needle"


def test_missing_engine_hides_tool_and_dispatch_explains_repair(tmp_path, monkeypatch):
    import asyncio

    from core.tools.search_files import register_search_files_tool
    from core.tools.tools import ToolRegistry

    def unavailable():
        raise ValueError("Repair the vBot installation; run python -m cli.search_runtime.")

    monkeypatch.setattr("core.tools.search_files.require_binary", unavailable)
    registry = ToolRegistry()
    register_search_files_tool(registry)
    assert registry.provider_definitions(["search_files"]) == []
    result = asyncio.run(registry.dispatch(context(tmp_path), {"args": ["--files"]}))
    assert result["error"]["code"] == "tool_not_ready"
    assert "cli.search_runtime" in result["error"]["message"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX literal filename grammar")
def test_unusual_literal_names_roundtrip_in_file_modes(tmp_path):
    for name in ('"quoted"', "literal\\name", "with\nnewline"):
        (tmp_path / name).write_text("needle\n")
        data = search(tmp_path, action="content", patterns=["needle"], paths=[name], options=["-c"])
        assert data["complete"]
        assert data["content"].endswith(":1")
