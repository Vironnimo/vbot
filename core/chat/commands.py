"""Slash command dispatch for chat entry points."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from core.models.models import REASONING_CONTROL_BUDGET, REASONING_CONTROL_ON_OFF
from core.providers.providers import resolve_context_window
from core.providers.reasoning import closest_supported_effort, normalize_thinking_effort
from core.runs import ChatRunManager, RunNotFoundError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.agents.agents import Agent, AgentStore
    from core.chat.chat import ChatMessage
    from core.models.models import ModelRegistry
    from core.providers.providers import ProviderRegistry
    from core.sessions import ChatSessionManager
else:
    Agent = Any
    AgentStore = Any
    ChatMessage = Any
    ChatSessionManager = Any
    ModelRegistry = Any
    ProviderRegistry = Any

CommandActionName = Literal["compact", "handoff", "new_session", "retry_last_turn"]
StatusActivityName = Literal["idle", "running"]

# Argument mode drives the autocomplete trigger behavior: ``none`` commands run
# immediately on selection; ``optional``/``required`` insert the token and wait
# for text. ``required`` is unused today but kept deliberately for future
# commands. Output channel drives how the accessor presents a handled command:
# ``toast`` (transient confirmation), ``transient`` (a non-persisted chat card),
# or ``action`` (a state change such as a session switch or a re-run).
CommandArgumentMode = Literal["none", "optional", "required"]
CommandOutputChannel = Literal["toast", "transient", "action"]

CommandHandler = Callable[[str, str, "str | None"], "CommandHandled | CommandAction"]

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
        "new": CommandSpec(
            "new",
            "Start a new session for the current agent.",
            argument="none",
            output="action",
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
        agents: AgentStore | None = None,
        sessions: ChatSessionManager | None = None,
        models: ModelRegistry | None = None,
        started_at: datetime | None = None,
        providers: ProviderRegistry | None = None,
    ) -> None:
        self._chat_runs = chat_runs
        self._agents = agents
        self._sessions = sessions
        self._models = models
        self._started_at = started_at
        self._providers = providers
        self._commands: dict[str, CommandHandler] = {
            "compact": self._handle_compact,
            "handoff": self._handle_handoff,
            "help": self._handle_help,
            "new": self._handle_new,
            "retry": self._handle_retry,
            "status": self._handle_status,
            "stop": self._handle_stop,
        }

    def dispatch(self, agent_id: str, session_id: str, message_text: str) -> DispatchResult:
        """Dispatch one message as a built-in slash command when recognized."""
        matched = self._match_command(message_text)
        if matched is None:
            return NotACommand()
        spec, argument = matched
        return self._commands[spec.name](agent_id, session_id, argument)

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
        self, agent_id: str, session_id: str, argument: str | None
    ) -> CommandAction:
        return CommandAction(name="compact", argument=argument)

    def _handle_handoff(
        self, agent_id: str, session_id: str, argument: str | None
    ) -> CommandAction:
        return CommandAction(name="handoff", argument=argument)

    def _handle_help(self, agent_id: str, session_id: str, argument: str | None) -> CommandHandled:
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

    def _handle_stop(self, agent_id: str, session_id: str, argument: str | None) -> CommandHandled:
        try:
            self._chat_runs.cancel_by_session(agent_id, session_id)
        except RunNotFoundError:
            return CommandHandled(reply="No active run to cancel.", output="toast")
        return CommandHandled(reply="Run cancelled.", output="toast")

    def _handle_new(self, agent_id: str, session_id: str, argument: str | None) -> CommandAction:
        return CommandAction(name="new_session")

    def _handle_retry(self, agent_id: str, session_id: str, argument: str | None) -> CommandAction:
        return CommandAction(name="retry_last_turn")

    def _handle_status(
        self, agent_id: str, session_id: str, argument: str | None
    ) -> CommandHandled:
        agent: Agent | None = None
        messages: list[ChatMessage] = []

        try:
            if self._agents is not None:
                agent = self._agents.get(agent_id)
        except Exception as error:
            log = (
                _LOGGER.warning
                if _has_exception_name(error, "AgentNotFoundError")
                else _LOGGER.error
            )
            log(
                "Failed to load agent %r while building /status reply",
                agent_id,
                exc_info=True,
            )
            agent = None

        try:
            if self._sessions is not None:
                messages = self._sessions.get(agent_id, session_id).load()
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
            ),
        )
        return CommandHandled(reply=text, output="transient")


@dataclass(frozen=True)
class StatusModelDetails:
    """Model facts needed to render a status reply.

    ``reasoning_levels`` is the model's effective effort ladder (empty when the
    model has no feed ladder) and ``reasoning_control`` its wire control kind
    (``levels`` / ``on_off`` / ``budget`` / ``None``); together they let
    ``resolve_actual_thinking_effort`` report the *actual* reasoning sent on the
    wire — a snapped effort for a ladder, or on/off for a toggle/budget model.
    """

    context_window: int | None
    display_name: str | None
    reasoning_levels: tuple[str, ...] = ()
    reasoning_control: str | None = None


def resolve_status_model_details(
    agent: Agent | None,
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
    )


def _status_provider_config(providers: ProviderRegistry | None, provider_id: str) -> Any:
    """Return the ProviderConfig for the read-side window default, or None."""
    if providers is None:
        return None
    try:
        return providers.get(provider_id)
    except (KeyError, AttributeError):
        return None


def resolve_actual_thinking_effort(
    selected_effort: str | None,
    reasoning_levels: tuple[str, ...],
    reasoning_control: str | None = None,
) -> str | None:
    """Return the reasoning actually sent on the wire for the selected effort.

    * ``levels`` control (any model exposing a non-empty effort ladder): the
      selection is snapped with ``closest_supported_effort`` — the same pure
      snapping the adapters apply — so ``/status`` reports the effort that really
      reaches the provider.
    * ``on_off`` / ``budget`` control: there is no effort ladder, only a thinking
      toggle or token budget, so report the resulting state — ``"off"`` for a
      ``none`` selection, ``"on"`` for any other effort (vBot requests reasoning
      for the turn).
    * Otherwise ``None`` (unknown / not applicable): no effort selected (provider
      default), or a model with neither a ladder nor a known control to report
      (the adapter then applies its own floor, which is not visible here).
    """
    effort = normalize_thinking_effort(selected_effort)
    if not effort:
        return None
    if reasoning_control in (REASONING_CONTROL_ON_OFF, REASONING_CONTROL_BUDGET):
        return REASONING_STATE_OFF if effort == "none" else REASONING_STATE_ON
    if not reasoning_levels:
        return None
    return closest_supported_effort(effort, reasoning_levels)


def build_status_reply(
    agent: Agent | None,
    messages: list[ChatMessage],
    context_window: int | None,
    started_at: datetime | None,
    model_display_name: str | None,
    activity: StatusActivity | None = None,
    actual_thinking_effort: str | None = None,
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
        )
    finally:
        _STATUS_MODEL_DISPLAY_OVERRIDE.reset(token)


def build_status_text(
    agent: Agent | None,
    messages: list[ChatMessage],
    context_window: int | None,
    started_at: datetime | None,
    activity: StatusActivity | None = None,
    actual_thinking_effort: str | None = None,
) -> str:
    """Build human-readable status text for the current session and runtime state.

    ``actual_thinking_effort`` is what reaches the wire after the model's ladder
    snaps the agent's selection (see :func:`resolve_actual_thinking_effort`); it
    is rendered alongside the selected effort so the two can differ visibly.
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
