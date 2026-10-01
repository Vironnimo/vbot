"""Map expected domain errors to stable RPC errors."""

from __future__ import annotations

from typing import Any

from core.agents import (
    AgentError,
    AgentNotFoundError,
    AgentOrderConflictError,
    AgentReferencedError,
    InvalidAgentOrderError,
)
from core.archive import (
    ArchiveEntryBusyError,
    ArchiveEntryError,
    ArchiveEntryNotFoundError,
    ArchiveNotRestorableError,
    ArchiveRestoreConflictError,
    RestoreProblem,
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
from core.storage import SettingsConflictError
from core.tools.terminal_manager import (
    TerminalCapacityError,
    TerminalClosedError,
    TerminalLaunchError,
    TerminalNotFoundError,
    TerminalProgramNotRunningError,
    TerminalStaleScreenError,
)
from core.utils.errors import ConfigError, VBotError
from server.rpc.errors import (
    RPC_ERROR_ACTIVE_RUN,
    RPC_ERROR_AGENT_IN_USE,
    RPC_ERROR_AGENT_NOT_FOUND,
    RPC_ERROR_AGENT_ORDER_CONFLICT,
    RPC_ERROR_ARCHIVE_ENTRY_BUSY,
    RPC_ERROR_ARCHIVE_ENTRY_NOT_FOUND,
    RPC_ERROR_ARCHIVE_NOT_RESTORABLE,
    RPC_ERROR_ARCHIVE_RESTORE_CONFLICT,
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
    RPC_ERROR_SETTINGS_CONFLICT,
    RPC_ERROR_TERMINAL_PROGRAM_NOT_RUNNING,
    RpcError,
)

_PROBLEM_KEYS = frozenset({"code", "message", "id"})


def restore_problem_payload(problem: RestoreProblem) -> dict[str, Any]:
    """One restore blocker or warning as ``{code, message, ...details}``."""
    details = {key: value for key, value in problem.details.items() if key not in _PROBLEM_KEYS}
    return {"code": problem.code, "message": problem.message, **details}


def _restore_conflict_payload(problem: RestoreProblem) -> dict[str, Any]:
    """A taken id or Session address, with ``id`` naming what is taken when it is one id."""
    details = problem.details
    taken = details.get("agent_id") or details.get("project_id")
    if taken is None:
        session_ids = {
            address.get("session_id")
            for address in details.get("addresses") or ()
            if isinstance(address, dict)
        }
        taken = next(iter(session_ids)) if len(session_ids) == 1 else None
    payload = restore_problem_payload(problem)
    return {"code": payload.pop("code"), "id": taken, **payload}


def _map_archive_error(error: ArchiveEntryError) -> RpcError:
    if isinstance(error, ArchiveEntryNotFoundError):
        return RpcError(
            RPC_ERROR_ARCHIVE_ENTRY_NOT_FOUND, str(error), data={"entry_ids": list(error.entry_ids)}
        )
    if isinstance(error, ArchiveEntryBusyError):
        return RpcError(
            RPC_ERROR_ARCHIVE_ENTRY_BUSY,
            str(error),
            data={"entry_id": error.entry_id, "state": error.state},
        )
    if isinstance(error, ArchiveRestoreConflictError):
        return RpcError(
            RPC_ERROR_ARCHIVE_RESTORE_CONFLICT,
            str(error),
            data={
                "entry_id": error.entry_id,
                "conflicts": [_restore_conflict_payload(conflict) for conflict in error.conflicts],
                "fix": "target_id",
            },
        )
    if isinstance(error, ArchiveNotRestorableError):
        return RpcError(
            RPC_ERROR_ARCHIVE_NOT_RESTORABLE,
            str(error),
            data={
                "entry_id": error.entry_id,
                "blockers": [restore_problem_payload(blocker) for blocker in error.blockers],
            },
        )
    return RpcError(RPC_ERROR_DOMAIN, str(error))


def _map_expected_error(error: Exception) -> RpcError:
    if isinstance(error, RpcError):
        return error
    if isinstance(error, ArchiveEntryError):
        return _map_archive_error(error)
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
    if isinstance(error, AgentReferencedError):
        return RpcError(RPC_ERROR_AGENT_IN_USE, str(error))
    if isinstance(error, SettingsConflictError):
        return RpcError(RPC_ERROR_SETTINGS_CONFLICT, str(error))
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
