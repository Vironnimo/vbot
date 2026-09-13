"""Private local application lifecycle RPC handlers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from core.sessions import SessionAddress
from core.tools._bash_update_handoff import read_update_handoff_ticket
from core.utils.atomic import atomic_write_text
from core.utils.server_control import is_authorized_control_token
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.errors import RPC_ERROR_ACTIVE_RUN, RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import _reject_unsupported, _required_string

JsonObject = dict[str, Any]
_OPERATION_ID_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
_CONTINUATION_PROMPT = (
    "A vBot update was requested from this Session. Inspect its saved result with "
    "vbot update status {operation_id}. If it is still pending, wait for its terminal status. "
    "Report the verified outcome, then continue the original task when appropriate. Do not "
    "repeat the update or replay earlier Tool actions."
)


def _authorize(state: Any, params: JsonObject, fields: set[str], method: str) -> str:
    _reject_unsupported(params, fields | {"control_token"}, method)
    provided = _required_string(params, "control_token")
    if not is_authorized_control_token(provided, getattr(state, "control_token", None)):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "application control authorization failed")
    return _operation_id(params)


def _operation_id(params: JsonObject) -> str:
    operation_id = _required_string(params, "operation_id")
    if len(operation_id) > 100 or any(c not in _OPERATION_ID_CHARS for c in operation_id):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.operation_id is invalid")
    return operation_id


def _verified_ticket(state: Any, ticket_id: str):
    try:
        ticket = read_update_handoff_ticket(state.runtime.storage.data_dir, ticket_id)
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    if not ticket.acknowledged:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "update handoff is not durably acknowledged")
    address = SessionAddress(
        project_id=ticket.project_id,
        agent_id=ticket.agent_id,
        session_id=ticket.session_id,
    )
    try:
        messages = state.runtime.chat_sessions.get(address).load()
    except Exception as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "update handoff Session is unavailable") from exc
    requested = any(
        message.role == "assistant"
        and any(call.id == ticket.tool_call_id for call in (message.tool_calls or []))
        for message in messages
    )
    persisted = any(
        message.role == "tool" and message.tool_call_id == ticket.tool_call_id
        for message in messages
    )
    if not requested or not persisted:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "update handoff does not match a durable Tool call in its Session",
        )
    return ticket, address


async def _maintenance_begin(state: Any, params: JsonObject) -> JsonObject:
    operation_id = _authorize(
        state,
        params,
        {"operation_id", "handoff_ticket_id"},
        "application.maintenance_begin",
    )
    origin = None
    ticket_id = params.get("handoff_ticket_id")
    if ticket_id is not None:
        if not isinstance(ticket_id, str) or not ticket_id:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                "params.handoff_ticket_id must be a non-empty string",
            )
        ticket, address = _verified_ticket(state, ticket_id)
        origin = (address, ticket.run_id)
    try:
        result = cast(
            JsonObject, await state.chat_runs.maintenance_begin(operation_id, origin=origin)
        )
        if origin is not None:
            address, run_id = origin
            active = state.chat_runs.active_run(
                agent_id=address.agent_id,
                session_id=address.session_id,
                project_id=address.project_id,
            )
            if active is not None and active.id == run_id:
                await state.chat_runs.cancel(run_id, reason="application_update")
                result = cast(JsonObject, await state.chat_runs.maintenance_status(operation_id))
        return result
    except Exception as exc:
        raise RpcError(RPC_ERROR_ACTIVE_RUN, str(exc)) from exc


async def _maintenance_status(state: Any, params: JsonObject) -> JsonObject:
    operation_id = _authorize(state, params, {"operation_id"}, "application.maintenance_status")
    try:
        return cast(JsonObject, await state.chat_runs.maintenance_status(operation_id))
    except Exception as exc:
        raise RpcError(RPC_ERROR_ACTIVE_RUN, str(exc)) from exc


async def _maintenance_end(state: Any, params: JsonObject) -> JsonObject:
    operation_id = _authorize(state, params, {"operation_id"}, "application.maintenance_end")
    try:
        return cast(JsonObject, await state.chat_runs.maintenance_end(operation_id))
    except Exception as exc:
        raise RpcError(RPC_ERROR_ACTIVE_RUN, str(exc)) from exc


def _continuation_receipt_path(state: Any, operation_id: str) -> Path:
    return (
        Path(state.runtime.storage.data_dir).resolve()
        / "runtime"
        / "update-continuations"
        / f"{operation_id}.json"
    )


def _read_receipt(path: Path, ticket_id: str) -> JsonObject | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "update continuation receipt is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != 1
        or payload.get("ticket_id") != ticket_id
        or not isinstance(payload.get("bootstrap_job_id"), str)
    ):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "update continuation receipt does not match")
    return payload


async def _update_continuation(state: Any, params: JsonObject) -> JsonObject:
    operation_id = _authorize(
        state,
        params,
        {"operation_id", "handoff_ticket_id"},
        "application.update_continuation",
    )
    ticket_id = _required_string(params, "handoff_ticket_id")
    ticket, _address = _verified_ticket(state, ticket_id)
    receipt_path = _continuation_receipt_path(state, operation_id)
    receipt = _read_receipt(receipt_path, ticket_id)
    if receipt is not None:
        return {
            "operation_id": operation_id,
            "bootstrap_job_id": receipt["bootstrap_job_id"],
            "created": False,
        }

    prompt = _CONTINUATION_PROMPT.format(operation_id=operation_id)
    name = f"Update continuation {operation_id}"
    service = state.runtime.bootstrap_service
    matching = [
        job
        for job in service.list_jobs()
        if job.name == name
        and job.agent_id == ticket.agent_id
        and job.project_id == ticket.project_id
        and job.session_id == ticket.session_id
        and job.prompt == prompt
        and job.mode == "once"
    ]
    created = False
    if matching:
        job = matching[0]
    else:
        job = service.create_job(
            agent_id=ticket.agent_id,
            project_id=ticket.project_id,
            session_id=ticket.session_id,
            name=name,
            prompt=prompt,
            mode="once",
        )
        created = True
    atomic_write_text(
        receipt_path,
        json.dumps(
            {
                "schema": 1,
                "operation_id": operation_id,
                "ticket_id": ticket_id,
                "bootstrap_job_id": job.id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        mode=0o600,
    )
    return {"operation_id": operation_id, "bootstrap_job_id": job.id, "created": created}


def method_handlers() -> dict[str, RpcMethodHandler]:
    return {
        "application.maintenance_begin": _maintenance_begin,
        "application.maintenance_status": _maintenance_status,
        "application.maintenance_end": _maintenance_end,
        "application.update_continuation": _update_continuation,
    }


__all__ = ["method_handlers"]
