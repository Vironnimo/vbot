"""Map expected domain errors to stable RPC errors."""

from __future__ import annotations

from core.agents import AgentError
from core.channels import ChannelConfigError, ChannelNotFoundError
from core.chat import ChatError, ChatSessionError
from core.model_tasks import TaskModelError
from core.projects import (
    AgentResolutionError,
    ProjectAlreadyExistsError,
    ProjectError,
    ProjectNotFoundError,
)
from core.runs import ActiveRunError, RunCancelledError, RunError, RunNotFoundError
from core.utils.errors import ConfigError, VBotError
from server.rpc.errors import (
    RPC_ERROR_ACTIVE_RUN,
    RPC_ERROR_CANCELLED,
    RPC_ERROR_CHANNEL_ALREADY_EXISTS,
    RPC_ERROR_CHANNEL_CONFIG,
    RPC_ERROR_CHANNEL_NOT_FOUND,
    RPC_ERROR_DOMAIN,
    RPC_ERROR_PROJECT_ALREADY_EXISTS,
    RPC_ERROR_PROJECT_NOT_FOUND,
    RPC_ERROR_RUN_NOT_FOUND,
    RpcError,
)


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
    if isinstance(error, ProjectNotFoundError):
        return RpcError(RPC_ERROR_PROJECT_NOT_FOUND, str(error))
    if isinstance(error, ProjectAlreadyExistsError):
        return RpcError(RPC_ERROR_PROJECT_ALREADY_EXISTS, str(error))
    if isinstance(error, ProjectError):
        return RpcError(RPC_ERROR_DOMAIN, str(error))
    if isinstance(
        error,
        (
            AgentError,
            AgentResolutionError,
            ChatError,
            ChatSessionError,
            ConfigError,
            RunError,
            TaskModelError,
            VBotError,
            KeyError,
        ),
    ):
        return RpcError(RPC_ERROR_DOMAIN, str(error))
    raise error
