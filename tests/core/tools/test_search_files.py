"""The search_files Tool: equality with ripgrep, paging, scope, encodings, and the result."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.permissions import (
    BoundaryContext,
    PermissionBoundary,
    PermissionDenied,
    normalize_permission_rules,
)
from core.tools import _search_execution as execution
from core.tools._search_execution import MAX_CHILD_MEMORY
from core.utils.search_binary import require_binary
from tests.core.tools.search_files_test_support import context, dispatch, search, search_registry
from tests.directory_links import link_directory

# The flags search_files always passes, in the form a ripgrep command line takes them.
_RG_DEFAULTS = ["--no-config", "--hidden", "--no-require-git", "--glob-case-insensitive"]
_RG_TEXT = ["--heading", "--with-filename", "--line-number", "--color=never"]
# ripgrep flags whose output lists files or counts instead of lines.
_RG_ENTRY_FLAGS = {"-c", "-l", "--count-matches", "--files-without-match"}


def _write(root: Path, files: dict[str, str | bytes]) -> None:
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8", newline="")


_PROJECT: dict[str, str | bytes] = {
    ".gitignore": "*.log\n!keep.log\nbuild/\n/root-only.txt\n**/gen/**/*.tmp\nnested/*.md\n",
    ".ignore": "ignored-by-dot-ignore.txt\n",
    "src/.gitignore": "local_only.py\n",
    ".git/config": "needle in git metadata\n",
    ".git/HEAD": "needle\n",
    ".hidden/conf.txt": "needle hidden\n",
    ".env": "NEEDLE=1\n",
    "a.log": "needle log\n",
    "keep.log": "needle kept log\n",
    "build/out.js": "needle built\n",
    "root-only.txt": "needle root\n",
    "sub/root-only.txt": "needle sub root\n",
    "gen/x/y.tmp": "needle tmp\n",
    "gen/y.txt": "needle txt\n",
    "nested/r.md": "needle md\n",
    "nested/deeper/s.md": "needle deeper md\n",
    "ignored-by-dot-ignore.txt": "needle\n",
    "src/app.py": "import os\n\ndef needle():\n    return needle_value\n\nNEEDLE = 1\n",
    "src/local_only.py": "needle\n",
    "src/pkg/test_mod.py": "def test_needle():\n    assert needle\n",
    "src/pkg/mod.py": "x = 'needle'\ny = 'haystack'\nz = 'needle needle'\n",
    "src/web/view.ts": "export const needle = () => needle;\n",
    "docs/guide.md": "# Guide\n\nThe needle\nspans lines\nhere.\n",
    "docs/notes.txt": "first needle\r\nsecond line\r\nthird needle\r\n",
    "data/blob.bin": b"needle\x00binary\n",
    "data/latin1.txt": b"caf\xe9 needle\n",
    "deep/a/b/c/d.txt": "needle deep\n",
    "special/[x].txt": "needle bracket\n",
    "special/with space.txt": "needle space\n",
    "special/umlaut-ä.txt": "needle umlaut\n",
    "plain.txt": "nothing here\n",
}


@pytest.fixture(scope="module")
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A tree with every kind of ignore rule; tests must not change it."""
    root = tmp_path_factory.mktemp("project")
    _write(root, _PROJECT)
    return root


def _ripgrep(root: Path, arguments: list[str]) -> str:
    completed = subprocess.run(
        [str(require_binary()), "--path-separator=/", *arguments],
        cwd=root,
        capture_output=True,
        check=False,
    )
    assert completed.returncode in (0, 1), completed.stderr
    return completed.stdout.decode("utf-8", errors="backslashreplace").replace("\r\n", "\n")


def _results(lines: list[str]) -> list[str]:
    """Flatten line output, pages of it included, to one ``label:N:text`` line per shown line.

    Every line that is neither numbered, empty nor ``--`` is a file heading.
    """
    flat: list[str] = []
    label = None
    for line in lines:
        numbered = re.fullmatch(r"(\d+)([:-])(.*)", line, re.S)
        if numbered:
            number, separator, text = numbered.groups()
            flat.append(f"{label}{separator}{number}{separator}{text}")
        elif line not in {"", "--"}:
            label = line
    return flat


def _all_pages(root: Path, arguments: dict[str, Any], limit: int) -> list[str]:
    lines: list[str] = []
    offset = 0
    while True:
        data = search(root, **arguments, limit=limit, offset=offset)
        if "content" in data:
            lines.extend(data["content"].split("\n"))
        if "next_offset" not in data:
            return lines
        assert data["next_offset"] > offset
        offset = data["next_offset"]


# Each case: the call, then the ripgrep flags and operands it stands for.
_CONTENT_CASES = [
    ({"pattern": "needle"}, [], ["needle"]),
    ({"pattern": "needle", "args": ["-i"]}, ["-i"], ["needle"]),
    ({"pattern": "needle", "glob": "*.py"}, ["-g", "*.py"], ["needle"]),
    (
        {"pattern": "needle", "glob": ["*.py", "!test_*"]},
        ["-g", "*.py", "-g", "!test_*"],
        ["needle"],
    ),
    ({"pattern": "needle", "glob": "src/**/*.py"}, ["-g", "src/**/*.py"], ["needle"]),
    ({"pattern": "needle", "glob": "*.{md,TXT}"}, ["-g", "*.{md,TXT}"], ["needle"]),
    ({"pattern": "needle", "glob": "*.log"}, ["-g", "*.log"], ["needle"]),
    ({"pattern": "needle", "path": ["docs", "src"]}, [], ["needle", "docs", "src"]),
    ({"pattern": "needle", "path": "src/pkg/mod.py"}, [], ["needle", "src/pkg/mod.py"]),
    ({"pattern": "needle", "args": ["-u"]}, ["-u"], ["needle"]),
    ({"pattern": "needle", "args": ["-uu"]}, ["-uu"], ["needle"]),
    ({"pattern": "needle", "args": ["--no-hidden"]}, ["--no-hidden"], ["needle"]),
    ({"pattern": "needle", "args": ["-t", "py"]}, ["-t", "py"], ["needle"]),
    ({"pattern": "needle", "args": ["-T", "py", "-T", "md"]}, ["-Tpy", "-Tmd"], ["needle"]),
    ({"pattern": "needle", "args": ["-w"]}, ["-w"], ["needle"]),
    ({"pattern": "needle hidden", "args": ["-x"]}, ["-x"], ["needle hidden"]),
    ({"pattern": "needle", "path": "src", "args": ["-v"]}, ["-v"], ["needle", "src"]),
    ({"pattern": "needle", "args": ["-m", "1"]}, ["-m1"], ["needle"]),
    ({"pattern": "needle", "args": ["-d", "2"]}, ["--max-depth=2"], ["needle"]),
    ({"pattern": "needle()", "args": ["-F"]}, ["-F"], ["needle()"]),
    ({"pattern": "needle(?=_)", "args": ["-P"]}, ["-P"], ["needle(?=_)"]),
    # The fallback to PCRE2 writes a multi-line debug message, which is no failure.
    (
        {"pattern": "needle(?=_)", "args": ["--engine=auto"]},
        ["--engine=auto"],
        ["needle(?=_)"],
    ),
    ({"pattern": "needle\\nspans", "args": ["-U"]}, ["-U"], ["needle\\nspans"]),
    ({"pattern": "needle", "context": 1}, ["-C1"], ["needle"]),
    (
        {"pattern": "needle", "path": "src", "args": ["-B", "2", "-A", "1"]},
        ["-B2", "-A1"],
        ["needle", "src"],
    ),
    ({"pattern": "ne+dle", "args": ["-o"]}, ["-o"], ["ne+dle"]),
    ({"pattern": "needle", "output": "count"}, ["-c"], ["needle"]),
    ({"pattern": "needle", "args": ["--count-matches"]}, ["--count-matches"], ["needle"]),
    ({"pattern": "needle", "output": "files"}, ["-l"], ["needle"]),
    (
        {"pattern": "needle", "args": ["--files-without-match"]},
        ["--files-without-match"],
        ["needle"],
    ),
]


@pytest.mark.parametrize(("arguments", "flags", "operands"), _CONTENT_CASES)
def test_content_results_equal_ripgreps_across_pages(
    project: Path, arguments: dict[str, Any], flags: list[str], operands: list[str]
) -> None:
    expected = _ripgrep(
        project,
        [*_RG_DEFAULTS, *_RG_TEXT, "--sort=path", *flags, "--glob=!.git", "--", *operands],
    ).splitlines()
    assert expected
    assert _all_pages(project, arguments, limit=10000) == expected
    # Small pages hold the same results, each exactly once, in the same order; each
    # page of line output names the file of its first line again.
    paged = _all_pages(project, arguments, limit=2)
    if _RG_ENTRY_FLAGS.isdisjoint(flags):
        assert _results(paged) == _results(expected)
    else:
        assert paged == expected


@pytest.mark.parametrize(
    ("arguments", "rg_arguments"),
    [
        ({}, []),
        ({"path": "src"}, ["src"]),
        ({"glob": "*.log"}, ["-g", "*.log"]),
        ({"glob": ["**/*.md", "!nested/**"]}, ["-g", "**/*.md", "-g", "!nested/**"]),
        ({"args": ["-u"]}, ["-u"]),
        ({"args": ["-t", "py", "-d", "3"]}, ["-tpy", "--max-depth=3"]),
        ({"glob": "**/*"}, ["-g", "**/*"]),
    ],
)
def test_file_lists_equal_ripgreps(
    project: Path, arguments: dict[str, Any], rg_arguments: list[str]
) -> None:
    operands = [argument for argument in rg_arguments if argument == "src"]
    flags = [argument for argument in rg_arguments if argument != "src"]
    expected = _ripgrep(
        project, [*_RG_DEFAULTS, "--files", *flags, "--glob=!.git", "--", *operands]
    ).splitlines()
    listed = _all_pages(project, arguments, limit=10000)
    assert sorted(listed) == sorted(expected)
    assert not any(line.startswith(".git/") for line in listed)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--dirs"], {"src/", "src/empty/", "src/lib/", ".cache/", "vendor/", "vendor/deep/"}),
        (["--dirs", "--no-hidden"], {"src/", "src/empty/", "src/lib/", "vendor/", "vendor/deep/"}),
        (["--dirs", "-g", "!vendor"], {"src/", "src/empty/", "src/lib/", ".cache/"}),
        (["--dirs", "-g", "empty"], {"src/empty/"}),
        (["--dirs", "-t", "py"], {"src/", "src/lib/", "vendor/", "vendor/deep/"}),
        (["--dirs", "--no-glob-case-insensitive", "-t", "py"], {"src/", "src/lib/"}),
        (["--dirs", "-e", "^emp"], {"src/empty/"}),
        (["--dirs", "-e", "^EMP"], set()),
        (["--dirs", "-i", "-e", "^EMP", "-e", "^li"], {"src/empty/", "src/lib/"}),
        # Names match with ripgrep's syntax and matching flags, each name as one whole text.
        (
            ["--dirs", "-x", "-e", "[[:lower:]]+"],
            {"src/", "src/empty/", "src/lib/", "vendor/", "vendor/deep/"},
        ),
        (["--dirs", "-d", "1"], {"src/", ".cache/", "vendor/"}),
        (
            ["--dirs", "-u"],
            {
                "src/",
                "src/empty/",
                "src/lib/",
                ".cache/",
                "vendor/",
                "vendor/deep/",
                "build/",
                "build/out/",
                "src/lib/ignored/",
            },
        ),  # fmt: skip
    ],
)
def test_directory_lists_include_empty_directories_ripgrep_enters(
    tmp_path: Path, args: list[str], expected: set[str]
) -> None:
    _write(
        tmp_path,
        {
            ".gitignore": "build/\nignored/\n",
            "src/lib/a.py": "x\n",
            ".cache/state": "x\n",
            "vendor/deep/notes.txt": "x\n",
            "vendor/deep/B.PY": "x\n",
        },
    )
    for name in ("src/empty", "build/out", "src/lib/ignored", ".git/objects"):
        (tmp_path / name).mkdir(parents=True)
    assert set(_all_pages(tmp_path, {"args": args}, limit=2)) == expected


_FILES = {f"d{index}/f{index}.txt" for index in range(40)}


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, _FILES),
        ({"pattern": "absent", "args": ["--files-without-match"]}, _FILES),
        # The directory list reads a file list first, then matches the directory names.
        ({"pattern": "^d", "args": ["--dirs"]}, {f"d{index}/" for index in range(40)}),
    ],
)
def test_the_record_bound_applies_to_each_listed_path_and_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    expected: set[str],
) -> None:
    _write(tmp_path, dict.fromkeys(_FILES, "x\n"))
    # Each path or name ripgrep prints is shorter than the bound; all of them are longer.
    monkeypatch.setattr("core.tools._search_native.MAX_PROTOCOL_LINE", 32)
    assert set(_all_pages(tmp_path, arguments, limit=10000)) == expected


def test_a_result_over_the_record_bound_fails_naming_the_file_to_leave_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(tmp_path, {"data/big.min.js": "needle " + "x" * 5000 + "\n", "src/a.py": "needle\n"})
    # The long line's match event exceeds the bound; every other record stays below it.
    monkeypatch.setattr("core.tools._search_native.MAX_PROTOCOL_LINE", 4096)

    result = asyncio.run(dispatch(tmp_path, {"pattern": "needle"}))

    assert result["error"] == {
        "code": "search_error",
        "message": "The search stopped without results: a line in data/big.min.js exceeds the "
        '8 MiB limit for one result. Exclude that file with glob "!data/big.min.js" to see the '
        'other results, or set output to "files" or "count", which show no lines.',
    }
    # The calls the error names succeed.
    excluded = search(tmp_path, pattern="needle", glob="!data/big.min.js")
    assert excluded["content"] == "src/a.py\n1:needle"
    listed = search(tmp_path, pattern="needle", output="files")
    assert listed["content"] == "data/big.min.js\nsrc/a.py"


@pytest.mark.parametrize(
    ("arguments", "advice"),
    [
        ({"pattern": "needle"}, 'add "--max-filesize", "50M" to args'),
        ({"pattern": "needle", "args": ["-U"]}, "Leave out -U unless matches need to span lines."),
        ({}, "Narrow path or glob to list fewer files."),
        ({"args": ["--dirs"]}, "Narrow path or glob to list fewer directories."),
    ],
)
def test_a_search_over_the_memory_bound_fails_with_advice_for_its_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, Any], advice: str
) -> None:
    _write(tmp_path, {"src/a.py": "needle\n"})

    class Oversized:
        def __init__(self, _pid: int) -> None:
            pass

        def memory_info(self) -> SimpleNamespace:
            return SimpleNamespace(rss=MAX_CHILD_MEMORY + 1)

    monkeypatch.setattr("core.tools._search_native.psutil.Process", Oversized)

    result = asyncio.run(dispatch(tmp_path, arguments))

    assert result["error"]["code"] == "search_error"
    message = result["error"]["message"]
    assert "stopped without results: it exceeded its 512 MiB memory limit." in message
    assert advice in message


_OUTPUT_BOUND = ", when their paths reached the 256 MiB output limit"


@pytest.mark.parametrize(
    ("bound", "value", "arguments", "reason"),
    [
        # Either bound keeps fewer than the 40 files: 5 entries, or about 100 bytes of output.
        # A listing pass prints paths; a counting pass prints counts and then statistics.
        ("MAX_ENTRIES", 5, {}, ""),
        ("MAX_ENTRIES", 5, {"pattern": "x", "output": "files"}, ""),
        ("MAX_SCAN_BYTES", 100, {}, _OUTPUT_BOUND),
        ("MAX_SCAN_BYTES", 100, {"pattern": "x", "output": "files"}, _OUTPUT_BOUND),
    ],
)
def test_a_pass_a_bound_cut_short_names_its_reason_and_the_files_kept(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bound: str,
    value: int,
    arguments: dict[str, Any],
    reason: str,
) -> None:
    _write(tmp_path, dict.fromkeys(_FILES, "x\n"))
    monkeypatch.setattr(f"core.tools._search_execution.{bound}", value)
    native_lines = execution.native_lines
    read = 0

    def counted(*args: Any, **kwargs: Any) -> Iterator[bytes]:
        nonlocal read
        records = native_lines(*args, **kwargs)
        with contextlib.closing(records):
            for record in records:
                read += 1
                yield record

    monkeypatch.setattr(execution, "native_lines", counted)

    data = search(tmp_path, **arguments, limit=10000)

    kept = data["content"].splitlines()
    assert 0 < len(kept) < len(_FILES)
    # The pass stops reading, and so its child, at the first record past the bound.
    assert read <= len(kept) + 1
    assert data["warnings"] == [
        f"The search stopped after {len(kept)} files{reason}; narrow path or glob to see the rest."
    ]
    assert data["summary"].endswith("The search is incomplete; see warnings.")


@pytest.mark.parametrize(
    ("arguments", "skipped"),
    [
        # No match and listings name what ignore files left out, shallowest first.
        ({"pattern": "absent"}, "build/, src/cache/"),
        ({"glob": "*.log"}, "build/, src/cache/"),
        ({"args": ["--dirs"]}, "build/, src/cache/"),
        # File types select files after ignore files, so they leave the same paths out.
        ({"args": ["-t", "py", "--files"]}, "build/, src/cache/"),
        ({"pattern": "absent", "args": ["-t", "py"]}, "build/, src/cache/"),
        # Every file matches, so no file lacks a match.
        ({"pattern": ".", "args": ["--files-without-match"]}, "build/, src/cache/"),
        # Matches answer the question; -u and an explicit exclusion leave nothing to name.
        ({"pattern": "needle"}, None),
        ({"pattern": "absent", "args": ["-u"]}, None),
        ({"pattern": "absent", "glob": "!src/a.py", "path": "src/a.py"}, None),
    ],
)
def test_results_name_paths_ignore_files_excluded(
    tmp_path: Path, arguments: dict[str, Any], skipped: str | None
) -> None:
    _write(
        tmp_path,
        {
            ".gitignore": "build/\ncache/\n",
            "src/a.py": "needle\n",
            "src/cache/c.log": "needle\n",
            "build/out/b.log": "needle\n",
        },
    )
    data = search(tmp_path, **arguments)
    expected = None
    if skipped is not None:
        expected = (
            f"Ignore rules such as .gitignore excluded {skipped}. Add -u to args to include them."
        )
    assert data.get("skipped") == expected


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_ignore_rules_select_the_files_git_selects(tmp_path: Path) -> None:
    rules = [
        "*.log",
        "!keep.log",
        "build/",
        "/root.txt",
        "**/gen/**/*.tmp",
        "docs/**/secret.md",
        "\\#hash.txt",
        "foo[0-9].dat",
        "dir/*",
        "!dir/keep.txt",
        "a/**/b",
        "nested/*.md",
        "q?.txt",
        "lead/**",
        "!lead/keep.txt",
    ]
    names = [
        "root.txt", "sub/root.txt", "a.log", "keep.log", "sub/keep.log", "build/out.js",
        "src/build", "gen/x/y.tmp", "gen/y.tmp", "other/gen/z.txt", "docs/a/secret.md",
        "docs/public.md", "#hash.txt", "foo1.dat", "fooa.dat", "dir/keep.txt", "dir/drop.txt",
        "a/b", "a/x/y/b", "a/bb", "nested/r.md", "nested/deeper/s.md", "q1.txt", "q12.txt",
        "lead/one.txt", "lead/keep.txt", ".hidden/f.txt", "info-excluded.txt",
        "sub/.gitignore", "sub/local.py", "sub/deep/local.py",
    ]  # fmt: skip
    _write(tmp_path, dict.fromkeys(names, "x\n"))
    _write(tmp_path, {".gitignore": "\n".join(rules) + "\n", "sub/.gitignore": "local.py\n"})
    git = ["git", "-c", "core.excludesFile=", "-C", str(tmp_path)]
    subprocess.run([*git, "init", "-q"], check=True)
    _write(tmp_path, {".git/info/exclude": "info-excluded.txt\n"})
    selected = subprocess.run(
        [*git, "ls-files", "--others", "--exclude-standard"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout.split()

    listed = _all_pages(tmp_path, {"args": ["--no-ignore-global"]}, limit=10000)

    assert sorted(listed) == sorted(selected)


def test_summary_states_totals_and_the_continuation(project: Path) -> None:
    first = search(project, pattern="needle", path="src", limit=2)
    assert first["summary"] == (
        "Showing matching lines 1-2 of 7 matching lines in 4 files. Continue with offset 2."
    )
    assert first["next_offset"] == 2
    last = search(project, pattern="needle", path="src", offset=6)
    assert last["summary"] == "Showing matching lines 7-7 of 7 matching lines in 4 files."
    assert "next_offset" not in last
    # An offset past the end, as after a query changed between pages, starts over.
    beyond = search(project, pattern="needle", path="src", offset=7, limit=2)
    assert (beyond["content"], beyond["next_offset"]) == (first["content"], 2)
    assert "offset 7" in beyond["note"]
    listed = search(project, path="src", offset=5)
    assert listed["content"] == search(project, path="src")["content"] and listed["note"]
    counts = search(project, pattern="needle", path="src", output="count", limit=1)
    assert counts["summary"] == (
        "Showing files 1-1 of 7 matching lines in 4 files. Continue with offset 1."
    )
    files = search(project, pattern="needle", path="src", output="files")
    assert files["summary"] == "Found 4 files with 7 matching lines."
    assert search(project, path="src")["summary"] == "Found 5 files, newest first."
    absent = search(project, pattern="absent", path="src")
    # An empty result echoes neither the pattern nor the roots and has no empty content.
    assert absent["summary"] == "No matches in 5 searched files."
    assert not {"content", "patterns", "searched_paths"} & absent.keys()
    # Globs that select no file are named; globs that select files leave the count.
    for arguments in ({"pattern": "needle"}, {}):
        unmatched = search(project, path="src", glob=["verify.py", "*.md"], **arguments)
        assert '"verify.py", "*.md"' in unmatched["summary"]
    assert '"*.py"' not in search(project, pattern="absent", glob="*.py")["summary"]


def test_file_lists_are_newest_first_unless_sorted(tmp_path: Path) -> None:
    _write(tmp_path, {"old.txt": "", "new.txt": "", "mid.txt": ""})
    for name, stamp in (("old.txt", 1_000_000_000), ("mid.txt", 1_500_000_000)):
        os.utime(tmp_path / name, (stamp, stamp))
    assert search(tmp_path)["content"] == "new.txt\nmid.txt\nold.txt"
    assert search(tmp_path, args=["--sort", "path"])["content"] == "mid.txt\nnew.txt\nold.txt"
    assert search(tmp_path, args=["--sortr=modified"])["content"] == "new.txt\nmid.txt\nold.txt"
    pages = _all_pages(tmp_path, {}, limit=1)
    assert pages == ["new.txt", "mid.txt", "old.txt"]


def test_context_never_shows_a_match_the_page_leaves_out(tmp_path: Path) -> None:
    _write(tmp_path, {"a": "one\nhit 1\nmid\nhit 2\nmid\nhit 3\nlast\n"})
    first = search(tmp_path, pattern="hit", context=2, limit=1)
    assert first["content"] == "a\n1-one\n2:hit 1\n3-mid"
    second = search(tmp_path, pattern="hit", context=2, limit=1, offset=1)
    assert second["content"] == "a\n3-mid\n4:hit 2\n5-mid"


def test_a_page_stops_at_the_output_limit_and_the_next_page_continues(tmp_path: Path) -> None:
    _write(tmp_path, {"a": "".join(f"needle {i} " + "x" * 900 + "\n" for i in range(150))})
    _write(tmp_path, {"b": ("context " + "y" * 900 + "\n") * 60 + "needle in b\n"})
    seen: list[str] = []
    offset = 0
    while True:
        data = search(tmp_path, pattern="needle", context=60, limit=1000, offset=offset)
        assert len(data["content"].encode()) <= 50 * 1024
        seen.extend(
            line.split(":", 2)[1]
            for line in _results(data["content"].splitlines())
            if line.startswith(("a:", "b:"))
        )
        if "next_offset" not in data:
            break
        assert "stopped at the 50 KB output limit" in data["summary"]
        offset = data["next_offset"]
    assert seen == [str(number) for number in range(1, 151)] + ["61"]
    # A result left without its context, or itself trimmed, says so: paging cannot show the rest.
    alone = search(tmp_path, pattern="needle in b", context=60)
    assert alone["content"] == "b\n61:needle in b"
    assert "context lines were left out" in alone["summary"]
    _write(tmp_path, {"c": "begin\n" + ("z" * 900 + "\n") * 100 + "end\n"})
    cut = search(tmp_path, pattern="begin[\\s\\S]*?end", path="c", args=["-U"])
    shown = _results(cut["content"].splitlines())
    assert 1 < len(shown) < 102 and "next_offset" not in cut
    assert "spans lines 1-102" in cut["summary"]
    assert f"from line {len(shown) + 1} " in cut["summary"]


def test_multiline_matches_count_and_page_as_whole_matches(tmp_path: Path) -> None:
    _write(tmp_path, {"a": "start\nmiddle\nend\nstart\nend\n"})
    arguments = {"pattern": "start\\n(middle\\n)?end", "args": ["-U"]}
    whole = search(tmp_path, **arguments)
    assert whole["content"] == "a\n1:start\n2:middle\n3:end\n4:start\n5:end"
    assert whole["summary"] == "Found 2 matches in 1 file."
    assert search(tmp_path, **arguments, output="count")["content"] == "a:2"
    first = search(tmp_path, **arguments, limit=1)
    assert first["content"] == "a\n1:start\n2:middle\n3:end"
    assert first["next_offset"] == 1
    assert _results(_all_pages(tmp_path, arguments, limit=1)) == _results(
        whole["content"].split("\n")
    )
    # Matches that share a line are separate results; the line appears once per page.
    _write(tmp_path, {"b": "a a\nb\na\n"})
    shared = search(tmp_path, pattern="a", path="b", args=["-U"])
    assert (shared["summary"], shared["content"]) == (
        "Found 3 matches in 1 file.",
        "b\n1:a a\n3:a",
    )
    assert _all_pages(tmp_path, {"pattern": "a", "path": "b", "args": ["-U"]}, limit=1) == [
        "b",
        "1:a a",
        "b",
        "1:a a",
        "b",
        "3:a",
    ]


def test_roots_outside_the_working_directory_keep_absolute_labels(tmp_path: Path) -> None:
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    _write(workspace, {"tests/test_a.py": "needle\n", "tests/data.txt": "needle\n"})
    _write(outside, {"lib/b.py": "needle\n", "lib/c.txt": "needle\n"})
    data = search(workspace, pattern="needle", path=["tests", str(outside / "lib")], glob="*.py")
    assert sorted(data["content"].split("\n")) == sorted(
        ["tests/test_a.py", "1:needle", "", (outside / "lib/b.py").as_posix(), "1:needle"]
    )
    # A glob with a slash also applies relative to each searched directory.
    anchored = search(workspace, pattern="needle", path="tests", glob="tests/*.py")
    assert anchored["content"] == "tests/test_a.py\n1:needle"
    relative = search(workspace, pattern="needle", path="tests", glob="/*.py")
    assert relative["content"] == "tests/test_a.py\n1:needle"


@pytest.mark.parametrize(
    "arguments",
    [
        {"pattern": "needle", "args": ["-uuu"]},
        {"pattern": "needle", "glob": "**/*"},
        {"pattern": "needle", "glob": ".git/**"},
        {"args": ["--files", "-uuu"]},
        {"args": ["--dirs", "-uuu"]},
    ],
)
def test_the_git_directory_is_never_searched(project: Path, arguments: dict[str, Any]) -> None:
    content = search(project, **arguments, limit=10000).get("content", "")
    assert not any(line.startswith(".git/") for line in content.splitlines())


@pytest.mark.parametrize(
    ("arguments", "listed"),
    [
        ({}, {"pkg/a.py"}),
        ({"pattern": "needle", "output": "files"}, {"pkg/a.py"}),
        ({"args": ["--dirs"]}, {"pkg/"}),
        ({"glob": "!*.txt"}, {"pkg/a.py"}),
        # Asked for: -u, a glob naming the caches or their files, or a root inside one.
        ({"args": ["-u"]}, {"pkg/a.py", "pkg/__pycache__/a.pyc", "__pycache__/note.txt"}),
        ({"glob": "*.pyc"}, {"pkg/__pycache__/a.pyc"}),
        ({"glob": "**/__pycache__/**"}, {"pkg/__pycache__/a.pyc", "__pycache__/note.txt"}),
        ({"path": "pkg/__pycache__"}, {"pkg/__pycache__/a.pyc"}),
    ],
)
def test_python_caches_are_skipped_unless_asked_for(
    tmp_path: Path, arguments: dict[str, Any], listed: set[str]
) -> None:
    _write(
        tmp_path,
        {"pkg/a.py": "needle\n", "pkg/__pycache__/a.pyc": "needle\n", "__pycache__/note.txt": "x"},
    )
    content = search(tmp_path, **arguments)["content"]
    assert set(content.splitlines()) == listed


def test_long_lines_show_the_match_in_an_excerpt(tmp_path: Path) -> None:
    _write(tmp_path, {"a": "x" * 100000 + "NEEDLE" + "y" * 100000 + "\n"})
    content = search(tmp_path, pattern="NEEDLE")["content"]
    assert "NEEDLE" in content
    assert content.startswith("a\n1:[99800 characters omitted] ")
    assert content.endswith(" [99206 characters omitted]")


def test_binary_files_and_encodings(project: Path, tmp_path: Path) -> None:
    assert "content" not in search(project, pattern="binary", path="data")
    assert search(project, pattern="binary", path="data", args=["-a"])["content"] == (
        "data/blob.bin\n1:needle\x00binary"
    )
    latin = search(project, pattern="café", path="data", args=["-E", "latin1"])
    assert latin["content"] == "data/latin1.txt\n1:café needle"
    _write(tmp_path, {"utf16": "needle\n".encode("utf-16"), "crlf": b"needle\r\n"})
    assert search(tmp_path, pattern="needle", path="utf16")["content"] == "utf16\n1:needle"
    assert search(tmp_path, pattern="^needle$", path="crlf", args=["--crlf"])["content"] == (
        "crlf\n1:needle"
    )


def test_link_loops_become_warnings_and_the_rest_is_searched(tmp_path: Path) -> None:
    _write(tmp_path, {"real/a": "needle\n"})
    try:
        (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
        (tmp_path / "real/loop").symlink_to(tmp_path / "real", target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit creating symbolic links")
    data = search(tmp_path, pattern="needle", path="link", args=["-L"])
    assert data["content"] == "link/a\n1:needle"
    assert any("loop" in warning for warning in data["warnings"])
    assert data["summary"].endswith("The search is incomplete; see warnings.")


def _read_errors(*named: tuple[str, int]) -> str:
    return "\n".join(
        f"rg: {name}: IO error for operation on {name}: failed (os error {code})"
        for name, code in named
    )


_LOCKED = [f"locked_{index}.txt" for index in range(8)]


@pytest.mark.parametrize(
    ("diagnostics", "dropped", "warned", "unread"),
    [
        # ripgrep's walk listed a SQLite journal that was gone when it read it; any other
        # read error, or a not-found error for a file that exists, still warns.
        (
            _read_errors(("gone.sqlite-journal", 2), ("locked_0.txt", 5), ("a.txt", 2)),
            0,
            ["locked_0.txt", "a.txt"],
            0,
        ),
        # Swarm run 2026-10-20: a root full of locked files. The first five are named,
        # the others and the messages past the native bound are counted.
        (_read_errors(*((name, 32) for name in _LOCKED)), 40, _LOCKED[:5], 43),
        # A message that names no path does not fail a search that ran.
        ("rg: something unexpected", 0, ["something unexpected"], 0),
    ],
)
def test_files_ripgrep_could_not_read_are_warnings_beside_the_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    diagnostics: str,
    dropped: int,
    warned: list[str],
    unread: int,
) -> None:
    _write(tmp_path, {"a.txt": "needle\n", **dict.fromkeys(_LOCKED, "x\n")})
    native_lines = execution.native_lines

    def with_read_errors(*args: Any, outcome: Any = None, **kwargs: Any) -> Iterator[bytes]:
        yield from native_lines(*args, outcome=outcome, **kwargs)
        # The counting pass reads every file; ripgrep fails after any such diagnostic.
        if outcome is not None and "--json" not in args[1]:
            outcome.returncode = 2
            outcome.diagnostics, outcome.dropped = diagnostics, dropped

    monkeypatch.setattr(execution, "native_lines", with_read_errors)
    data = search(tmp_path, pattern="needle")
    assert data["content"] == "a.txt\n1:needle"
    assert data["summary"].endswith("The search is incomplete; see warnings.")
    warnings = data["warnings"]
    assert all(any(text in warning for warning in warnings) for text in warned)
    assert "gone" not in " ".join(warnings)
    assert len(warnings) == len(warned) + bool(unread)
    if unread:
        assert str(unread) in warnings[-1]


def test_junctions_are_followed_only_on_request_or_as_explicit_roots(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows directory junctions")
    import _winapi

    project, outside = tmp_path / "project", tmp_path / "outside"
    _write(project, {"a.txt": "needle"})
    _write(outside, {"secret.txt": "needle"})
    _winapi.CreateJunction(str(outside), str(project / "linked"))

    assert search(project, pattern="needle")["content"] == "a.txt\n1:needle"
    assert search(project)["content"] == "a.txt"
    assert search(project, pattern="needle", args=["-L"])["content"] == (
        "a.txt\n1:needle\n\nlinked/secret.txt\n1:needle"
    )
    assert search(project, pattern="needle", path="linked")["content"] == (
        "linked/secret.txt\n1:needle"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX file name grammar")
def test_unusual_file_names_are_labelled_so_they_can_be_passed_back(tmp_path: Path) -> None:
    _write(tmp_path, {'"quoted"': "needle\n", "back\\slash": "needle\n", "new\nline": "needle\n"})
    content = search(tmp_path, pattern="needle", output="files")["content"].splitlines()
    assert sorted(content) == sorted(['"quoted"', "back\\slash", '"new\\nline"'])


@pytest.mark.asyncio
async def test_timeout_and_user_cancel(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import core.tools.search as shared

    cancelled = await dispatch(project, {"pattern": "needle"}, cancel_check_hook=lambda: True)
    assert cancelled["error"]["code"] == "cancelled_by_user"
    monkeypatch.setattr(shared, "SEARCH_TIMEOUT_SECONDS", -1)
    timed_out = await dispatch(project, {"pattern": "needle"})
    assert timed_out["ok"], timed_out
    assert timed_out["data"]["warnings"] == [
        "The search stopped at its 30-second limit, so results are partial. Narrow path or "
        "glob to search the rest."
    ]
    assert "incomplete" in timed_out["data"]["summary"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_at", ["start", "root"])
async def test_a_failed_start_is_explained_in_english(
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
    deny_access,
    failure_at: str,
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError(errno.EACCES, "Zugriff verweigert", "rg.exe", 5)

    if failure_at == "root":
        deny_access(project / "plain.txt")
    else:
        monkeypatch.setattr("core.tools._search_native.subprocess.Popen", refuse)
    result = await dispatch(project, {"pattern": "needle", "path": "plain.txt"})
    assert result["error"]["code"] == "search_error"
    assert "Zugriff" not in result["error"]["message"]
    if failure_at == "root":
        assert "plain.txt" in result["error"]["message"]


def test_schema_display_and_registry_repairs(project: Path) -> None:
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
    result = asyncio.run(dispatch(project, {"argv": ["-n", "needle", "src/pkg"], "limit": "1"}))
    assert result["data"]["content"] == "src/pkg/mod.py\n1:x = 'needle'"


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


@pytest.mark.asyncio
async def test_the_user_sees_the_results_further_pages_and_warnings(tmp_path: Path) -> None:
    _write(tmp_path, {"src/a.py": "needle\nneedle\n"})
    arguments = {"pattern": "needle", "path": ["missing", "src"], "limit": 1}
    registry = search_registry()

    result = await registry.dispatch(context(tmp_path), arguments)
    details = registry.display_for_call("search_files", arguments, result=result)["details"]

    # The content is read from the result, not copied; paging fields stay raw.
    assert details == [
        {
            "type": "text",
            "label": "results",
            "source": {"from": "result", "path": ["data", "content"]},
        },
        {
            "type": "notice",
            "level": "info",
            "text": "More results follow; the next page starts at result 2.",
        },
        {
            "type": "notice",
            "level": "warning",
            "text": f"Path not found: missing (relative to the working directory "
            f"{tmp_path.as_posix()}).",
        },
    ]


_DENYING = {
    "read": "work_area",
    "deny": [
        "Read({workdir}/src/secrets/**)",
        "Read({workdir}/**/*.key)",
        "Read({workdir}/**/draft[1].txt)",
    ],
}


def _restricted_tree(tmp_path: Path) -> tuple[Path, PermissionBoundary]:
    work = tmp_path / "work"
    _write(
        work,
        {
            "src/a.txt": "token\n",
            "src/secrets/k.txt": "token\n",
            "src/c.key": "token\n",
            "src/D.KEY": "token\n",
            "src/draft[1].txt": "token\n",
            "b.key": "token\n",
        },
    )
    _write(tmp_path / "outside", {"o.txt": "token\n"})
    link_directory(work / "src" / "linked", tmp_path / "outside")
    boundary = PermissionBoundary.build(
        normalize_permission_rules(_DENYING),
        BoundaryContext(workdir=work, home=tmp_path / "home", data_dir=tmp_path / "data"),
    )
    return work, boundary


@pytest.mark.parametrize(
    "arguments",
    [
        {"pattern": "token"},
        {"pattern": "token", "args": ["-l"]},
        {"pattern": "token", "path": "src"},
        {"pattern": "token", "args": ["-L"]},
        {"args": ["--files"]},
        {"args": ["--dirs", "-L"]},
    ],
)
def test_a_restricted_search_leaves_out_what_the_rules_refuse(
    tmp_path: Path, arguments: dict[str, Any]
) -> None:
    work, boundary = _restricted_tree(tmp_path)

    result = asyncio.run(dispatch(work, arguments, permissions=boundary))
    full = json.dumps(search(work, **arguments))

    assert result["ok"], result
    note = result["data"].pop("note")
    restricted = json.dumps(result["data"])
    assert "src" in restricted
    assert "secrets" in full
    for refused in ("secrets", ".key", "o.txt", "draft"):
        assert refused not in restricted
    assert ("KEY" in restricted) is (sys.platform != "win32")
    # Like a read listing, the note names what the rules left out, as the rules write it.
    assert "src/secrets/**" in note and "the permission rules refuse reading them" in note
    assert ("src/**/*.key" if "path" in arguments else "**/*.key") in note
    if "-L" in arguments.get("args", []):
        assert "links" in note


def test_a_restricted_search_counts_only_the_files_it_may_read(tmp_path: Path) -> None:
    work, boundary = _restricted_tree(tmp_path)

    result = asyncio.run(dispatch(work, {"pattern": "nomatch"}, permissions=boundary))

    # src/a.txt, and src/D.KEY where the rules match case-sensitively.
    searched = "1 searched file." if sys.platform == "win32" else "2 searched files."
    assert result["data"]["summary"] == f"No matches in {searched}"


@pytest.mark.parametrize(
    "arguments",
    [
        {"pattern": "token", "path": "../outside"},
        {"pattern": "token", "path": "src/secrets"},
        {"pattern": "token", "path": "src/linked/o.txt"},
        {"pattern": "token", "path": "{src,../outside}"},
        {"args": ["-f", "../outside/o.txt"]},
        {"pattern": "token", "args": ["--ignore-file=src/secrets/k.txt"]},
    ],
)
def test_a_restricted_search_refuses_unreadable_roots_and_path_flags(
    tmp_path: Path, arguments: dict[str, Any]
) -> None:
    work, boundary = _restricted_tree(tmp_path)

    with pytest.raises(PermissionDenied) as refused:
        asyncio.run(dispatch(work, arguments, permissions=boundary))

    assert refused.value.refusal.access == "read"
