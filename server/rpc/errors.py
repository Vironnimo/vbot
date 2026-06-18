"""Stable RPC error codes and envelope error type."""

from __future__ import annotations

from typing import Any

JsonObject = dict[str, Any]

RPC_ERROR_INVALID_REQUEST = "invalid_request"
RPC_ERROR_METHOD_NOT_FOUND = "method_not_found"
RPC_ERROR_DOMAIN = "domain_error"
RPC_ERROR_ACTIVE_RUN = "active_run"
RPC_ERROR_RUN_NOT_FOUND = "run_not_found"
RPC_ERROR_CANCELLED = "run_cancelled"
RPC_ERROR_LAST_AGENT = "last_agent"
RPC_ERROR_AGENT_BUSY = "agent_busy"
RPC_ERROR_AGENT_IN_USE = "agent_in_use"
RPC_ERROR_OAUTH_NOT_SUPPORTED = "oauth_not_supported"
RPC_ERROR_CHANNEL_NOT_FOUND = "channel_not_found"
RPC_ERROR_CHANNEL_ALREADY_EXISTS = "channel_already_exists"
RPC_ERROR_CHANNEL_CONFIG = "channel_config_error"
RPC_ERROR_QUEUE_ITEM_NOT_FOUND = "queue_item_not_found"
RPC_ERROR_PROJECT_NOT_FOUND = "project_not_found"
RPC_ERROR_PROJECT_ALREADY_EXISTS = "project_already_exists"
RPC_ERROR_PROJECT_BUSY = "project_busy"
RPC_ERROR_PROJECT_IN_USE = "project_in_use"


class RpcError(Exception):
    """Expected RPC request or domain error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> JsonObject:
        """Return the provider-agnostic error envelope payload."""
        return {"code": self.code, "message": self.message}
