import asyncio
import json
import sys
from pathlib import Path

import psutil  # type: ignore[import-untyped]
import pytest

from core.model_tasks.decision_actions import run_command
from core.model_tasks.decision_types import DecisionError


@pytest.mark.asyncio
async def test_real_command_preserves_literal_arguments_and_stdout(tmp_path):
    result = await run_command(
        {
            "argv": [
                sys.executable,
                "-c",
                "import json,sys; print(json.dumps(sys.argv[1:]))",
                "x; echo nope",
                "two words",
                "ä",
            ],
            "cwd": str(tmp_path),
        },
        10,
    )
    assert json.loads(result) == ["x; echo nope", "two words", "ä"]


def _assert_process_stopped(pid_file: Path) -> None:
    """The child that recorded its pid in *pid_file* no longer runs."""

    assert not pid_file.with_suffix(".survived").exists()
    if not pid_file.exists():
        return  # Stopped before it reached its first statement.
    try:
        child = psutil.Process(int(pid_file.read_text()))
    except psutil.NoSuchProcess:
        return
    _gone, alive = psutil.wait_procs([child], timeout=5)
    for survivor in alive:
        survivor.kill()
    assert not alive


@pytest.mark.asyncio
async def test_timeout_and_cancellation_stop_child_before_return(tmp_path):
    def command(pid_file: Path) -> dict[str, object]:
        return {
            "argv": [
                sys.executable,
                "-c",
                "import os,pathlib,sys,time; p = pathlib.Path(sys.argv[1]); "
                "p.write_text(str(os.getpid())); time.sleep(5); "
                "p.with_suffix('.survived').touch()",
                str(pid_file),
            ],
            "cwd": str(tmp_path),
        }

    timed_out = tmp_path / "timed-out.pid"
    with pytest.raises(DecisionError) as error:
        await run_command(command(timed_out), 0.1)
    assert error.value.code == "command_timeout"
    _assert_process_stopped(timed_out)

    cancelled = tmp_path / "cancelled.pid"
    task = asyncio.create_task(run_command(command(cancelled), 30))
    async with asyncio.timeout(10):
        while not cancelled.exists() or not cancelled.read_text():
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _assert_process_stopped(cancelled)


@pytest.mark.asyncio
async def test_nonzero_and_excess_output_fail_without_retry(tmp_path):
    with pytest.raises(DecisionError) as error:
        await run_command(
            {
                "argv": [
                    sys.executable,
                    "-c",
                    "import sys; sys.stderr.write('fixture'); sys.exit(7)",
                ],
                "cwd": str(tmp_path),
            },
            10,
        )
    assert error.value.code == "command_failed"
    with pytest.raises(DecisionError) as error:
        await run_command(
            {"argv": [sys.executable, "-c", "print('x' * 300000)"], "cwd": str(tmp_path)}, 10
        )
    assert error.value.code == "output_limit"
