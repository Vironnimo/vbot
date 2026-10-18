"""Chat input preparation and generic command outcome projection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from core.chat import (
    CommandDispatcher,
    CommandExecutionContext,
    CommandOutcome,
    CommandResourceChange,
    NewSessionCommandContext,
    PreparedCommand,
    ReplySurface,
)
from core.chat.content_blocks import ContentBlock
from core.chat.file_mentions import expand_file_mentions, resolve_mention_root
from core.projects import AgentOverrides, format_agent_address
from core.runs import Run
from core.sessions import (
    AGENT_DEFAULT_PROJECT,
    SESSION_ID_PATTERN,
    SessionAddress,
    WorkingProjectChoice,
)
from core.utils.logging import get_logger
from server.events import RESOURCE_KIND_AGENTS
from server.rpc._chat_reads import _CHAT_RPC_WORKERS
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import (
    RPC_ERROR_INVALID_REQUEST,
    RpcError,
)
from server.rpc.event_bridge import (
    publish_resource_changed,
)
from server.rpc.validation import (
    _optional_agent_overrides,
    _optional_string,
    _optional_working_project,
    _reject_unsupported,
)

JsonObject = dict[str, Any]

_LOGGER = get_logger("server.rpc.chat")


def _command_output(outcome: CommandOutcome) -> str:
    if outcome.navigation is not None:
        return "action"
    if outcome.feedback is not None and outcome.feedback.kind == "detail":
        return "transient"
    return "toast"


def _command_change_key(change: CommandResourceChange) -> tuple[str, tuple[tuple[str, str], ...]]:
    return change.kind, tuple(sorted(change.scope.items()))


def _publish_command_change(state: Any, change: CommandResourceChange) -> None:
    publish_resource_changed(state, change.kind, scope=dict(change.scope) or None)


def _command_outcome_response(outcome: CommandOutcome, session_id: str | None) -> JsonObject:
    """Project a command outcome; ``session_id`` is the Session the command ran in.

    A command sent for a new Session runs in none unless it created one, and
    its response then carries no ``session_id``.
    """
    response: JsonObject = {
        "command_handled": True,
        "reply": outcome.feedback.text if outcome.feedback is not None else "",
        "output": _command_output(outcome),
    }
    if session_id is not None:
        response["session_id"] = session_id
    data: JsonObject = {"command": outcome.command, **dict(outcome.facts)}
    if outcome.navigation is not None:
        navigation = outcome.navigation
        if navigation.kind == "open_extension_page":
            data["navigation"] = {
                "kind": navigation.kind,
                "extension": navigation.extension,
                "page": navigation.page,
                "route": navigation.route,
            }
        elif navigation.kind == "new_session":
            data["navigation"] = {
                "kind": navigation.kind,
                "agent_id": format_agent_address(navigation.agent_id, navigation.project_id),
            }
        else:
            data.setdefault("session_id", navigation.session_id)
            data.setdefault(
                "agent_id",
                format_agent_address(navigation.agent_id, navigation.project_id),
            )
    if len(data) > 1:
        response["data"] = data
    return response


def _primary_command_run(outcome: CommandOutcome) -> Run | None:
    primary_runs = [
        command_run.run for command_run in outcome.runs if command_run.role == "primary"
    ]
    if len(primary_runs) > 1:
        raise ValueError("A command outcome may expose at most one primary Run")
    return primary_runs[0] if primary_runs else None


def _command_change_publisher(state: Any) -> Callable[[CommandResourceChange], None]:
    """Publish each distinct resource change of one command once."""
    emitted_changes: set[tuple[str, tuple[tuple[str, str], ...]]] = set()

    def on_change(change: CommandResourceChange) -> None:
        key = _command_change_key(change)
        if key in emitted_changes:
            return
        emitted_changes.add(key)
        _publish_command_change(state, change)

    return on_change


async def _execute_chat_command(
    state: Any,
    address: SessionAddress,
    prepared: PreparedCommand,
    *,
    reply_surface: ReplySurface,
) -> Run | JsonObject:
    dispatcher: CommandDispatcher = state.command_dispatcher
    on_change = _command_change_publisher(state)
    try:
        outcome = await dispatcher.execute(
            prepared,
            CommandExecutionContext(
                agent_id=address.agent_id,
                session_id=address.session_id,
                project_id=address.project_id,
                reply_surface=reply_surface,
                on_change=on_change,
            ),
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    for change in outcome.resource_changes:
        on_change(change)
    primary_run = _primary_command_run(outcome)
    if primary_run is not None:
        return primary_run
    return _command_outcome_response(outcome, address.session_id)


@dataclass(frozen=True)
class _NewSessionTarget:
    """A chat submission's new Session: its Agent overrides and working Project."""

    agent_overrides: AgentOverrides
    working_project_id: WorkingProjectChoice = AGENT_DEFAULT_PROJECT
    session_id: str | None = None


async def _execute_new_session_chat_command(
    state: Any,
    agent_id: str,
    prepared: PreparedCommand,
    target: _NewSessionTarget,
    *,
    project_id: str | None,
    reply_surface: ReplySurface,
) -> Run | JsonObject:
    """Run a command sent for a new Session; the command decides whether it creates one."""
    on_change = _command_change_publisher(state)
    dispatcher: CommandDispatcher = state.command_dispatcher
    try:
        result = await dispatcher.execute_for_new_session(
            prepared,
            NewSessionCommandContext(
                agent_id=agent_id,
                project_id=project_id,
                reply_surface=reply_surface,
                agent_overrides=target.agent_overrides,
                working_project_id=target.working_project_id,
                on_change=on_change,
                session_id=target.session_id,
            ),
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    for change in result.outcome.resource_changes:
        on_change(change)
    if result.session_id is not None and project_id is None:
        await _mark_current_session(state, SessionAddress(project_id, agent_id, result.session_id))
    primary_run = _primary_command_run(result.outcome)
    if primary_run is not None:
        return primary_run
    return _command_outcome_response(result.outcome, result.session_id)


async def _expand_content_file_mentions(
    state: Any,
    address: SessionAddress,
    content: str | list[ContentBlock],
    file_mentions: list[str],
    *,
    new_session_project: WorkingProjectChoice = AGENT_DEFAULT_PROJECT,
) -> str | list[ContentBlock]:
    """Snapshot ``@``-mentioned files into the outgoing content, if any.

    Runs before Run start *and* before busy-session enqueue, so a queued message
    carries the files as they were when the user hit send. The files resolve in
    the Session's working Project; for a Session the submission creates, in
    *new_session_project*. The root resolution reads the Agent (whose
    current-Session pointer it verifies) on the Session database's pool; the
    file I/O runs on the Chat RPC pool.
    """
    if not file_mentions:
        return content
    runtime = state.runtime
    try:
        root = await runtime.chat_sessions.run_async(
            lambda: resolve_mention_root(
                runtime,
                address.agent_id,
                address.project_id,
                session_id=address.session_id,
                working_project_id=new_session_project,
            )
        )
        return await _CHAT_RPC_WORKERS.run(
            expand_file_mentions,
            content,
            file_mentions,
            root=root,
            session_id=address.session_id,
            file_state=runtime.file_read_state,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _mark_current_session(state: Any, address: SessionAddress) -> None:
    """Re-aim an identity agent's current-session pointer to the session the
    user just wrote to.

    Best-effort: the pointer is a convenience for re-opening the last active
    session after a restart, so a failure must never block the user's message.
    The agent store is the single writer; other windows learn about the new
    current marking through the agents channel.

    Skipped when the session is already current: re-marking the same session on
    every message would emit a redundant ``resource_changed(kind="agents")``
    signal that tears down the chat view in every connected window.
    """
    runtime = state.runtime
    agents = runtime.agents
    # Agent reads verify, and updates validate, the pointer against Sessions.
    chat_sessions = runtime.chat_sessions
    try:
        agent = await chat_sessions.run_async(agents.get, address.agent_id)
    except Exception as exc:
        _LOGGER.warning(
            "Failed to read agent for current-session mark (agent=%s): %s",
            address.agent_id,
            exc,
        )
        return
    if agent.current_session_id == address.session_id:
        return
    try:
        await chat_sessions.run_async(
            agents.update, address.agent_id, current_session_id=address.session_id
        )
    except Exception as exc:
        _LOGGER.warning(
            "Failed to mark current session (agent=%s session=%s): %s",
            address.agent_id,
            address.session_id,
            exc,
        )
    else:
        publish_resource_changed(state, RESOURCE_KIND_AGENTS)
        _LOGGER.debug(
            "Current session marked (agent=%s session=%s)",
            address.agent_id,
            address.session_id,
        )


def _chat_target(
    params: JsonObject, agent_id: str, project_id: str | None
) -> SessionAddress | _NewSessionTarget:
    """Read where a chat submission goes: exactly one of two fields.

    ``session_id`` names an existing Session, returned as its full address. ``new_session``
    is an object asking for a new Session that the submission creates; its
    optional ``session_id`` supplies a new, unused address. Its
    ``agent_overrides`` and ``working_project_id`` (validated like
    ``session.create``'s) are what the new Session starts with.
    """
    session_id = _optional_string(params, "session_id")
    new_session = params.get("new_session")
    if (session_id is None) == (new_session is None):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params must name exactly one of session_id (an existing Session) "
            "and new_session (a new Session)",
        )
    if session_id is not None:
        return SessionAddress(project_id, agent_id, session_id)
    if not isinstance(new_session, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.new_session must be an object")
    _reject_unsupported(
        new_session, {"session_id", "agent_overrides", "working_project_id"}, "params.new_session"
    )
    requested_id = new_session.get("session_id")
    if "session_id" in new_session and (
        not isinstance(requested_id, str) or SESSION_ID_PATTERN.fullmatch(requested_id) is None
    ):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.new_session.session_id must be 1-128 ASCII letters, digits, hyphen, "
            "or underscore, starting with a letter or digit",
        )
    overrides = _optional_agent_overrides(
        new_session, allow_clear=False, label="params.new_session"
    )
    return _NewSessionTarget(
        AgentOverrides(**overrides) if overrides else AgentOverrides(),
        _optional_working_project(new_session, project_id, label="params.new_session"),
        requested_id,
    )
