"""End-to-end slash Command preparation and execution for Chat entry points."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, cast

from core.chat.content_blocks import ContentBlock, TextBlock
from core.chat.messages import ReplySurface
from core.chat.status_report import (
    ReasoningRenderDescriber,
)
from core.extensions.extensions import invoke_extension_handler
from core.runs import (
    ChatRunManager,
    Run,
    RunNotFoundError,
)
from core.skills.skill_validator import SKILL_NAME_TRIGGER_PATTERN
from core.tools.terminal_manager import TerminalManager
from core.utils.logging import get_logger
from core.utils.workers import BoundedWorkerPool

if TYPE_CHECKING:
    from core.agents import AgentStore
    from core.models.models import ModelRegistry
    from core.projects import AgentResolver, ProjectStore, RuntimeAgent
    from core.providers.providers import ProviderRegistry
    from core.sessions import ChatSessionManager
else:
    AgentResolver = Any
    AgentStore = Any
    ChatSessionManager = Any
    ModelRegistry = Any
    ProjectStore = Any
    ProviderRegistry = Any
    RuntimeAgent = Any

# Argument mode drives autocomplete: ``none`` commands run immediately on
# selection; ``optional``/``required`` insert the token and wait for text.
CommandArgumentMode = Literal["none", "optional", "required"]
CommandCatalogResult = Literal["notice", "detail", "state_change"]
CommandExecutionMode = Literal["immediate", "serialized"]
CommandFeedbackKind = Literal["notice", "detail"]
CommandNavigationKind = Literal["continue_in_session", "offer_session", "open_extension_page"]
CommandRunRole = Literal["primary", "follow_up"]
CommandSurfaceKind = Literal["webui", "channel"]

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


@dataclass(frozen=True)
class CommandSpec:
    """Declarative metadata for one slash command.

    The spec stays surface-neutral. Accessors project ``catalog_result`` into
    their own presentation vocabulary and honor availability/mode without
    branching on the command name.
    """

    name: str
    description: str
    argument: CommandArgumentMode
    catalog_result: CommandCatalogResult
    execution_mode: CommandExecutionMode
    argument_execution_mode: CommandExecutionMode | None = None
    accepts_preferred_session_id: bool = False
    unavailable_surfaces: frozenset[CommandSurfaceKind] = frozenset()


@dataclass(frozen=True)
class PreparedCommand:
    """One recognized and parsed command, ready for execution."""

    name: str
    argument: str | None
    execution_mode: CommandExecutionMode
    accepts_preferred_session_id: bool = False
    registration_id: int | None = None


@dataclass(frozen=True)
class CommandUnavailability:
    """A Chat-owned surface restriction discovered before execution."""

    command: str
    surface: CommandSurfaceKind

    def __post_init__(self) -> None:
        if not self.command.startswith("/"):
            raise ValueError("command unavailability token must start with '/'")


@dataclass(frozen=True)
class CommandFeedback:
    """Surface-neutral user feedback from a completed command."""

    kind: CommandFeedbackKind
    text: str


@dataclass(frozen=True)
class CommandNavigation:
    """A neutral Session or registered Extension page destination."""

    kind: CommandNavigationKind
    agent_id: str = ""
    session_id: str = ""
    project_id: str | None = None
    extension: str | None = None
    page: str | None = None
    route: str = ""


@dataclass(frozen=True)
class CommandRun:
    """A Run exposed by the command and its relationship to the response."""

    role: CommandRunRole
    run: Run


@dataclass(frozen=True)
class CommandResourceChange:
    """Accessor-neutral shared-resource invalidation fact."""

    kind: str
    scope: Mapping[str, str] = field(default_factory=dict)


CommandChangeObserver = Callable[[CommandResourceChange], None]


@dataclass(frozen=True)
class CommandExecutionContext:
    """Execution addressing and surface facts supplied by an accessor."""

    agent_id: str
    session_id: str
    project_id: str | None
    reply_surface: ReplySurface
    on_change: CommandChangeObserver | None = None
    preferred_new_session_id: str | None = None

    def report_change(self, change: CommandResourceChange) -> None:
        if self.on_change is not None:
            self.on_change(change)


ExtensionRunStarter = Callable[[str | list[ContentBlock], bool], Awaitable[Run]]


@dataclass(frozen=True)
class ExtensionCommandContext:
    """Narrow workflow surface handed to an Extension command handler."""

    agent_id: str
    session_id: str
    project_id: str | None
    reply_surface: ReplySurface
    _start_run: ExtensionRunStarter = field(repr=False)
    _on_change: CommandChangeObserver | None = field(default=None, repr=False)

    async def start_run(
        self,
        content: str | list[ContentBlock],
        *,
        internal: bool = False,
    ) -> Run:
        """Start or enqueue a follow-up Run at this command's current address."""
        return await self._start_run(content, internal)

    def report_change(self, change: CommandResourceChange) -> None:
        """Publish a time-sensitive neutral resource change when available."""
        if self._on_change is not None:
            self._on_change(change)


@dataclass(frozen=True)
class CommandOutcome:
    """Complete surface-neutral result of one slash command."""

    command: str
    feedback: CommandFeedback | None = None
    facts: Mapping[str, object] = field(default_factory=dict)
    navigation: CommandNavigation | None = None
    runs: tuple[CommandRun, ...] = ()
    resource_changes: tuple[CommandResourceChange, ...] = ()


CommandExecutionHandler = Callable[[CommandExecutionContext, str | None], Awaitable[CommandOutcome]]
ExtensionCommandHandler = Callable[[ExtensionCommandContext, str | None], Any]


@dataclass(frozen=True)
class _RegisteredExtensionCommand:
    spec: CommandSpec
    extension_name: str
    handler: ExtensionCommandHandler
    registration_id: int
    page_ids: frozenset[str]


@dataclass(frozen=True)
class HandoffArgument:
    """Parsed ``/handoff`` argument: an optional target agent and instruction."""

    target_agent_id: str | None
    instruction: str | None


def parse_handoff_argument(argument: str | None) -> HandoffArgument:
    """Split a raw ``/handoff`` argument into a target agent and an instruction.

    The grammar is an optional leading ``agent:<id>`` token that selects the
    receiving agent, with everything after it (or the whole argument when the
    token is absent) taken as a free-text instruction woven into the handoff
    prompt. The ``agent:`` keyword is matched case-insensitively while the id
    keeps its case. A bare ``agent:`` with no id is not a valid target, so it
    falls through as instruction text — a stray colon in free text never
    swallows the target slot (e.g. ``remember: call bob``).
    """
    text = (argument or "").strip()
    if not text:
        return HandoffArgument(target_agent_id=None, instruction=None)
    first_token, _, remainder = text.partition(" ")
    if first_token.lower().startswith("agent:"):
        target = first_token[len("agent:") :].strip()
        if target:
            return HandoffArgument(
                target_agent_id=target,
                instruction=remainder.strip() or None,
            )
    return HandoffArgument(target_agent_id=None, instruction=text)


@dataclass(frozen=True)
class AgentArgument:
    """Parsed ``/agent`` argument: a target address and an optional task."""

    address: str
    task: str | None


def parse_agent_argument(argument: str) -> AgentArgument:
    """Split a raw ``/agent`` argument into a target address and an optional task.

    The grammar is ``/agent``-specific and deliberately *not*
    ``parse_handoff_argument``: the first whitespace-separated token *is* the
    target address, and the trimmed remainder is an optional task. Address
    validation (the ``agent@projekt`` split) happens later through
    ``parse_agent_address`` — the one address seam — so a stray
    ``/agent agent:planner`` (a ``/handoff`` reflex) is rejected as a malformed
    address rather than silently reinterpreted as task text.
    """
    text = argument.strip()
    first_token, _, remainder = text.partition(" ")
    return AgentArgument(address=first_token, task=remainder.strip() or None)


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
            catalog_result="state_change",
            execution_mode="immediate",
            argument_execution_mode="serialized",
            unavailable_surfaces=frozenset({"channel"}),
        ),
        "compact": CommandSpec(
            "compact",
            "Compact the current session's context immediately.",
            argument="optional",
            catalog_result="notice",
            execution_mode="serialized",
        ),
        "handoff": CommandSpec(
            "handoff",
            "Write a handoff and start a new session (optionally for another agent).",
            argument="optional",
            catalog_result="state_change",
            execution_mode="serialized",
        ),
        "help": CommandSpec(
            "help",
            "Show available slash commands.",
            argument="none",
            catalog_result="detail",
            execution_mode="immediate",
        ),
        "learn": CommandSpec(
            "learn",
            "Author a reusable skill into your own home from a source (folder, URL, or text).",
            argument="optional",
            catalog_result="state_change",
            execution_mode="serialized",
        ),
        "model": CommandSpec(
            "model",
            "Show, set, or reset this session's model (/model reset to clear).",
            argument="optional",
            catalog_result="state_change",
            execution_mode="immediate",
            argument_execution_mode="serialized",
        ),
        "new": CommandSpec(
            "new",
            "Start a new session for the current agent.",
            argument="none",
            catalog_result="state_change",
            execution_mode="serialized",
            accepts_preferred_session_id=True,
        ),
        "reflect": CommandSpec(
            "reflect",
            "Review this session in a fork and save durable memory and skill updates.",
            argument="optional",
            catalog_result="state_change",
            execution_mode="serialized",
        ),
        "rename": CommandSpec(
            "rename",
            "Rename this session; no argument clears the name.",
            argument="optional",
            catalog_result="notice",
            execution_mode="serialized",
        ),
        "status": CommandSpec(
            "status",
            "Show current session and runtime status.",
            argument="none",
            catalog_result="detail",
            execution_mode="immediate",
        ),
        "stop": CommandSpec(
            "stop",
            "Cancel the active run for this session.",
            argument="none",
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
        local_context_windows_loader: Callable[[], Mapping[str, Any]] | None = None,
        trigger_service: Any | None = None,
        reflection_service: Any | None = None,
        storage: Any | None = None,
        terminal_manager: TerminalManager | None = None,
        reasoning_render_describer: ReasoningRenderDescriber | None = None,
    ) -> None:
        from core.chat import _command_builtin, _command_status

        self._chat_runs = chat_runs
        self._trigger_service = trigger_service
        self._execution_commands: dict[str, CommandExecutionHandler] = {
            "agent": partial(
                _command_builtin._execute_agent,
                agent_resolver=agent_resolver,
                agents=agents,
                chat_runs=chat_runs,
                projects=projects,
                sessions=sessions,
                terminal_manager=terminal_manager,
                trigger_service=trigger_service,
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
                sessions=sessions,
            ),
            "reflect": partial(
                _command_builtin._execute_reflect,
                agent_resolver=agent_resolver,
                chat_runs=chat_runs,
                reflection_service=reflection_service,
            ),
            "rename": partial(_command_builtin._execute_rename, sessions=sessions),
            "status": partial(
                _command_status._execute_status,
                agent_resolver=agent_resolver,
                chat_runs=chat_runs,
                local_context_windows_loader=local_context_windows_loader,
                models=models,
                projects=projects,
                providers=providers,
                reasoning_render_describer=reasoning_render_describer,
                sessions=sessions,
                started_at=started_at,
                storage=storage,
            ),
            "stop": self._execute_stop,
        }
        self._extension_commands: dict[str, _RegisteredExtensionCommand] = {}
        self._next_extension_registration_id = 1

    @classmethod
    def built_in_command_names(cls) -> frozenset[str]:
        """Return the immutable names reserved by Built-in Commands."""
        return frozenset(cls.BUILT_IN_COMMANDS)

    def catalog(self) -> tuple[CommandSpec, ...]:
        """Return the active combined command catalog sorted by canonical name."""
        combined = {
            **self.BUILT_IN_COMMANDS,
            **{name: registered.spec for name, registered in self._extension_commands.items()},
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
    ) -> int:
        """Validate and install one Extension-owned command.

        Runtime/ExtensionRegistry owns ordering and diagnostics. This seam still
        rejects every invalid or conflicting direct call so the dispatcher can
        never contain an ambiguous command table.
        """
        if not isinstance(extension_name, str) or not extension_name:
            raise ValueError("extension name must be a non-empty string")
        if (
            not isinstance(name, str)
            or name != name.lower()
            or SKILL_NAME_TRIGGER_PATTERN.fullmatch(name) is None
        ):
            raise ValueError(
                "name must be 1-64 lowercase letters, digits, hyphens, or underscores "
                "and start with a letter or digit"
            )
        if name in self.BUILT_IN_COMMANDS:
            raise ValueError("a Built-in Command already uses this name")
        if name in self._extension_commands:
            owner = self._extension_commands[name].extension_name
            raise ValueError(f"name already registered by extension {owner!r}")
        if not isinstance(description, str) or not description.strip():
            raise ValueError("description must be a non-empty string")
        if not callable(handler):
            raise ValueError("handler must be callable")
        if not isinstance(page_ids, frozenset) or any(
            not isinstance(page_id, str) or not page_id for page_id in page_ids
        ):
            raise ValueError("page_ids must contain registered page identifiers")
        if not isinstance(argument, str) or argument not in {"none", "optional", "required"}:
            raise ValueError("argument must be one of: none, optional, required")
        if not isinstance(catalog_result, str) or catalog_result not in {
            "notice",
            "detail",
            "state_change",
        }:
            raise ValueError("catalog_result must be one of: notice, detail, state_change")
        if not isinstance(execution_mode, str) or execution_mode not in {
            "immediate",
            "serialized",
        }:
            raise ValueError("execution_mode must be one of: immediate, serialized")
        if argument_execution_mode is not None and (
            not isinstance(argument_execution_mode, str)
            or argument_execution_mode not in {"immediate", "serialized"}
        ):
            raise ValueError(
                "argument_execution_mode must be one of: immediate, serialized, or None"
            )
        if (
            isinstance(unavailable_surfaces, (str, bytes))
            or not isinstance(unavailable_surfaces, (tuple, list, set, frozenset))
            or any(not isinstance(surface, str) for surface in unavailable_surfaces)
        ):
            raise ValueError(
                "unavailable_surfaces must be a collection containing webui and/or channel"
            )
        normalized_surfaces = frozenset(unavailable_surfaces)
        invalid_surfaces = normalized_surfaces - {"webui", "channel"}
        if invalid_surfaces:
            names = ", ".join(sorted(invalid_surfaces))
            raise ValueError(f"unavailable_surfaces contains unsupported values: {names}")

        registration_id = self._next_extension_registration_id
        self._next_extension_registration_id += 1
        self._extension_commands[name] = _RegisteredExtensionCommand(
            spec=CommandSpec(
                name=name,
                description=description.strip(),
                argument=cast(CommandArgumentMode, argument),
                catalog_result=cast(CommandCatalogResult, catalog_result),
                execution_mode=cast(CommandExecutionMode, execution_mode),
                argument_execution_mode=cast(CommandExecutionMode | None, argument_execution_mode),
                unavailable_surfaces=cast(frozenset[CommandSurfaceKind], normalized_surfaces),
            ),
            extension_name=extension_name,
            handler=handler,
            registration_id=registration_id,
            page_ids=page_ids,
        )
        return registration_id

    def unregister_extension_commands(self, extension_name: str) -> int:
        """Remove every live command owned by *extension_name*."""
        removed_names = [
            name
            for name, registered in self._extension_commands.items()
            if registered.extension_name == extension_name
        ]
        for name in removed_names:
            del self._extension_commands[name]
        return len(removed_names)

    def prepare(self, content: str | list[ContentBlock]) -> PreparedCommand | None:
        """Recognize and parse one command-eligible Chat content value."""
        if isinstance(content, str):
            command_text = content
        elif len(content) == 1 and isinstance(content[0], TextBlock):
            command_text = content[0].text
        else:
            return None
        matched = self._match_command(command_text)
        if matched is None:
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
            accepts_preferred_session_id=spec.accepts_preferred_session_id,
            registration_id=registration_id,
        )

    def unavailability(
        self, prepared: PreparedCommand, reply_surface: ReplySurface
    ) -> CommandUnavailability | None:
        """Return a Chat-owned surface restriction before scheduling execution."""
        spec = self._prepared_spec(prepared)
        if spec is None:
            return None
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
            return await handler(context, prepared.argument)
        registered = self._extension_commands[prepared.name]
        return await self._execute_extension_command(registered, context, prepared.argument)

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
        first_token, _, remainder = stripped_text.partition(" ")
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
            self._validate_extension_outcome(
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

    @staticmethod
    def _validate_extension_outcome(
        result: object,
        *,
        expected_command: str,
        extension_name: str,
        page_ids: frozenset[str],
    ) -> None:
        """Keep malformed Extension values from escaping into surface projectors."""
        if not isinstance(result, CommandOutcome):
            raise TypeError("handler must return CommandOutcome")
        if result.command != expected_command:
            raise ValueError(
                f"handler returned command {result.command!r}; expected {expected_command!r}"
            )
        if result.feedback is not None and (
            not isinstance(result.feedback, CommandFeedback)
            or result.feedback.kind not in {"notice", "detail"}
            or not isinstance(result.feedback.text, str)
        ):
            raise TypeError("CommandOutcome.feedback must be valid CommandFeedback")
        if not isinstance(result.facts, Mapping) or any(
            not isinstance(key, str) for key in result.facts
        ):
            raise TypeError("CommandOutcome.facts must be a mapping with string keys")
        if result.navigation is not None and (
            not isinstance(result.navigation, CommandNavigation)
            or result.navigation.kind
            not in {"continue_in_session", "offer_session", "open_extension_page"}
            or not isinstance(result.navigation.agent_id, str)
            or not isinstance(result.navigation.session_id, str)
            or (
                result.navigation.project_id is not None
                and not isinstance(result.navigation.project_id, str)
            )
        ):
            raise TypeError("CommandOutcome.navigation must be valid CommandNavigation")
        navigation = result.navigation
        if navigation is not None:
            if navigation.kind == "open_extension_page":
                if (
                    navigation.extension != extension_name
                    or not isinstance(navigation.page, str)
                    or navigation.page not in page_ids
                    or not isinstance(navigation.route, str)
                    or len(navigation.route) > 2048
                    or navigation.route.startswith("/")
                    or any(character in navigation.route for character in ("\\", "\0", ":"))
                    or any(part in {".", ".."} for part in navigation.route.split("/"))
                    or navigation.agent_id
                    or navigation.session_id
                    or navigation.project_id is not None
                ):
                    raise TypeError("CommandOutcome.navigation must be valid CommandNavigation")
            elif (
                navigation.extension is not None or navigation.page is not None or navigation.route
            ):
                raise TypeError("CommandOutcome.navigation must be valid CommandNavigation")
        if not isinstance(result.runs, tuple) or any(
            not isinstance(command_run, CommandRun)
            or command_run.role not in {"primary", "follow_up"}
            or not isinstance(command_run.run, Run)
            for command_run in result.runs
        ):
            raise TypeError("CommandOutcome.runs must contain valid CommandRun values")
        if sum(command_run.role == "primary" for command_run in result.runs) > 1:
            raise TypeError("CommandOutcome may contain at most one primary CommandRun")
        if not isinstance(result.resource_changes, tuple) or any(
            not isinstance(change, CommandResourceChange)
            or not isinstance(change.kind, str)
            or not change.kind
            or not isinstance(change.scope, Mapping)
            or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in change.scope.items()
            )
            for change in result.resource_changes
        ):
            raise TypeError(
                "CommandOutcome.resource_changes must contain valid CommandResourceChange values"
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
        self, context: CommandExecutionContext, argument: str | None
    ) -> CommandOutcome:
        lines = ["Slash commands:"]
        lines.extend(f"/{spec.name} - {spec.description}" for spec in self.catalog())
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

    async def _execute_stop(
        self, context: CommandExecutionContext, argument: str | None
    ) -> CommandOutcome:
        try:
            self._chat_runs.cancel_by_session(
                context.agent_id,
                context.session_id,
                project_id=context.project_id,
                reason="user",
            )
        except RunNotFoundError:
            return _notice("stop", "No active run to cancel.")
        return _notice("stop", "Run cancelled.")


def _has_exception_name(error: BaseException, expected_name: str) -> bool:
    return any(exception_type.__name__ == expected_name for exception_type in type(error).__mro__)
