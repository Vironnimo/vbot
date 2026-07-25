"""End-to-end slash Command preparation and execution for Chat entry points."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

from core.chat.content_blocks import ContentBlock, TextBlock
from core.chat.messages import ChatMessage, ReplySurface
from core.chat.usage import aggregate_session_usage
from core.projects import (
    AgentResolutionError,
    InvalidAgentAddressError,
    format_agent_address,
    parse_agent_address,
)
from core.providers.providers import resolve_effective_context_window
from core.providers.reasoning import (
    REASONING_INTENT_BUDGET,
    REASONING_INTENT_DEFAULT,
    REASONING_INTENT_OFF,
    REASONING_INTENT_ON,
    resolve_reasoning_intent,
)
from core.runs import ChatRunManager, Run, RunAdmissionBlockedError, RunNotFoundError
from core.sessions import SESSION_MOVE_STRIP_META_KEYS
from core.skills.skill_validator import SKILL_NAME_TRIGGER_PATTERN
from core.utils.logging import get_logger

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

StatusActivityName = Literal["idle", "running"]

# Argument mode drives autocomplete: ``none`` commands run immediately on
# selection; ``optional``/``required`` insert the token and wait for text.
CommandArgumentMode = Literal["none", "optional", "required"]
CommandCatalogResult = Literal["notice", "detail", "state_change"]
CommandExecutionMode = Literal["immediate", "serialized"]
CommandFeedbackKind = Literal["notice", "detail"]
CommandNavigationKind = Literal["continue_in_session", "offer_session"]
CommandRunRole = Literal["follow_up"]
CommandSurfaceKind = Literal["webui", "channel"]

_LOGGER = get_logger("chat.commands")

STATUS_PLACEHOLDER = "—"
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
# Reported "actual" reasoning state for a model steered by a thinking toggle or a
# token budget rather than an effort ladder: there is no effort level to show, so
# ``/status`` reports whether reasoning is on or off for the selection.
REASONING_STATE_ON = "on"
REASONING_STATE_OFF = "off"
_STATUS_TIME_FORMAT = "%Y-%m-%d %H:%M:%S %Z"
_CACHE_PERCENT_SCALE = 100
_CACHE_HIT_RATE_DECIMALS = 1
_STATUS_MODEL_DISPLAY_OVERRIDE: ContextVar[str | None] = ContextVar(
    "status_model_display_override",
    default=None,
)
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


@dataclass(frozen=True)
class StatusActivity:
    """Run activity summary for one Session."""

    activity: StatusActivityName
    run_id: str | None
    created_at: str | None
    updated_at: str | None


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
            result = registered.handler(extension_context, argument)
            if inspect.isawaitable(result):
                result = await result
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
            or command_run.role != "follow_up"
            or not isinstance(command_run.run, Run)
            for command_run in result.runs
        ):
            raise TypeError("CommandOutcome.runs must contain valid follow-up CommandRun values")
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
        reply = await trigger_service.compact_session(
            context.agent_id,
            context.session_id,
            argument,
            project_id=context.project_id,
        )
        return self._notice("compact", reply)

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
                resolver.resolve_agent(target_project_id, target_agent_id)
            except AgentResolutionError:
                return self._notice("handoff", f"Cannot handoff to unknown agent: {target_display}")

        handoff_run = await trigger_service.trigger_run(
            context.agent_id,
            _build_handoff_prompt(
                storage.read_prompt_fragment(HANDOFF_FRAGMENT_NAME), parsed.instruction
            ),
            session_id=context.session_id,
            project_id=context.project_id,
            internal=True,
            reply_surface=context.reply_surface,
        )
        handoff_message = await handoff_run.wait()
        handoff_text = _extract_text(handoff_message.content)
        if not handoff_text:
            return self._notice("handoff", "Handoff could not be generated.")

        target_session = sessions.create(target_agent_id, project_id=target_project_id)
        if target_project_id is None:
            agents = _require_dependency(self._agents, "AgentStore")
            agents.update(target_agent_id, current_session_id=target_session.id)
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
        agent = resolver.resolve_agent(context.project_id, context.agent_id)
        if not getattr(agent, "workspace", ""):
            return self._notice(
                "learn", "Skill authoring needs an identity agent with its own skill home."
            )
        learn_run = await trigger_service.trigger_run(
            context.agent_id,
            _build_learn_prompt(storage.read_prompt_fragment(LEARN_FRAGMENT_NAME), argument),
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
        agent = resolver.resolve_agent(context.project_id, context.agent_id)
        if not getattr(agent, "workspace", ""):
            return self._notice(
                "reflect", "Reflection needs an identity agent with its own memory and skill home."
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
        reflection.reset_counters(context.agent_id, context.session_id, context.project_id)
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
                feedback=CommandFeedback(kind="detail", text=self._build_agent_directory()),
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
            resolver.resolve_agent(target_project_id, target_agent_id)
        except AgentResolutionError:
            return self._notice("agent", f"Cannot move to unknown agent: {target_display}")

        source_session_key = (context.project_id, context.agent_id, context.session_id)
        target_session_key = (target_project_id, target_agent_id, context.session_id)
        try:
            async with self._chat_runs.session_admission_guard(
                source_session_key, target_session_key
            ):
                metadata = sessions.get_metadata(
                    context.agent_id, context.session_id, context.project_id
                )
                refusal = self._session_move_block_reason(metadata)
                if refusal is not None:
                    return self._notice("agent", refusal)

                await sessions.move(
                    context.agent_id,
                    context.session_id,
                    target_agent_id,
                    source_project_id=context.project_id,
                    target_project_id=target_project_id,
                    strip_meta_keys=SESSION_MOVE_STRIP_META_KEYS,
                )
                async with sessions.write_lock(
                    target_agent_id, context.session_id, target_project_id
                ):
                    destination = sessions.get(
                        target_agent_id, context.session_id, target_project_id
                    )
                    destination.append(
                        ChatMessage.agent_takeover(
                            from_address=source_display, to_address=target_display
                        )
                    )
                    destination.add_note(AGENT_TAKEOVER_NOTE.format(source=source_display))

                if context.project_id is None:
                    agents.reset_current_after_session_removed(context.agent_id, context.session_id)
                if target_project_id is None:
                    agents.update(target_agent_id, current_session_id=context.session_id)
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
                    text=self._build_model_summary(context.agent_id, context.project_id),
                ),
            )
        resolver = _require_dependency(self._agent_resolver, "AgentResolver")
        raw = argument.strip()
        is_reset = raw.lower() == MODEL_RESET_TOKEN
        model = "" if is_reset else raw
        if not is_reset:
            resolver.require_model_configured(model)
        changed = True
        if context.project_id is None:
            agents = _require_dependency(self._agents, "AgentStore")
            previous_model = _stored_agent_model(agents, context.agent_id)
            agents.update(context.agent_id, model=model)
            changed = previous_model is _MISSING or previous_model != model
        elif is_reset:
            projects = _require_dependency(self._projects, "ProjectStore")
            previous_model = _stored_project_override(
                projects, context.project_id, context.agent_id, "model"
            )
            projects.clear_override(context.project_id, context.agent_id, "model")
            changed = previous_model is _MISSING or previous_model is not None
        else:
            projects = _require_dependency(self._projects, "ProjectStore")
            previous_model = _stored_project_override(
                projects, context.project_id, context.agent_id, "model"
            )
            projects.set_override(context.project_id, context.agent_id, "model", model)
            changed = previous_model is _MISSING or previous_model != model
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
        resolver.resolve_agent(context.project_id, context.agent_id)
        session = sessions.create(
            context.agent_id,
            session_id=context.preferred_new_session_id,
            project_id=context.project_id,
        )
        if context.project_id is None:
            agents = _require_dependency(self._agents, "AgentStore")
            agents.update(context.agent_id, current_session_id=session.id)
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
        stored_title = sessions.set_title(
            context.agent_id,
            context.session_id,
            argument or "",
            context.project_id,
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
                agent = self._agent_resolver.resolve_agent(context.project_id, context.agent_id)
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
                messages = self._sessions.get(
                    context.agent_id, context.session_id, context.project_id
                ).load()
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
            actual_thinking_effort=resolve_actual_thinking_effort(
                agent.thinking_effort if agent is not None else None,
                model_details.reasoning_levels,
                model_details.reasoning_control,
                model_details.reasoning_budget_max,
            ),
            project_label=resolve_status_project_label(self._projects, context.project_id),
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


@dataclass(frozen=True)
class StatusModelDetails:
    """Model facts needed to render a status reply.

    ``reasoning_levels`` is the model's effective effort ladder (empty when the
    model has no feed ladder), ``reasoning_control`` its wire control kind
    (``levels`` / ``on_off`` / ``budget`` / ``None``), and ``reasoning_budget_max``
    the max thinking-token budget for a ``budget`` model (``None`` when unknown).
    Together they let ``resolve_actual_thinking_effort`` report the *actual*
    reasoning sent on the wire — a snapped effort for a ladder, ``on``/``off`` for
    a toggle, or the rendered token budget for a budget model.
    """

    context_window: int | None
    display_name: str | None
    reasoning_levels: tuple[str, ...] = ()
    reasoning_control: str | None = None
    reasoning_budget_max: int | None = None


def resolve_status_model_details(
    agent: RuntimeAgent | None,
    models: ModelRegistry | None,
    providers: ProviderRegistry | None = None,
    local_context_windows: Mapping[str, Any] | None = None,
) -> StatusModelDetails:
    """Resolve model facts for status output from the model registry.

    Returns context window, display name, and the effective reasoning-effort
    ladder. A missing agent/registry/model yields empty details so status
    rendering degrades to placeholders instead of failing.

    ``context_window`` is the *effective* window (user-set/capped for
    flagged-local models, else the read-side default chain — see
    :func:`resolve_effective_context_window`), so ``/status`` reports the budget
    compaction actually uses rather than ``unknown`` for a window-less model.
    It stays ``None`` only when no model could be resolved at all.
    """
    if agent is None or models is None:
        return StatusModelDetails(context_window=None, display_name=None)

    provider_id, model_id = _parse_registry_model_key(agent.model)
    if provider_id is None or model_id is None:
        return StatusModelDetails(context_window=None, display_name=None)

    try:
        model = models.get(provider_id, model_id)
    except KeyError:
        _LOGGER.warning(
            "Model registry entry missing for %r/%r while building status",
            provider_id,
            model_id,
        )
        return StatusModelDetails(context_window=None, display_name=None)
    except Exception:
        _LOGGER.error(
            "Failed model registry lookup for %r/%r while building status",
            provider_id,
            model_id,
            exc_info=True,
        )
        return StatusModelDetails(context_window=None, display_name=None)

    return StatusModelDetails(
        context_window=resolve_effective_context_window(
            model.context_window,
            _status_provider_config(providers, provider_id),
            model_metadata=model.metadata,
            model_key=f"{provider_id}/{model_id}",
            local_context_windows=local_context_windows,
        ),
        display_name=model.name,
        reasoning_levels=tuple(model.capabilities.reasoning.levels),
        reasoning_control=model.capabilities.reasoning.control,
        reasoning_budget_max=model.capabilities.reasoning.budget_max,
    )


def _status_provider_config(providers: ProviderRegistry | None, provider_id: str) -> Any:
    """Return the ProviderConfig for the read-side window default, or None."""
    if providers is None:
        return None
    try:
        return providers.get(provider_id)
    except (KeyError, AttributeError):
        return None


def resolve_status_project_label(
    projects: ProjectStore | None,
    project_id: str | None,
) -> str | None:
    """Return a display label for the session's project, or ``None`` for identity.

    An identity session (``project_id is None``) has no project, so status renders
    the placeholder. A project session resolves the project's display name as
    ``"<display name> (<id>)"``; it degrades to the bare id when the store is
    absent or the project can't be loaded — the stable id is still informative.
    """
    if project_id is None:
        return None
    if projects is None:
        return project_id
    try:
        project = projects.get(project_id)
    except Exception:
        _LOGGER.warning(
            "Failed to load project %r while building status reply",
            project_id,
            exc_info=True,
        )
        return project_id
    return f"{project.display_name} ({project_id})"


def resolve_actual_thinking_effort(
    selected_effort: str | None,
    reasoning_levels: tuple[str, ...],
    reasoning_control: str | None = None,
    reasoning_budget_max: int | None = None,
) -> str | None:
    """Return the reasoning actually sent on the wire for the selected effort.

    Reuses :func:`resolve_reasoning_intent` — the same policy the adapters render
    — so ``/status`` reports exactly what reaches the provider:

    * ``levels`` control (or any non-empty ladder): the snapped effort level.
    * ``budget`` control: ``"on (<N> tokens)"`` — the rendered token budget,
      scaled by ``reasoning_budget_max`` when seeded (else the absolute ladder).
    * ``on_off`` control: ``"on"`` / ``"off"``.
    * Otherwise ``None`` (no effort selected, or no ladder/control to report —
      the adapter then applies its own floor, which is not visible here).
    """
    intent = resolve_reasoning_intent(
        supported=True,
        control=reasoning_control,
        levels=reasoning_levels,
        effort=selected_effort,
        budget_max=reasoning_budget_max,
        max_tokens=None,
    )
    if intent.kind == REASONING_INTENT_DEFAULT:
        return None
    if intent.kind == REASONING_INTENT_OFF:
        return REASONING_STATE_OFF
    if intent.kind == REASONING_INTENT_ON:
        return REASONING_STATE_ON
    if intent.kind == REASONING_INTENT_BUDGET:
        return f"{REASONING_STATE_ON} ({intent.budget_tokens:,} tokens)"
    return intent.effort_level


def build_status_reply(
    agent: RuntimeAgent | None,
    messages: list[ChatMessage],
    context_window: int | None,
    started_at: datetime | None,
    model_display_name: str | None,
    activity: StatusActivity | None = None,
    actual_thinking_effort: str | None = None,
    project_label: str | None = None,
) -> str:
    """Build status text while applying an optional model-display override."""
    token = _STATUS_MODEL_DISPLAY_OVERRIDE.set(model_display_name)
    try:
        return build_status_text(
            agent,
            messages,
            context_window,
            started_at,
            activity,
            actual_thinking_effort=actual_thinking_effort,
            project_label=project_label,
        )
    finally:
        _STATUS_MODEL_DISPLAY_OVERRIDE.reset(token)


def build_status_text(
    agent: RuntimeAgent | None,
    messages: list[ChatMessage],
    context_window: int | None,
    started_at: datetime | None,
    activity: StatusActivity | None = None,
    actual_thinking_effort: str | None = None,
    project_label: str | None = None,
) -> str:
    """Build human-readable status text for the current session and runtime state.

    ``actual_thinking_effort`` is what reaches the wire after the model's ladder
    snaps the agent's selection (see :func:`resolve_actual_thinking_effort`); it
    is rendered alongside the selected effort so the two can differ visibly.
    ``project_label`` names the session's project (``None`` for an identity
    session, rendered as the placeholder).
    """
    now_utc = datetime.now(UTC)
    now_local = now_utc.astimezone()

    if agent is None:
        agent_summary = STATUS_PLACEHOLDER
        model_display = STATUS_PLACEHOLDER
        fallback_model = STATUS_PLACEHOLDER
        selected_thinking_effort = STATUS_PLACEHOLDER
        temperature = STATUS_PLACEHOLDER
    else:
        model_string = agent.model.strip() or STATUS_PLACEHOLDER
        agent_summary = f"{agent.name} ({model_string})"
        model_display = _STATUS_MODEL_DISPLAY_OVERRIDE.get() or _model_display_name(model_string)
        fallback_model = agent.fallback_model.strip() or STATUS_PLACEHOLDER
        selected_thinking_effort = _thinking_effort_text(agent.thinking_effort)
        temperature = _temperature_text(agent.temperature)

    actual_thinking_effort_text = _actual_thinking_effort_text(actual_thinking_effort)
    context_usage = _context_usage_text(messages, context_window)
    last_request_cache = _last_request_cache_text(messages)
    session_cache = _session_cache_text(messages)
    session_started = _session_started_text(messages, now_utc)
    turn_count = _turn_count_text(messages)
    app_uptime = _app_uptime_text(started_at, now_utc)
    activity_name = activity.activity if activity is not None else STATUS_PLACEHOLDER
    run_created_at = activity.created_at if activity is not None else None
    run_updated_at = activity.updated_at if activity is not None else None

    lines = [
        f"Agent: {agent_summary}",
        f"Project: {project_label or STATUS_PLACEHOLDER}",
        f"Model display name: {model_display}",
        f"Fallback model: {fallback_model}",
        f"Selected thinking effort: {selected_thinking_effort}",
        f"Actual model thinking effort: {actual_thinking_effort_text}",
        f"Temperature: {temperature}",
        f"Activity: {activity_name}",
        f"Run created at: {run_created_at or STATUS_PLACEHOLDER}",
        f"Run updated at: {run_updated_at or STATUS_PLACEHOLDER}",
        f"Context usage: {context_usage}",
        f"Last request cache: {last_request_cache}",
        f"Session cache: {session_cache}",
        f"Session started: {session_started}",
        f"Turn count: {turn_count}",
        f"App uptime: {app_uptime}",
        f"Current time: {now_local.strftime(_STATUS_TIME_FORMAT)}",
    ]
    return "\n".join(lines)


def resolve_status_activity(
    chat_runs: ChatRunManager,
    agent_id: str,
    session_id: str,
    project_id: str | None,
) -> StatusActivity:
    """Return running/idle activity for one Session (project-scoped run key)."""
    run = chat_runs.active_run(agent_id=agent_id, session_id=session_id, project_id=project_id)
    if run is None:
        return StatusActivity(
            activity="idle",
            run_id=None,
            created_at=None,
            updated_at=None,
        )
    return StatusActivity(
        activity="running",
        run_id=run.id,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _model_display_name(model_string: str) -> str:
    _, model_id = _parse_registry_model_key(model_string)
    if model_id is None:
        return STATUS_PLACEHOLDER
    return model_id


def _thinking_effort_text(value: str | None) -> str:
    if value is None:
        return "default"
    return value.strip() or "default"


def _actual_thinking_effort_text(value: str | None) -> str:
    """Render the snapped wire effort, or a placeholder when it is not resolvable.

    ``None`` means there is nothing to report: no effort was selected (provider
    default) or the model exposes no ladder to snap against (the adapter floor is
    not visible here). The selected-effort line still shows the agent's choice.
    """
    if not value:
        return STATUS_PLACEHOLDER
    return value


def _temperature_text(value: float | None) -> str:
    if value is None:
        return "default"
    return f"{value:g}"


def _parse_registry_model_key(model_string: str) -> tuple[str | None, str | None]:
    normalized_model = _strip_pinned_connection_suffix(model_string.strip())
    provider_id, separator, model_id = normalized_model.partition("/")
    if not provider_id or not separator or not model_id:
        return None, None
    return provider_id, model_id


def _strip_pinned_connection_suffix(model_string: str) -> str:
    base_model, separator, _connection_id = model_string.rpartition("::")
    if separator and base_model:
        return base_model
    return model_string


def _context_usage_text(messages: list[ChatMessage], context_window: int | None) -> str:
    if context_window is None or context_window <= 0:
        return STATUS_PLACEHOLDER

    latest_usage = _latest_assistant_usage(messages)
    if latest_usage is None:
        return STATUS_PLACEHOLDER

    input_tokens, estimated = latest_usage
    prefix = "~" if estimated else ""
    return f"{prefix}{input_tokens} / {context_window}"


def _turn_count_text(messages: list[ChatMessage]) -> str:
    if not messages:
        return STATUS_PLACEHOLDER
    return str(sum(1 for message in messages if message.role == "user"))


def _latest_assistant_usage(messages: list[ChatMessage]) -> tuple[int, bool] | None:
    usage = _latest_assistant_usage_object(messages, require_input=True)
    if usage is None:
        return None
    input_tokens = _coerce_int(usage.get("input_tokens"))
    if input_tokens is None:
        return None
    return input_tokens, bool(usage.get("estimated"))


def _latest_assistant_usage_object(
    messages: list[ChatMessage],
    *,
    require_input: bool = False,
) -> dict[str, Any] | None:
    for message in reversed(messages):
        if message.role != "assistant" or not isinstance(message.usage, dict):
            continue
        if require_input and _coerce_int(message.usage.get("input_tokens")) is None:
            continue
        return message.usage
    return None


def _last_request_cache_text(messages: list[ChatMessage]) -> str:
    usage = _latest_assistant_usage_object(messages)
    if usage is None or usage.get("estimated") is True:
        return STATUS_PLACEHOLDER

    cache_data = _cache_data_from_usage(usage)
    if cache_data is None:
        return STATUS_PLACEHOLDER
    return _format_cache_data(cache_data)


def _session_cache_text(messages: list[ChatMessage]) -> str:
    totals = aggregate_session_usage(messages)
    cache_turns = _coerce_non_negative_int(totals.get("cache_turns")) or 0
    if cache_turns <= 0:
        return STATUS_PLACEHOLDER

    cache_input_tokens = 0
    for message in messages:
        if message.role != "assistant" or not isinstance(message.usage, dict):
            continue
        if message.usage.get("estimated") is True:
            continue
        cache_data = _cache_data_from_usage(message.usage)
        if cache_data is None:
            continue
        cache_input_tokens += cache_data[0]

    cache_data = (
        cache_input_tokens,
        _coerce_non_negative_int(totals.get("cache_read_tokens")) or 0,
        _coerce_non_negative_int(totals.get("cache_write_tokens")) or 0,
    )
    return f"{_format_cache_data(cache_data)}, turns {cache_turns}"


def _cache_data_from_usage(usage: dict[str, Any]) -> tuple[int, int, int] | None:
    if "cache_read_tokens" not in usage and "cache_write_tokens" not in usage:
        return None
    input_tokens = _coerce_non_negative_int(usage.get("input_tokens"))
    if input_tokens is None:
        return None
    return (
        input_tokens,
        _coerce_non_negative_int(usage.get("cache_read_tokens")) or 0,
        _coerce_non_negative_int(usage.get("cache_write_tokens")) or 0,
    )


def _format_cache_data(cache_data: tuple[int, int, int]) -> str:
    input_tokens, cache_read_tokens, cache_write_tokens = cache_data
    return (
        f"read {cache_read_tokens} / {input_tokens} "
        f"({_cache_hit_rate_text(cache_read_tokens, input_tokens)} hit), "
        f"write {cache_write_tokens}"
    )


def _cache_hit_rate_text(cache_read_tokens: int, input_tokens: int) -> str:
    if input_tokens <= 0:
        return STATUS_PLACEHOLDER
    hit_rate = cache_read_tokens / input_tokens * _CACHE_PERCENT_SCALE
    return f"{hit_rate:.{_CACHE_HIT_RATE_DECIMALS}f}%"


def _session_started_text(messages: list[ChatMessage], now_utc: datetime) -> str:
    if not messages:
        return STATUS_PLACEHOLDER

    parsed_timestamp = _parse_utc_timestamp(messages[0].timestamp)
    if parsed_timestamp is None:
        return STATUS_PLACEHOLDER

    local_started = parsed_timestamp.astimezone()
    age_text = _format_duration(now_utc - parsed_timestamp)
    return f"{local_started.strftime(_STATUS_TIME_FORMAT)} ({age_text} ago)"


def _app_uptime_text(started_at: datetime | None, now_utc: datetime) -> str:
    if started_at is None:
        return STATUS_PLACEHOLDER
    started_at_utc = _to_utc(started_at)
    return _format_duration(now_utc - started_at_utc)


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_utc_timestamp(value: str) -> datetime | None:
    normalized_value = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized_value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def _coerce_non_negative_int(value: object) -> int | None:
    coerced = _coerce_int(value)
    if coerced is None or coerced < 0:
        return None
    return coerced


def _has_exception_name(error: BaseException, expected_name: str) -> bool:
    return any(exception_type.__name__ == expected_name for exception_type in type(error).__mro__)


def _format_duration(delta: timedelta) -> str:
    total_seconds = max(0, int(delta.total_seconds()))
    days, remainder = divmod(total_seconds, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, seconds = divmod(remainder, 60)

    parts: list[str] = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or hours > 0 or days > 0:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)
