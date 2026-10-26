"""The real Python/Tool bridge: data, independent callers and owned process lifetime.

These short subprocess checks protect the actual IPC protocol and process-tree
cleanup, which an in-process fake cannot establish.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil  # type: ignore[import-untyped]
import pytest

from core.tools import _code_process
from core.tools._code_process import MAX_OUTPUT_BYTES, MAX_STDERR_BYTES, run_python
from core.tools._tool_results import tool_failure, tool_success
from core.tools.contracts import JsonObject


@pytest.mark.asyncio
async def test_python_calls_keep_data_and_parallel_call_results(tmp_path: Path) -> None:
    entered: list[int] = []
    all_entered = asyncio.Event()

    async def invoke(name: str, arguments: JsonObject) -> JsonObject:
        if name == "refused":
            return tool_failure("permission_denied", "Test-owned refusal.")
        value = arguments["value"]
        entered.append(value)
        if len(entered) == 4:
            all_entered.set()
        await all_entered.wait()
        return tool_success({"value": value, "nested": {"items": [value, "\u00fc"]}})

    result = await run_python(
        """import json
from concurrent.futures import ThreadPoolExecutor
from vbot_tools import tools, ToolError
with ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(lambda n: tools.echo(value=n), range(4)))
try:
    tools.call("refused", {})
except ToolError as error:
    print(error.code)
print(json.dumps(results))
""",
        cwd=tmp_path,
        environment=os.environ,
        invoke=invoke,
    )
    assert result.status == "completed"
    assert result.exit_code == 0
    assert sorted(entered) == list(range(4))
    lines = result.output.splitlines()
    assert lines[0] == "permission_denied"
    assert json.loads(lines[1]) == [
        {"value": value, "nested": {"items": [value, "\u00fc"]}} for value in range(4)
    ]
    assert result.tool_calls == 5
    assert result.tool_failure_count == 1
    assert result.tool_failures[0]["code"] == "permission_denied"
    assert result.stderr == ""


@pytest.mark.asyncio
async def test_python_tool_keywords_do_not_collide_with_the_client(tmp_path: Path) -> None:
    calls: list[JsonObject] = []

    async def invoke(name: str, arguments: JsonObject) -> JsonObject:
        calls.append({"tool": name, "arguments": arguments})
        return tool_success(arguments)

    result = await run_python(
        """from vbot_tools import tools
tools.skill(name='demo')
tools.mcp(action='call', target='demo/echo', arguments={'text': 'hello'})
tools.call('dynamic', {'name': 'kept', 'arguments': {'nested': True}})
""",
        cwd=tmp_path,
        environment=os.environ,
        invoke=invoke,
    )
    assert result.status == "completed" and result.tool_calls == 3
    assert calls == [
        {"tool": "skill", "arguments": {"name": "demo"}},
        {
            "tool": "mcp",
            "arguments": {"action": "call", "target": "demo/echo", "arguments": {"text": "hello"}},
        },
        {"tool": "dynamic", "arguments": {"name": "kept", "arguments": {"nested": True}}},
    ]


@pytest.mark.asyncio
async def test_python_rejects_non_json_arguments_before_sending(tmp_path: Path) -> None:
    async def invoke(_name: str, _arguments: JsonObject) -> JsonObject:
        raise AssertionError("Invalid arguments must never be dispatched")

    result = await run_python(
        """from vbot_tools import tools, ToolError
for value in [float('nan'), {1, 2}, 'x' * (2 * 1024 * 1024)]:
    try:
        tools.echo(value=value)
    except ToolError as error:
        print(error.code, error.message.split(':')[0])
""",
        cwd=tmp_path,
        environment=os.environ,
        invoke=invoke,
    )
    assert result.status == "completed" and result.tool_calls == 0
    assert result.output.splitlines() == ["invalid_arguments Nothing was sent"] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize("log_limit", [512, 300_000])
async def test_python_output_is_bounded_and_log_keeps_the_printed_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, log_limit: int
) -> None:
    async def invoke(_name: str, _arguments: JsonObject) -> JsonObject:
        raise AssertionError("This program calls no Tools")

    log = tmp_path / "output.txt"
    log.touch()  # The Tool reserves its private log before launching the worker.
    monkeypatch.setattr(_code_process, "MAX_LOG_BYTES", log_limit)
    result = await run_python(
        f"import sys\nprint('begin' + 'x' * {MAX_OUTPUT_BYTES * 2} + 'end')\n"
        f"print('stderr-begin' + 'y' * {MAX_STDERR_BYTES * 2} + 'stderr-end', file=sys.stderr)",
        cwd=tmp_path,
        environment=os.environ,
        invoke=invoke,
        output_path=log,
    )
    assert result.status == "completed"
    assert result.output.startswith("begin") and result.output.rstrip().endswith("end")
    assert result.stderr.startswith("stderr-begin") and result.stderr.rstrip().endswith(
        "stderr-end"
    )
    assert result.output_truncated and result.stderr_truncated
    assert result.output_path == log
    logged = log.read_text(encoding="utf-8")
    if log_limit == 512:
        assert result.log_truncated and log.stat().st_size == log_limit
    else:
        assert not result.log_truncated
        assert logged.count("x") == MAX_OUTPUT_BYTES * 2
        assert logged.count("y") == MAX_STDERR_BYTES * 2
        assert "begin" in logged and "stderr-begin" in logged and "stderr-end" in logged


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limit", "value", "calls", "error_code"),
    [
        ("MAX_RESPONSE_BYTES", 512, 1, "code_result_too_large"),
        ("MAX_TOOL_CALLS", 0, 0, "code_tool_limit"),
    ],
)
async def test_python_bridge_limits_report_failures_without_replaying(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
    value: int,
    calls: int,
    error_code: str,
) -> None:
    invoked: list[str] = []
    monkeypatch.setattr(_code_process, limit, value)

    async def invoke(name: str, _arguments: JsonObject) -> JsonObject:
        invoked.append(name)
        return tool_success({"large": "x" * 1000})

    result = await run_python(
        """from vbot_tools import tools, ToolError
try:
    tools.change()
except ToolError as error:
    print(error.code)
""",
        cwd=tmp_path,
        environment=os.environ,
        invoke=invoke,
    )
    assert result.status == "completed"
    assert result.output.strip() == error_code
    assert len(invoked) == calls == result.tool_calls
    assert result.tool_failure_count == 1
    assert result.tool_failures[0]["code"] == error_code


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "exit_code", "has_traceback"),
    [
        ("raise ValueError('test-owned script failure')", 1, True),
        ("raise SystemExit(4)", 4, False),
        ("def malformed(\n", 1, True),
    ],
)
async def test_python_failure_is_not_a_successful_cell(
    tmp_path: Path, source: str, exit_code: int, has_traceback: bool
) -> None:
    async def invoke(_name: str, _arguments: JsonObject) -> JsonObject:
        raise AssertionError("This program calls no Tools")

    result = await run_python(source, cwd=tmp_path, environment=os.environ, invoke=invoke)
    assert result.status == "failed"
    assert result.exit_code == exit_code
    assert bool(result.stderr) is has_traceback
    assert result.tool_calls == 0


@pytest.mark.asyncio
async def test_cancelling_python_stops_descendants_and_drains_tool_calls(tmp_path: Path) -> None:
    admitted = asyncio.Event()
    cancelled = asyncio.Event()
    processes: list[psutil.Process] = []

    async def invoke(_name: str, arguments: JsonObject) -> JsonObject:
        processes.extend(psutil.Process(pid) for pid in arguments["pids"])
        admitted.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("A cancelled call cannot return")

    task = asyncio.create_task(
        run_python(
            """import os, subprocess, sys
from vbot_tools import tools
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
tools.hold(pids=[os.getpid(), child.pid])
""",
            cwd=tmp_path,
            environment=os.environ,
            invoke=invoke,
        )
    )
    try:
        await asyncio.wait_for(admitted.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert all(not process.is_running() for process in processes)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_python_deadline_stops_uncooperative_code(tmp_path: Path) -> None:
    async def invoke(_name: str, _arguments: JsonObject) -> JsonObject:
        raise AssertionError("This program calls no Tools")

    result = await run_python(
        "while True: pass",
        cwd=tmp_path,
        environment=os.environ,
        invoke=invoke,
        timeout_seconds=0.2,
    )
    assert result.status == "timed_out"
    assert result.exit_code is None


@pytest.mark.asyncio
async def test_output_failure_stops_python_and_closes_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processes: list[psutil.Process] = []
    original_worker = _code_process.run_tool_worker
    log_closed = False

    async def fail_log_write(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        nonlocal log_closed
        if getattr(function, "__name__", "") == "write":
            raise OSError("Test-owned disk failure")
        if getattr(function, "__name__", "") == "close":
            log_closed = True
        return await original_worker(function, *args, **kwargs)

    async def invoke(_name: str, arguments: JsonObject) -> JsonObject:
        processes.append(psutil.Process(arguments["pid"]))
        return tool_success({})

    monkeypatch.setattr(_code_process, "run_tool_worker", fail_log_write)
    with pytest.raises(OSError, match="Test-owned disk failure"):
        await run_python(
            """import os
from vbot_tools import tools
tools.remember(pid=os.getpid())
print('This output cannot be saved')
while True: pass
""",
            cwd=tmp_path,
            environment=os.environ,
            invoke=invoke,
            output_path=tmp_path / "output.txt",
        )
    assert log_closed
    assert len(processes) == 1 and not processes[0].is_running()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="Windows Jobs provide kill-on-close")
async def test_refused_termination_closes_the_job_before_waiting_for_pipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_worker = _code_process.run_tool_worker
    processes: list[psutil.Process] = []

    async def refuse_termination(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if getattr(function, "__name__", "") == "terminate":
            raise OSError("Test-owned termination refusal")
        return await original_worker(function, *args, **kwargs)

    async def invoke(_name: str, arguments: JsonObject) -> JsonObject:
        processes.append(psutil.Process(arguments["pid"]))
        return tool_success({})

    monkeypatch.setattr(_code_process, "run_tool_worker", refuse_termination)
    with pytest.raises(OSError, match="Test-owned termination refusal"):
        await run_python(
            "import os\nfrom vbot_tools import tools\ntools.remember(pid=os.getpid())",
            cwd=tmp_path,
            environment=os.environ,
            invoke=invoke,
        )
    assert len(processes) == 1 and not processes[0].is_running()
