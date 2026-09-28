"""The search_files Tool: modes, scope, ignores, links, encodings, paging, and selection."""

from __future__ import annotations

import asyncio
import errno
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.tools import _search_execution, _search_ignores, _search_selection
from core.tools._search_selection import Glob
from core.utils.search_binary import require_binary
from tests.core.tools.search_files_test_support import context, dispatch, search, search_registry


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


@pytest.mark.parametrize(
    ("text", "options", "expected"),
    [
        # Overlapping context merges, and every line keeps its own coordinate.
        ("before\nrun\nrun\nafter\n", ["-C", "1"], "a:1-before\na:2:run\na:3:run\na:4-after"),
        # -A and -B override -C, whatever their order.
        (
            "l1\nl2\nl3\nl4\nrun\nl6\nl7\nl8\n",
            ["-A1", "-C3", "-B2", "-C4"],
            "a:3-l3\na:4-l4\na:5:run\na:6-l6",
        ),
    ],
)
def test_context_lines(tmp_path: Path, text: str, options: list[str], expected: str) -> None:
    (tmp_path / "a").write_text(text)
    data = search(tmp_path, action="content", patterns=["run"], options=options)
    assert data["content"] == expected


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


# The engine validates a pattern even when no file could be searched. With candidates
# spanning several native batches, its error surfaces while the candidate list is still
# being read and must not be replaced by a failure to release that list.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "candidates"),
    [(["["], 0), (["-P", "a{2,1}"], 1), (["-P", "a{2,1}"], 100)],
)
async def test_native_validation_with_and_without_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], candidates: int
) -> None:
    # Leaves room for about forty of the paths below per native command line.
    monkeypatch.setattr(
        _search_execution, "MAX_COMMAND_LINE_BYTES", len(str(require_binary())) + 600
    )
    for index in range(candidates):
        (tmp_path / f"c{index:03d}.txt").write_text("[a{2,1}")
    result = await dispatch(tmp_path, {"args": args})
    assert result["ok"] is False
    message = result["error"]["message"]
    assert "regex parse error" in message or "PCRE2: error compiling pattern" in message
    assert "If you meant literal text, add -F to args." in message


@pytest.mark.asyncio
async def test_timeout_and_user_cancel(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import core.tools.search as shared

    cancelled = await dispatch(tree, {"args": ["run"]}, cancel_check_hook=lambda: True)
    assert cancelled["error"]["code"] == "cancelled_by_user"
    monkeypatch.setattr(shared, "SEARCH_TIMEOUT_SECONDS", -1)
    timed_out = await dispatch(tree, {"args": ["--entries"]})
    assert timed_out["data"]["complete"] is False


# Windows reports a locked file in the system language and names the file, here the
# search's own scratch spool; the Agent reads the reason in English, without paths.
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locked", "pattern", "expected"),
    [
        (
            "selection.sqlite",
            "absent",
            "search_files could not use a file the search needs: {reason}.",
        ),
        (
            "selection.sqlite",
            "runner",
            "search_files could not use a file the search needs: {reason}.",
        ),
        (
            ".gitignore",
            "runner",
            "search_files could not read the ignore file .gitignore: {reason}.",
        ),
    ],
)
async def test_system_errors_are_explained_in_english_without_paths(
    tree: Path, monkeypatch: pytest.MonkeyPatch, locked: str, pattern: str, expected: str
) -> None:
    import core.tools.search_files as search_files

    def in_use(path: Path) -> PermissionError:
        message = "Der Prozess kann nicht auf die Datei zugreifen"
        return PermissionError(errno.EACCES, message, str(path), 32)

    class LockedSpool(tempfile.TemporaryDirectory):
        def __exit__(self, *exc_info):
            super().__exit__(*exc_info)
            raise in_use(Path(self.name) / locked)

    stamp = _search_ignores._stamp

    def locked_stamp(path: Path):
        if path.name == locked:
            raise in_use(path)
        return stamp(path)

    (tree / ".gitignore").write_text("*.log\n")
    if locked == ".gitignore":
        monkeypatch.setattr(_search_ignores, "_stamp", locked_stamp)
    else:
        monkeypatch.setattr(
            search_files, "tempfile", SimpleNamespace(TemporaryDirectory=LockedSpool)
        )
    result = await dispatch(tree, {"pattern": pattern})

    reason = "another program is using it" if os.name == "nt" else "permission denied"
    message = f"{expected.format(reason=reason)} Retry the call."
    if pattern == "absent" or locked == ".gitignore":
        assert result["error"] == {"code": "search_error", "message": message}
    else:
        assert result["data"]["content"] == "src/a.py:5:runner"
        assert (result["data"]["complete"], result["data"]["warnings"]) == (False, [message])


def test_schema_display_and_registry_repairs(tree: Path) -> None:
    registry = search_registry()
    definition = registry.provider_definitions(["search_files"])[0]
    assert list(definition["parameters"]["properties"]) == [
        "pattern",
        "path",
        "glob",
        "output",
        "context",
        "args",
        "limit",
        "offset",
    ]
    assert "required" not in definition["parameters"]
    assert "additionalProperties" not in definition["parameters"]
    display = registry.get("search_files").display.to_payload(
        {"args": ["-e", "alpha", "-e", "beta", "src", "tests"]}
    )
    assert [part["value"] for part in display["primary"]] == ["-e alpha -e beta src tests"]
    result = asyncio.run(dispatch(tree, {"argv": ["-n", "run", "tests"], "limit": "1"}))
    assert result["data"]["content"] == "tests/b.PY:1:run"


def test_live_path_pagination_newest_and_ties(tree: Path) -> None:
    os.utime(tree / "plain.txt", (2000000000, 2000000000))
    page = search(tree, action="paths", kind="files", limit=1)
    assert page["content"] == "plain.txt"
    assert page["next_offset"] == 1
    assert "plain.txt" not in search(tree, action="paths", kind="files", offset=1)["content"]


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


def _age(path: Path, seconds: int = 60) -> None:
    moment = time.time() - seconds
    os.utime(path, (moment, moment))


def _found(root: Path) -> set[str]:
    return set(search(root, action="content", patterns=["needle"])["content"].splitlines())


def test_unchanged_ignore_sources_are_compiled_once_across_searches(tmp_path, monkeypatch):
    compiled: list[list[str]] = []
    real = _search_ignores.PathSpec

    class CountingPathSpec:
        @staticmethod
        def from_lines(kind, lines):
            compiled.append(list(lines))
            return real.from_lines(kind, lines)

    monkeypatch.setattr(_search_ignores, "PathSpec", CountingPathSpec)
    (tmp_path / ".gitignore").write_text("a.py\n")
    _age(tmp_path / ".gitignore")
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("needle")
    assert _found(tmp_path) == {"b.py:1:needle"}
    assert ["a.py"] in compiled
    compiled.clear()
    for _ in range(2):
        assert _found(tmp_path) == {"b.py:1:needle"}
    assert compiled == []


def test_ignore_and_git_configuration_edits_apply_to_the_next_search(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    for name in ("a.py", "b.py", "c.py"):
        (root / name).write_text("needle")
    (home / "one").write_text("c.py\n")
    (home / "two").write_text("b.py\n")
    config = home / ".gitconfig"
    config.write_text('[core]\n excludesFile = "' + (home / "one").as_posix() + '"\n')
    ignore = root / ".gitignore"
    ignore.write_text("a.py\n")
    for path in (home / "one", home / "two", config, ignore):
        _age(path)
    assert _found(root) == {"b.py:1:needle"}
    # Same-size rewrites differ only in modification time.
    ignore.write_text("b.py\n")
    _age(ignore, 30)
    assert _found(root) == {"a.py:1:needle"}
    config.write_text('[core]\n excludesFile = "' + (home / "two").as_posix() + '"\n')
    _age(config, 30)
    assert _found(root) == {"a.py:1:needle", "c.py:1:needle"}
    ignore.unlink()
    assert _found(root) == {"a.py:1:needle", "c.py:1:needle"}
    config.unlink()
    assert _found(root) == {"a.py:1:needle", "b.py:1:needle", "c.py:1:needle"}


def test_recently_written_ignore_sources_are_reread_with_an_identical_stamp(tmp_path):
    ignore = tmp_path / ".gitignore"
    ignore.write_text("a.py\n")
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("needle")
    written = ignore.stat()
    assert _found(tmp_path) == {"b.py:1:needle"}
    # A rewrite within one filesystem timestamp tick keeps size and mtime.
    ignore.write_text("b.py\n")
    os.utime(ignore, ns=(written.st_atime_ns, written.st_mtime_ns))
    assert _found(tmp_path) == {"a.py:1:needle"}


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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory junctions")
def test_junctions_are_followed_only_on_request_or_as_explicit_roots(tmp_path):
    import _winapi

    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / "a.txt").write_text("needle")
    (outside / "secret.txt").write_text("needle")
    _winapi.CreateJunction(str(outside), str(project / "linked"))

    content = search(project, action="content", patterns=["needle"], options=["-F"])
    paths = search(project, action="paths", kind="files")
    followed = search(project, action="content", patterns=["needle"], options=["-F", "-L"])
    explicit = search(project, action="content", patterns=["needle"], paths=["linked"])

    assert content["content"] == "a.txt:1:needle"
    assert paths["content"] == "a.txt"
    assert followed["content"] == "a.txt:1:needle\nlinked/secret.txt:1:needle"
    assert explicit["content"] == "linked/secret.txt:1:needle"


@pytest.mark.asyncio
@pytest.mark.parametrize("pattern", ["true", "false"])
async def test_boolean_words_are_search_text_not_flag_values(tmp_path, pattern):
    (tmp_path / "a").write_text("true false\n")
    result = await dispatch(tmp_path, {"args": ["-n", pattern]})
    assert result["data"]["content"] == "a:1:true false"


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


@pytest.mark.asyncio
async def test_missing_engine_hides_tool_and_dispatch_explains_repair(tmp_path, monkeypatch):
    def unavailable():
        raise ValueError("Repair the vBot installation; run python -m cli.search_runtime.")

    monkeypatch.setattr("core.tools.search_files.require_binary", unavailable)
    registry = search_registry()
    assert registry.provider_definitions(["search_files"]) == []
    result = await registry.dispatch(context(tmp_path), {"args": ["--files"]})
    assert result["error"]["code"] == "tool_not_ready"
    assert "cli.search_runtime" in result["error"]["message"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX literal filename grammar")
def test_unusual_literal_names_roundtrip_in_file_modes(tmp_path):
    for name in ('"quoted"', "literal\\name", "with\nnewline"):
        (tmp_path / name).write_text("needle\n")
        data = search(tmp_path, action="content", patterns=["needle"], paths=[name], options=["-c"])
        assert data["complete"]
        assert data["content"].endswith(":1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("globs", "expected", "must_visit"),
    [
        (["wanted/**"], ["wanted/.hidden.rs", "wanted/deep/a.rs"], {"wanted/deep"}),
        (["wanted/*.rs"], ["wanted/.hidden.rs"], set()),
        (["{wanted,other}/deep/*.rs"], ["other/deep/b.rs", "wanted/deep/a.rs"], {"other/deep"}),
        (["**/deep/*.rs"], ["other/deep/b.rs", "wanted/deep/a.rs"], {"other", "wanted/deep"}),
        (
            ["wanted/**", "!wanted/deep/**", "wanted/deep/a.rs"],
            ["wanted/.hidden.rs", "wanted/deep/a.rs"],
            {"wanted/deep"},
        ),
        (["WANTED/**"], ["wanted/.hidden.rs", "wanted/deep/a.rs"], {"wanted/deep"}),
        (["./*.rs"], ["top.rs"], set()),
    ],
)
async def test_rooted_filters_prune_only_impossible_subtrees(
    tmp_path, monkeypatch, globs, expected, must_visit
):
    files = {
        "wanted/.hidden.rs": "needle\n",
        "wanted/deep/a.rs": "needle\n",
        "wanted/ignored.rs": "needle\n",
        "wanted/.git/config": "needle\n",
        "wanted/.gitignore": "ignored.rs\n",
        "other/deep/b.rs": "needle\n",
        "unrelated/deep/c.txt": "needle\n",
        "top.rs": "needle\n",
    }
    for name, content in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    visited = set()
    scandir = _search_selection.os.scandir

    def observe(path):
        if isinstance(path, (str, Path)) and Path(path).is_relative_to(tmp_path):
            visited.add(Path(path).relative_to(tmp_path).as_posix())
        return scandir(path)

    monkeypatch.setattr(_search_selection.os, "scandir", observe)
    result = await dispatch(tmp_path, {"pattern": "needle", "glob": globs, "output": "files"})

    assert result["ok"], result
    assert result["data"]["content"].splitlines() == expected
    assert result["data"]["complete"] is True
    assert must_visit <= visited
    assert "wanted/.git" not in visited
    if "**/deep/*.rs" not in globs:
        assert "unrelated" not in visited
    if globs == ["wanted/*.rs"]:
        assert "wanted/deep" not in visited


@pytest.mark.asyncio
async def test_basename_filters_still_find_deep_matches_and_explicit_ignored_roots(tmp_path):
    (tmp_path / ".gitignore").write_text("vendor/\n")
    target = tmp_path / "vendor/one/deep/file.rs"
    target.parent.mkdir(parents=True)
    target.write_text("needle\n")

    result = await dispatch(tmp_path, {"pattern": "needle", "glob": "*.rs", "path": "vendor"})

    assert result["data"]["content"] == "vendor/one/deep/file.rs:1:needle"
    assert result["data"]["complete"] is True


@pytest.mark.asyncio
async def test_pruning_preserves_selected_directories_and_overlapping_roots(tmp_path):
    (tmp_path / "wanted/empty").mkdir(parents=True)
    (tmp_path / "other/deep").mkdir(parents=True)

    result = await dispatch(
        tmp_path, {"args": ["--dirs", "--sort=path"], "glob": "wanted/", "path": [".", "."]}
    )

    assert result["data"]["content"] == "wanted/"
    assert result["data"]["complete"] is True


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DirEntry omits device identities")
@pytest.mark.asyncio
async def test_one_file_system_uses_real_device_identity_for_files_and_directories(
    tmp_path, monkeypatch
):
    for name in ["local.rs", "foreign.rs", "mount/nested.rs"]:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("needle\n")
    foreign_paths = {tmp_path / "foreign.rs", tmp_path / "mount"}
    original_stat = Path.stat

    def device_stat(path, **kwargs):
        info = original_stat(path, **kwargs)
        if path in foreign_paths:
            fields = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
            fields["st_dev"] += 1
            return SimpleNamespace(**fields)
        return info

    monkeypatch.setattr(Path, "stat", device_stat)
    unrestricted = await dispatch(tmp_path, {"glob": "*.rs", "args": ["--sort=path"]})
    restricted = await dispatch(tmp_path, {"glob": "*.rs", "args": ["--one-file-system"]})

    assert unrestricted["data"]["content"] == "foreign.rs\nlocal.rs\nmount/nested.rs"
    assert restricted["data"]["content"] == "local.rs"
    assert restricted["data"]["complete"] is True


@pytest.mark.asyncio
async def test_timeout_offers_a_callable_directory_narrowing_step(tmp_path, monkeypatch):
    import core.tools.search as shared

    (tmp_path / "vendor/package/src").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    with monkeypatch.context() as patch:
        patch.setattr(shared, "SEARCH_TIMEOUT_SECONDS", -1)
        timed_out = await dispatch(
            tmp_path, {"pattern": "needle", "path": "vendor", "glob": "*.rs"}
        )

    assert timed_out["ok"]
    assert timed_out["data"]["complete"] is False
    assert timed_out["data"]["warnings"]
    recovery = await dispatch(tmp_path, timed_out["data"]["narrow_call"])
    assert recovery["data"]["content"] == "vendor/package/"
    assert recovery["data"]["complete"] is True
