"""Shared, voluntary first-setup progress for every Accessor."""

from __future__ import annotations

from typing import Any, cast

from core.runtime._setup import observe_setup, update_setup
from core.settings.setup_state import SETUP_STEPS, SetupState, SetupStep
from server.events import RESOURCE_KIND_SETUP
from server.rpc._mutations import serialized_mutation
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.event_bridge import publish_resource_changed
from server.rpc.validation import _reject_unsupported

JsonObject = dict[str, Any]


def observe_configuration(state: Any) -> None:
    """Blocking: latch an ordinary configuration change and notify Accessors."""
    if observe_setup(state.runtime, actor="configuration"):
        publish_resource_changed(state, RESOURCE_KIND_SETUP)


async def _change(state: Any, *, step: SetupStep | None = None, finish: bool = False) -> JsonObject:
    try:
        outcome: tuple[SetupState, bool] = await state.runtime.chat_sessions.run_async(
            update_setup, state.runtime, step=step, finish=finish
        )
    except Exception as error:
        raise _map_expected_error(error) from error
    result, changed = outcome
    if changed:
        publish_resource_changed(state, RESOURCE_KIND_SETUP)
    return result.payload()


async def _get(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, set(), "setup.get")
    return await _change(state)


async def _update(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"step"}, "setup.update")
    step = params.get("step")
    if not isinstance(step, str) or step not in SETUP_STEPS:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.step must be provider, model, components or services",
        )
    return await _change(state, step=cast(SetupStep, step))


async def _finish(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, set(), "setup.finish")
    return await _change(state, finish=True)


def method_handlers() -> dict[str, RpcMethodHandler]:
    return {
        name: serialized_mutation(handler, lock_attribute="_setup_mutation_lock")
        for name, handler in (
            ("setup.get", _get),
            ("setup.update", _update),
            ("setup.finish", _finish),
        )
    }
