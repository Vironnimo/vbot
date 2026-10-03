"""Librarian RPC handlers.

``librarian.status`` returns an Identity Agent's Librarian settings, its recent
passes (when, what each did, the Librarian Session of each consolidation Run),
the next scheduled pass and the Skill revisions the last pass recorded;
``unscheduled_reason`` says why the Agent gets no scheduled pass.
``librarian.overview`` says whether the built-in Librarian is available and
lists the recent passes over all Agents. ``librarian.run`` starts a pass at
once regardless of the interval: it refuses with ``agent_busy`` while a pass
runs or the Agent or the Librarian has an active or queued Run, and with
``invalid_request`` when no pass can run for the Agent (the Librarian is
unavailable, the Agent's ``librarian_enabled`` is off, it has no Skills of its
own, or it is the Librarian).
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
    except LibrarianUnavailableError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _librarian_overview(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, set(), "librarian.overview")
    return await _librarian(state).overview()


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
        "librarian.overview": _librarian_overview,
        "librarian.run": _librarian_run,
    }
