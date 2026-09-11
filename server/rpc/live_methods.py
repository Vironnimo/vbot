"""Live voice accessor startup; app actions use the existing operator RPCs."""

from __future__ import annotations

from typing import Any

from core.model_tasks.live import BACKEND_MODEL, LIVE_MODEL, LiveClient
from core.model_tasks.model_tasks import parse_task_model_target_id
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderOutcomeUnknownError,
)
from core.utils.logging import get_logger
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import _reject_unsupported, _required_string

_LOGGER = get_logger("server.rpc.live")


def _status(state: Any, params: dict[str, Any]) -> dict[str, Any]:
    _reject_unsupported(params, set(), "live.status")
    usable = state.runtime.provider_credentials.is_usable("openai", "openai:api-key")
    return {"configured": bool(usable), "model": LIVE_MODEL, "backend_model": BACKEND_MODEL}


async def _create(state: Any, params: dict[str, Any]) -> dict[str, Any]:
    _reject_unsupported(params, {"sdp"}, "live.create")
    sdp = _required_string(params, "sdp")
    if not _status(state, {})["configured"]:
        return {"error": "api_key_required"}
    try:
        client = LiveClient.from_runtime(
            state.runtime, parse_task_model_target_id(f"openai/{LIVE_MODEL}::api-key")
        )
        result = await client.create_session(sdp)
    except ValueError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "invalid_sdp") from exc
    except ProviderAuthError:
        _LOGGER.warning("Live voice access rejected by Provider")
        return {"error": "access_denied"}
    except ProviderOutcomeUnknownError:
        _LOGGER.warning("Live voice creation outcome unknown; request was not replayed")
        return {"error": "outcome_unknown"}
    except (ProviderError, NetworkError):
        _LOGGER.warning("Live voice creation failed")
        return {"error": "provider_error"}
    _LOGGER.info("Live voice session created session_id=%s", result["session"]["id"])
    return result


def method_handlers() -> dict[str, RpcMethodHandler]:
    return {"live.status": _status, "live.create": _create}
