"""The Python Tool retains shell authority and returns only selected execution output."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

import core.tools.execute_code as execute_code
import core.tools.shell_environment as shell_environment
from core.tools import ToolContext, ToolRegistry, tool_success
from core.tools._code_process import CodeExecutionResult
from tests.core.permissions.command_check_support import (
    FakeAnalyzer,
    fake_checker,
    plan,
    restricted,
)
from tests.core.tools.tools_test_support import dispatch_as_executor, make_context


def registry(analyzer: FakeAnalyzer | None = None) -> ToolRegistry:
    result = ToolRegistry()
    execute_code.register_execute_code_tool(
        result,
        command_checker=fake_checker(analyzer or FakeAnalyzer()),
        credential_names=lambda: {"TEST_SERVICE_TOKEN"},
    )
    return result


async def invoke(context: ToolContext, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return tool_success({"name": name, **arguments})


def completed(output: str = "selected\n") -> CodeExecutionResult:
    return CodeExecutionResult("completed", 0, output, "", 0, 0, [])


@pytest.mark.parametrize(
    "arguments",
    [
        {"code": 'print("Grüße")\n'},
        {"script": 'print("Grüße")\n'},
        json.dumps({"code": 'print("Grüße")\n'}),
        'print("Grüße")\n',
    ],
)
@pytest.mark.asyncio
async def test_python_source_reaches_worker_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: Any
) -> None:
    calls: list[str] = []

    async def worker(code: str, **kwargs: Any) -> CodeExecutionResult:
        calls.append(code)
        assert kwargs["cwd"] == tmp_path
        nested = await kwargs["invoke"]("read", {"path": "private.txt"})
        assert nested == tool_success({"name": "read", "path": "private.txt"})
        return completed()

    log = tmp_path / "code.log"
    log.touch()
    monkeypatch.setattr(execute_code, "run_python", worker)
    monkeypatch.setattr(execute_code, "_output_path", lambda _scratch: log)
    result = await dispatch_as_executor(
        registry(),
        make_context("execute_code", workspace=tmp_path, tool_call_hook=invoke),
        arguments,
        ["execute_code", "bash"],
    )
    assert result == tool_success(
        {"status": "completed", "output": "selected\n", "tool_calls": 0, "exit_code": 0}
    )
    assert calls == ['print("Grüße")\n']
    assert not log.exists()  # Only shortened output names (and keeps) its capture file.


@pytest.mark.parametrize(
    ("arguments", "fields", "allowed", "error"),
    [
        ({"code": "print(1)", "python": "print(2)"}, {}, ["bash"], "invalid_arguments"),
        ({"code": "print(1)"}, {"programmatic": True}, ["bash"], "code_recursion"),
        ({"code": "print(1)"}, {}, [], "tool_not_allowed"),
        ({"code": "print(1)"}, {"tool_call_hook": None}, ["bash"], "code_unavailable"),
    ],
)
@pytest.mark.asyncio
async def test_refused_code_never_starts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    arguments: Any,
    fields: dict[str, Any],
    allowed: list[str],
    error: str,
) -> None:
    async def worker(*args: Any, **kwargs: Any) -> CodeExecutionResult:
        pytest.fail("a refused execution started")

    monkeypatch.setattr(execute_code, "run_python", worker)
    context = make_context(
        "execute_code", **{"workspace": tmp_path, "tool_call_hook": invoke, **fields}
    )
    tools = registry()
    result = await dispatch_as_executor(tools, context, arguments, ["execute_code", *allowed])
    assert result["error"]["code"] == error, result
    display = tools.get("execute_code").display
    assert display is not None
    details = display.to_payload({"code": "print(1)"}, result=result)["details"]
    assert {"type": "notice", "level": "error", "text": result["error"]["message"]} in details


@pytest.mark.parametrize("denied", [False, True])
@pytest.mark.asyncio
async def test_python_launch_uses_existing_command_rules_and_confined_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, denied: bool
) -> None:
    analyzer = FakeAnalyzer(plan("python -c program"))
    rules = {"deny": ["Shell(python *)"]} if denied else {}
    monkeypatch.setattr(
        shell_environment,
        "_login_environment",
        lambda: {"TEST_SERVICE_TOKEN": "withheld", "USER_VARIABLE": "available"},
    )
    launched = False

    async def worker(code: str, **kwargs: Any) -> CodeExecutionResult:
        nonlocal launched
        launched = True
        environment = kwargs["environment"]
        assert "TEST_SERVICE_TOKEN" not in environment
        assert not any(key.startswith("VBOT_RUN_") for key in environment)
        assert environment["USER_VARIABLE"] == "available"
        return completed()

    monkeypatch.setattr(execute_code, "run_python", worker)
    result = await dispatch_as_executor(
        registry(analyzer),
        make_context(
            "execute_code",
            workspace=tmp_path,
            permissions=restricted(tmp_path, **rules),
            tool_call_hook=invoke,
        ),
        {"code": "print(1)"},
        ["execute_code", "bash"],
    )
    assert analyzer.argvs == [((sys.executable, "-c", "print(1)"), tmp_path)]
    assert launched is not denied
    if denied:
        assert result["error"]["code"] == "permission_denied", result
    else:
        assert result["ok"] is True, result


@pytest.mark.parametrize(
    ("ran", "note_parts"),
    [
        (True, ["300-second", "remain applied", "incomplete"]),
        (False, ["Nothing was run", "compile"]),
    ],
)
@pytest.mark.asyncio
async def test_partial_execution_and_shortened_output_are_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ran: bool, note_parts: list[str]
) -> None:
    log = tmp_path / "output.log"
    log.touch()

    async def worker(*args: Any, **kwargs: Any) -> CodeExecutionResult:
        if not ran:
            return CodeExecutionResult(
                "failed", 1, "", "SyntaxError: '(' was never closed", 0, 0, [], ran=False
            )
        return CodeExecutionResult(
            "timed_out",
            None,
            "selected",
            "unfinished",
            4,
            2,
            [{"tool": "write", "code": "permission_denied", "message": "refused"}],
            output_truncated=True,
            output_path=log,
            log_truncated=True,
        )

    monkeypatch.setattr(execute_code, "run_python", worker)
    monkeypatch.setattr(execute_code, "_output_path", lambda _scratch: log)
    tools = registry()
    result = await dispatch_as_executor(
        tools,
        make_context("execute_code", workspace=tmp_path, tool_call_hook=invoke),
        {"code": "print(1)"},
        ["execute_code", "bash"],
    )
    assert result["ok"] is True, result
    data = result["data"]
    for part in note_parts:
        assert part in data["note"]
    if ran:
        assert data["status"] == "timed_out"
        assert data["tool_calls"] == 4
        assert data["tool_failure_count"] == 2
        assert data["output_truncated"] is True
        assert data["log_truncated"] is True
        assert data["output_file"] == log.as_posix()
        assert log.exists()
    else:
        # Nothing ran, so nothing needs inspecting.
        assert "remain applied" not in data["note"]
        assert not log.exists()
    display = tools.get("execute_code").display
    assert display is not None
    details = display.to_payload({"code": "print(1)"}, result=result)["details"]
    assert {"type": "notice", "level": "error", "text": data["note"]} in details


@pytest.mark.parametrize("whole_run", [False, True])
@pytest.mark.asyncio
async def test_stopping_one_execution_returns_a_result_but_run_cancellation_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, whole_run: bool
) -> None:
    entered = asyncio.Event()
    cancelled = False
    callbacks: list[Any] = []

    async def worker(*args: Any, **kwargs: Any) -> CodeExecutionResult:
        entered.set()
        await kwargs["stop"].wait()
        # A user stop keeps what the program printed until then.
        return CodeExecutionResult("stopped", None, "partial", "", 1, 0, [])

    monkeypatch.setattr(execute_code, "run_python", worker)
    task = asyncio.create_task(
        dispatch_as_executor(
            registry(),
            make_context(
                "execute_code",
                workspace=tmp_path,
                tool_call_hook=invoke,
                cancellation_hook=lambda: whole_run and cancelled,
                cancel_check_hook=lambda: cancelled,
                cancel_registration_hook=callbacks.append,
            ),
            {"code": "pass"},
            ["execute_code", "bash"],
        )
    )
    await entered.wait()
    cancelled = True
    callbacks[0]()
    if whole_run:
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result["ok"] is True, result
        assert result["data"]["status"] == "stopped"
        assert result["data"]["output"] == "partial"
        assert "remain applied" in result["data"]["note"]
