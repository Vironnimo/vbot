"""Task model methods."""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from core.model_tasks import (
    TaskModelError,
    validate_task_type,
)
from core.settings import (
    SettingsValidationError,
    parse_settings_update,
)
from core.utils.logging import get_logger
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import (
    _reject_unsupported,
    _required_string,
)

JsonObject = dict[str, Any]

_LOGGER = get_logger("server.rpc.settings")


def _task_model_settings(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "task_model.settings does not accept params")
    try:
        return {"model_tasks": state.runtime.model_tasks.settings()}
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _task_model_update(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"model_tasks"}, "task_model.update")
    try:
        settings_update = parse_settings_update({"model_tasks": params.get("model_tasks")})
    except SettingsValidationError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc

    try:
        previous = state.runtime.model_tasks.settings()
        model_tasks = state.runtime.model_tasks.update(settings_update["model_tasks"])
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    changed_tasks = sorted(
        task_type
        for task_type in set(previous) | set(model_tasks)
        if previous.get(task_type) != model_tasks.get(task_type)
    )
    if changed_tasks:
        _LOGGER.info("Task Model bindings updated (tasks=%s)", ",".join(changed_tasks))
    return {"model_tasks": model_tasks}


def _task_model_list_targets(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"task_type"}, "task_model.list_targets")
    task_type = _required_string(params, "task_type")
    try:
        targets = state.runtime.model_tasks.list_targets(task_type)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"targets": [target.to_dict() for target in targets]}


def _task_model_status(state: Any, params: JsonObject) -> JsonObject:
    """Report whether one configured Task Model is currently executable."""

    _reject_unsupported(params, {"task_type"}, "task_model.status")
    task_type = _required_string(params, "task_type")
    try:
        normalized_task_type = validate_task_type(task_type)
        try:
            state.runtime.model_tasks.binding_for(normalized_task_type)
        except TaskModelError:
            configured = False
        else:
            configured = True
        usable = (
            state.runtime.model_tasks.binding_is_usable(normalized_task_type)
            if configured
            else False
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {
        "task_type": normalized_task_type,
        "configured": configured,
        "usable": usable,
    }


def _task_model_options(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"task_type", "target"}, "task_model.options")
    task_type = _required_string(params, "task_type")
    target = params.get("target")
    if target is not None and (not isinstance(target, str) or not target.strip()):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.target must be a non-empty string")
    try:
        binding = None
        if target is None:
            binding = state.runtime.model_tasks.binding_for(task_type)
            target = binding.target
        else:
            target = target.strip()
        schema = state.runtime.model_tasks.options(task_type, target)
        if binding is None:
            with suppress(TaskModelError):
                configured = state.runtime.model_tasks.binding_for(task_type)
                if configured.target == target:
                    binding = configured
        configured_options = dict(binding.options) if binding is not None else {}
        effective_options = schema.default_options()
        effective_options.update(configured_options)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    payload = schema.to_dict()
    payload["configured_options"] = configured_options
    payload["effective_options"] = effective_options
    return {"schema": payload}


def _task_model_patch_options(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"task_type", "set", "unset"}, "task_model.patch_options")
    task_type = _required_string(params, "task_type")
    set_values = params.get("set", {})
    unset_names = params.get("unset", [])
    if not isinstance(set_values, dict):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.set must be an object")
    if not isinstance(unset_names, list) or not all(
        isinstance(name, str) and name.strip() for name in unset_names
    ):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "params.unset must be an array of non-empty strings",
        )
    normalized_unsets = tuple(name.strip() for name in unset_names)
    overlap = sorted(set(set_values) & set(normalized_unsets))
    if overlap:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"options cannot be set and unset together: {', '.join(overlap)}",
        )
    if not set_values and not normalized_unsets:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "at least one option must be set or unset")
    try:
        previous = state.runtime.model_tasks.settings()
        model_tasks = state.runtime.model_tasks.patch_options(
            task_type,
            set_values=set_values,
            unset_names=normalized_unsets,
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if previous.get(task_type) != model_tasks.get(task_type):
        _LOGGER.info("Task Model options updated (task=%s)", task_type)
    return {"model_tasks": model_tasks}


def _local_speech_setup_status(state: Any, params: JsonObject) -> JsonObject:
    setup = _speech_setup(state, params)
    return {
        **setup.status(),
        "restart_available": state.request_restart is not None,
    }


def _local_speech_setup_install(state: Any, params: JsonObject) -> JsonObject:
    setup = _speech_setup(state, params)
    return {
        **setup.install(),
        "restart_available": state.request_restart is not None,
    }


def _local_speech_setup_restart(state: Any, params: JsonObject) -> JsonObject:
    setup = _speech_setup(state, params)
    if setup.status()["state"] != "restart_required":
        return {"state": "failed", "error": "setup_not_finished"}
    if state.request_restart is None:
        return {"state": "failed", "error": "restart_unavailable"}
    try:
        state.request_restart()
    except Exception:
        _LOGGER.warning("Local speech setup could not schedule server restart")
        return {"state": "failed", "error": "restart_unavailable"}
    return {"state": "restarting"}


def _local_speech_memory_status(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, set(), "speech.local_memory_status")
    return dict(state.runtime.speech.local_memory_status())


async def _local_speech_unload(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"target"}, "speech.local_unload")
    target = params.get("target")
    if not isinstance(target, str) or not target:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "target must be a non-empty string")
    try:
        return dict(await state.runtime.speech.unload_local(target))
    except ValueError as error:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(error)) from error


def _speech_setup(state: Any, params: JsonObject) -> Any:
    _reject_unsupported(params, {"target"}, "speech.local_setup")
    target = params.get("target", "")
    if not isinstance(target, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "target must be a string")
    if not target:
        return state.runtime.speech.local_setup
    try:
        return state.runtime.speech.local_setup_for(target)
    except ValueError as error:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(error)) from error


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the registered task-model RPC handlers."""
    return {
        "speech.local_memory_status": _local_speech_memory_status,
        "speech.local_unload": _local_speech_unload,
        "speech.local_setup_status": _local_speech_setup_status,
        "speech.local_setup_install": _local_speech_setup_install,
        "speech.local_setup_restart": _local_speech_setup_restart,
        "task_model.settings": _task_model_settings,
        "task_model.update": _task_model_update,
        "task_model.list_targets": _task_model_list_targets,
        "task_model.status": _task_model_status,
        "task_model.options": _task_model_options,
        "task_model.patch_options": _task_model_patch_options,
    }
