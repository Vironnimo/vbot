"""Thin RPC access to decisions and persistent experiments."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from core.model_tasks.decision_types import DecisionError
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import _reject_unsupported, _required_string


def _integer(params: dict[str, Any], key: str) -> int:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"{key} must be a positive integer")
    return value


async def _list(state: Any, params: dict[str, Any]) -> Any:
    _reject_unsupported(params, set(), "decision.list")
    return await state.runtime.decisions.list_experiments()


async def _get(state: Any, params: dict[str, Any]) -> Any:
    _reject_unsupported(params, {"id"}, "decision.get")
    return await state.runtime.decisions.get_experiment(_required_string(params, "id"))


async def _save(state: Any, params: dict[str, Any]) -> Any:
    _reject_unsupported(params, {"id", "revision", "draft"}, "decision.save")
    identifier = _required_string(params, "id") if "id" in params else None
    revision = _integer(params, "revision") if identifier else None
    if not identifier and "revision" in params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "New experiments do not have a revision")
    return await state.runtime.decisions.save_experiment(params.get("draft"), identifier, revision)


async def _delete(state: Any, params: dict[str, Any]) -> Any:
    _reject_unsupported(params, {"id", "revision"}, "decision.delete")
    await state.runtime.decisions.delete_experiment(
        _required_string(params, "id"), _integer(params, "revision")
    )
    return {"deleted": True}


async def _history(state: Any, params: dict[str, Any]) -> Any:
    _reject_unsupported(params, {"id", "before"}, "decision.history")
    return await state.runtime.decisions.history(
        _required_string(params, "id"), _integer(params, "before") if "before" in params else None
    )


async def _start(state: Any, params: dict[str, Any]) -> Any:
    _reject_unsupported(params, {"id", "revision", "request_id", "mode"}, "decision.start")
    return await state.runtime.decisions.start(
        _required_string(params, "id"),
        _integer(params, "revision"),
        _required_string(params, "request_id"),
        params.get("mode", "evaluate"),
    )


async def _evaluation(state: Any, params: dict[str, Any]) -> Any:
    _reject_unsupported(params, {"id"}, "decision.result")
    return await state.runtime.decisions.evaluation(_required_string(params, "id"))


async def _cancel(state: Any, params: dict[str, Any]) -> Any:
    _reject_unsupported(params, {"id"}, "decision.cancel")
    return await state.runtime.decisions.cancel(_required_string(params, "id"))


async def _evaluate(state: Any, params: dict[str, Any]) -> Any:
    _reject_unsupported(params, {"state", "questions"}, "decision.evaluate")
    return await state.runtime.decisions.evaluate(params.get("state"), params.get("questions"))


def _mapped(handler: Callable[[Any, dict[str, Any]], Awaitable[Any]]) -> RpcMethodHandler:
    async def wrapped(state: Any, params: dict[str, Any]) -> Any:
        try:
            return await handler(state, params)
        except DecisionError as exc:
            raise RpcError(exc.code, str(exc)) from exc

    return wrapped


def method_handlers() -> dict[str, RpcMethodHandler]:
    return {
        name: _mapped(handler)
        for name, handler in {
            "decision.list": _list,
            "decision.get": _get,
            "decision.save": _save,
            "decision.delete": _delete,
            "decision.history": _history,
            "decision.start": _start,
            "decision.result": _evaluation,
            "decision.cancel": _cancel,
            "decision.evaluate": _evaluate,
        }.items()
    }
