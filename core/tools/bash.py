"""Built-in bash tool backed by the shared process manager."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from core.tools.arguments import optional_number, optional_string
from core.tools.availability import bash_allowed_env_keys, normalize_env_keys
from core.tools.bash_hints import annotate_failure
from core.tools.process_manager import (
    ProcessManager,
    ProcessNotFoundError,
    TrackedProcess,
    subprocess_creation_flags,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayField,
    ToolPromptBlockRegistry,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.utils.logging import get_logger
from core.utils.paths import model_path

BASH_TOOL_NAME = "bash"
BASH_COMPLETION_STATUS_PREFIX = "### Bash process — "
BASH_COMPLETION_PROCESS_ID_PREFIX = "Process ID: "
CredentialResolver = Callable[[str], str]

# Model-facing output cap. The complete output always lands in the process log
# file, so the result only ever carries the newest slice; anything bigger is a
# context bomb (500 KiB of build output is >100k tokens in one tool message).
BASH_MODEL_OUTPUT_CAP_CHARS = 30_000
# Failure messages (timeout, sub-agent kill) carry a shorter tail: enough to
# diagnose, small enough not to bloat an error envelope.
FAILURE_OUTPUT_TAIL_CHARS = 10_000
BASH_HANDOFF_PROCESS_NOTE = (
    "Use process_id with the process Tool for status, raw stdin input, or kill. Process input "
    "writes to a pipe; it does not provide a terminal or TTY. output is the newest capped "
    "snapshot collected before handoff. The result's log_file field carries the path to the "
    "complete combined stdout/stderr stream, written live from command start through exit."
)
DEFAULT_BACKGROUND_AFTER_SECONDS = 30.0
# Inside a Sub-Agent auto mode cannot hand off, so its background_after_seconds
# threshold doubles as the kill deadline. Default it generously there: a 30s
# handoff would kill a normal pytest/build. Explicit background_after_seconds
# or timeout still wins, and the Sub-Agent Run timeout is the outer bound.
DEFAULT_SUBAGENT_BACKGROUND_AFTER_SECONDS = 1800.0


def _shell_syntax_notes() -> str:
    """Name the actual shell so the model writes matching syntax.

    The tool is called "bash", so without this a model on Windows guesses cmd or
    bash syntax. Mirrors the platform branch in ``_shell_argv``; constant per host,
    so provider prompt caching is unaffected.
    """
    if sys.platform == "win32":
        return (
            " Commands use PowerShell 7 (pwsh), not cmd or bash: use $env:VAR, redirect "
            "stderr with 2>$null, and assign environment variables separately. PowerShell "
            "is non-interactive and its stdin never reaches EOF: Read-Host is unavailable; "
            "$input in double-quoted strings expands and hangs the command forever "
            "(single-quote it or escape as `$input); raw stdin requires "
            "[Console]::In.ReadLine() or a native child process. The outer shell is "
            "already PowerShell — pipe into cmdlets directly instead of a nested "
            "pwsh -Command."
        )
    return " Commands run in bash on this host."


BASH_TOOL_DESCRIPTION = (
    "Run an unattended shell command on the host through pipes when no interactive terminal "
    "input or live screen is needed, such as scripts, builds, non-interactive Git, file "
    "operations, and servers. Use foreground when this Run needs the result, auto to wait "
    "before handing off a still-running command, and background for known long-lived commands. "
    "Handed-off commands are monitored automatically: continue independent work or end the Run "
    "instead of polling or starting another copy. Never manually detach or daemonize a command "
    "because that bypasses vBot's process ownership. Result output keeps the newest "
    f"{BASH_MODEL_OUTPUT_CAP_CHARS} characters; when output is truncated or a command is handed "
    "off, the result includes a log_file path to the complete combined stdout/stderr stream — "
    "read or grep it for the full output. A non-zero exit code returns an additional `hint` "
    "field when a well-known failure shape was recognized — follow it instead of retrying "
    "blindly." + _shell_syntax_notes()
)
BASH_SUBAGENT_TOOL_DESCRIPTION = (
    "Run an unattended shell command inside this Sub-Agent through pipes when no interactive "
    "terminal input or live screen is needed, such as scripts, builds, non-interactive Git, and "
    "file operations; process handoff is unavailable. Use foreground to wait for completion and "
    "auto only for bounded work; auto kills a command still running after "
    "background_after_seconds. Never "
    "manually detach or daemonize a command. Result output keeps the newest "
    f"{BASH_MODEL_OUTPUT_CAP_CHARS} characters; when output is truncated, the result includes a "
    "log_file path to the complete combined stdout/stderr stream." + _shell_syntax_notes()
)
DEFAULT_EXECUTION_MODE = "foreground"
BASH_EXECUTION_MODES = (DEFAULT_EXECUTION_MODE, "auto", "background")
VBOT_RUN_AGENT_ID_ENV = "VBOT_RUN_AGENT_ID"
VBOT_RUN_SESSION_ID_ENV = "VBOT_RUN_SESSION_ID"
VBOT_RUN_PROJECT_ID_ENV = "VBOT_RUN_PROJECT_ID"
_BASH_COMMAND_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": "Shell command to run.",
}
_BASH_DESCRIPTION_PARAMETER: JsonObject = {
    "type": "string",
    "description": (
        "Short 3–5 word title for the command’s purpose. Omit when the command is self-explanatory."
    ),
}
_BASH_WORKDIR_PARAMETER: JsonObject = {
    "type": "string",
    "description": (
        "Directory to run in. Omit to use the working directory; a relative path resolves from it."
    ),
}
_BASH_TIMEOUT_PARAMETER: JsonObject = {
    "type": "number",
    "exclusiveMinimum": 0,
    "description": (
        "Hard kill deadline in seconds. Omit for no Tool-level timeout; in auto mode it does "
        "not extend background_after_seconds."
    ),
}
_BASH_ENV_KEYS_PARAMETER: JsonObject = {
    "type": "array",
    "description": (
        "Exact names of granted environment credentials to make available to the command. "
        "Omit when no credential is needed."
    ),
    "items": {"type": "string", "minLength": 1},
    "minItems": 1,
    "uniqueItems": True,
}


def _bash_tool_parameters(*, subagent: bool) -> JsonObject:
    background_after_default = (
        DEFAULT_SUBAGENT_BACKGROUND_AFTER_SECONDS if subagent else DEFAULT_BACKGROUND_AFTER_SECONDS
    )
    mode_description = (
        "Execution behavior. Omit for foreground, which waits for completion; auto waits until "
        "background_after_seconds, "
        "then kills a still-running command because handoff is unavailable — "
        "background_after_seconds applies only to auto."
        if subagent
        else "Execution behavior. Omit for foreground, which waits for completion; auto waits "
        "until background_after_seconds, then hands a still-running command off to vBot; "
        "background hands off immediately. background_after_seconds applies only to auto."
    )
    background_after_description = (
        'Only valid when mode is "auto". Seconds auto waits before the command is killed '
        "because process handoff is unavailable. Omit for the default (30 minutes); "
        "independent of timeout."
        if subagent
        else 'Only valid when mode is "auto". Seconds auto waits before a still-running '
        "command is handed to vBot. The command keeps running past this point — this is "
        "not a timeout; it only ends the synchronous wait. Omit for the default "
        "(30 seconds); independent of timeout."
    )
    modes = BASH_EXECUTION_MODES[:2] if subagent else BASH_EXECUTION_MODES
    return {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": list(modes),
                "description": mode_description,
            },
            "command": _BASH_COMMAND_PARAMETER,
            "description": _BASH_DESCRIPTION_PARAMETER,
            "workdir": _BASH_WORKDIR_PARAMETER,
            "background_after_seconds": {
                "type": "number",
                "minimum": 0,
                "description": background_after_description,
                "default": background_after_default,
            },
            "timeout": _BASH_TIMEOUT_PARAMETER,
            "env_keys": _BASH_ENV_KEYS_PARAMETER,
        },
        "required": ["command"],
    }


BASH_TOOL_PARAMETERS = _bash_tool_parameters(subagent=False)
BASH_SUBAGENT_TOOL_PARAMETERS = _bash_tool_parameters(subagent=True)

FOREGROUND_POLL_INTERVAL_SECONDS = 0.05
SHELL_ENV_PROBE_TIMEOUT_SECONDS = 5.0
SHELL_ENV_PROBE_REAP_TIMEOUT_SECONDS = 1.0
# A freshly installed program does not appear in a shell spawned by Bash until
# the cached environment is re-probed. On Windows the vBot process never sees
# the registry-broadcast PATH change, and the one-time probe inherits that
# frozen PATH, so without re-probing the only remedy was a full restart. The
# TTL bounds staleness automatically; an explicit invalidation is exposed for
# the runtime and for the spawn-failure safety-net.
SHELL_ENV_CACHE_TTL_SECONDS = 300.0
HARD_KILL_SIGNAL = getattr(signal, "SIGKILL", 9)
USER_CANCELLED_FAILURE_CODE = "cancelled_by_user"
USER_CANCELLED_FAILURE_MESSAGE = "Command aborted by the user"
RUN_CANCELLED_FAILURE_CODE = "run_cancelled"
RUN_CANCELLED_FAILURE_MESSAGE = "Command stopped because the owning Run was cancelled"
BACKGROUND_USER_CANCELLED_MESSAGE = "Background process was aborted by the user."

# Handoff-at-depth block: a Sub-Agent (nesting depth >= 1) runs in an ephemeral
# Session that nobody reads once it returns its single result, so a handed-off
# process there could not report back. Background mode is rejected before spawn;
# auto mode kills and reports failure if it reaches background_after_seconds. No process is
# left running and no completion watcher is spawned. Top level is unaffected.
# FLIP-BACK: set BLOCK_BACKGROUND_AT_DEPTH = False to allow background bash at depth.
BLOCK_BACKGROUND_AT_DEPTH = True
BACKGROUND_AT_DEPTH_FAILURE_CODE = "background_unavailable_in_subagent"
BACKGROUND_AT_DEPTH_EXPLICIT_MESSAGE = (
    "Background mode is not available inside a Sub-Agent: its Session ends with this Run, "
    "so a handed-off process could not report back. Use foreground mode, or auto mode with "
    "a sufficient background_after_seconds and optional timeout."
)

_LOGGER = get_logger("tools.bash")

_cached_shell_env: dict[str, str] | None = None
_shell_env_cache_time: float = 0.0
_shell_env_probe_task: asyncio.Task[dict[str, str]] | None = None


def reset_shell_env_cache() -> None:
    """Invalidate the cached shell environment so the next Bash call re-probes.

    Exposed as the seam behind ``Runtime.reload_shell_env()``. After a program
    is installed (or PATH otherwise mutates outside vBot), a call here makes the
    next Bash command see the fresh environment without restarting the server.
    """
    global _cached_shell_env, _shell_env_cache_time, _shell_env_probe_task
    _cached_shell_env = None
    _shell_env_cache_time = 0.0
    # An in-flight probe is harmless to leave running — it still populates the
    # cache when it finishes, and the TTL check on the next call will refresh
    # again if the result is already stale by then.


def project_bash_tool_definitions(
    definitions: list[JsonObject],
    *,
    nesting_depth: int,
) -> list[JsonObject]:
    """Narrow Bash's Provider definition to the execution modes valid at this depth."""
    if nesting_depth < 1 or not BLOCK_BACKGROUND_AT_DEPTH:
        return definitions

    projected: list[JsonObject] = []
    for definition in definitions:
        if definition.get("name") != BASH_TOOL_NAME:
            projected.append(definition)
            continue
        narrowed = deepcopy(definition)
        narrowed["description"] = BASH_SUBAGENT_TOOL_DESCRIPTION
        narrowed["parameters"] = deepcopy(BASH_SUBAGENT_TOOL_PARAMETERS)
        projected.append(narrowed)
    return projected


def _background_blocked_at_depth(context: ToolContext) -> bool:
    """Return whether process handoff is blocked for this Sub-Agent call."""
    return BLOCK_BACKGROUND_AT_DEPTH and context.nesting_depth >= 1


def _background_at_depth_timeout_message(background_after_seconds: float) -> str:
    """Build the failure message for Sub-Agent auto mode reaching the threshold."""
    return (
        f"Auto waited the full background_after_seconds window ({background_after_seconds:g} s) "
        "and the command was still running, but process handoff is not "
        "available inside a Sub-Agent. The process was stopped. Use foreground mode when "
        "the next action needs this result, or choose a sufficient "
        "background_after_seconds and timeout "
        "for bounded independent work."
    )


def _resolve_background_after_seconds(context: ToolContext, explicit: float | None) -> float:
    """Resolve auto mode's inline wait: explicit wins, else a per-context default.

    Inside a Sub-Agent the command cannot be handed off, so the window is the max
    runtime before a kill; default it generously there. Top level keeps the short
    handoff default.
    """
    if explicit is not None:
        return explicit
    if _background_blocked_at_depth(context):
        return DEFAULT_SUBAGENT_BACKGROUND_AFTER_SECONDS
    return DEFAULT_BACKGROUND_AFTER_SECONDS


async def bash_handler(
    context: ToolContext,
    arguments: JsonObject,
    process_manager: ProcessManager,
    trigger_service: Any | None = None,
    credential_resolver: CredentialResolver | None = None,
) -> JsonObject:
    """Run a shell command and return a stable tool result envelope."""
    parsed = _parse_arguments(arguments)
    if isinstance(parsed, str):
        return tool_failure("invalid_arguments", parsed)

    # A sub-agent cannot park a background process for a later run, so reject an
    # explicit background request before spawning anything (no process, no watcher).
    mode = str(parsed["mode"])
    if mode == "background" and _background_blocked_at_depth(context):
        return tool_failure(
            BACKGROUND_AT_DEPTH_FAILURE_CODE,
            BACKGROUND_AT_DEPTH_EXPLICIT_MESSAGE,
        )

    command = parsed["command"]
    workdir = _resolve_workdir(context, parsed.get("workdir"))
    requested_env_keys = parsed["env_keys"]
    allowed_env_keys = set(bash_allowed_env_keys(context.tool_settings)) | set(
        context.skill_env_keys
    )
    unauthorized_env_keys = [key for key in requested_env_keys if key not in allowed_env_keys]
    if unauthorized_env_keys:
        names = ", ".join(unauthorized_env_keys)
        return tool_failure(
            "invalid_arguments",
            f"env_keys contains key(s) not granted to this Agent: {names}",
        )
    env = await _get_shell_env()
    resolve_credential = credential_resolver or (lambda key: os.environ.get(key, ""))
    for key in requested_env_keys:
        env[key] = resolve_credential(key)
    env[VBOT_RUN_AGENT_ID_ENV] = context.agent_id
    env[VBOT_RUN_SESSION_ID_ENV] = context.session_id
    if context.project_id is None:
        env.pop(VBOT_RUN_PROJECT_ID_ENV, None)
    else:
        env[VBOT_RUN_PROJECT_ID_ENV] = context.project_id
    argv = _shell_argv(command)

    try:
        process_id = await process_manager.spawn(
            context.run_id,
            context.agent_id,
            argv,
            env=env,
            cwd=workdir,
        )
    except FileNotFoundError:
        # The shell binary itself (pwsh/bash) was not found. This is not a
        # user-command failure — it means the shell executable disappeared or
        # PATH is stale. Re-probe the environment once in case PATH changed,
        # then retry the spawn before giving up.
        _LOGGER.info(
            "Shell spawn failed with FileNotFoundError; refreshing shell "
            "environment cache and retrying once.",
        )
        reset_shell_env_cache()
        env = await _get_shell_env()
        for key in requested_env_keys:
            env[key] = resolve_credential(key)
        env[VBOT_RUN_AGENT_ID_ENV] = context.agent_id
        env[VBOT_RUN_SESSION_ID_ENV] = context.session_id
        if context.project_id is None:
            env.pop(VBOT_RUN_PROJECT_ID_ENV, None)
        else:
            env[VBOT_RUN_PROJECT_ID_ENV] = context.project_id
        try:
            process_id = await process_manager.spawn(
                context.run_id,
                context.agent_id,
                argv,
                env=env,
                cwd=workdir,
            )
        except (OSError, ValueError) as error:
            return tool_failure("process_spawn_failed", _spawn_failure_message(argv, error))
    except (OSError, ValueError) as error:
        return tool_failure("process_spawn_failed", _spawn_failure_message(argv, error))

    _register_user_cancel_callback(process_manager, context, process_id)

    timeout_task, timeout_state = _schedule_timeout(
        process_manager,
        process_id,
        context.agent_id,
        parsed.get("timeout"),
    )

    if mode == "background":
        result = await _background_result(
            process_manager,
            context,
            process_id,
            mode=mode,
            handoff_after=None,
        )
        _maybe_spawn_completion_watcher(
            process_manager,
            context,
            process_id,
            command,
            trigger_service,
        )
        return result

    background_after_seconds = (
        _resolve_background_after_seconds(context, parsed["background_after_seconds"])
        if mode == "auto"
        else None
    )
    result = await _run_foreground_phase(
        process_manager,
        context,
        process_id,
        background_after_seconds,
        mode=mode,
        command=command,
    )

    if context.is_cancelled() or context.was_cancelled_by_user():
        if timeout_task is not None:
            timeout_task.cancel()
        await process_manager.kill(process_id, context.agent_id)
        if context.was_cancelled_by_user():
            return tool_failure(USER_CANCELLED_FAILURE_CODE, USER_CANCELLED_FAILURE_MESSAGE)
        return tool_failure(RUN_CANCELLED_FAILURE_CODE, RUN_CANCELLED_FAILURE_MESSAGE)

    if result["data"] is not None and result["data"].get("status") == "running":
        # At depth auto mode outran background_after_seconds but a Sub-Agent cannot hand off
        # the process: kill and fail instead of spawning a watcher.
        if _background_blocked_at_depth(context):
            if timeout_task is not None:
                timeout_task.cancel()
            await process_manager.kill(process_id, context.agent_id)
            suffix = await _failure_output_suffix(process_manager, context, process_id)
            if background_after_seconds is None:
                raise RuntimeError("only auto mode may reach the Sub-Agent handoff boundary")
            return tool_failure(
                BACKGROUND_AT_DEPTH_FAILURE_CODE,
                _background_at_depth_timeout_message(background_after_seconds) + suffix,
            )
        _maybe_spawn_completion_watcher(
            process_manager,
            context,
            process_id,
            command,
            trigger_service,
        )
        return result

    if timeout_task is not None:
        timeout_task.cancel()

    if timeout_state["timed_out"] and _timed_out_process_killed(
        process_manager, context, process_id
    ):
        suffix = await _failure_output_suffix(process_manager, context, process_id)
        return tool_failure(
            "process_timeout",
            f"process timed out after {parsed['timeout']} seconds" + suffix,
        )

    return result


def register_bash_tool(
    registry: ToolRegistry,
    process_manager: ProcessManager,
    trigger_service: Any | None = None,
    *,
    credential_resolver: CredentialResolver | None = None,
    prompt_blocks: ToolPromptBlockRegistry | None = None,
) -> None:
    """Register the bash tool with a vBot tool registry."""

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await bash_handler(
            context,
            arguments,
            process_manager,
            trigger_service=trigger_service,
            credential_resolver=credential_resolver,
        )

    registry.register(
        BASH_TOOL_NAME,
        BASH_TOOL_DESCRIPTION,
        BASH_TOOL_PARAMETERS,
        handler,
        family="execution",
        open_input_schema=True,
        result_schema={"type": "object", "required": ["status"]},
        display=ToolDisplay(
            primary_candidates=(
                ToolDisplayField("description", kind="description", quote=True),
                ToolDisplayField("command", kind="command"),
            )
        ),
    )
    if prompt_blocks is not None:
        prompt_blocks.register(
            BASH_TOOL_NAME,
            render=_render_bash_env_prompt_block,
        )


def format_bash_env_usage(env_keys: Sequence[str], *, intro: str) -> str:
    """Render the shared model-facing contract for granted Bash credentials."""
    keys = list(dict.fromkeys(env_keys))
    if not keys:
        return ""
    key_lines = "\n".join(f"- `{key}`" for key in keys)
    return (
        f"{intro}\n\n"
        f"Available environment keys:\n{key_lines}\n\n"
        "To use one, include its exact name in the `env_keys` array of every `bash` call "
        "that needs it. vBot resolves the value server-side and injects it only into that "
        "process environment; put the name, never the credential value, in the Tool call. "
        "Refer to the variable with the current host shell's environment syntax and do not "
        "print or otherwise expose its value."
    )


def _render_bash_env_prompt_block(context: Any) -> str:
    env_keys = bash_allowed_env_keys(getattr(context.agent, "tools", None))
    if not env_keys:
        return ""
    guidance = format_bash_env_usage(
        env_keys,
        intro="This Agent has permanent permission to use these credentials in Bash calls.",
    )
    return f"## Bash Environment Access\n\n{guidance}"


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
    process_id: str,
    agent_id: str,
    chat_session_id: str,
    origin_run_id: str,
    command: str,
    trigger_service: Any,
    project_id: str | None = None,
) -> None:
    try:
        tracked = process_manager.get_process(process_id, agent_id)
        wait_task = tracked.wait_task
        if wait_task is not None:
            await wait_task
        else:
            while process_manager.get_process(process_id, agent_id).status == "running":
                await asyncio.sleep(FOREGROUND_POLL_INTERVAL_SECONDS)
    except ProcessNotFoundError as error:
        _LOGGER.warning(
            "Bash completion watcher skipped trigger for agent=%s process=%s: %s",
            agent_id,
            process_id,
            error,
        )
        return

    try:
        log_result = await process_manager.log(process_id, agent_id)
        tracked = process_manager.get_process(process_id, agent_id)
    except ProcessNotFoundError as error:
        _LOGGER.warning(
            "Bash completion watcher skipped trigger for agent=%s process=%s: %s",
            agent_id,
            process_id,
            error,
        )
        return

    output = log_result.get("output", "")
    if not isinstance(output, str):
        output = ""
    # The automatic note lands in the model's context like a tool result, so
    # the same output cap and full-log pointer apply here.
    output = str(_shape_output_fields(tracked, output)["output"])

    user_cancelled = tracked.cancelled_by_user

    if user_cancelled:
        body = (
            f"{BASH_COMPLETION_STATUS_PREFIX}aborted by user\n"
            f"{BASH_COMPLETION_PROCESS_ID_PREFIX}{process_id}\n"
            f"{BACKGROUND_USER_CANCELLED_MESSAGE}\n"
            f"Command: {command}\n"
            "Output:\n"
            f"{output}"
        )
    else:
        body = (
            f"{BASH_COMPLETION_STATUS_PREFIX}{tracked.status}\n"
            f"{BASH_COMPLETION_PROCESS_ID_PREFIX}{process_id}\n"
            f"Command: {command}\n"
            f"Exit code: {tracked.exit_code}\n"
            "Output:\n"
            f"{output}"
        )
        hint = annotate_failure(command, tracked.exit_code, output)
        if hint:
            body += f"\n\nHint: {hint}"

    notice_id = f"bash:{process_id}"
    delivery = trigger_service.submit_completion(
        agent_id,
        chat_session_id,
        notice_id=notice_id,
        origin_run_id=origin_run_id,
        body=body,
        project_id=project_id,
    )
    try:
        await delivery
    except asyncio.CancelledError:
        trigger_service.cancel_completion(
            agent_id,
            chat_session_id,
            notice_id=notice_id,
            project_id=project_id,
        )
        raise


def _maybe_spawn_completion_watcher(
    process_manager: ProcessManager,
    context: ToolContext,
    process_id: str,
    command: str,
    trigger_service: Any | None,
) -> None:
    if trigger_service is None:
        return

    task = asyncio.create_task(
        _watch_background_process(
            process_manager,
            process_id,
            context.agent_id,
            context.session_id,
            context.run_id,
            command,
            trigger_service,
            project_id=context.project_id,
        )
    )
    try:
        process_manager.register_completion_notification(
            process_id,
            context.agent_id,
            task,
        )
    except Exception:
        task.cancel()
        raise
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
    process_id: str,
) -> None:
    """Register a cancel callback that kills the spawned process and tags its record.

    Every owning-Run cancellation kills the process. A user cancellation also
    marks the process as user-cancelled so any already-handed-off completion report can
    use explicit user-abort wording. The kill coroutine is scheduled on the
    running event loop because the callback type is synchronous.
    """

    def cancel_callback() -> None:
        kill_coro = (
            process_manager.cancel_for_user(process_id, context.agent_id)
            if context.was_cancelled_by_user()
            else process_manager.kill(process_id, context.agent_id)
        )
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
                    f"agent={context.agent_id} process={process_id}",
                )
            )

    context.on_cancel(cancel_callback)


def _parse_arguments(arguments: JsonObject) -> JsonObject | str:
    unknown_arguments = set(arguments) - {
        "command",
        "description",
        "mode",
        "workdir",
        "background_after_seconds",
        "timeout",
        "env_keys",
    }
    if unknown_arguments:
        names = ", ".join(sorted(unknown_arguments))
        return f"Unknown argument(s): {names}"

    command = arguments.get("command")
    if not isinstance(command, str) or not command:
        return "command must be a non-empty string"

    mode = arguments.get("mode", DEFAULT_EXECUTION_MODE)
    if not isinstance(mode, str) or mode not in BASH_EXECUTION_MODES:
        return "mode must be one of: foreground, auto, background"
    if mode != "auto" and "background_after_seconds" in arguments:
        return "background_after_seconds is only valid when mode is auto"

    try:
        workdir = optional_string(arguments.get("workdir"), field_name="workdir")
        optional_string(arguments.get("description"), field_name="description")
        background_after_seconds = optional_number(
            arguments.get("background_after_seconds"),
            field_name="background_after_seconds",
            default=None,
            minimum=0,
        )
        timeout = optional_number(
            arguments.get("timeout"),
            field_name="timeout",
            minimum=0,
            minimum_exclusive=True,
        )
        env_keys = normalize_env_keys(
            arguments.get("env_keys", []),
            field_name="env_keys",
        )
    except ValueError as error:
        return str(error)

    return {
        "command": command,
        "mode": mode,
        "workdir": workdir,
        "background_after_seconds": background_after_seconds,
        "timeout": timeout,
        "env_keys": env_keys,
    }


def _resolve_workdir(context: ToolContext, workdir: object) -> Path:
    if workdir is None:
        return context.effective_cwd.resolve()

    return context.resolve_path(str(workdir))


def _shell_argv(command: str) -> list[str]:
    if sys.platform == "win32":
        # stdin stays open so backgrounded native programs can still receive
        # input through the process tool. Prevent PowerShell itself from
        # treating that pipe as an interactive host after a command failure.
        return ["pwsh", "-NonInteractive", "-Command", command]
    return ["bash", "-c", command]


async def _get_shell_env() -> dict[str, str]:
    global _cached_shell_env, _shell_env_cache_time, _shell_env_probe_task

    if _cached_shell_env is not None and _is_shell_env_cache_fresh():
        return dict(_cached_shell_env)

    if _cached_shell_env is None:
        # First-ever probe — same concurrent-dedup logic as before.
        probe_task = _shell_env_probe_task
        if probe_task is None:
            probe_task = asyncio.create_task(_probe_shell_env())
            _shell_env_probe_task = probe_task
        try:
            probed_env = await asyncio.shield(probe_task)
        except asyncio.CancelledError:
            if probe_task.cancelled() and _shell_env_probe_task is probe_task:
                _shell_env_probe_task = None
            raise
        except BaseException:
            if _shell_env_probe_task is probe_task:
                _shell_env_probe_task = None
            raise
        if _cached_shell_env is None:
            _cached_shell_env = probed_env
            _shell_env_cache_time = time.monotonic()
        if _shell_env_probe_task is probe_task:
            _shell_env_probe_task = None
    else:
        # Cache exists but is stale — re-probe directly. The TTL makes this
        # infrequent (default 5 min), and concurrent callers each get their own
        # refresh copy. An in-flight first-probe task is left untouched.
        _cached_shell_env = await _probe_shell_env()
        _shell_env_cache_time = time.monotonic()

    return dict(_cached_shell_env)


def _is_shell_env_cache_fresh() -> bool:
    if SHELL_ENV_CACHE_TTL_SECONDS <= 0:
        return True
    return (time.monotonic() - _shell_env_cache_time) < SHELL_ENV_CACHE_TTL_SECONDS


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
                return _overlay_registry_path(os.environ.copy())
            env = _parse_line_env(stdout.decode("utf-8", errors="replace"))
            return _overlay_registry_path(env)

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
        if sys.platform == "win32":
            return _overlay_registry_path(os.environ.copy())
        return os.environ.copy()


def _probe_creationflags() -> int:
    return subprocess_creation_flags(new_process_group=True)


def _probe_start_new_session() -> bool:
    return sys.platform != "win32"


def _overlay_registry_path(env: dict[str, str]) -> dict[str, str]:
    """Overlay the current Windows registry PATH onto a probed environment.

    On Windows, a headless process never receives the ``WM_SETTINGCHANGE``
    broadcast that follows a PATH edit, so ``os.environ['PATH']`` — and thus
    the probe — stays frozen at vBot start time. Reading the machine and user
    PATH values directly from the registry gives the probe the live value
    without a restart, mirroring what Chocolatey's ``refreshenv`` does.
    """
    registry_path = _read_registry_path()
    if registry_path is not None:
        env["PATH"] = registry_path
    return env


def _read_registry_path() -> str | None:
    """Read the effective PATH from the Windows registry (machine + user).

    Returns ``None`` when the registry cannot be read, so the caller keeps the
    probed value unchanged instead of clobbering it with an empty string.
    """
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None

    segments: list[str] = []
    with (
        contextlib.suppress(OSError),
        winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ) as key,
    ):
        machine_path, _regtype = winreg.QueryValueEx(key, "PATH")
        if machine_path:
            segments.append(machine_path)
    with (
        contextlib.suppress(OSError),
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key,
    ):
        user_path, _regtype = winreg.QueryValueEx(key, "PATH")
        if user_path:
            segments.append(user_path)

    if not segments:
        return None
    return ";".join(segments)


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
                    creationflags=subprocess_creation_flags(),
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
    process_id: str,
    agent_id: str,
    timeout: float | None,
) -> tuple[asyncio.Task[None] | None, dict[str, bool]]:
    state = {"timed_out": False}
    if timeout is None:
        return None, state

    async def kill_after_timeout() -> None:
        await asyncio.sleep(timeout)
        state["timed_out"] = True
        await process_manager.kill(process_id, agent_id)

    return asyncio.create_task(kill_after_timeout(), name=f"bash-timeout:{process_id}"), state


def _timed_out_process_killed(
    process_manager: ProcessManager,
    context: ToolContext,
    process_id: str,
) -> bool:
    """Confirm the timeout actually terminated a still-running process.

    The timer flag only records that the deadline elapsed; the kill it triggers
    is a no-op once the process has already exited. A process that finishes on
    its own a hair before the deadline keeps its completed/failed terminal
    status, while a genuine timeout kill leaves the process "killed". Reading
    that terminal status — not the timer flag alone — stops a race at the
    deadline from masking a successful run as a timeout.
    """
    tracked = process_manager.get_process(process_id, context.agent_id)
    return tracked.status == "killed"


async def _run_foreground_phase(
    process_manager: ProcessManager,
    context: ToolContext,
    process_id: str,
    background_after_seconds: float | None,
    *,
    mode: str,
    command: str,
) -> JsonObject:
    deadline = (
        asyncio.get_running_loop().time() + background_after_seconds
        if background_after_seconds is not None
        else None
    )

    while True:
        poll_result = await process_manager.poll(process_id, context.agent_id, timeout_ms=0)
        await _emit_output_chunks(context, process_id, poll_result)

        if poll_result["status"] != "running":
            return await _completion_result(
                process_manager,
                context,
                process_id,
                mode=mode,
                command=command,
            )

        if context.is_cancelled():
            await process_manager.kill(process_id, context.agent_id)
            return await _completion_result(
                process_manager,
                context,
                process_id,
                mode=mode,
                command=command,
            )
        if deadline is not None and asyncio.get_running_loop().time() >= deadline:
            return await _background_result(
                process_manager,
                context,
                process_id,
                mode=mode,
                handoff_after=background_after_seconds,
            )

        sleep_seconds = FOREGROUND_POLL_INTERVAL_SECONDS
        if deadline is not None:
            sleep_seconds = min(
                sleep_seconds,
                deadline - asyncio.get_running_loop().time(),
            )
        if sleep_seconds > 0:
            await asyncio.sleep(sleep_seconds)


async def _emit_output_chunks(
    context: ToolContext,
    process_id: str,
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
                "process_id": process_id,
                "data": data,
            },
        )


async def _background_result(
    process_manager: ProcessManager,
    context: ToolContext,
    process_id: str,
    *,
    mode: str,
    handoff_after: float | None,
) -> JsonObject:
    process_manager.mark_backgrounded(process_id, context.agent_id)
    tracked = process_manager.get_process(process_id, context.agent_id)
    output = await _combined_output(process_manager, context, process_id)
    fields = _shape_output_fields(tracked, output)
    if tracked.log_file is not None:
        # A background process keeps writing after this result; always hand the
        # model the log path so it can grep progress without polling.
        fields["log_file"] = model_path(tracked.log_file)
    result: JsonObject = {
        "status": "running",
        "process_id": process_id,
        "mode": mode,
        **fields,
    }
    result["delivery"] = "automatic"
    result["handoff_note"] = _handoff_note(mode, handoff_after)
    result["process_note"] = BASH_HANDOFF_PROCESS_NOTE
    return tool_success(result)


def background_bash_statuses(messages: Sequence[Any]) -> JsonObject:
    """Fold durable Bash and Process results into background-process statuses.

    Background Bash results are immutable handoff records, so their terminal
    state arrives later in either an automatic completion note or a manually
    persisted Process Tool Result. The WebUI history response uses this folded
    projection without exposing internal notes themselves.
    """
    statuses: JsonObject = {}
    for message in messages:
        role = getattr(message, "role", None)
        content = getattr(message, "content", None)
        if role == "tool" and isinstance(content, str):
            _fold_background_tool_status(
                statuses,
                getattr(message, "name", None),
                content,
            )
        elif role == "note" and isinstance(content, str):
            _fold_background_completion_note(statuses, content)
    return statuses


def _fold_background_tool_status(statuses: JsonObject, tool_name: Any, content: str) -> None:
    try:
        envelope = json.loads(content)
    except json.JSONDecodeError:
        return
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        return
    data = envelope.get("data")
    if not isinstance(data, dict):
        return

    if tool_name == BASH_TOOL_NAME:
        if data.get("delivery") != "automatic":
            return
        _record_background_status(statuses, data)
        return
    if tool_name != "process":
        return

    _record_background_status(statuses, data)
    processes = data.get("processes")
    if isinstance(processes, list):
        for tracked in processes:
            if isinstance(tracked, dict):
                _record_background_status(statuses, tracked)


def _record_background_status(statuses: JsonObject, data: JsonObject) -> None:
    process_id = data.get("process_id")
    status = data.get("status")
    if (
        isinstance(process_id, str)
        and process_id
        and status
        in {
            "running",
            "completed",
            "failed",
            "killed",
            "cancelled",
        }
    ):
        statuses[process_id] = status


def _fold_background_completion_note(statuses: JsonObject, content: str) -> None:
    pending_status: str | None = None
    for line in content.splitlines():
        if line.startswith(BASH_COMPLETION_STATUS_PREFIX):
            pending_status = _completion_process_status(
                line.removeprefix(BASH_COMPLETION_STATUS_PREFIX)
            )
            continue
        if pending_status is None or not line.startswith(BASH_COMPLETION_PROCESS_ID_PREFIX):
            continue
        process_id = line.removeprefix(BASH_COMPLETION_PROCESS_ID_PREFIX).strip()
        if process_id:
            statuses[process_id] = pending_status
        pending_status = None


def _completion_process_status(status: str) -> str | None:
    normalized = status.strip().lower()
    if normalized == "aborted by user":
        return "cancelled"
    if normalized in {"completed", "failed", "killed"}:
        return normalized
    return None


async def _completion_result(
    process_manager: ProcessManager,
    context: ToolContext,
    process_id: str,
    *,
    mode: str,
    command: str,
) -> JsonObject:
    tracked = process_manager.get_process(process_id, context.agent_id)
    output = await _combined_output(process_manager, context, process_id)
    result: JsonObject = {
        "status": "completed",
        "exit_code": tracked.exit_code,
        "mode": mode,
        **_shape_output_fields(tracked, output),
    }
    hint = annotate_failure(command, tracked.exit_code, output)
    if hint:
        result["hint"] = hint
    return tool_success(result)


def _handoff_note(mode: str, handoff_after: float | None) -> str:
    if mode == "auto" and handoff_after is not None:
        transition = (
            "The command is still running and has been handed off to vBot after "
            f"{handoff_after:g} seconds."
        )
    else:
        transition = "The command is still running and has been handed off to vBot immediately."
    return (
        f"{transition} vBot will monitor it and deliver its terminal result automatically "
        "in one coalesced follow-up Run. You may continue work that does not depend on "
        "this result, or finish the current Run now. Do not poll merely to wait, and do "
        "not start another copy of the command. If your next action depends on the "
        "result, inspect the process explicitly or use foreground mode next time."
    )


def _shape_output_fields(tracked: TrackedProcess, output: str) -> JsonObject:
    """Cap model-facing output to the newest chars and point at the full log.

    ``truncated`` covers both cut points: the model cap applied here and the
    process buffer cap that already dropped the oldest bytes in memory. Either
    way the missing part is the beginning, and the marker says so.
    """
    log_file = model_path(tracked.log_file) if tracked.log_file is not None else None
    capped = len(output) > BASH_MODEL_OUTPUT_CAP_CHARS
    truncated = capped or tracked.truncated
    if capped:
        output = output[-BASH_MODEL_OUTPUT_CAP_CHARS:]
    if truncated:
        output = _truncation_marker(log_file) + output

    fields: JsonObject = {"output": output, "truncated": truncated}
    if truncated and log_file is not None:
        fields["log_file"] = log_file
    return fields


def _truncation_marker(log_file: str | None) -> str:
    if log_file is None:
        return "[earlier output truncated]\n"
    return f"[earlier output truncated — complete output in {log_file}; grep/read it]\n"


async def _failure_output_suffix(
    process_manager: ProcessManager,
    context: ToolContext,
    process_id: str,
) -> str:
    """Build the output tail + log pointer appended to timeout-style failures.

    Without it a killed command fails with only the timing fact and every byte
    of diagnostics the process printed is lost to the model.
    """
    output = await _combined_output(process_manager, context, process_id)
    tracked = process_manager.get_process(process_id, context.agent_id)

    parts: list[str] = []
    if output:
        tail = output[-FAILURE_OUTPUT_TAIL_CHARS:]
        label = "Output tail" if len(output) > len(tail) else "Output"
        parts.append(f"\n{label}:\n{tail}")
    if tracked.log_file is not None:
        parts.append(f"\nComplete output: {model_path(tracked.log_file)}")
    return "".join(parts)


def _spawn_failure_message(argv: list[str], error: Exception) -> str:
    message = f"failed to start process: {error}"
    if not isinstance(error, FileNotFoundError):
        return message

    shell = argv[0] if argv else "the shell"
    message += f". The shell '{shell}' was not found on this host"
    if shell == "pwsh":
        message += (
            " — the bash tool requires PowerShell 7 (pwsh) on Windows; install it or add it to PATH"
        )
    return message


async def _combined_output(
    process_manager: ProcessManager,
    context: ToolContext,
    process_id: str,
) -> str:
    log_result = await process_manager.log(process_id, context.agent_id, offset=0, limit=None)
    output = log_result.get("output", "")
    return output if isinstance(output, str) else ""


__all__ = [
    "BASH_MODEL_OUTPUT_CAP_CHARS",
    "BASH_HANDOFF_PROCESS_NOTE",
    "BASH_SUBAGENT_TOOL_DESCRIPTION",
    "BASH_SUBAGENT_TOOL_PARAMETERS",
    "BASH_TOOL_DESCRIPTION",
    "BASH_TOOL_NAME",
    "BASH_TOOL_PARAMETERS",
    "FAILURE_OUTPUT_TAIL_CHARS",
    "bash_handler",
    "format_bash_env_usage",
    "project_bash_tool_definitions",
    "register_bash_tool",
]
