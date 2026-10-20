"""The layered environment of shell commands and Terminal Sessions."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import core.tools.shell_environment as shell_environment
from core.tools.scratch_areas import ScratchFolder
from core.tools.shell_environment import (
    RunIdentity,
    command_environment,
    terminal_environment,
    unattended_command_environment,
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
    monkeypatch.setattr(shell_environment, "_instance_command_directory", None)
    for name in ("VBOT_DATA_DIR", "VBOT_SERVER_PORT", "VBOT_INSTALL_ROOT"):
        monkeypatch.delenv(name, raising=False)
    return base


def test_command_layers_apply_in_order_and_the_run_identity_wins(
    login_base: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VBOT_DATA_DIR", "/data/dev")
    own = tmp_path / "scratch" / "session-1"
    scratch = ScratchFolder(
        own=own, area=own.parent, tmp=own / "tmp", recreated=True, incarnation="first"
    )
    scratch.tmp.mkdir(parents=True)
    layers = command_environment(
        RunIdentity("agent-1", "session-1", None, update_handoff="token-1"),
        variables={
            "PAGER": "more",
            "VBOT_RUN_AGENT_ID": "someone-else",
            "MODE": "ci",
            "TMP": "elsewhere",
            "VBOT_SCRATCH_DIR": "elsewhere",
        },
        credentials={"API_TOKEN": "secret"},
        scratch=scratch,
    )
    environment = layers.complete()

    assert environment["HOME"] == "/home/user"
    assert environment["VBOT_DATA_DIR"] == "/data/dev"
    # A command writes to a pipe: no terminal, no colors, Python output line by line.
    assert environment["TERM"] == "dumb"
    assert environment["NO_COLOR"] == "1"
    assert environment["PYTHONUNBUFFERED"] == "1"
    # Unattended defaults replace the login's, and the Agent's variables replace those.
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GIT_PAGER"] == environment["GH_PAGER"] == "cat"
    assert environment["PYTHONIOENCODING"] == "utf-8"
    assert environment["PYTHONUTF8"] == "1"
    assert environment["PAGER"] == "more"
    assert (environment["MODE"], environment["API_TOKEN"]) == ("ci", "secret")
    assert environment["VBOT_RUN_AGENT_ID"] == "agent-1"
    assert environment["VBOT_RUN_SESSION_ID"] == "session-1"
    assert "VBOT_RUN_PROJECT_ID" not in environment
    assert environment["VBOT_UPDATE_HANDOFF"] == "token-1"
    assert "VBOT_SERVER_PORT" not in environment
    assert environment["VBOT_SCRATCH_DIR"] == str(scratch.own)
    for name in ("TEMP", "TMP", "TMPDIR"):
        assert environment[name] == str(scratch.tmp)
        assert layers.changes[name] == str(scratch.tmp)
        assert Path(environment[name]).is_dir()
    # The shell starts with the layers every call shares; the call's own come on top.
    other = command_environment(RunIdentity("agent-2", "session-2", "project-2", "token-2"))
    assert layers.base == other.base
    assert layers.base["PAGER"] == "cat"
    # A command vBot runs outside a Run starts with these layers and nothing of a Run.
    unattended = unattended_command_environment()
    assert (unattended.base, unattended.changes) == (layers.base, {})
    assert "VBOT_SCRATCH_DIR" not in other.complete()
    assert "TMP" not in other.complete()
    per_call = {"MODE", "API_TOKEN", "VBOT_RUN_AGENT_ID", "VBOT_RUN_PROJECT_ID"}
    assert not (per_call | {"VBOT_RUN_SESSION_ID", "VBOT_UPDATE_HANDOFF"}) & layers.base.keys()
    # Python's UTF-8 mode gives way to the user's own setting.
    login_base["PYTHONUTF8"] = "0"
    assert command_environment(RunIdentity("agent-1", "session-1", None)).base["PYTHONUTF8"] == "0"


def test_terminal_environment_has_no_command_layers(login_base: dict[str, str]) -> None:
    environment = terminal_environment({"EXTRA": "1"})

    assert environment["PAGER"] == "less"
    assert environment["TERM"] == "xterm-256color"
    assert environment["EXTRA"] == "1"
    assert not {"GIT_EDITOR", "NO_COLOR", "PYTHONUNBUFFERED", "PYTHONUTF8"} & environment.keys()


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


def test_checkout_server_leads_shell_path_with_a_vbot_for_itself(
    login_base: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "cli").mkdir()
    # The fake CLI reports what the real one would act on: its arguments and target.
    (checkout / "cli" / "main.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps([sys.argv[1:], os.environ['VBOT_DATA_DIR'], "
        "os.environ['VBOT_SERVER_PORT']]))\n",
        encoding="utf-8",
    )
    data_dir = tmp_path / "data dir's"
    monkeypatch.setenv("VBOT_INSTALL_ROOT", str(tmp_path / "installed"))
    assert (
        shell_environment.provide_instance_command(
            tmp_path / "unused", data_dir, 22333, checkout=checkout
        )
        is None
    )
    monkeypatch.delenv("VBOT_INSTALL_ROOT")

    directory = shell_environment.provide_instance_command(
        data_dir / "bin", data_dir, 22333, checkout=checkout
    )

    assert directory == data_dir / "bin"
    environment = terminal_environment()
    assert environment["PATH"] == os.pathsep.join([str(directory), "/usr/bin"])
    arguments = ["agent", "update", "a", "--name", "Mi ra", '{"mode":"selected"}']
    shell_path = os.pathsep.join([str(directory), os.environ.get("PATH", "")])
    if sys.platform == "win32":
        pwsh = shutil.which("pwsh")
        if pwsh is None:
            pytest.skip("PowerShell 7 is not installed")
        quoted = " ".join("'" + argument.replace("'", "''") + "'" for argument in arguments)
        # The previous values come back in the calling shell after the command.
        argv = [
            pwsh,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"vbot {quoted}; $env:VBOT_DATA_DIR",
        ]
    else:
        argv = ["sh", "-c", 'vbot "$@"', "sh", *arguments]
    output = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={**os.environ, "PATH": shell_path},
    ).stdout.splitlines()

    assert json.loads(output[0]) == [arguments, str(data_dir), "22333"]
    assert output[1:] in ([], [""])


@pytest.mark.skipif(sys.platform != "win32", reason="Windows login environment")
def test_windows_base_is_a_fresh_login_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VBOT_SERVER_ONLY_VARIABLE", "1")

    environment = command_environment(
        RunIdentity("a", "s", None), variables={"path": r"C:\x"}
    ).complete()

    assert "VBOT_SERVER_ONLY_VARIABLE" not in environment
    assert "SYSTEMROOT" in environment
    # Names are case-insensitive: the Agent's ``path`` replaces PATH.
    assert [name for name in environment if name.upper() == "PATH"] == ["PATH"]
    assert environment["PATH"] == r"C:\x"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_uses_prepared_messages_and_fails_with_the_fix_where_it_needs_text(
    tmp_path: Path,
) -> None:
    environment = command_environment(RunIdentity("a", "s", None)).complete()
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
    assert (
        "No editor is available for this command: pass the message as an argument, "
        'for example git commit -m "Fix the parser".'
    ) in empty.stderr

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
    assert "No editor is available for the git rebase -i todo list." in todo.stderr
    assert "GIT_SEQUENCE_EDITOR" in todo.stderr
    assert git("rebase", "-i", "HEAD~1", GIT_SEQUENCE_EDITOR=":").returncode == 0
