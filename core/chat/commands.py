"""Slash command dispatch for chat entry points."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from core.runs import ChatRunManager, RunNotFoundError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.agents.agents import Agent, AgentStore
    from core.chat.chat import ChatMessage
    from core.models.models import ModelRegistry
    from core.sessions import ChatSessionManager
else:
    Agent = Any
    AgentStore = Any
    ChatMessage = Any
    ChatSessionManager = Any
    ModelRegistry = Any

CommandHandler = Callable[[str, str], "CommandHandled"]

_LOGGER = get_logger("chat.commands")

STATUS_PLACEHOLDER = "—"
_STATUS_TIME_FORMAT = "%Y-%m-%d %H:%M:%S %Z"
_STATUS_MODEL_DISPLAY_OVERRIDE: ContextVar[str | None] = ContextVar(
    "status_model_display_override",
    default=None,
)


@dataclass(frozen=True)
class CommandHandled:
    """Result indicating command dispatch handled the message."""

    reply: str | None


@dataclass(frozen=True)
class NotACommand:
    """Result indicating message should continue through normal chat flow."""


DispatchResult = CommandHandled | NotACommand


class CommandDispatcher:
    """Dispatches built-in slash commands before run startup."""

    BUILT_IN_COMMANDS: dict[str, str] = {
        "compact": "Compact the current session's context immediately.",
        "new": "Start a new session for the current agent.",
        "status": "Show current session and runtime status.",
        "stop": "Cancel the active run for this session.",
    }

    def __init__(
        self,
        chat_runs: ChatRunManager,
        agents: AgentStore | None = None,
        sessions: ChatSessionManager | None = None,
        models: ModelRegistry | None = None,
        started_at: datetime | None = None,
    ) -> None:
        self._chat_runs = chat_runs
        self._agents = agents
        self._sessions = sessions
        self._models = models
        self._started_at = started_at
        self._commands: dict[str, CommandHandler] = {
            "/new": self._handle_new,
            "/status": self._handle_status,
            "/stop": self._handle_stop,
        }

    def dispatch(self, agent_id: str, session_id: str, message_text: str) -> DispatchResult:
        """Dispatch one message as a built-in slash command when recognized."""
        normalized_text = message_text.strip().lower()
        handler = self._commands.get(normalized_text)
        if handler is None:
            return NotACommand()
        return handler(agent_id, session_id)

    def _handle_stop(self, agent_id: str, session_id: str) -> CommandHandled:
        try:
            self._chat_runs.cancel_by_session(agent_id, session_id)
        except RunNotFoundError:
            return CommandHandled(reply="No active run to cancel.")
        return CommandHandled(reply="Run cancelled.")

    def _handle_new(self, agent_id: str, session_id: str) -> CommandHandled:
        if self._sessions is None or self._agents is None:
            return CommandHandled(reply="Session management is not available.")

        try:
            active_run = self._chat_runs.active_run(agent_id=agent_id, session_id=session_id)
            if active_run is not None:
                return CommandHandled(
                    reply="A new session can be started after the current run finishes.",
                )

            self._agents.get(agent_id)
            new_session = self._sessions.create(agent_id)
            self._agents.update(agent_id, current_session_id=new_session.id)
            return CommandHandled(reply=f"New session started: {new_session.id}")
        except Exception:
            raise

    def _handle_status(self, agent_id: str, session_id: str) -> CommandHandled:
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

        context_window, model_display_name = resolve_status_model_details(agent, self._models)
        text = build_status_reply(
            agent,
            messages,
            context_window,
            self._started_at,
            model_display_name,
        )
        return CommandHandled(reply=text)


def resolve_status_model_details(
    agent: Agent | None,
    models: ModelRegistry | None,
) -> tuple[int | None, str | None]:
    """Resolve context window and display name for status output from the model registry."""
    if agent is None or models is None:
        return None, None

    provider_id, model_id = _parse_registry_model_key(agent.model)
    if provider_id is None or model_id is None:
        return None, None

    try:
        model = models.get(provider_id, model_id)
    except KeyError:
        _LOGGER.warning(
            "Model registry entry missing for %r/%r while building status",
            provider_id,
            model_id,
        )
        return None, None
    except Exception:
        _LOGGER.error(
            "Failed model registry lookup for %r/%r while building status",
            provider_id,
            model_id,
            exc_info=True,
        )
        return None, None

    return model.context_window, model.name


def build_status_reply(
    agent: Agent | None,
    messages: list[ChatMessage],
    context_window: int | None,
    started_at: datetime | None,
    model_display_name: str | None,
) -> str:
    """Build status text while applying an optional model-display override."""
    token = _STATUS_MODEL_DISPLAY_OVERRIDE.set(model_display_name)
    try:
        return build_status_text(agent, messages, context_window, started_at)
    finally:
        _STATUS_MODEL_DISPLAY_OVERRIDE.reset(token)


def build_status_text(
    agent: Agent | None,
    messages: list[ChatMessage],
    context_window: int | None,
    started_at: datetime | None,
) -> str:
    """Build human-readable status text for the current session and runtime state."""
    now_utc = datetime.now(UTC)
    now_local = now_utc.astimezone()

    if agent is None:
        agent_summary = STATUS_PLACEHOLDER
        model_display = STATUS_PLACEHOLDER
        fallback_model = STATUS_PLACEHOLDER
        thinking_effort = STATUS_PLACEHOLDER
        temperature = STATUS_PLACEHOLDER
    else:
        model_string = agent.model.strip() or STATUS_PLACEHOLDER
        agent_summary = f"{agent.name} ({model_string})"
        model_display = _STATUS_MODEL_DISPLAY_OVERRIDE.get() or _model_display_name(model_string)
        fallback_model = agent.fallback_model.strip() or STATUS_PLACEHOLDER
        thinking_effort = _thinking_effort_text(agent.thinking_effort)
        temperature = _temperature_text(agent.temperature)

    context_usage = _context_usage_text(messages, context_window)
    session_started = _session_started_text(messages, now_utc)
    turn_count = _turn_count_text(messages)
    app_uptime = _app_uptime_text(started_at, now_utc)

    lines = [
        f"Agent: {agent_summary}",
        f"Model display name: {model_display}",
        f"Fallback model: {fallback_model}",
        f"Thinking effort: {thinking_effort}",
        f"Temperature: {temperature}",
        f"Context usage: {context_usage}",
        f"Session started: {session_started}",
        f"Turn count: {turn_count}",
        f"App uptime: {app_uptime}",
        f"Current time: {now_local.strftime(_STATUS_TIME_FORMAT)}",
    ]
    return "\n".join(lines)


def _model_display_name(model_string: str) -> str:
    _, model_id = _parse_registry_model_key(model_string)
    if model_id is None:
        return STATUS_PLACEHOLDER
    return model_id


def _thinking_effort_text(value: str | None) -> str:
    if value is None:
        return "default"
    return value.strip() or "default"


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
