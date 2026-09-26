"""Live voice RPCs: status, call start/stop, and the owner's UI request answers."""

from __future__ import annotations

import unicodedata
from typing import Any

from core.model_tasks.live import LIVE_MEDIA_KINDS, LiveStartRejected
from server.live import LiveCallRegistry, LiveRegistryClosedError
from server.rpc.dispatcher import RpcMethodHandler
from server.rpc.errors import RPC_ERROR_INVALID_REQUEST, RpcError
from server.rpc.validation import _reject_unsupported, _required_string

JsonObject = dict[str, Any]

LIVE_UI_ERROR_MAX_CHARS = 64
LIVE_WAKE_PHRASES_MAX = 8
LIVE_WAKE_PHRASE_MAX_CHARS = 60
# Wake phrases are quoted into the voice instructions: no control or format
# characters, line or paragraph separators, or unpaired surrogates.
_WAKE_PHRASE_REJECTED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})


def _registry(state: Any) -> LiveCallRegistry:
    registry: LiveCallRegistry = state.live_calls
    return registry


def _status(state: Any, params: JsonObject) -> JsonObject:
    """Report whether a ``live_voice`` binding is configured and usable."""
    _reject_unsupported(params, set(), "live.status")
    status: JsonObject = state.runtime.live_voice.status()
    return status


async def _start(state: Any, params: JsonObject) -> JsonObject:
    """Start a call with the accessor's media kind, replacing any active call.

    ``{"media": "webrtc", "sdp"}`` carries the accessor's SDP offer;
    ``{"media": "relay"}`` relays audio over the owner socket. Optional
    ``wake_phrases`` name the phrases that address other vBot Agents during
    the call, so the voice model ignores speech starting with them.
    """
    _reject_unsupported(params, {"media", "sdp", "wake_phrases"}, "live.start")
    media = _required_string(params, "media")
    if media not in LIVE_MEDIA_KINDS:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.media must be one of: {', '.join(sorted(LIVE_MEDIA_KINDS))}",
        )
    offer_sdp: str | None = None
    if media == "webrtc":
        offer_sdp = _required_string(params, "sdp")
    elif "sdp" in params:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, f"params.sdp is not used with media {media}")
    wake_phrases = _optional_wake_phrases(params)
    try:
        call = await _registry(state).start(
            state.runtime.live_voice, media=media, offer_sdp=offer_sdp, wake_phrases=wake_phrases
        )
    except LiveStartRejected as exc:
        return {"error": exc.code}
    except LiveRegistryClosedError as exc:
        raise RpcError(RPC_ERROR_INVALID_REQUEST, "The server is shutting down") from exc
    return {"call_id": call.id, "media": call.media}


def _optional_wake_phrases(params: JsonObject) -> tuple[str, ...]:
    """Read ``wake_phrases``: absent or empty means none.

    Each phrase is trimmed; case-insensitive duplicates keep the first spelling.
    """
    if "wake_phrases" not in params:
        return ()
    value = params["wake_phrases"]
    if not isinstance(value, list) or len(value) > LIVE_WAKE_PHRASES_MAX:
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            f"params.wake_phrases must be an array of at most {LIVE_WAKE_PHRASES_MAX} strings",
        )
    phrases: dict[str, str] = {}
    for index, item in enumerate(value):
        field = f"params.wake_phrases[{index}]"
        if not isinstance(item, str):
            raise RpcError(RPC_ERROR_INVALID_REQUEST, f"{field} must be a string")
        phrase = item.strip()
        if not 1 <= len(phrase) <= LIVE_WAKE_PHRASE_MAX_CHARS:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"{field} must have 1 to {LIVE_WAKE_PHRASE_MAX_CHARS} characters after trimming",
            )
        if any(unicodedata.category(char) in _WAKE_PHRASE_REJECTED_CATEGORIES for char in phrase):
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST, f"{field} must not contain control characters"
            )
        phrases.setdefault(phrase.casefold(), phrase)
    return tuple(phrases.values())


def _stop(state: Any, params: JsonObject) -> JsonObject:
    """Close a call gracefully in the background."""
    _reject_unsupported(params, {"call_id"}, "live.stop")
    call_id = _required_string(params, "call_id")
    return {"stopping": _registry(state).stop(call_id)}


def _ui_result(state: Any, params: JsonObject) -> JsonObject:
    """Answer one UI request with either a result object or an error code."""
    _reject_unsupported(params, {"call_id", "request_id", "result", "error"}, "live.ui_result")
    call_id = _required_string(params, "call_id")
    request_id = _required_string(params, "request_id")
    if ("result" in params) == ("error" in params):
        raise RpcError(
            RPC_ERROR_INVALID_REQUEST,
            "live.ui_result requires exactly one of params.result or params.error",
        )
    result: JsonObject | None = None
    error: str | None = None
    if "result" in params:
        result = params["result"]
        if not isinstance(result, dict):
            raise RpcError(RPC_ERROR_INVALID_REQUEST, "params.result must be an object")
    else:
        error = _required_string(params, "error")
        if len(error) > LIVE_UI_ERROR_MAX_CHARS:
            raise RpcError(
                RPC_ERROR_INVALID_REQUEST,
                f"params.error must be at most {LIVE_UI_ERROR_MAX_CHARS} characters",
            )
    accepted = _registry(state).resolve_ui_request(call_id, request_id, result=result, error=error)
    return {"accepted": accepted}


def method_handlers() -> dict[str, RpcMethodHandler]:
    return {
        "live.status": _status,
        "live.start": _start,
        "live.stop": _stop,
        "live.ui_result": _ui_result,
    }
