"""The shell Tool: one command per call, each in a new terminal.

Every call starts the host shell (PowerShell 7 on Windows, bash elsewhere) on
the command in a new Terminal Session of kind ``command`` and waits for it. The
result is the command's rendered output - what a person would have seen in the
terminal - with the shell's exit code and the facts that exit code hides:
programs that failed inside the command, processes it left running, and why
vBot stopped it when vBot did.

A command that is still running after the hand-off time, waits for input, or
is moved to the background by the user continues as a listed terminal. At
Session depth 0 its result arrives as a new message when it exits; the Agent
can answer or stop it with the terminal Tool.
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
from pathlib import Path
from typing import Any

from core.runs import TOOL_CALL_OUTPUT_EVENT
from core.tools._path_suggestions import similar_entries
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
from core.tools.arguments import optional_number, optional_string
from core.tools.availability import bash_allowed_env_keys, normalize_env_keys
from core.tools.contracts import ToolContractError
from core.tools.model_names import BASH_TOOL_NAME, SHELL_MODEL_NAME
from core.tools.shell_environment import RunIdentity, command_environment, inherited_value
from core.tools.terminal_manager import (
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
SHELL_DEFAULT_TIMEOUT_SECONDS = 600.0
# The output text of one result: head and tail around an omitted middle.
SHELL_OUTPUT_HEAD_CHARS = 4_000
SHELL_OUTPUT_TAIL_CHARS = 8_000
SHELL_OUTPUT_LINE_CHARS = 2_000
# Screen rows a running command's result shows after its transcript.
_RUNNING_SCREEN_ROWS = 40
# Windows rejects longer command lines.
_WINDOWS_COMMAND_LINE_MAX_CHARS = 32_000
_TERMINAL_TOOL = "terminal"
# Console encodings for the command's own output and input; without them
# programs print in the console's legacy code page.
_POWERSHELL_UTF8_PREFIX = (
    "[Console]::OutputEncoding=[Console]::InputEncoding=[Text.UTF8Encoding]::new();"
)
# Statements PowerShell accepts only at the start of a script.
_POWERSHELL_LEADING_STATEMENTS = ("using ", "param(", "param ", "#requires", "[cmdletbinding")
# pwsh -Command exits with 1 whenever the last statement failed, also when a native
# program returned another code; this ending passes that program's code on. The
# blank line ends a trailing line continuation; $? is read before anything changes it.
_POWERSHELL_EXIT_STATUS = (
    "\n\nif ($?) { exit 0 }; "
    "$__vbotExitCode = Get-Variable LASTEXITCODE -ValueOnly -ErrorAction Ignore; "
    "if ($__vbotExitCode) { exit $__vbotExitCode }; exit 1"
)
# A script made of named blocks accepts no statement after them.
_POWERSHELL_NAMED_BLOCK = re.compile(r"^\s*(?:begin|process|end|dynamicparam)\s*\{", re.I | re.M)

BACKGROUND_AT_DEPTH_FAILURE_CODE = "background_unavailable_in_subagent"


# Definition


_FILE_TOOL_USES = (("read", "read"), ("search", "search_files"), ("edit", "apply_patch"))


def _joined(words: Sequence[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _shell_description(offered: frozenset[str] | None, *, nesting_depth: int) -> str:
    """The description for the Tools offered with the shell, at this Session depth.

    *offered* None stands for the usual set: the file Tools and the terminal Tool.
    """
    if sys.platform == "win32":
        opening = (
            "Run a PowerShell 7 command in a new terminal and return its output and exit "
            "code. Use PowerShell syntax."
        )
    else:
        opening = (
            "Run a bash command in a new terminal and return its output and exit code. "
            "Use bash syntax."
        )
    uses = [(use, name) for use, name in _FILE_TOOL_USES if offered is None or name in offered]
    output = "Output is rendered terminal text"
    if uses:
        output += (
            f"; use {_joined([name for _, name in uses])} to "
            f"{_joined([use for use, _ in uses])} files"
        )
    terminal = offered is None or _TERMINAL_TOOL in offered
    if nesting_depth < 1 and terminal:
        continuation = (
            f" A command still running after {SHELL_HANDOFF_SECONDS:g} seconds, or waiting "
            "for input, continues as a terminal; the result says how to proceed."
        )
    elif nesting_depth < 1:
        continuation = (
            f" A command still running after {SHELL_HANDOFF_SECONDS:g} seconds continues "
            "in the background; the result says how to proceed."
        )
    elif terminal:
        continuation = (
            " A command waiting for input continues as a terminal; the result says how to proceed."
        )
    else:
        continuation = ""
    return (
        f"{opening} {output}. No editor or credential prompt is available: pass commit "
        f"messages and answers as arguments.{continuation}"
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
_TIMEOUT_PARAMETER: JsonObject = {
    "type": "number",
    "minimum": 0,
    "description": (
        f"Seconds before the command is stopped. Omit for {SHELL_DEFAULT_TIMEOUT_SECONDS:g}; "
        "0 for no limit."
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
        "foreground returns when the command exits, or hands it to the background "
        f"after {SHELL_HANDOFF_SECONDS:g} seconds. background returns at once; "
        "use it for servers, watchers and other commands whose result your next "
        "step does not need. Background results arrive automatically. "
        "Omit for foreground."
    ),
}


def _shell_parameters(*, subagent: bool) -> JsonObject:
    properties: JsonObject = {
        "command": _COMMAND_PARAMETER,
        "description": _DESCRIPTION_PARAMETER,
        "workdir": _WORKDIR_PARAMETER,
        "timeout": _TIMEOUT_PARAMETER,
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


def _parse_call(context: ToolContext, arguments: JsonObject) -> _ShellCall:
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
    timeout_seconds = (
        SHELL_DEFAULT_TIMEOUT_SECONDS if timeout is None else (timeout if timeout > 0 else None)
    )

    raw_workdir = optional_string(arguments.get("workdir"), field_name="workdir")
    workdir = context.resolve_path(raw_workdir) if raw_workdir else context.effective_cwd
    if not workdir.is_dir():
        raise _not_run(_missing_workdir(workdir))

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
        background=mode == "background",
        variables=variables,
        credential_names=tuple(name for name in names if name in granted),
        notes=tuple(notes),
    )


def _missing_workdir(workdir: Path) -> str:
    message = f"workdir {model_path(workdir)} is not an existing directory."
    nearby = similar_entries(workdir, kind="dirs")
    if nearby:
        message += f" Similar directories: {', '.join(model_path(path) for path in nearby)}."
    return message


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
        first = command.lstrip().casefold()
        script = (
            command
            if first.startswith(_POWERSHELL_LEADING_STATEMENTS)
            else _POWERSHELL_UTF8_PREFIX + command
        )
        if not _POWERSHELL_NAMED_BLOCK.search(command):
            script += _POWERSHELL_EXIT_STATUS
        argv = [pwsh, "-NoProfile", "-Command", script]
        if len(subprocess.list2cmdline(argv)) > _WINDOWS_COMMAND_LINE_MAX_CHARS:
            raise _not_run(
                f"the command is {len(command):,} characters long and Windows accepts at most "
                f"{_WINDOWS_COMMAND_LINE_MAX_CHARS:,} for a command line. Save the script to a "
                ".ps1 file and run that file."
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
    ) -> None:
        self._terminals = terminal_manager
        self._resolve_credential = credential_resolver or (lambda _name: "")
        self._update_handoffs = update_handoffs
        # Tasks that outlive their call: releasing handoff tokens, stopping commands.
        self._tasks: set[asyncio.Task[Any]] = set()

    async def __call__(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        call = _parse_call(context, arguments)
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
            report = self._terminals.hand_off_command(terminal_id, deliver=True)
            if report.exited:
                return self._exited(context, call, report)
            return await self._running(context, call, terminal_id, reason="requested")
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
        try:
            return await self._terminals.spawn_command(
                TerminalOwner(context.project_id, context.agent_id, context.session_id),
                argv,
                command=call.command,
                description=call.description,
                cwd=call.workdir,
                env=environment,
                timeout_seconds=call.timeout_seconds,
                formatter=format_command_delivery,
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
                idle_seconds=COMMAND_IDLE_SECONDS if context.can_call(_TERMINAL_TOOL) else None,
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
            return self._exited(context, call, report)
        report = self._terminals.hand_off_command(terminal_id, deliver=depth < 1)
        if report.exited:
            return self._exited(context, call, report)
        return await self._running(context, call, terminal_id, reason=outcome)

    # Results

    def _exited(self, context: ToolContext, call: _ShellCall, report: CommandReport) -> JsonObject:
        data: JsonObject = {"status": "stopped" if report.stop_reason else "exited"}
        if report.stop_reason is not None:
            data["stopped_because"] = _stop_text(report)
        if report.exit_code is not None:
            data["exit_code"] = report.exit_code
        output, truncated = command_output_text(report)
        data["output"] = output
        if truncated and report.transcript.log_path is not None:
            data["log_file"] = model_path(report.transcript.log_path)
        if report.nonzero_exits:
            data["failed_programs"] = list(report.nonzero_exits)
        if report.still_running:
            data["terminal_id"] = report.terminal_id
            data["still_running"] = [_process_text(process) for process in report.still_running]
            data["next"] = _still_running_text(context, report)
        return _with_notes(data, call.notes)

    async def _running(
        self, context: ToolContext, call: _ShellCall, terminal_id: str, *, reason: str
    ) -> JsonObject:
        report = self._terminals.command_report(terminal_id)
        screen = ""
        with contextlib.suppress(TerminalManagerError):
            screen = await self._terminals.command_screen(terminal_id, _RUNNING_SCREEN_ROWS)
        output, truncated = command_output_text(report, screen=screen)
        data: JsonObject = {"status": "running", "terminal_id": terminal_id, "output": output}
        if truncated and report.transcript.log_path is not None:
            data["log_file"] = model_path(report.transcript.log_path)
        data["next"] = _running_text(context, terminal_id, reason=reason)
        return _with_notes(data, call.notes)

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


def _with_notes(data: JsonObject, notes: Sequence[str]) -> JsonObject:
    if notes:
        data["notes"] = list(notes)
    return tool_success(data)


def _stop_text(report: CommandReport) -> str:
    reason = report.stop_reason
    if reason == "timeout":
        limit = f"{report.timeout_seconds:g}" if report.timeout_seconds else "its"
        return (
            f"it was still running at the {limit}-second timeout. To let it finish, run it "
            "again with a larger timeout, or 0 for no limit."
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


def _still_running_text(context: ToolContext, report: CommandReport) -> str:
    names = ", ".join(_process_text(process) for process in report.still_running)
    if context.can_call(_TERMINAL_TOOL):
        return (
            f"The shell exited, but processes the command started still run: {names}. Stop "
            f"them with terminal kill on {report.terminal_id} once they are no longer needed."
        )
    return (
        f"The shell exited, but processes the command started still run: {names}. They "
        "keep running until they exit or vBot stops."
    )


def _running_text(context: ToolContext, terminal_id: str, *, reason: str) -> str:
    delivers = context.nesting_depth < 1
    arrives = " Its result arrives as a new message when it exits." if delivers else ""
    if reason == "idle":
        return (
            f"The command printed nothing for {COMMAND_IDLE_SECONDS:g} seconds and uses no "
            "CPU; it is probably waiting for input. Answer with terminal input, or stop it "
            f"with terminal kill.{arrives}"
        )
    where = (
        f"in terminal {terminal_id}" if context.can_call(_TERMINAL_TOOL) else "in the background"
    )
    moved = "The user moved the command to the background. " if reason == "moved" else ""
    return (
        f"{moved}The command continues {where}.{arrives} Do not poll or start it again; "
        "continue other work or end your turn."
    )


def command_output_text(report: CommandReport, *, screen: str = "") -> tuple[str, bool]:
    """The output for a result: head and tail within the character budget.

    Returns the text and whether anything was left out of it.
    """
    transcript = report.transcript
    head = [_shortened(line) for line in transcript.head]
    tail = [_shortened(line) for line in transcript.tail]
    if screen:
        tail.extend(_shortened(line) for line in screen.splitlines())
    shortened = any(
        len(line) > SHELL_OUTPUT_LINE_CHARS for line in (*transcript.head, *transcript.tail)
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


def format_command_delivery(report: CommandReport) -> str:
    """The message that delivers a handed-off command's result when it ends."""
    subject = report.description or _first_line(report.command)
    if report.stop_reason is not None:
        status = f"was stopped: {_stop_text(report)}"
    else:
        status = f"exited with code {report.exit_code}."
    lines = [f"The command in terminal {report.terminal_id} ({subject}) {status}"]
    if report.nonzero_exits:
        lines.append(f"Failed programs: {'; '.join(report.nonzero_exits)}.")
    if report.still_running:
        names = ", ".join(_process_text(process) for process in report.still_running)
        lines.append(f"Processes it started still run: {names}.")
    output, truncated = command_output_text(report)
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
) -> ShellTool:
    """Register the shell Tool with a vBot Tool registry."""
    tool = ShellTool(
        terminal_manager,
        credential_resolver=credential_resolver,
        update_handoffs=update_handoffs,
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
    "format_command_delivery",
    "format_shell_env_usage",
    "project_shell_tool_definitions",
    "register_shell_tool",
    "shell_detail_blocks",
]
