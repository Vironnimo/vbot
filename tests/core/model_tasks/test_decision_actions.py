import asyncio
import json
import sys

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


@pytest.mark.asyncio
async def test_timeout_and_cancellation_stop_child_before_return(tmp_path):
    marker = tmp_path / "unexpected"
    command = {
        "argv": [
            sys.executable,
            "-c",
            "import time,pathlib,sys; time.sleep(1); pathlib.Path(sys.argv[1]).touch()",
            str(marker),
        ],
        "cwd": str(tmp_path),
    }
    with pytest.raises(DecisionError) as error:
        await run_command(command, 0.1)
    assert error.value.code == "command_timeout"
    task = asyncio.create_task(run_command(command, 10))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(1.1)
    assert not marker.exists()


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
