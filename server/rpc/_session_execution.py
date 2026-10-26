"""Operator-owned computer selection for a Session's native Tools."""

from __future__ import annotations

from typing import Any, cast

from core.computers import ComputerError, normalize_execution
from core.runs import RunAdmissionBlockedError
from core.sessions import SUBAGENT_PARENT_META_KEY
from core.subagents.links import read_link
from core.utils.workers import settle_before_cancelling
from server.access import current_principal
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RPC_ERROR_SESSION_BUSY, RpcError
from server.rpc.event_bridge import publish_session_changed
from server.rpc.validation import _reject_unsupported, _required_session_address


def optional_execution(
    state: Any, params: dict[str, Any], *, label: str = "params"
) -> dict[str, Any] | None:
    """Validate explicit operator grants; omission leaves Session defaults alone."""
    if "execution" not in params:
        return None
    if current_principal() is None:
        raise RpcError("access_denied", "Only the user can choose a Session's computers.")
    value = params["execution"]
    if not isinstance(value, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"{label}.execution must be an object")
    _reject_unsupported(value, {"computer_id", "allowed_computer_ids"}, f"{label}.execution")
    try:
        execution = normalize_execution(value)
        if execution["computer_id"] not in execution["allowed_computer_ids"]:
            raise ValueError("The default computer must be included in allowed_computer_ids.")
        for computer_id in execution["allowed_computer_ids"]:
            state.runtime.computers.get(computer_id)
    except (ValueError, ComputerError) as error:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(error)) from error
    return execution


async def set_session_execution(state: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Change one idle Session's explicit computer authority atomically."""
    _reject_unsupported(params, {"agent_id", "session_id", "execution"}, "session.set_execution")
    execution = optional_execution(state, params)
    if execution is None:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.execution is required")
    address = _required_session_address(params)
    sessions = state.runtime.chat_sessions

    def apply() -> dict[str, Any]:
        # This edit may grant a linked child only computers its Parent grants
        # now. Parent edits alone do not rewrite the child's stored policy;
        # a later delegation send intersects it with the Parent's grants.
        if sessions.metadata_value(address, SUBAGENT_PARENT_META_KEY) is not None:
            link = read_link(sessions, address, strict=True)
            if link is None:
                raise ValueError("The Sub-Agent's Parent Session is unavailable.")
            parent = normalize_execution(sessions.metadata_value(link.parent, "execution"))
            if not set(execution["allowed_computer_ids"]).issubset(parent["allowed_computer_ids"]):
                raise ValueError("A Sub-Agent can use only its Parent Session's computers.")

        def update(metadata: dict[str, Any]) -> None:
            previous = metadata.get("execution")
            # Generation 1 writers preserve fields they do not understand.
            metadata["execution"] = {
                **(previous if isinstance(previous, dict) else {}),
                **execution,
            }

        return cast(dict[str, Any], sessions.mutate_metadata(address, update)["execution"])

    async def apply_and_publish() -> dict[str, Any]:
        updated = await sessions.run_async(apply)
        publish_session_changed(state, address)
        return {"execution": updated}

    try:
        async with (
            sessions.write_lock(address),
            state.runtime.chat_run_manager.session_admission_guard(address),
        ):
            return await settle_before_cancelling(apply_and_publish())
    except RunAdmissionBlockedError as error:
        raise RpcError(
            RPC_ERROR_SESSION_BUSY,
            "This Session has active or queued work. Finish or stop it before changing computers.",
        ) from error
    except ValueError as error:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(error)) from error
    except Exception as error:
        raise _map_expected_error(error) from error
