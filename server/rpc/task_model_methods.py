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
    parse_settings_update_base,
)
from core.utils.logging import get_logger
from server.rpc._mutations import serialized_mutation
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


async def _task_model_update(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, {"model_tasks", "base"}, "task_model.update")
    try:
        settings_update = parse_settings_update({"model_tasks": params.get("model_tasks")})
        # Like `settings.update`: the caller's view of the bindings it writes,
        # as `{"model_tasks": {...}}`.
        raw_base = params.get("base")
        base = (
            None
            if raw_base is None
            else parse_settings_update_base(raw_base, settings_update).get("model_tasks")
        )
    except SettingsValidationError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc

    try:
        previous_settings = state.runtime.storage.load_settings()
        previous = state.runtime.model_tasks.settings()
        model_tasks = state.runtime.model_tasks.update(settings_update["model_tasks"], base=base)
        await state.runtime.apply_settings_change(
            previous_settings, state.runtime.storage.load_settings()
        )
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
            binding = state.runtime.model_tasks.binding_for(normalized_task_type)
        except TaskModelError:
            configured = False
        else:
            configured = True
            if binding.target.startswith("local/") and (
                setup := _owned_local_setup(state, binding.target)
            ):
                # Only report the selected engine, never unused optional engines
                # encountered during catalog enumeration. The owner deduplicates
                # repeated readiness checks and reports recovery.
                setup.status(log_unavailable=True)
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


async def _task_model_patch_options(state: Any, params: JsonObject) -> JsonObject:
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
        previous_settings = state.runtime.storage.load_settings()
        previous = state.runtime.model_tasks.settings()
        model_tasks = state.runtime.model_tasks.patch_options(
            task_type,
            set_values=set_values,
            unset_names=normalized_unsets,
        )
        await state.runtime.apply_settings_change(
            previous_settings, state.runtime.storage.load_settings()
        )
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    if previous.get(task_type) != model_tasks.get(task_type):
        _LOGGER.info("Task Model options updated (task=%s)", task_type)
    return {"model_tasks": model_tasks}


def _local_speech_setup_restart(state: Any, params: JsonObject) -> JsonObject:
    """Restart the server once a development checkout's STT packages need it."""
    try:
        setup = state.runtime.speech.local_setup_for(
            _local_target(params, "speech.local_setup_restart")
        )
    except ValueError as error:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(error)) from error
    if setup.status()["state"] != "restart_required":
        return {"state": "failed", "error": "setup_not_finished"}
    if state.request_restart is None:
        return {"state": "failed", "error": "restart_unavailable"}
    try:
        state.request_restart()
    except Exception:
        _LOGGER.warning("Local speech setup could not schedule server restart")
        return {"state": "failed", "error": "restart_unavailable"}
    _LOGGER.info(
        "Server restart requested (reason=local_speech_setup target=%s actor=rpc)",
        params["target"],
    )
    return {"state": "restarting"}


def _speech_prepare_transcription(state: Any, params: JsonObject) -> JsonObject:
    """Start loading the bound local STT model because a transcription is coming."""
    _reject_unsupported(params, set(), "speech.prepare_transcription")
    return {"state": state.runtime.speech.prepare_transcription()}


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


# Services that run local targets, each owning its targets' installations.
_LOCAL_OWNERS = ("speech", "embeddings")


def _owned_local_setup(state: Any, target: str) -> Any | None:
    """The installation behind one local target, from whichever service runs it."""
    for owner in _LOCAL_OWNERS:
        with suppress(ValueError):
            return getattr(state.runtime, owner).local_setup_for(target)
    return None


def _local_target(params: JsonObject, method: str) -> str:
    _reject_unsupported(params, {"target"}, method)
    target = params.get("target")
    if not isinstance(target, str) or not target.startswith("local/"):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "target must be a local target id")
    return target


def _local_setup(state: Any, params: JsonObject, method: str) -> Any:
    setup = _owned_local_setup(state, _local_target(params, method))
    if setup is None:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "Unknown local target")
    return setup


def _local_setup_status(state: Any, params: JsonObject) -> JsonObject:
    """Installation state of one local target's engine, for any local task."""
    setup = _local_setup(state, params, "task_model.local_setup_status")
    return {**setup.status(), "restart_available": state.request_restart is not None}


def _local_setup_install(state: Any, params: JsonObject) -> JsonObject:
    """Start (or join) the fixed installation of one local target's engine."""
    setup = _local_setup(state, params, "task_model.local_setup_install")
    return {**setup.install(), "restart_available": state.request_restart is not None}


async def _local_setup_cancel(state: Any, params: JsonObject) -> JsonObject:
    """Stop a running installation of one local target; finished downloads are kept."""
    setup = _local_setup(state, params, "task_model.local_setup_cancel")
    return {**await setup.cancel(), "restart_available": state.request_restart is not None}


def _local_memory_status(state: Any, params: JsonObject) -> JsonObject:
    _reject_unsupported(params, set(), "task_model.local_memory_status")
    return {
        "models": [
            model
            for owner in _LOCAL_OWNERS
            for model in getattr(state.runtime, owner).local_memory_status()["models"]
        ]
    }


async def _local_unload(state: Any, params: JsonObject) -> JsonObject:
    target = _local_target(params, "task_model.local_unload")
    for owner in _LOCAL_OWNERS:
        with suppress(ValueError):
            result = dict(await getattr(state.runtime, owner).unload_local(target))
            break
    else:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "Unknown local target")
    # One combined view, the same as task_model.local_memory_status.
    return {**_local_memory_status(state, {}), "released": result["released"]}


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return the registered task-model RPC handlers."""
    return {
        "speech.local_memory_status": _local_speech_memory_status,
        "speech.local_unload": _local_speech_unload,
        "speech.local_setup_restart": _local_speech_setup_restart,
        "speech.prepare_transcription": _speech_prepare_transcription,
        "task_model.settings": _task_model_settings,
        "task_model.update": serialized_mutation(
            _task_model_update, lock_attribute="_settings_mutation_lock"
        ),
        "task_model.list_targets": _task_model_list_targets,
        "task_model.status": _task_model_status,
        "task_model.options": _task_model_options,
        "task_model.patch_options": serialized_mutation(
            _task_model_patch_options, lock_attribute="_settings_mutation_lock"
        ),
        "task_model.local_setup_status": _local_setup_status,
        "task_model.local_setup_install": _local_setup_install,
        "task_model.local_setup_cancel": _local_setup_cancel,
        "task_model.local_memory_status": _local_memory_status,
        "task_model.local_unload": _local_unload,
    }
