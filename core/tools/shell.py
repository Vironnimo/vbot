"""The shell Tool: one command per call, each in a new terminal.

Every call starts the host shell (PowerShell 7 on Windows, bash elsewhere) on
the command in a new Terminal Session of kind ``command`` and waits for it. The
result is the command's rendered output - what a person would have seen in the
terminal - with the shell's exit code and the facts that exit code hides:
programs that failed inside the command, processes it left running, why vBot
stopped it when vBot did, and a hint for well-known failures.

A command that is still running after the hand-off time, waits for input, or
is moved to the background by the user continues as a listed terminal. Its
result arrives as a new message when it exits; the Agent can wait for it,
answer it or stop it with the terminal Tool, whose results for a command
terminal come from ``command_terminal_result`` in the same shape.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from core.projects import ProjectStore
from core.runs import TOOL_CALL_OUTPUT_EVENT
from core.tools._path_suggestions import similar_entries
from core.tools._powershell import powershell_command
from core.tools._shell_arguments import (
    SHELL_UNADVERTISED_PARAMETERS,
    inherited_env_keys_note,
    normalize_shell_arguments,
    resolve_timeout,
    shell_display_parts,
    split_env_object,
    unknown_env_keys_message,
)
from core.tools._terminal_command import COMMAND_IDLE_SECONDS, CommandReport, StopReason
from core.tools._tool_display import display_notice, display_text
from core.tools._workdir import ProjectWorkdirError, is_project_workdir, project_workdir
from core.tools.arguments import optional_number, optional_string
from core.tools.availability import bash_allowed_env_keys, normalize_env_keys
from core.tools.contracts import ToolContractError
from core.tools.model_names import BASH_TOOL_NAME, SHELL_MODEL_NAME
from core.tools.shell_environment import RunIdentity, command_environment, inherited_value
from core.tools.shell_hints import HINT_TOOL_NAMES, annotate_failure
from core.tools.terminal_manager import (
    TERMINAL_MAX_ROWS,
    TerminalCapacityError,
    TerminalClosedError,
    TerminalManager,
    TerminalManagerError,
    TerminalOwner,
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

SHELL_TOOL_NAME = BASH_TOOL_NAME
# The Tool waits this long before the command continues as a terminal.
SHELL_HANDOFF_SECONDS = 90.0
# The timeout of a foreground command that names none; a background command has none.
SHELL_DEFAULT_TIMEOUT_SECONDS = 600.0
# The output text of one result: head and tail around an omitted middle.
SHELL_OUTPUT_HEAD_CHARS = 4_000
SHELL_OUTPUT_TAIL_CHARS = 8_000
SHELL_OUTPUT_LINE_CHARS = 2_000
# A running command's result shows only its newest output; its log file has all of it.
SHELL_RUNNING_OUTPUT_LINES = 20
SHELL_RUNNING_OUTPUT_CHARS = 4_000
# A running command's output is its transcript and then its whole screen; a
# screen has at most this many rows.
_SCREEN_ROWS = TERMINAL_MAX_ROWS
# Windows rejects longer command lines.
_WINDOWS_COMMAND_LINE_MAX_CHARS = 32_000
_TERMINAL_TOOL = "terminal"


# Definition


if sys.platform == "win32":
    _OPENING = (
        "Run a PowerShell 7 command in a new terminal and return its output and exit code. "
        "Write PowerShell, not bash or cmd: $env:NAME for environment variables, $null instead "
        "of /dev/null, Select-String instead of grep for command output, and single quotes or a "
        "here-string (@'...'@) instead of \\\" escapes and heredocs."
    )
    # How Agents start a program in the background without the Tool.
    _DETACHED_START = "Start-Process or a trailing &"
else:
    _OPENING = (
        "Run a bash command in a new terminal and return its output and exit code. Use bash syntax."
    )
    _DETACHED_START = "nohup or a trailing &"

# The same for every Tool set: what happens to a long or waiting command, and how
# to answer a prompt, the results say when it happens.
SHELL_TOOL_DESCRIPTION = (
    f"{_OPENING} When another tool covers the task, use it instead of a shell command."
)

_COMMAND_PARAMETER: JsonObject = {
    "type": "string",
    "minLength": 1,
    "description": "Shell command to run.",
}
_DESCRIPTION_PARAMETER: JsonObject = {
    "type": "string",
    "description": (
        "Short 3–5 word title for the command’s purpose. Omit when the command is self-explanatory."
    ),
}
_WORKDIR_PARAMETER: JsonObject = {
    "type": "string",
    "description": (
        "Directory to run in, absolute or relative to the working directory. Omit to use the "
        "working directory."
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
# The handler's defaults: SHELL_DEFAULT_TIMEOUT_SECONDS in the foreground, none in
# the background.
_TIMEOUT_PARAMETER: JsonObject = {
    "type": "number",
    "minimum": 0,
    "description": (
        "Seconds before the command is stopped, counted from its start; 0 for no limit. "
        f"Omitted, it is {SHELL_DEFAULT_TIMEOUT_SECONDS:g} in foreground and no limit in "
        "background."
    ),
}
_MODE_PARAMETER: JsonObject = {
    "type": "string",
    "enum": ["foreground", "background"],
    "description": (
        "foreground returns when the command exits, or after "
        f"{SHELL_HANDOFF_SECONDS:g} seconds if it still runs then. background returns at "
        "once; use it for servers, watchers and other commands whose "
        f"result your next step does not need, instead of {_DETACHED_START}. Omit for "
        "foreground."
    ),
}

SHELL_TOOL_PARAMETERS: JsonObject = {
    "type": "object",
    "properties": {
        "command": _COMMAND_PARAMETER,
        "description": _DESCRIPTION_PARAMETER,
        "workdir": _WORKDIR_PARAMETER,
        "timeout": _TIMEOUT_PARAMETER,
        "env_keys": _ENV_KEYS_PARAMETER,
        "mode": _MODE_PARAMETER,
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
    timeout_seconds: float | None
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


def _start_failure(error: Exception, follow_up: bool) -> JsonObject:
    """The failure for a shell that could not be started; *follow_up* when terminal is offered."""
    if isinstance(error, TerminalCapacityError):
        message = (
            f"{SHELL_MODEL_NAME} was not run: vBot already runs {error.limit} commands, the "
            "most it runs at once."
        )
        if follow_up:
            message += (
                f" Find your running commands with {_TERMINAL_TOOL} "
                f"{json.dumps({'action': 'list'})}, stop the ones you no longer need with "
                'action "kill", then run the command again.'
            )
        else:
            message += " Run the command again once one of your running commands has exited."
        return tool_failure("command_limit", message, retryable=True)
    if isinstance(error, TerminalClosedError):
        return tool_failure(
            "command_not_started",
            f"{SHELL_MODEL_NAME} was not run: this turn was cancelled, or vBot is shutting down.",
            retryable=False,
        )
    cause = error.__cause__ if error.__cause__ is not None else error
    return tool_failure(
        "command_not_started",
        f"{SHELL_MODEL_NAME} was not run: the shell could not be started ({cause}). Run the "
        "command again; if it fails the same way, tell the user.",
        retryable=True,
    )


def _parse_call(
    context: ToolContext, arguments: JsonObject, projects: ProjectStore | None
) -> _ShellCall:
    notes: list[str] = []
    command = arguments.get("command")
    if not isinstance(command, str) or not command.strip():
        raise _not_run("command is required; pass the command line to run as a string.")
    description = optional_string(arguments.get("description"), field_name="description")
    mode = arguments.get("mode")
    if mode not in (None, "foreground", "background"):
        raise _not_run(f"mode must be foreground or background, not {mode!r}.")
    timeout, note = resolve_timeout(
        optional_number(arguments.get("timeout"), field_name="timeout"),
        optional_number(arguments.get("timeout_ms"), field_name="timeout_ms"),
    )
    if note is not None:
        notes.append(note)
    if timeout is not None and timeout < 0:
        raise _not_run("timeout must be 0 or more seconds.")
    background = mode == "background"
    if timeout is None:
        # A background command runs until it exits: servers and watchers never finish.
        timeout_seconds = None if background else SHELL_DEFAULT_TIMEOUT_SECONDS
    else:
        timeout_seconds = timeout if timeout > 0 else None

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
    names = list(dict.fromkeys([*requested, *env_credentials]))
    ungranted = [name for name in names if name not in granted]
    if ungranted:
        unknown = [name for name in ungranted if inherited_value(name) is None]
        if unknown:
            raise ToolContractError(unknown_env_keys_message(unknown, granted))
        notes.append(inherited_env_keys_note(ungranted))
    return _ShellCall(
        command=command,
        description=description.strip() if description and description.strip() else None,
        workdir=workdir,
        timeout_seconds=timeout_seconds,
        background=background,
        variables=variables,
        credential_names=tuple(name for name in names if name in granted),
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


def _shell_argv(command: str, environment: dict[str, str]) -> list[str]:
    """The host shell started on *command*."""
    search_path = next(
        (value for name, value in environment.items() if name.upper() == "PATH"), None
    )
    if sys.platform == "win32":
        pwsh = shutil.which("pwsh", path=search_path)
        if pwsh is None:
            raise _not_run(
                "PowerShell 7 (pwsh) is not installed on this host. Ask the user to install it."
            )
        argv = [pwsh, "-NoProfile", "-Command", powershell_command(command)]
        length = len(subprocess.list2cmdline(argv))
        if length > _WINDOWS_COMMAND_LINE_MAX_CHARS:
            room = max(0, _WINDOWS_COMMAND_LINE_MAX_CHARS - (length - len(command)))
            raise _not_run(
                f"the command is {len(command):,} characters long, and the Windows command "
                f"line takes at most about {room:,} for it. Save the script to a .ps1 file and "
                "run that file."
            )
        return argv
    bash = shutil.which("bash", path=search_path)
    if bash is None:
        raise _not_run("bash is not installed on this host. Ask the user to install it.")
    return [bash, "-c", command]


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
    """Runs shell commands for Agents through the TerminalManager."""

    def __init__(
        self,
        terminal_manager: TerminalManager,
        *,
        credential_resolver: CredentialResolver | None = None,
        update_handoffs: UpdateHandoffs | None = None,
        projects: ProjectStore | None = None,
    ) -> None:
        self._terminals = terminal_manager
        self._resolve_credential = credential_resolver or (lambda _name: "")
        self._update_handoffs = update_handoffs
        self._projects = projects
        # Tasks that outlive their call: releasing handoff tokens, stopping commands.
        self._tasks: set[asyncio.Task[Any]] = set()

    async def __call__(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        call = _parse_call(context, arguments, self._projects)
        handoff = _issue_update_handoff(self._update_handoffs, context)
        try:
            terminal_id = await self._start(context, call, handoff)
        except _NotStartedError as failure:
            if handoff is not None:
                handoff.release()
            return failure.result
        except BaseException:
            if handoff is not None:
                handoff.release()
            raise
        if handoff is not None:
            self._keep(self._release_when_finished(terminal_id, handoff))
        if call.background:
            self._terminals.hand_off_command(terminal_id, deliver=True)
            return await self._result(context, call, terminal_id, reason="requested")
        return await self._wait(context, call, terminal_id)

    async def _start(
        self, context: ToolContext, call: _ShellCall, handoff: UpdateHandoffGrant | None
    ) -> str:
        credentials = {name: self._resolve_credential(name) for name in call.credential_names}
        environment = await asyncio.to_thread(
            command_environment,
            RunIdentity(
                context.agent_id,
                context.session_id,
                context.project_id,
                handoff.token if handoff is not None else None,
            ),
            variables=call.variables,
            credentials=credentials,
        )
        argv = _shell_argv(call.command, environment)
        # The delivery names only Tools offered now; it outlives this context.
        offered = frozenset(
            name for name in (*HINT_TOOL_NAMES, _TERMINAL_TOOL) if context.offers(name)
        )
        try:
            return await self._terminals.spawn_command(
                TerminalOwner(context.project_id, context.agent_id, context.session_id),
                argv,
                command=call.command,
                description=call.description,
                cwd=call.workdir,
                env=environment,
                timeout_seconds=call.timeout_seconds,
                formatter=partial(format_command_delivery, offers=offered.__contains__),
                origin_run_id=context.run_id,
                execution_owner=context.execution_owner,
            )
        except (OSError, TerminalManagerError) as error:
            raise _NotStartedError(_start_failure(error, context.offers(_TERMINAL_TOOL))) from error

    async def _wait(self, context: ToolContext, call: _ShellCall, terminal_id: str) -> JsonObject:
        interrupt = asyncio.Event()
        moved_by_user = False

        def move_to_background() -> bool:
            nonlocal moved_by_user
            if self._terminals.command_report(terminal_id).exited or interrupt.is_set():
                return False
            moved_by_user = True
            interrupt.set()
            return True

        def cancel() -> asyncio.Task[Any]:
            reason: StopReason = "run_cancelled" if context.is_cancelled() else "user"
            return self._keep(self._terminals.stop_command(terminal_id, reason))

        if context.background_registration_hook is not None:
            context.background_registration_hook(move_to_background)
        context.on_cancel(cancel)

        async def progress(screen: str) -> None:
            await context.emit(
                TOOL_CALL_OUTPUT_EVENT,
                {
                    "tool_call_id": context.tool_call_id,
                    "terminal_id": terminal_id,
                    "screen": screen,
                },
            )

        waiting = asyncio.ensure_future(
            self._terminals.wait_command(
                terminal_id,
                seconds=SHELL_HANDOFF_SECONDS,
                # Idleness hands a command off only to an Agent that can answer it.
                idle_seconds=COMMAND_IDLE_SECONDS if context.offers(_TERMINAL_TOOL) else None,
                progress=progress,
            )
        )
        interrupted = asyncio.ensure_future(interrupt.wait())
        try:
            await asyncio.wait({waiting, interrupted}, return_when=asyncio.FIRST_COMPLETED)
        except asyncio.CancelledError:
            if not self._terminals.command_report(terminal_id).exited:
                self._keep(self._terminals.stop_command(terminal_id, "run_cancelled"))
            raise
        finally:
            for task in (waiting, interrupted):
                task.cancel()
            await asyncio.gather(waiting, interrupted, return_exceptions=True)

        error = waiting.exception() if waiting.done() and not waiting.cancelled() else None
        if error is not None:
            raise error
        outcome = "moved" if moved_by_user else waiting.result()
        report = self._terminals.command_report(terminal_id)
        if report.exited:
            if report.still_running:
                # Listed, so the user and the Agent can see and stop what remains.
                self._terminals.hand_off_command(terminal_id, deliver=False)
        else:
            self._terminals.hand_off_command(terminal_id, deliver=True)
        return await self._result(context, call, terminal_id, reason=outcome)

    async def _result(
        self, context: ToolContext, call: _ShellCall, terminal_id: str, *, reason: str
    ) -> JsonObject:
        report, screen = await _report_with_screen(self._terminals, terminal_id)
        idle_seconds = None
        if reason == "idle" and not report.exited:
            # The hand-off found it idle; the text names how long it has been.
            idle_seconds = await self._terminals.command_idle_seconds(terminal_id)
        data = _result_data(
            context, report, screen=screen, reason=reason, idle_seconds=idle_seconds
        )
        if call.notes:
            data["notes"] = list(call.notes)
        return tool_success(data)

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

    async def _release_when_finished(self, terminal_id: str, handoff: UpdateHandoffGrant) -> None:
        """Keep the handoff token claimable exactly while the command's processes run."""
        try:
            await self._terminals.wait_finished(terminal_id)
        except TerminalManagerError:
            pass
        finally:
            handoff.release()


# Results


async def command_terminal_result(
    terminals: TerminalManager,
    context: ToolContext,
    terminal_id: str,
    *,
    wait_ended: str | None = None,
) -> JsonObject:
    """The current result of a command terminal, in the shape of a shell result.

    Returns the ``data`` of a Tool result, not wrapped in ``tool_success``:
    ``status`` (``running``, ``exited`` or ``stopped``) and, as they apply,
    ``terminal_id``, ``exit_code``, ``stopped_because``, ``output``,
    ``failed_programs``, ``still_running``, ``hint`` and ``next``, plus
    ``wait_ended`` when given.
    A running command's ``next`` follows from how the wait ended
    (``matched``, ``timeout``, or no wait), unless the command is idle: then
    it is the idle text with the seconds it has been idle.
    Raises ``TerminalManagerError`` for an unknown terminal or one that runs no
    command.
    """
    report, screen = await _report_with_screen(terminals, terminal_id)
    reason = {"matched": "matched", "timeout": "waited"}.get(wait_ended or "", "following")
    idle_seconds = None
    if not report.exited and reason != "matched":
        idle_seconds = await terminals.command_idle_seconds(terminal_id)
    data = _result_data(
        context,
        report,
        screen=screen,
        reason="idle" if idle_seconds is not None else reason,
        idle_seconds=idle_seconds,
    )
    if wait_ended is not None:
        data["wait_ended"] = wait_ended
    return data


async def _report_with_screen(
    terminals: TerminalManager, terminal_id: str
) -> tuple[CommandReport, str]:
    """The command's report and the output that follows its transcript.

    While the shell runs, that is its whole screen; afterwards, what processes
    it left running printed that the transcript does not hold yet.
    """
    return await terminals.command_view(terminal_id, _SCREEN_ROWS)


def _result_data(
    context: ToolContext,
    report: CommandReport,
    *,
    screen: str = "",
    reason: str = "continues",
    idle_seconds: float | None = None,
) -> JsonObject:
    """The result data of one command; *reason* says why a running command was returned,
    and *idle_seconds* how long an idle one has been idle."""
    if not report.exited:
        output = _running_output_text(report, screen)
        running: JsonObject = {
            "status": "running",
            "terminal_id": report.terminal_id,
            "output": output,
        }
        running["next"] = _running_text(context, report, reason=reason, idle_seconds=idle_seconds)
        return running
    data: JsonObject = {"status": "stopped" if report.stop_reason else "exited"}
    if report.still_running:
        data["terminal_id"] = report.terminal_id
    if report.exit_code is not None:
        data["exit_code"] = report.exit_code
    if report.stop_reason is not None:
        data["stopped_because"] = _stop_text(report, with_duration=True)
    output = command_output_text(report, screen=screen)
    data["output"] = output
    failed = _failed_programs(report)
    if failed:
        data["failed_programs"] = list(failed)
    if report.still_running:
        data["still_running"] = [_process_text(process) for process in report.still_running]
    hint = _failure_hint(report, output, context.offers)
    if hint is not None:
        data["hint"] = hint
    if report.still_running:
        data["next"] = _still_running_text(context, report)
    return data


def _failed_programs(report: CommandReport) -> tuple[str, ...]:
    """The failed programs that tell more than the exit code.

    A single failed program whose code is the command's exit code repeats it.
    Each entry reads ``<name> exited with code <code>``, with the hex form after
    large codes.
    """
    exits = report.nonzero_exits
    if len(exits) == 1:
        code = exits[0].rpartition(" exited with code ")[2].split(" ", 1)[0]
        if code == str(report.exit_code):
            return ()
    return exits


def _failure_hint(report: CommandReport, output: str, offers: Callable[[str], bool]) -> str | None:
    """A hint for a command that failed on its own; a stop by vBot explains itself."""
    if report.stop_reason is not None:
        return None
    return annotate_failure(
        report.command, report.exit_code, output, workdir=report.workdir, offers=offers
    )


def _stop_text(report: CommandReport, *, with_duration: bool = False) -> str:
    """Why vBot stopped the command; *with_duration* adds when the user stopped it."""
    reason = report.stop_reason
    if reason == "timeout":
        limit = (
            f"its {report.timeout_seconds:g}-second timeout"
            if report.timeout_seconds
            else "its timeout"
        )
        return (
            f"it was still running at {limit}. Check the output before you run it again. If "
            "it needs more time, run it again with a larger timeout, or 0 for no limit; run "
            "servers and watchers with mode background."
        )
    if reason == "user":
        if with_duration:
            return f"the user stopped it after {_duration(report.elapsed_seconds)}."
        return "the user stopped it."
    if reason == "run_cancelled":
        return "the turn that started it was cancelled."
    if reason == "shutdown":
        return "vBot shut down."
    return "you stopped it."


def _process_text(process: Any) -> str:
    return f"{process.name} (pid {process.pid})"


def _terminal_call(action: str, terminal_id: str) -> str:
    """A complete terminal call, as the Agent writes it."""
    return f"{_TERMINAL_TOOL} {json.dumps({'action': action, 'terminal_id': terminal_id})}"


def _still_running_text(context: ToolContext, report: CommandReport) -> str:
    names = ", ".join(_process_text(process) for process in report.still_running)
    limit = _leftover_limit_text(report)
    if context.offers(_TERMINAL_TOOL):
        until = f"; {limit}" if limit else ""
        return (
            f"The shell exited, but processes the command started still run: {names}{until}. "
            f"Stop them with {_terminal_call('kill', report.terminal_id)} when they are no "
            "longer needed."
        )
    if limit:
        return (
            f"The shell exited, but processes the command started still run: {names}. They "
            f"keep running until they exit, vBot stops, or {limit}."
        )
    return (
        f"The shell exited, but processes the command started still run: {names}. They "
        "keep running until they exit or vBot stops."
    )


def _leftover_limit_text(report: CommandReport) -> str:
    """When the command's timeout stops the processes its shell left running, as a clause."""
    remaining = report.timeout_remaining_seconds
    if remaining is None:
        return ""
    return (
        f"the command's {report.timeout_seconds:g}-second timeout stops them in "
        f"{_duration(remaining)}"
    )


def _duration(seconds: float) -> str:
    """A duration in words: ``45 seconds``, ``14 minutes 5 seconds``, ``1 hour 2 minutes``."""
    total = max(0, round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [
        f"{count} {unit}{'' if count == 1 else 's'}"
        for count, unit in ((hours, "hour"), (minutes, "minute"))
        if count
    ]
    if secs or not parts:
        parts.append(f"{secs} second{'' if secs == 1 else 's'}")
    return " ".join(parts)


# Every command handed off while its shell runs delivers its result when it ends.
_RESULT_ARRIVES = "Its result arrives as a new message when it exits."
# Agents restarted handed-off commands, or slept and polled until they ended.
_NO_REPEAT = "do not start it again, and do not sleep or poll for its result."


def _log_text(report: CommandReport) -> str:
    """Where a running command's complete output is, as a sentence after a space."""
    path = report.transcript.log_path
    if path is None:
        return ""
    # The result shows only the newest output; Agents did not know what a bare
    # log_file field held.
    return f" The command's complete output is written live to {model_path(path)}."


def _limit_text(report: CommandReport) -> str:
    """The time limit of a running command, as a clause."""
    remaining = report.timeout_remaining_seconds
    if remaining is None:
        return "it has no timeout"
    return f"its {report.timeout_seconds:g}-second timeout stops it in {_duration(remaining)}"


def _running_text(
    context: ToolContext,
    report: CommandReport,
    *,
    reason: str,
    idle_seconds: float | None = None,
) -> str:
    """What to do about a command that keeps running after the result.

    *reason* is ``idle`` for a command that printed nothing and used no CPU
    (for *idle_seconds*), ``moved`` when the user moved it to the background;
    for a terminal call, ``matched`` when a wait matched its pattern,
    ``waited`` when a wait timed out and ``following`` for a status or an
    input; ``requested`` for a command started in the background; otherwise
    the shell call handed the command off.
    """
    terminal_id = report.terminal_id
    limit = _limit_text(report)
    log = _log_text(report)
    follow_up = context.offers(_TERMINAL_TOOL)
    if reason == "idle" and follow_up:
        silent = COMMAND_IDLE_SECONDS if idle_seconds is None else idle_seconds
        return (
            f"The command in terminal {terminal_id} has printed nothing for "
            f"{_duration(silent)} and uses no CPU; {limit}.{log} If its output ends in "
            f'a question or prompt, answer it with terminal action "input", terminal_id '
            f'"{terminal_id}", your answer as text, and key "enter". Otherwise it waits for '
            f"something else, such as the network. {_RESULT_ARRIVES} If it hangs, stop it with "
            f"{_terminal_call('kill', terminal_id)}."
        )
    if reason in {"matched", "waited", "following"} and follow_up:
        return _followed_text(report, f"{limit}.{log}", matched=reason == "matched")
    where = f"in terminal {terminal_id}" if follow_up else "in the background"
    if reason == "requested":
        place = f"in the background in terminal {terminal_id}" if follow_up else "in the background"
        opening = f"The command runs {place}; {limit}."
    elif reason == "moved":
        opening = (
            "The user moved the command to the background after "
            f"{_duration(report.elapsed_seconds)}. It keeps running {where}; {limit}."
        )
    elif reason == "deadline":
        opening = (
            f"The command was still running after {SHELL_HANDOFF_SECONDS:g} seconds and keeps "
            f"running {where}; {limit}."
        )
    else:
        opening = f"The command keeps running {where}; {limit}."
    opening += log
    continuation = (
        f"Continue other work, or end your turn if your next step needs the result; {_NO_REPEAT}"
    )
    if reason == "requested" and follow_up:
        # Started in the background: often a server, whose result arrives only
        # once it is stopped.
        return (
            f"{opening} {_RESULT_ARRIVES} If it runs until stopped, such as a server, and your "
            f"next step needs it ready, call {_terminal_call('wait', terminal_id)} with pattern "
            f"set to a line it prints when ready. Otherwise {continuation[0].lower()}"
            f"{continuation[1:]}"
        )
    return f"{opening} {_RESULT_ARRIVES} {continuation}"


def _followed_text(report: CommandReport, limit: str, *, matched: bool) -> str:
    """What to do about a running command a terminal wait, status or input returned.

    *limit* ends the first sentence: the time limit and where the output is.
    After a match the Agent continues with its next step; otherwise with other
    work, until the result arrives.
    """
    if matched:
        return (
            f"The command keeps running; {limit} {_RESULT_ARRIVES} Continue with your next "
            "step; do not start it again."
        )
    return (
        f"The command has run for {_duration(report.elapsed_seconds)} and keeps running; "
        f"{limit} {_RESULT_ARRIVES} Continue other work, or end your turn if your next step "
        "needs the result; do not sleep or poll for its result. Stop it with "
        f"{_terminal_call('kill', report.terminal_id)} when it is no longer needed."
    )


def command_output_text(report: CommandReport, *, screen: str = "") -> str:
    """The output for a result: head and tail within the character budget.

    *screen* is the screen of a running command, which follows its transcript.
    A marker names the file with the complete output where anything is left out.
    """
    transcript = report.transcript
    where = _full_output(report)
    head = [_shortened(line, where) for line in transcript.head]
    tail = [_shortened(line, where) for line in transcript.tail]
    screen_lines = screen.splitlines()
    tail.extend(_shortened(line, where) for line in screen_lines)
    omitted = transcript.omitted_lines
    head, dropped_head = _within(head, SHELL_OUTPUT_HEAD_CHARS, keep="start")
    tail, dropped_tail = _within(tail, SHELL_OUTPUT_TAIL_CHARS, keep="end")
    omitted += dropped_head + dropped_tail
    lines = list(head)
    if omitted:
        noun = "line" if omitted == 1 else "lines"
        lines.append(f"[... {omitted:,} {noun} omitted{where} ...]")
    lines.extend(tail)
    return "\n".join(lines)


def _running_output_text(report: CommandReport, screen: str) -> str:
    """The newest output of a running command: whole lines within the running limits."""
    transcript = report.transcript
    screen_lines = screen.splitlines()
    lines = [*transcript.head, *transcript.tail, *screen_lines]
    where = _full_output(report)
    newest = [_shortened(line, where) for line in lines[-SHELL_RUNNING_OUTPUT_LINES:]]
    kept, _ = _within(newest, SHELL_RUNNING_OUTPUT_CHARS, keep="end")
    omitted = transcript.total_lines + len(screen_lines) - len(kept)
    if not omitted:
        return "\n".join(kept)
    noun = "line" if omitted == 1 else "lines"
    marker = f"[... {omitted:,} earlier {noun} omitted{where} ...]"
    return "\n".join([marker, *kept])


def _full_output(report: CommandReport) -> str:
    """Where the omitted output is, as a clause of a marker."""
    path = report.transcript.log_path
    return f"; the complete output is in {model_path(path)}" if path is not None else ""


def _shortened(line: str, where: str) -> str:
    """*line* cut to the line budget; *where* names the complete output."""
    if len(line) <= SHELL_OUTPUT_LINE_CHARS:
        return line
    rest = len(line) - SHELL_OUTPUT_LINE_CHARS
    return f"{line[:SHELL_OUTPUT_LINE_CHARS]}[... {rest:,} more characters{where}]"


def _within(lines: list[str], budget: int, *, keep: str) -> tuple[list[str], int]:
    """Keep whole lines from one end while they fit *budget* characters."""
    ordered = lines if keep == "start" else list(reversed(lines))
    kept: list[str] = []
    used = 0
    for line in ordered:
        if used + len(line) + 1 > budget and kept:
            break
        kept.append(line)
        used += len(line) + 1
    dropped = len(lines) - len(kept)
    return (kept if keep == "start" else list(reversed(kept))), dropped


def _offers_nothing(_name: str) -> bool:
    return False


def format_command_delivery(
    report: CommandReport, *, offers: Callable[[str], bool] = _offers_nothing
) -> str:
    """The message that delivers a handed-off command's result when it ends.

    *offers* tells which Tools the message can name.
    """
    subject = report.description or _first_line(report.command)
    ran = _duration(report.elapsed_seconds)
    if report.stop_reason is not None:
        status = f"was stopped after {ran}: {_stop_text(report)}"
    elif report.exit_code is None:
        status = f"exited after {ran}; its exit code is unknown."
    else:
        status = f"exited with code {report.exit_code} after {ran}."
    lines = [f"The command in terminal {report.terminal_id} ({subject}) {status}"]
    if report.description:
        # The subject names the purpose; the Agent needs the exact command too.
        lines.append(f"Command: {_capped(report.command, _DELIVERY_COMMAND_CHARS)}")
    output = command_output_text(report)
    hint = _failure_hint(report, output, offers)
    if hint is not None:
        lines.append(f"Hint: {hint}")
    # The status line of a stopped command names no exit code to repeat.
    failed = report.nonzero_exits if report.stop_reason is not None else _failed_programs(report)
    if failed:
        lines.append(f"Failed programs: {'; '.join(failed)}.")
    if report.still_running:
        names = ", ".join(_process_text(process) for process in report.still_running)
        stop = (
            f" Stop them with {_terminal_call('kill', report.terminal_id)} when they are no "
            "longer needed."
            if offers(_TERMINAL_TOOL)
            else ""
        )
        limit = _leftover_limit_text(report)
        until = f"; {limit}" if limit else ""
        lines.append(f"Processes it started still run: {names}{until}.{stop}")
    lines.append("Output:" if output else "Output: (none)")
    if output:
        lines.append(output)
    return "\n".join(lines)


# A delivery shows the command up to this many characters.
_DELIVERY_COMMAND_CHARS = 1_000


def _capped(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _first_line(command: str) -> str:
    line = command.strip().splitlines()[0] if command.strip() else command
    return line if len(line) <= 120 else line[:117] + "..."


# Background command statuses

# The durable records the status fold reads: results of this Tool, and the
# delivered results of handed-off commands, which start with this text.
COMMAND_STATUS_TOOL_NAMES = (SHELL_TOOL_NAME,)
COMMAND_STATUS_NOTE_MARKER = "The command in terminal "
_DELIVERY_STATUS = re.compile(
    r"The command in terminal (?P<terminal>term_[A-Za-z0-9]+) \(.*?\) "
    r"(?:exited(?: with code (?P<code>-?\d+|None))?(?: after [^.;]*)?[.;]"
    r"|(?P<stopped>was stopped)(?: after [^:]*)?:)"
)


def background_command_statuses(records: Sequence[Any]) -> JsonObject:
    """Fold durable shell results and deliveries into the statuses of handed-off commands.

    Maps each terminal id to ``running``, ``completed`` (exit code 0),
    ``failed`` (another exit code) or ``stopped``. Folding a later range of
    records onto an earlier fold equals folding both ranges at once.
    """
    statuses: JsonObject = {}
    for record in records:
        role = getattr(record, "role", None)
        content = getattr(record, "content", None)
        if not isinstance(content, str):
            continue
        if role == "tool" and getattr(record, "name", None) == SHELL_TOOL_NAME:
            _fold_result(statuses, content)
        elif role == "note":
            for match in _DELIVERY_STATUS.finditer(content):
                statuses[match["terminal"]] = _delivered_status(match)
    return statuses


def _delivered_status(match: re.Match[str]) -> str:
    if match["stopped"]:
        return "stopped"
    return "completed" if match["code"] == "0" else "failed"


def _fold_result(statuses: JsonObject, content: str) -> None:
    try:
        envelope = json.loads(content)
    except json.JSONDecodeError:
        return
    data = envelope.get("data") if isinstance(envelope, dict) and envelope.get("ok") else None
    if not isinstance(data, dict) or data.get("status") != "running":
        return
    terminal_id = data.get("terminal_id")
    if isinstance(terminal_id, str) and terminal_id:
        statuses.setdefault(terminal_id, "running")


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
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
        blocks.append(display_notice("warning", f"The command exited with code {exit_code}."))
    failed = data.get("failed_programs")
    if isinstance(failed, list) and failed:
        blocks.append(display_notice("warning", "; ".join(str(item) for item in failed)))
    if isinstance(data.get("hint"), str):
        blocks.append(display_notice("info", f"Hint: {data['hint']}"))
    if data.get("status") == "running":
        blocks.append(display_notice("info", "The command continues in a terminal."))
    return blocks


def register_shell_tool(
    registry: ToolRegistry,
    terminal_manager: TerminalManager,
    *,
    credential_resolver: CredentialResolver | None = None,
    prompt_blocks: ToolPromptBlockRegistry | None = None,
    update_handoffs: UpdateHandoffs | None = None,
    projects: ProjectStore | None = None,
) -> ShellTool:
    """Register the shell Tool with a vBot Tool registry.

    *projects* resolves ``workdir: "project:<project-id>"``; without it that form fails.
    """
    tool = ShellTool(
        terminal_manager,
        credential_resolver=credential_resolver,
        update_handoffs=update_handoffs,
        projects=projects,
    )
    registry.register(
        SHELL_TOOL_NAME,
        SHELL_TOOL_DESCRIPTION,
        SHELL_TOOL_PARAMETERS,
        tool,
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
    "SHELL_DEFAULT_TIMEOUT_SECONDS",
    "SHELL_HANDOFF_SECONDS",
    "SHELL_TOOL_DESCRIPTION",
    "SHELL_TOOL_NAME",
    "SHELL_TOOL_PARAMETERS",
    "ShellTool",
    "background_command_statuses",
    "command_output_text",
    "command_terminal_result",
    "format_command_delivery",
    "format_shell_env_usage",
    "register_shell_tool",
    "shell_detail_blocks",
]
