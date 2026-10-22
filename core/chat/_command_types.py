"""Command protocol records and argument interpretation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from core.chat.content_blocks import ContentBlock
from core.chat.messages import MessageSender, ReplySurface
from core.projects import AgentOverrides
from core.runs import (
    Run,
)
from core.sessions import AGENT_DEFAULT_PROJECT, WorkingProjectChoice

# Argument mode drives autocomplete: ``none`` commands run immediately on
# selection; ``optional``/``required`` insert the token and wait for text.
CommandArgumentMode = Literal["none", "optional", "required"]
CommandCatalogResult = Literal["notice", "detail", "state_change"]
CommandExecutionMode = Literal["immediate", "serialized"]
CommandFeedbackKind = Literal["notice", "detail"]
CommandNavigationKind = Literal[
    "continue_in_session", "offer_session", "open_extension_page", "new_session"
]
# How a command sent for a new Session (one the conversation has not created yet)
# runs: ``session_less`` runs without a Session; ``create_session`` creates the
# Session first and then runs as usual; ``needs_session`` is refused until a
# message has created the Session.
CommandNewSessionMode = Literal["session_less", "create_session", "needs_session"]
CommandRunRole = Literal["primary", "follow_up"]
CommandSurfaceKind = Literal["webui", "tui", "channel"]


@dataclass(frozen=True)
class CommandSpec:
    """Declarative metadata for one slash command.

    The spec stays surface-neutral. Accessors project ``catalog_result`` into
    their own presentation vocabulary and honor availability/mode without
    branching on the command name. ``new_session_mode`` decides how the
    command runs for a new Session (:meth:`CommandDispatcher.execute_for_new_session`).
    """

    name: str
    description: str
    argument: CommandArgumentMode
    catalog_result: CommandCatalogResult
    execution_mode: CommandExecutionMode
    argument_execution_mode: CommandExecutionMode | None = None
    unavailable_surfaces: frozenset[CommandSurfaceKind] = frozenset()
    new_session_mode: CommandNewSessionMode = "needs_session"
    # False refuses the command in group conversations of every surface.
    group_conversations: bool = True
    # The argument can carry a Secret value: accessors keep no copy of the command
    # text, and a Channel removes the user's message where the platform allows.
    secret_argument: bool = False
    # How to use the command, in plain words (`your goal, or pause, resume,
    # clear`), shown greyed after the typed command; the description says what
    # it does. Empty for a command without an argument.
    argument_hint: str = ""


@dataclass(frozen=True)
class PreparedCommand:
    """One recognized and parsed command, ready for execution."""

    name: str
    argument: str | None
    execution_mode: CommandExecutionMode
    registration_id: int | None = None
    # The command as the user wrote it, which a command-started Run shows as the
    # user's message; empty means ``/<name> [argument]``.
    text: str = ""
    secret_argument: bool = False


@dataclass(frozen=True)
class CommandUnavailability:
    """A Chat-owned surface restriction discovered before execution."""

    command: str
    surface: CommandSurfaceKind
    # ``group``: refused because the conversation is a group, not because of the surface.
    conversation: Literal["group"] | None = None

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
    """A neutral Session or registered Extension page destination.

    ``new_session`` names no Session: the surface opens a new conversation with
    the Agent, and its next message creates the Session.
    """

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
    # Who wrote the command in a Channel conversation.
    sender: MessageSender | None = None
    # The command as the user wrote it; ``execute`` supplies it from the prepared command.
    command_text: str = ""

    def report_change(self, change: CommandResourceChange) -> None:
        if self.on_change is not None:
            self.on_change(change)


@dataclass(frozen=True)
class NewSessionCommandContext:
    """Addressing of a command sent for a new Session, which does not exist yet.

    ``agent_overrides`` are the Agent overrides the new Session starts with and
    ``working_project_id`` the working Project a Session of an Identity Agent
    starts in (a Project id, ``None`` for the Workspace, or the Agent's default
    Project). An optional ``session_id`` names the Session to create when the
    Command needs a new one; Commands that run without a Session ignore it.
    """

    agent_id: str
    project_id: str | None
    reply_surface: ReplySurface
    agent_overrides: AgentOverrides = field(default_factory=AgentOverrides)
    working_project_id: WorkingProjectChoice = AGENT_DEFAULT_PROJECT
    on_change: CommandChangeObserver | None = None
    session_id: str | None = None

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


@dataclass(frozen=True)
class NewSessionCommandResult:
    """A command run for a new Session; ``session_id`` names the Session it created."""

    outcome: CommandOutcome
    session_id: str | None = None


CommandExecutionHandler = Callable[[CommandExecutionContext, str | None], Awaitable[CommandOutcome]]
SessionLessCommandHandler = Callable[
    [NewSessionCommandContext, str | None], Awaitable[CommandOutcome]
]
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
