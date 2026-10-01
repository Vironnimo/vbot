"""Stable RPC error codes and envelope error type."""

from __future__ import annotations

from typing import Any

JsonObject = dict[str, Any]

RPC_ERROR_INVALID_REQUEST = "invalid_request"
RPC_ERROR_METHOD_NOT_FOUND = "method_not_found"
RPC_ERROR_INTERNAL = "internal_error"
RPC_ERROR_SESSION_CAPABILITY_EXPIRED = "session_capability_expired"
RPC_ERROR_DOMAIN = "domain_error"
RPC_ERROR_ACTIVE_RUN = "active_run"
RPC_ERROR_RUN_NOT_FOUND = "run_not_found"
RPC_ERROR_CANCELLED = "run_cancelled"
RPC_ERROR_LAST_AGENT = "last_agent"
RPC_ERROR_AGENT_BUSY = "agent_busy"
RPC_ERROR_AGENT_IN_USE = "agent_in_use"
RPC_ERROR_AGENT_ORDER_CONFLICT = "agent_order_conflict"
RPC_ERROR_AGENT_NOT_FOUND = "agent_not_found"
RPC_ERROR_SKILL_NOT_FOUND = "skill_not_found"
RPC_ERROR_SESSION_BUSY = "session_busy"
RPC_ERROR_SESSION_IN_USE = "session_in_use"
RPC_ERROR_OAUTH_NOT_SUPPORTED = "oauth_not_supported"
RPC_ERROR_CHANNEL_NOT_FOUND = "channel_not_found"
RPC_ERROR_CHANNEL_ALREADY_EXISTS = "channel_already_exists"
RPC_ERROR_CHANNEL_CONFIG = "channel_config_error"
RPC_ERROR_QUEUE_ITEM_NOT_FOUND = "queue_item_not_found"
RPC_ERROR_QUEUE_ITEM_STEERING = "queue_item_steering"
RPC_ERROR_PROJECT_NOT_FOUND = "project_not_found"
RPC_ERROR_PROJECT_ALREADY_EXISTS = "project_already_exists"
RPC_ERROR_PROJECT_BUSY = "project_busy"
RPC_ERROR_PROJECT_IN_USE = "project_in_use"
RPC_ERROR_PERFORMANCE_RECORDING_ACTIVE = "performance_recording_active"
RPC_ERROR_PERFORMANCE_RECORDING_INACTIVE = "performance_recording_inactive"
RPC_ERROR_TERMINAL_PROGRAM_NOT_RUNNING = "terminal_program_not_running"
RPC_ERROR_SETTINGS_CONFLICT = "settings_conflict"
RPC_ERROR_ARCHIVE_ENTRY_NOT_FOUND = "archive_entry_not_found"
RPC_ERROR_ARCHIVE_ENTRY_BUSY = "archive_entry_busy"
RPC_ERROR_ARCHIVE_RESTORE_CONFLICT = "archive_restore_conflict"
RPC_ERROR_ARCHIVE_NOT_RESTORABLE = "archive_not_restorable"


class RpcError(Exception):
    """Expected RPC request or domain error.

    ``data`` optionally carries structured facts about the refusal, such as the
    references that block a deletion, so clients can present them in their own
    words instead of parsing ``message``.
    """

    def __init__(self, code: str, message: str, *, data: JsonObject | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> JsonObject:
        """Return the provider-agnostic error envelope payload."""
        payload: JsonObject = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return payload
