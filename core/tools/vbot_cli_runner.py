"""Run a vbot Tool command through the serving installation's CLI."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path
from typing import Any

from core.tools._python_process import OutputCapture, python_executable
from core.tools.shell_environment import RunIdentity, command_environment
from core.utils.config import DEFAULT_HOST, VBOT_ROOT
from core.utils.processes import (
    create_subprocess_exec,
    kill_process_tree_async,
    subprocess_creation_flags,
    wait_for_exit,
)
from core.utils.workers import finish_despite_cancel

REVIEW_ENV = "VBOT_CLI_REVIEW_AGENT_ACCESS"
REVIEW_EXIT = 79


class VBotAccessReviewRequiredError(Exception):
    """The bound CLI stopped before dispatch and described its actual operands."""

    def __init__(self, rights: bool = True, reads: Sequence[tuple[str, bool]] = ()) -> None:
        super().__init__()
        self.rights = rights
        self.reads = tuple((path, subtree) for path, subtree in reads)


async def _capture(reader: asyncio.StreamReader, output: OutputCapture) -> None:
    while chunk := await reader.read(65536):
        output.append(chunk)


async def run_vbot_cli(
    argv: Sequence[str],
    *,
    stdin: str | None,
    data_dir: Path,
    port: int | None,
    run: RunIdentity,
    host: str = DEFAULT_HOST,
    check_agent_access: bool = False,
) -> dict[str, Any]:
    if port is None:
        raise ValueError("The vBot CLI Tool needs a running server.")
    env = command_environment(run).complete()
    env.update(
        VBOT_DATA_DIR=str(data_dir.resolve()),
        VBOT_SERVER_PORT=str(port),
        VBOT_APP_CLI_LISTEN_HOST=host,
    )
    env.pop("VBOT_SERVER_URL", None)
    env.pop(REVIEW_ENV, None)
    review_marker = secrets.token_hex(24) if check_agent_access else None
    if review_marker is not None:
        env[REVIEW_ENV] = review_marker
    # The CLI owns detached restart/update workers. A shell's POSIX subreaper
    # would wait for those workers, which themselves wait for this Tool Result.
    starting = asyncio.create_task(
        create_subprocess_exec(
            python_executable(),
            "-m",
            "cli.app_agent",
            *argv,
            cwd=VBOT_ROOT,
            env=env,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess_creation_flags(new_process_group=True),
            start_new_session=os.name != "nt",
        )
    )
    stdout, stderr = OutputCapture(64 * 1024), OutputCapture(16 * 1024)
    timeout = False
    try:
        process = await asyncio.shield(starting)
        assert process.stdout is not None and process.stderr is not None

        async def send_input() -> None:
            if process.stdin is not None and stdin is not None:
                try:
                    process.stdin.write(stdin.encode("utf-8"))
                    await process.stdin.drain()
                except BrokenPipeError, ConnectionResetError:
                    pass  # A parse error or a command without stdin can close the pipe early.
                finally:
                    process.stdin.close()

        try:
            async with asyncio.timeout(120), asyncio.TaskGroup() as group:
                group.create_task(_capture(process.stdout, stdout))
                group.create_task(_capture(process.stderr, stderr))
                group.create_task(send_input())
                result = await wait_for_exit(process)
        except TimeoutError:
            timeout = True
            result = 124
    finally:
        # Cancellation during launch still owns the child once the OS creates it.
        if not starting.cancelled():
            process = await finish_despite_cancel(starting)
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    await finish_despite_cancel(kill_process_tree_async(process))
                await finish_despite_cancel(wait_for_exit(process))
    error_output = stderr.text()
    if review_marker and result == REVIEW_EXIT:
        lines = stdout.text().splitlines()
        if lines and lines[0] == review_marker:
            if len(lines) != 2 or stdout.truncated:
                raise ValueError("The vbot command has too many input paths. Use fewer per call.")
            review = json.loads(lines[1])
            raise VBotAccessReviewRequiredError(review["rights"], review["reads"])
    if timeout:
        error_output += "\nCommand timed out. Inspect the result before repeating a change."
    return {
        "exit_code": result,
        "stdout": stdout.text(),
        "stderr": error_output,
        "stdout_truncated": stdout.truncated,
        "stderr_truncated": stderr.truncated,
    }
