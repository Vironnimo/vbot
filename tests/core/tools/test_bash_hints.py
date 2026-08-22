"""Tests for the bash output-pattern failure hints."""

from __future__ import annotations

from core.tools.bash_hints import annotate_failure


def test_success_never_annotated() -> None:
    assert annotate_failure("echo hi", 0, "hi") is None


def test_unknown_failure_returns_none() -> None:
    assert annotate_failure("true", 7, "some arbitrary failure text") is None


def test_bare_python_not_found_gets_specific_hint() -> None:
    hint = annotate_failure("python setup.py", 127, "bash: python: command not found")
    assert hint is not None
    assert "python3" in hint


def test_bare_pip_not_found_gets_specific_hint() -> None:
    hint = annotate_failure("pip install x", 127, "sh: pip: command not found")
    assert hint is not None
    assert "pip3" in hint


def test_other_command_not_found_names_the_binary() -> None:
    hint = annotate_failure("make build", 127, "bash: line 1: make: command not found")
    assert hint is not None
    assert "`make`" in hint
    assert "`which make`" in hint


def test_powershell_command_not_found_hint() -> None:
    hint = annotate_failure(
        "cargo build",
        1,
        "The term 'cargo' is not recognized as a name of a cmdlet, "
        "function, script file, or executable program.",
    )
    assert hint is not None
    assert "`cargo`" in hint
    assert "Get-Command cargo" in hint


def test_module_not_found_suggests_venv() -> None:
    hint = annotate_failure(
        "python -m flask run",
        1,
        "Traceback (most recent call last):\nModuleNotFoundError: No module named 'flask'",
    )
    assert hint is not None
    assert "flask" in hint
    assert "venv" in hint


def test_merge_conflict_hint_says_do_not_retry() -> None:
    hint = annotate_failure(
        "git merge feature",
        1,
        "CONFLICT (content): Merge conflict in src/a.py\n"
        "Automatic merge failed; fix conflicts and then commit the result.",
    )
    assert hint is not None
    assert "Do not retry" in hint
    assert "git add" in hint


def test_already_exists_hint() -> None:
    hint = annotate_failure(
        "git branch feature", 1, "fatal: a branch named 'feature' already exists"
    )
    assert hint is not None
    assert "'feature' already exists" in hint


def test_port_in_use_hint_with_port() -> None:
    hint = annotate_failure(
        "python -m http.server 8000", 1, "OSError: [Errno 98] Address already in use"
    )
    assert hint is not None
    assert "port" in hint


def test_port_in_use_hint_named_port() -> None:
    hint = annotate_failure(
        "uvicorn app:app",
        1,
        "ERROR: [Errno 98] error while attempting to bind on address ('127.0.0.1', 8421)",
    )
    assert hint is not None
    assert "8421" in hint


def test_permission_denied_hint() -> None:
    hint = annotate_failure("./deploy.sh", 1, "bash: ./deploy.sh: Permission denied")
    assert hint is not None
    assert "Permission denied" in hint


def test_rate_limit_hint() -> None:
    hint = annotate_failure("git push", 1, "error: API rate limit exceeded for user")
    assert hint is not None
    assert "rate limit" in hint.lower()


def test_gh_unknown_json_field_hint() -> None:
    hint = annotate_failure(
        "gh pr list --json bogus",
        1,
        'gh: Unknown JSON field: "bogus"\nValid fields are: author, body',
    )
    assert hint is not None
    assert "bogus" in hint


def test_first_match_wins() -> None:
    output = "bash: python: command not found\nfatal: a branch named 'x' already exists"
    hint = annotate_failure("python", 1, output)
    assert hint is not None
    assert "python3" in hint


def test_exit_126_hint() -> None:
    hint = annotate_failure("run.sh", 126, "")
    assert hint is not None
    assert "chmod +x" in hint


def test_exit_137_hint() -> None:
    hint = annotate_failure("stress", 137, "")
    assert hint is not None
    assert "SIGKILL" in hint


def test_exit_124_hint_mentions_background_mode() -> None:
    hint = annotate_failure("pytest", 124, "")
    assert hint is not None
    assert "background" in hint


def test_exit_code_hint_yields_to_output_pattern() -> None:
    hint = annotate_failure("python", 124, "bash: python: command not found")
    assert hint is not None
    assert "python3" in hint


def test_scan_window_is_bounded() -> None:
    output = "ok\n" * 5000 + "bash: python: command not found"
    hint = annotate_failure("python", 127, output)
    assert hint is None
