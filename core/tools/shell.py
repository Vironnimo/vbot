"""The shell Tool: one command per call, without input or a terminal."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import sys
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from functools import partial
from typing import Any

from core.projects import ProjectStore
from core.runs import TOOL_CALL_OUTPUT_EVENT
from core.secrets.agent_secrets import AgentSecrets, SecretUse, SecretUseError, describe_uses
from core.sessions import SessionAddress
from core.tools._powershell import (
    PowerShellRequest,
    powershell_bootstrap,
    powershell_request,
)
from core.tools._shell_arguments import (
    SHELL_UNADVERTISED_PARAMETERS,
    _parse_call,
    _ShellCall,
    normalize_shell_arguments,
    shell_display_parts,
)
from core.tools._shell_results import (
    COMMAND_STATUS_NOTE_MARKER,
    COMMAND_STATUS_TOOL_NAMES,
    SHELL_TOOL_NAME,
    TERMINAL_TOOL_NAME,
    ToolNames,
    _cannot_start,
    _NotStartedError,
    _start_failure,
    background_command_statuses,
    command_output_text,
    delivery_names,
    duration,
    ended_result,
    exit_code_text,
    format_command_delivery,
    holding_stage,
    named_tools,
    not_run_problem,
    running_output_note,
    running_output_text,
    silence_note,
    stop_with,
)
from core.tools._tool_display import display_notice, display_text
from core.tools.command_manager import (
    CommandCheckpointError,
    CommandManager,
    CommandManagerError,
    CommandNotFoundError,
    CommandOwner,
    CommandReport,
    StopReason,
)
from core.tools.model_names import SHELL_MODEL_NAME
from core.tools.scratch_areas import ScratchAreas, ScratchUnavailableError
from core.tools.shell_environment import (
    CommandEnvironment,
    RunIdentity,
    command_environment,
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

from ._shell_definition import (
    SHELL_DEFAULT_WAIT_SECONDS,
    SHELL_MAX_WAIT_SECONDS,
    SHELL_TOOL_DESCRIPTION,
    SHELL_TOOL_PARAMETERS,
    _render_env_prompt_block,
)
from ._shell_definition import (
    format_shell_env_usage as format_shell_env_usage,
)

_LOGGER = get_logger("tools.shell")

CredentialResolver = Callable[[str], str]
# On Windows a command's processes have a hidden console whose input nothing
# writes: a program that reads it directly instead of stdin (Python's getpass,
# $Host.UI.RawUI.ReadKey()) waits silently until stopped instead of failing.
_CONSOLE_READS_WAIT = sys.platform == "win32"


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
        secrets: AgentSecrets | None = None,
    ) -> None:
        self._commands = command_manager
        self._resolve_credential = credential_resolver or (lambda _name: "")
        self._secrets = secrets
        self._update_handoffs = update_handoffs
        self._projects = projects
        self._scratch_areas = scratch_areas
        # Tasks that outlive their call: releasing handoff tokens, stopping commands.
        self._tasks: set[asyncio.Task[Any]] = set()

    async def __call__(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        names = named_tools(context)
        secrets = self._secrets
        call = _parse_call(
            context,
            arguments,
            self._projects,
            terminal_available=names(TERMINAL_TOOL_NAME),
            secret_names=(
                secrets.environment_names(
                    context.agent_id, context.session_id, passed_only=context.subagent_session
                )
                if secrets is not None
                else frozenset()
            ),
        )
        try:
            secret_environment = await self._secret_environment(context, call)
        except SecretUseError as error:
            return tool_failure("secret_unavailable", f"{SHELL_MODEL_NAME} was not run: {error}")
        handoff = _issue_update_handoff(self._update_handoffs, context)
        try:
            command_id = await self._start(context, call, handoff, names, secret_environment)
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
            return await self._background_result(call, command_id, names, reason="background")
        return await self._wait(context, call, command_id, names)

    async def _secret_environment(self, context: ToolContext, call: _ShellCall) -> dict[str, str]:
        """The variables of the Agent Secrets this call names; blocking sources run off-loop."""
        secrets = self._secrets
        if secrets is None or not (call.secret_keys or call.secret_variables):
            return {}

        passed_only = context.subagent_session

        def resolve() -> tuple[dict[str, str], list[SecretUse]]:
            variables: dict[str, str] = {}
            uses: list[SecretUse] = []
            for name in call.secret_keys:
                found = secrets.environment(
                    context.agent_id, context.session_id, name, passed_only=passed_only
                )
                if found is not None:
                    variables.update(found[0])
                    uses.extend(found[1])
            for name, reference in call.secret_variables:
                variables[name], found_uses = secrets.substitute(
                    context.agent_id,
                    context.session_id,
                    reference,
                    "environment",
                    passed_only=passed_only,
                )
                uses.extend(found_uses)
            return variables, uses

        variables, uses = await asyncio.to_thread(resolve)
        if uses:
            context.record_secret_uses(describe_uses(uses))
        return variables

    async def _background_result(
        self,
        call: _ShellCall,
        command_id: str,
        names: ToolNames,
        *,
        reason: str,
        idle: bool = False,
    ) -> JsonObject:
        recovery_failed = False
        try:
            report = await self._commands.to_background_durably(
                command_id,
                deliver=not self._commands.report(command_id).ended,
            )
        except CommandCheckpointError:
            _LOGGER.warning(
                "Command recovery state unavailable (command=%s)", command_id, exc_info=True
            )
            report = self._commands.report(command_id)
            recovery_failed = True
        return _result(
            call, report, names, reason=reason, idle=idle, recovery_failed=recovery_failed
        )

    async def _start(
        self,
        context: ToolContext,
        call: _ShellCall,
        handoff: UpdateHandoffGrant | None,
        names: ToolNames,
        secret_environment: dict[str, str],
    ) -> str:
        credentials = {name: self._resolve_credential(name) for name in call.credential_names}
        if self._secrets is not None:
            # A credential the process inherited is masked once a command got it.
            for name, value in credentials.items():
                self._secrets.remember_value(value, f"{{{{secret:{name}}}}}")
        credentials.update(secret_environment)
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

        waiting = asyncio.ensure_future(
            self._commands.wait(
                command_id, seconds=call.wait_seconds, check_idle=_CONSOLE_READS_WAIT
            )
        )
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
        reason = "timeout" if call.timeout_given else "default"
        if moved.is_set():
            reason = "moved"
        return await self._background_result(call, command_id, names, reason=reason, idle=idle)

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
    recovery_failed: bool = False,
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
    if recovery_failed:
        data.setdefault("notes", []).append(
            "vBot could not save recovery state for this command. The command has already "
            "started; its completion notification is not guaranteed. Check its status and "
            "effects before repeating it."
        )
        if not report.ended:
            data["next"] = (
                f"The command continues as {report.command_id}.{running_output_note(report)} "
                f"{stop_with(report, names)}"
            ).rstrip()
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
    silence = silence_note(report)
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
        silence = ""
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
            f"{duration(report.elapsed_seconds)}; it continues as {command_id}."
            f"{silence}{log} {tail}"
        )
    if reason == "timeout":
        opening = f"The command was still running at its {wait_seconds:g}-second timeout"
    else:
        opening = f"The command was still running after {SHELL_DEFAULT_WAIT_SECONDS:g} seconds"
    return f"{opening} and continues in the background as {command_id}.{silence}{log} {tail}"


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
        blocks.append(
            display_notice("warning", f"The command exited with code {exit_code_text(exit_code)}.")
        )
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
    secrets: AgentSecrets | None = None,
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
        secrets=secrets,
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
