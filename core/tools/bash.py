"""Built-in bash tool backed by the shared process manager."""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from core.tools.process_manager import ProcessManager, SessionNotFoundError
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger

BASH_TOOL_NAME = "bash"
BASH_TOOL_DESCRIPTION = (
    "Run a shell command on the host system. Short commands complete in the foreground; "
    "long-running commands return a process session_id for later process-tool management."
)
BASH_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "Shell command to run.",
        },
        "workdir": {
            "type": "string",
            "description": "Working directory. Relative paths resolve from the workspace.",
        },
        "env": {
            "type": "object",
            "description": (
                "Additional environment variables. Dangerous loader/path keys are ignored."
            ),
            "additionalProperties": {"type": "string"},
        },
        "yield_after": {
            "type": "number",
            "description": "Seconds to wait for foreground completion before backgrounding.",
            "default": 30,
        },
        "background": {
            "type": "boolean",
            "description": "Return a background session immediately.",
        },
        "timeout": {
            "type": "number",
            "description": "Seconds after which the process is killed.",
        },
    },
    "required": ["command"],
    "additionalProperties": False,
}

BLOCKED_ENV_KEYS = {"PATH", "LD_PRELOAD", "BASH_ENV", "DYLD_INSERT_LIBRARIES"}
DEFAULT_YIELD_AFTER_SECONDS = 30.0
FOREGROUND_POLL_INTERVAL_SECONDS = 0.05
SHELL_ENV_PROBE_TIMEOUT_SECONDS = 5.0
SHELL_ENV_PROBE_REAP_TIMEOUT_SECONDS = 1.0
HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", 9)
USER_CANCELLED_FAILURE_CODE = "cancelled_by_user"
USER_CANCELLED_FAILURE_MESSAGE = "Command aborted by the user"
BACKGROUND_USER_CANCELLED_MESSAGE = "Background process was aborted by the user."

_LOGGER = get_logger("tools.bash")

_cached_shell_env: dict[str, str] | None = None

# Process session ids killed by the per-tool-call user-cancel callback. The
# completion watcher reads this set to distinguish user-killed sessions from
# natural completion and tool-enforced timeouts.
_user_cancelled_session_ids: set[str] = set()


async def bash_handler(
    context: ToolContext,
    arguments: JsonObject,
    process_manager: ProcessManager,
    trigger_service: Any | None = None,
) -> JsonObject:
    """Run a shell command and return a stable tool result envelope."""
    parsed = _parse_arguments(arguments)
    if isinstance(parsed, str):
        return tool_failure("invalid_arguments", parsed)

    command = parsed["command"]
    workdir = _resolve_workdir(context, parsed.get("workdir"))
    env = await _build_process_env(parsed.get("env"))
    argv = _shell_argv(command)

    try:
        session_id = await process_manager.spawn(
            context.run_id,
            context.agent_id,
            argv,
            env=env,
            cwd=workdir,
        )
    except (OSError, ValueError) as error:
        return tool_failure("process_spawn_failed", f"failed to start process: {error}")

    _register_user_cancel_callback(process_manager, context, session_id)

    timeout_task, timeout_state = _schedule_timeout(
        process_manager,
        session_id,
        context.agent_id,
        parsed.get("timeout"),
    )

    if parsed["background"]:
        result = await _background_result(process_manager, context, session_id)
        _maybe_spawn_completion_watcher(
            process_manager,
            context,
            session_id,
            command,
            trigger_service,
        )
        return result

    result = await _run_foreground_phase(
        process_manager,
        context,
        session_id,
        parsed["yield_after"],
    )

    if timeout_task is not None:
        timeout_task.cancel()

    if context.was_cancelled_by_user():
        return tool_failure(USER_CANCELLED_FAILURE_CODE, USER_CANCELLED_FAILURE_MESSAGE)

    if result["data"] is not None and result["data"].get("status") == "running":
        _maybe_spawn_completion_watcher(
            process_manager,
            context,
            session_id,
            command,
            trigger_service,
        )
        return result

    if timeout_state["timed_out"] and _timed_out_process_killed(
        process_manager, context, session_id
    ):
        return tool_failure(
            "process_timeout", f"process timed out after {parsed['timeout']} seconds"
        )

    return result


def register_bash_tool(
    registry: ToolRegistry,
    process_manager: ProcessManager,
    trigger_service: Any | None = None,
) -> None:
    """Register the bash tool with a vBot tool registry."""

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await bash_handler(
            context,
            arguments,
            process_manager,
            trigger_service=trigger_service,
        )

    registry.register(
        BASH_TOOL_NAME,
        BASH_TOOL_DESCRIPTION,
        BASH_TOOL_PARAMETERS,
        handler,
        display=ToolDisplay(summary_fields=("command",)),
    )


def _log_background_task_result(task: asyncio.Task[Any], message: str) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is None:
        return
    _LOGGER.error(
        "%s: %s",
        message,
        error,
        exc_info=(type(error), error, error.__traceback__),
    )


async def _watch_background_process(
    process_manager: ProcessManager,
    process_session_id: str,
    agent_id: str,
    chat_session_id: str,
    command: str,
    trigger_service: Any,
) -> None:
    try:
        session = process_manager.get_session(process_session_id, agent_id)
        wait_task = session.wait_task
        if wait_task is not None:
            await wait_task
        else:
            while process_manager.get_session(process_session_id, agent_id).status == "running":
                await asyncio.sleep(FOREGROUND_POLL_INTERVAL_SECONDS)
    except SessionNotFoundError as error:
        _LOGGER.warning(
            "Bash completion watcher skipped trigger for agent=%s process_session=%s: %s",
            agent_id,
            process_session_id,
            error,
        )
        return

    try:
        log_result = await process_manager.log(process_session_id, agent_id)
        session = process_manager.get_session(process_session_id, agent_id)
    except SessionNotFoundError as error:
        _LOGGER.warning(
            "Bash completion watcher skipped trigger for agent=%s process_session=%s: %s",
            agent_id,
            process_session_id,
            error,
        )
        return

    output = log_result.get("output", "")
    if not isinstance(output, str):
        output = ""

    user_cancelled = process_session_id in _user_cancelled_session_ids

    if user_cancelled:
        message = f"{BACKGROUND_USER_CANCELLED_MESSAGE}\nCommand: {command}\nOutput:\n{output}"
        _user_cancelled_session_ids.discard(process_session_id)
    else:
        message = (
            "Background process completed.\n"
            f"Command: {command}\n"
            f"Exit code: {session.exit_code}\n"
            "Output:\n"
            f"{output}"
        )

    await trigger_service.trigger_run(
        agent_id,
        message,
        session_id=chat_session_id,
        internal=True,
    )


def _maybe_spawn_completion_watcher(
    process_manager: ProcessManager,
    context: ToolContext,
    process_session_id: str,
    command: str,
    trigger_service: Any | None,
) -> None:
    if trigger_service is None:
        return

    task = asyncio.create_task(
        _watch_background_process(
            process_manager,
            process_session_id,
            context.agent_id,
            context.session_id,
            command,
            trigger_service,
        )
    )
    task.add_done_callback(
        lambda completed: _log_background_task_result(
            completed,
            f"Bash completion trigger failed for "
            f"agent={context.agent_id} session={context.session_id}",
        )
    )


def _register_user_cancel_callback(
    process_manager: ProcessManager,
    context: ToolContext,
    session_id: str,
) -> None:
    """Register a cancel callback that kills the spawned process and tags the session.

    The callback runs once when the runtime invokes it for a per-tool-call user
    cancel. It marks the session id in the bash module's local set so the
    background completion watcher can distinguish user-killed sessions from
    natural completion and tool-enforced timeouts. The kill coroutine is
    scheduled on the running event loop because the callback type is sync.
    """

    def cancel_callback() -> None:
        _user_cancelled_session_ids.add(session_id)
        kill_coro = process_manager.kill(session_id, context.agent_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(kill_coro)
        else:
            kill_task = loop.create_task(kill_coro)
            kill_task.add_done_callback(
                lambda completed: _log_background_task_result(
                    completed,
                    f"Bash user-cancel kill failed for "
                    f"agent={context.agent_id} session={session_id}",
                )
            )

    context.on_cancel(cancel_callback)


def _parse_arguments(arguments: JsonObject) -> JsonObject | str:
    unknown_arguments = set(arguments) - {
        "command",
        "workdir",
        "env",
        "yield_after",
        "background",
        "timeout",
    }
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        return f"Unknown argument(s): {names}"

    command = arguments.get("command")
    if not isinstance(command, str) or not command:
        return "command must be a non-empty string"

    workdir = arguments.get("workdir")
    if workdir is not None and not isinstance(workdir, str):
        return "workdir must be a string"

    background = arguments.get("background", False)
    if not isinstance(background, bool):
        return "background must be a boolean"

    try:
        yield_after = _coerce_non_negative_float(
            arguments.get("yield_after", DEFAULT_YIELD_AFTER_SECONDS),
            field_name="yield_after",
        )
        timeout = _coerce_optional_positive_float(arguments.get("timeout"), field_name="timeout")
    except ValueError as error:
        return str(error)

    env = arguments.get("env")
    if env is not None:
        if not isinstance(env, dict):
            return "env must be an object"
        for key, value in env.items():
            if not isinstance(key, str) or not key:
                return "env keys must be non-empty strings"
            if not isinstance(value, str):
                return "env values must be strings"

    return {
        "command": command,
        "workdir": workdir,
        "env": env,
        "yield_after": yield_after,
        "background": background,
        "timeout": timeout,
    }


def _coerce_non_negative_float(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a number")
    coerced = float(value)
    if coerced < 0:
        raise ValueError(f"{field_name} must be >= 0")
    return coerced


def _coerce_optional_positive_float(value: object, *, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a number")
    coerced = float(value)
    if coerced <= 0:
        raise ValueError(f"{field_name} must be > 0")
    return coerced


def _resolve_workdir(context: ToolContext, workdir: object) -> Path:
    if workdir is None:
        return context.workspace.resolve()

    candidate = Path(str(workdir)).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    return (context.workspace / candidate).resolve()


def _shell_argv(command: str) -> list[str]:
    if sys.platform == "win32":
        return ["pwsh", "-Command", command]
    return ["bash", "-c", command]


async def _build_process_env(overrides: object) -> dict[str, str]:
    env = await _get_shell_env()
    if overrides is None:
        return env

    assert isinstance(overrides, dict)
    for key, value in overrides.items():
        if key.upper() in BLOCKED_ENV_KEYS:
            continue
        env[key] = value
    return env


async def _get_shell_env() -> dict[str, str]:
    global _cached_shell_env

    if _cached_shell_env is None:
        _cached_shell_env = await _probe_shell_env()
    return dict(_cached_shell_env)


async def _probe_shell_env() -> dict[str, str]:
    try:
        if sys.platform == "win32":
            proc = await asyncio.create_subprocess_exec(
                "pwsh",
                "-NoProfile",
                "-Command",
                'Get-ChildItem Env: | ForEach-Object { "$($_.Name)=$($_.Value)" }',
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=_probe_creationflags(),
                start_new_session=_probe_start_new_session(),
            )
            stdout = await _communicate_with_probe_timeout(proc)
            if proc.returncode != 0:
                return os.environ.copy()
            return _parse_line_env(stdout.decode("utf-8", errors="replace"))

        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-l",
            "-c",
            "env -0",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=_probe_creationflags(),
            start_new_session=_probe_start_new_session(),
        )
        stdout = await _communicate_with_probe_timeout(proc)
        if proc.returncode != 0:
            return os.environ.copy()
        return _parse_null_env(stdout.decode("utf-8", errors="replace"))
    except (OSError, TimeoutError):
        return os.environ.copy()


def _probe_creationflags() -> int:
    if sys.platform == "win32":
        return subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


def _probe_start_new_session() -> bool:
    return sys.platform != "win32"


async def _communicate_with_probe_timeout(proc: asyncio.subprocess.Process) -> bytes:
    try:
        stdout, _stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=SHELL_ENV_PROBE_TIMEOUT_SECONDS,
        )
        return stdout
    except TimeoutError:
        await _terminate_probe_process(proc)
        raise


async def _terminate_probe_process(proc: asyncio.subprocess.Process) -> None:
    try:
        if sys.platform == "win32":
            try:
                completed = subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                taskkill_succeeded = completed.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                taskkill_succeeded = False

            if not taskkill_succeeded:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
        else:
            os.killpg(proc.pid, HARD_KILL_SIGNAL)
    except (OSError, ProcessLookupError):
        with contextlib.suppress(ProcessLookupError):
            proc.kill()

    try:
        await asyncio.wait_for(
            proc.communicate(),
            timeout=SHELL_ENV_PROBE_REAP_TIMEOUT_SECONDS,
        )
    except (ProcessLookupError, RuntimeError, TimeoutError):
        return


def _parse_line_env(output: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator and key:
            env[key] = value
    return env


def _parse_null_env(output: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in output.split("\0"):
        key, separator, value = item.partition("=")
        if separator and key:
            env[key] = value
    return env


def _schedule_timeout(
    process_manager: ProcessManager,
    session_id: str,
    agent_id: str,
    timeout: float | None,
) -> tuple[asyncio.Task[None] | None, dict[str, bool]]:
    state = {"timed_out": False}
    if timeout is None:
        return None, state

    async def kill_after_timeout() -> None:
        await asyncio.sleep(timeout)
        state["timed_out"] = True
        await process_manager.kill(session_id, agent_id)

    return asyncio.create_task(kill_after_timeout(), name=f"bash-timeout:{session_id}"), state


def _timed_out_process_killed(
    process_manager: ProcessManager,
    context: ToolContext,
    session_id: str,
) -> bool:
    """Confirm the timeout actually terminated a still-running process.

    The timer flag only records that the deadline elapsed; the kill it triggers
    is a no-op once the process has already exited. A process that finishes on
    its own a hair before the deadline keeps its completed/failed terminal
    status, while a genuine timeout kill leaves the session "killed". Reading
    that terminal status — not the timer flag alone — stops a race at the
    deadline from masking a successful run as a timeout.
    """
    session = process_manager.get_session(session_id, context.agent_id)
    return session.status == "killed"


async def _run_foreground_phase(
    process_manager: ProcessManager,
    context: ToolContext,
    session_id: str,
    yield_after: float,
) -> JsonObject:
    deadline = asyncio.get_running_loop().time() + yield_after

    while True:
        poll_result = await process_manager.poll(session_id, context.agent_id, timeout_ms=0)
        await _emit_output_chunks(context, session_id, poll_result)

        if poll_result["status"] != "running":
            return await _completion_result(process_manager, context, session_id)

        if context.is_cancelled() or asyncio.get_running_loop().time() >= deadline:
            return await _background_result(process_manager, context, session_id)

        sleep_seconds = min(
            FOREGROUND_POLL_INTERVAL_SECONDS, deadline - asyncio.get_running_loop().time()
        )
        if sleep_seconds > 0:
            await asyncio.sleep(sleep_seconds)


async def _emit_output_chunks(
    context: ToolContext,
    session_id: str,
    poll_result: JsonObject,
) -> None:
    chunks = poll_result.get("chunks", [])
    if not isinstance(chunks, list):
        return

    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        stream = chunk.get("stream")
        data = chunk.get("data")
        if stream not in {"stdout", "stderr"} or not isinstance(data, str) or not data:
            continue
        await context.emit(
            f"tool_call_{stream}",
            {
                "tool_call_id": context.tool_call_id,
                "session_id": session_id,
                "data": data,
            },
        )


async def _background_result(
    process_manager: ProcessManager,
    context: ToolContext,
    session_id: str,
) -> JsonObject:
    process_manager.mark_backgrounded(session_id, context.agent_id)
    output = await _combined_output(process_manager, context, session_id)
    return tool_success({"status": "running", "session_id": session_id, "output": output})


async def _completion_result(
    process_manager: ProcessManager,
    context: ToolContext,
    session_id: str,
) -> JsonObject:
    session = process_manager.get_session(session_id, context.agent_id)
    output = await _combined_output(process_manager, context, session_id)
    return tool_success(
        {
            "status": "completed",
            "exit_code": session.exit_code,
            "output": output,
            "truncated": session.truncated,
        }
    )


async def _combined_output(
    process_manager: ProcessManager,
    context: ToolContext,
    session_id: str,
) -> str:
    log_result = await process_manager.log(session_id, context.agent_id, offset=0, limit=None)
    output = log_result.get("output", "")
    return output if isinstance(output, str) else ""


__all__ = [
    "BASH_TOOL_DESCRIPTION",
    "BASH_TOOL_NAME",
    "BASH_TOOL_PARAMETERS",
    "bash_handler",
    "register_bash_tool",
]
