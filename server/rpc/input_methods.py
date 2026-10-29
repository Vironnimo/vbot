"""Operator configuration of task inputs. HTTP submission owns no configuration."""

from __future__ import annotations

from typing import Any

from core.projects import format_agent_address
from core.utils.workers import settle_before_cancelling
from server.events import RESOURCE_KIND_INPUTS
from server.rpc.agent_refs import _agent_reference_lock
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.validation import _reject_unsupported, _required_agent_address, _required_string


async def _list(state: Any, params: dict[str, Any]) -> dict[str, Any]:
    _reject_unsupported(params, set(), "input.list")
    inputs = state.runtime.direct_inputs
    try:
        rows = inputs.list_inputs()
    except Exception as error:
        raise _map_expected_error(error) from error
    return {
        "inputs": [
            {
                **inputs.payload(row),
                "agent_id": format_agent_address(row["agent_id"], row.get("project_id")),
            }
            for row in rows
        ]
    }


async def _mutate(state: Any, params: dict[str, Any], action: str) -> dict[str, Any]:
    allowed = (
        {"id", "name", "agent_id", "working_project_id", "enabled", "run_overrides"}
        if action in {"create", "update"}
        else {"id"}
    )
    _reject_unsupported(params, allowed, f"input.{action}")
    inputs = state.runtime.direct_inputs
    identity = None if action == "create" else _required_string(params, "id")
    changes = {key: value for key, value in params.items() if key != "id"}
    if action in {"create", "update"} and "agent_id" in changes:
        agent_id, project_id = _required_agent_address(params, "agent_id")
        changes.update(agent_id=agent_id, project_id=project_id)

    async def apply() -> dict[str, Any]:
        if action in {"create", "update"}:
            row = await inputs.configure(identity, changes)
            row["agent_id"] = format_agent_address(row["agent_id"], row.get("project_id"))
            result = {"input": row}
        elif action == "delete":
            await inputs.delete(identity)
            result = {"ok": True}
        elif action == "secret":
            return {"secret": await inputs.secret(identity)}
        else:
            result = {"secret": await inputs.rotate_secret(identity)}
        publish_resource_changed(state, RESOURCE_KIND_INPUTS)
        return result

    try:
        async with _agent_reference_lock(state):
            return await settle_before_cancelling(apply())
    except Exception as error:
        raise _map_expected_error(error) from error


def method_handlers() -> dict[str, RpcMethodHandler]:
    async def create(state: Any, params: dict[str, Any]) -> dict[str, Any]:
        return await _mutate(state, params, "create")

    async def update(state: Any, params: dict[str, Any]) -> dict[str, Any]:
        return await _mutate(state, params, "update")

    async def delete(state: Any, params: dict[str, Any]) -> dict[str, Any]:
        return await _mutate(state, params, "delete")

    async def secret(state: Any, params: dict[str, Any]) -> dict[str, Any]:
        return await _mutate(state, params, "secret")

    async def rotate(state: Any, params: dict[str, Any]) -> dict[str, Any]:
        return await _mutate(state, params, "rotate_secret")

    return {
        "input.list": _list,
        "input.create": create,
        "input.update": update,
        "input.delete": delete,
        "input.secret": secret,
        "input.rotate_secret": rotate,
    }
