"""End-to-end slash Command preparation and execution for Chat entry points."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from core.chat._command_types import CommandArgumentMode as CommandArgumentMode
from core.chat._command_types import CommandCatalogResult as CommandCatalogResult
from core.chat._command_types import CommandExecutionMode as CommandExecutionMode
from core.chat._command_types import CommandFeedbackKind as CommandFeedbackKind
from core.chat._command_types import CommandNavigationKind as CommandNavigationKind
from core.chat._command_types import CommandNewSessionMode as CommandNewSessionMode
from core.chat._command_types import CommandRunRole as CommandRunRole
from core.chat._command_types import CommandSurfaceKind as CommandSurfaceKind
from core.chat._extension_commands import ExtensionCommandRegistry, validate_extension_outcome
from core.chat.content_blocks import ContentBlock, TextBlock
from core.chat.messages import ReplySurface
from core.chat.status_report import (
    ContextWindowResolver,
    ReasoningRenderDescriber,
    WireProfileDescriber,
)
from core.database import SnapshotBarrier
from core.extensions.extensions import invoke_extension_handler
from core.runs import (
    ChatRunManager,
    Run,
    RunAdmissionBlockedError,
    RunNotFoundError,
)
from core.sessions import SessionAddress
from core.tools.terminal_manager import TerminalManager
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.agents import AgentStore
    from core.automation import AutomationReferences
    from core.automation.goals import GoalService
    from core.models.models import ModelRegistry
    from core.projects import AgentResolver, ProjectStore, RuntimeAgent
    from core.providers.providers import ProviderRegistry
    from core.secrets.agent_secrets import AgentSecrets
    from core.secrets.requests import SecretRequests
    from core.sessions import ChatSessionManager
    from core.tools.command_manager import CommandManager
else:
    AgentSecrets = Any
    SecretRequests = Any
    AgentResolver = Any
    AgentStore = Any
    AutomationReferences = Any
    GoalService = Any
    ChatSessionManager = Any
    CommandManager = Any
    ModelRegistry = Any
    ProjectStore = Any
    ProviderRegistry = Any
    RuntimeAgent = Any

from core.chat._command_types import (
    AgentArgument as AgentArgument,
)
from core.chat._command_types import (
    CommandChangeObserver as CommandChangeObserver,
)
from core.chat._command_types import (
    CommandExecutionContext as CommandExecutionContext,
)
from core.chat._command_types import (
    CommandExecutionHandler as CommandExecutionHandler,
)
from core.chat._command_types import (
    CommandFeedback as CommandFeedback,
)
from core.chat._command_types import (
    CommandNavigation as CommandNavigation,
)
from core.chat._command_types import (
    CommandOutcome as CommandOutcome,
)
from core.chat._command_types import (
    CommandResourceChange as CommandResourceChange,
)
from core.chat._command_types import (
    CommandRun as CommandRun,
)
from core.chat._command_types import (
    CommandSpec as CommandSpec,
)
from core.chat._command_types import (
    CommandUnavailability as CommandUnavailability,
)
from core.chat._command_types import (
    ExtensionCommandContext as ExtensionCommandContext,
)
from core.chat._command_types import (
    ExtensionCommandHandler as ExtensionCommandHandler,
)
from core.chat._command_types import (
    ExtensionRunStarter as ExtensionRunStarter,
)
from core.chat._command_types import (
    HandoffArgument as HandoffArgument,
)
from core.chat._command_types import (
    NewSessionCommandContext as NewSessionCommandContext,
)
from core.chat._command_types import (
    NewSessionCommandResult as NewSessionCommandResult,
)
from core.chat._command_types import (
    PreparedCommand as PreparedCommand,
)
from core.chat._command_types import (
    SessionLessCommandHandler as SessionLessCommandHandler,
)
from core.chat._command_types import (
    _RegisteredExtensionCommand as _RegisteredExtensionCommand,
)
from core.chat._command_types import (
    parse_agent_argument as parse_agent_argument,
)
from core.chat._command_types import (
    parse_handoff_argument as parse_handoff_argument,
)

# Argument mode drives autocomplete: ``none`` commands run immediately on
# selection; ``optional``/``required`` insert the token and wait for text.
# How a command sent for a new Session (one the conversation has not created yet)
# runs: ``session_less`` runs without a Session; ``create_session`` creates the
# Session first and then runs as usual; ``needs_session`` is refused until a
# message has created the Session.

_LOGGER = get_logger("chat.commands")
_COMMAND_WORKERS = BoundedWorkerPool(name="command", max_workers=4)


async def _command_session_io(
    manager: Any,
    async_name: str,
    sync_name: str,
    *arguments: Any,
    **keyword_arguments: Any,
) -> Any:
    async_method = getattr(manager, async_name, None)
    if inspect.iscoroutinefunction(async_method):
        return await async_method(*arguments, **keyword_arguments)
    return await _COMMAND_WORKERS.run(
        getattr(manager, sync_name),
        *arguments,
        **keyword_arguments,
    )


def _extract_text(content: str | list[ContentBlock] | None) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(block.text for block in content if isinstance(block, TextBlock)).strip()
    return ""


def _require_dependency(value: Any, name: str) -> Any:
    if value is None:
        raise RuntimeError(f"CommandDispatcher requires {name} for this command")
    return value


def _notice(command: str, text: str) -> CommandOutcome:
    return CommandOutcome(command=command, feedback=CommandFeedback(kind="notice", text=text))


class CommandDispatcher:
    """Prepares and executes Built-in and Extension Commands before Chat Runs."""

    BUILT_IN_COMMANDS: dict[str, CommandSpec] = {
        "agent": CommandSpec(
            "agent",
            "Move this session to another agent; no argument lists the directory.",
            argument="optional",
            argument_hint="agent name, optionally followed by a task",
            catalog_result="state_change",
            execution_mode="immediate",
            argument_execution_mode="serialized",
            unavailable_surfaces=frozenset({"channel"}),
        ),
        "compact": CommandSpec(
            "compact",
            "Compact the current session's context immediately.",
            argument="optional",
            argument_hint="optional: what the summary should keep",
            catalog_result="notice",
            execution_mode="serialized",
        ),
        "goal": CommandSpec(
            "goal",
            "Set a goal the agent works toward across turns; /goal pause, resume or clear.",
            argument="optional",
            argument_hint="your goal, or pause, resume, clear",
            catalog_result="state_change",
            execution_mode="immediate",
            new_session_mode="create_session",
            group_conversations=False,
        ),
        "handoff": CommandSpec(
            "handoff",
            "Write a handoff and start a new session (optionally for another agent).",
            argument="optional",
            argument_hint="optional: agent:name for another agent, then what to focus on",
            catalog_result="state_change",
            execution_mode="serialized",
        ),
        "help": CommandSpec(
            "help",
            "Show available slash commands.",
            argument="none",
            catalog_result="detail",
            execution_mode="immediate",
            new_session_mode="session_less",
        ),
        "learn": CommandSpec(
            "learn",
            "Author a reusable skill into your own home from a source (folder, URL, or text).",
            argument="optional",
            argument_hint="a folder, link or text to learn from",
            catalog_result="state_change",
            execution_mode="serialized",
            new_session_mode="create_session",
        ),
        "model": CommandSpec(
            "model",
            "Show or permanently set this agent's model (/model reset to clear).",
            argument="optional",
            argument_hint="model name, or reset",
            catalog_result="state_change",
            execution_mode="immediate",
            argument_execution_mode="serialized",
            new_session_mode="session_less",
        ),
        "new": CommandSpec(
            "new",
            "Start a new session for the current agent.",
            argument="none",
            catalog_result="state_change",
            execution_mode="serialized",
            new_session_mode="session_less",
        ),
        "reflect": CommandSpec(
            "reflect",
            "Review this session in a fork and save durable memory and skill updates.",
            argument="optional",
            argument_hint="optional: what to focus on",
            catalog_result="state_change",
            execution_mode="serialized",
        ),
        "rename": CommandSpec(
            "rename",
            "Rename this session; no argument clears the name.",
            argument="optional",
            argument_hint="new name, or nothing to clear it",
            catalog_result="notice",
            execution_mode="serialized",
        ),
        "secret": CommandSpec(
            "secret",
            "Give the agent a password or key it uses without seeing it.",
            argument="optional",
            argument_hint="name and value, e.g. API_KEY abc123",
            catalog_result="notice",
            # Immediate: the Run may be waiting in a secrets request this answers.
            execution_mode="immediate",
            new_session_mode="create_session",
            group_conversations=False,
            secret_argument=True,
        ),
        "status": CommandSpec(
            "status",
            "Show current session and runtime status.",
            argument="none",
            catalog_result="detail",
            execution_mode="immediate",
            new_session_mode="session_less",
        ),
        "stop": CommandSpec(
            "stop",
            "Stop the active run; /stop all also stops background commands, terminals and "
            "sub-agents.",
            argument="optional",
            argument_hint='"all" also stops background work',
            catalog_result="notice",
            execution_mode="immediate",
        ),
    }

    def __init__(
        self,
        chat_runs: ChatRunManager,
        agent_resolver: AgentResolver | None = None,
        sessions: ChatSessionManager | None = None,
        models: ModelRegistry | None = None,
        started_at: datetime | None = None,
        providers: ProviderRegistry | None = None,
        projects: ProjectStore | None = None,
        agents: AgentStore | None = None,
        context_window_resolver: ContextWindowResolver | None = None,
        trigger_service: Any | None = None,
        reflection_service: Any | None = None,
        storage: Any | None = None,
        terminal_manager: TerminalManager | None = None,
        command_manager: CommandManager | None = None,
        reasoning_render_describer: ReasoningRenderDescriber | None = None,
        wire_profile_describer: WireProfileDescriber | None = None,
        automation_references: AutomationReferences | None = None,
        snapshot_barrier: SnapshotBarrier | None = None,
        stop_all: Callable[[SessionAddress], Awaitable[int]] | None = None,
        goals: GoalService | None = None,
        secrets: AgentSecrets | None = None,
        secret_requests: SecretRequests | None = None,
    ) -> None:
        from core.chat import _command_builtin, _command_secret, _command_status

        self._chat_runs = chat_runs
        self._agent_resolver = agent_resolver
        self._sessions = sessions
        self._stop_all = stop_all
        self._trigger_service = trigger_service
        self._goals = goals
        self._execution_commands: dict[str, CommandExecutionHandler] = {
            "agent": partial(
                _command_builtin._execute_agent,
                agent_resolver=agent_resolver,
                agents=agents,
                automation_references=automation_references,
                chat_runs=chat_runs,
                projects=projects,
                sessions=sessions,
                snapshot_barrier=(
                    snapshot_barrier if snapshot_barrier is not None else SnapshotBarrier()
                ),
                terminal_manager=terminal_manager,
                command_manager=command_manager,
                trigger_service=trigger_service,
                goals=goals,
            ),
            "compact": partial(_command_builtin._execute_compact, trigger_service=trigger_service),
            "handoff": partial(
                _command_builtin._execute_handoff,
                agent_resolver=agent_resolver,
                agents=agents,
                chat_runs=chat_runs,
                sessions=sessions,
                storage=storage,
                trigger_service=trigger_service,
            ),
            "goal": self._execute_goal,
            "help": self._execute_help,
            "learn": partial(
                _command_builtin._execute_learn,
                agent_resolver=agent_resolver,
                chat_runs=chat_runs,
                storage=storage,
                trigger_service=trigger_service,
            ),
            "model": partial(
                _command_status._execute_model,
                agent_resolver=agent_resolver,
                agents=agents,
                projects=projects,
            ),
            "new": partial(
                _command_builtin._execute_new,
                agent_resolver=agent_resolver,
                agents=agents,
                chat_runs=chat_runs,
            ),
            "reflect": partial(
                _command_builtin._execute_reflect,
                agent_resolver=agent_resolver,
                chat_runs=chat_runs,
                reflection_service=reflection_service,
            ),
            "rename": partial(_command_builtin._execute_rename, sessions=sessions),
            "secret": partial(
                _command_secret.execute_secret,
                secrets=secrets,
                requests=secret_requests,
                sessions=sessions,
            ),
            "status": partial(
                _command_status._execute_status,
                agent_resolver=agent_resolver,
                chat_runs=chat_runs,
                context_window_resolver=context_window_resolver,
                models=models,
                projects=projects,
                providers=providers,
                reasoning_render_describer=reasoning_render_describer,
                sessions=sessions,
                started_at=started_at,
                storage=storage,
                wire_profile_describer=wire_profile_describer,
            ),
            "stop": self._execute_stop,
        }
        self._session_less_commands: dict[str, SessionLessCommandHandler] = {
            "help": self._execute_help,
            "model": partial(
                _command_status._execute_model_without_session,
                agent_resolver=agent_resolver,
                agents=agents,
                projects=projects,
            ),
            "new": _command_builtin._execute_new_without_session,
            "status": partial(
                _command_status._execute_status_without_session,
                agent_resolver=agent_resolver,
                context_window_resolver=context_window_resolver,
                models=models,
                projects=projects,
                providers=providers,
                reasoning_render_describer=reasoning_render_describer,
                started_at=started_at,
                storage=storage,
                wire_profile_describer=wire_profile_describer,
            ),
        }
        session_less = {
            name
            for name, spec in self.BUILT_IN_COMMANDS.items()
            if spec.new_session_mode == "session_less"
        }
        if session_less != set(self._session_less_commands):
            raise RuntimeError("every session-less Built-in Command needs a session-less handler")
        self._extension_commands = ExtensionCommandRegistry(self.BUILT_IN_COMMANDS)

    @classmethod
    def built_in_command_names(cls) -> frozenset[str]:
        """Return the immutable names reserved by Built-in Commands."""
        return frozenset(cls.BUILT_IN_COMMANDS)

    def catalog(self) -> tuple[CommandSpec, ...]:
        """Return the active combined command catalog sorted by canonical name."""
        combined = {
            **self.BUILT_IN_COMMANDS,
            **self._extension_commands.specs(),
        }
        return tuple(sorted(combined.values(), key=lambda spec: spec.name))

    def extension_command_owner(self, name: str) -> str | None:
        """Return the live Extension owner of *name*, if it is Extension-provided."""
        registered = self._extension_commands.get(name)
        return registered.extension_name if registered is not None else None

    def register_extension_command(
        self,
        extension_name: str,
        *,
        name: str,
        description: str,
        handler: ExtensionCommandHandler,
        argument: str = "optional",
        catalog_result: str = "notice",
        execution_mode: str = "serialized",
        argument_execution_mode: str | None = None,
        unavailable_surfaces: object = (),
        page_ids: frozenset[str] = frozenset(),
        argument_hint: str = "",
    ) -> int:
        return self._extension_commands.register(
            extension_name,
            name=name,
            description=description,
            handler=handler,
            argument=argument,
            catalog_result=catalog_result,
            execution_mode=execution_mode,
            argument_execution_mode=argument_execution_mode,
            unavailable_surfaces=unavailable_surfaces,
            page_ids=page_ids,
            argument_hint=argument_hint,
        )

    def unregister_extension_commands(self, extension_name: str) -> int:
        return self._extension_commands.unregister(extension_name)

    def prepare(self, content: str | list[ContentBlock]) -> PreparedCommand | None:
        """Recognize and parse one command-eligible Chat content value."""
        if isinstance(content, str):
            command_text = content
        else:
            texts = [block.text for block in content if isinstance(block, TextBlock)]
            if len(texts) != 1:
                return None
            command_text = texts[0]
        matched = self._match_command(command_text)
        if matched is None:
            return None
        # With attachments only a command that can carry a Secret value counts,
        # so the value never reaches the Agent; the command ignores them.
        if not isinstance(content, str) and len(content) != 1 and not matched[0].secret_argument:
            return None
        spec, argument, registration_id = matched
        execution_mode = (
            spec.argument_execution_mode
            if argument is not None and spec.argument_execution_mode is not None
            else spec.execution_mode
        )
        return PreparedCommand(
            name=spec.name,
            argument=argument,
            execution_mode=execution_mode,
            registration_id=registration_id,
            text=command_text.strip(),
            secret_argument=spec.secret_argument,
        )

    def unavailability(
        self, prepared: PreparedCommand, reply_surface: ReplySurface
    ) -> CommandUnavailability | None:
        """Return a Chat-owned surface restriction before scheduling execution."""
        spec = self._prepared_spec(prepared)
        if spec is None:
            return None
        if not spec.group_conversations and reply_surface.conversation_kind == "group":
            return CommandUnavailability(
                command=f"/{prepared.name}", surface=reply_surface.kind, conversation="group"
            )
        if reply_surface.kind not in spec.unavailable_surfaces:
            return None
        return CommandUnavailability(command=f"/{prepared.name}", surface=reply_surface.kind)

    async def execute(
        self, prepared: PreparedCommand, context: CommandExecutionContext
    ) -> CommandOutcome:
        """Execute one prepared command completely inside Chat core."""
        spec = self._prepared_spec(prepared)
        if spec is None:
            return _notice(
                prepared.name,
                f"The /{prepared.name} command is no longer available. Please send it again.",
            )
        unavailable = self.unavailability(prepared, context.reply_surface)
        if unavailable is not None:
            raise ValueError(
                f"{unavailable.command} is unavailable on {unavailable.surface} surfaces"
            )
        if prepared.registration_id is None:
            handler = self._execution_commands.get(prepared.name)
            if handler is None:
                raise ValueError(f"unknown Built-in Command: {prepared.name}")
            command_text = prepared.text or " ".join(
                part for part in (f"/{prepared.name}", prepared.argument) if part
            )
            return await handler(replace(context, command_text=command_text), prepared.argument)
        registered = self._extension_commands[prepared.name]
        return await self._execute_extension_command(registered, context, prepared.argument)

    async def execute_for_new_session(
        self, prepared: PreparedCommand, context: NewSessionCommandContext
    ) -> NewSessionCommandResult:
        """Execute one prepared command sent for a new Session, as its spec's mode says.

        A ``session_less`` command runs without a Session and creates none. A
        ``create_session`` command first checks the Agent and an override Model,
        then creates the Session with the context's Agent overrides and runs as
        :meth:`execute` would; when it left that Session unused (no History, no
        active or queued Run) the Session is removed again and the result names
        none. A ``needs_session`` command is refused with a notice.
        """
        spec = self._prepared_spec(prepared)
        if spec is None:
            return NewSessionCommandResult(
                _notice(
                    prepared.name,
                    f"The /{prepared.name} command is no longer available. Please send it again.",
                )
            )
        unavailable = self.unavailability(prepared, context.reply_surface)
        if unavailable is not None:
            raise ValueError(
                f"{unavailable.command} is unavailable on {unavailable.surface} surfaces"
            )
        if spec.new_session_mode == "session_less":
            handler = self._session_less_commands[prepared.name]
            return NewSessionCommandResult(await handler(context, prepared.argument))
        if spec.new_session_mode == "needs_session":
            return NewSessionCommandResult(
                _notice(
                    prepared.name,
                    f"/{prepared.name} works on an existing session. "
                    "Send a message first to start this one.",
                )
            )
        return await self._execute_in_created_session(prepared, context)

    async def _execute_in_created_session(
        self, prepared: PreparedCommand, context: NewSessionCommandContext
    ) -> NewSessionCommandResult:
        resolver = _require_dependency(self._agent_resolver, "AgentResolver")
        sessions = _require_dependency(self._sessions, "ChatSessionManager")
        # The Agent, an override Model and the working Project must be usable
        # before the Session exists.
        agent = await resolver.resolve_agent_async(
            context.project_id,
            context.agent_id,
            new_session_overrides=context.agent_overrides,
        )
        working_project_id = await resolver.resolve_working_project_async(
            context.project_id, agent, requested=context.working_project_id
        )
        session = await sessions.create_async(
            context.agent_id,
            session_id=context.session_id,
            project_id=context.project_id,
            actor="command",
            metadata=context.agent_overrides.session_metadata() or None,
            working_project_id=working_project_id if context.project_id is None else None,
        )
        address = SessionAddress(context.project_id, context.agent_id, session.id)
        try:
            outcome = await self.execute(
                prepared,
                CommandExecutionContext(
                    agent_id=context.agent_id,
                    session_id=session.id,
                    project_id=context.project_id,
                    reply_surface=context.reply_surface,
                    on_change=context.on_change,
                ),
            )
        except BaseException:
            await self._remove_unused_session(address)
            raise
        if await self._remove_unused_session(address):
            return NewSessionCommandResult(outcome)
        scope = {"agent_id": context.agent_id, "session_id": session.id}
        if context.project_id is not None:
            scope["project_id"] = context.project_id
        created = CommandResourceChange(kind="sessions", scope=scope)
        return NewSessionCommandResult(
            replace(outcome, resource_changes=(created, *outcome.resource_changes)),
            session_id=session.id,
        )

    async def _remove_unused_session(self, address: SessionAddress) -> bool:
        """Remove a Session a command created and left without History or Runs."""
        sessions = _require_dependency(self._sessions, "ChatSessionManager")
        try:
            async with self._chat_runs.session_admission_guard(address):
                versions = await sessions.run_async(sessions.list_history_versions, [address])
                if address in versions and versions[address][1] != 0:
                    return False
                await sessions.run_async(sessions.delete, address)
        except RunAdmissionBlockedError:
            return False
        except Exception:
            _LOGGER.warning(
                "Could not remove an unused command Session (agent=%s session=%s)",
                address.agent_id,
                address.session_id,
                exc_info=True,
            )
            return False
        return True

    def _prepared_spec(self, prepared: PreparedCommand) -> CommandSpec | None:
        if prepared.registration_id is None:
            return self.BUILT_IN_COMMANDS.get(prepared.name)
        registered = self._extension_commands.get(prepared.name)
        if registered is None or registered.registration_id != prepared.registration_id:
            return None
        return registered.spec

    def _match_command(
        self, message_text: str
    ) -> tuple[CommandSpec, str | None, int | None] | None:
        """Resolve a message to a command spec and its parsed argument.

        ``none`` commands match only when nothing trails the token, so text after
        a no-argument command falls through as a normal message. ``optional`` and
        ``required`` commands take the entire remainder after the first token as
        their argument through this single preparation source of truth.
        """
        stripped_text = message_text.strip()
        if not stripped_text.startswith("/"):
            return None
        first_token, *rest = stripped_text.split(maxsplit=1)
        remainder = rest[0] if rest else ""
        name = first_token[1:].lower()
        spec = self.BUILT_IN_COMMANDS.get(name)
        registered = self._extension_commands.get(name)
        if spec is None and registered is None:
            return None
        registration_id = None
        if spec is None and registered is not None:
            spec = registered.spec
            registration_id = registered.registration_id
        assert spec is not None
        argument = remainder.strip()
        if spec.argument == "none":
            if argument:
                return None
            return spec, None, registration_id
        if spec.argument == "required" and not argument:
            return None
        return spec, (argument or None), registration_id

    async def _execute_extension_command(
        self,
        registered: _RegisteredExtensionCommand,
        context: CommandExecutionContext,
        argument: str | None,
    ) -> CommandOutcome:
        extension_context = ExtensionCommandContext(
            agent_id=context.agent_id,
            session_id=context.session_id,
            project_id=context.project_id,
            reply_surface=context.reply_surface,
            _start_run=lambda content, internal: self._start_extension_follow_up(
                context, content, internal=internal
            ),
            _on_change=context.on_change,
        )
        try:
            result = await invoke_extension_handler(
                registered.handler,
                extension_context,
                argument,
            )
            validate_extension_outcome(
                result,
                expected_command=registered.spec.name,
                extension_name=registered.extension_name,
                page_ids=registered.page_ids,
            )
            if self._extension_commands.get(registered.spec.name) is not registered:
                raise ValueError("Extension command registration changed during execution")
            return cast(CommandOutcome, result)
        except Exception as exc:
            _LOGGER.error(
                "Extension command failed (extension=%s command=%s): %s",
                registered.extension_name,
                registered.spec.name,
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return _notice(
                registered.spec.name,
                f"The /{registered.spec.name} command failed. Check the server logs.",
            )

    async def _start_extension_follow_up(
        self,
        context: CommandExecutionContext,
        content: str | list[ContentBlock],
        *,
        internal: bool,
    ) -> Run:
        trigger_service = _require_dependency(self._trigger_service, "TriggerService")
        return cast(
            Run,
            await trigger_service.trigger_run(
                context.agent_id,
                content,
                context.session_id,
                internal=internal,
                reply_surface=context.reply_surface,
                project_id=context.project_id,
            ),
        )

    async def _execute_help(
        self, context: CommandExecutionContext | NewSessionCommandContext, argument: str | None
    ) -> CommandOutcome:
        lines = ["Slash commands:"]
        lines.extend(
            f"/{spec.name} - {spec.description}"
            for spec in self.catalog()
            if context.reply_surface.kind not in spec.unavailable_surfaces
        )
        lines.extend(
            [
                "",
                "Skill shortcuts also start with slash names. "
                "Use $skill-name to force a skill without sending a slash command.",
            ]
        )
        return CommandOutcome(
            command="help",
            feedback=CommandFeedback(kind="detail", text="\n".join(lines)),
        )

    async def _execute_goal(
        self, context: CommandExecutionContext, argument: str | None
    ) -> CommandOutcome:
        goals: GoalService = _require_dependency(self._goals, "GoalService")
        return await goals.execute_command(context, argument)

    async def _execute_stop(
        self, context: CommandExecutionContext, argument: str | None
    ) -> CommandOutcome:
        surface = context.reply_surface
        initiator = (
            f"channel_command:{surface.channel_id}"
            if surface.kind == "channel"
            else f"{surface.kind}_command"
        )
        address = SessionAddress(
            project_id=context.project_id,
            agent_id=context.agent_id,
            session_id=context.session_id,
        )
        if argument is not None:
            if argument.strip().casefold() != "all" or self._stop_all is None:
                return _notice("stop", "Use /stop or /stop all.")
            stopped = await self._stop_all(address)
            goal_paused = await self._pause_goal_for_stop(address)
            if not stopped:
                if goal_paused:
                    return _notice("stop", "Goal paused. Resume it with /goal resume.")
                return _notice("stop", "Nothing is running in this session.")
            text = "Stopped the run, its background commands, terminals and sub-agents."
            if goal_paused:
                text += " The goal is paused; resume it with /goal resume."
            return _notice("stop", text)
        try:
            self._chat_runs.cancel_by_session(
                context.agent_id,
                context.session_id,
                project_id=context.project_id,
                reason="user",
                initiator=initiator,
            )
        except RunNotFoundError:
            # Between automatic turns no Run is active, but Stop still ends them.
            if await self._pause_goal_for_stop(address):
                return _notice("stop", "Goal paused. Resume it with /goal resume.")
            return _notice("stop", "No active run to cancel.")
        if await self._pause_goal_for_stop(address):
            return _notice(
                "stop", "Run cancelled. The goal is paused; resume it with /goal resume."
            )
        return _notice("stop", "Run cancelled.")

    async def _pause_goal_for_stop(self, address: SessionAddress) -> bool:
        """Stop always ends a goal's automatic turns; True when it paused one."""
        if self._goals is None:
            return False
        return await self._goals.pause_for_stop(address)


def _has_exception_name(error: BaseException, expected_name: str) -> bool:
    return any(exception_type.__name__ == expected_name for exception_type in type(error).__mro__)
