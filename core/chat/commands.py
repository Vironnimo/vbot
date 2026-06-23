"""Slash command dispatch for chat entry points."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from core.projects import format_agent_address
from core.providers.providers import resolve_context_window
from core.providers.reasoning import (
    REASONING_INTENT_BUDGET,
    REASONING_INTENT_DEFAULT,
    REASONING_INTENT_OFF,
    REASONING_INTENT_ON,
    resolve_reasoning_intent,
)
from core.runs import ChatRunManager, RunNotFoundError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.agents import AgentStore
    from core.chat.chat import ChatMessage
    from core.models.models import ModelRegistry
    from core.projects import AgentResolver, ProjectStore, RuntimeAgent
    from core.providers.providers import ProviderRegistry
    from core.sessions import ChatSessionManager
else:
    AgentResolver = Any
    AgentStore = Any
    ChatMessage = Any
    ChatSessionManager = Any
    ModelRegistry = Any
    ProjectStore = Any
    ProviderRegistry = Any
    RuntimeAgent = Any

CommandActionName = Literal[
    "compact",
    "handoff",
    "move_session",
    "new_session",
    "rename_session",
    "retry_last_turn",
    "set_model",
]
StatusActivityName = Literal["idle", "running"]

# Argument mode drives the autocomplete trigger behavior: ``none`` commands run
# immediately on selection; ``optional``/``required`` insert the token and wait
# for text. ``required`` is unused today but kept deliberately for future
# commands. Output channel drives how the accessor presents a handled command:
# ``toast`` (transient confirmation), ``transient`` (a non-persisted chat card),
# or ``action`` (a state change such as a session switch or a re-run).
CommandArgumentMode = Literal["none", "optional", "required"]
CommandOutputChannel = Literal["toast", "transient", "action"]

CommandHandler = Callable[[str, str, "str | None", "str | None"], "CommandHandled | CommandAction"]

_LOGGER = get_logger("chat.commands")

STATUS_PLACEHOLDER = "—"
# Reported "actual" reasoning state for a model steered by a thinking toggle or a
# token budget rather than an effort ladder: there is no effort level to show, so
# ``/status`` reports whether reasoning is on or off for the selection.
REASONING_STATE_ON = "on"
REASONING_STATE_OFF = "off"
_STATUS_TIME_FORMAT = "%Y-%m-%d %H:%M:%S %Z"
_STATUS_MODEL_DISPLAY_OVERRIDE: ContextVar[str | None] = ContextVar(
    "status_model_display_override",
    default=None,
)


@dataclass(frozen=True)
class CommandSpec:
    """Declarative metadata for a built-in slash command.

    ``argument`` and ``output`` replace per-command special cases: trigger and
    presentation behavior are derived from these two attributes instead of
    being hardcoded at each call site.
    """

    name: str
    description: str
    argument: CommandArgumentMode
    output: CommandOutputChannel


@dataclass(frozen=True)
class CommandHandled:
    """Result indicating command dispatch handled the message."""

    reply: str | None
    data: dict[str, object] | None = None
    output: CommandOutputChannel | None = None


@dataclass(frozen=True)
class CommandAction:
    """Result indicating a recognized command needs accessor-level execution."""

    name: CommandActionName
    # Optional command argument, kept as the raw text after the command token so
    # the dataclass stays reusable across argument-bearing commands. ``compact``
    # passes its free-text instruction through verbatim; ``handoff`` carries the
    # ``agent:<id>``-prefixed grammar that ``parse_handoff_argument`` interprets.
    argument: str | None = None


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


@dataclass(frozen=True)
class StatusActivity:
    """Run activity summary for one Session."""

    activity: StatusActivityName
    run_id: str | None
    created_at: str | None
    updated_at: str | None


@dataclass(frozen=True)
class NotACommand:
    """Result indicating message should continue through normal chat flow."""


DispatchResult = CommandHandled | CommandAction | NotACommand


class CommandDispatcher:
    """Dispatches built-in slash commands before run startup."""

    BUILT_IN_COMMANDS: dict[str, CommandSpec] = {
        "agent": CommandSpec(
            "agent",
            "Move this session to another agent; no argument lists the directory.",
            argument="optional",
            output="action",
        ),
        "compact": CommandSpec(
            "compact",
            "Compact the current session's context immediately.",
            argument="optional",
            output="toast",
        ),
        "handoff": CommandSpec(
            "handoff",
            "Write a handoff and start a new session (optionally for another agent).",
            argument="optional",
            output="action",
        ),
        "help": CommandSpec(
            "help",
            "Show available built-in slash commands.",
            argument="none",
            output="transient",
        ),
        "model": CommandSpec(
            "model",
            "Show, set, or reset this session's model (/model reset to clear).",
            argument="optional",
            output="action",
        ),
        "new": CommandSpec(
            "new",
            "Start a new session for the current agent.",
            argument="none",
            output="action",
        ),
        "rename": CommandSpec(
            "rename",
            "Rename this session; no argument clears the name.",
            argument="optional",
            output="toast",
        ),
        "retry": CommandSpec(
            "retry",
            "Retry the last user turn in this session.",
            argument="none",
            output="action",
        ),
        "status": CommandSpec(
            "status",
            "Show current session and runtime status.",
            argument="none",
            output="transient",
        ),
        "stop": CommandSpec(
            "stop",
            "Cancel the active run for this session.",
            argument="none",
            output="toast",
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
    ) -> None:
        self._chat_runs = chat_runs
        self._agent_resolver = agent_resolver
        self._sessions = sessions
        self._models = models
        self._started_at = started_at
        self._providers = providers
        self._projects = projects
        self._agents = agents
        self._commands: dict[str, CommandHandler] = {
            "agent": self._handle_agent,
            "compact": self._handle_compact,
            "handoff": self._handle_handoff,
            "help": self._handle_help,
            "model": self._handle_model,
            "new": self._handle_new,
            "rename": self._handle_rename,
            "retry": self._handle_retry,
            "status": self._handle_status,
            "stop": self._handle_stop,
        }

    def dispatch(
        self,
        agent_id: str,
        session_id: str,
        message_text: str,
        project_id: str | None = None,
    ) -> DispatchResult:
        """Dispatch one message as a built-in slash command when recognized.

        ``project_id`` is the session's project (``None`` for an identity session).
        It flows to the handlers so ``/status`` resolves a project agent through
        the same seam the run path uses, instead of degrading to an empty reply.
        """
        matched = self._match_command(message_text)
        if matched is None:
            return NotACommand()
        spec, argument = matched
        return self._commands[spec.name](agent_id, session_id, argument, project_id)

    def recognizes(self, message_text: str) -> bool:
        """Return whether dispatching this message would handle it as a command.

        Lets accessors gate command authorization before ``dispatch()`` runs handler
        side effects (e.g. ``/stop`` cancelling a Run).
        """
        return self._match_command(message_text) is not None

    def _match_command(self, message_text: str) -> tuple[CommandSpec, str | None] | None:
        """Resolve a message to a command spec and its parsed argument.

        ``none`` commands match only when nothing trails the token, so text after
        a no-argument command falls through as a normal message. ``optional`` and
        ``required`` commands take the entire remainder after the first token as
        their argument (single source of truth for both ``dispatch`` and
        ``recognizes``).
        """
        stripped_text = message_text.strip()
        if not stripped_text.startswith("/"):
            return None
        first_token, _, remainder = stripped_text.partition(" ")
        name = first_token[1:].lower()
        spec = self.BUILT_IN_COMMANDS.get(name)
        if spec is None:
            return None
        argument = remainder.strip()
        if spec.argument == "none":
            if argument:
                return None
            return spec, None
        return spec, (argument or None)

    def _handle_compact(
        self, agent_id: str, session_id: str, argument: str | None, project_id: str | None
    ) -> CommandAction:
        return CommandAction(name="compact", argument=argument)

    def _handle_handoff(
        self, agent_id: str, session_id: str, argument: str | None, project_id: str | None
    ) -> CommandAction:
        return CommandAction(name="handoff", argument=argument)

    def _handle_agent(
        self, agent_id: str, session_id: str, argument: str | None, project_id: str | None
    ) -> CommandHandled | CommandAction:
        """Argument-dependent: no argument lists the directory, otherwise move.

        ``/agent`` (no argument) returns a transient, non-persisted directory card
        — a real choice with no side effect. ``/agent <addr> [task]`` returns a
        ``move_session`` action carrying the raw argument; the orchestrator owns
        the parse, the guards, and the relocation, so the command layer stays a
        thin trigger. This makes ``/agent`` the first built-in with an
        argument-dependent output channel (transient card vs. action).
        """
        if argument is None:
            return CommandHandled(reply=self._build_agent_directory(), output="transient")
        return CommandAction(name="move_session", argument=argument)

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

    def _handle_model(
        self, agent_id: str, session_id: str, argument: str | None, project_id: str | None
    ) -> CommandHandled | CommandAction:
        """Argument-dependent: no argument shows the current model, otherwise set it.

        Bare ``/model`` returns a transient card naming the session's current model
        and where it comes from (a per-agent local override, the project
        configuration, or the agent's own configuration) — a read with no side
        effect. ``/model <value>`` and ``/model reset`` return a ``set_model``
        action carrying the raw text; the accessor layer owns validation,
        identity-vs-project routing, and the persistent write, so the command stays
        a thin trigger (mirroring ``/agent``).
        """
        if argument is None:
            return CommandHandled(
                reply=self._build_model_summary(agent_id, project_id),
                output="transient",
            )
        return CommandAction(name="set_model", argument=argument)

    def _build_model_summary(self, agent_id: str, project_id: str | None) -> str:
        """Describe the session's current model and where it resolves from.

        Cheap, fresh reads only (no model-chain replication): the resolved model is
        what the next run would use (already post-override), and the origin is read
        straight off the project's override map. None-guarded like ``/status`` so a
        minimally constructed dispatcher degrades to a placeholder instead of
        crashing.
        """
        model = STATUS_PLACEHOLDER
        if self._agent_resolver is not None:
            try:
                model = self._agent_resolver.resolve_agent(project_id, agent_id).model.strip()
                model = model or STATUS_PLACEHOLDER
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
        return f"Current model: {model}\nSource: {self._model_origin(agent_id, project_id)}"

    def _model_origin(self, agent_id: str, project_id: str | None) -> str:
        """Return where the session's current model comes from, in plain English.

        Identity session (``project_id is None``) → the agent's own configuration.
        Project session → a per-agent local override when one is pinned (the top
        model-chain tier), otherwise the project configuration (the repo-declared
        model or a project/global default). None-guarded; an unreadable project
        degrades to the project-configuration label.
        """
        if project_id is None:
            return "agent configuration"
        if self._projects is not None:
            try:
                if self._projects.get(project_id).model_overrides.get(agent_id):
                    return "local override"
            except Exception:
                _LOGGER.warning(
                    "Failed to load project %r while building /model reply",
                    project_id,
                    exc_info=True,
                )
        return "project configuration"

    def _handle_help(
        self, agent_id: str, session_id: str, argument: str | None, project_id: str | None
    ) -> CommandHandled:
        lines = ["Built-in slash commands:"]
        lines.extend(
            f"/{spec.name} - {spec.description}"
            for spec in sorted(self.BUILT_IN_COMMANDS.values(), key=lambda spec: spec.name)
        )
        lines.extend(
            [
                "",
                "Skill shortcuts also start with slash names. "
                "Use $skill-name to force a skill without sending a slash command.",
            ]
        )
        return CommandHandled(reply="\n".join(lines), output="transient")

    def _handle_stop(
        self, agent_id: str, session_id: str, argument: str | None, project_id: str | None
    ) -> CommandHandled:
        try:
            self._chat_runs.cancel_by_session(agent_id, session_id)
        except RunNotFoundError:
            return CommandHandled(reply="No active run to cancel.", output="toast")
        return CommandHandled(reply="Run cancelled.", output="toast")

    def _handle_new(
        self, agent_id: str, session_id: str, argument: str | None, project_id: str | None
    ) -> CommandAction:
        return CommandAction(name="new_session")

    def _handle_rename(
        self, agent_id: str, session_id: str, argument: str | None, project_id: str | None
    ) -> CommandAction:
        """Set or clear this session's title; the accessor owns the write.

        Like ``/model``, a thin trigger: the raw argument (``None`` clears the
        title) travels to the server handler, which writes through the single
        titling seam and emits the session-list refresh. Keeping it an action,
        not a direct ``CommandHandled``, is what lets the rename publish that
        event — the dispatcher itself cannot.
        """
        return CommandAction(name="rename_session", argument=argument)

    def _handle_retry(
        self, agent_id: str, session_id: str, argument: str | None, project_id: str | None
    ) -> CommandAction:
        return CommandAction(name="retry_last_turn")

    def _handle_status(
        self, agent_id: str, session_id: str, argument: str | None, project_id: str | None
    ) -> CommandHandled:
        agent: RuntimeAgent | None = None
        messages: list[ChatMessage] = []

        try:
            if self._agent_resolver is not None:
                agent = self._agent_resolver.resolve_agent(project_id, agent_id)
        except Exception as error:
            log = (
                _LOGGER.warning
                if _has_exception_name(error, "AgentResolutionError")
                else _LOGGER.error
            )
            log(
                "Failed to resolve agent %r while building /status reply",
                agent_id,
                exc_info=True,
            )
            agent = None

        try:
            if self._sessions is not None:
                messages = self._sessions.get(agent_id, session_id, project_id).load()
        except Exception as error:
            log = (
                _LOGGER.warning if _has_exception_name(error, "ChatSessionError") else _LOGGER.error
            )
            log(
                "Failed to load session %r for agent %r while building /status reply",
                session_id,
                agent_id,
                exc_info=True,
            )
            messages = []

        model_details = resolve_status_model_details(agent, self._models, self._providers)
        activity = resolve_status_activity(self._chat_runs, agent_id, session_id)
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
            project_label=resolve_status_project_label(self._projects, project_id),
        )
        return CommandHandled(reply=text, output="transient")


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
) -> StatusModelDetails:
    """Resolve model facts for status output from the model registry.

    Returns context window, display name, and the effective reasoning-effort
    ladder. A missing agent/registry/model yields empty details so status
    rendering degrades to placeholders instead of failing.

    ``context_window`` is the *resolved* window through the read-side default
    chain (model window → provider-config default → global floor, see
    :func:`resolve_context_window`), so ``/status`` reports the budget compaction
    actually uses rather than ``unknown`` for a window-less model. It stays
    ``None`` only when no model could be resolved at all.
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
        context_window=resolve_context_window(
            model.context_window,
            _status_provider_config(providers, provider_id),
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
) -> StatusActivity:
    """Return running/idle activity for one Session."""
    run = chat_runs.active_run(agent_id=agent_id, session_id=session_id)
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
    for message in reversed(messages):
        if message.role != "assistant" or not isinstance(message.usage, dict):
            continue
        input_tokens = _coerce_int(message.usage.get("input_tokens"))
        if input_tokens is None:
            continue
        return input_tokens, bool(message.usage.get("estimated"))
    return None


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
