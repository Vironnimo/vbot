"""Compatibility facade for server RPC dispatch and event bridging."""

from __future__ import annotations

from typing import Any

import core.runs as core_runs
from server.rpc import (
    agent_methods,
    automation_methods,
    catalog_methods,
    channel_methods,
    chat_methods,
    connection_methods,
    event_bridge,
    operations_methods,
    payloads,
    provider_access,
    runtime_access,
    settings_methods,
)
from server.rpc import errors as rpc_errors
from server.rpc.dispatcher import dispatch_rpc as _dispatch_rpc_envelope
from server.rpc.methods import build_method_handlers

JsonObject = dict[str, Any]
RPC_ERROR_METHOD_NOT_FOUND = rpc_errors.RPC_ERROR_METHOD_NOT_FOUND
RPC_ERROR_INVALID_REQUEST = rpc_errors.RPC_ERROR_INVALID_REQUEST
ALLOWED_THINKING_EFFORTS = agent_methods.ALLOWED_THINKING_EFFORTS
MIN_TEMPERATURE = agent_methods.MIN_TEMPERATURE
MAX_TEMPERATURE = agent_methods.MAX_TEMPERATURE
MAX_CHAT_HISTORY_LIMIT = chat_methods.MAX_CHAT_HISTORY_LIMIT
RUN_OUTPUT_EVENT_TYPES = event_bridge.RUN_OUTPUT_EVENT_TYPES
RUN_DELTA_EVENT_TYPES = event_bridge.RUN_DELTA_EVENT_TYPES
SERVER_EVENT_TYPES = event_bridge.SERVER_EVENT_TYPES
RUN_COMPLETED_EVENT = core_runs.RUN_COMPLETED_EVENT
RUN_FAILED_EVENT = core_runs.RUN_FAILED_EVENT
OAuthTokenGetter = provider_access.OAuthTokenGetter
refresh_models = connection_methods.refresh_models
datetime = automation_methods.datetime
ZoneInfo = automation_methods.ZoneInfo
_LOGGER = event_bridge._LOGGER
connection_methods._LOGGER = _LOGGER


async def dispatch_rpc(state: Any, request: Any) -> JsonObject:
    """Dispatch one JSON-RPC-like vBot server request."""
    _sync_legacy_aliases()
    return await _dispatch_rpc_envelope(state, request, build_method_handlers())


def _sync_legacy_aliases() -> None:
    """Propagate transitional facade monkeypatches into split handler modules."""
    connection_methods.refresh_models = refresh_models
    _set_module_attr(provider_access, "OAuthTokenGetter", OAuthTokenGetter)
    connection_methods._LOGGER = _LOGGER
    event_bridge._LOGGER = _LOGGER
    event_bridge._bridge_run_to_event_bus = _bridge_run_to_event_bus
    _set_module_attr(automation_methods, "datetime", datetime)
    _set_module_attr(automation_methods, "ZoneInfo", ZoneInfo)
    runtime_access._state_chat_runs = _state_chat_runs
    runtime_access._streaming_chat_loop = _streaming_chat_loop
    runtime_access._build_streaming_queue_update = _build_streaming_queue_update
    chat_methods._state_chat_runs = _state_chat_runs
    chat_methods._streaming_chat_loop = _streaming_chat_loop
    chat_methods._build_streaming_queue_update = _build_streaming_queue_update
    chat_methods._bridge_run_to_event_bus = _bridge_run_to_event_bus
    chat_methods._bridge_queued_item_to_event_bus = _bridge_queued_item_to_event_bus


def _set_module_attr(module: Any, name: str, value: Any) -> None:
    setattr(module, name, value)


# Legacy private-name aliases kept for tests and transitional callers.
_list_agents = agent_methods._list_agents
_get_agent = agent_methods._get_agent
_create_agent = agent_methods._create_agent
_update_agent = agent_methods._update_agent
_delete_agent = agent_methods._delete_agent
_create_session = agent_methods._create_session
_list_sessions = agent_methods._list_sessions
_link_session_to_channel = agent_methods._link_session_to_channel
_list_models = connection_methods._list_models
_list_connections = connection_methods._list_connections
_set_provider_key = connection_methods._set_provider_key
_refresh_model_db = connection_methods._refresh_model_db
_connect_provider = connection_methods._connect_provider
_disconnect_provider = connection_methods._disconnect_provider
_provider_connection_status = connection_methods._provider_connection_status
_list_tools = catalog_methods._list_tools
_list_skills = catalog_methods._list_skills
_list_commands = catalog_methods._list_commands
_chat_history = chat_methods._chat_history
_send_chat = chat_methods._send_chat
_stream_chat = chat_methods._stream_chat
_retry_chat = chat_methods._retry_chat
_cancel_chat = chat_methods._cancel_chat
_chat_queue_list = chat_methods._chat_queue_list
_chat_queue_remove = chat_methods._chat_queue_remove
_chat_queue_update = chat_methods._chat_queue_update
_list_channels = channel_methods._list_channels
_create_channel = channel_methods._create_channel
_update_channel = channel_methods._update_channel
_delete_channel = channel_methods._delete_channel
_enable_channel = channel_methods._enable_channel
_disable_channel = channel_methods._disable_channel
_channel_status = channel_methods._channel_status
_cron_create = automation_methods._cron_create
_cron_list = automation_methods._cron_list
_cron_update = automation_methods._cron_update
_cron_delete = automation_methods._cron_delete
_cron_enable = automation_methods._cron_enable
_cron_disable = automation_methods._cron_disable
_get_settings_raw = settings_methods._get_settings_raw
_set_settings_key = settings_methods._set_settings_key
_get_settings = settings_methods._get_settings
_update_settings = settings_methods._update_settings
_task_model_settings = settings_methods._task_model_settings
_task_model_update = settings_methods._task_model_update
_task_model_list_targets = settings_methods._task_model_list_targets
_task_model_options = settings_methods._task_model_options
_settings_response = settings_methods._settings_response
_list_logs = operations_methods._list_logs
_read_log = operations_methods._read_log
_list_prompts = operations_methods._list_prompts
_update_prompt = operations_methods._update_prompt
_reset_prompt = operations_methods._reset_prompt
_preview_prompt = operations_methods._preview_prompt
_remove_opaque_provider_metadata = payloads._remove_opaque_provider_metadata
_visible_message = payloads._visible_message
_server_event_from_run_event = event_bridge._server_event_from_run_event
_bridge_run_to_event_bus = event_bridge._bridge_run_to_event_bus
_bridge_queued_item_to_event_bus = event_bridge._bridge_queued_item_to_event_bus
_run_was_already_bridged = event_bridge._run_was_already_bridged
bridge_run_to_event_bus = event_bridge.bridge_run_to_event_bus
_state_chat_runs = runtime_access._state_chat_runs
_streaming_chat_loop = runtime_access._streaming_chat_loop
_build_streaming_queue_update = runtime_access._build_streaming_queue_update
