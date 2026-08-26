"""End-to-end slash Command preparation and execution for Chat entry points."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from core.chat.content_blocks import ContentBlock, TextBlock
from core.chat.errors import CompactionUnavailableError
from core.chat.messages import ChatMessage, ReplySurface
from core.chat.status_report import (
    STATUS_PLACEHOLDER,
    ReasoningRenderDescriber,
    build_status_reply,
    resolve_reported_thinking_effort,
    resolve_status_activity,
    resolve_status_model_details,
    resolve_status_project_label,
    resolve_status_temperature,
)
from core.extensions.extensions import invoke_extension_handler
from core.projects import (
    AgentResolutionError,
    InvalidAgentAddressError,
    format_agent_address,
    parse_agent_address,
)
from core.runs import (
    ActiveRunError,
    ChatRunManager,
    Run,
    RunAdmissionBlockedError,
    RunNotFoundError,
)
from core.sessions import SESSION_MOVE_STRIP_META_KEYS, SessionAddress
from core.skills.skill_validator import SKILL_NAME_TRIGGER_PATTERN
from core.tools.availability import memory_tool_enabled
from core.tools.terminal_manager import TerminalManager, TerminalOwner
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
CommandNavigationKind = Literal["continue_in_session", "offer_session"]
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


# Plain-English origin wording for the /model reply, keyed by the provenance
# ``source`` tier from ``AgentResolver.effective_config``. Chat output, not i18n.
# Identity and project sessions read the same tiers differently, so they have
# separate maps; a missing/None source falls back to "not configured".
_MODEL_ORIGIN_NOT_CONFIGURED = "not configured"
_IDENTITY_MODEL_ORIGINS: dict[str | None, str] = {
    "agent": "agent configuration",
    "global_default": "global default",
}
_PROJECT_MODEL_ORIGINS: dict[str | None, str] = {
    "override": "override (set via /model)",
    "agent": "agent file in repo",
    "project_default": "project default",
    "global_default": "global default",
}
HANDOFF_FRAGMENT_NAME = "handoff.md"
LEARN_FRAGMENT_NAME = "learn.md"
CHANNEL_SOURCE_META_KEY = "source_channel_id"
SUBAGENT_SESSION_METADATA_FLAG = "is_subagent_session"
SUBAGENT_PARENT_METADATA_KEY = "subagent_parent"
AGENT_TAKEOVER_NOTE = "This session was just moved to you from {source}."
MODEL_RESET_TOKEN = "reset"
_MISSING = object()


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
    """A neutral Session destination and how an accessor should treat it."""

    kind: CommandNavigationKind
    agent_id: str
    session_id: str
    project_id: str | None = None


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


def _build_handoff_prompt(base_instruction: str, instruction: str | None) -> str:
    base = base_instruction.strip()
    cleaned = (instruction or "").strip()
    if not cleaned:
        return base
    return (
        f"{base}\n\n"
        "The user added a specific instruction for this handoff. Follow it while "
        "writing, without dropping anything else that genuinely matters:\n"
        f"{cleaned}"
    )


def _build_learn_prompt(base_instruction: str, argument: str | None) -> str:
    base = base_instruction.strip()
    cleaned = (argument or "").strip()
    if not cleaned:
        return (
            f"{base}\n\n"
            "No request was given. Ask the user what they want captured into a skill, or, "
            "if the recent conversation clearly demonstrates a reusable procedure, author "
            "a skill from that."
        )
    return f"{base}\n\nThe request to learn from:\n{cleaned}"


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


def _stored_agent_model(agents: Any, agent_id: str) -> object:
    getter = getattr(agents, "get_raw", None) or getattr(agents, "get", None)
    if not callable(getter):
        return _MISSING
    return getattr(getter(agent_id), "model", _MISSING)


def _stored_project_override(projects: Any, project_id: str, agent_id: str, field: str) -> object:
    getter = getattr(projects, "get", None)
    if not callable(getter):
        return _MISSING
    project = getter(project_id)
    overrides = getattr(project, "overrides", {})
    if not isinstance(overrides, Mapping):
        return None
    agent_override = overrides.get(agent_id, {})
    if not isinstance(agent_override, Mapping):
        return None
    return agent_override.get(field)


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
        self._chat_runs = chat_runs
        self._agent_resolver = agent_resolver
        self._sessions = sessions
        self._models = models
        self._started_at = started_at
        self._providers = providers
        self._projects = projects
        self._agents = agents
        self._trigger_service = trigger_service
        self._reflection_service = reflection_service
        self._storage = storage
        self._terminal_manager = terminal_manager
        # Adapter-backed render description for the /status thinking-effort
        # line; absent (e.g. minimally wired test setups) degrades to the
        # declared-control fallback.
        self._reasoning_render_describer = reasoning_render_describer
        # Live loader for the user-configured local-model window map, read at
        # execution time so a settings change applies to the next /status.
        self._local_context_windows_loader = local_context_windows_loader
        self._execution_commands: dict[str, CommandExecutionHandler] = {
            "agent": self._execute_agent,
            "compact": self._execute_compact,
            "handoff": self._execute_handoff,
            "help": self._execute_help,
            "learn": self._execute_learn,
            "model": self._execute_model,
            "new": self._execute_new,
            "reflect": self._execute_reflect,
            "rename": self._execute_rename,
            "status": self._execute_status,
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
            return self._notice(
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
            self._validate_extension_outcome(result, expected_command=registered.spec.name)
            return cast(CommandOutcome, result)
        except Exception as exc:
            _LOGGER.error(
                "Extension command failed (extension=%s command=%s): %s",
                registered.extension_name,
                registered.spec.name,
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            return self._notice(
                registered.spec.name,
                f"The /{registered.spec.name} command failed. Check the server logs.",
            )

    @staticmethod
    def _validate_extension_outcome(result: object, *, expected_command: str) -> None:
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
            or result.navigation.kind not in {"continue_in_session", "offer_session"}
            or not isinstance(result.navigation.agent_id, str)
            or not isinstance(result.navigation.session_id, str)
            or (
                result.navigation.project_id is not None
                and not isinstance(result.navigation.project_id, str)
            )
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

    @staticmethod
    def _notice(command: str, text: str) -> CommandOutcome:
        return CommandOutcome(command=command, feedback=CommandFeedback(kind="notice", text=text))

    async def _execute_compact(
        self, context: CommandExecutionContext, argument: str | None
    ) -> CommandOutcome:
        trigger_service = _require_dependency(self._trigger_service, "TriggerService")
        try:
            run = await trigger_service.start_compaction_run(
                context.agent_id,
                context.session_id,
                argument,
                project_id=context.project_id,
            )
        except CompactionUnavailableError:
            return self._notice("compact", "Compaction is not available.")
        except ActiveRunError:
            return self._notice(
                "compact",
                "Cannot compact while a run is active for this session.",
            )
        return CommandOutcome(
            command="compact",
            runs=(CommandRun(role="primary", run=run),),
        )

    async def _execute_handoff(
        self, context: CommandExecutionContext, argument: str | None
    ) -> CommandOutcome:
        resolver = _require_dependency(self._agent_resolver, "AgentResolver")
        sessions = _require_dependency(self._sessions, "ChatSessionManager")
        storage = _require_dependency(self._storage, "StorageManager")
        trigger_service = _require_dependency(self._trigger_service, "TriggerService")
        parsed = parse_handoff_argument(argument)
        try:
            if parsed.target_agent_id is None:
                target_agent_id, target_project_id = context.agent_id, context.project_id
            else:
                target_agent_id, target_project_id = parse_agent_address(parsed.target_agent_id)
        except InvalidAgentAddressError:
            return self._notice(
                "handoff", f"Cannot handoff to invalid agent address: {parsed.target_agent_id}"
            )

        target_display = format_agent_address(target_agent_id, target_project_id)
        if (
            self._chat_runs.active_run(
                agent_id=context.agent_id,
                session_id=context.session_id,
                project_id=context.project_id,
            )
            is not None
        ):
            return self._notice(
                "handoff", "A handoff can be started after the current run finishes."
            )

        if (target_agent_id, target_project_id) != (context.agent_id, context.project_id):
            try:
                await _COMMAND_WORKERS.run(
                    resolver.resolve_agent,
                    target_project_id,
                    target_agent_id,
                )
            except AgentResolutionError:
                return self._notice("handoff", f"Cannot handoff to unknown agent: {target_display}")

        handoff_prompt = await _COMMAND_WORKERS.run(
            storage.read_prompt_fragment,
            HANDOFF_FRAGMENT_NAME,
        )
        handoff_run = await trigger_service.trigger_run(
            context.agent_id,
            _build_handoff_prompt(handoff_prompt, parsed.instruction),
            session_id=context.session_id,
            project_id=context.project_id,
            internal=True,
            reply_surface=context.reply_surface,
        )
        handoff_message = await handoff_run.wait()
        handoff_text = _extract_text(handoff_message.content)
        if not handoff_text:
            return self._notice("handoff", "Handoff could not be generated.")

        target_session = await _command_session_io(
            sessions,
            "create_async",
            "create",
            target_agent_id,
            project_id=target_project_id,
        )
        if target_project_id is None:
            agents = _require_dependency(self._agents, "AgentStore")
            await _COMMAND_WORKERS.run(
                agents.update,
                target_agent_id,
                current_session_id=target_session.id,
            )
        change = CommandResourceChange(kind="sessions", scope={"agent_id": target_agent_id})
        context.report_change(change)
        target_run = await trigger_service.trigger_run(
            target_agent_id,
            handoff_text,
            session_id=target_session.id,
            project_id=target_project_id,
            internal=False,
            reply_surface=context.reply_surface,
        )
        _LOGGER.info(
            "Session handoff created "
            "(source_agent=%s source_session=%s target_agent=%s target_session=%s)",
            format_agent_address(context.agent_id, context.project_id),
            context.session_id,
            target_display,
            target_session.id,
        )
        return CommandOutcome(
            command="handoff",
            feedback=CommandFeedback(
                kind="notice",
                text=f"Handoff sent to {target_display}, session {target_session.id}.",
            ),
            facts={"session_id": target_session.id, "agent_id": target_display},
            navigation=CommandNavigation(
                kind="offer_session",
                agent_id=target_agent_id,
                session_id=target_session.id,
                project_id=target_project_id,
            ),
            runs=(CommandRun(role="follow_up", run=target_run),),
            resource_changes=(change,),
        )

    async def _execute_learn(
        self, context: CommandExecutionContext, argument: str | None
    ) -> CommandOutcome:
        resolver = _require_dependency(self._agent_resolver, "AgentResolver")
        storage = _require_dependency(self._storage, "StorageManager")
        trigger_service = _require_dependency(self._trigger_service, "TriggerService")
        if (
            self._chat_runs.active_run(
                agent_id=context.agent_id,
                session_id=context.session_id,
                project_id=context.project_id,
            )
            is not None
        ):
            return self._notice("learn", "A skill can be authored after the current run finishes.")
        agent = await _COMMAND_WORKERS.run(
            resolver.resolve_agent,
            context.project_id,
            context.agent_id,
        )
        if not getattr(agent, "workspace", ""):
            return self._notice(
                "learn", "Skill authoring needs an identity agent with its own skill home."
            )
        learn_prompt = await _COMMAND_WORKERS.run(
            storage.read_prompt_fragment,
            LEARN_FRAGMENT_NAME,
        )
        learn_run = await trigger_service.trigger_run(
            context.agent_id,
            _build_learn_prompt(learn_prompt, argument),
            session_id=context.session_id,
            project_id=context.project_id,
            internal=True,
            reply_surface=context.reply_surface,
        )
        learn_message = await learn_run.wait()
        summary = _extract_text(learn_message.content) or "Skill authoring run completed."
        return self._notice("learn", summary)

    async def _execute_reflect(
        self, context: CommandExecutionContext, argument: str | None
    ) -> CommandOutcome:
        resolver = _require_dependency(self._agent_resolver, "AgentResolver")
        reflection = _require_dependency(self._reflection_service, "ReflectionService")
        if (
            self._chat_runs.active_run(
                agent_id=context.agent_id,
                session_id=context.session_id,
                project_id=context.project_id,
            )
            is not None
        ):
            return self._notice("reflect", "A reflection can run after the current run finishes.")
        agent = await _COMMAND_WORKERS.run(
            resolver.resolve_agent,
            context.project_id,
            context.agent_id,
        )
        if not getattr(agent, "workspace", ""):
            return self._notice(
                "reflect", "Reflection needs an identity agent with its own memory and skill home."
            )
        if not memory_tool_enabled(agent.memory_prompt_mode):
            return self._notice(
                "reflect", "Reflection needs the memory Tool to be active for this Agent."
            )
        focus = (argument or "").strip()
        extra_instruction = (
            f"The user asked you to focus this reflection on:\n{focus}" if focus else None
        )
        change = CommandResourceChange(kind="sessions", scope={"agent_id": context.agent_id})
        result = await reflection.run_review(
            context.agent_id,
            context.session_id,
            project_id=context.project_id,
            extra_instruction=extra_instruction,
            on_fork_created=lambda _fork_id: context.report_change(change),
            reply_surface=context.reply_surface,
        )
        await _COMMAND_WORKERS.run(
            reflection.reset_counters,
            context.agent_id,
            context.session_id,
            context.project_id,
        )
        return CommandOutcome(
            command="reflect",
            feedback=CommandFeedback(kind="notice", text=result.summary or "Reflection completed."),
            facts={"session_id": result.session_id, "agent_id": context.agent_id},
            resource_changes=(change,),
        )

    async def _execute_agent(
        self, context: CommandExecutionContext, argument: str | None
    ) -> CommandOutcome:
        if argument is None:
            return CommandOutcome(
                command="agent",
                feedback=CommandFeedback(
                    kind="detail",
                    text=await _COMMAND_WORKERS.run(self._build_agent_directory),
                ),
            )

        resolver = _require_dependency(self._agent_resolver, "AgentResolver")
        sessions = _require_dependency(self._sessions, "ChatSessionManager")
        agents = _require_dependency(self._agents, "AgentStore")
        parsed = parse_agent_argument(argument)
        source_display = format_agent_address(context.agent_id, context.project_id)
        try:
            target_agent_id, target_project_id = parse_agent_address(parsed.address)
        except InvalidAgentAddressError:
            return self._notice("agent", f"Cannot move to invalid agent address: {parsed.address}")
        target_display = format_agent_address(target_agent_id, target_project_id)
        if (target_agent_id, target_project_id) == (context.agent_id, context.project_id):
            return self._notice("agent", f"This session already belongs to {target_display}.")
        if (
            self._chat_runs.active_run(
                agent_id=context.agent_id,
                session_id=context.session_id,
                project_id=context.project_id,
            )
            is not None
        ):
            return self._notice("agent", "This session can be moved once its current run finishes.")
        if self._chat_runs.list_queued(
            context.agent_id, context.session_id, project_id=context.project_id
        ):
            return self._notice("agent", "This session can be moved once its queued run finishes.")
        try:
            await _COMMAND_WORKERS.run(
                resolver.resolve_agent,
                target_project_id,
                target_agent_id,
            )
        except AgentResolutionError:
            return self._notice("agent", f"Cannot move to unknown agent: {target_display}")

        source_address = SessionAddress(
            project_id=context.project_id, agent_id=context.agent_id, session_id=context.session_id
        )
        target_address = SessionAddress(
            project_id=target_project_id, agent_id=target_agent_id, session_id=context.session_id
        )
        try:
            async with self._chat_runs.session_admission_guard(source_address, target_address):
                metadata = await _command_session_io(
                    sessions,
                    "get_metadata_async",
                    "get_metadata",
                    source_address,
                )
                refusal = self._session_move_block_reason(metadata)
                if refusal is not None:
                    return self._notice("agent", refusal)

                await sessions.move(
                    source_address,
                    target_address,
                    strip_meta_keys=SESSION_MOVE_STRIP_META_KEYS,
                )
                async with sessions.write_lock(target_address):
                    destination = await _command_session_io(
                        sessions,
                        "get_async",
                        "get",
                        target_address,
                    )
                    await _command_session_io(
                        destination,
                        "append_async",
                        "append",
                        ChatMessage.agent_takeover(
                            from_address=source_display, to_address=target_display
                        ),
                    )
                    await _command_session_io(
                        destination,
                        "add_note_async",
                        "add_note",
                        AGENT_TAKEOVER_NOTE.format(source=source_display),
                    )

                if self._terminal_manager is not None:
                    self._terminal_manager.transfer_scope(
                        TerminalOwner(
                            context.project_id,
                            context.agent_id,
                            context.session_id,
                        ),
                        TerminalOwner(
                            target_project_id,
                            target_agent_id,
                            context.session_id,
                        ),
                    )

                if context.project_id is None:
                    await _COMMAND_WORKERS.run(
                        agents.reset_current_after_session_removed,
                        context.agent_id,
                        context.session_id,
                    )
                if target_project_id is None:
                    await _COMMAND_WORKERS.run(
                        agents.update,
                        target_agent_id,
                        current_session_id=context.session_id,
                    )
        except RunAdmissionBlockedError:
            return self._notice(
                "agent", "This session can be moved once its source and destination are idle."
            )

        changes = [
            CommandResourceChange(kind="sessions", scope={"agent_id": context.agent_id}),
            CommandResourceChange(kind="sessions", scope={"agent_id": target_agent_id}),
        ]
        if context.project_id is None or target_project_id is None:
            changes.append(CommandResourceChange(kind="agents"))
        for change in changes:
            context.report_change(change)

        _LOGGER.info(
            "Session moved between Agents (session=%s source_agent=%s target_agent=%s)",
            context.session_id,
            source_display,
            target_display,
        )

        runs: tuple[CommandRun, ...] = ()
        if parsed.task is not None:
            trigger_service = _require_dependency(self._trigger_service, "TriggerService")
            task_run = await trigger_service.trigger_run(
                target_agent_id,
                parsed.task,
                session_id=context.session_id,
                project_id=target_project_id,
                internal=False,
                reply_surface=context.reply_surface,
            )
            runs = (CommandRun(role="follow_up", run=task_run),)
        reply = (
            f"Session moved to {target_display}; it is now running your task."
            if runs
            else f"Session moved to {target_display}; it is waiting."
        )
        return CommandOutcome(
            command="agent",
            feedback=CommandFeedback(kind="notice", text=reply),
            facts={"session_id": context.session_id, "agent_id": target_display},
            navigation=CommandNavigation(
                kind="offer_session",
                agent_id=target_agent_id,
                session_id=context.session_id,
                project_id=target_project_id,
            ),
            runs=runs,
            resource_changes=tuple(changes),
        )

    @staticmethod
    def _session_move_block_reason(metadata: Mapping[str, object]) -> str | None:
        if metadata.get(CHANNEL_SOURCE_META_KEY):
            return "A channel-bound session cannot be moved to another agent."
        if metadata.get(SUBAGENT_SESSION_METADATA_FLAG) or metadata.get(
            SUBAGENT_PARENT_METADATA_KEY
        ):
            return "A sub-agent session cannot be moved to another agent."
        return None

    async def _execute_model(
        self, context: CommandExecutionContext, argument: str | None
    ) -> CommandOutcome:
        if argument is None:
            return CommandOutcome(
                command="model",
                feedback=CommandFeedback(
                    kind="detail",
                    text=await _COMMAND_WORKERS.run(
                        self._build_model_summary,
                        context.agent_id,
                        context.project_id,
                    ),
                ),
            )
        raw = argument.strip()
        is_reset = raw.lower() == MODEL_RESET_TOKEN
        model = "" if is_reset else raw
        changed = await _COMMAND_WORKERS.run(
            self._apply_model_setting,
            context.agent_id,
            context.project_id,
            model,
            is_reset,
        )
        if changed:
            _LOGGER.info(
                "Agent model configuration %s (agent=%s field=model)",
                "reset" if is_reset else "updated",
                format_agent_address(context.agent_id, context.project_id),
            )
        return CommandOutcome(
            command="model",
            feedback=CommandFeedback(
                kind="notice", text="Model reset." if is_reset else f"Model set to {model}."
            ),
            facts={"agent_id": context.agent_id, "model": model},
        )

    def _apply_model_setting(
        self,
        agent_id: str,
        project_id: str | None,
        model: str,
        is_reset: bool,
    ) -> bool:
        resolver = _require_dependency(self._agent_resolver, "AgentResolver")
        if not is_reset:
            resolver.require_model_configured(model)
        if project_id is None:
            agents = _require_dependency(self._agents, "AgentStore")
            previous_model = _stored_agent_model(agents, agent_id)
            agents.update(agent_id, model=model)
            return previous_model is _MISSING or previous_model != model
        if is_reset:
            projects = _require_dependency(self._projects, "ProjectStore")
            previous_model = _stored_project_override(projects, project_id, agent_id, "model")
            projects.clear_override(project_id, agent_id, "model")
            return previous_model is _MISSING or previous_model is not None
        projects = _require_dependency(self._projects, "ProjectStore")
        previous_model = _stored_project_override(projects, project_id, agent_id, "model")
        projects.set_override(project_id, agent_id, "model", model)
        return previous_model is _MISSING or previous_model != model

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
            return self._notice("stop", "No active run to cancel.")
        return self._notice("stop", "Run cancelled.")

    async def _execute_new(
        self, context: CommandExecutionContext, argument: str | None
    ) -> CommandOutcome:
        resolver = _require_dependency(self._agent_resolver, "AgentResolver")
        sessions = _require_dependency(self._sessions, "ChatSessionManager")
        if (
            self._chat_runs.active_run(
                agent_id=context.agent_id,
                session_id=context.session_id,
                project_id=context.project_id,
            )
            is not None
        ):
            return self._notice(
                "new", "A new session can be started after the current run finishes."
            )
        await _COMMAND_WORKERS.run(
            resolver.resolve_agent,
            context.project_id,
            context.agent_id,
        )
        session = await _command_session_io(
            sessions,
            "create_async",
            "create",
            context.agent_id,
            session_id=context.preferred_new_session_id,
            project_id=context.project_id,
        )
        if context.project_id is None:
            agents = _require_dependency(self._agents, "AgentStore")
            await _COMMAND_WORKERS.run(
                agents.update,
                context.agent_id,
                current_session_id=session.id,
            )
        return CommandOutcome(
            command="new",
            feedback=CommandFeedback(kind="notice", text=f"New session started: {session.id}"),
            facts={"session_id": session.id},
            navigation=CommandNavigation(
                kind="continue_in_session",
                agent_id=context.agent_id,
                session_id=session.id,
                project_id=context.project_id,
            ),
            resource_changes=(
                CommandResourceChange(kind="sessions", scope={"agent_id": context.agent_id}),
            ),
        )

    async def _execute_rename(
        self, context: CommandExecutionContext, argument: str | None
    ) -> CommandOutcome:
        sessions = _require_dependency(self._sessions, "ChatSessionManager")
        stored_title = await _command_session_io(
            sessions,
            "set_title_async",
            "set_title",
            SessionAddress(
                project_id=context.project_id,
                agent_id=context.agent_id,
                session_id=context.session_id,
            ),
            argument or "",
        )
        reply = f"Session renamed to {stored_title}." if stored_title else "Session name cleared."
        return CommandOutcome(
            command="rename",
            feedback=CommandFeedback(kind="notice", text=reply),
            facts={"session_id": context.session_id, "title": stored_title},
        )

    async def _execute_status(
        self, context: CommandExecutionContext, argument: str | None
    ) -> CommandOutcome:
        agent: RuntimeAgent | None = None
        messages: list[ChatMessage] = []
        try:
            if self._agent_resolver is not None:
                agent = await _COMMAND_WORKERS.run(
                    self._agent_resolver.resolve_agent,
                    context.project_id,
                    context.agent_id,
                )
        except Exception as error:
            log = (
                _LOGGER.warning
                if _has_exception_name(error, "AgentResolutionError")
                else _LOGGER.error
            )
            log(
                "Failed to resolve agent %r while building /status reply",
                context.agent_id,
                exc_info=True,
            )
        try:
            if self._sessions is not None:
                session = await _command_session_io(
                    self._sessions,
                    "get_async",
                    "get",
                    SessionAddress(
                        project_id=context.project_id,
                        agent_id=context.agent_id,
                        session_id=context.session_id,
                    ),
                )
                messages = await _command_session_io(
                    session,
                    "load_async",
                    "load",
                )
        except Exception as error:
            log = (
                _LOGGER.warning if _has_exception_name(error, "ChatSessionError") else _LOGGER.error
            )
            log(
                "Failed to load session %r for agent %r while building /status reply",
                context.session_id,
                context.agent_id,
                exc_info=True,
            )
        model_details = resolve_status_model_details(
            agent,
            self._models,
            self._providers,
            local_context_windows=self._load_local_context_windows(),
        )
        activity = resolve_status_activity(
            self._chat_runs,
            context.agent_id,
            context.session_id,
            context.project_id,
        )
        text = build_status_reply(
            agent,
            messages,
            model_details.context_window,
            self._started_at,
            model_details.display_name,
            activity,
            actual_thinking_effort=resolve_reported_thinking_effort(
                agent=agent,
                models=self._models,
                model_details=model_details,
                describe_render=self._reasoning_render_describer,
            ),
            project_label=resolve_status_project_label(self._projects, context.project_id),
            temperature_status=resolve_status_temperature(
                agent.temperature if agent is not None else None,
                model_details,
            ),
        )
        return CommandOutcome(
            command="status",
            feedback=CommandFeedback(kind="detail", text=text),
        )

    def _build_agent_directory(self) -> str:
        """List the move targets: personal agents plus every project's team.

        Bare ids are personal agents; team agents are shown project-qualified as
        ``name@projekt`` through the one address seam, so the card itself teaches
        the addressing the move expects. A project whose scan fails is skipped
        rather than failing the whole card.
        """
        lines = ["Move this session to another agent with /agent <id> [task].", ""]

        personal = sorted(agent.id for agent in self._agents.list()) if self._agents else []
        lines.append("Personal agents:")
        if personal:
            lines.extend(f"  {format_agent_address(agent_id, None)}" for agent_id in personal)
        else:
            lines.append("  (none)")

        if self._projects is not None and self._agent_resolver is not None:
            for project in self._projects.list():
                try:
                    team = self._agent_resolver.scan_project_report(project).team
                except Exception:
                    _LOGGER.warning(
                        "Failed to scan project %r while building the /agent directory",
                        project.project_id,
                        exc_info=True,
                    )
                    continue
                if not team:
                    continue
                lines.append("")
                lines.append(f"Team — {project.display_name} ({project.project_id}):")
                lines.extend(
                    f"  {format_agent_address(member.agent_id, project.project_id)}"
                    for member in sorted(team, key=lambda member: member.agent_id)
                )
        return "\n".join(lines)

    def _build_model_summary(self, agent_id: str, project_id: str | None) -> str:
        """Describe the session's current model and where it resolves from.

        Reads the resolver's provenance seam once (``effective_config``): the model
        value is what the next run would use (already post-override), and its source names
        the winning tier. None-guarded like ``/status`` so a minimally constructed
        dispatcher degrades to a placeholder instead of crashing; a resolver error is
        logged and degrades to placeholder + "not configured".
        """
        model = STATUS_PLACEHOLDER
        source: str | None = None
        if self._agent_resolver is not None:
            try:
                effective = self._agent_resolver.effective_config(project_id, agent_id)
                model_field = effective.get("model", {})
                value = model_field.get("value")
                model = (value or "").strip() or STATUS_PLACEHOLDER
                source = model_field.get("source")
            except Exception as error:
                log = (
                    _LOGGER.warning
                    if _has_exception_name(error, "AgentResolutionError")
                    else _LOGGER.error
                )
                log(
                    "Failed to resolve agent %r while building /model reply",
                    agent_id,
                    exc_info=True,
                )
        return f"Current model: {model}\nSource: {self._model_origin(project_id, source)}"

    def _model_origin(self, project_id: str | None, source: str | None) -> str:
        """Return where the session's current model comes from, in plain English.

        Maps the provenance ``source`` tier (from ``effective_config``) onto the
        wire wording, keyed by session kind so identity and project sessions read
        differently. A ``None`` source (chain fully fell through) reports "not
        configured".
        """
        if project_id is None:
            return _IDENTITY_MODEL_ORIGINS.get(source, _MODEL_ORIGIN_NOT_CONFIGURED)
        return _PROJECT_MODEL_ORIGINS.get(source, _MODEL_ORIGIN_NOT_CONFIGURED)

    def _load_local_context_windows(self) -> Mapping[str, Any]:
        """Return the live local-model window map, empty when no loader is wired."""
        if self._local_context_windows_loader is None:
            return {}
        try:
            return self._local_context_windows_loader()
        except Exception:
            _LOGGER.warning("Failed to load local-model context windows", exc_info=True)
            return {}


def _has_exception_name(error: BaseException, expected_name: str) -> bool:
    return any(exception_type.__name__ == expected_name for exception_type in type(error).__mro__)
