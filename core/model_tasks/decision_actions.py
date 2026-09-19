"""Fixed operator-authored commands for observing and acting on external apps."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any, cast

from core.model_tasks.decision_types import DecisionError, finite_number, json_copy, text
from core.utils.processes import (
    guarded_process_launch,
    kill_process_tree_async,
    subprocess_creation_flags,
)


def validate_control(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "instructions",
        "observe",
        "actions",
        "interval_ms",
        "max_steps",
        "timeout_seconds",
    }:
        raise DecisionError(
            "Control needs instructions, observe, actions, interval_ms, "
            "max_steps, and timeout_seconds."
        )
    value = json_copy(value)
    text(value["instructions"], "Control instructions")
    validate_command(value["observe"])
    actions = value["actions"]
    if not isinstance(actions, dict) or len(actions) < 2:
        raise DecisionError("Define at least two named actions.")
    for key, action in actions.items():
        text(key, "Action id", maximum=128)
        if not isinstance(action, dict) or set(action) != {"description", "command"}:
            raise DecisionError(
                "Each action needs description and command. Use null for a no-op command."
            )
        text(action["description"], "Action description")
        if action["command"] is not None:
            validate_command(action["command"])
    for name, maximum in (("interval_ms", 60_000), ("max_steps", 1_000_000)):
        if not isinstance(value[name], int) or not finite_number(value[name], maximum=maximum):
            raise DecisionError(f"{name} must be an integer from 0 to {maximum}.")
    if not finite_number(value["timeout_seconds"], minimum=0.1, maximum=300):
        raise DecisionError("Command timeout must be between 0.1 and 300 seconds.")
    return cast(dict[str, Any], value)


def validate_command(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"argv", "cwd"}:
        raise DecisionError("A command needs argv and an absolute cwd.")
    argv = value["argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and "\0" not in item for item in argv)
    ):
        raise DecisionError("Command argv must be a non-empty array of literal string arguments.")
    text(argv[0], "Executable")
    cwd = text(value["cwd"], "Command working directory")
    if not Path(cwd).is_absolute() or "\0" in cwd:
        raise DecisionError("Command working directory must be an absolute path on the vBot host.")


async def run_command(command: dict[str, Any], timeout: float) -> str:
    """Capture bounded UTF-8 stdout; never interpolate Model data into commands."""
    launch = guarded_process_launch(command["argv"])
    pending = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *launch.argv,
            cwd=command["cwd"],
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            creationflags=subprocess_creation_flags(new_process_group=True),
            start_new_session=os.name != "nt",
            pass_fds=launch.pass_fds if os.name != "nt" else (),
        )
    )
    try:
        proc = await asyncio.shield(pending)
    except asyncio.CancelledError:
        # A cancelled spawn may already have created its child. Reap it before returning.
        proc = await pending
        with contextlib.suppress(ProcessLookupError):
            await kill_process_tree_async(proc)
        await proc.wait()
        raise
    except OSError as exc:
        raise DecisionError(f"Could not start command: {exc}", code="command_failed") from exc

    async def read(stream: asyncio.StreamReader | None) -> bytes:
        assert stream is not None
        chunks = []
        size = 0
        while chunk := await stream.read(8192):
            size += len(chunk)
            if size > 256_000:
                raise DecisionError(
                    "Command output exceeded 256 KB. Return a compact state.", code="output_limit"
                )
            chunks.append(chunk)
        return b"".join(chunks)

    readers = [asyncio.create_task(read(proc.stdout)), asyncio.create_task(read(proc.stderr))]
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await asyncio.gather(*readers)
            await proc.wait()
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace")[-2000:]
            raise DecisionError(
                f"Command exited with {proc.returncode}: {detail}", code="command_failed"
            )
        return stdout.decode("utf-8")
    except TimeoutError as exc:
        raise DecisionError(
            "Command timed out. The action may already have taken effect; it was not retried.",
            code="command_timeout",
        ) from exc
    except UnicodeDecodeError as exc:
        raise DecisionError("Command output must use UTF-8.", code="invalid_observation") from exc
    finally:
        # Kill the process group even when the root exited with descendants still alive.
        with contextlib.suppress(ProcessLookupError):
            await kill_process_tree_async(proc)
        await proc.wait()
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
