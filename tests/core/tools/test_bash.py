"""Bash: execution behavior."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import core.tools._bash_environment as bash_environment
import core.tools._bash_results as bash_results
import core.tools.bash as bash_module
from core.tools.bash import (
    bash_handler,
)
from core.tools.process_manager import ProcessManager
from core.utils.processes import subprocess_creation_flags
from tests.core.tools.bash_helpers import (
    AGENT_ID,
    RUN_ID,
    delivered_future,
    make_context,
    python_command,
)
from tests.core.tools.bash_helpers import (
    manager as manager,
)
from tests.core.tools.bash_helpers import (
    shell_env_cache as shell_env_cache,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["foreground", "auto"])
async def test_user_handoff_preserves_process_and_automatic_delivery(
    manager, tmp_path, monkeypatch, mode
):
    from core.runs import Run

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    run = Run(run_id=RUN_ID, agent_id=AGENT_ID, session_id="session-a")
    run.begin_tool_call("call-a")
    context = make_context(tmp_path)
    ready = asyncio.Event()
    delivered = asyncio.Event()
    notices = []
    handoffs = []
    original_handoff_note = bash_results._handoff_note

    def record_handoff(mode, elapsed, *, requested_by_user=False):
        handoffs.append((mode, elapsed, requested_by_user))
        return original_handoff_note(mode, elapsed, requested_by_user=requested_by_user)

    monkeypatch.setattr(bash_results, "_handoff_note", record_handoff)

    def register(callback):
        run.register_tool_background("call-a", callback)
        ready.set()

    class Trigger:
        def submit_completion(self, *args, **kwargs):
            notices.append(kwargs)
            delivered.set()
            return delivered_future()

    context = replace(context, background_registration_hook=register)
    task = asyncio.create_task(
        bash_handler(
            context,
            {
                "command": (
                    "from pathlib import Path\nimport time\nprint('ready', flush=True)\n"
                    "while not Path('release').exists():\n    time.sleep(0.01)\n"
                    "print('finished')"
                ),
                "mode": mode,
            },
            manager,
            trigger_service=Trigger(),
        )
    )
    await asyncio.wait_for(ready.wait(), 5)
    assert run.background_tool_call("call-a")
    result = await asyncio.wait_for(task, 5)
    assert result["ok"] and result["data"]["delivery"] == "automatic"
    assert len(handoffs) == 1
    assert handoffs[0][0] == mode
    assert handoffs[0][1] >= 0
    assert handoffs[0][2] is True
    process_id = result["data"]["process_id"]
    assert manager.get_process(process_id, AGENT_ID, project_id=None).status == "running"
    (tmp_path / "release").write_text("continue", encoding="utf-8")
    await asyncio.wait_for(delivered.wait(), 5)
    assert len(notices) == 1
    assert "finished" in notices[0]["body"]


@pytest.mark.asyncio
async def test_subagent_foreground_never_exposes_user_handoff(manager, tmp_path, monkeypatch):
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, nesting_depth=1)
    callbacks = []
    context = replace(context, background_registration_hook=callbacks.append)
    result = await bash_handler(context, {"command": "print('inline')"}, manager)
    assert result["data"]["status"] == "completed"
    assert callbacks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [None, "foreground", "auto", "background"])
async def test_bash_modes_finish_without_stdin_input(manager, tmp_path, monkeypatch, mode):
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    arguments = {"command": "import sys; assert sys.stdin.read() == ''; print('eof')"}
    if mode is not None:
        arguments["mode"] = mode
    result = await asyncio.wait_for(bash_handler(make_context(tmp_path), arguments, manager), 5)
    if mode == "background":
        tracked = manager.get_process(result["data"]["process_id"], AGENT_ID)
        assert tracked.wait_task is not None
        await asyncio.wait_for(asyncio.shield(tracked.wait_task), 5)
        data = await manager.snapshot(tracked.process_id, AGENT_ID)
    else:
        data = result["data"]
    assert result["ok"] is True
    assert data["exit_code"] == 0
    assert data["output"].strip() == "eof"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell-specific stdin contract")
@pytest.mark.parametrize(
    "command, expected",
    [
        ('Write-Output "x: $input"', "x:"),
        ('$input | ForEach-Object { $_ }; Write-Output "done"', "done"),
        ('[Console]::In.ReadToEnd(); Write-Output "done"', "done"),
        ("'alpha','beta' | ForEach-Object { $_.ToUpper() }", "ALPHA\nBETA"),
        (
            "'alpha','beta' | pwsh -NonInteractive -Command "
            "'$input | ForEach-Object { $_.ToUpper() }'",
            "ALPHA\nBETA",
        ),
    ],
)
async def test_windows_shell_eof_and_command_pipelines(manager, tmp_path, command, expected):
    result = await asyncio.wait_for(
        bash_handler(make_context(tmp_path), {"command": command}, manager), 10
    )
    assert result["ok"] is True
    assert result["data"]["exit_code"] == 0
    assert result["data"]["output"].strip().replace("\r\n", "\n") == expected


@pytest.mark.asyncio
async def test_shell_pipeline_and_script_owned_input_remain_available(manager, tmp_path):

    child = (
        "import sys\nvalue = 0\n"
        "for line in sys.stdin:\n"
        "    value += int(line)\n"
        "    print(value, flush=True)\n"
    )
    script = tmp_path / "input test.py"
    script.write_text(
        "import subprocess, sys\n"
        "assert sys.stdin.read().strip() == 'pipeline-input'\n"
        f"child = subprocess.Popen([sys.executable, '-u', '-c', {child!r}], "
        "stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, "
        f"creationflags={subprocess_creation_flags()})\n"
        "try:\n"
        "    child.stdin.write('3\\n'); child.stdin.flush()\n"
        "    observed = int(child.stdout.readline())\n"
        "    assert observed == 3\n"
        "    child.stdin.write(str(10 - observed) + '\\n'); child.stdin.flush()\n"
        "    assert child.stdout.readline().strip() == '10'\n"
        "    child.stdin.close()\n"
        "    assert child.wait(timeout=3) == 0\n"
        "    print('child-dialog-ok')\n"
        "finally:\n"
        "    if child.poll() is None:\n"
        "        child.kill(); child.wait()\n",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        executable = sys.executable.replace("'", "''")
        command = f"'pipeline-input' | & '{executable}' 'input test.py'"
    else:
        import shlex

        command = f"printf '%s' 'pipeline-input' | {shlex.quote(sys.executable)} 'input test.py'"
    result = await asyncio.wait_for(
        bash_handler(make_context(tmp_path), {"command": command, "timeout": 8}, manager), 10
    )
    assert result["ok"] is True
    assert result["data"]["exit_code"] == 0
    assert result["data"]["output"].strip() == "child-dialog-ok"


@pytest.mark.asyncio
async def test_short_command_completes_and_streams_stdout(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit_hook(event_type: str, payload: dict[str, Any]) -> None:
        events.append((event_type, payload))

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, emit_hook=emit_hook)

    result = await bash_handler(
        context,
        {"command": "print('hello')", "mode": "foreground"},
        manager,
    )

    assert result["ok"] is True
    assert result["data"]["status"] == "completed"
    assert result["data"]["exit_code"] == 0
    assert result["data"]["output"].replace("\r\n", "\n") == "hello\n"
    assert "stdout" not in result["data"]
    assert "stderr" not in result["data"]
    assert events == [
        (
            "tool_call_stdout",
            {
                "tool_call_id": "call-a",
                "process_id": events[0][1]["process_id"],
                "data": events[0][1]["data"],
            },
        )
    ]
    assert events[0][1]["data"].replace("\r\n", "\n") == "hello\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("grant_source", ["agent", "skill"])
async def test_granted_env_key_is_resolved_into_only_the_spawned_process(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    grant_source: str,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(
        tmp_path,
        tool_settings=(
            {"bash": {"allowed_env": ["TEST_API_TOKEN"]}} if grant_source == "agent" else None
        ),
        skill_env_keys=("TEST_API_TOKEN",) if grant_source == "skill" else (),
    )
    resolved: list[str] = []

    def resolve_credential(key: str) -> str:
        resolved.append(key)
        return "hidden-token"

    result = await bash_handler(
        context,
        {
            "command": "import os; print(os.environ['TEST_API_TOKEN'])",
            "mode": "foreground",
            "env_keys": ["TEST_API_TOKEN"],
        },
        manager,
        credential_resolver=resolve_credential,
    )

    assert result["ok"] is True
    assert result["data"]["output"].strip() == "hidden-token"
    assert resolved == ["TEST_API_TOKEN"]
    assert bash_environment._cached_shell_env == {"PATH": "original-path"}


@pytest.mark.asyncio
async def test_bash_injects_current_run_context(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    context = make_context(tmp_path, project_id="vbot")

    result = await bash_handler(
        context,
        {
            "command": (
                "import os; print(os.environ['VBOT_RUN_AGENT_ID']); "
                "print(os.environ['VBOT_RUN_SESSION_ID']); "
                "print(os.environ['VBOT_RUN_PROJECT_ID'])"
            ),
            "mode": "foreground",
        },
        manager,
    )

    assert result["data"]["output"].replace("\r\n", "\n") == "agent-a\nsession-a\nvbot\n"


@pytest.mark.asyncio
async def test_identity_bash_removes_host_project_context(
    manager: ProcessManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bash_environment,
        "_cached_shell_env",
        {"PATH": "original-path", "VBOT_RUN_PROJECT_ID": "host-value"},
    )
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    result = await bash_handler(
        make_context(tmp_path),
        {
            "command": "import os; print(os.environ.get('VBOT_RUN_PROJECT_ID', 'missing'))",
            "mode": "foreground",
        },
        manager,
    )

    assert result["data"]["output"].strip() == "missing"


@pytest.mark.asyncio
async def test_ungranted_env_key_is_rejected_before_spawn(
    manager: ProcessManager,
    tmp_path: Path,
) -> None:
    result = await bash_handler(
        make_context(tmp_path),
        {
            "command": "print('must not run')",
            "mode": "foreground",
            "env_keys": ["OPENAI_API_KEY"],
        },
        manager,
        credential_resolver=lambda _key: pytest.fail("credential must not be resolved"),
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert "OPENAI_API_KEY" in result["error"]["message"]
    assert manager.list_processes(AGENT_ID) == []
