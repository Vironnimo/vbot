"""RPC dispatcher and transport-only delegates for server commands."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter  # type: ignore[import-untyped]

from core.agents import AgentError
from core.channels import ChannelConfig, ChannelConfigError, ChannelNotFoundError
from core.chat import (
    ChatError,
    ChatLoop,
    ChatMessage,
    ChatSessionError,
    CommandDispatcher,
    CommandHandled,
)
from core.chat.chat import (
    _close_adapter,
    _display_content_preview,
    _ensure_provider_exists,
    _resolve_agent_connection,
    _split_agent_model,
    parse_bare_model,
    parse_model_with_connection,
)
from core.chat.content_blocks import (
    ContentBlock,
    ContentBlockError,
    TextBlock,
    content_block_from_dict,
)
from core.compaction import CompactionSettings
from core.models.discovery import refresh_models
from core.models.models import ModelRegistry
from core.prompts import PromptError, PromptFragmentManager
from core.providers.auth_flow import DeviceFlowEngine
from core.providers.token_getter import OAuthTokenGetter
from core.runs import (
    ASSISTANT_OUTPUT_DELTA_EVENT,
    ASSISTANT_OUTPUT_EVENT,
    ERROR_MESSAGE_PERSISTED_EVENT,
    MODEL_FALLBACK_ACTIVATED_EVENT,
    REASONING_DELTA_EVENT,
    REASONING_EVENT,
    RUN_CANCELLED_EVENT,
    RUN_COMPLETED_EVENT,
    RUN_FAILED_EVENT,
    RUN_STARTED_EVENT,
    TOOL_CALL_DELTA_EVENT,
    TOOL_CALL_RESULT_EVENT,
    TOOL_CALL_STARTED_EVENT,
    TOOL_CALL_STDERR_EVENT,
    TOOL_CALL_STDOUT_EVENT,
    USER_MESSAGE_EVENT,
    ActiveRunError,
    ChatRunManager,
    QueuedRunItem,
    Run,
    RunCancelledError,
    RunError,
    RunEvent,
    RunNotFoundError,
)
from core.settings import SettingsValidationError, parse_settings_update
from core.utils.errors import ConfigError, VBotError
from core.utils.log_viewer import LogViewer
from core.utils.tokens import estimate_tokens
from server.events import (
    AGENT_CREATED_EVENT,
    AGENT_DELETED_EVENT,
    AGENT_UPDATED_EVENT,
    PROVIDER_AUTH_COMPLETED_EVENT,
    RUN_CANCELLED_SERVER_EVENT,
    RUN_COMPLETED_SERVER_EVENT,
    RUN_FAILED_SERVER_EVENT,
    RUN_OUTPUT_SERVER_EVENT,
    RUN_STARTED_SERVER_EVENT,
)

JsonObject = dict[str, Any]
_LOGGER = logging.getLogger("vbot.server.delegates")

ALLOWED_THINKING_EFFORTS = {"", "none", "minimal", "low", "medium", "high", "xhigh", "max"}
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0
SUBAGENT_SETTING_FIELDS = (
    "max_subagent_depth",
    "max_subagents_per_turn",
    "subagent_timeout_minutes",
)
CRON_SCHEDULE_TYPES = frozenset(("cron", "once"))
CRON_JOB_STATUSES = frozenset(("active", "paused", "completed"))
CHANNEL_PLATFORMS = frozenset(("telegram",))
CHANNEL_DM_SCOPES = frozenset(("per_conversation", "main", "per_peer", "per_account_channel_peer"))

RPC_ERROR_INVALID_REQUEST = "invalid_request"
RPC_ERROR_METHOD_NOT_FOUND = "method_not_found"
RPC_ERROR_DOMAIN = "domain_error"
RPC_ERROR_ACTIVE_RUN = "active_run"
RPC_ERROR_RUN_NOT_FOUND = "run_not_found"
RPC_ERROR_CANCELLED = "run_cancelled"
RPC_ERROR_LAST_AGENT = "last_agent"
RPC_ERROR_OAUTH_NOT_SUPPORTED = "oauth_not_supported"
RPC_ERROR_CHANNEL_NOT_FOUND = "channel_not_found"
RPC_ERROR_CHANNEL_ALREADY_EXISTS = "channel_already_exists"
RPC_ERROR_CHANNEL_CONFIG = "channel_config_error"
RPC_ERROR_QUEUE_ITEM_NOT_FOUND = "queue_item_not_found"


class RpcError(Exception):
    """Expected RPC request or domain error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> JsonObject:
        """Return the provider-agnostic error envelope payload."""
        return {"code": self.code, "message": self.message}


async def dispatch_rpc(state: Any, request: Any) -> JsonObject:
    """Dispatch one JSON-RPC-like vBot server request."""
    try:
        method, params = _parse_rpc_request(request)
        result = await _dispatch_method(state, method, params)
    except RpcError as exc:
        return {"ok": False, "error": exc.to_dict()}
    return {"ok": True, "result": result}


async def _dispatch_method(state: Any, method: str, params: JsonObject) -> JsonObject:
    match method:
        case "connection.list":
            return _list_connections(state, params)
        case "model.list":
            return _list_models(state, params)
        case "model.refresh_db":
            return await _refresh_model_db(state, params)
        case "provider.connect":
            return await _connect_provider(state, params)
        case "provider.disconnect":
            return _disconnect_provider(state, params)
        case "provider.connection_status":
            return _provider_connection_status(state, params)
        case "tool.list":
            return _list_tools(state, params)
        case "skill.list":
            return _list_skills(state, params)
        case "chat.commands":
            return _list_commands(state, params)
        case "agent.list":
            return _list_agents(state)
        case "agent.get":
            return _get_agent(state, params)
        case "agent.create":
            return _create_agent(state, params)
        case "agent.update":
            return _update_agent(state, params)
        case "agent.delete":
            return await _delete_agent(state, params)
        case "session.create":
            return _create_session(state, params)
        case "session.list":
            return _list_sessions(state, params)
        case "session.link_channel":
            return _link_session_to_channel(state, params)
        case "chat.history":
            return _chat_history(state, params)
        case "chat.send":
            return await _send_chat(state, params)
        case "chat.stream":
            return await _stream_chat(state, params)
        case "chat.retry_last_turn":
            return await _retry_chat(state, params)
        case "chat.cancel":
            return await _cancel_chat(state, params)
        case "chat.queue_list":
            return _chat_queue_list(state, params)
        case "chat.queue_remove":
            return _chat_queue_remove(state, params)
        case "chat.queue_update":
            return _chat_queue_update(state, params)
        case "channel.list":
            return _list_channels(state, params)
        case "channel.create":
            return _create_channel(state, params)
        case "channel.update":
            return _update_channel(state, params)
        case "channel.delete":
            return _delete_channel(state, params)
        case "channel.enable":
            return _enable_channel(state, params)
        case "channel.disable":
            return _disable_channel(state, params)
        case "channel.status":
            return _channel_status(state, params)
        case "cron.create":
            return _cron_create(state, params)
        case "cron.list":
            return _cron_list(state, params)
        case "cron.update":
            return _cron_update(state, params)
        case "cron.delete":
            return _cron_delete(state, params)
        case "cron.enable":
            return _cron_enable(state, params)
        case "cron.disable":
            return _cron_disable(state, params)
        case "settings.get_raw":
            return _get_settings_raw(state, params)
        case "settings.set_key":
            return _set_settings_key(state, params)
        case "settings.get":
            return _get_settings(state, params)
        case "settings.update":
            return _update_settings(state, params)
        case "log.list":
            return _list_logs(state, params)
        case "log.read":
            return _read_log(state, params)
        case "prompt.list":
            return _list_prompts(state)
        case "prompt.update":
            return _update_prompt(state, params)
        case "prompt.reset":
            return _reset_prompt(state, params)
        case "prompt.preview":
            return await _preview_prompt(state, params)
        case _:
            raise RpcError(RPC_ERROR_METHOD_NOT_FOUND, f"unknown RPC method: {method}")


def _list_agents(state: Any) -> JsonObject:
    try:
        agents = sorted(state.runtime.agents.list(), key=lambda agent: agent.id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"agents": [_agent_response(state, agent) for agent in agents]}


def _get_agent(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported agent.get fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "id")
    try:
        agent = state.runtime.agents.get(agent_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _agent_response(state, agent)


def _list_models(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "model.list does not accept params")
    try:
        runtime = state.runtime
        models = sorted(
            (
                _model_response(provider_id, model)
                for provider_id in runtime.providers.list_ids()
                if _provider_has_credentials(runtime, provider_id)
                for model in runtime.models.list_for_provider(provider_id)
            ),
            key=lambda model: (model["provider_id"], model["model_id"]),
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"models": models}


def _list_connections(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "connection.list does not accept params")
    try:
        runtime = state.runtime
        connections = [
            _connection_response(runtime, provider_id, connection)
            for provider_id in runtime.providers.list_ids()
            for connection in runtime.providers.get(provider_id).connections
        ]
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"connections": connections}


async def _refresh_model_db(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"provider_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported model refresh fields: {', '.join(unsupported_fields)}",
        )

    try:
        runtime = state.runtime
        resources_dir = _runtime_resources_dir(runtime)
        if "provider_id" in params:
            provider_id = _required_string(params, "provider_id")
            return await _refresh_provider_model_db(runtime, provider_id, resources_dir)

        result = await _refresh_global_model_db(runtime, resources_dir)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return result


async def _connect_provider(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"provider_id", "connection_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported provider connect fields: {', '.join(unsupported_fields)}",
        )

    provider_id = _required_string(params, "provider_id")
    connection_id = _required_string(params, "connection_id")

    try:
        connection = _oauth_device_connection(state.runtime, provider_id, connection_id)
        engine = _device_flow_engine(state)
        oauth_config = connection.oauth
        session = await engine.start_device_flow(provider_id, connection.id, oauth_config)

        async def on_complete(*, success: bool) -> None:
            _publish_provider_auth_completed_event(
                state,
                provider_id=provider_id,
                connection_id=connection_id,
                success=success,
            )

        asyncio.create_task(
            engine._poll_for_token(
                provider_id,
                connection.id,
                oauth_config,
                session.device_code,
                session.interval,
                session.expires_in,
                on_complete,
            )
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return {
        "user_code": session.user_code,
        "verification_uri": session.verification_uri,
        "expires_in": session.expires_in,
    }


def _disconnect_provider(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"provider_id", "connection_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported provider disconnect fields: {', '.join(unsupported_fields)}",
        )

    provider_id = _required_string(params, "provider_id")
    connection_id = _required_string(params, "connection_id")

    try:
        connection = _oauth_connection(state.runtime, provider_id, connection_id)
        _runtime_token_store(state.runtime).delete(provider_id, connection.id)
        engine = getattr(state, "device_flow_engine", None)
        if engine is not None:
            engine.cancel_flow(provider_id, connection.id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return {"provider_id": provider_id, "connection_id": connection_id, "status": "disconnected"}


def _provider_connection_status(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"provider_id", "connection_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported provider connection status fields: {', '.join(unsupported_fields)}",
        )

    provider_id = _required_string(params, "provider_id")
    connection_id = _required_string(params, "connection_id")

    try:
        connection = _oauth_connection(state.runtime, provider_id, connection_id)
        token_store = _runtime_token_store(state.runtime)
        engine = getattr(state, "device_flow_engine", None)
        connected = token_store.has_valid_token(provider_id, connection.id)
        flow_active = _device_flow_active(engine, provider_id, connection.id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return {
        "provider_id": provider_id,
        "connection_id": connection_id,
        "connected": connected,
        "flow_active": flow_active,
    }


async def _refresh_global_model_db(runtime: Any, resources_dir: Path) -> JsonObject:
    refreshed_providers: list[JsonObject] = []
    for provider_id in runtime.providers.list_ids():
        provider = runtime.providers.get(provider_id)
        if not getattr(provider, "models_endpoint", None):
            continue

        try:
            credential_connection, credential_value = await _first_usable_provider_credential(
                runtime,
                provider_id,
                provider,
            )
        except ConfigError:
            continue

        result = await refresh_models(
            provider,
            credential_value,
            resources_dir,
            credential_connection=credential_connection,
        )
        refreshed_providers.append(result)

    _reload_runtime_model_registry(runtime, resources_dir)
    return {
        "providers": refreshed_providers,
        "refreshed_count": len(refreshed_providers),
        "model_count": sum(_model_count(result) for result in refreshed_providers),
    }


async def _refresh_provider_model_db(
    runtime: Any,
    provider_id: str,
    resources_dir: Path,
) -> JsonObject:
    provider = runtime.providers.get(provider_id)
    if not getattr(provider, "models_endpoint", None):
        raise RpcError(
            RPC_ERROR_DOMAIN,
            f"provider '{provider_id}' does not support model refresh",
        )

    credential_connection, credential_value = await _first_usable_provider_credential(
        runtime,
        provider_id,
        provider,
    )
    result = await refresh_models(
        provider,
        credential_value,
        resources_dir,
        credential_connection=credential_connection,
    )
    _reload_runtime_model_registry(runtime, resources_dir)
    return result


def _reload_runtime_model_registry(runtime: Any, resources_dir: Path) -> None:
    ModelRegistry.invalidate(resources_dir)
    runtime._models = ModelRegistry.load(resources_dir)


def _model_count(result: JsonObject) -> int:
    model_count = result.get("model_count", 0)
    if isinstance(model_count, bool) or not isinstance(model_count, int):
        return 0
    return int(model_count)


def _list_tools(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "tool.list does not accept params")
    try:
        tools = state.runtime.tools.list_tools()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"tools": [_tool_response(tool) for tool in tools]}


def _list_skills(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "skill.list does not accept params")
    try:
        skills = state.runtime.skills.list_all()
        invalid_skills = state.runtime.skills.invalid_diagnostics()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {
        "skills": [_skill_response(state.runtime.skills, skill) for skill in skills],
        "invalid_skills": [_invalid_skill_response(diagnostic) for diagnostic in invalid_skills],
    }


def _list_commands(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "chat.commands does not accept params")
    try:
        command_items = [
            {
                "name": name.removeprefix("/"),
                "description": description,
                "type": "command",
            }
            for name, description in sorted(CommandDispatcher.BUILT_IN_COMMANDS.items())
        ]
        skills = sorted(state.runtime.skills.list_all(), key=lambda skill: skill.name)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    skill_items = [
        {
            "name": skill.name,
            "description": skill.description,
            "type": "skill",
        }
        for skill in skills
    ]
    return {"items": [*command_items, *skill_items]}


def _create_agent(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "id")
    name = _required_string(params, "name")
    try:
        state.runtime.agents.create(
            agent_id, name, **_agent_changes(params, blocked={"id", "name"}, for_create=True)
        )
        agent = state.runtime.agents.get(agent_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    response = _agent_response(state, agent)
    _publish_agent_event(state, AGENT_CREATED_EVENT, response)
    return response


def _update_agent(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "id")
    try:
        agent = state.runtime.agents.update(
            agent_id, **_agent_changes(params, blocked={"id"}, for_create=False)
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    response = _agent_response(state, agent)
    _publish_agent_event(state, AGENT_UPDATED_EVENT, response)
    return response


async def _delete_agent(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "id")
    try:
        async with state.agent_delete_lock:
            remaining_agents = [
                agent for agent in state.runtime.agents.list() if agent.id != agent_id
            ]
            if not remaining_agents:
                raise RpcError(RPC_ERROR_LAST_AGENT, "cannot delete the last agent")
            state.runtime.agents.delete(agent_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    result = {
        "agent_id": agent_id,
        "remaining_agents": [_agent_response(state, agent) for agent in remaining_agents],
    }
    _publish_agent_event(state, AGENT_DELETED_EVENT, result)
    return result


def _create_session(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "agent_id")
    session_id = _optional_string(params, "session_id")
    make_current = _optional_bool(params, "make_current", default=False)
    try:
        state.runtime.agents.get(agent_id)
        session = state.runtime.chat_sessions.create(agent_id, session_id=session_id)
        if make_current:
            state.runtime.agents.update(agent_id, current_session_id=session.id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"agent_id": agent_id, "session_id": session.id}


def _list_sessions(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"agent_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported session.list fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "agent_id")
    try:
        sessions = state.runtime.chat_sessions.list_with_metadata(agent_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"sessions": sessions}


def _link_session_to_channel(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {"agent_id", "session_id", "channel_id", "platform_conv_id"}
    unsupported_fields = sorted(set(params) - supported_fields)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported session.link_channel fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    channel_id = _required_string(params, "channel_id")
    platform_conv_id = _required_string(params, "platform_conv_id")

    try:
        channel_service = state.runtime.channel_service
        channel_config = _channel_config_by_id(channel_service, channel_id)
        session = state.runtime.chat_sessions.get(agent_id, session_id)
        metadata = dict(state.runtime.chat_sessions.get_metadata(agent_id, session_id))
        metadata.update(
            {
                "source_channel_id": channel_id,
                "platform": channel_config.platform,
                "platform_conv_id": platform_conv_id,
                "last_reply_target": {
                    "channel_id": channel_id,
                    "platform_target": platform_conv_id,
                },
            }
        )
        state.runtime.chat_sessions.set_metadata(agent_id, session_id, metadata)
        session.add_note(
            _channel_system_reminder(channel_config.platform, channel_id, platform_conv_id)
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


def _chat_history(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "agent_id")
    session_id = _optional_string(params, "session_id")
    try:
        agent = state.runtime.agents.get(agent_id)
        active_session_id = session_id or agent.current_session_id
        session = state.runtime.chat_sessions.get(agent_id, active_session_id)
        messages = [
            _visible_message(message)
            for message in session.load()
            if _is_visible_history_message(message)
        ]
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"agent_id": agent_id, "session_id": active_session_id, "messages": messages}


def _extract_command_text(content: str | list[ContentBlock]) -> str | None:
    if isinstance(content, str):
        return content

    if len(content) != 1:
        return None

    block = content[0]
    if isinstance(block, TextBlock):
        return block.text
    return None


def _command_handled_response(reply: str | None) -> JsonObject:
    return {
        "command_handled": True,
        "reply": reply or "",
    }


async def _send_chat(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    content = _parse_chat_content(params, "content")

    command_text = _extract_command_text(content)
    if command_text and command_text.strip().lower() == "/compact":
        try:
            return await _handle_compact_command(state, agent_id, session_id)
        except Exception as exc:
            raise _map_expected_error(exc) from exc

    if command_text is not None:
        try:
            command_result = _state_command_dispatcher(state).dispatch(
                agent_id,
                session_id,
                command_text,
            )
        except Exception as exc:
            raise _map_expected_error(exc) from exc
        if isinstance(command_result, CommandHandled):
            return _command_handled_response(command_result.reply)

    try:
        run = await state.chat_loop.start_run(agent_id, content, session_id=session_id)
    except ActiveRunError:
        try:
            queued_item = await state.chat_loop.queue_run(
                agent_id,
                content,
                session_id=session_id,
            )
            _bridge_queued_item_to_event_bus(state, queued_item)
        except Exception as exc:
            raise _map_expected_error(exc) from exc
        return _queued_response(queued_item)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    try:
        _bridge_run_to_event_bus(state, run)
        assistant_message = await run.wait()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run, final_message=assistant_message)


async def _stream_chat(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    content = _parse_chat_content(params, "content")

    command_text = _extract_command_text(content)
    if command_text and command_text.strip().lower() == "/compact":
        try:
            return await _handle_compact_command(state, agent_id, session_id)
        except Exception as exc:
            raise _map_expected_error(exc) from exc

    if command_text is not None:
        try:
            command_result = _state_command_dispatcher(state).dispatch(
                agent_id,
                session_id,
                command_text,
            )
        except Exception as exc:
            raise _map_expected_error(exc) from exc
        if isinstance(command_result, CommandHandled):
            return _command_handled_response(command_result.reply)

    streaming_chat_loop = _streaming_chat_loop(state)
    try:
        run = await streaming_chat_loop.start_run(agent_id, content, session_id=session_id)
    except ActiveRunError:
        try:
            queued_item = await streaming_chat_loop.queue_run(
                agent_id,
                content,
                session_id=session_id,
            )
            _bridge_queued_item_to_event_bus(state, queued_item)
        except Exception as exc:
            raise _map_expected_error(exc) from exc
        return _queued_response(queued_item)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    try:
        _bridge_run_to_event_bus(state, run)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run, sse_url=f"/api/runs/{run.id}/events")


async def _handle_compact_command(state: Any, agent_id: str, session_id: str) -> JsonObject:
    compaction_service = getattr(state, "compaction_service", None)
    if compaction_service is None:
        return _command_handled_response("Compaction is not available.")

    runtime = state.runtime
    chat_runs = getattr(state, "chat_runs", None)
    if not isinstance(chat_runs, ChatRunManager):
        chat_runs = getattr(runtime, "chat_runs", None)
    if isinstance(chat_runs, ChatRunManager):
        active_run = chat_runs.active_run(agent_id=agent_id, session_id=session_id)
        if active_run is not None:
            return _command_handled_response(
                "Cannot compact while a run is active for this session."
            )

    agent = runtime.agents.get(agent_id)
    session = runtime.chat_sessions.get(agent_id, session_id)
    messages = session.load()
    raw_settings = runtime.storage.load_compaction_settings()
    settings = CompactionSettings(
        auto=raw_settings["auto"],
        threshold=raw_settings["threshold"],
        tail_tokens=raw_settings["tail_tokens"],
        summary_model=raw_settings["summary_model"],
    )

    provider_id, connection_id = _resolve_agent_connection(runtime, agent)
    adapter = runtime.get_adapter(provider_id, connection_id)
    _model_provider_id, model_id = _split_agent_model(agent.model)
    summary_adapter = adapter
    summary_model_id = model_id

    try:
        summary_adapter, summary_model_id = _resolve_summary_adapter_for_compact(
            runtime,
            adapter,
            model_id,
            settings,
        )
        checkpoint = await compaction_service.compact(
            messages,
            agent=agent,
            summary_adapter=summary_adapter,
            summary_model_id=summary_model_id,
            storage=runtime.storage,
            settings=settings,
        )
        session.append(checkpoint)
    except Exception as exc:
        return _command_handled_response(f"Compaction failed: {exc}")
    finally:
        await _close_adapter(adapter)
        if summary_adapter is not adapter:
            await _close_adapter(summary_adapter)

    return _command_handled_response("Context compacted.")


def _resolve_summary_adapter_for_compact(
    runtime: Any,
    adapter: Any,
    model_id: str,
    settings: CompactionSettings,
) -> tuple[Any, str]:
    summary_model = settings.summary_model
    if not isinstance(summary_model, str):
        return adapter, model_id

    normalized_summary_model = summary_model.strip()
    if not normalized_summary_model:
        return adapter, model_id

    try:
        provider_id, summary_model_id, connection_suffix = parse_model_with_connection(
            normalized_summary_model
        )
    except ChatError:
        return adapter, model_id

    connection_id: str | None = None
    if connection_suffix:
        connection_id = f"{provider_id}:{connection_suffix}"
    else:
        try:
            provider = runtime.providers.get(provider_id)
        except Exception:
            return adapter, model_id

        credential_resolver = getattr(runtime, "provider_credentials", None)
        if credential_resolver is None:
            return adapter, model_id

        for connection in provider.connections:
            candidate_connection_id = f"{provider_id}:{connection.id}"
            if credential_resolver.has_credentials(provider_id, candidate_connection_id):
                connection_id = candidate_connection_id
                break

    if connection_id is None:
        return adapter, model_id

    try:
        summary_adapter = runtime.get_adapter(provider_id, connection_id)
    except Exception:
        return adapter, model_id

    return summary_adapter, summary_model_id


async def _retry_chat(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    try:
        streaming_chat_loop = _streaming_chat_loop(state)
        run = await streaming_chat_loop.retry_run(agent_id, session_id)
        _bridge_run_to_event_bus(state, run)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run, sse_url=f"/api/runs/{run.id}/events")


async def _cancel_chat(state: Any, params: JsonObject) -> JsonObject:
    run_id = _required_string(params, "run_id")
    try:
        run = await state.chat_runs.cancel(run_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return _run_response(run)


def _chat_queue_list(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"agent_id", "session_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.queue_list fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    try:
        items = [
            item
            for item in _state_chat_runs(state).list_queued(agent_id, session_id)
            if not item.internal
        ]
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"items": [item.to_dict() for item in items]}


def _chat_queue_remove(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"agent_id", "session_id", "item_id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.queue_remove fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    item_id = _required_string(params, "item_id")
    try:
        chat_runs = _state_chat_runs(state)
        if not _queue_item_is_public(chat_runs, agent_id, session_id, item_id):
            raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
        removed = chat_runs.remove_queued(agent_id, session_id, item_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if not removed:
        raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
    return {"ok": True}


def _chat_queue_update(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"agent_id", "session_id", "item_id", "content"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported chat.queue_update fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "agent_id")
    session_id = _required_string(params, "session_id")
    item_id = _required_string(params, "item_id")
    content = _parse_chat_content(params, "content")

    try:
        chat_runs = _state_chat_runs(state)
        if not _queue_item_is_public(chat_runs, agent_id, session_id, item_id):
            raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")

        (
            resolved_session_id,
            updated_executor,
            updated_display_content,
        ) = _build_streaming_queue_update(state, agent_id, session_id, content)
        updated = chat_runs.update_queued(
            agent_id,
            resolved_session_id,
            item_id,
            updated_executor,
            updated_display_content,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    if not updated:
        raise RpcError(RPC_ERROR_QUEUE_ITEM_NOT_FOUND, f"queued item not found: {item_id}")
    return {"ok": True}


def _list_channels(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "channel.list does not accept params")

    try:
        channels = [config.to_dict() for config in state.runtime.channel_service.list_channels()]
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"channels": channels}


def _create_channel(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {
        "id",
        "platform",
        "agent_id",
        "dm_scope",
        "allowed_chat_ids",
        "token_env_var",
        "enabled",
    }
    unsupported_fields = sorted(set(params) - supported_fields)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported channel.create fields: {', '.join(unsupported_fields)}",
        )

    config = ChannelConfig(
        id=_required_string(params, "id"),
        platform=_required_channel_platform(params, "platform"),
        agent_id=_required_string(params, "agent_id"),
        dm_scope=_optional_channel_dm_scope(params, "dm_scope", default="per_conversation"),
        allowed_chat_ids=_optional_integer_list(params, "allowed_chat_ids", default=[]),
        token_env_var=_required_string(params, "token_env_var"),
        enabled=_optional_bool(params, "enabled", default=True),
    )

    try:
        _validate_channel_agent_exists(state, config.agent_id)
        state.runtime.channel_service.create_channel(config)
        state.runtime.reload_channel_tool()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"id": config.id}


def _update_channel(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {
        "id",
        "platform",
        "agent_id",
        "dm_scope",
        "allowed_chat_ids",
        "token_env_var",
        "enabled",
    }
    unsupported_fields = sorted(set(params) - supported_fields)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported channel.update fields: {', '.join(unsupported_fields)}",
        )

    channel_id = _required_string(params, "id")
    updates: JsonObject = {}
    if "platform" in params:
        updates["platform"] = _required_channel_platform(params, "platform")
    if "agent_id" in params:
        updates["agent_id"] = _required_string(params, "agent_id")
    if "dm_scope" in params:
        updates["dm_scope"] = _optional_channel_dm_scope(params, "dm_scope", default="")
    if "allowed_chat_ids" in params:
        updates["allowed_chat_ids"] = _required_integer_list(params, "allowed_chat_ids")
    if "token_env_var" in params:
        updates["token_env_var"] = _required_string(params, "token_env_var")
    if "enabled" in params:
        updates["enabled"] = _required_bool(params, "enabled")

    try:
        if "agent_id" in updates:
            _validate_channel_agent_exists(state, updates["agent_id"])
        state.runtime.channel_service.update_channel(channel_id, **updates)
        state.runtime.reload_channel_tool()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


def _delete_channel(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported channel.delete fields: {', '.join(unsupported_fields)}",
        )

    channel_id = _required_string(params, "id")
    try:
        state.runtime.channel_service.delete_channel(channel_id)
        state.runtime.reload_channel_tool()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


def _enable_channel(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported channel.enable fields: {', '.join(unsupported_fields)}",
        )

    channel_id = _required_string(params, "id")
    try:
        state.runtime.channel_service.enable_channel(channel_id)
        state.runtime.reload_channel_tool()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


def _disable_channel(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported channel.disable fields: {', '.join(unsupported_fields)}",
        )

    channel_id = _required_string(params, "id")
    try:
        state.runtime.channel_service.disable_channel(channel_id)
        state.runtime.reload_channel_tool()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


def _channel_status(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported channel.status fields: {', '.join(unsupported_fields)}",
        )

    channel_id = _required_string(params, "id")
    try:
        channel_service = state.runtime.channel_service
        config = _channel_config_by_id(channel_service, channel_id)
        running = _channel_is_running(channel_service, channel_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {
        "id": config.id,
        "enabled": config.enabled,
        "running": running,
    }


def _validate_channel_agent_exists(state: Any, agent_id: str) -> None:
    try:
        state.runtime.agents.get(agent_id)
    except Exception as error:
        raise ChannelConfigError(f"Unknown agent_id: {agent_id}") from error


def _cron_create(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {
        "agent_id",
        "prompt",
        "schedule_type",
        "cron_expression",
        "run_at",
        "timezone",
        "session_id",
    }
    unsupported_fields = sorted(set(params) - supported_fields)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported cron.create fields: {', '.join(unsupported_fields)}",
        )

    agent_id = _required_string(params, "agent_id")
    prompt = _required_string(params, "prompt")
    schedule_type = _required_string(params, "schedule_type")
    if schedule_type not in CRON_SCHEDULE_TYPES:
        options = ", ".join(sorted(CRON_SCHEDULE_TYPES))
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.schedule_type must be one of: {options}",
        )

    cron_expression = _optional_string(params, "cron_expression")
    run_at = _optional_string(params, "run_at")
    timezone = _optional_string(params, "timezone")
    session_id = _optional_string(params, "session_id")

    if schedule_type == "cron":
        if cron_expression is None:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.cron_expression is required when params.schedule_type is 'cron'",
            )
        run_at = None
    else:
        if run_at is None:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.run_at is required when params.schedule_type is 'once'",
            )
        cron_expression = None

    try:
        job = state.runtime.cron_service.create_job(
            agent_id=agent_id,
            prompt=prompt,
            schedule_type=schedule_type,
            cron_expression=cron_expression,
            run_at=run_at,
            timezone=timezone,
            session_id=session_id,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"id": job.id}


def _cron_list(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "cron.list does not accept params")

    try:
        jobs = state.runtime.cron_service.list_jobs()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"jobs": [_cron_job_response(job) for job in jobs]}


def _cron_update(state: Any, params: JsonObject) -> JsonObject:
    supported_fields = {
        "id",
        "agent_id",
        "prompt",
        "schedule_type",
        "cron_expression",
        "run_at",
        "timezone",
        "session_id",
        "status",
    }
    unsupported_fields = sorted(set(params) - supported_fields)
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported cron.update fields: {', '.join(unsupported_fields)}",
        )

    job_id = _required_string(params, "id")
    updates: JsonObject = {}

    if "agent_id" in params:
        updates["agent_id"] = _required_string(params, "agent_id")
    if "prompt" in params:
        updates["prompt"] = _required_string(params, "prompt")
    if "schedule_type" in params:
        schedule_type = _required_string(params, "schedule_type")
        if schedule_type not in CRON_SCHEDULE_TYPES:
            options = ", ".join(sorted(CRON_SCHEDULE_TYPES))
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"params.schedule_type must be one of: {options}",
            )
        updates["schedule_type"] = schedule_type
    if "cron_expression" in params:
        updates["cron_expression"] = _required_string(params, "cron_expression")
    if "run_at" in params:
        updates["run_at"] = _required_string(params, "run_at")
    if "timezone" in params:
        updates["timezone"] = _optional_string(params, "timezone")
    if "session_id" in params:
        updates["session_id"] = _optional_string(params, "session_id")
    if "status" in params:
        status = _required_string(params, "status")
        if status not in CRON_JOB_STATUSES:
            options = ", ".join(sorted(CRON_JOB_STATUSES))
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"params.status must be one of: {options}",
            )
        updates["status"] = status

    try:
        state.runtime.cron_service.update_job(job_id, **updates)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


def _cron_delete(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported cron.delete fields: {', '.join(unsupported_fields)}",
        )

    job_id = _required_string(params, "id")
    try:
        state.runtime.cron_service.delete_job(job_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


def _cron_enable(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported cron.enable fields: {', '.join(unsupported_fields)}",
        )

    job_id = _required_string(params, "id")
    try:
        state.runtime.cron_service.enable_job(job_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


def _cron_disable(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"id"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported cron.disable fields: {', '.join(unsupported_fields)}",
        )

    job_id = _required_string(params, "id")
    try:
        state.runtime.cron_service.disable_job(job_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"ok": True}


def _cron_job_response(job: Any) -> JsonObject:
    return {
        "id": job.id,
        "agent_id": job.agent_id,
        "prompt": job.prompt,
        "schedule_type": job.schedule_type,
        "cron_expression": job.cron_expression,
        "run_at": job.run_at,
        "timezone": job.timezone,
        "session_id": job.session_id,
        "status": job.status,
        "last_fired_at": job.last_fired_at,
        "next_fire_at": _cron_next_fire_at(job),
        "created_at": job.created_at,
    }


def _cron_next_fire_at(job: Any) -> str | None:
    if job.schedule_type != "cron" or job.status != "active" or job.cron_expression is None:
        return None

    try:
        timezone = _resolve_cron_timezone(job.timezone)
        now_local = datetime.now(timezone)
        next_fire_local = cast(
            datetime,
            croniter(job.cron_expression, now_local).get_next(datetime),
        )
        if next_fire_local.tzinfo is None:
            next_fire_local = next_fire_local.replace(tzinfo=timezone)
        return next_fire_local.astimezone(UTC).isoformat()
    except (ValueError, ZoneInfoNotFoundError):
        return None


def _resolve_cron_timezone(timezone_name: str | None) -> tzinfo:
    if timezone_name:
        normalized_timezone = timezone_name.strip()
        if normalized_timezone.upper() == "UTC":
            return UTC
        return ZoneInfo(normalized_timezone)

    local_timezone = datetime.now().astimezone().tzinfo
    if local_timezone is not None:
        return local_timezone
    return UTC


def _get_settings_raw(state: Any, params: JsonObject) -> JsonObject:
    try:
        settings = state.runtime.storage.load_settings()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"settings": dict(settings)}


def _set_settings_key(state: Any, params: JsonObject) -> JsonObject:
    if "key" not in params or "value" not in params:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "settings.set_key requires 'key' and 'value'",
        )

    key = _required_string(params, "key")
    value = params["value"]

    try:
        settings = dict(state.runtime.storage.load_settings())
        settings[key] = value
        state.runtime.storage.save_settings(settings)
    except Exception as exc:
        raise _map_expected_error(exc) from exc

    return {"settings": settings}


def _get_settings(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "settings.get does not accept params")
    try:
        return _settings_response(state)
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _update_settings(state: Any, params: JsonObject) -> JsonObject:
    try:
        settings_update = parse_settings_update(params)
    except SettingsValidationError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc

    try:
        if "appearance" in settings_update:
            state.runtime.storage.update_appearance_settings(settings_update["appearance"])
        if "skills" in settings_update:
            state.runtime.storage.update_skill_directory_settings(
                settings_update["skills"]["directories"]
            )
            reload_skills = getattr(state.runtime, "reload_skills", None)
            if callable(reload_skills):
                reload_skills()
        if "subagents" in settings_update:
            _update_subagent_settings(state.runtime.storage, settings_update["subagents"])
        if "compaction" in settings_update:
            state.runtime.storage.update_compaction_settings(settings_update["compaction"])
        if "defaults" in settings_update:
            defaults_update = cast(JsonObject, settings_update["defaults"])
            if "agent" in defaults_update:
                state.runtime.storage.update_defaults("agent", defaults_update["agent"])
        return _settings_response(state)
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _list_logs(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "log.list does not accept params")
    return _log_viewer(state).list_files()


def _read_log(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"file"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported log read fields: {', '.join(unsupported_fields)}",
        )

    file_name = _required_string(params, "file")
    try:
        return _log_viewer(state).read_file(file_name)
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except FileNotFoundError as exc:
        raise RpcError(RPC_ERROR_DOMAIN, str(exc)) from exc


def _list_prompts(state: Any) -> JsonObject:
    try:
        fragments = PromptFragmentManager(state.runtime.storage).list_fragments()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"fragments": fragments}


def _update_prompt(state: Any, params: JsonObject) -> JsonObject:
    name = _required_string(params, "name")
    content = params.get("content")
    if not isinstance(content, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.content must be a string")
    try:
        return PromptFragmentManager(state.runtime.storage).update_fragment(name, content)
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _reset_prompt(state: Any, params: JsonObject) -> JsonObject:
    name = _required_string(params, "name")
    try:
        return PromptFragmentManager(state.runtime.storage).reset_fragment(name)
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _preview_prompt(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "agent_id")
    try:
        agent = state.runtime.agents.get(agent_id)
    except KeyError as exc:
        raise RpcError(RPC_ERROR_DOMAIN, f"agent not found: {agent_id}") from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    try:
        prompt_manager = state.runtime.system_prompts
        text = prompt_manager.build_system_prompt(agent)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    token_count, estimated = estimate_tokens(text)
    return {"text": text, "tokens": token_count, "estimated": estimated}


def _parse_rpc_request(request: Any) -> tuple[str, JsonObject]:
    if not isinstance(request, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "RPC request must be a JSON object")
    method = request.get("method")
    if not isinstance(method, str) or not method:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "RPC method must be a non-empty string")
    params = request.get("params", {})
    if not isinstance(params, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "RPC params must be an object")
    return method, params


def _parse_chat_content(params: JsonObject, key: str) -> str | list[ContentBlock]:
    value = params.get(key)
    if isinstance(value, str):
        if value:
            return value
    elif isinstance(value, list):
        parsed_blocks: list[ContentBlock] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise RpcError(
                    RPC_ERROR_INVALID_REQUEST,
                    f"params.{key}[{index}] must be an object",
                )
            try:
                parsed_blocks.append(content_block_from_dict(item))
            except ContentBlockError as exc:
                raise RpcError(
                    RPC_ERROR_INVALID_REQUEST,
                    f"params.{key}[{index}] is invalid: {exc}",
                ) from exc
        return parsed_blocks

    raise RpcError(
        RPC_ERROR_INVALID_REQUEST,
        f"params.{key} must be a non-empty string or a list of content blocks",
    )


def _required_string(params: JsonObject, key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a non-empty string")
    return value


def _required_bool(params: JsonObject, key: str) -> bool:
    value = params.get(key)
    if not isinstance(value, bool):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a boolean")
    return value


def _required_integer_list(params: JsonObject, key: str) -> list[int]:
    value = params.get(key)
    if not isinstance(value, list):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a list of integers")

    parsed: list[int] = []
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"params.{key} must be a list of integers",
            )
        parsed.append(item)
    return parsed


def _optional_integer_list(params: JsonObject, key: str, *, default: list[int]) -> list[int]:
    if key not in params:
        return list(default)
    return _required_integer_list(params, key)


def _required_channel_platform(params: JsonObject, key: str) -> str:
    platform = _required_string(params, key)
    if platform not in CHANNEL_PLATFORMS:
        options = ", ".join(sorted(CHANNEL_PLATFORMS))
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be one of: {options}",
        )
    return platform


def _optional_channel_dm_scope(params: JsonObject, key: str, *, default: str) -> str:
    if key not in params:
        return default

    dm_scope = _required_string(params, key)
    if dm_scope not in CHANNEL_DM_SCOPES:
        options = ", ".join(sorted(CHANNEL_DM_SCOPES))
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.{key} must be one of: {options}",
        )
    return dm_scope


def _channel_config_by_id(channel_service: Any, channel_id: str) -> ChannelConfig:
    for config in channel_service.list_channels():
        if config.id == channel_id:
            return cast(ChannelConfig, config)
    raise ChannelNotFoundError(f"Channel not found: {channel_id}")


def _channel_is_running(channel_service: Any, channel_id: str) -> bool:
    running_checker = getattr(channel_service, "_is_running", None)
    if callable(running_checker):
        return bool(running_checker(channel_id))

    adapter_tasks = getattr(channel_service, "_adapter_tasks", None)
    if isinstance(adapter_tasks, dict):
        task = adapter_tasks.get(channel_id)
        return bool(task is not None and not task.done())
    return False


def _channel_system_reminder(platform: str, channel_id: str, platform_conv_id: str) -> str:
    platform_name = platform.capitalize()
    return (
        f"This session is receiving messages via {platform_name} "
        f"(channel: {channel_id}, chat: {platform_conv_id}).\n"
        f"Respond in a style appropriate for {platform_name} messaging."
    )


def _settings_response(state: Any) -> JsonObject:
    runtime = state.runtime
    appearance = runtime.storage.load_appearance_settings()
    subagents = runtime.storage.load_subagent_settings()
    compaction = runtime.storage.load_compaction_settings()
    defaults = runtime.storage.load_defaults()
    server_bind = _server_bind_response(state)

    response = {
        "general": {
            "server": server_bind,
            "data_directory": str(runtime.storage.data_dir),
        },
        "providers": {
            "items": [
                _provider_settings_item(runtime, provider_id)
                for provider_id in runtime.providers.list_ids()
            ],
            "custom_endpoints": {
                "supported": False,
                "items": [],
            },
        },
        "appearance": {
            "language": appearance["language"],
            "available_languages": runtime.storage.supported_appearance_languages(),
        },
        "defaults": defaults,
        "subagents": {field: subagents[field] for field in SUBAGENT_SETTING_FIELDS},
        "compaction": dict(compaction),
    }
    skill_directory_loader = getattr(runtime.storage, "load_skill_directory_settings", None)
    if callable(skill_directory_loader):
        response["skills"] = {
            "default_directory": str(runtime.storage.data_dir / "skills"),
            "directories": skill_directory_loader(),
        }
    return response


def _update_subagent_settings(storage: Any, subagents: JsonObject) -> None:
    settings = storage.load_settings()
    merged_settings = dict(settings)
    for field in SUBAGENT_SETTING_FIELDS:
        merged_settings[field] = subagents[field]
    storage.save_settings(merged_settings)


def _server_bind_response(state: Any) -> JsonObject:
    server_bind = getattr(state, "server_bind", {})
    listen_host = server_bind.get("listen_host", "127.0.0.1")
    listen_port = server_bind.get("listen_port", 8420)
    port_source = server_bind.get("port_source", "default")
    return {
        "listen_host": listen_host,
        "listen_port": listen_port,
        "port_source": port_source,
    }


def _log_viewer(state: Any) -> LogViewer:
    log_viewer = getattr(state, "log_viewer", None)
    if log_viewer is not None:
        return cast(LogViewer, log_viewer)
    log_viewer = LogViewer(state.runtime.storage.data_dir)
    state.log_viewer = log_viewer
    return log_viewer


def _provider_settings_item(runtime: Any, provider_id: str) -> JsonObject:
    provider = runtime.providers.get(provider_id)
    credentials_configured = _provider_has_credentials(runtime, provider_id)
    return {
        "id": provider.id,
        "name": provider.name,
        "base_url": provider.base_url,
        "models_endpoint": getattr(provider, "models_endpoint", None),
        "connections": [
            _provider_settings_connection(runtime, provider.id, connection)
            for connection in provider.connections
        ],
        "credentials_configured": credentials_configured,
        "status": "configured" if credentials_configured else "missing_credentials",
        "model_count": len(runtime.models.list_for_provider(provider_id)),
        "kind": "remote" if provider.base_url else "local",
        "editable": False,
    }


def _provider_has_credentials(runtime: Any, provider_id: str) -> bool:
    return bool(runtime.has_provider_credentials(provider_id))


def _connection_has_credentials(runtime: Any, provider_id: str, connection_id: str) -> bool:
    return bool(runtime.provider_credentials.has_credentials(provider_id, connection_id))


async def _first_usable_provider_credential(
    runtime: Any,
    provider_id: str,
    provider: Any,
) -> tuple[Any, str]:
    for connection in provider.connections:
        connection_id = f"{provider_id}:{connection.id}"
        if runtime.provider_credentials.has_credentials(provider_id, connection_id):
            credential = await _runtime_provider_credential(
                runtime, provider_id, connection_id, connection
            )
            return connection, str(credential)
    raise ConfigError(f"Provider credentials not found for provider '{provider_id}'")


async def _runtime_provider_credential(
    runtime: Any,
    provider_id: str,
    connection_id: str,
    connection: Any,
) -> str:
    if getattr(connection, "type", "") != "oauth" or getattr(connection, "oauth", None) is None:
        return str(runtime.provider_credentials.get_credentials(provider_id, connection_id))

    token_store = _runtime_token_store(runtime)
    getter = OAuthTokenGetter(token_store, provider_id, connection.id, connection.oauth)
    async with getter:
        return await getter()


def _runtime_resources_dir(runtime: Any) -> Path:
    resolve_resources_path = getattr(runtime, "_resolve_resources_path", None)
    if callable(resolve_resources_path):
        return Path(resolve_resources_path())
    resources_dir = getattr(runtime, "resources_dir", None)
    if resources_dir is not None:
        return Path(resources_dir)
    raise ConfigError("Runtime resources directory is not available")


def _oauth_device_connection(runtime: Any, provider_id: str, connection_id: str) -> Any:
    connection = _oauth_connection(runtime, provider_id, connection_id)
    oauth_config = getattr(connection, "oauth", None)
    if oauth_config is None or getattr(oauth_config, "flow", "") != "device":
        raise RpcError(
            RPC_ERROR_OAUTH_NOT_SUPPORTED,
            f"provider connection '{connection_id}' does not support OAuth Device Flow",
        )
    return connection


def _oauth_connection(runtime: Any, provider_id: str, connection_id: str) -> Any:
    connection = _provider_connection(runtime, provider_id, connection_id)
    if getattr(connection, "type", "") != "oauth":
        raise RpcError(
            RPC_ERROR_OAUTH_NOT_SUPPORTED,
            f"provider connection '{connection_id}' is not an OAuth connection",
        )
    return connection


def _provider_connection(runtime: Any, provider_id: str, connection_id: str) -> Any:
    provider = runtime.providers.get(provider_id)
    expected_prefix = f"{provider_id}:"
    if not connection_id.startswith(expected_prefix):
        raise ConfigError(
            f"Connection id '{connection_id}' does not belong to provider '{provider_id}'"
        )
    local_connection_id = connection_id.removeprefix(expected_prefix)
    return provider.get_connection(local_connection_id)


def _runtime_token_store(runtime: Any) -> Any:
    token_store = getattr(runtime, "token_store", None)
    if token_store is None:
        raise ConfigError("Runtime OAuth token store is not available")
    return token_store


def _device_flow_engine(state: Any) -> DeviceFlowEngine:
    engine = getattr(state, "device_flow_engine", None)
    if engine is not None:
        return cast(DeviceFlowEngine, engine)
    engine = DeviceFlowEngine(_runtime_token_store(state.runtime))
    state.device_flow_engine = engine
    return engine


def _device_flow_active(engine: Any, provider_id: str, local_connection_id: str) -> bool:
    if engine is None:
        return False
    active_flows = getattr(engine, "_active_flows", {})
    task = active_flows.get((provider_id, local_connection_id))
    return bool(task is not None and not task.done())


def _connection_response(runtime: Any, provider_id: str, connection: Any) -> JsonObject:
    connection_id = f"{provider_id}:{connection.id}"
    return {
        "id": connection_id,
        "provider_id": provider_id,
        "type": connection.type,
        "label": connection.label,
        "usable": _connection_has_credentials(runtime, provider_id, connection_id),
    }


def _provider_settings_connection(runtime: Any, provider_id: str, connection: Any) -> JsonObject:
    connection_id = f"{provider_id}:{connection.id}"
    return {
        "id": connection_id,
        "type": connection.type,
        "label": connection.label,
        "configured": _connection_has_credentials(runtime, provider_id, connection_id),
    }


def _optional_string(params: JsonObject, key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a non-empty string")
    return value


def _optional_bool(params: JsonObject, key: str, *, default: bool) -> bool:
    value = params.get(key, default)
    if not isinstance(value, bool):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a boolean")
    return value


def _agent_changes(params: JsonObject, *, blocked: set[str], for_create: bool) -> JsonObject:
    public_fields = {
        "name",
        "model",
        "fallback_model",
        "temperature",
        "thinking_effort",
        "allowed_tools",
        "allowed_skills",
    }
    if not for_create:
        public_fields.add("current_session_id")

    rejected_fields = sorted(set(params) - public_fields - blocked)
    if rejected_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported agent fields: {', '.join(rejected_fields)}",
        )

    changes: JsonObject = {}
    for key, value in params.items():
        if key in blocked:
            continue
        changes[key] = _validate_agent_field(key, value)
    return changes


def _validate_agent_field(key: str, value: Any) -> Any:
    if key in {"name", "current_session_id"}:
        if not isinstance(value, str) or not value:
            raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a non-empty string")
        return value
    if key in {"model", "fallback_model"}:
        if not isinstance(value, str):
            raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a string")
        return value
    if key == "temperature":
        return _validate_temperature(value, allow_none=True)
    if key == "thinking_effort":
        return _validate_thinking_effort(value, allow_none=True)
    if key in {"allowed_tools", "allowed_skills"}:
        return _validate_string_list(key, value)
    raise RpcError(RPC_ERROR_INVALID_REQUEST, f"unsupported agent field: {key}")


def _validate_temperature(
    value: Any,
    *,
    label: str = "params.temperature",
    allow_none: bool = False,
) -> float | None:
    if value is None:
        if allow_none:
            return None
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"{label} must be a number")

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"{label} must be a number")
    temperature = float(value)
    if not math.isfinite(temperature):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"{label} must be finite")
    if temperature < MIN_TEMPERATURE or temperature > MAX_TEMPERATURE:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"{label} must be between {MIN_TEMPERATURE:g} and {MAX_TEMPERATURE:g}",
        )
    return temperature


def _validate_thinking_effort(
    value: Any,
    *,
    label: str = "params.thinking_effort",
    allow_none: bool = False,
) -> str | None:
    if value is None:
        if allow_none:
            return None
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"{label} must be a string")

    if not isinstance(value, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"{label} must be a string")
    if value not in ALLOWED_THINKING_EFFORTS:
        allowed = ", ".join(repr(item) for item in sorted(ALLOWED_THINKING_EFFORTS))
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"{label} must be one of: {allowed}",
        )
    return value


def _validate_string_list(key: str, value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.{key} must be a list of strings")
    return list(value)


def _map_expected_error(error: Exception) -> RpcError:
    if isinstance(error, RpcError):
        return error
    if isinstance(error, ChannelNotFoundError):
        return RpcError(RPC_ERROR_CHANNEL_NOT_FOUND, str(error))
    if isinstance(error, ChannelConfigError):
        message = str(error)
        if message.startswith("Channel already exists"):
            return RpcError(RPC_ERROR_CHANNEL_ALREADY_EXISTS, message)
        return RpcError(RPC_ERROR_CHANNEL_CONFIG, message)
    if isinstance(error, ActiveRunError):
        return RpcError(RPC_ERROR_ACTIVE_RUN, str(error))
    if isinstance(error, RunNotFoundError):
        return RpcError(RPC_ERROR_RUN_NOT_FOUND, str(error))
    if isinstance(error, RunCancelledError):
        return RpcError(RPC_ERROR_CANCELLED, str(error))
    if isinstance(
        error, (AgentError, ChatError, ChatSessionError, ConfigError, RunError, VBotError, KeyError)
    ):
        return RpcError(RPC_ERROR_DOMAIN, str(error))
    raise error


def _run_response(
    run: Run,
    *,
    final_message: ChatMessage | None = None,
    sse_url: str | None = None,
) -> JsonObject:
    response: JsonObject = {
        "run_id": run.id,
        "agent_id": run.agent_id,
        "session_id": run.session_id,
        "status": run.status.value,
        "events": [_remove_opaque_provider_metadata(event.to_dict()) for event in run.events],
    }
    if final_message is not None:
        response["message"] = _visible_message(final_message)
    if sse_url is not None:
        response["sse_url"] = sse_url
    return response


def _queued_response(item: QueuedRunItem) -> JsonObject:
    return {
        "queued": True,
        "item": item.to_dict(),
    }


def _visible_message(message: ChatMessage) -> JsonObject:
    return cast(JsonObject, _remove_opaque_provider_metadata(message.to_dict()))


def _is_visible_history_message(message: ChatMessage) -> bool:
    return message.role != "note"


def _resolve_context_window(state: Any, model: str) -> int | None:
    """Resolve a model string (provider/model-id) to its context_window from the registry.

    Returns None if the model format is invalid or the model is not found.
    """
    bare_model = parse_bare_model(model)
    if "/" not in bare_model:
        return None
    provider_id, _, model_id = bare_model.partition("/")
    if not provider_id or not model_id:
        return None
    try:
        model_entry = state.runtime.models.get(provider_id, model_id)
    except (KeyError, AttributeError):
        return None
    return int(model_entry.context_window)


def _agent_response(state: Any, agent: Any) -> JsonObject:
    return {
        "id": agent.id,
        "name": agent.name,
        "model": agent.model,
        "fallback_model": agent.fallback_model,
        "workspace": agent.workspace,
        "temperature": agent.temperature,
        "thinking_effort": agent.thinking_effort,
        "allowed_tools": list(agent.allowed_tools),
        "allowed_skills": list(agent.allowed_skills),
        "current_session_id": agent.current_session_id,
        "context_window": _resolve_context_window(state, agent.model),
        "created_at": agent.created_at,
        "updated_at": agent.updated_at,
    }


def _model_response(provider_id: str, model: Any) -> JsonObject:
    return {
        "id": f"{provider_id}/{model.model_id}",
        "provider_id": provider_id,
        "model_id": model.model_id,
        "name": model.name,
        "capabilities": {
            "vision": model.capabilities.vision,
            "tools": model.capabilities.tools,
            "json_mode": model.capabilities.json_mode,
            "reasoning": {
                "supported": model.capabilities.reasoning.supported,
            },
        },
        "context_window": model.context_window,
        "max_output_tokens": model.max_output_tokens,
    }


def _tool_response(tool: Any) -> JsonObject:
    return {
        "name": tool.name,
        "description": tool.description,
    }


def _skill_response(skill_registry: Any, skill: Any) -> JsonObject:
    warnings = skill_registry.warnings_for(skill.name)
    return {
        "name": skill.name,
        "description": skill.description,
        "valid": len(warnings) == 0,
        "warnings": warnings,
    }


def _invalid_skill_response(diagnostic: Any) -> JsonObject:
    return {
        "name": diagnostic.name,
        "path": str(diagnostic.path),
        "valid": False,
        "warnings": list(diagnostic.warnings),
    }


def _bridge_run_to_event_bus(state: Any, run: Run) -> None:
    event_bus = getattr(state, "event_bus", None)
    if event_bus is None:
        return
    task = asyncio.create_task(_publish_run_events(event_bus, run))
    task.add_done_callback(_on_run_event_bridge_done)


def _on_run_event_bridge_done(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        _LOGGER.warning("Run event bridge failed", exc_info=True)


def _bridge_queued_item_to_event_bus(state: Any, item: QueuedRunItem) -> None:
    """Bridge the eventual run start for one queued item into server lifecycle events."""

    def _on_run_started(future: asyncio.Future[Run]) -> None:
        if future.cancelled():
            return
        try:
            run = future.result()
        except BaseException:
            return
        _bridge_run_to_event_bus(state, run)

    item.future.add_done_callback(_on_run_started)


def _queue_item_is_public(
    chat_runs: ChatRunManager,
    agent_id: str,
    session_id: str,
    item_id: str,
) -> bool:
    for item in chat_runs.list_queued(agent_id, session_id):
        if item.item_id == item_id:
            return not item.internal
    return False


def _streaming_chat_loop(state: Any) -> Any:
    chat_loop = getattr(state, "streaming_chat_loop", None)
    if chat_loop is not None:
        return chat_loop
    runtime = getattr(state, "runtime", None)
    if runtime is not None:
        try:
            chat_loop = runtime.streaming_chat_loop
        except AttributeError:
            chat_loop = getattr(runtime, "_streaming_chat_loop", None)
        except RuntimeError:
            if runtime.__class__.__name__ == "Runtime" and runtime.__class__.__module__.startswith(
                "core.runtime"
            ):
                raise
            chat_loop = getattr(runtime, "_streaming_chat_loop", None)
        if chat_loop is not None:
            state.streaming_chat_loop = chat_loop
            return chat_loop
    chat_loop = ChatLoop(state.runtime, streaming=True)
    state.streaming_chat_loop = chat_loop
    return chat_loop


def _build_streaming_queue_update(
    state: Any,
    agent_id: str,
    session_id: str,
    content: str | list[ContentBlock],
) -> tuple[str, Any, str]:
    streaming_chat_loop = _streaming_chat_loop(state)
    runtime = getattr(streaming_chat_loop, "_runtime", state.runtime)

    agent = runtime.agents.get(agent_id)
    provider_id, _connection_id = _resolve_agent_connection(runtime, agent)
    _ensure_provider_exists(runtime.providers, provider_id)

    get_session = getattr(streaming_chat_loop, "_get_session", None)
    if callable(get_session):
        session = get_session(agent_id, session_id, create_missing=False)
    else:
        session = runtime.chat_sessions.get(agent_id, session_id)

    return (
        session.id,
        lambda run: streaming_chat_loop._execute_run(run, content),
        _display_content_preview(content),
    )


def _state_chat_runs(state: Any) -> ChatRunManager:
    chat_runs = getattr(state, "chat_runs", None)
    if isinstance(chat_runs, ChatRunManager):
        return chat_runs

    runtime = getattr(state, "runtime", None)
    if runtime is not None:
        runtime_chat_runs = getattr(runtime, "chat_runs", None)
        if isinstance(runtime_chat_runs, ChatRunManager):
            state.chat_runs = runtime_chat_runs
            return runtime_chat_runs

        try:
            runtime_chat_runs = runtime.chat_run_manager
        except AttributeError:
            runtime_chat_runs = getattr(runtime, "_chat_run_manager", None)
        except RuntimeError:
            if runtime.__class__.__name__ == "Runtime" and runtime.__class__.__module__.startswith(
                "core.runtime"
            ):
                raise
            runtime_chat_runs = getattr(runtime, "_chat_run_manager", None)

        if isinstance(runtime_chat_runs, ChatRunManager):
            runtime.chat_runs = runtime_chat_runs
            state.chat_runs = runtime_chat_runs
            return runtime_chat_runs

    fallback_chat_runs = ChatRunManager()
    state.chat_runs = fallback_chat_runs
    if runtime is not None:
        runtime.chat_runs = fallback_chat_runs
    return fallback_chat_runs


def _state_command_dispatcher(state: Any) -> CommandDispatcher:
    command_dispatcher = getattr(state, "command_dispatcher", None)
    if isinstance(command_dispatcher, CommandDispatcher):
        return command_dispatcher

    runtime = getattr(state, "runtime", None)
    if runtime is not None:
        try:
            command_dispatcher = runtime.command_dispatcher
        except AttributeError:
            command_dispatcher = getattr(runtime, "_command_dispatcher", None)
        except RuntimeError:
            if runtime.__class__.__name__ == "Runtime" and runtime.__class__.__module__.startswith(
                "core.runtime"
            ):
                raise
            command_dispatcher = getattr(runtime, "_command_dispatcher", None)

        if isinstance(command_dispatcher, CommandDispatcher):
            state.command_dispatcher = command_dispatcher
            return command_dispatcher

    fallback_dispatcher = CommandDispatcher(ChatRunManager())
    state.command_dispatcher = fallback_dispatcher
    return fallback_dispatcher


def _publish_agent_event(state: Any, event_type: str, payload: JsonObject) -> None:
    """Publish an agent CRUD event to the server event bus if available."""
    event_bus = getattr(state, "event_bus", None)
    if event_bus is None:
        return
    event_bus.publish(event_type, payload)


def _publish_provider_auth_completed_event(
    state: Any,
    *,
    provider_id: str,
    connection_id: str,
    success: bool,
) -> None:
    event_bus = getattr(state, "event_bus", None)
    if event_bus is None:
        return
    event_bus.publish(
        PROVIDER_AUTH_COMPLETED_EVENT,
        {"provider_id": provider_id, "connection_id": connection_id, "success": success},
    )


async def _publish_run_events(event_bus: Any, run: Run) -> None:
    async for event in run.subscribe():
        if event.type in RUN_DELTA_EVENT_TYPES:
            continue
        summary = _server_event_from_run_event(event)
        event_bus.publish(summary["type"], summary["payload"])


def _server_event_from_run_event(event: RunEvent) -> JsonObject:
    payload: JsonObject = {
        "run_id": event.run_id,
        "agent_id": event.agent_id,
        "session_id": event.session_id,
        "run_event_type": event.type,
        "run_event_sequence": event.sequence,
        "run_event_timestamp": event.timestamp,
    }
    if event.type in RUN_OUTPUT_EVENT_TYPES:
        payload["output"] = _remove_opaque_provider_metadata(event.payload)
    if event.type in RUN_TERMINAL_EVENT_TYPES:
        payload["status"] = event.payload.get("status")
    if event.type == RUN_COMPLETED_EVENT and "usage" in event.payload:
        payload["usage"] = _remove_opaque_provider_metadata(event.payload["usage"])
    return {"type": SERVER_EVENT_TYPES.get(event.type, RUN_OUTPUT_SERVER_EVENT), "payload": payload}


def _remove_opaque_provider_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _remove_opaque_provider_metadata(item)
            for key, item in value.items()
            if key != "reasoning_meta"
        }
    if isinstance(value, list):
        return [_remove_opaque_provider_metadata(item) for item in value]
    return value


RUN_OUTPUT_EVENT_TYPES = {
    USER_MESSAGE_EVENT,
    REASONING_EVENT,
    TOOL_CALL_STARTED_EVENT,
    TOOL_CALL_RESULT_EVENT,
    ASSISTANT_OUTPUT_EVENT,
    MODEL_FALLBACK_ACTIVATED_EVENT,
    ERROR_MESSAGE_PERSISTED_EVENT,
}
RUN_DELTA_EVENT_TYPES = {
    ASSISTANT_OUTPUT_DELTA_EVENT,
    REASONING_DELTA_EVENT,
    TOOL_CALL_DELTA_EVENT,
    TOOL_CALL_STDOUT_EVENT,
    TOOL_CALL_STDERR_EVENT,
}
RUN_TERMINAL_EVENT_TYPES = {RUN_COMPLETED_EVENT, RUN_CANCELLED_EVENT, RUN_FAILED_EVENT}
SERVER_EVENT_TYPES = {
    RUN_STARTED_EVENT: RUN_STARTED_SERVER_EVENT,
    USER_MESSAGE_EVENT: RUN_OUTPUT_SERVER_EVENT,
    REASONING_EVENT: RUN_OUTPUT_SERVER_EVENT,
    TOOL_CALL_STARTED_EVENT: RUN_OUTPUT_SERVER_EVENT,
    TOOL_CALL_RESULT_EVENT: RUN_OUTPUT_SERVER_EVENT,
    ASSISTANT_OUTPUT_EVENT: RUN_OUTPUT_SERVER_EVENT,
    MODEL_FALLBACK_ACTIVATED_EVENT: RUN_OUTPUT_SERVER_EVENT,
    ERROR_MESSAGE_PERSISTED_EVENT: RUN_OUTPUT_SERVER_EVENT,
    RUN_COMPLETED_EVENT: RUN_COMPLETED_SERVER_EVENT,
    RUN_CANCELLED_EVENT: RUN_CANCELLED_SERVER_EVENT,
    RUN_FAILED_EVENT: RUN_FAILED_SERVER_EVENT,
}
