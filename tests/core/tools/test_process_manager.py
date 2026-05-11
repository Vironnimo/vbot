"""Tests for async process session management."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import pytest

from core.tools.process_manager import (
    PROCESS_BUFFER_CAP_BYTES,
    ProcessManager,
    SessionNotFoundError,
    SessionStillRunningError,
)

PollResult = dict[str, object]

AGENT_A = "agent-a"
AGENT_B = "agent-b"
SCOPE_A = "run-a"


@pytest.fixture
def manager() -> ProcessManager:
    return ProcessManager(sweep_interval_seconds=3600)


async def poll_until_terminal(
    manager: ProcessManager,
    session_id: str,
    *,
    agent_id: str = AGENT_A,
) -> PollResult:
    combined_result: PollResult = {}
    stdout = ""
    stderr = ""
    output = ""
    for _ in range(20):
        result = await manager.poll(session_id, agent_id, timeout_ms=500)
        stdout += as_text(result["stdout"])
        stderr += as_text(result["stderr"])
        output += as_text(result["output"])
        combined_result = dict(result)
        combined_result["stdout"] = stdout
        combined_result["stderr"] = stderr
        combined_result["output"] = output
        if result["status"] != "running":
            return combined_result

    return combined_result


def as_text(value: object) -> str:
    assert isinstance(value, str)
    return value


@pytest.mark.asyncio
async def test_spawn_captures_stdout_and_stderr(manager: ProcessManager) -> None:
    session_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [
            sys.executable,
            "-c",
            "import sys; print('hello'); print('problem', file=sys.stderr)",
        ],
        env=None,
        cwd=None,
    )

    result = await poll_until_terminal(manager, session_id)

    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert "hello" in as_text(result["stdout"])
    assert "problem" in as_text(result["stderr"])
    assert result["output"]


@pytest.mark.asyncio
async def test_buffer_cap_drops_oldest_bytes_and_marks_truncated(tmp_path) -> None:
    manager = ProcessManager(buffer_cap_bytes=32, sweep_interval_seconds=3600)
    script = "import sys; sys.stdout.write('a' * 64); sys.stdout.flush()"
    session_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", script],
        env=None,
        cwd=tmp_path,
    )

    result = await poll_until_terminal(manager, session_id)
    log_result = await manager.log(session_id, AGENT_A)

    assert result["status"] == "completed"
    assert log_result["truncated"] is True
    assert log_result["output"] == "a" * 32


@pytest.mark.asyncio
async def test_sweep_finished_removes_expired_sessions(manager: ProcessManager) -> None:
    session_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "print('done')"],
        env=None,
        cwd=None,
    )
    await poll_until_terminal(manager, session_id)
    session = manager.get_session(session_id, AGENT_A)
    session.finished_at = datetime.now(UTC) - timedelta(minutes=31)

    await manager.sweep_finished()

    with pytest.raises(SessionNotFoundError):
        manager.get_session(session_id, AGENT_A)


@pytest.mark.asyncio
async def test_cancel_scope_kills_active_sessions(manager: ProcessManager) -> None:
    session_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        cwd=None,
    )

    manager.cancel_scope(SCOPE_A)
    result = await manager.poll(session_id, AGENT_A, timeout_ms=5000)

    assert result["status"] == "killed"


@pytest.mark.asyncio
async def test_poll_timeout_waits_for_new_output(manager: ProcessManager) -> None:
    session_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [
            sys.executable,
            "-c",
            "import sys, time; time.sleep(0.1); print('later'); sys.stdout.flush()",
        ],
        env=None,
        cwd=None,
    )

    result = await manager.poll(session_id, AGENT_A, timeout_ms=2000)

    assert "later" in as_text(result["stdout"])


@pytest.mark.asyncio
async def test_poll_timeout_returns_empty_when_no_output_arrives(manager: ProcessManager) -> None:
    session_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(1)"],
        env=None,
        cwd=None,
    )

    result = await manager.poll(session_id, AGENT_A, timeout_ms=20)
    await manager.kill(session_id, AGENT_A)

    assert result["status"] == "running"
    assert result["output"] == ""


@pytest.mark.asyncio
async def test_write_submit_kill_clear_and_list_sessions(manager: ProcessManager) -> None:
    script = "import sys; line = sys.stdin.readline(); print('got:' + line.strip())"
    session_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", script],
        env=None,
        cwd=None,
    )

    await manager.write(session_id, AGENT_A, "value")
    await manager.submit(session_id, AGENT_A)
    result = await poll_until_terminal(manager, session_id)

    assert result["status"] == "completed"
    assert "got:value" in as_text(result["stdout"])
    assert [session.session_id for session in manager.list_sessions(AGENT_A)] == [session_id]

    await manager.clear(session_id, AGENT_A)

    assert manager.list_sessions(AGENT_A) == []


@pytest.mark.asyncio
async def test_write_with_eof_closes_stdin(manager: ProcessManager) -> None:
    script = "import sys; data = sys.stdin.read(); print('read:' + data)"
    session_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", script],
        env=None,
        cwd=None,
    )

    await manager.write(session_id, AGENT_A, "payload", eof=True)
    result = await poll_until_terminal(manager, session_id)

    assert result["status"] == "completed"
    assert "read:payload" in as_text(result["stdout"])


@pytest.mark.asyncio
async def test_kill_stops_process_and_clear_removes_it(manager: ProcessManager) -> None:
    session_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        cwd=None,
    )

    await manager.kill(session_id, AGENT_A)
    result = await manager.poll(session_id, AGENT_A, timeout_ms=5000)
    await manager.clear(session_id, AGENT_A)

    assert result["status"] == "killed"
    with pytest.raises(SessionNotFoundError):
        manager.get_session(session_id, AGENT_A)


@pytest.mark.asyncio
async def test_clear_running_session_raises(manager: ProcessManager) -> None:
    session_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        cwd=None,
    )

    with pytest.raises(SessionStillRunningError):
        await manager.clear(session_id, AGENT_A)

    await manager.kill(session_id, AGENT_A)


@pytest.mark.asyncio
async def test_agent_isolation_for_session_access_methods(manager: ProcessManager) -> None:
    session_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=None,
        cwd=None,
    )

    with pytest.raises(SessionNotFoundError):
        await manager.poll(session_id, AGENT_B, timeout_ms=0)
    with pytest.raises(SessionNotFoundError):
        await manager.log(session_id, AGENT_B)
    with pytest.raises(SessionNotFoundError):
        await manager.write(session_id, AGENT_B, "data")
    with pytest.raises(SessionNotFoundError):
        await manager.submit(session_id, AGENT_B)
    with pytest.raises(SessionNotFoundError):
        await manager.kill(session_id, AGENT_B)
    with pytest.raises(SessionNotFoundError):
        await manager.clear(session_id, AGENT_B)

    assert manager.list_sessions(AGENT_B) == []
    assert [session.session_id for session in manager.list_sessions(AGENT_A)] == [session_id]

    await manager.kill(session_id, AGENT_A)


@pytest.mark.asyncio
async def test_log_returns_windowed_combined_output(manager: ProcessManager) -> None:
    script = "import sys; sys.stdout.write('one\\ntwo\\nthree\\n'); sys.stdout.flush()"
    session_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", script],
        env=None,
        cwd=None,
    )
    await poll_until_terminal(manager, session_id)

    result = await manager.log(session_id, AGENT_A, offset=1, limit=1)

    assert as_text(result["output"]).replace("\r\n", "\n") == "two\n"
    assert result["total_lines"] == 3


@pytest.mark.asyncio
async def test_foreground_capture_can_be_stopped(manager: ProcessManager) -> None:
    session_id = await manager.spawn(
        SCOPE_A,
        AGENT_A,
        [sys.executable, "-c", "import sys; print('foreground'); sys.stdout.flush()"],
        env=None,
        cwd=None,
    )
    await poll_until_terminal(manager, session_id)

    manager.mark_backgrounded(session_id, AGENT_A)
    session = manager.get_session(session_id, AGENT_A)

    assert b"foreground" in b"".join(session.stdout_lines)
    assert session.foreground_capture_open is False


def test_buffer_cap_default_is_500_kb() -> None:
    assert PROCESS_BUFFER_CAP_BYTES == 500 * 1024
