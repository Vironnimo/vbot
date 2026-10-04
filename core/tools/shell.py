"""The shell Tool: one command per call, each in a new terminal.

Every call starts the host shell (PowerShell 7 on Windows, bash elsewhere) on
the command in a new Terminal Session of kind ``command`` and waits for it. The
result is the command's rendered output - what a person would have seen in the
terminal - with the shell's exit code and the facts that exit code hides:
programs that failed inside the command, processes it left running, why vBot
stopped it when vBot did, and a hint for well-known failures.

A command that is still running after the hand-off time, waits for input, or
is moved to the background by the user continues as a listed terminal. At
Session depth 0 its result arrives as a new message when it exits; the Agent
can wait for it, answer it or stop it with the terminal Tool, whose results for
a command terminal come from ``command_terminal_result`` in the same shape.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Coroutine, Sequence
from copy import deepcopy
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
# The Tool waits this long at Session depth 0 before the command continues as a terminal.
SHELL_HANDOFF_SECONDS = 90.0
# The timeout of a foreground command that names none; a background command has none.
SHELL_DEFAULT_TIMEOUT_SECONDS = 600.0
# The output text of one result: head and tail around an omitted middle.
SHELL_OUTPUT_HEAD_CHARS = 4_000
SHELL_OUTPUT_TAIL_CHARS = 8_000
SHELL_OUTPUT_LINE_CHARS = 2_000
# A running command's output is its transcript and then its whole screen; a
# screen has at most this many rows.
_SCREEN_ROWS = TERMINAL_MAX_ROWS
# Windows rejects longer command lines.
_WINDOWS_COMMAND_LINE_MAX_CHARS = 32_000
_TERMINAL_TOOL = "terminal"
_WEB_FETCH_TOOL = "web_fetch"

BACKGROUND_AT_DEPTH_FAILURE_CODE = "background_unavailable_in_subagent"


# Definition


# The file Tools the description points to, by use; each use names the first of
# its Tools that is offered. A route offers one file edit dialect: apply_patch,
# or write and edit. Registry names, literal to avoid importing the file Tools.
_FILE_TOOL_USES = (
    ("reading", ("read",)),
    ("searching", ("search_files",)),
    ("creating", ("apply_patch", "write")),
    ("editing", ("apply_patch", "edit")),
)
# The Tools a usual request offers alongside the shell: the canonical description's.
_USUAL_TOOLS = frozenset({"read", "search_files", "apply_patch", _WEB_FETCH_TOOL, _TERMINAL_TOOL})

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


def _joined(words: Sequence[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _neighbor_sentence(offered: frozenset[str]) -> str:
    """Which offered Tools to use instead of shell commands for files and web pages."""
    uses: list[tuple[str, str]] = []
    for use, names in _FILE_TOOL_USES:
        name = next((name for name in names if name in offered), None)
        if name is not None:
            uses.append((use, name))
    parts: list[str] = []
    if uses:
        tools = list(dict.fromkeys(name for _, name in uses))
        parts.append(
            f"for {_joined([use for use, _ in uses])} files, use {_joined(tools)} "
            "instead of shell commands"
        )
    if _WEB_FETCH_TOOL in offered:
        parts.append(f"for web pages, use {_WEB_FETCH_TOOL}")
    if not parts:
        return ""
    sentence = "; ".join(parts)
    return f" {sentence[0].upper()}{sentence[1:]}."


def _shell_description(offered: frozenset[str] | None, *, nesting_depth: int) -> str:
    """The description for the Tools offered with the shell, at this Session depth.

    *offered* None stands for the usual set (``_USUAL_TOOLS``).
    """
    offered = _USUAL_TOOLS if offered is None else offered
    terminal = _TERMINAL_TOOL in offered
    if nesting_depth < 1 and terminal:
        continuation = (
            f" A command still running after {SHELL_HANDOFF_SECONDS:g} seconds, or waiting for "
            "input, keeps running in a terminal, and the result says how to follow it up."
        )
    elif nesting_depth < 1:
        continuation = (
            f" A command still running after {SHELL_HANDOFF_SECONDS:g} seconds keeps running "
            "in the background, and its result arrives as a new message when it exits."
        )
    elif terminal:
        continuation = (
            " A command waiting for input keeps running in a terminal, and the result says "
            "how to follow it up."
        )
    else:
        continuation = ""
    boundary = (
        " For programs you operate by typing into them, such as REPLs, TUIs and coding-agent "
        f"CLIs, use {_TERMINAL_TOOL}."
        if terminal
        else ""
    )
    return (
        f"{_OPENING}{_neighbor_sentence(offered)} Output is rendered terminal text. No editor or "
        "credential prompt is available: pass commit messages and answers as arguments."
        f"{continuation}{boundary}"
    )


SHELL_TOOL_DESCRIPTION = _shell_description(None, nesting_depth=0)

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
_MODE_PARAMETER: JsonObject = {
    "type": "string",
    "enum": ["foreground", "background"],
    "description": (
        f"foreground waits up to {SHELL_HANDOFF_SECONDS:g} seconds for the command to exit. "
        "background returns at once; use it for servers, watchers and other commands whose "
        f"result your next step does not need, instead of {_DETACHED_START}. A background "
        "command's result arrives as a new message when it exits. Omit for foreground."
    ),
}


def _timeout_parameter(*, subagent: bool) -> JsonObject:
    # The handler's defaults: SHELL_DEFAULT_TIMEOUT_SECONDS in the foreground, none
    # in the background, which a Sub-Agent cannot choose.
    omitted = (
        f"Omitted, it is {SHELL_DEFAULT_TIMEOUT_SECONDS:g}."
        if subagent
        else f"Omitted, it is {SHELL_DEFAULT_TIMEOUT_SECONDS:g} in foreground and no limit in "
        "background."
    )
    return {
        "type": "number",
        "minimum": 0,
        "description": (
            "Seconds before the command is stopped, counted from its start; 0 for no limit. "
            f"{omitted}"
        ),
    }


def _shell_parameters(*, subagent: bool) -> JsonObject:
    properties: JsonObject = {
        "command": _COMMAND_PARAMETER,
        "description": _DESCRIPTION_PARAMETER,
        "workdir": _WORKDIR_PARAMETER,
        "timeout": _timeout_parameter(subagent=subagent),
        "env_keys": _ENV_KEYS_PARAMETER,
    }
    if not subagent:
        properties["mode"] = _MODE_PARAMETER
    return {"type": "object", "properties": properties, "required": ["command"]}


SHELL_TOOL_PARAMETERS = _shell_parameters(subagent=False)
_SUBAGENT_SHELL_TOOL_PARAMETERS = _shell_parameters(subagent=True)


def project_shell_tool_definitions(
    definitions: list[JsonObject], *, nesting_depth: int
) -> list[JsonObject]:
    """Fit the shell definition to one request: its Session depth and the Tools offered."""
    offered = frozenset(str(definition.get("name")) for definition in definitions)
    description = _shell_description(offered, nesting_depth=nesting_depth)
    if nesting_depth < 1 and description == SHELL_TOOL_DESCRIPTION:
        return definitions
    projected: list[JsonObject] = []
    for definition in definitions:
        if definition.get("name") != SHELL_TOOL_NAME:
            projected.append(definition)
            continue
        fitted = deepcopy(definition)
        fitted["description"] = description
        if nesting_depth >= 1:
            fitted["parameters"] = deepcopy(_SUBAGENT_SHELL_TOOL_PARAMETERS)
        projected.append(fitted)
    return projected


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
        if call.background and context.nesting_depth >= 1:
            return tool_failure(
                BACKGROUND_AT_DEPTH_FAILURE_CODE,
                "Background execution is unavailable inside a Sub-Agent. Use foreground or "
                "omit mode.",
            )
        handoff = _issue_update_handoff(self._update_handoffs, context)
        try:
            terminal_id = await self._start(context, call, handoff)
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
            raise _not_run(f"the shell could not be started: {error}") from error

    async def _wait(self, context: ToolContext, call: _ShellCall, terminal_id: str) -> JsonObject:
        depth = context.nesting_depth
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

        if depth < 1 and context.background_registration_hook is not None:
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
                seconds=SHELL_HANDOFF_SECONDS if depth < 1 else None,
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
            self._terminals.hand_off_command(terminal_id, deliver=depth < 1)
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
    ``log_file`` (only when the output was cut), ``failed_programs``,
    ``still_running``, ``hint`` and ``next``, plus ``wait_ended`` when given.
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
    """The command's report and, while it runs, its whole screen, without gap or overlap.

    The transcript holds the lines that scrolled off the screen, and output is
    rendered and the screen read under the same session lock. The report is taken
    right after the screen, with no await in between, so the transcript ends
    exactly where the screen starts. Once the shell exited, the transcript holds
    the screen as well.
    """
    if terminals.command_report(terminal_id).exited:
        return terminals.command_report(terminal_id), ""
    screen = ""
    # A session that closed meanwhile has a final report that holds every line.
    with contextlib.suppress(TerminalManagerError):
        screen = await terminals.command_screen(terminal_id, _SCREEN_ROWS)
    report = terminals.command_report(terminal_id)
    return report, "" if report.exited else screen


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
        output, truncated = command_output_text(report, screen=screen)
        running: JsonObject = {
            "status": "running",
            "terminal_id": report.terminal_id,
            "output": output,
        }
        if truncated and report.transcript.log_path is not None:
            running["log_file"] = model_path(report.transcript.log_path)
        running["next"] = _running_text(context, report, reason=reason, idle_seconds=idle_seconds)
        return running
    data: JsonObject = {"status": "stopped" if report.stop_reason else "exited"}
    if report.still_running:
        data["terminal_id"] = report.terminal_id
    if report.exit_code is not None:
        data["exit_code"] = report.exit_code
    if report.stop_reason is not None:
        data["stopped_because"] = _stop_text(report, mode=context.nesting_depth < 1)
    output, truncated = command_output_text(report)
    data["output"] = output
    if truncated and report.transcript.log_path is not None:
        data["log_file"] = model_path(report.transcript.log_path)
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


def _stop_text(report: CommandReport, *, mode: bool) -> str:
    """Why vBot stopped the command; *mode* when the Agent can choose background mode."""
    reason = report.stop_reason
    if reason == "timeout":
        limit = (
            f"the {report.timeout_seconds:g}-second timeout"
            if report.timeout_seconds
            else "its timeout"
        )
        servers = "; run servers and watchers with mode background" if mode else ""
        return (
            f"it was still running at {limit}. To let it finish, run it again with a larger "
            f"timeout, or 0 for no limit{servers}."
        )
    if reason == "user":
        return "the user stopped it."
    if reason == "run_cancelled":
        return "the Run that started it was cancelled."
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
    if context.offers(_TERMINAL_TOOL):
        return (
            f"The shell exited, but processes the command started still run: {names}. Stop "
            f"them with {_terminal_call('kill', report.terminal_id)} when they are no longer "
            "needed."
        )
    return (
        f"The shell exited, but processes the command started still run: {names}. They "
        "keep running until they exit or vBot stops."
    )


def _limit_text(report: CommandReport) -> str:
    """The time limit of a running command, as a clause."""
    if not report.timeout_seconds:
        return "it has no timeout"
    remaining = max(0.0, report.timeout_seconds - report.duration_seconds)
    return f"its {report.timeout_seconds:g}-second timeout stops it in {remaining:.0f} seconds"


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
    input; otherwise the shell call handed the command off.
    """
    terminal_id = report.terminal_id
    limit = _limit_text(report)
    follow_up = context.offers(_TERMINAL_TOOL)
    delivered = (
        "Its result arrives as a new message when it exits."
        if report.delivers_result
        else "Its result does not arrive on its own."
    )
    if reason == "idle" and follow_up:
        silent = COMMAND_IDLE_SECONDS if idle_seconds is None else idle_seconds
        return (
            f"The command in terminal {terminal_id} has printed nothing for "
            f"{silent:.0f} seconds and uses no CPU; {limit}. If its output ends in "
            f'a question or prompt, answer it with terminal action "input", terminal_id '
            f'"{terminal_id}", your answer as text, and key "enter". Otherwise it is waiting '
            f"for something else: wait for it with {_terminal_call('wait', terminal_id)}, or "
            f"stop it with {_terminal_call('kill', terminal_id)} if it hangs. {delivered}"
        )
    if reason in {"matched", "waited", "following"} and follow_up:
        return _followed_text(report, limit, matched=reason == "matched", waited=reason == "waited")
    moved = "The user moved the command to the background. " if reason == "moved" else ""
    if not follow_up:
        text = f"{moved}The command keeps running in the background; {limit}."
        if report.delivers_result:
            text += f" {delivered} Continue other work or end your turn; do not start it again."
        return text
    wait = _terminal_call("wait", terminal_id)
    pattern = "add pattern to wait for a line it prints, such as a server's ready line"
    if report.delivers_result:
        return (
            f"{moved}The command keeps running in terminal {terminal_id}; {limit}. {delivered} "
            f"To wait for it now, call {wait}; {pattern}. Otherwise continue other work or end "
            "your turn; do not start it again."
        )
    return (
        f"{moved}The command keeps running in terminal {terminal_id}; {limit}. {delivered[:-1]}"
        f": wait for it with {wait}; {pattern}. Do not start it again."
    )


def _followed_text(report: CommandReport, limit: str, *, matched: bool, waited: bool) -> str:
    """What to do about a running command a terminal wait, status or input returned.

    After a match the Agent continues; otherwise it waits again or stops it.
    """
    wait = _terminal_call("wait", report.terminal_id)
    if matched:
        arrival = (
            "Its result arrives as a new message when it exits."
            if report.delivers_result
            else f"Its result does not arrive on its own; to get it, call {wait}."
        )
        return (
            f"The command keeps running; {limit}. {arrival} Continue with your next step; do "
            "not start it again."
        )
    arrival = (
        "its result arrives as a new message when it exits"
        if report.delivers_result
        else "its result does not arrive on its own"
    )
    again = "Wait again" if waited else "Wait for it"
    return (
        f"The command keeps running; {limit}. {again} with {wait}, or stop it with "
        f"{_terminal_call('kill', report.terminal_id)}; {arrival}."
    )


def command_output_text(report: CommandReport, *, screen: str = "") -> tuple[str, bool]:
    """The output for a result: head and tail within the character budget.

    *screen* is the screen of a running command, which follows its transcript.
    Returns the text and whether anything was left out of it.
    """
    transcript = report.transcript
    head = [_shortened(line) for line in transcript.head]
    tail = [_shortened(line) for line in transcript.tail]
    screen_lines = screen.splitlines()
    tail.extend(_shortened(line) for line in screen_lines)
    shortened = any(
        len(line) > SHELL_OUTPUT_LINE_CHARS
        for line in (*transcript.head, *transcript.tail, *screen_lines)
    )
    omitted = transcript.omitted_lines
    head, dropped_head = _within(head, SHELL_OUTPUT_HEAD_CHARS, keep="start")
    tail, dropped_tail = _within(tail, SHELL_OUTPUT_TAIL_CHARS, keep="end")
    omitted += dropped_head + dropped_tail
    lines = list(head)
    if omitted:
        noun = "line" if omitted == 1 else "lines"
        lines.append(f"[... {omitted:,} {noun} omitted; log_file has the full output ...]")
    lines.extend(tail)
    return "\n".join(lines), bool(omitted) or shortened


def _shortened(line: str) -> str:
    if len(line) <= SHELL_OUTPUT_LINE_CHARS:
        return line
    rest = len(line) - SHELL_OUTPUT_LINE_CHARS
    return f"{line[:SHELL_OUTPUT_LINE_CHARS]}[... {rest:,} more characters in log_file]"


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
    if report.stop_reason is not None:
        # Only depth 0 delivers, where background mode exists.
        status = f"was stopped: {_stop_text(report, mode=True)}"
    else:
        status = f"exited with code {report.exit_code}."
    lines = [f"The command in terminal {report.terminal_id} ({subject}) {status}"]
    output, truncated = command_output_text(report)
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
        lines.append(f"Processes it started still run: {names}.{stop}")
    if truncated and report.transcript.log_path is not None:
        lines.append(f"Full output: {model_path(report.transcript.log_path)}")
    lines.append("Output:" if output else "Output: (none)")
    if output:
        lines.append(output)
    return "\n".join(lines)


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
    r"(?:exited with code (?P<code>-?\d+|None)\.|(?P<stopped>was stopped):)"
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
    "project_shell_tool_definitions",
    "register_shell_tool",
    "shell_detail_blocks",
]
