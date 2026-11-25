"""The shell Tool: one command per call, without input or a terminal."""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
from collections.abc import Awaitable, Callable, Container, Coroutine, Iterable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from core.permissions import CommandChecker, PermissionDenied
from core.projects import ProjectStore
from core.runs import TOOL_CALL_OUTPUT_EVENT
from core.secrets.agent_secrets import (
    AgentSecrets,
    SecretEnvironmentTargets,
    SecretUse,
    SecretUseError,
    describe_uses,
)
from core.sessions import SessionAddress
from core.tools._permission_preflight import authorize_command
from core.tools._powershell import (
    PowerShellRequest,
    powershell_bootstrap,
    powershell_request,
)
from core.tools._shell_arguments import (
    SHELL_UNADVERTISED_PARAMETERS,
    _parse_call,
    _ShellCall,
    environment_name,
    normalize_shell_arguments,
    shell_display_parts,
    validate_workdir,
)
from core.tools._shell_arguments import (
    shell_display_values as shell_display_values,
)
from core.tools._shell_display import record_command_stop, shell_detail_blocks
from core.tools._shell_results import (
    COMMAND_STATUS_NOTE_MARKER,
    COMMAND_STATUS_TOOL_NAMES,
    CONSUMED_SECRETS_NOTE,
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
    format_command_delivery,
    holding_stage,
    named_tools,
    not_run_problem,
    output_encoding_note,
    running_output_note,
    running_output_text,
    silence_note,
    stop_action,
)
from core.tools._tool_context import ToolApprovalCancelledError
from core.tools._tool_workers import run_tool_worker, run_tool_worker_guarded
from core.tools.command_manager import (
    CommandCheckpointError,
    CommandManager,
    CommandManagerError,
    CommandNotFoundError,
    CommandOwner,
    CommandReport,
    CommandWaitOutcome,
    StopReason,
)
from core.tools.contracts import ToolContractError
from core.tools.model_names import SHELL_MODEL_NAME
from core.tools.scratch_areas import ScratchAreas, ScratchFolder, ScratchUnavailableError
from core.tools.shell_environment import (
    CommandEnvironment,
    RunIdentity,
    command_environment,
)
from core.tools.shell_hints import sleeps
from core.tools.tools import (
    JsonObject,
    ToolContext,
    ToolDisplay,
    ToolPromptBlockRegistry,
    ToolRegistry,
    tool_failure,
    tool_success,
)
from core.tools.update_handoff import UpdateHandoffGrant, UpdateHandoffs, issue_for_tool_context
from core.utils.logging import get_logger
from core.utils.surrogates import replace_lone_surrogates

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

if TYPE_CHECKING:
    from core.subagents import SubAgentTree
    from core.tools.shell_cli import ShellCliAccess, ShellCliGrant

_LOGGER = get_logger("tools.shell")

CredentialResolver = Callable[[str], str]
# The environment names vBot knows as credentials (``Runtime.environment_credential_names``).
CredentialNames = Callable[[], Container[str]]
# The folders a command's Remove-Item and Move-Item leave in place beyond those
# PowerShell's bootstrap knows itself (``_powershell.py``, Guards). Blocking.
ProtectedFolders = Callable[[], Iterable[str]]


class ShellReviewer(Protocol):
    def __call__(
        self,
        context: ToolContext,
        arguments: JsonObject,
        *,
        command: str,
        shell: str,
        cwd: Path,
        environment: Mapping[str, str],
    ) -> Awaitable[bool]: ...


# On Windows a command's processes have a hidden console whose input nothing
# writes: a program that reads it directly instead of stdin (Python's getpass,
# $Host.UI.RawUI.ReadKey()) waits silently until stopped instead of failing.
_CONSOLE_READS_WAIT = sys.platform == "win32"
# The language of the host shell, for command analysis under Permission Rules.
HOST_SHELL_KIND: Literal["powershell", "bash"] = "powershell" if sys.platform == "win32" else "bash"
# A command that sleeps and runs this long while work that wakes the caller
# still runs is waiting for that work (Session, 2026-10-23: an Agent slept
# 45-270 seconds per call for 17 minutes while its Sub-Agents worked).
_WAITED_SECONDS = 20.0
# The waiting hint names at most this many of the caller's running tasks.
_WAITED_SHOWN = 5


@dataclass(frozen=True)
class HostShellStart:
    """How a command's host shell starts, without input or terminal."""

    argv: list[str]
    env: dict[str, str]
    # What PowerShell reads from stdin before the command runs (Windows only).
    request: PowerShellRequest | None = None
    # Whether a shell started before the call can serve it (``CommandManager.launch``).
    warm: bool = True


class HostShellMissingError(Exception):
    """This host has no shell to run commands in; the message names it, without a final period."""


def powershell_executable(search_path: str | None) -> Path | None:
    """The native PowerShell 7 executable in an absolute directory on *search_path*.

    The Shell Tool and Permission Rules use the same lookup. A bare-name
    lookup on Windows can select a file in the server's working directory.
    """
    name = "pwsh.exe" if sys.platform == "win32" else "pwsh"
    for entry in os.get_exec_path(None if search_path is None else {"PATH": search_path}):
        directory = Path(entry)
        if directory.is_absolute():
            candidate = directory / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    return None


def host_shell_start(
    command: str,
    workdir: Path,
    environment: CommandEnvironment,
    protected: Iterable[str] = (),
) -> HostShellStart:
    """The host shell for *command* in *workdir*, without input or terminal; blocking.

    PowerShell 7 on Windows, bash elsewhere. PowerShell's argv and start
    environment are the same for every call, so a shell started before the call
    can serve it: the command, its workdir and the call's layer of the
    environment travel as the request on stdin. The request cannot carry an
    empty value, which ``SetEnvironmentVariable`` removes on .NET 8 (pwsh 7.4):
    a call that sets one starts a shell of its own with its whole environment.
    *protected* are the folders PowerShell's guards keep besides their own
    (``_powershell.py``, Guards); bash has no such guards.
    Raises ``HostShellMissingError`` when this host has no such shell.
    """
    complete = environment.complete()
    if sys.platform == "win32":
        search_path = next(
            (value for name, value in complete.items() if name.upper() == "PATH"), None
        )
        pwsh = powershell_executable(search_path)
        if pwsh is None:
            raise HostShellMissingError("PowerShell 7 (pwsh) is not installed on this host")
        argv = [str(pwsh), "-NoProfile", "-NonInteractive", "-Command", powershell_bootstrap()]
        if "" in environment.changes.values():
            request = powershell_request(command, str(workdir), {}, protected)
            return HostShellStart(argv, complete, request, warm=False)
        request = powershell_request(command, str(workdir), environment.changes, protected)
        return HostShellStart(argv, environment.base, request)
    # exec searches relative and empty PATH entries from the child's cwd.
    directory = workdir.absolute()
    for entry in os.get_exec_path(complete):
        bash = shutil.which(str(directory / entry / "bash"))
        if bash is not None:
            return HostShellStart([bash, "-c", command], complete)
    raise HostShellMissingError("bash is not installed on this host")


def _check_environment_names(variables: Mapping[str, str], opaque_names: set[str]) -> None:
    from core.permissions.commands.commands import (
        controls_command_analysis,
        controls_command_execution,
    )

    blocked = sorted(
        {name for name in variables if controls_command_execution(name)}
        | {name for name in opaque_names if controls_command_analysis(name)}
    )
    if blocked:
        names = ", ".join(blocked)
        raise ToolContractError(
            f"{SHELL_MODEL_NAME} was not run: Permission Rules block these environment "
            f"overrides: {names}. Omit them from env and env_keys."
        )


def _shell_start(
    call: _ShellCall, environment: CommandEnvironment, protected: Iterable[str]
) -> HostShellStart:
    """The host shell for *call*; raises ``_NotStartedError`` when this host cannot run it."""
    try:
        return host_shell_start(call.command, call.workdir, environment, protected)
    except HostShellMissingError as error:
        raise _cannot_start(f"{error}. Ask the user to install it.") from error
    except UnicodeEncodeError:
        raise _cannot_start(
            "The shell request contains an unpaired Unicode surrogate. "
            "Check the working directory and shell environment for invalid Unicode."
        ) from None


class ShellTool:
    """Runs shell commands for Agents through the ``CommandManager``."""

    def __init__(
        self,
        command_manager: CommandManager,
        *,
        credential_resolver: CredentialResolver | None = None,
        credential_names: CredentialNames | None = None,
        update_handoffs: UpdateHandoffs | None = None,
        projects: ProjectStore | None = None,
        scratch_areas: ScratchAreas | None = None,
        secrets: AgentSecrets | None = None,
        command_checker: CommandChecker | None = None,
        shell_cli: ShellCliAccess | None = None,
        subagents: SubAgentTree | None = None,
        review_shell: ShellReviewer | None = None,
        protected_folders: ProtectedFolders | None = None,
    ) -> None:
        self._commands = command_manager
        self._protected_folders: ProtectedFolders = protected_folders or tuple
        self._subagents = subagents
        self._checker = command_checker
        self._shell_cli = shell_cli
        self._review_shell = review_shell
        self._resolve_credential = credential_resolver or (lambda _name: "")
        self._credential_names: CredentialNames = credential_names or frozenset
        self._secrets = secrets
        self._update_handoffs = update_handoffs
        self._projects = projects
        self._scratch_areas = scratch_areas
        # Tasks that outlive their call: releasing handoff tokens, stopping commands.
        self._tasks: set[asyncio.Task[Any]] = set()

    async def __call__(self, context: ToolContext, arguments: JsonObject) -> JsonObject:
        names = named_tools(context)
        secrets = self._secrets
        confined = context.permissions.restricted
        # A confined command gets a credential vBot knows only as a granted one.
        withheld = self._credential_names() if confined else frozenset[str]()
        secret_targets = (
            secrets.environment_targets(
                SessionAddress(context.project_id, context.agent_id, context.session_id),
                passed_only=context.subagent_session,
            )
            if secrets is not None
            else {}
        )
        call = await run_tool_worker(
            _parse_call,
            context,
            arguments,
            self._projects,
            terminal_available=names(TERMINAL_TOOL_NAME),
            withheld=withheld,
            secret_targets=secret_targets,
        )
        secret_names = {
            *(name for key in call.secret_keys for name in secret_targets[key.upper()]),
            *(name for name, _ in call.secret_variables),
        }
        opaque_names = secret_names.union(call.credential_names)
        if confined:
            _check_environment_names(call.variables, opaque_names)
        scratch = await self._scratch(context)
        uses_cli = False
        if confined:
            # Before any Agent Secret is used and before a command grant exists.
            uses_cli = await self._check_permissions(
                context, call, scratch, withheld, arguments, opaque_names
            )
        await run_tool_worker(validate_workdir, context, arguments, call.workdir)
        if self._review_shell is not None:
            reviewed_cwd = await run_tool_worker(call.workdir.resolve)
            public_environment = {**call.variables, **dict(call.secret_variables)}
            approved = await self._review_shell(
                context,
                arguments,
                command=call.command,
                shell=HOST_SHELL_KIND,
                cwd=reviewed_cwd,
                environment=public_environment,
            )
            if not approved:
                return tool_failure(
                    "command_not_approved",
                    f"{SHELL_MODEL_NAME} was not run: the command was not approved.",
                )
            await context.revalidate_permissions()
            if await run_tool_worker(call.workdir.resolve) != reviewed_cwd:
                raise ToolContractError(
                    "The command's paths changed while the Tool was waiting. Call it again."
                )
            if context.permissions.restricted != confined:
                raise ToolContractError(
                    "Permission Rules changed while the command was prepared. Call it again."
                )
            await run_tool_worker(validate_workdir, context, arguments, call.workdir)
        try:
            secret_environment, secret_uses = await self._secret_environment(
                context, call, secret_targets
            )
        except SecretUseError as error:
            return tool_failure("secret_unavailable", f"{SHELL_MODEL_NAME} was not run: {error}")
        await context.revalidate_permissions()
        if context.permissions.restricted != confined:
            raise ToolContractError(
                "Permission Rules changed while the command was prepared. Call it again."
            )
        cli_grant = None
        with contextlib.ExitStack() as grants:
            handoff = None
            if not confined or uses_cli:
                handoff = issue_for_tool_context(self._update_handoffs, context)
                if handoff is not None:
                    grants.callback(handoff.release)
            if uses_cli and self._shell_cli is not None:
                cli_grant = self._shell_cli.issue(
                    context, arguments, handoff_token=handoff.token if handoff else None
                )
                grants.callback(cli_grant.release)
            try:
                command_id = await self._start(
                    context,
                    arguments,
                    call,
                    handoff,
                    names,
                    secret_environment,
                    secret_uses,
                    scratch,
                    withheld,
                    cli_grant,
                )
            except _NotStartedError as failure:
                return failure.result
            if handoff is not None or cli_grant is not None:
                self._keep(self._release_when_finished(command_id, grants.pop_all().close))
        if call.background:
            return await self._background_result(
                call, command_id, names, context, reason="background"
            )
        return await self._wait(context, call, command_id, names, cli_grant)

    async def _scratch(self, context: ToolContext) -> ScratchFolder | None:
        if self._scratch_areas is None:
            return None
        # Scratch is optional; its owner logged an unavailable folder. The
        # command still runs with the host's normal temporary directories.
        with contextlib.suppress(ScratchUnavailableError):
            return await self._scratch_areas.folder_for(
                SessionAddress(context.project_id, context.agent_id, context.session_id)
            )
        return None

    async def _check_permissions(
        self,
        context: ToolContext,
        call: _ShellCall,
        scratch: ScratchFolder | None,
        withheld: Container[str],
        arguments: JsonObject,
        opaque_names: set[str],
    ) -> bool:
        """Authorize the whole command and identify an invocation of the reviewed CLI."""
        boundary = context.permissions
        if self._checker is None:
            raise PermissionDenied(
                boundary.refusal(
                    "command",
                    call.command,
                    "unanalyzable",
                    detail="this vBot cannot analyze commands to check them",
                )
            )
        environment = await run_tool_worker(
            partial(
                command_environment,
                None,
                variables=call.variables,
                scratch=scratch,
                confined=True,
                credential_names=withheld,
            )
        )
        # Unknown in the analysis, even when a call replaces an inherited value.
        opaque_keys = (
            {name.upper() for name in opaque_names} if sys.platform == "win32" else opaque_names
        )
        visible_env = {
            name: value
            for name, value in environment.complete().items()
            if (name.upper() if sys.platform == "win32" else name) not in opaque_keys
        }
        plan = await authorize_command(
            context,
            self._checker,
            arguments=arguments,
            command=call.command,
            shell=HOST_SHELL_KIND,
            cwd=call.workdir,
            env=visible_env,
            workdir_parameter=True,
            allows_instance_cli=partial(self._shell_cli.allows, context)
            if self._shell_cli
            else None,
        )
        return plan is not None and plan.instance_cli_only

    async def _secret_environment(
        self, context: ToolContext, call: _ShellCall, targets: SecretEnvironmentTargets
    ) -> tuple[dict[str, str], list[SecretUse]]:
        """Resolve Secret variables without spending them; blocking sources run off-loop."""
        secrets = self._secrets
        if secrets is None or not (call.secret_keys or call.secret_variables):
            return {}, []

        passed_only = context.subagent_session

        def resolve() -> tuple[dict[str, str], list[SecretUse]]:
            variables: dict[str, str] = {}
            uses: list[SecretUse] = []
            for name in call.secret_keys:
                found = secrets.environment(
                    SessionAddress(context.project_id, context.agent_id, context.session_id),
                    name,
                    passed_only=passed_only,
                    consume=False,
                )
                expected = targets[name.upper()]
                if (
                    found is None
                    or found[0].keys() - expected.keys()
                    or {use.label for use in found[1]} - {labels[0] for labels in expected.values()}
                ):
                    raise ToolContractError(
                        f"{SHELL_MODEL_NAME} was not run: the Secret's environment variables "
                        "changed while the call was prepared. Call it again."
                    )
                variables.update(found[0])
                uses.extend(found[1])
            for name, reference in call.secret_variables:
                variables[name], found_uses = secrets.substitute(
                    SessionAddress(context.project_id, context.agent_id, context.session_id),
                    reference,
                    "environment",
                    passed_only=passed_only,
                    consume=False,
                )
                uses.extend(found_uses)
            return variables, uses

        return await run_tool_worker(resolve)

    async def _background_result(
        self,
        call: _ShellCall,
        command_id: str,
        names: ToolNames,
        context: ToolContext,
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
            call,
            report,
            names,
            context,
            reason=reason,
            idle=idle,
            recovery_failed=recovery_failed,
        )

    async def _start(
        self,
        context: ToolContext,
        arguments: JsonObject,
        call: _ShellCall,
        handoff: UpdateHandoffGrant | None,
        names: ToolNames,
        secret_environment: dict[str, str],
        secret_uses: list[SecretUse],
        scratch: ScratchFolder | None,
        withheld: Container[str],
        cli_grant: ShellCliGrant | None,
    ) -> str:
        credentials = {name: self._resolve_credential(name) for name in call.credential_names}
        for name, value in (credentials | secret_environment).items():
            if replace_lone_surrogates(value) is not value:
                raise ToolContractError(
                    f"{SHELL_MODEL_NAME} was not run: the value supplied for {name} contains "
                    "an unpaired Unicode surrogate. Ask the user to correct its source."
                )
        if self._secrets is not None:
            # A credential the process inherited is masked once a command got it.
            for value in credentials.values():
                self._secrets.remember_value(value)
        credentials.update(secret_environment)

        def prepare() -> HostShellStart:
            environment = command_environment(
                RunIdentity(
                    context.agent_id,
                    context.session_id,
                    context.project_id,
                    handoff.token if handoff is not None else None,
                ),
                shell_cli=cli_grant.environment_value if cli_grant else None,
                variables=call.variables,
                credentials=credentials,
                scratch=scratch,
                confined=context.permissions.restricted,
                credential_names=withheld,
            )
            # PATH lookup probes the filesystem and uses this call's fresh environment.
            if call.inherited_variables:
                available = {environment_name(key) for key in environment.complete()}
                for key in call.inherited_variables:
                    if environment_name(key) not in available:
                        raise ToolContractError(
                            f"{SHELL_MODEL_NAME} was not run: env requests the existing value of "
                            f"{key}, but this command has no such variable. Omit it from env "
                            "or ask the user to make it available."
                        )
            return _shell_start(call, environment, self._protected_folders())

        confined = context.permissions.restricted
        start = await run_tool_worker(prepare)

        async def authorize_launch() -> None:
            await context.revalidate_permissions()
            if context.permissions.restricted != confined:
                raise ToolContractError(
                    "Permission Rules changed while the command was prepared. Call it again."
                )

        await authorize_launch()
        await run_tool_worker(validate_workdir, context, arguments, call.workdir)
        # Output can arrive before the launch returns the command's id.
        started: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        async def progress(output: str) -> None:
            command_id = await started
            if self._secrets is not None:
                output = await run_tool_worker(
                    self._secrets.protect_output,
                    SessionAddress(context.project_id, context.agent_id, context.session_id),
                    output,
                    passed_only=context.subagent_session,
                    growing=True,
                )
            await context.emit(
                TOOL_CALL_OUTPUT_EVENT,
                {"tool_call_id": context.tool_call_id, "command_id": command_id, "output": output},
            )

        if self._secrets is not None and secret_uses:
            secrets = self._secrets

            def consume() -> None:
                if context.is_cancelled():
                    raise asyncio.CancelledError
                if context.was_cancelled_by_user():
                    raise ToolApprovalCancelledError
                secrets.consume(
                    SessionAddress(context.project_id, context.agent_id, context.session_id),
                    secret_uses,
                    passed_only=context.subagent_session,
                )

            try:
                await run_tool_worker(secrets.validate_prepared_values, secret_uses)
                await run_tool_worker_guarded(authorize_launch, consume)
            except SecretUseError as error:
                started.cancel()
                raise _NotStartedError(
                    tool_failure("secret_unavailable", f"{SHELL_MODEL_NAME} was not run: {error}")
                ) from error
            except BaseException:
                started.cancel()
                raise
            context.record_secret_uses(describe_uses(secret_uses))
        else:
            await authorize_launch()
        spent = any(use.secret is not None and use.secret.lifetime == "once" for use in secret_uses)
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
                formatter=partial(
                    format_command_delivery,
                    names=delivery_names(names),
                    permissions=context.permissions,
                ),
                permissions=context.permissions,
                execution_owner=context.execution_owner,
                on_output=progress,
                request=start.request,
                warm=start.warm,
                consumed_once_secrets=spent,
            )
        except (OSError, CommandManagerError) as error:
            started.cancel()
            result = await run_tool_worker(_start_failure, error, call, context, names)
            if spent:
                result["error"]["message"] += f" {CONSUMED_SECRETS_NOTE}"
                result["error"]["retryable"] = False
            raise _NotStartedError(result) from error
        except BaseException:
            started.cancel()
            raise
        started.set_result(handle.command_id)
        return handle.command_id

    async def _wait(
        self,
        context: ToolContext,
        call: _ShellCall,
        command_id: str,
        names: ToolNames,
        cli_grant: ShellCliGrant | None,
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

        async def wait_for_command() -> CommandWaitOutcome:
            while True:
                outcome = await self._commands.wait(
                    command_id, seconds=call.wait_seconds, check_idle=_CONSOLE_READS_WAIT
                )
                if outcome == "ended" or cli_grant is None or not cli_grant.reviewing:
                    return outcome
                # Human review time is separate from the shell command's execution budget.
                await cli_grant.wait_for_reviews()

        waiting = asyncio.ensure_future(wait_for_command())
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
            waited = await self._waited_hint(context, report)
            return _result(call, report, names, context, waited=waited)
        # Listed from now on, so the user and the Agent can see and stop it; a
        # command that ended is delivered by this result, not later.
        handed_off = True
        reason = "timeout" if call.timeout_given else "default"
        if moved.is_set():
            reason = "moved"
        return await self._background_result(
            call, command_id, names, context, reason=reason, idle=idle
        )

    async def _waited_hint(self, context: ToolContext, report: CommandReport) -> str | None:
        """Tell a command that slept while the caller's own work runs how to wait instead.

        That work is its direct Sub-Agents whose answers reach it and its
        background commands that will deliver a result.
        """
        if report.elapsed_seconds < _WAITED_SECONDS or not sleeps(report.command):
            return None
        caller = SessionAddress(context.project_id, context.agent_id, context.session_id)
        running = [
            f"command {command_id}" for command_id in self._commands.pending_wake_ids(caller)
        ]
        if self._subagents is not None:
            try:
                tree = await self._subagents.subagents(caller)
            except Exception:
                _LOGGER.warning("Reading the Sub-Agents of %s failed", caller, exc_info=True)
                tree = []
            running[:0] = [
                f"Sub-Agent {item.id} ({item.title})" for item in tree if item.answer_pending
            ]
        if not running:
            return None
        named = ", ".join(running[:_WAITED_SHOWN])
        if len(running) > _WAITED_SHOWN:
            named += f" and {len(running) - _WAITED_SHOWN} more"
        return (
            f"This command took {duration(report.elapsed_seconds)} and slept while work that "
            f"reports to you still runs: {named}. Each result arrives as a new message by "
            "itself; sleeping or checking files and logs does not bring it sooner. Continue with "
            "other work your task still needs. When no such work is left, end your turn: reply "
            "with a short status and no tool call. Your next turn starts when a result arrives."
        )

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

    async def _release_when_finished(self, command_id: str, release: Callable[[], None]) -> None:
        """Keep the grants live exactly while the command's processes run."""
        try:
            await self._commands.wait_finished(command_id)
        except CommandNotFoundError:
            pass
        finally:
            release()


# Results


def _result(
    call: _ShellCall,
    report: CommandReport,
    names: ToolNames,
    context: ToolContext,
    *,
    reason: str = "",
    idle: bool = False,
    recovery_failed: bool = False,
    waited: str | None = None,
) -> JsonObject:
    """The Tool result of one call; *reason* says why a running command was
    returned, *idle* that its processes were idle when the wait passed, and
    *waited* is the hint for an ended command that slept while the caller's
    own work runs, shown when no other hint is."""
    if report.ended:
        not_run = not_run_problem(report)
        if not_run is not None:
            problem, retryable = not_run
            return tool_failure(
                "command_not_started",
                f"{SHELL_MODEL_NAME} was not run: {problem}",
                retryable=retryable,
            )
        data = ended_result(report, names, permissions=context.permissions)
        record_command_stop(context, report)
        if waited is not None and "hint" not in data:
            data["hint"] = waited
    else:
        data = {"status": "running", "command_id": report.command_id}
        output = running_output_text(report)
        if output:
            data["output"] = output
        data["next"] = _running_next(
            report, names, reason=reason, wait_seconds=call.wait_seconds, idle=idle
        )
    if call.notes:
        data.setdefault("notes", []).extend(call.notes)
    if not report.ended and (encoding_note := output_encoding_note(report)) is not None:
        data.setdefault("notes", []).append(encoding_note)
    if recovery_failed:
        data.setdefault("notes", []).append(
            "vBot could not save recovery state for this command. The command has already "
            "started; its completion notification is not guaranteed. Check its status and "
            "effects before repeating it."
        )
        if not report.ended:
            data["next"] = (
                f"The command continues as {report.command_id}.{running_output_note(report)} "
                f"If you no longer need it, {stop_action(report, names)}."
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
    stop = stop_action(report, names)
    if reason == "background":
        # Often a server: its result arrives only once it is stopped, and the
        # Agent checks that it is ready itself.
        return (
            f"The command runs in the background as {command_id}.{log} Its result arrives as a "
            "new message when it exits; do not start it again, and do not sleep or poll for "
            "that result. If it runs until stopped, such as a server, check that it is ready "
            "before you use it, for example with a request to its port, and "
            f"{stop} once it is no longer needed. Otherwise continue with other work your task "
            "still needs. When no such work is left, end your turn: reply with a short status "
            "and no tool call. Your next turn starts when the result arrives."
        )
    # "or it runs until stopped" keeps an Agent from waiting for a server's exit.
    stopping = f"If you no longer need it, or it runs until stopped, such as a server, {stop}."
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
            f"which commands never get, {stop} and pass the answer as an option or "
            "argument. If you no longer need it, or it runs until stopped, such as a server, "
            "stop it the same way."
        )
    tail = (
        "Its result arrives as a new message when it exits; do not start it again, and do not "
        f"sleep or poll for that result. {stopping} Otherwise continue with other work your "
        "task still needs. When no such work is left, end your turn: reply with a short status "
        "and no tool call. Your next turn starts when the result arrives."
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


def register_shell_tool(
    registry: ToolRegistry,
    command_manager: CommandManager,
    *,
    credential_resolver: CredentialResolver | None = None,
    credential_names: CredentialNames | None = None,
    prompt_blocks: ToolPromptBlockRegistry | None = None,
    update_handoffs: UpdateHandoffs | None = None,
    projects: ProjectStore | None = None,
    scratch_areas: ScratchAreas | None = None,
    secrets: AgentSecrets | None = None,
    command_checker: CommandChecker | None = None,
    shell_cli: ShellCliAccess | None = None,
    subagents: SubAgentTree | None = None,
    review_shell: ShellReviewer | None = None,
    protected_folders: ProtectedFolders | None = None,
) -> ShellTool:
    """Register the shell Tool with a vBot Tool registry.

    *projects* resolves ``workdir: "project:<project-id>"``; without it that form fails.
    *command_checker* checks a restricted Session's commands before launch;
    without it such a Session cannot run commands. *credential_names* are the
    names a restricted Session's commands do not inherit (``shell_environment``).
    *subagents* lets a command that slept name the caller's working Sub-Agents.
    *protected_folders* names, for each command, the folders its ``Remove-Item``
    and ``Move-Item`` leave in place (``_powershell.py``, Guards).
    """
    tool = ShellTool(
        command_manager,
        credential_resolver=credential_resolver,
        credential_names=credential_names,
        update_handoffs=update_handoffs,
        projects=projects,
        scratch_areas=scratch_areas,
        secrets=secrets,
        command_checker=command_checker,
        shell_cli=shell_cli,
        subagents=subagents,
        review_shell=review_shell,
        protected_folders=protected_folders,
    )
    registry.register(
        SHELL_TOOL_NAME,
        SHELL_TOOL_DESCRIPTION,
        SHELL_TOOL_PARAMETERS,
        tool,
        summary="Run shell commands.",
        family="execution",
        announce_changes=("description",),
        open_input_schema=True,
        unadvertised_parameters=SHELL_UNADVERTISED_PARAMETERS,
        argument_normalizer=normalize_shell_arguments,
        result_schema={"type": "object", "required": ["status"]},
        display=ToolDisplay(
            parts_builder=shell_display_parts,
            detail_builder=shell_detail_blocks,
            hidden_argument_keys=("description",),
        ),
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
