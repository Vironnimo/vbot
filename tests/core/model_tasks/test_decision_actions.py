import asyncio
import contextlib
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


def _assert_no_child_runs(marker: Path) -> None:
    """No process started with *marker* on its command line runs anymore.

    The command line names the child even before its first statement, and an
    ended process has none left to read, so neither a lingering nor a reused
    PID can mislead the check.
    """
    survivors = []
    for process in psutil.process_iter():
        with contextlib.suppress(psutil.Error):
            if str(marker) in process.cmdline():
                survivors.append(process)
    for survivor in survivors:
        with contextlib.suppress(psutil.Error):
            survivor.kill()
    assert not survivors


@pytest.mark.asyncio
async def test_timeout_and_cancellation_stop_child_before_return(tmp_path):
    def command(started: Path) -> dict[str, object]:
        # The child marks that it runs, then outlasts the test unless it is stopped.
        return {
            "argv": [
                sys.executable,
                "-c",
                "import pathlib,sys,time; pathlib.Path(sys.argv[1]).touch(); time.sleep(60)",
                str(started),
            ],
            "cwd": str(tmp_path),
        }

    timed_out = tmp_path / "timed-out.started"
    with pytest.raises(DecisionError) as error:
        await run_command(command(timed_out), 0.1)
    assert error.value.code == "command_timeout"
    _assert_no_child_runs(timed_out)

    cancelled = tmp_path / "cancelled.started"
    task = asyncio.create_task(run_command(command(cancelled), 60))
    while not cancelled.exists() and not task.done():
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _assert_no_child_runs(cancelled)


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
