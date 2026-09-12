"""Bash: output behavior."""

from __future__ import annotations

import asyncio
import types
from dataclasses import replace
from pathlib import Path

import pytest

import core.tools._bash_results as bash_results
import core.tools.bash as bash_module
from core.tools.bash import (
    bash_handler,
)
from tests.core.tools.bash_helpers import (
    AGENT_ID,
    delivered_future,
    kill_background,
    make_context,
    make_spool_manager,
    python_command,
)
from tests.core.tools.bash_helpers import (
    manager as manager,
)
from tests.core.tools.bash_helpers import (
    shell_env_cache as shell_env_cache,
)


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("has_log", [False, True])
def test_handoff_snapshot_keeps_twenty_newest_lines(tmp_path, newline, has_log):
    tracked = types.SimpleNamespace(
        log_file=tmp_path / "command.log" if has_log else None, truncated=False
    )
    lines = [f"line-{index}{newline}" for index in range(40)]
    fields = bash_results._shape_output_fields(tracked, "".join(lines), handoff=True)
    assert fields["truncated"] is True
    assert fields["output"].split("\n", 1)[1] == "".join(lines[-20:])
    assert len(fields["output"]) <= 4000
    if has_log:
        assert Path(fields["log_file"]) == tracked.log_file


@pytest.mark.parametrize("output", ["", "tiny\n", "x" * 4000, "x" * 5000, "x\n" * 20])
@pytest.mark.parametrize("already_truncated", [False, True])
def test_handoff_snapshot_character_budget_and_upstream_truncation(output, already_truncated):
    tracked = types.SimpleNamespace(log_file=None, truncated=already_truncated)
    fields = bash_results._shape_output_fields(tracked, output, handoff=True)
    truncated = already_truncated or len(output) > 4000
    assert fields["truncated"] is truncated
    assert len(fields["output"]) <= 4000
    if not truncated:
        assert fields["output"] == output
    else:
        tail = fields["output"].split("\n", 1)[1]
        assert output.endswith(tail)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["foreground", "auto", "background"])
async def test_every_handoff_mode_uses_snapshot(manager, tmp_path, monkeypatch, mode):
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    output = "".join(f"activity-{index}\n" for index in range(40))

    async def captured_output(*args):
        return output

    monkeypatch.setattr(bash_results, "_combined_output", captured_output)
    context = make_context(tmp_path)
    if mode == "foreground":
        context = replace(context, background_registration_hook=lambda callback: callback())
    arguments = {"command": "import sys; sys.stdin.readline()", "mode": mode}
    if mode == "auto":
        arguments["background_after_seconds"] = 0
    result = await bash_handler(context, arguments, manager)
    try:
        data = result["data"]
        assert data["status"] == "running"
        assert data["delivery"] == "automatic"
        assert data["truncated"] is True
        assert data["output"].split("\n", 1)[1] == "".join(output.splitlines(True)[-20:])
    finally:
        await kill_background(manager, result)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["foreground", "background"])
@pytest.mark.parametrize("exit_code", [0, 3])
@pytest.mark.parametrize("large_line", [False, True])
async def test_final_output_limits_preserve_exit_and_complete_log(
    tmp_path, monkeypatch, mode, exit_code, large_line
):
    manager = make_spool_manager(tmp_path)
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    output = "x" * 12000 if large_line else "\n".join(f"line-{i}" for i in range(200))
    delivered = asyncio.Event()
    notices = []

    class Trigger:
        def submit_completion(self, *args, **kwargs):
            notices.append(kwargs["body"])
            delivered.set()
            return delivered_future()

    try:
        result = await bash_handler(
            make_context(tmp_path),
            {"command": f"import sys; print({output!r}); sys.exit({exit_code})", "mode": mode},
            manager,
            trigger_service=Trigger(),
        )
        assert result["ok"] is True
        if mode == "background":
            await asyncio.wait_for(delivered.wait(), 5)
            assert len(notices) == 1
            assert f"Exit code: {exit_code}" in notices[0]
            tail = notices[0].split("Output:\n", 1)[1]
        else:
            assert result["data"]["exit_code"] == exit_code
            assert result["data"]["truncated"] is True
            tail = result["data"]["output"]
        assert len(tail) <= 8000
        if large_line:
            assert tail.rstrip().endswith("x" * 100)
        else:
            assert tail.splitlines()[1:] == [f"line-{i}" for i in range(100, 200)]
        log_file = Path(result["data"]["log_file"])
        assert log_file.read_text(encoding="utf-8") == output + "\n"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_output_cap_keeps_tail_and_names_log_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool_manager = make_spool_manager(tmp_path)
    try:
        monkeypatch.setattr(bash_module, "_shell_argv", python_command)
        context = make_context(tmp_path)

        result = await bash_handler(
            context,
            {
                "command": "print('a' * 9000 + 'END-MARKER')",
                "mode": "foreground",
            },
            spool_manager,
        )

        assert result["ok"] is True
        data = result["data"]
        assert data["truncated"] is True
        assert "END-MARKER" in data["output"]
        assert len(data["output"]) <= 8000
        assert "[earlier output truncated" in data["output"]
        assert data["output"].index("truncated") < data["output"].index("END-MARKER")

        log_file = Path(data["log_file"])
        content = log_file.read_text(encoding="utf-8")
        assert "a" * 9000 + "END-MARKER" in content, "log file must hold the uncut output"
    finally:
        await spool_manager.aclose()


@pytest.mark.asyncio
async def test_small_output_is_not_truncated_and_names_no_log_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool_manager = make_spool_manager(tmp_path)
    try:
        monkeypatch.setattr(bash_module, "_shell_argv", python_command)
        context = make_context(tmp_path)

        result = await bash_handler(
            context,
            {"command": "print('tiny')", "mode": "foreground"},
            spool_manager,
        )

        assert result["ok"] is True
        data = result["data"]
        assert data["truncated"] is False
        assert "log_file" not in data
        assert "[earlier output truncated" not in data["output"]
    finally:
        await spool_manager.aclose()


@pytest.mark.asyncio
async def test_background_result_always_names_log_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool_manager = make_spool_manager(tmp_path)
    try:
        monkeypatch.setattr(bash_module, "_shell_argv", python_command)
        context = make_context(tmp_path)

        result = await bash_handler(
            context,
            {"command": "import time; time.sleep(30)", "mode": "background"},
            spool_manager,
        )

        assert result["ok"] is True
        data = result["data"]
        assert data["status"] == "running"
        assert Path(data["log_file"]).exists()

        await kill_background(spool_manager, result)
    finally:
        await spool_manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("large_output", [False, True])
async def test_timeout_failure_carries_output_tail_and_log_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    large_output: bool,
) -> None:
    spool_manager = make_spool_manager(tmp_path)
    try:
        monkeypatch.setattr(bash_module, "_shell_argv", python_command)
        context = make_context(tmp_path)
        output = "x" * 9000 + "diag-marker" if large_output else "diag-marker"

        result = await bash_handler(
            context,
            {
                "command": f"print({output!r}, flush=True); import time; time.sleep(30)",
                "mode": "auto",
                "timeout": 1.5,
                "background_after_seconds": 10,
            },
            spool_manager,
        )

        assert result["ok"] is False
        assert result["error"]["code"] == "process_timeout"
        message = result["error"]["message"]
        assert "diag-marker" in message, "output produced before the kill must survive"
        if large_output:
            tail = message.split("Output tail:\n", 1)[1].split("\nComplete output:", 1)[0]
            assert len(tail) <= 8000
    finally:
        await spool_manager.aclose()


@pytest.mark.asyncio
async def test_subagent_kill_failure_carries_output_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spool_manager = make_spool_manager(tmp_path)
    try:
        monkeypatch.setattr(bash_module, "_shell_argv", python_command)
        context = make_context(tmp_path, nesting_depth=1)

        result = await bash_handler(
            context,
            {
                "command": ("print('diag-marker', flush=True); import time; time.sleep(30)"),
                "mode": "auto",
                "background_after_seconds": 1.5,
            },
            spool_manager,
        )

        assert result["ok"] is False
        assert result["error"]["code"] == bash_module.BACKGROUND_AT_DEPTH_FAILURE_CODE
        message = result["error"]["message"]
        assert "diag-marker" in message
    finally:
        await spool_manager.aclose()


def test_spawn_failure_message_names_missing_shell() -> None:
    message = bash_results._spawn_failure_message(
        ["missing-vbot-shell", "-c", "x"], FileNotFoundError("no such file")
    )

    assert "missing-vbot-shell" in message


def test_spawn_failure_message_explains_pwsh_requirement() -> None:
    message = bash_results._spawn_failure_message(
        ["pwsh", "-Command", "x"], FileNotFoundError("no such file")
    )

    assert "PowerShell 7" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [0, 7])
async def test_real_timeout_kill_during_reader_drain_keeps_exit(
    manager, tmp_path, monkeypatch, code
):
    monkeypatch.setattr(bash_module, "_shell_argv", python_command)
    original_readers = manager._await_reader_tasks
    original_spawn = manager.spawn
    original_kill = manager.kill
    kill_entered = asyncio.Event()

    async def delayed_readers(tracked):
        # Hold the watcher after OS exit until the real timeout invokes kill.
        await kill_entered.wait()
        await original_readers(tracked)

    async def spawn_exited(scope_key, agent_id, argv, **kwargs):
        process_id = await original_spawn(scope_key, agent_id, argv, **kwargs)
        tracked = manager.get_process(process_id, agent_id, project_id=kwargs.get("project_id"))
        await tracked.proc.wait()
        return process_id

    async def observe_kill(process_id, agent_id, **kwargs):
        tracked = manager.get_process(process_id, agent_id, **kwargs)
        assert tracked.proc.returncode == code
        assert tracked.status == "running"
        kill_entered.set()
        await original_kill(process_id, agent_id, **kwargs)

    monkeypatch.setattr(manager, "_await_reader_tasks", delayed_readers)
    monkeypatch.setattr(manager, "spawn", spawn_exited)
    monkeypatch.setattr(manager, "kill", observe_kill)
    try:
        result = await asyncio.wait_for(
            bash_handler(
                make_context(tmp_path),
                {
                    "command": f"print('test-owned output'); raise SystemExit({code})",
                    "mode": "foreground",
                    "timeout": 0.01,
                },
                manager,
            ),
            5,
        )
    finally:
        kill_entered.set()
    assert kill_entered.is_set()
    assert result["ok"] is True
    assert result["data"]["exit_code"] == code
    assert result["data"]["status"] == "completed"
    assert "test-owned output" in result["data"]["output"]


@pytest.mark.asyncio
async def test_failed_timeout_kill_returns_error_without_losing_process(
    manager, tmp_path, monkeypatch
):
    import core.tools.process_manager as manager_module

    monkeypatch.setattr(bash_module, "_shell_argv", python_command)

    async def denied(proc, **kwargs):
        raise PermissionError("test-owned denied timeout kill")

    with monkeypatch.context() as patch:
        patch.setattr(manager_module, "kill_process_tree_async", denied)
        result = await asyncio.wait_for(
            bash_handler(
                make_context(tmp_path),
                {
                    "command": "import time; time.sleep(30)",
                    "timeout": 0.05,
                },
                manager,
            ),
            3,
        )
    assert result["ok"] is False
    assert result["error"]["code"] == "process_kill_failed"
    tracked = manager.list_processes(AGENT_ID)[0]
    assert tracked.status == "running"
    assert tracked.process_id in result["error"]["message"]
    await manager.kill(tracked.process_id, AGENT_ID)
    assert tracked.status == "killed"
