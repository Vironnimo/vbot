"""Map expected domain errors to stable RPC errors."""

from __future__ import annotations

from core.agents import (
    AgentError,
    AgentNotFoundError,
    AgentOrderConflictError,
    InvalidAgentOrderError,
)
from core.channels import ChannelConfigError, ChannelNotFoundError
from core.chat import ChatError, ChatSessionError
from core.database import DatabaseError, IncidentConflictError
from core.extensions.extensions import SessionCapabilityExpiredError
from core.model_tasks import TaskModelError, TaskModelValidationError
from core.performance import RecordingActiveError, RecordingInactiveError
from core.projects import (
    AgentResolutionError,
    ModelConfigurationError,
    ProjectAlreadyExistsError,
    ProjectError,
    ProjectNotFoundError,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
)
from core.runs import ActiveRunError, RunCancelledError, RunError, RunNotFoundError
from core.sessions import SessionPageCursorError
from core.tools.terminal_manager import (
    TerminalCapacityError,
    TerminalClosedError,
    TerminalCursorError,
    TerminalLaunchError,
    TerminalNotFoundError,
    TerminalProgramNotRunningError,
    TerminalStaleScreenError,
)
from core.utils.errors import ConfigError, VBotError
from server.rpc.errors import (
    RPC_ERROR_ACTIVE_RUN,
    RPC_ERROR_AGENT_NOT_FOUND,
    RPC_ERROR_AGENT_ORDER_CONFLICT,
    RPC_ERROR_CANCELLED,
    RPC_ERROR_CHANNEL_ALREADY_EXISTS,
    RPC_ERROR_CHANNEL_CONFIG,
    RPC_ERROR_CHANNEL_NOT_FOUND,
    RPC_ERROR_DOMAIN,
    RPC_ERROR_INVALID_REQUEST,
    RPC_ERROR_PERFORMANCE_RECORDING_ACTIVE,
    RPC_ERROR_PERFORMANCE_RECORDING_INACTIVE,
    RPC_ERROR_PROJECT_ALREADY_EXISTS,
    RPC_ERROR_PROJECT_NOT_FOUND,
    RPC_ERROR_RUN_NOT_FOUND,
    RPC_ERROR_SESSION_CAPABILITY_EXPIRED,
    RPC_ERROR_TERMINAL_PROGRAM_NOT_RUNNING,
    RpcError,
)


def _map_expected_error(error: Exception) -> RpcError:
    if isinstance(error, RpcError):
        return error
    if isinstance(error, SessionCapabilityExpiredError):
        return RpcError(
            RPC_ERROR_SESSION_CAPABILITY_EXPIRED,
            "Session capability expired; reload the Extension page and retry.",
        )
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
    if isinstance(error, (SessionPageCursorError, IncidentConflictError)):
        return RpcError(RPC_ERROR_INVALID_REQUEST, str(error))
    # Every database failure (unavailable, corrupt, format, schema mismatch)
    # maps uniformly; the message names the database and the kind of failure.
    if isinstance(error, DatabaseError):
        return RpcError(RPC_ERROR_DOMAIN, str(error))
    # Agent resolution keeps a missing Project or Agent precise, so a missing
    # address reports the same not-found code as a direct store lookup.
    if isinstance(error, (ProjectNotFoundError, ResolutionProjectNotFoundError)):
        return RpcError(RPC_ERROR_PROJECT_NOT_FOUND, str(error))
    if isinstance(error, ProjectAlreadyExistsError):
        return RpcError(RPC_ERROR_PROJECT_ALREADY_EXISTS, str(error))
    if isinstance(error, ModelConfigurationError):
        return RpcError(RPC_ERROR_INVALID_REQUEST, str(error))
    if isinstance(error, TaskModelValidationError):
        return RpcError(RPC_ERROR_INVALID_REQUEST, str(error))
    if isinstance(error, (AgentNotFoundError, ResolutionAgentNotFoundError)):
        return RpcError(RPC_ERROR_AGENT_NOT_FOUND, str(error))
    if isinstance(error, AgentOrderConflictError):
        return RpcError(RPC_ERROR_AGENT_ORDER_CONFLICT, str(error))
    if isinstance(error, InvalidAgentOrderError):
        return RpcError(RPC_ERROR_INVALID_REQUEST, str(error))
    if isinstance(error, TerminalNotFoundError):
        return RpcError(RPC_ERROR_INVALID_REQUEST, str(error))
    if isinstance(error, TerminalProgramNotRunningError):
        return RpcError(RPC_ERROR_TERMINAL_PROGRAM_NOT_RUNNING, str(error))
    if isinstance(
        error,
        (
            TerminalCapacityError,
            TerminalClosedError,
            TerminalCursorError,
            TerminalLaunchError,
            TerminalStaleScreenError,
        ),
    ):
        return RpcError(RPC_ERROR_INVALID_REQUEST, str(error))
    if isinstance(error, RecordingActiveError):
        return RpcError(RPC_ERROR_PERFORMANCE_RECORDING_ACTIVE, str(error))
    if isinstance(error, RecordingInactiveError):
        return RpcError(RPC_ERROR_PERFORMANCE_RECORDING_INACTIVE, str(error))
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
        ),
    ):
        return RpcError(RPC_ERROR_DOMAIN, str(error))
    raise error
