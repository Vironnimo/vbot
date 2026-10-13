"""The shell Tool: one command per call, in a new process without input or terminal.

Every call starts the host shell (PowerShell 7 on Windows, bash elsewhere) on
the command through ``CommandManager`` and waits for it: until it ends, or until
the call's wait passes (``timeout``, else ``SHELL_DEFAULT_WAIT_SECONDS``, at most
``SHELL_MAX_WAIT_SECONDS``). No timer stops a command: one still running when
the wait passes continues in the background and delivers its result as a new
message when it ends, as does a command started with ``run_in_background`` or
moved to the background by the user. The Agent lists, checks and stops its
background commands with ``background_tasks``.

The result is the command's clean output with the shell's exit code and the
facts that exit code hides: programs that failed inside the command, processes
it left running, why vBot stopped it when vBot did, and a hint for well-known
failures (``_shell_results``).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import shutil
import sys
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from core.projects import ProjectStore
from core.runs import TOOL_CALL_OUTPUT_EVENT
from core.sessions import SessionAddress
from core.tools._path_suggestions import similar_entries
from core.tools._powershell import (
    PowerShellCulture,
    PowerShellRequest,
    powershell_bootstrap,
    powershell_culture,
    powershell_request,
)
from core.tools._shell_arguments import (
    NO_EFFECT_FIELDS,
    SHELL_UNADVERTISED_PARAMETERS,
    inherited_env_keys_note,
    no_effect_notes,
    normalize_shell_arguments,
    resolve_timeout,
    shell_display_parts,
    split_env_object,
    unknown_env_keys_message,
)
from core.tools._shell_results import (
    BACKGROUND_TASKS_TOOL_NAME,
    COMMAND_STATUS_NOTE_MARKER,
    COMMAND_STATUS_TOOL_NAMES,
    RUN_AGAIN,
    SHELL_TOOL_NAME,
    TERMINAL_TOOL_NAME,
    ToolNames,
    background_command_statuses,
    command_output_text,
    delivery_names,
    duration,
    ended_result,
    format_command_delivery,
    holding_stage,
    named_tools,
    not_run_problem,
    running_output_note,
    running_output_text,
    stop_with,
)
from core.tools._tool_display import display_notice, display_text
from core.tools._workdir import ProjectWorkdirError, is_project_workdir, project_workdir
from core.tools.arguments import optional_number, optional_string
from core.tools.availability import bash_allowed_env_keys, normalize_env_keys
from core.tools.command_manager import (
    CommandCapacityError,
    CommandLaunchError,
    CommandManager,
    CommandManagerError,
    CommandNotFoundError,
    CommandNotStartedError,
    CommandOwner,
    CommandReport,
    StopReason,
)
from core.tools.contracts import ToolContractError
from core.tools.model_names import SHELL_MODEL_NAME
from core.tools.scratch_areas import ScratchAreas, ScratchUnavailableError
from core.tools.shell_environment import (
    CommandEnvironment,
    RunIdentity,
    command_environment,
    inherited_value,
)
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolPromptBlockRegistry,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.tools.update_handoff import UpdateHandoffGrant, UpdateHandoffs
from core.utils.logging import get_logger
from core.utils.paths import model_path

_LOGGER = get_logger("tools.shell")

CredentialResolver = Callable[[str], str]

# How long a call waits for its command when it names no timeout, and at most.
SHELL_DEFAULT_WAIT_SECONDS = 120.0
SHELL_MAX_WAIT_SECONDS = 600.0
# On Windows a command's processes have a hidden console whose input nothing
# writes: a program that reads it directly instead of stdin (Python's getpass,
# $Host.UI.RawUI.ReadKey()) waits silently until stopped instead of failing.
_CONSOLE_READS_WAIT = sys.platform == "win32"


# Definition


if sys.platform == "win32":
    _OPENING = (
        "Run a PowerShell 7 command in a new process and return its output and exit code. "
        "Write PowerShell, not bash or cmd: $env:NAME for environment variables, $null instead "
        "of /dev/null, Select-String instead of grep for command output, and single quotes or a "
        "here-string (@'...'@) instead of \\\" escapes and heredocs."
    )
    # How Agents start a program in the background without the Tool.
    _DETACHED_START = "Start-Process or a trailing &"
    # How Agents cut a command's output themselves.
    _TRIMMING = "Select-Object -First or -Last, or with head and tail, which PowerShell lacks"
else:
    _OPENING = (
        "Run a bash command in a new process and return its output and exit code. Use bash syntax."
    )
    _DETACHED_START = "nohup or a trailing &"
    _TRIMMING = "head or tail"


def _culture_sentence(culture: PowerShellCulture | None) -> str:
    """Name the user's format for numbers and dates where it is not the one Models assume."""
    if culture is None or culture.name in ("", "en-US"):
        return ""
    return (
        f" PowerShell prints and parses numbers and dates in the user's {culture.name} format, "
        f"such as {culture.number} and {culture.date}."
    )


# The same for every Tool set: what happens to a long command, and the calls
# that follow it up, the results say when it happens. The culture is read once
# per process, so the definition stays the same for the Provider's cache.
SHELL_TOOL_DESCRIPTION = (
    f"{_OPENING}{_culture_sentence(powershell_culture())} Long output keeps its first and last "
    f"lines, and the result names a file with all of it, so there is no need to trim it with "
    f"{_TRIMMING}. Calls in one message run at the same time; chain dependent commands in one "
    "call. When another tool covers the task, use it instead of a shell command."
)

_COMMAND_PARAMETER: JsonObject = {"type": "string", "minLength": 1}
_DESCRIPTION_PARAMETER: JsonObject = {
    "type": "string",
    "description": "Title of what the command does, in 3–5 words, shown to the user.",
}
_WORKDIR_PARAMETER: JsonObject = {
    "type": "string",
    "description": (
        "Directory to run in, absolute or relative to the working directory. Omit to use the "
        "working directory."
    ),
}
# No schema maximum: the handler caps the wait, and some Provider adapters
# reject extra keywords.
_TIMEOUT_PARAMETER: JsonObject = {
    "type": "number",
    "minimum": 0,
    "default": int(SHELL_DEFAULT_WAIT_SECONDS),
    "description": (
        "Seconds to wait for the command before it continues in the background, at most "
        f"{SHELL_MAX_WAIT_SECONDS:g}. Omit for {SHELL_DEFAULT_WAIT_SECONDS:g}."
    ),
}
_RUN_IN_BACKGROUND_PARAMETER: JsonObject = {
    "type": "boolean",
    "description": (
        "Use for servers, watchers and other commands whose result your next step does not "
        f"need, instead of {_DETACHED_START}. Omit to wait for the command."
    ),
}
_ENV_KEYS_PARAMETER: JsonObject = {
    "type": "array",
    "description": (
        "Exact names of granted environment credentials to make available to the command. "
        "Omit when no credential is needed."
    ),
    "items": {"type": "string", "minLength": 1},
    "minItems": 1,
    "uniqueItems": True,
}

SHELL_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "command": _COMMAND_PARAMETER,
        "description": _DESCRIPTION_PARAMETER,
        "workdir": _WORKDIR_PARAMETER,
        "timeout": _TIMEOUT_PARAMETER,
        "run_in_background": _RUN_IN_BACKGROUND_PARAMETER,
        "env_keys": _ENV_KEYS_PARAMETER,
    },
    "required": ["command"],
}


# Granted credentials


def format_shell_env_usage(env_keys: Sequence[str], *, intro: str) -> str:
    """Render the shared model-facing contract for granted shell credentials."""
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


def _render_env_prompt_block(context: Any) -> str:
    env_keys = bash_allowed_env_keys(getattr(context.agent, "tools", None))
    if not env_keys:
        return ""
    guidance = format_shell_env_usage(
        env_keys,
        intro="This Agent has permanent permission to use these credentials in shell commands.",
    )
    return f"## Shell Environment Access\n\n{guidance}"


# Calls


@dataclass(frozen=True)
class _ShellCall:
    command: str
    description: str | None
    workdir: Path
    # How long the call waits; *timeout_given* when the Agent chose it.
    wait_seconds: float
    timeout_given: bool
    background: bool
    variables: dict[str, str]
    credential_names: tuple[str, ...]
    notes: tuple[str, ...]


def _not_run(problem: str) -> ToolContractError:
    return ToolContractError(f"{SHELL_MODEL_NAME} was not run: {problem}")


class _NotStartedError(Exception):
    """The shell could not be started for a reason other than the call; nothing ran."""

    def __init__(self, result: JsonObject) -> None:
        super().__init__(result["error"]["message"])
        self.result = result


def _cannot_start(problem: str) -> _NotStartedError:
    """The shell cannot be started on this host for this command; a retry fails the same way."""
    return _NotStartedError(
        tool_failure(
            "command_not_started", f"{SHELL_MODEL_NAME} was not run: {problem}", retryable=False
        )
    )


def _start_failure(
    error: Exception, call: _ShellCall, context: ToolContext, names: ToolNames
) -> JsonObject:
    """The failure for a command the manager did not start."""
    if isinstance(error, CommandCapacityError):
        message = (
            f"{SHELL_MODEL_NAME} was not run: vBot already runs {error.limit} commands, the "
            "most it runs at once."
        )
        if names(BACKGROUND_TASKS_TOOL_NAME):
            message += (
                f' Find your background commands with {BACKGROUND_TASKS_TOOL_NAME} {{"action": '
                '"list"}, stop the ones you no longer need with action "stop", then run the '
                "command again."
            )
        else:
            message += " Run the command again once one of your background commands has exited."
        return tool_failure("command_limit", message, retryable=True)
    if isinstance(error, CommandNotStartedError):
        return tool_failure(
            "command_not_started",
            f"{SHELL_MODEL_NAME} was not run: this turn was cancelled, or vBot is shutting down.",
            retryable=False,
        )
    if not call.workdir.is_dir():
        # The directory was removed after the call was checked.
        given = call.workdir != context.effective_cwd
        return tool_failure(
            "command_not_started",
            f"{SHELL_MODEL_NAME} was not run: {_missing_workdir(call.workdir, given=given)}",
            retryable=False,
        )
    _LOGGER.warning("Starting a shell command failed", exc_info=error)
    problem, retryable = _launch_problem(error, call, names)
    return tool_failure(
        "command_not_started", f"{SHELL_MODEL_NAME} was not run: {problem}", retryable=retryable
    )


# Operating system errors a start can meet, by errno and by Windows error code.
_NO_RESOURCES_ERRNOS = frozenset({errno.ENOMEM, errno.EAGAIN, errno.EMFILE, errno.ENFILE})
_NO_RESOURCES_WINERRORS = frozenset({4, 8, 14, 1450, 1455})
_DISK_FULL_ERRNOS = frozenset({errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)})
_DISK_FULL_WINERRORS = frozenset({39, 112})


def _launch_problem(error: Exception, call: _ShellCall, names: ToolNames) -> tuple[str, bool]:
    """What kept the command from starting and what to do, and whether a retry can help.

    The operating system's own message is in the host's language and names no
    next step: the text names the state it reports instead.
    """
    os_error = _os_error(error)
    # A launch error is the shell process's; any other is the output file's.
    process = isinstance(error, CommandLaunchError)
    if os_error is None:
        return f"vBot could not start the shell. {RUN_AGAIN} vBot's log has the details.", True
    winerror = getattr(os_error, "winerror", None)
    if os_error.errno in _NO_RESOURCES_ERRNOS or winerror in _NO_RESOURCES_WINERRORS:
        problem = "the system is out of memory or process resources, so the shell could not start."
        if names(BACKGROUND_TASKS_TOOL_NAME):
            return (
                f"{problem} Stop the background commands you no longer need (find them with "
                f'{BACKGROUND_TASKS_TOOL_NAME} {{"action": "list"}}), then run the command again; '
                "if it fails the same way, tell the user.",
                True,
            )
        return f"{problem} {RUN_AGAIN}", True
    if os_error.errno in _DISK_FULL_ERRNOS or winerror in _DISK_FULL_WINERRORS:
        return (
            "the disk is full, so vBot could not write the command's output. Tell the user that "
            "the disk is full.",
            False,
        )
    if isinstance(os_error, PermissionError):
        if process:
            return (
                f"access was denied when vBot started the shell in {model_path(call.workdir)}. "
                "If the command can run in another directory, pass that directory as workdir; "
                "otherwise tell the user.",
                False,
            )
        return (
            "access was denied to vBot's data directory, where the command's output is written. "
            "Tell the user.",
            False,
        )
    code = winerror if winerror is not None else os_error.errno
    detail = f" (operating system error {code})" if code is not None else ""
    if process:
        problem = f"the operating system could not start the shell{detail}."
    else:
        problem = f"vBot could not create the file for the command's output{detail}."
    return f"{problem} {RUN_AGAIN}", True


def _os_error(error: BaseException) -> OSError | None:
    """The operating system error *error* is or was caused by, if any."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, OSError):
            return current
        seen.add(id(current))
        current = current.__cause__
    return None


def _parse_call(
    context: ToolContext, arguments: JsonObject, projects: ProjectStore | None, names: ToolNames
) -> _ShellCall:
    notes: list[str] = []
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        raise _not_run("command is required; pass the command line to run as a string.")
    description = optional_string(arguments.get("description"), field_name="description")
    background = arguments.get("run_in_background") is True
    timeout, note = resolve_timeout(
        optional_number(arguments.get("timeout"), field_name="timeout"),
        optional_number(arguments.get("timeout_ms"), field_name="timeout_ms"),
    )
    if note is not None:
        notes.append(note)
    if timeout is not None and timeout < 0:
        raise _not_run("timeout must be 0 or more seconds.")
    wait_seconds, timeout_given = SHELL_DEFAULT_WAIT_SECONDS, False
    # A call in the background does not wait: its timeout has nothing to bound.
    if timeout is not None and not background:
        if timeout == 0:
            notes.append(
                f"timeout 0 was read as omitted, so the call waits up to "
                f"{SHELL_DEFAULT_WAIT_SECONDS:g} seconds."
            )
        elif timeout > SHELL_MAX_WAIT_SECONDS:
            notes.append(
                f"timeout {timeout:g} is more than {SHELL_MAX_WAIT_SECONDS:g} seconds, the "
                f"longest a call waits, so it was read as {SHELL_MAX_WAIT_SECONDS:g}."
            )
            wait_seconds, timeout_given = SHELL_MAX_WAIT_SECONDS, True
        else:
            wait_seconds, timeout_given = timeout, True
    no_effect = arguments.get(NO_EFFECT_FIELDS)
    if isinstance(no_effect, dict):
        notes.extend(no_effect_notes(no_effect, terminal=names(TERMINAL_TOOL_NAME)))

    workdir = _workdir(context, arguments, projects)

    granted = frozenset(bash_allowed_env_keys(context.tool_settings)) | frozenset(
        context.skill_env_keys
    )
    env = arguments.get("env")
    if env is not None and not isinstance(env, dict):
        raise _not_run("env must be an object of variable names and values.")
    try:
        variables, env_credentials = split_env_object(env, granted)
    except ValueError as error:
        raise ToolContractError(str(error)) from error
    raw_keys = arguments.get("env_keys")
    try:
        requested = normalize_env_keys(raw_keys, field_name="env_keys") if raw_keys else []
    except ValueError as error:
        raise _not_run(f"env_keys is invalid: {error}") from error
    credential_names = list(dict.fromkeys([*requested, *env_credentials]))
    ungranted = [name for name in credential_names if name not in granted]
    if ungranted:
        unknown = [name for name in ungranted if inherited_value(name) is None]
        if unknown:
            raise ToolContractError(unknown_env_keys_message(unknown, granted))
        notes.append(inherited_env_keys_note(ungranted))
    return _ShellCall(
        command=command,
        description=description.strip() if description and description.strip() else None,
        workdir=workdir,
        wait_seconds=wait_seconds,
        timeout_given=timeout_given,
        background=background,
        variables=variables,
        credential_names=tuple(name for name in credential_names if name in granted),
        notes=tuple(notes),
    )


def _workdir(context: ToolContext, arguments: JsonObject, projects: ProjectStore | None) -> Path:
    raw = optional_string(arguments.get("workdir"), field_name="workdir")
    if not raw:
        workdir = context.effective_cwd
    elif is_project_workdir(raw):
        # Not advertised; the Projects block names each Project's directory path.
        try:
            return project_workdir(projects, raw)
        except ProjectWorkdirError as error:
            raise _not_run(str(error)) from error
    else:
        workdir = context.resolve_path(raw)
    if not workdir.is_dir():
        raise _not_run(_missing_workdir(workdir, given=bool(raw)))
    return workdir


def _missing_workdir(workdir: Path, *, given: bool) -> str:
    """The failure for a missing directory; *given* when the call named it as workdir."""
    if not given:
        return (
            f"the working directory {model_path(workdir)} is not an existing directory. Pass an "
            "existing directory as workdir."
        )
    if workdir.exists():
        return (
            f"workdir {model_path(workdir)} is a file, not a directory. Pass the directory it "
            "is in, or omit workdir to use the working directory."
        )
    message = f"workdir {model_path(workdir)} is not an existing directory."
    nearby = similar_entries(workdir, kind="dirs")
    if nearby:
        message += f" Similar directories: {', '.join(model_path(path) for path in nearby)}."
    return f"{message} Pass an existing directory, or omit workdir to use the working directory."


@dataclass(frozen=True)
class _ShellStart:
    """How a call's host shell starts, without input or terminal."""

    argv: list[str]
    env: dict[str, str]
    # What PowerShell reads from stdin before the command runs (Windows only).
    request: PowerShellRequest | None = None
    # Whether a shell started before the call can serve it (``CommandManager.launch``).
    warm: bool = True


def _shell_start(call: _ShellCall, environment: CommandEnvironment) -> _ShellStart:
    """The host shell for *call*, without input or terminal.

    PowerShell's argv and start environment are the same for every call, so a
    shell started before the call can serve it: the command, its workdir and
    the call's layer of the environment travel as the request on stdin. The
    request cannot carry an empty value, which ``SetEnvironmentVariable``
    removes on .NET 8 (pwsh 7.4): a call that sets one starts a shell of its
    own with its whole environment. Raises ``_NotStartedError`` when this host
    cannot run it.
    """
    complete = environment.complete()
    search_path = next((value for name, value in complete.items() if name.upper() == "PATH"), None)
    if sys.platform == "win32":
        pwsh = shutil.which("pwsh", path=search_path)
        if pwsh is None:
            raise _cannot_start(
                "PowerShell 7 (pwsh) is not installed on this host. Ask the user to install it."
            )
        argv = [pwsh, "-NoProfile", "-NonInteractive", "-Command", powershell_bootstrap()]
        if "" in environment.changes.values():
            request = powershell_request(call.command, str(call.workdir), {})
            return _ShellStart(argv, complete, request, warm=False)
        request = powershell_request(call.command, str(call.workdir), environment.changes)
        return _ShellStart(argv, environment.base, request)
    bash = shutil.which("bash", path=search_path)
    if bash is None:
        raise _cannot_start("bash is not installed on this host. Ask the user to install it.")
    return _ShellStart([bash, "-c", call.command], complete)


def _issue_update_handoff(
    update_handoffs: UpdateHandoffs | None, context: ToolContext
) -> UpdateHandoffGrant | None:
    """Give a call whose Tool Result has a persistence boundary its handoff token."""
    if update_handoffs is None or context.result_persisted_hook is None:
        return None
    try:
        handoff = update_handoffs.issue(
            run_id=context.run_id,
            tool_call_id=context.tool_call_id,
            agent_id=context.agent_id,
            project_id=context.project_id,
            session_id=context.session_id,
        )
    except ValueError:
        # A token is scoped to the call's Run ids; without them the command runs
        # without the capability.
        _LOGGER.warning(
            "Shell call %s runs without an update handoff: its Run ids are incomplete",
            context.tool_call_id,
            exc_info=True,
        )
        return None
    context.after_result_persisted(handoff.acknowledge)
    return handoff


class ShellTool:
    """Runs shell commands for Agents through the ``CommandManager``."""

    def __init__(
        self,
        command_manager: CommandManager,
        *,
        credential_resolver: CredentialResolver | None = None,
        update_handoffs: UpdateHandoffs | None = None,
        projects: ProjectStore | None = None,
        scratch_areas: ScratchAreas | None = None,
    ) -> None:
        self._commands = command_manager
        self._resolve_credential = credential_resolver or (lambda _name: "")
        self._update_handoffs = update_handoffs
        self._projects = projects
        self._scratch_areas = scratch_areas
        # Tasks that outlive their call: releasing handoff tokens, stopping commands.
        self._tasks: set[asyncio.Task[Any]] = set()

    async def __call__(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        names = named_tools(context)
        call = _parse_call(context, arguments, self._projects, names)
        handoff = _issue_update_handoff(self._update_handoffs, context)
        try:
            command_id = await self._start(context, call, handoff, names)
        except _NotStartedError as failure:
            if handoff is not None:
                handoff.release()
            return failure.result
        except BaseException:
            if handoff is not None:
                handoff.release()
            raise
        if handoff is not None:
            self._keep(self._release_when_finished(command_id, handoff))
        if call.background:
            report = self._commands.to_background(command_id, deliver=True)
            return _result(call, report, names, reason="background")
        return await self._wait(context, call, command_id, names)

    async def _start(
        self,
        context: ToolContext,
        call: _ShellCall,
        handoff: UpdateHandoffGrant | None,
        names: ToolNames,
    ) -> str:
        credentials = {name: self._resolve_credential(name) for name in call.credential_names}
        scratch = None
        if self._scratch_areas is not None:
            # Scratch is optional; its owner logged an unavailable folder. The
            # command still runs with the host's normal temporary directories.
            with contextlib.suppress(ScratchUnavailableError):
                scratch = await self._scratch_areas.folder_for(
                    SessionAddress(context.project_id, context.agent_id, context.session_id)
                )

        def prepare() -> _ShellStart:
            environment = command_environment(
                RunIdentity(
                    context.agent_id,
                    context.session_id,
                    context.project_id,
                    handoff.token if handoff is not None else None,
                ),
                variables=call.variables,
                credentials=credentials,
                scratch=scratch,
            )
            # PATH lookup probes the filesystem and uses this call's fresh environment.
            return _shell_start(call, environment)

        start = await asyncio.to_thread(prepare)
        # Output can arrive before the launch returns the command's id.
        started: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def progress(output: str) -> None:
            command_id = await started
            await context.emit(
                TOOL_CALL_OUTPUT_EVENT,
                {"tool_call_id": context.tool_call_id, "command_id": command_id, "output": output},
            )

        try:
            handle = await self._commands.launch(
                CommandOwner(context.project_id, context.agent_id, context.session_id),
                start.argv,
                command=call.command,
                description=call.description,
                cwd=call.workdir,
                env=start.env,
                origin_run_id=context.run_id,
                # The delivery names only Tools named now; it outlives this context.
                formatter=partial(format_command_delivery, names=delivery_names(names)),
                execution_owner=context.execution_owner,
                on_output=progress,
                request=start.request.data if start.request is not None else None,
                warm=start.warm,
                not_run_codes=start.request.not_run_codes if start.request is not None else None,
            )
        except (OSError, CommandManagerError) as error:
            started.cancel()
            raise _NotStartedError(_start_failure(error, call, context, names)) from error
        except BaseException:
            started.cancel()
            raise
        started.set_result(handle.command_id)
        return handle.command_id

    async def _wait(
        self, context: ToolContext, call: _ShellCall, command_id: str, names: ToolNames
    ) -> JsonObject:
        moved = asyncio.Event()
        # Set once this call hands the command to the background: from then on a
        # stop of the call or its Run leaves the command running.
        handed_off = False

        def move_to_background() -> bool:
            if moved.is_set() or self._commands.report(command_id).ended:
                return False
            moved.set()
            return True

        def cancel() -> asyncio.Task[Any] | None:
            if handed_off:
                return None
            reason: StopReason = "run_cancelled" if context.is_cancelled() else "user"
            return self._keep(self._stop(command_id, reason))

        if context.background_registration_hook is not None:
            context.background_registration_hook(move_to_background)
        context.on_cancel(cancel)

        waiting = asyncio.ensure_future(self._commands.wait(command_id, seconds=call.wait_seconds))
        moving = asyncio.ensure_future(moved.wait())
        try:
            await asyncio.wait({waiting, moving}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            if not self._commands.report(command_id).ended:
                self._keep(self._stop(command_id, "run_cancelled"))
            raise
        finally:
            for task in (waiting, moving):
                task.cancel()
            await asyncio.gather(waiting, moving, return_exceptions=True)

        error = waiting.exception() if waiting.done() and not waiting.cancelled() else None
        if error is not None:
            raise error
        idle = waiting.done() and not waiting.cancelled() and waiting.result() == "idle"
        report = self._commands.report(command_id)
        if report.ended and not report.still_running:
            return _result(call, report, names)
        # Listed from now on, so the user and the Agent can see and stop it; a
        # command that ended is delivered by this result, not later.
        handed_off = True
        report = self._commands.to_background(command_id, deliver=not report.ended)
        reason = "timeout" if call.timeout_given else "default"
        if moved.is_set():
            reason = "moved"
        return _result(call, report, names, reason=reason, idle=idle)

    async def _stop(self, command_id: str, reason: StopReason) -> None:
        try:
            await self._commands.stop(command_id, reason)
        except CommandManagerError:
            _LOGGER.warning("Stopping command %s failed", command_id, exc_info=True)

    # Background work

    def _keep(self, work: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(work)
        self._tasks.add(task)
        task.add_done_callback(self._settled)
        return task

    def _settled(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.warning("Shell background work failed", exc_info=task.exception())

    async def _release_when_finished(self, command_id: str, handoff: UpdateHandoffGrant) -> None:
        """Keep the handoff token claimable exactly while the command's processes run."""
        try:
            await self._commands.wait_finished(command_id)
        except CommandNotFoundError:
            pass
        finally:
            handoff.release()


# Results


def _result(
    call: _ShellCall,
    report: CommandReport,
    names: ToolNames,
    *,
    reason: str = "",
    idle: bool = False,
) -> JsonObject:
    """The Tool result of one call; *reason* says why a running command was
    returned, *idle* that its processes were idle when the wait passed."""
    if report.ended:
        not_run = not_run_problem(report)
        if not_run is not None:
            problem, retryable = not_run
            return tool_failure(
                "command_not_started",
                f"{SHELL_MODEL_NAME} was not run: {problem}",
                retryable=retryable,
            )
        data = ended_result(report, names)
    else:
        data = {"status": "running", "command_id": report.command_id}
        output = running_output_text(report)
        if output:
            data["output"] = output
        data["next"] = _running_next(
            report, names, reason=reason, wait_seconds=call.wait_seconds, idle=idle
        )
    if call.notes:
        data["notes"] = list(call.notes)
    return tool_success(data)


def _running_next(
    report: CommandReport, names: ToolNames, *, reason: str, wait_seconds: float, idle: bool
) -> str:
    """What to do about a command that continues in the background.

    *reason* is ``background`` for a call with ``run_in_background``, ``moved``
    when the user moved it, ``timeout`` when the Agent's timeout passed, and
    ``default`` when the default wait passed. *idle*: the command's processes
    were idle over the end of the wait (``CommandManager.wait``).
    """
    command_id = report.command_id
    log = running_output_note(report)
    stop = stop_with(report, names)
    if reason == "background":
        # Often a server: its result arrives only once it is stopped, and the
        # Agent checks that it is ready itself.
        return (
            f"The command runs in the background as {command_id}.{log} Its result arrives as a "
            "new message when it exits; do not start it again, and do not sleep or poll for "
            "that result. If it runs until stopped, such as a server, check that it is ready "
            "before you use it, for example with a request to its port, and stop it "
            f"{stop} once it is no longer needed. Otherwise continue other work, or end your "
            "turn if your next step needs the result."
        )
    # "or it runs until stopped" keeps an Agent from waiting for a server's exit.
    stopping = (
        f"If you no longer need it, or it runs until stopped, such as a server, stop it {stop}."
    )
    if (
        _CONSOLE_READS_WAIT
        and idle
        and reason in ("timeout", "default")
        and not report.transcript.total_lines
        # A stage that holds the output until the end explains the silence, and an
        # idle program behind it, such as one in a retry back-off, may print later.
        and holding_stage(report.command) is None
    ):
        # A program that waits for the console never ends, and its result
        # otherwise reads like a slow command's that delivers later.
        stopping = (
            "It has printed nothing and its processes are idle. If it waits for console input, "
            f"which commands never get, stop it {stop} and pass the answer as an option or "
            "argument. If you no longer need it, or it runs until stopped, such as a server, "
            "stop it the same way."
        )
    tail = (
        "Its result arrives as a new message when it exits; do not start it again, and do not "
        f"sleep or poll for that result. {stopping} Otherwise continue other work, or end "
        "your turn if your next step needs the result."
    )
    if reason == "moved":
        return (
            "The user moved the command to the background after "
            f"{duration(report.elapsed_seconds)}; it continues as {command_id}.{log} {tail}"
        )
    if reason == "timeout":
        opening = f"The command was still running at its {wait_seconds:g}-second timeout"
    else:
        opening = f"The command was still running after {SHELL_DEFAULT_WAIT_SECONDS:g} seconds"
    return f"{opening} and continues in the background as {command_id}.{log} {tail}"


# Display


def shell_detail_blocks(arguments: JsonObject, result: JsonObject | None) -> list[JsonObject]:
    """Show the user the command, its output and what its outcome means."""
    try:
        normalized = normalize_shell_arguments(arguments)
    except ValueError:
        normalized = arguments
    blocks: list[JsonObject] = []
    command = normalized.get("command") if isinstance(normalized, dict) else None
    if isinstance(command, str) and command.strip():
        blocks.append(display_text("command", text=command))
    data = result.get("data") if isinstance(result, dict) and result.get("ok") is True else None
    if not isinstance(data, dict):
        return blocks
    blocks.append(display_text("output", source="result", path=("data", "output")))
    if data.get("status") == "stopped" and isinstance(data.get("stopped_because"), str):
        blocks.append(display_notice("warning", f"Stopped: {data['stopped_because']}"))
    exit_code = data.get("exit_code")
    if (
        data.get("status") == "exited"
        and isinstance(exit_code, int)
        and not isinstance(exit_code, bool)
        and exit_code != 0
    ):
        blocks.append(display_notice("warning", f"The command exited with code {exit_code}."))
    failed = data.get("failed_programs")
    if isinstance(failed, list) and failed:
        blocks.append(display_notice("warning", "; ".join(str(item) for item in failed)))
    if isinstance(data.get("hint"), str):
        blocks.append(display_notice("info", f"Hint: {data['hint']}"))
    if data.get("status") == "running":
        blocks.append(display_notice("info", "The command continues in the background."))
    return blocks


def register_shell_tool(
    registry: ToolRegistry,
    command_manager: CommandManager,
    *,
    credential_resolver: CredentialResolver | None = None,
    prompt_blocks: ToolPromptBlockRegistry | None = None,
    update_handoffs: UpdateHandoffs | None = None,
    projects: ProjectStore | None = None,
    scratch_areas: ScratchAreas | None = None,
) -> ShellTool:
    """Register the shell Tool with a vBot Tool registry.

    *projects* resolves ``workdir: "project:<project-id>"``; without it that form fails.
    """
    tool = ShellTool(
        command_manager,
        credential_resolver=credential_resolver,
        update_handoffs=update_handoffs,
        projects=projects,
        scratch_areas=scratch_areas,
    )
    registry.register(
        SHELL_TOOL_NAME,
        SHELL_TOOL_DESCRIPTION,
        SHELL_TOOL_PARAMETERS,
        tool,
        summary="Run shell commands.",
        family="execution",
        open_input_schema=True,
        unadvertised_parameters=SHELL_UNADVERTISED_PARAMETERS,
        argument_normalizer=normalize_shell_arguments,
        result_schema={"type": "object", "required": ["status"]},
        display=ToolDisplay(parts_builder=shell_display_parts, detail_builder=shell_detail_blocks),
    )
    if prompt_blocks is not None:
        prompt_blocks.register(SHELL_TOOL_NAME, render=_render_env_prompt_block)
    return tool


__all__ = [
    "COMMAND_STATUS_NOTE_MARKER",
    "COMMAND_STATUS_TOOL_NAMES",
    "SHELL_DEFAULT_WAIT_SECONDS",
    "SHELL_MAX_WAIT_SECONDS",
    "SHELL_TOOL_DESCRIPTION",
    "SHELL_TOOL_NAME",
    "SHELL_TOOL_PARAMETERS",
    "ShellTool",
    "background_command_statuses",
    "command_output_text",
    "format_command_delivery",
    "format_shell_env_usage",
    "register_shell_tool",
    "shell_detail_blocks",
]
