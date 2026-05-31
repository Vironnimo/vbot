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


def _list_prompts(state: Any) -> JsonObject:
    try:
        fragments = PromptFragmentManager(state.runtime.storage).list_fragments()
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    return {"fragments": fragments}


def _update_prompt(state: Any, params: JsonObject) -> JsonObject:
    name = _required_string(params, "name")
    content = params.get("content")
    if not isinstance(content, str):
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.content must be a string")
    try:
        return PromptFragmentManager(state.runtime.storage).update_fragment(name, content)
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


def _reset_prompt(state: Any, params: JsonObject) -> JsonObject:
    name = _required_string(params, "name")
    try:
        return PromptFragmentManager(state.runtime.storage).reset_fragment(name)
    except PromptError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, str(exc)) from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc


async def _preview_prompt(state: Any, params: JsonObject) -> JsonObject:
    agent_id = _required_string(params, "agent_id")
    try:
        agent = state.runtime.agents.get(agent_id)
    except KeyError as exc:
        raise RpcError(RPC_ERROR_DOMAIN, f"agent not found: {agent_id}") from exc
    except Exception as exc:
        raise _map_expected_error(exc) from exc
    try:
        prompt_manager = state.runtime.system_prompts
        text = prompt_manager.build_system_prompt(agent)
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


def method_handlers() -> dict[str, RpcMethodHandler]:
    """Return log and prompt RPC handlers."""

    def list_prompts(state: Any, _params: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], _list_prompts(state))

    return {
        "log.list": _list_logs,
        "log.read": _read_log,
        "prompt.list": list_prompts,
        "prompt.update": _update_prompt,
        "prompt.reset": _reset_prompt,
        "prompt.preview": _preview_prompt,
    }
