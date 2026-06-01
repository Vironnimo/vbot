"""Log and prompt RPC handlers."""

from __future__ import annotations

from typing import Any, cast

from core.prompts import PromptError, PromptFragmentManager
from core.utils.log_viewer import LogViewer
from core.utils.tokens import estimate_tokens
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.error_mapping import _map_expected_error
from server.rpc.errors import RPC_ERROR_DOMAIN, RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import _required_string

JsonObject = dict[str, Any]


def _list_logs(state: Any, params: JsonObject) -> JsonObject:
    if params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "log.list does not accept params")
    return _log_viewer(state).list_files()


def _read_log(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"file"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported log read fields: {', '.join(unsupported_fields)}",
        )

    file_name = _required_string(params, "file")
    try:
        return _log_viewer(state).read_file(file_name)
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except FileNotFoundError as exc:
        raise RpcError(RPC_ERROR_DOMAIN, str(exc)) from exc


def _list_prompts(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"scope"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported prompt.list fields: {', '.join(unsupported_fields)}",
        )

    try:
        manager = _prompt_fragment_manager(state)
        fragments = manager.list_fragments(params.get("scope"))
        scopes = manager.list_scopes()
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"fragments": fragments, "scopes": scopes}


def _update_prompt(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"name", "content", "scope"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported prompt.update fields: {', '.join(unsupported_fields)}",
        )

    name = _required_string(params, "name")
    content = params.get("content")
    if not isinstance(content, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.content must be a string")
    try:
        return _prompt_fragment_manager(state).update_fragment(name, content, params.get("scope"))
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _reset_prompt(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"name", "scope"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported prompt.reset fields: {', '.join(unsupported_fields)}",
        )

    name = _required_string(params, "name")
    try:
        return _prompt_fragment_manager(state).reset_fragment(name, params.get("scope"))
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _preview_prompt(state: Any, params: JsonObject) -> JsonObject:
    unsupported_fields = sorted(set(params) - {"agent_id", "scope"})
    if unsupported_fields:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"unsupported prompt.preview fields: {', '.join(unsupported_fields)}",
        )

    scope = params.get("scope")
    try:
        prompt_scope = (
            _prompt_fragment_manager(state).validate_scope(scope) if scope is not None else None
        )
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc

    if prompt_scope is not None and prompt_scope.type == "agent":
        agent_id = cast(str, prompt_scope.agent_id)
    else:
        agent_id = _required_string(params, "agent_id")

    try:
        agent = state.runtime.agents.get(agent_id)
    except KeyError as exc:
        raise RpcError(RPC_ERROR_DOMAIN, f"agent not found: {agent_id}") from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    try:
        prompt_manager = state.runtime.system_prompts
        text = prompt_manager.build_system_prompt(agent, scope=prompt_scope)
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    token_count, estimated = estimate_tokens(text)
    return {"text": text, "tokens": token_count, "estimated": estimated}


def _log_viewer(state: Any) -> LogViewer:
    log_viewer = getattr(state, "log_viewer", None)
    if log_viewer is not None:
        return cast(LogViewer, log_viewer)
    log_viewer = LogViewer(state.runtime.storage.data_dir)
    state.log_viewer = log_viewer
    return log_viewer


def _prompt_fragment_manager(state: Any) -> PromptFragmentManager:
    return PromptFragmentManager(state.runtime.storage, state.runtime.agents)


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return log and prompt RPC handlers."""

    return {
        "log.list": _list_logs,
        "log.read": _read_log,
        "prompt.list": _list_prompts,
        "prompt.update": _update_prompt,
        "prompt.reset": _reset_prompt,
        "prompt.preview": _preview_prompt,
    }
