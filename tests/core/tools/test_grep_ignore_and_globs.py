"""Grep: ignore and globs behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

import core.tools.search as search_module
from core.tools.grep import (
    grep_handler,
)
from tests.core.tools.grep_helpers import (
    assert_success_envelope,
    force_python_fallback,
    get_success_content,
    install_fake_rg,
    make_context,
)


def test_grep_skips_gitignored_files_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath(".gitignore").write_text("node_modules/\n", encoding="utf-8")
    workspace.joinpath("node_modules").mkdir()
    workspace.joinpath("node_modules", "lib.js").write_text("needle\n", encoding="utf-8")
    workspace.joinpath("app.js").write_text("needle\n", encoding="utf-8")

    default_result = grep_handler(make_context(workspace), {"pattern": "needle"})
    opted_in_result = grep_handler(
        make_context(workspace), {"pattern": "needle", "include_ignored": True}
    )

    assert get_success_content(default_result) == "app.js:1: needle"
    opted_in_content = get_success_content(opted_in_result)
    assert "app.js:1: needle" in opted_in_content
    assert "node_modules/lib.js:1: needle" in opted_in_content


def test_grep_absolute_repo_path_glob_does_not_override_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()
    repo.joinpath(".git").mkdir()
    repo.joinpath(".gitignore").write_text(".opencode/\n", encoding="utf-8")
    ignored = repo / ".opencode" / "node_modules"
    ignored.mkdir(parents=True)
    ignored.joinpath("lib.js").write_text("needle\n", encoding="utf-8")
    repo.joinpath("app.js").write_text("needle\n", encoding="utf-8")
    created = install_fake_rg(
        monkeypatch,
        stdout_text=".opencode/node_modules/lib.js:1:needle\n",
    )

    result = grep_handler(
        make_context(workspace),
        {"pattern": "needle", "path": str(repo), "glob": "**/*"},
    )

    assert get_success_content(result) == f"{(repo / 'app.js').resolve().as_posix()}:1: needle"
    assert created == []


def test_grep_honors_nested_gitignore_negation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Deeper .gitignore files win over shallower ones, git-style: the nested
    # negation re-includes a file the root pattern ignored.
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath(".gitignore").write_text("*.log\n", encoding="utf-8")
    workspace.joinpath("root.log").write_text("needle\n", encoding="utf-8")
    sub = workspace / "sub"
    sub.mkdir()
    sub.joinpath(".gitignore").write_text("!keep.log\n", encoding="utf-8")
    sub.joinpath("keep.log").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle"})

    assert get_success_content(result) == "sub/keep.log:1: needle"


def test_grep_always_skips_git_internals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    git_dir = workspace / ".git"
    git_dir.mkdir()
    git_dir.joinpath("config").write_text("needle\n", encoding="utf-8")
    workspace.joinpath("app.txt").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle", "include_ignored": True})

    assert get_success_content(result) == "app.txt:1: needle"


def test_grep_searches_explicitly_named_ignored_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath(".gitignore").write_text("secret.txt\n", encoding="utf-8")
    workspace.joinpath("secret.txt").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle", "path": "secret.txt"})

    assert get_success_content(result) == "secret.txt:1: needle"


def test_grep_searches_explicitly_targeted_ignored_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath(".git").mkdir()
    workspace.joinpath(".gitignore").write_text("vendor/\n", encoding="utf-8")
    vendor = workspace / "vendor"
    vendor.mkdir()
    vendor.joinpath("lib.js").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle", "path": "vendor"})

    assert get_success_content(result) == "vendor/lib.js:1: needle"


def test_grep_rg_command_uses_ignore_respecting_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    created = install_fake_rg(monkeypatch)

    grep_handler(make_context(workspace), {"pattern": "needle"})

    command = created[0].command
    assert "--no-ignore" not in command
    assert "--hidden" in command
    assert "--no-require-git" in command
    assert "--glob-case-insensitive" in command
    exclusion_index = command.index("!**/.git")
    assert command[exclusion_index - 1] == "--glob"


def test_grep_rg_command_disables_ignore_rules_on_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    created = install_fake_rg(monkeypatch)

    grep_handler(
        make_context(workspace),
        {"pattern": "needle", "glob": "**/*", "include_ignored": True},
    )

    command = created[0].command
    assert "--no-ignore" in command
    assert "!**/.git" in command
    assert "**/*" in command


def test_grep_searches_worktree_under_ignored_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Worktrees typically live in a gitignored .worktrees/ folder of the main
    # repo. A worktree carries its own .git pointer *file*, which bounds the
    # gitignore evaluation: the main repo's ".worktrees/" rule must not blank
    # out searches running inside the worktree, while the worktree's own
    # checked-out .gitignore still applies.
    force_python_fallback(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    repo.joinpath(".git").mkdir()
    repo.joinpath(".gitignore").write_text(".worktrees/\nnode_modules/\n", encoding="utf-8")
    worktree = repo / ".worktrees" / "task"
    worktree.mkdir(parents=True)
    worktree.joinpath(".git").write_text("gitdir: ../../.git/worktrees/task\n", encoding="utf-8")
    worktree.joinpath(".gitignore").write_text(".worktrees/\nnode_modules/\n", encoding="utf-8")
    worktree.joinpath("app.py").write_text("needle in worktree\n", encoding="utf-8")
    worktree.joinpath("node_modules").mkdir()
    worktree.joinpath("node_modules", "lib.js").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(worktree), {"pattern": "needle"})

    assert get_success_content(result) == "app.py:1: needle in worktree"


def test_grep_never_surfaces_git_pointer_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath(".git").write_text("gitdir: ../elsewhere\n", encoding="utf-8")
    workspace.joinpath("app.py").write_text("gitdir mention\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "gitdir"})

    assert get_success_content(result) == "app.py:1: gitdir mention"


def test_grep_glob_filter_matches_case_insensitively(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("keep.py").write_text("needle\n", encoding="utf-8")
    workspace.joinpath("skip.txt").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle", "glob": "*.PY"})

    data = assert_success_envelope(result)
    assert data["content"] == "keep.py:1: needle"


def test_grep_glob_filter_expands_brace_alternations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # rg --glob semantics: '{py,js}' is an alternation, not a literal name —
    # the fnmatch-based fallback must expand it instead of matching nothing.
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("keep.py").write_text("needle\n", encoding="utf-8")
    workspace.joinpath("also.js").write_text("needle\n", encoding="utf-8")
    workspace.joinpath("skip.txt").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle", "glob": "**/*.{py,js}"})

    data = assert_success_envelope(result)
    assert data["content"] == "also.js:1: needle\nkeep.py:1: needle"


def test_grep_bare_name_brace_filter_matches_at_any_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("src").mkdir()
    workspace.joinpath("src", "app.py").write_text("needle\n", encoding="utf-8")
    workspace.joinpath("src", "app.ts").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle", "glob": "*.{py,ts}"})

    data = assert_success_envelope(result)
    assert data["content"] == "src/app.py:1: needle\nsrc/app.ts:1: needle"


def test_grep_glob_filter_negation_excludes_matching_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # rg --glob semantics: a leading '!' turns the filter into an exclusion,
    # so '!*.py' keeps every file that is not a .py file. The fnmatch-based
    # fallback must not treat the '!' as a literal pattern character (which
    # would silently match nothing).
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("keep.py").write_text("needle\n", encoding="utf-8")
    workspace.joinpath("skip.txt").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle", "glob": "!*.py"})

    data = assert_success_envelope(result)
    assert data["content"] == "skip.txt:1: needle"


def test_grep_glob_filter_character_class_braces_stay_literal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # rg --glob semantics: braces inside a character class are literal class
    # members, never alternations. '*[{,}]*' must match files whose name
    # contains '{', ',' or '}', not expand into the empty class '*[]*' and
    # silently match nothing.
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("a{b,c}.txt").write_text("needle\n", encoding="utf-8")
    workspace.joinpath("plain.txt").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle", "glob": "*[{,}]*"})

    data = assert_success_envelope(result)
    assert data["content"] == "a{b,c}.txt:1: needle"


def test_grep_glob_filter_directory_exclusion_removes_subtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # rg --glob semantics: '!build/' names the directory, so it excludes the
    # whole subtree; a file *named* 'build' is not a directory and stays.
    force_python_fallback(monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    workspace.joinpath("build").mkdir()
    workspace.joinpath("build", "x.txt").write_text("needle\n", encoding="utf-8")
    workspace.joinpath("top.txt").write_text("needle\n", encoding="utf-8")
    workspace.joinpath("build2").write_text("needle\n", encoding="utf-8")

    result = grep_handler(make_context(workspace), {"pattern": "needle", "glob": "!build/"})

    data = assert_success_envelope(result)
    assert data["content"] == "build2:1: needle\ntop.txt:1: needle"


def test_grep_glob_filter_directory_matching_semantics() -> None:
    # rg --glob semantics for directory candidates: a trailing-slash glob
    # names a directory only (a file with that name stays), while a bare-name
    # exclusion also prunes directories of that name and their subtree.
    assert not search_module.file_filter_matches("build/x.txt", "!build/")
    assert search_module.file_filter_matches("build", "!build/")
    assert not search_module.file_filter_matches("build/x.txt", "!build")
    assert not search_module.file_filter_matches("a/build/x.txt", "!build")
    assert search_module.file_filter_matches("build2/x.txt", "!build")
    assert not search_module.file_filter_matches("src/generated/x.txt", "!src/generated")
    assert search_module.file_filter_matches("src/other.txt", "!src/generated")
    # Positive filters still match file names only — never directories.
    assert not search_module.file_filter_matches("build/x.txt", "build")
    assert search_module.file_filter_matches("build", "build")
