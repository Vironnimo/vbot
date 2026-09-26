"""Shell Tool calls written in other harnesses' argument shapes, and the env object."""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

import core.tools._shell_arguments as shell_arguments
import core.tools.bash as bash_module
from core.tools._shell_arguments import (
    normalize_shell_arguments,
    resolve_timeout,
    shell_display_parts,
    split_env_object,
)
from core.tools.bash import register_bash_tool
from core.tools.model_names import SHELL_MODEL_NAME
from core.tools.process_manager import ProcessManager
from core.tools.tools import ToolContext, ToolRegistry
from tests.core.tools.bash_helpers import (
    AGENT_ID,
    make_context,
    python_command,
)
from tests.core.tools.bash_helpers import (
    manager as manager,
)
from tests.core.tools.bash_helpers import (
    shell_env_cache as shell_env_cache,
)

NOT_RUN = f"{SHELL_MODEL_NAME} was not run: "


async def _dispatch(
    manager: ProcessManager,
    context: ToolContext,
    arguments: dict[str, Any],
    *,
    credential_resolver: Any = None,
) -> dict[str, Any]:
    registry = ToolRegistry()
    register_bash_tool(registry, manager, credential_resolver=credential_resolver)
    return await registry.dispatch(context, arguments)


def _refuse_credentials(key: str) -> str:
    pytest.fail(f"credential {key} must not be resolved")


# --- Argument shapes -------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"cmd": "ls"}, {"command": "ls"}),
        ({"script": "ls", "cwd": "src"}, {"command": "ls", "workdir": "src"}),
        ({"command": "ls", "directory": "src"}, {"command": "ls", "workdir": "src"}),
        ({"command": "ls", "working_directory": "src"}, {"command": "ls", "workdir": "src"}),
        ({"commands": ["cd src", "", "ls"]}, {"command": "cd src\nls"}),
        ({"command": ["ls"]}, {"command": "ls"}),
        ({"command": "ls", "timeout_ms": 5000}, {"command": "ls", "timeout_ms": 5000}),
        ({"command": "ls", "timeout_seconds": 5}, {"command": "ls", "timeout": 5}),
        ({"command": "ls", "mode": "auto"}, {"command": "ls", "mode": "foreground"}),
        ({"command": "ls", "mode": "bg"}, {"command": "ls", "mode": "background"}),
        ({"command": "ls", "run_in_background": True}, {"command": "ls", "mode": "background"}),
        ({"command": "ls", "background": "true"}, {"command": "ls", "mode": "background"}),
        ({"command": "ls", "is_background": False}, {"command": "ls", "mode": "foreground"}),
        (
            {"command": "ls", "background": True, "mode": "background"},
            {"command": "ls", "mode": "background"},
        ),
        ({"command": "ls", "yieldMs": 10000}, {"command": "ls"}),
        ({"command": "ls", "background_after_seconds": 0}, {"command": "ls", "mode": "background"}),
        (
            {"command": "ls", "explanation": "List files"},
            {"command": "ls", "description": "List files"},
        ),
        (
            {"command": "ls", "description": "Mine", "justification": "Other"},
            {"command": "ls", "description": "Mine"},
        ),
        (
            {"command": "ls", "title": "Check working directory"},
            {"command": "ls", "description": "Check working directory"},
        ),
        ({"command": "ls", "label": "List"}, {"command": "ls", "description": "List"}),
        ({"command": "ls", "summary": "List"}, {"command": "ls", "description": "List"}),
        (
            {
                "command": "ls",
                "login": True,
                "max_output_tokens": 1000,
                "notifyOnComplete": True,
                "dangerouslyDisableSandbox": True,
                "pty": False,
                "sandbox_permissions": "use_default",
                "require_user_approval": False,
                "user": "",
            },
            {"command": "ls"},
        ),
        ({"command": "ls", "env_keys": []}, {"command": "ls"}),
        ({"command": "ls", "env_keys": "TOKEN_A"}, {"command": "ls", "env_keys": ["TOKEN_A"]}),
        (
            # OpenAI local_shell wraps the call in an action object.
            {
                "action": {
                    "type": "exec",
                    "command": ["ls"],
                    "working_directory": "src",
                    "timeout_ms": 5000,
                    "env": {"DEBUG": "1"},
                }
            },
            {"command": "ls", "workdir": "src", "timeout_ms": 5000, "env": {"DEBUG": "1"}},
        ),
    ],
)
def test_other_harness_shapes_map_onto_the_shell_fields(arguments, expected) -> None:
    assert normalize_shell_arguments(arguments) == expected


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"command": "ls", "background": True, "mode": "foreground"},
            'mode is "foreground" but background asks for background; send one of them.',
        ),
        (
            {"command": "ls", "is_background": False, "mode": "background"},
            'mode is "background" but is_background asks for foreground; send one of them.',
        ),
        ({"command": "ls", "background": "maybe"}, "background must be true or false."),
        ({"command": "ls", "commands": ["pwd"]}, "command and commands are both given; send one."),
        (
            {"command": "ls", "action": {"command": ["pwd"]}},
            "command is given twice with different values.",
        ),
        (
            {"command": "vim", "pty": True},
            "it has no terminal, so pty: true cannot be honored. Run interactive programs "
            "with the terminal Tool if you have it; otherwise call again without pty.",
        ),
        (
            {"command": "ls", "elevated": True},
            "it cannot raise permissions. Call again without elevated; if the command needs "
            "elevated rights, ask the user to run it.",
        ),
        (
            {"command": "ls", "sandbox_permissions": "require_escalated"},
            "it cannot raise permissions. Call again without sandbox_permissions;",
        ),
        (
            {"command": "ls", "require_user_approval": True},
            "it cannot ask the user for approval. Ask the user in your reply first, then "
            "call again without require_user_approval.",
        ),
        (
            {"command": "ls", "user": "root"},
            "commands run as the vBot user; call again without user.",
        ),
    ],
)
def test_calls_asking_for_a_different_effect_fail_with_the_fix(arguments, message) -> None:
    with pytest.raises(ValueError) as raised:
        normalize_shell_arguments(arguments)
    assert str(raised.value).startswith(NOT_RUN + message)


@pytest.mark.parametrize(
    ("platform", "argv", "command"),
    [
        # Codex runs every command through a login shell; the script is the command.
        ("linux", ["bash", "-lc", "ls -la | head -5"], "ls -la | head -5"),
        ("linux", ["/bin/bash", "-c", "echo $HOME"], "echo $HOME"),
        ("linux", ["ls", "-la", "my dir"], "ls -la 'my dir'"),
        ("linux", ["bash", "script.sh"], "bash script.sh"),
        ("win32", ["pwsh", "-NoProfile", "-Command", "Get-Date"], "Get-Date"),
        ("win32", ["pwsh.exe", "-c", "Get-Date | Out-String"], "Get-Date | Out-String"),
        # Another shell is a program the Agent asked for, not the host shell.
        ("win32", ["bash", "-lc", "ls -la"], "bash -lc 'ls -la'"),
        ("win32", ["powershell", "-Command", "Get-Date"], "powershell -Command Get-Date"),
        ("win32", ["python", "-c", "print(1)"], "python -c 'print(1)'"),
        ("win32", ["git", "log", "-n", "3", "--oneline"], "git log -n '3' --oneline"),
        ("win32", ["echo", "0x10", "it's"], "echo '0x10' 'it''s'"),
        (
            "win32",
            ["C:\\Program Files\\Tool\\tool.exe", "--flag"],
            "& 'C:\\Program Files\\Tool\\tool.exe' --flag",
        ),
        ("win32", ["./build.ps1", "-Release"], "./build.ps1 -Release"),
    ],
)
def test_argv_arrays_become_one_host_command_line(monkeypatch, platform, argv, command) -> None:
    monkeypatch.setattr(shell_arguments.sys, "platform", platform)
    assert normalize_shell_arguments({"command": argv}) == {"command": command}


@pytest.mark.asyncio
async def test_argv_array_reaches_the_program_argument_for_argument(
    manager: ProcessManager, tmp_path: Path
) -> None:
    arguments = ["0x10", "it's", "a b", "$HOME", 'say "hi"', "--flag=x"]
    result = await _dispatch(
        manager,
        make_context(tmp_path),
        {"command": [sys.executable, "-c", "import sys; print(sys.argv[1:])", *arguments]},
    )

    assert result["ok"] is True, result
    assert ast.literal_eval(result["data"]["output"].strip()) == arguments


# --- Timeouts --------------------------------------------------------------


@pytest.mark.parametrize(
    ("timeout", "timeout_ms", "seconds", "note"),
    [
        (None, None, None, None),
        (120, None, 120, None),
        (9000, None, 9000, None),
        (12345, None, 12345, None),
        (None, 5000, 5, None),
        (5, 5000, 5, None),
        (5000, 5000, 5, None),
        (
            120000,
            None,
            120,
            "timeout 120000 was read as milliseconds (120 s); timeout takes seconds.",
        ),
    ],
)
def test_timeout_is_read_in_seconds_unless_it_is_clearly_milliseconds(
    timeout, timeout_ms, seconds, note
) -> None:
    assert resolve_timeout(timeout, timeout_ms) == (seconds, note)


def test_disagreeing_timeouts_fail_before_running() -> None:
    with pytest.raises(ValueError) as raised:
        resolve_timeout(120, 5000)
    assert str(raised.value) == (
        NOT_RUN + "timeout (120 s) and timeout_ms (5000 ms) disagree; send one of them."
    )


@pytest.mark.asyncio
async def test_millisecond_timeout_runs_with_a_note(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    result = await _dispatch(
        manager, make_context(tmp_path), {"cmd": "print('done')", "timeout": 60000}
    )

    assert result["ok"] is True
    assert result["data"]["output"].strip() == "done"
    assert result["data"]["note"] == (
        "timeout 60000 was read as milliseconds (60 s); timeout takes seconds."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "depth", "offers_background"),
    [({"timeout_ms": 500}, 0, True), ({"timeout": 0.5}, 1, False)],
)
async def test_timeout_message_names_the_next_call(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: dict[str, Any],
    depth: int,
    offers_background: bool,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    result = await _dispatch(
        manager,
        make_context(tmp_path, nesting_depth=depth),
        {"command": "import time; time.sleep(30)", **arguments},
    )

    assert result["error"]["code"] == "process_timeout"
    message = result["error"]["message"]
    assert message.startswith(
        "The command was stopped when its 0.5 s timeout elapsed. Check the output before "
        "retrying. If it needs more time, call again with a larger timeout (seconds) or "
        "timeout: 0 for no limit"
    )
    assert ('mode: "background"' in message) is offers_background


def test_timeout_message_repeats_how_the_timeout_was_read() -> None:
    note = "timeout 60000 was read as milliseconds (60 s); timeout takes seconds."
    assert bash_module._timeout_message(60, [note], background=True).endswith(
        f'; start servers and other long-running commands with mode: "background". Note: {note}'
    )


# --- Working directory -----------------------------------------------------


@pytest.mark.asyncio
async def test_other_harness_workdir_spelling_runs_there(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    (tmp_path / "src").mkdir()

    result = await _dispatch(
        manager, make_context(tmp_path), {"cmd": "import os; print(os.getcwd())", "cwd": "src"}
    )

    assert Path(result["data"]["output"].strip()) == tmp_path / "src"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workdir", "expected"),
    [
        ("srcc", ["srcc does not exist.", "Similar directories: ", "src."]),
        ("notes.txt", ["notes.txt is a file, not a directory."]),
        ("missing/deeper", ["deeper does not exist.", "missing does not exist either."]),
    ],
)
async def test_missing_workdir_fails_before_spawn_with_nearby_directories(
    manager: ProcessManager, tmp_path: Path, workdir: str, expected: list[str]
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")

    result = await _dispatch(
        manager, make_context(tmp_path), {"command": "never runs", "workdir": workdir}
    )

    assert result["error"]["code"] == "invalid_arguments"
    message = result["error"]["message"]
    assert message.startswith(NOT_RUN + "workdir ")
    for text in expected:
        assert text in message
    assert manager.list_processes(AGENT_ID) == []


# --- env and env_keys ------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "granted", "variables", "credentials"),
    [
        (
            {"DEBUG": "1", "RETRIES": 3, "VERBOSE": True},
            (),
            {"DEBUG": "1", "RETRIES": "3", "VERBOSE": "true"},
            [],
        ),
        ({"API_TOKEN": "$API_TOKEN"}, ("API_TOKEN",), {}, ["API_TOKEN"]),
        ({"API_TOKEN": "${env:API_TOKEN}"}, ("API_TOKEN",), {}, ["API_TOKEN"]),
        ({"API_TOKEN": ""}, ("API_TOKEN",), {}, ["API_TOKEN"]),
        # A reference to an ungranted variable keeps the value the command inherits.
        ({"HOME": "$HOME"}, (), {}, []),
        ({"KEYBOARD_LAYOUT": "de"}, (), {"KEYBOARD_LAYOUT": "de"}, []),
    ],
)
def test_env_object_splits_into_variables_and_granted_credentials(
    env, granted, variables, credentials
) -> None:
    assert split_env_object(env, frozenset(granted)) == (variables, credentials)


@pytest.mark.parametrize(
    ("env", "granted", "message"),
    [
        (
            {"API_TOKEN": "abc123secret"},
            ("API_TOKEN",),
            "env sets API_TOKEN to a value written in the call, and vBot does not pass "
            "credential values written in Tool calls. API_TOKEN is a granted credential: "
            'pass env_keys: ["API_TOKEN"] and leave it out of env.',
        ),
        (
            {"OPENAI_API_KEY": "abc123secret"},
            ("API_TOKEN", "GH_TOKEN"),
            "env sets OPENAI_API_KEY to a value written in the call, and vBot does not pass "
            "credential values written in Tool calls. Granted credentials for env_keys: "
            "API_TOKEN, GH_TOKEN. If OPENAI_API_KEY is not a secret, set it inside the "
            "command instead.",
        ),
        (
            {"db-password": "abc123secret"},
            (),
            "env sets db-password to a value written in the call, and vBot does not pass "
            "credential values written in Tool calls. No credentials are granted to this "
            "Agent; ask the user if the command needs one. If db-password is not a secret, "
            "set it inside the command instead.",
        ),
        ({"VBOT_RUN_AGENT_ID": "other"}, (), "env cannot set VBOT_RUN_AGENT_ID;"),
        ({"A=B": "1"}, (), "env has an invalid variable name: 'A=B'."),
        ({"DEBUG": {"level": 1}}, (), "env value for DEBUG must be a string."),
    ],
)
def test_env_object_refuses_written_credentials_and_reserved_names(env, granted, message) -> None:
    with pytest.raises(ValueError) as raised:
        split_env_object(env, frozenset(granted))
    assert str(raised.value).startswith(NOT_RUN + message)
    assert "abc123secret" not in str(raised.value)


@pytest.mark.asyncio
async def test_env_variable_reaches_only_the_command(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    result = await _dispatch(
        manager,
        make_context(tmp_path),
        {"command": "import os; print(os.environ['SAFE_VALUE'])", "env": {"SAFE_VALUE": "on"}},
        credential_resolver=_refuse_credentials,
    )

    assert result["data"]["output"].strip() == "on"
    assert "SAFE_VALUE" not in bash_module.os.environ


@pytest.mark.asyncio
async def test_env_reference_to_a_granted_credential_resolves_it(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, tool_settings={"bash": {"allowed_env": ["TEST_API_TOKEN"]}})

    result = await _dispatch(
        manager,
        context,
        {
            "command": "import os; print(os.environ['TEST_API_TOKEN'])",
            "env": {"TEST_API_TOKEN": "$TEST_API_TOKEN"},
        },
        credential_resolver=lambda key: f"resolved-{key}",
    )

    assert result["data"]["output"].strip() == "resolved-TEST_API_TOKEN"


@pytest.mark.asyncio
async def test_env_with_a_written_secret_fails_without_echoing_it(
    manager: ProcessManager, tmp_path: Path
) -> None:
    result = await _dispatch(
        manager,
        make_context(tmp_path),
        {"command": "never runs", "env": {"OPENAI_API_KEY": "sk-abc123secret"}},
        credential_resolver=_refuse_credentials,
    )

    assert result["error"]["code"] == "invalid_arguments"
    assert "OPENAI_API_KEY" in result["error"]["message"]
    assert "abc123secret" not in str(result)
    assert manager.list_processes(AGENT_ID) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allowed", "message"),
    [
        (
            ["TEST_API_TOKEN"],
            "env_keys names OPENAI_API_KEY, which is not granted to this Agent. Granted "
            "credentials: TEST_API_TOKEN. Call again with only those names, or omit env_keys.",
        ),
        (
            [],
            "env_keys names OPENAI_API_KEY, which is not granted to this Agent, and no "
            "credentials are granted. Omit env_keys; if the command needs a credential, ask "
            "the user to grant it.",
        ),
    ],
)
async def test_ungranted_env_key_names_the_granted_ones(
    manager: ProcessManager, tmp_path: Path, allowed: list[str], message: str
) -> None:
    context = make_context(tmp_path, tool_settings={"bash": {"allowed_env": allowed}})

    result = await _dispatch(
        manager,
        context,
        {"command": "never runs", "env_keys": ["OPENAI_API_KEY"]},
        credential_resolver=_refuse_credentials,
    )

    assert result["error"]["message"] == NOT_RUN + message
    assert manager.list_processes(AGENT_ID) == []


@pytest.mark.asyncio
async def test_env_key_for_an_inherited_variable_runs_with_a_note(
    manager: ProcessManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    result = await _dispatch(
        manager,
        make_context(tmp_path),
        {"command": "import os; print(os.environ['PATH'])", "env_keys": ["PATH"]},
        credential_resolver=_refuse_credentials,
    )

    assert result["data"]["output"].strip() == "original-path"
    assert result["data"]["note"] == (
        "PATH is not a granted credential, so the command sees the value it inherits; "
        "env_keys is only for granted credentials."
    )


# --- Display ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "kind", "value"),
    [
        ({"cmd": "git status"}, "command", "git status"),
        ({"command": "ls", "explanation": "List files"}, "description", "List files"),
        ({"action": {"command": ["ls"]}}, "command", "ls"),
        # A call that fails validation still shows what it tried to run.
        ({"command": "vim", "pty": True}, "command", "vim"),
    ],
)
def test_activity_row_shows_the_command_in_any_shape(arguments, kind, value) -> None:
    (part,) = shell_display_parts(arguments)
    assert (part.kind, part.value) == (kind, value)
