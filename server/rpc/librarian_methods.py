"""Librarian RPC handlers.

``librarian.status`` returns an Identity Agent's Librarian settings, the state
of its last pass (when, what it did, the next scheduled pass) and the Skill
revisions that pass recorded; ``unscheduled_reason`` says why the Agent gets
no scheduled pass. ``librarian.run`` starts a pass at once regardless of the
interval: it refuses with ``agent_busy`` while a pass of the Agent runs or the
Agent has an active or queued Run, and with ``invalid_request`` when the
Agent's ``librarian_enabled`` is off or it cannot call ``skill`` and
``skill_manage``.
"""

from __future__ import annotations

from typing import Any

from core.automation import LibrarianBusyError, LibrarianService, LibrarianUnavailableError
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_AGENT_BUSY, RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import _reject_unsupported, _required_string

JsonObject = dict[str, Any]


def _librarian(state: Any) -> LibrarianService:
    service: LibrarianService = state.runtime.librarian
    return service


async def _librarian_status(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id"}, "librarian.status")
    agent_id = _required_string(params, "agent_id")
    try:
        return await _librarian(state).status(agent_id)
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _librarian_run(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"agent_id"}, "librarian.run")
    agent_id = _required_string(params, "agent_id")
    try:
        return await _librarian(state).run(agent_id)
    except LibrarianBusyError as exc:
        raise RpcError(RPC_ERROR_AGENT_BUSY, str(exc)) from exc
    except LibrarianUnavailableError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the Librarian RPC handlers."""

    return {
        "librarian.status": _librarian_status,
        "librarian.run": _librarian_run,
    }
