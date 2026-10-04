"""The search_files Tool: equality with ripgrep, paging, scope, encodings, and the result."""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from core.utils.search_binary import require_binary
from tests.core.tools.search_files_test_support import context, dispatch, search, search_registry

# The flags search_files always passes, in the form a ripgrep command line takes them.
_RG_DEFAULTS = ["--no-config", "--hidden", "--no-require-git", "--glob-case-insensitive"]
_RG_TEXT = ["--no-heading", "--with-filename", "--line-number", "--color=never"]


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


def _all_pages(root: Path, arguments: dict[str, Any], limit: int) -> list[str]:
    lines: list[str] = []
    offset = 0
    while True:
        data = search(root, **arguments, limit=limit, offset=offset)
        if data["content"]:
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
    # Small pages hold the same results, each exactly once, in the same order.
    paged = [line for line in _all_pages(project, arguments, limit=2) if line != "--"]
    assert paged == [line for line in expected if line != "--"]


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


@pytest.mark.parametrize(
    ("arguments", "skipped"),
    [
        # No match and listings name what ignore files left out, shallowest first.
        ({"pattern": "absent"}, "build/, src/cache/"),
        ({"glob": "*.log"}, "build/, src/cache/"),
        ({"args": ["--dirs"]}, "build/, src/cache/"),
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
    beyond = search(project, pattern="needle", path="src", offset=50)
    assert beyond["summary"] == (
        "No results at offset 50; the search found 7 matching lines in 4 files."
    )
    counts = search(project, pattern="needle", path="src", output="count", limit=1)
    assert counts["summary"] == (
        "Showing files 1-1 of 7 matching lines in 4 files. Continue with offset 1."
    )
    files = search(project, pattern="needle", path="src", output="files")
    assert files["summary"] == "Found 4 files with 7 matching lines."
    assert search(project, path="src")["summary"] == "Found 5 files, newest first."
    absent = search(project, pattern="absent", path="src")
    assert absent["summary"] == "No matches in 5 searched files."
    assert absent["patterns"] == ["absent"]


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
    assert first["content"] == "a-1-one\na:2:hit 1\na-3-mid"
    second = search(tmp_path, pattern="hit", context=2, limit=1, offset=1)
    assert second["content"] == "a-3-mid\na:4:hit 2\na-5-mid"


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
            for line in data["content"].splitlines()
            if line.startswith(("a:", "b:"))
        )
        if "next_offset" not in data:
            break
        assert "stopped at the 50 KB output limit" in data["summary"]
        offset = data["next_offset"]
    assert seen == [str(number) for number in range(1, 151)] + ["61"]


def test_multiline_matches_count_and_page_as_whole_matches(tmp_path: Path) -> None:
    _write(tmp_path, {"a": "start\nmiddle\nend\nstart\nend\n"})
    arguments = {"pattern": "start\\n(middle\\n)?end", "args": ["-U"]}
    whole = search(tmp_path, **arguments)
    assert whole["content"] == "a:1:start\na:2:middle\na:3:end\na:4:start\na:5:end"
    assert whole["summary"] == "Found 2 matches in 1 file."
    assert search(tmp_path, **arguments, output="count")["content"] == "a:2"
    first = search(tmp_path, **arguments, limit=1)
    assert first["content"] == "a:1:start\na:2:middle\na:3:end"
    assert first["next_offset"] == 1
    assert _all_pages(tmp_path, arguments, limit=1) == whole["content"].split("\n")
    # Matches that share a line are separate results; the line appears once per page.
    _write(tmp_path, {"b": "a a\nb\na\n"})
    shared = search(tmp_path, pattern="a", path="b", args=["-U"])
    assert (shared["summary"], shared["content"]) == (
        "Found 3 matches in 1 file.",
        "b:1:a a\nb:3:a",
    )
    assert _all_pages(tmp_path, {"pattern": "a", "path": "b", "args": ["-U"]}, limit=1) == [
        "b:1:a a",
        "b:1:a a",
        "b:3:a",
    ]


def test_roots_outside_the_working_directory_keep_absolute_labels(tmp_path: Path) -> None:
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    _write(workspace, {"tests/test_a.py": "needle\n", "tests/data.txt": "needle\n"})
    _write(outside, {"lib/b.py": "needle\n", "lib/c.txt": "needle\n"})
    data = search(workspace, pattern="needle", path=["tests", str(outside / "lib")], glob="*.py")
    assert sorted(data["content"].split("\n")) == sorted(
        ["tests/test_a.py:1:needle", f"{(outside / 'lib/b.py').as_posix()}:1:needle"]
    )
    # A glob with a slash also applies relative to each searched directory.
    anchored = search(workspace, pattern="needle", path="tests", glob="tests/*.py")
    assert anchored["content"] == "tests/test_a.py:1:needle"
    relative = search(workspace, pattern="needle", path="tests", glob="/*.py")
    assert relative["content"] == "tests/test_a.py:1:needle"


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
    content = search(project, **arguments, limit=10000)["content"]
    assert not any(line.startswith(".git/") for line in content.splitlines())


def test_long_lines_show_the_match_in_an_excerpt(tmp_path: Path) -> None:
    _write(tmp_path, {"a": "x" * 100000 + "NEEDLE" + "y" * 100000 + "\n"})
    content = search(tmp_path, pattern="NEEDLE")["content"]
    assert "NEEDLE" in content
    assert content.startswith("a:1:[99800 characters omitted] ")
    assert content.endswith(" [99206 characters omitted]")


def test_binary_files_and_encodings(project: Path, tmp_path: Path) -> None:
    assert search(project, pattern="binary", path="data")["content"] == ""
    assert search(project, pattern="binary", path="data", args=["-a"])["content"] == (
        "data/blob.bin:1:needle\x00binary"
    )
    latin = search(project, pattern="café", path="data", args=["-E", "latin1"])
    assert latin["content"] == "data/latin1.txt:1:café needle"
    _write(tmp_path, {"utf16": "needle\n".encode("utf-16"), "crlf": b"needle\r\n"})
    assert search(tmp_path, pattern="needle", path="utf16")["content"] == "utf16:1:needle"
    assert search(tmp_path, pattern="^needle$", path="crlf", args=["--crlf"])["content"] == (
        "crlf:1:needle"
    )


def test_link_loops_become_warnings_and_the_rest_is_searched(tmp_path: Path) -> None:
    _write(tmp_path, {"real/a": "needle\n"})
    try:
        (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
        (tmp_path / "real/loop").symlink_to(tmp_path / "real", target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit creating symbolic links")
    data = search(tmp_path, pattern="needle", path="link", args=["-L"])
    assert data["content"] == "link/a:1:needle"
    assert any("loop" in warning for warning in data["warnings"])
    assert data["summary"].endswith("The search is incomplete; see warnings.")


def test_junctions_are_followed_only_on_request_or_as_explicit_roots(tmp_path: Path) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows directory junctions")
    import _winapi

    project, outside = tmp_path / "project", tmp_path / "outside"
    _write(project, {"a.txt": "needle"})
    _write(outside, {"secret.txt": "needle"})
    _winapi.CreateJunction(str(outside), str(project / "linked"))

    assert search(project, pattern="needle")["content"] == "a.txt:1:needle"
    assert search(project)["content"] == "a.txt"
    assert search(project, pattern="needle", args=["-L"])["content"] == (
        "a.txt:1:needle\nlinked/secret.txt:1:needle"
    )
    assert search(project, pattern="needle", path="linked")["content"] == (
        "linked/secret.txt:1:needle"
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
async def test_a_failed_start_is_explained_in_english(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError(errno.EACCES, "Zugriff verweigert", "rg.exe", 5)

    monkeypatch.setattr("core.tools._search_execution.subprocess.Popen", refuse)
    result = await dispatch(project, {"pattern": "needle"})
    assert result["error"]["code"] == "search_error"
    assert result["error"]["message"].startswith("search_files could not run the search: ")
    assert "Zugriff" not in result["error"]["message"]
    assert result["error"]["message"].endswith(" Retry the call.")


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
    assert result["data"]["content"] == "src/pkg/mod.py:1:x = 'needle'"


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
