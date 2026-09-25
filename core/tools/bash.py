"""Built-in bash tool backed by the shared process manager."""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.tools._bash_environment import (
    HARD_KILL_SIGNAL,
    SHELL_ENV_CACHE_TTL_SECONDS,
    SHELL_ENV_PROBE_REAP_TIMEOUT_SECONDS,
    SHELL_ENV_PROBE_TIMEOUT_SECONDS,
    get_shell_env,
    reset_shell_env_cache,
)
from core.tools._bash_results import (
    BACKGROUND_STATUS_NOTE_MARKER,
    BACKGROUND_STATUS_TOOL_NAMES,
    BACKGROUND_USER_CANCELLED_MESSAGE,
    BASH_COMPLETION_PROCESS_ID_PREFIX,
    BASH_COMPLETION_STATUS_PREFIX,
    BASH_HANDOFF_OUTPUT_CAP_CHARS,
    BASH_HANDOFF_OUTPUT_MAX_LINES,
    BASH_TOOL_NAME,
    USER_CANCELLED_FAILURE_CODE,
    USER_CANCELLED_FAILURE_MESSAGE,
    _background_result,
    _background_user_cancelled_message,
    _completion_result,
    _failure_output_suffix,
    _shape_output_fields,
    _spawn_failure_message,
    _user_cancelled_failure_message,
    background_bash_statuses,
)
from core.tools._bash_update_handoff import (
    HANDOFF_ENV,
    UpdateHandoffGrant,
    UpdateHandoffs,
)
from core.tools._powershell import powershell_command
from core.tools.arguments import optional_number, optional_string
from core.tools.availability import bash_allowed_env_keys, normalize_env_keys
from core.tools.bash_hints import annotate_failure
from core.tools.model_names import SHELL_MODEL_NAME
from core.tools.process_manager import (
    ProcessManager,
    ProcessNotFoundError,
    ProcessTerminationError,
    log_background_task_result,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolDisplayField,
    ToolPromptBlockRegistry,
    ToolRegistry,
    tool_failure,
)
from core.utils.logging import get_logger

CredentialResolver = Callable[[str], str]

FOREGROUND_HANDOFF_SECONDS = 90.0
DEFAULT_TIMEOUT_SECONDS = 180.0


def _shell_syntax_notes() -> str:
    """Name the actual shell so the model writes matching syntax.

    The Model sees this Tool as powershell on Windows (``model_names``); the note
    adds that it is PowerShell 7, so Models avoid cmd and Windows PowerShell 5.1
    syntax. Mirrors the platform branch in ``_shell_argv``; constant per host, so
    provider prompt caching is unaffected.
    """
    if sys.platform == "win32":
        return (
            " Commands use PowerShell 7 (pwsh), not cmd or bash: use $env:VAR, redirect "
            "stderr with 2>$null, and assign environment variables separately. PowerShell "
            "runs non-interactively."
        )
    return " Commands run in bash on this host."


BASH_TOOL_DESCRIPTION = (
    "Run an unattended shell command and capture its output, such as scripts, builds, "
    "non-interactive Git, file operations, and servers. "
    "For file discovery and content search, use search_files when available. "
    "No interactive input or live screen "
    "is available; provide input through files or pipelines. Never manually detach or "
    "daemonize commands." + _shell_syntax_notes()
)
BASH_SUBAGENT_TOOL_DESCRIPTION = (
    "Run an unattended shell command and wait for its output, such as scripts, builds, "
    "non-interactive Git, and file operations. "
    "For file discovery and content search, use search_files when available. "
    "Background execution is unavailable. "
    "No interactive input or live screen is available; provide input through files or "
    "pipelines. Never manually detach or daemonize commands." + _shell_syntax_notes()
)
DEFAULT_EXECUTION_MODE = "foreground"
BASH_EXECUTION_MODES = (DEFAULT_EXECUTION_MODE, "background")
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
    "minimum": 0,
    "description": (
        "Total runtime limit in seconds, including time in background. Foreground default: "
        "180 seconds; background mode has no default limit. Use a longer limit for slow "
        "work or 0 for no limit."
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
    properties = {
        "command": _BASH_COMMAND_PARAMETER,
        "description": _BASH_DESCRIPTION_PARAMETER,
        "workdir": _BASH_WORKDIR_PARAMETER,
        "timeout": _BASH_TIMEOUT_PARAMETER,
        "env_keys": _BASH_ENV_KEYS_PARAMETER,
    }
    if not subagent:
        properties["mode"] = {
            "type": "string",
            "enum": list(BASH_EXECUTION_MODES),
            "description": (
                "Start in foreground and hand off a still-running command after 90 seconds, "
                "or start in background immediately. Background results arrive automatically. "
                "Omit for foreground."
            ),
        }
    return {"type": "object", "properties": properties, "required": ["command"]}


BASH_TOOL_PARAMETERS = _bash_tool_parameters(subagent=False)
BASH_SUBAGENT_TOOL_PARAMETERS = _bash_tool_parameters(subagent=True)

FOREGROUND_POLL_INTERVAL_SECONDS = 0.05
RUN_CANCELLED_FAILURE_CODE = "run_cancelled"
RUN_CANCELLED_FAILURE_MESSAGE = "Command stopped because the owning Run was cancelled"

# Sub-Agent Sessions cannot receive completion after their Run ends.
BACKGROUND_AT_DEPTH_FAILURE_CODE = "background_unavailable_in_subagent"
BACKGROUND_AT_DEPTH_EXPLICIT_MESSAGE = (
    "Background execution is unavailable inside a Sub-Agent. Use foreground or omit mode."
)

_LOGGER = get_logger("tools.bash")


# An in-flight probe is harmless to leave running — it still populates the
# cache when it finishes, and the TTL check on the next call will refresh
# again if the result is already stale by then.


def project_bash_tool_definitions(
    definitions: list[JsonObject],
    *,
    nesting_depth: int,
) -> list[JsonObject]:
    """Narrow Bash's Provider definition to the execution modes valid at this depth."""
    if nesting_depth < 1:
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
    return context.nesting_depth >= 1


async def bash_handler(
    context: ToolContext,
    arguments: JsonObject,
    process_manager: ProcessManager,
    trigger_service: Any | None = None,
    credential_resolver: CredentialResolver | None = None,
    update_handoffs: UpdateHandoffs | None = None,
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
    resolve_credential = credential_resolver or (lambda key: os.environ.get(key, ""))
    handoff = _issue_update_handoff(update_handoffs, context)

    async def command_environment() -> dict[str, str]:
        env = await get_shell_env()
        env.pop(HANDOFF_ENV, None)
        for key in requested_env_keys:
            env[key] = resolve_credential(key)
        env[VBOT_RUN_AGENT_ID_ENV] = context.agent_id
        env[VBOT_RUN_SESSION_ID_ENV] = context.session_id
        if context.project_id is None:
            env.pop(VBOT_RUN_PROJECT_ID_ENV, None)
        else:
            env[VBOT_RUN_PROJECT_ID_ENV] = context.project_id
        if handoff is not None:
            env[HANDOFF_ENV] = handoff.token
        return env

    argv = _shell_argv(command)
    try:
        spawned = await _spawn_command(
            process_manager, context, argv, workdir, environment=command_environment
        )
    except BaseException:
        if handoff is not None:
            handoff.release()
        raise
    if not isinstance(spawned, str):
        if handoff is not None:
            handoff.release()
        return spawned
    process_id = spawned
    if handoff is not None:
        _release_handoff_after_exit(process_manager, context, process_id, handoff)

    _register_user_cancel_callback(process_manager, context, process_id)

    timeout_task, timeout_state = _schedule_timeout(
        process_manager, context, process_id, parsed.get("timeout")
    )

    if mode == "background":
        result = await _background_result(
            process_manager,
            context,
            process_id,
            handoff_after=None,
            timeout_seconds=parsed["timeout"],
        )
        _maybe_spawn_completion_watcher(
            process_manager,
            context,
            process_id,
            command,
            trigger_service,
            timeout_state=timeout_state,
            timeout_seconds=parsed["timeout"],
        )
        return result

    result = await _run_foreground_phase(
        process_manager,
        context,
        process_id,
        command=command,
        timeout_seconds=parsed["timeout"],
    )

    if context.is_cancelled() or context.was_cancelled_by_user():
        if timeout_task is not None:
            timeout_task.cancel()
        await process_manager.kill(process_id, context.agent_id, project_id=context.project_id)
        if context.was_cancelled_by_user():
            return tool_failure(
                USER_CANCELLED_FAILURE_CODE,
                _user_cancelled_failure_message(process_manager, context, process_id),
            )
        return tool_failure(RUN_CANCELLED_FAILURE_CODE, RUN_CANCELLED_FAILURE_MESSAGE)

    if result["data"] is not None and result["data"].get("status") == "running":
        _maybe_spawn_completion_watcher(
            process_manager,
            context,
            process_id,
            command,
            trigger_service,
            timeout_state=timeout_state,
            timeout_seconds=parsed["timeout"],
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
            f"process timed out after {parsed['timeout']} seconds. Inspect the output before "
            "retrying; set a longer timeout if the command legitimately needs more time." + suffix,
        )

    return result


def register_bash_tool(
    registry: ToolRegistry,
    process_manager: ProcessManager,
    trigger_service: Any | None = None,
    *,
    credential_resolver: CredentialResolver | None = None,
    prompt_blocks: ToolPromptBlockRegistry | None = None,
    update_handoffs: UpdateHandoffs | None = None,
) -> None:
    """Register the bash tool with a vBot tool registry."""

    async def handler(context: ToolContext, arguments: JsonObject) -> JsonObject:
        return await bash_handler(
            context,
            arguments,
            process_manager,
            trigger_service=trigger_service,
            credential_resolver=credential_resolver,
            update_handoffs=update_handoffs,
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
        f"To use one, include its exact name in the `env_keys` array of every "
        f"`{SHELL_MODEL_NAME}` call "
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
        intro="This Agent has permanent permission to use these credentials in shell commands.",
    )
    return f"## Shell Environment Access\n\n{guidance}"


async def _watch_background_process(
    process_manager: ProcessManager,
    process_id: str,
    agent_id: str,
    chat_session_id: str,
    origin_run_id: str,
    command: str,
    trigger_service: Any,
    project_id: str | None = None,
    *,
    timeout_state: dict[str, bool] | None = None,
    timeout_seconds: float | None = None,
) -> None:
    try:
        tracked = process_manager.get_process(process_id, agent_id, project_id=project_id)
        wait_task = tracked.wait_task
        if wait_task is not None:
            await asyncio.shield(wait_task)
        else:
            while (
                process_manager.get_process(process_id, agent_id, project_id=project_id).status
                == "running"
            ):
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
        log_result = await process_manager.log(process_id, agent_id, project_id=project_id)
        tracked = process_manager.get_process(process_id, agent_id, project_id=project_id)
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
    timed_out = (
        timeout_state is not None
        and bool(timeout_state.get("timed_out"))
        and tracked.status == "killed"
    )

    if user_cancelled:
        body = (
            f"{BASH_COMPLETION_STATUS_PREFIX}aborted by user\n"
            f"{BASH_COMPLETION_PROCESS_ID_PREFIX}{process_id}\n"
            f"{_background_user_cancelled_message(tracked)}\n"
            f"Command: {command}\n"
            "Output:\n"
            f"{output}"
        )
    elif timed_out:
        limit = f"{timeout_seconds:g}" if timeout_seconds is not None else "configured"
        body = (
            f"{BASH_COMPLETION_STATUS_PREFIX}killed\n"
            f"{BASH_COMPLETION_PROCESS_ID_PREFIX}{process_id}\n"
            f"Command: {command}\n"
            "Reason: the vBot tool timeout of "
            f"{limit} s elapsed and killed the process. Set a longer timeout if the "
            "command legitimately needs more time, or timeout: 0 for no limit.\n"
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
        execution_owner=tracked.execution_owner,
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
    *,
    timeout_state: dict[str, bool] | None = None,
    timeout_seconds: float | None = None,
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
            timeout_state=timeout_state,
            timeout_seconds=timeout_seconds,
        )
    )
    try:
        process_manager.register_completion_notification(
            process_id,
            context.agent_id,
            task,
            project_id=context.project_id,
        )
    except Exception:
        task.cancel()
        raise
    task.add_done_callback(
        lambda completed: log_background_task_result(
            completed,
            f"Bash completion trigger failed for "
            f"agent={context.agent_id} session={context.session_id}",
        )
    )


def _issue_update_handoff(
    update_handoffs: UpdateHandoffs | None,
    context: ToolContext,
) -> UpdateHandoffGrant | None:
    """Give a call whose Tool Result has a persistence boundary its handoff token."""
    if update_handoffs is None or context.result_persisted_hook is None:
        return None
    handoff = update_handoffs.issue(
        run_id=context.run_id,
        tool_call_id=context.tool_call_id,
        agent_id=context.agent_id,
        project_id=context.project_id,
        session_id=context.session_id,
    )
    context.after_result_persisted(handoff.acknowledge)
    return handoff


async def _spawn_command(
    process_manager: ProcessManager,
    context: ToolContext,
    argv: list[str],
    workdir: Path,
    *,
    environment: Callable[[], Awaitable[dict[str, str]]],
) -> str | JsonObject:
    """Spawn the shell and return its process id, or a spawn failure envelope."""
    env = await environment()
    try:
        return await process_manager.spawn(
            context.run_id,
            context.agent_id,
            argv,
            project_id=context.project_id,
            env=env,
            cwd=workdir,
            execution_owner=context.execution_owner,
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
        env = await environment()
        try:
            return await process_manager.spawn(
                context.run_id,
                context.agent_id,
                argv,
                project_id=context.project_id,
                env=env,
                cwd=workdir,
                execution_owner=context.execution_owner,
            )
        except (OSError, ValueError) as error:
            return tool_failure("process_spawn_failed", _spawn_failure_message(argv, error))
    except (OSError, ValueError) as error:
        return tool_failure("process_spawn_failed", _spawn_failure_message(argv, error))


def _release_handoff_after_exit(
    process_manager: ProcessManager,
    context: ToolContext,
    process_id: str,
    handoff: UpdateHandoffGrant,
) -> None:
    """Keep the handoff token claimable exactly while the call's process may run."""
    try:
        wait_task = process_manager.get_process(
            process_id, context.agent_id, project_id=context.project_id
        ).wait_task
    except ProcessNotFoundError:
        wait_task = None
    if wait_task is None or wait_task.done():
        handoff.release()
        return
    wait_task.add_done_callback(lambda _task: handoff.release())


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
            process_manager.cancel_for_user(
                process_id, context.agent_id, project_id=context.project_id
            )
            if context.was_cancelled_by_user()
            else process_manager.kill(process_id, context.agent_id, project_id=context.project_id)
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(kill_coro)
        else:
            kill_task = loop.create_task(kill_coro)
            kill_task.add_done_callback(
                lambda completed: log_background_task_result(
                    completed,
                    f"Bash user-cancel kill failed for "
                    f"agent={context.agent_id} process={process_id}",
                )
            )

    context.on_cancel(cancel_callback)


def _parse_arguments(arguments: JsonObject) -> JsonObject | str:
    command = arguments.get("command")
    if not isinstance(command, str) or not command:
        return "command must be a non-empty string"

    mode = arguments.get("mode", DEFAULT_EXECUTION_MODE)
    if not isinstance(mode, str) or mode not in BASH_EXECUTION_MODES:
        return "mode must be foreground or background"

    try:
        workdir = optional_string(arguments.get("workdir"), field_name="workdir")
        optional_string(arguments.get("description"), field_name="description")
        timeout = optional_number(
            arguments.get("timeout"),
            field_name="timeout",
            minimum=0,
        )
        env_keys = normalize_env_keys(
            arguments.get("env_keys", []),
            field_name="env_keys",
        )
    except ValueError as error:
        return str(error)

    if timeout is None and mode == DEFAULT_EXECUTION_MODE:
        timeout = DEFAULT_TIMEOUT_SECONDS

    return {
        "command": command,
        "mode": mode,
        "workdir": workdir,
        "timeout": None if timeout == 0 else timeout,
        "env_keys": env_keys,
    }


def _resolve_workdir(context: ToolContext, workdir: object) -> Path:
    if workdir is None:
        return context.effective_cwd.resolve()

    return context.resolve_path(str(workdir))


def _shell_argv(command: str) -> list[str]:
    if sys.platform == "win32":
        # Keep PowerShell host prompts unavailable even when a command attempts
        # to use the host instead of the closed standard input stream. Process
        # output is decoded as UTF-8, so the console must also emit UTF-8, and
        # the exit status must be the last native program's, as with bash.
        return ["pwsh", "-NonInteractive", "-Command", powershell_command(command)]
    return ["bash", "-c", command]


def _schedule_timeout(
    process_manager: ProcessManager,
    context: ToolContext,
    process_id: str,
    timeout: float | None,
) -> tuple[asyncio.Task[None] | None, dict[str, bool]]:
    state = {"timed_out": False}
    if timeout is None:
        return None, state

    async def kill_after_timeout() -> None:
        tracked = process_manager.get_process(
            process_id, context.agent_id, project_id=context.project_id
        )
        if tracked.wait_task is not None:
            # Observe the manager-owned finalizer without cancelling it. A
            # completed background command must not leave a sleeping deadline
            # task holding its Run context until the original timeout expires.
            done, _ = await asyncio.wait([tracked.wait_task], timeout=timeout)
            if done:
                return
        else:
            await asyncio.sleep(timeout)
        state["timed_out"] = True
        try:
            await process_manager.kill(process_id, context.agent_id, project_id=context.project_id)
        except ProcessTerminationError:
            # The manager retains the failure for the foreground result and
            # subsequent explicit kills, and already logs the OS failure.
            return

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
    tracked = process_manager.get_process(
        process_id, context.agent_id, project_id=context.project_id
    )
    return tracked.status == "killed"


async def _run_foreground_phase(
    process_manager: ProcessManager,
    context: ToolContext,
    process_id: str,
    *,
    command: str,
    timeout_seconds: float | None = None,
) -> JsonObject:
    deadline = (
        asyncio.get_running_loop().time() + FOREGROUND_HANDOFF_SECONDS
        if not _background_blocked_at_depth(context)
        else None
    )
    background_requested = False

    def request_background() -> bool:
        nonlocal background_requested
        tracked = process_manager.get_process(
            process_id, context.agent_id, project_id=context.project_id
        )
        if tracked.status != "running" or context.is_cancelled() or context.was_cancelled_by_user():
            return False
        background_requested = True
        return True

    if not _background_blocked_at_depth(context) and context.background_registration_hook:
        context.background_registration_hook(request_background)

    while True:
        poll_result = await process_manager.poll(
            process_id, context.agent_id, timeout_ms=0, project_id=context.project_id
        )
        await _emit_output_chunks(context, process_id, poll_result)

        tracked = process_manager.get_process(
            process_id, context.agent_id, project_id=context.project_id
        )
        if tracked.termination_failed:
            return tool_failure(
                "process_kill_failed", str(ProcessTerminationError(process_id)), retryable=True
            )

        if poll_result["status"] != "running":
            return await _completion_result(
                process_manager,
                context,
                process_id,
                command=command,
            )

        if context.is_cancelled():
            await process_manager.kill(process_id, context.agent_id, project_id=context.project_id)
            return await _completion_result(
                process_manager,
                context,
                process_id,
                command=command,
            )
        if background_requested or (
            deadline is not None and asyncio.get_running_loop().time() >= deadline
        ):
            return await _background_result(
                process_manager,
                context,
                process_id,
                requested_by_user=background_requested,
                timeout_seconds=timeout_seconds,
                handoff_after=(
                    (
                        datetime.now(UTC)
                        - process_manager.get_process(
                            process_id, context.agent_id, project_id=context.project_id
                        ).started_at
                    ).total_seconds()
                    if background_requested
                    else FOREGROUND_HANDOFF_SECONDS
                ),
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


__all__ = [
    "BASH_SUBAGENT_TOOL_DESCRIPTION",
    "BASH_SUBAGENT_TOOL_PARAMETERS",
    "BASH_TOOL_DESCRIPTION",
    "BASH_TOOL_NAME",
    "BASH_TOOL_PARAMETERS",
    "UpdateHandoffs",
    "bash_handler",
    "format_bash_env_usage",
    "project_bash_tool_definitions",
    "register_bash_tool",
    "SHELL_ENV_PROBE_TIMEOUT_SECONDS",
    "SHELL_ENV_PROBE_REAP_TIMEOUT_SECONDS",
    "SHELL_ENV_CACHE_TTL_SECONDS",
    "HARD_KILL_SIGNAL",
    "reset_shell_env_cache",
    "get_shell_env",
    "BASH_COMPLETION_STATUS_PREFIX",
    "BASH_COMPLETION_PROCESS_ID_PREFIX",
    "BASH_HANDOFF_OUTPUT_CAP_CHARS",
    "BASH_HANDOFF_OUTPUT_MAX_LINES",
    "USER_CANCELLED_FAILURE_CODE",
    "USER_CANCELLED_FAILURE_MESSAGE",
    "BACKGROUND_USER_CANCELLED_MESSAGE",
    "BACKGROUND_STATUS_NOTE_MARKER",
    "BACKGROUND_STATUS_TOOL_NAMES",
    "background_bash_statuses",
]
