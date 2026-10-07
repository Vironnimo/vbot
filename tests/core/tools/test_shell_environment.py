"""The layered environment of shell commands and Terminal Sessions."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import core.tools.shell_environment as shell_environment
from core.tools.shell_environment import (
    RunIdentity,
    command_environment,
    terminal_environment,
)


@pytest.fixture
def login_base(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "PAGER": "less",
        "VBOT_RUN_PROJECT_ID": "inherited-project",
        "VBOT_UPDATE_HANDOFF": "stale-token",
    }
    monkeypatch.setattr(shell_environment, "_login_environment", lambda: dict(base))
    for name in ("VBOT_DATA_DIR", "VBOT_SERVER_PORT", "VBOT_INSTALL_ROOT"):
        monkeypatch.delenv(name, raising=False)
    return base


def test_command_layers_apply_in_order_and_the_run_identity_wins(
    login_base: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VBOT_DATA_DIR", "/data/dev")
    environment = command_environment(
        RunIdentity("agent-1", "session-1", None, update_handoff="token-1"),
        variables={"PAGER": "more", "VBOT_RUN_AGENT_ID": "someone-else", "MODE": "ci"},
        credentials={"API_TOKEN": "secret"},
    )

    assert environment["HOME"] == "/home/user"
    assert environment["VBOT_DATA_DIR"] == "/data/dev"
    assert environment["TERM"] == "xterm-256color"
    # Unattended defaults replace the login's, and the Agent's variables replace those.
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_PAGER"] == "cat"
    assert environment["PYTHONIOENCODING"] == "utf-8"
    assert environment["PAGER"] == "more"
    assert (environment["MODE"], environment["API_TOKEN"]) == ("ci", "secret")
    assert environment["VBOT_RUN_AGENT_ID"] == "agent-1"
    assert environment["VBOT_RUN_SESSION_ID"] == "session-1"
    assert "VBOT_RUN_PROJECT_ID" not in environment
    assert environment["VBOT_UPDATE_HANDOFF"] == "token-1"
    assert "VBOT_SERVER_PORT" not in environment


def test_terminal_environment_has_no_command_layers(login_base: dict[str, str]) -> None:
    environment = terminal_environment({"EXTRA": "1"})

    assert environment["PAGER"] == "less"
    assert environment["TERM"] == "xterm-256color"
    assert environment["EXTRA"] == "1"
    assert "GIT_EDITOR" not in environment


def test_server_environment_base_drops_what_vbot_added(monkeypatch: pytest.MonkeyPatch) -> None:
    venv = Path(sys.prefix)
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    server = {
        "PATH": os.pathsep.join([str(scripts), "/usr/bin"]),
        "VIRTUAL_ENV": str(venv),
        "PYTHONPATH": "/src",
        "VBOT_RUN_AGENT_ID": "server-agent",
        "LANG": "C.UTF-8",
    }
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "base_prefix", str(venv.parent / "base-python"))
    monkeypatch.setattr(os, "environ", server)

    environment = terminal_environment()

    assert environment["PATH"] == "/usr/bin"
    assert environment["LANG"] == "C.UTF-8"
    assert not {"VIRTUAL_ENV", "PYTHONPATH", "VBOT_RUN_AGENT_ID"} & environment.keys()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows login environment")
def test_windows_base_is_a_fresh_login_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VBOT_SERVER_ONLY_VARIABLE", "1")

    environment = command_environment(RunIdentity("a", "s", None), variables={"path": r"C:\x"})

    assert "VBOT_SERVER_ONLY_VARIABLE" not in environment
    assert "SYSTEMROOT" in environment
    # Names are case-insensitive: the Agent's ``path`` replaces PATH.
    assert [name for name in environment if name.upper() == "PATH"] == ["PATH"]
    assert environment["PATH"] == r"C:\x"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_uses_prepared_messages_and_fails_with_the_fix_where_it_needs_text(
    tmp_path: Path,
) -> None:
    environment = command_environment(RunIdentity("a", "s", None))
    environment["PATH"] = os.environ["PATH"]
    # The repository's own identity; nothing outside it is configured.
    identity = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    environment.update(identity)

    def git(*arguments: str, **variables: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=tmp_path,
            env={**environment, **variables},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def commit_file(text: str, message: str) -> None:
        (tmp_path / "file.txt").write_text(text, encoding="utf-8")
        git("add", "file.txt")
        assert git("commit", "-q", "-m", message).returncode == 0

    git("init", "-q", "-b", "main")
    commit_file("base\n", "base")
    git("checkout", "-q", "-b", "side")
    commit_file("side\n", "side change")
    git("checkout", "-q", "main")
    commit_file("main\n", "main change")

    # Without -m, the message is empty: git fails at once and the error names the fix.
    empty = git("commit", "--allow-empty")
    assert empty.returncode != 0
    assert "pass the message as an argument, for example git commit -m" in empty.stderr

    # A message git prepared is used as is: rebase --continue has no flag to skip it.
    git("checkout", "-q", "side")
    assert git("rebase", "main").returncode != 0
    (tmp_path / "file.txt").write_text("resolved\n", encoding="utf-8")
    git("add", "file.txt")
    resumed = git("rebase", "--continue")
    assert resumed.returncode == 0, resumed.stderr
    assert git("log", "-1", "--format=%s").stdout.strip() == "side change"

    # A rebase todo list needs a real edit; the error names the assignment that runs it as is.
    todo = git("rebase", "-i", "HEAD~1")
    assert todo.returncode != 0
    assert "GIT_SEQUENCE_EDITOR" in todo.stderr
    assert git("rebase", "-i", "HEAD~1", GIT_SEQUENCE_EDITOR=":").returncode == 0
