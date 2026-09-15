"""Bash defaults bound runtime without treating output as a heartbeat."""

import asyncio

import pytest

import core.tools.bash as bash_module
from core.tools.bash import bash_handler
from tests.core.tools.bash_helpers import AGENT_ID, make_context, python_command
from tests.core.tools.bash_helpers import manager as manager
from tests.core.tools.bash_helpers import shell_env_cache as shell_env_cache


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["foreground", "auto", "background"])
async def test_omitted_timeout_schedules_three_minutes_and_retires_after_exit(
    manager, tmp_path, monkeypatch, mode
):
    observed = []
    original = bash_module._schedule_timeout

    def schedule(manager, context, process_id, timeout):
        observed.append(timeout)
        return original(manager, context, process_id, timeout)

    monkeypatch.setattr(bash_module, "_schedule_timeout", schedule)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    result = await bash_handler(
        make_context(tmp_path), {"command": "print('done')", "mode": mode}, manager
    )
    assert result["ok"]
    assert observed == [180]
    tracked = manager.list_processes(AGENT_ID)[0]
    await asyncio.wait_for(asyncio.shield(tracked.wait_task), 5)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert tracked.status == "completed"
    assert not any(
        task.get_name() == f"bash-timeout:{tracked.process_id}" for task in asyncio.all_tasks()
    )
    for parameters in (
        bash_module.BASH_TOOL_PARAMETERS,
        bash_module.BASH_SUBAGENT_TOOL_PARAMETERS,
    ):
        assert parameters["properties"]["timeout"]["default"] == observed[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,depth", [("foreground", 0), ("foreground", 1), ("auto", 0), ("background", 0)]
)
@pytest.mark.parametrize("noisy", [False, True])
async def test_omitted_timeout_ends_silent_and_noisy_commands(
    manager, tmp_path, monkeypatch, mode, depth, noisy
):
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    monkeypatch.setattr(bash_module, "DEFAULT_TIMEOUT_SECONDS", 0.7)
    arguments = {
        "mode": mode,
        "command": (
            "import time\nwhile True:\n    print('working', flush=True)\n    time.sleep(0.02)"
            if noisy
            else "import time; time.sleep(30)"
        ),
    }
    if mode == "auto":
        arguments["background_after_seconds"] = 0
    result = await asyncio.wait_for(
        bash_handler(make_context(tmp_path, nesting_depth=depth), arguments, manager), 10
    )
    tracked = manager.list_processes(AGENT_ID)[0]
    await asyncio.wait_for(asyncio.shield(tracked.wait_task), 10)
    assert tracked.status == "killed"
    assert tracked.proc.returncode is not None
    if mode == "foreground":
        assert result["error"]["code"] == "process_timeout"
        if noisy:
            assert "working" in result["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout", [0, 5])
async def test_explicit_timeout_allows_silent_work_beyond_default(
    manager, tmp_path, monkeypatch, timeout
):
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    monkeypatch.setattr(bash_module, "DEFAULT_TIMEOUT_SECONDS", 0.01)
    result = await asyncio.wait_for(
        bash_handler(
            make_context(tmp_path),
            {"command": "import time; time.sleep(0.1); print('finished')", "timeout": timeout},
            manager,
        ),
        10,
    )
    assert result["ok"]
    assert result["data"]["status"] == "completed"
    assert "finished" in result["data"]["output"]


@pytest.mark.parametrize("timeout", [-1, float("inf"), float("nan"), "never"])
def test_invalid_timeout_does_not_disable_deadline(timeout):
    parsed = bash_module._parse_arguments({"command": "unused", "timeout": timeout})
    assert isinstance(parsed, str)
    assert "timeout" in parsed
