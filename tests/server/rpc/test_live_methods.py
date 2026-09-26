"""Live voice RPCs: status, start/stop and the owner's UI request answers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.model_tasks.live import LiveStartRejected
from server.live import LiveRegistryClosedError
from server.rpc.errors import RpcError
from server.rpc.live_methods import _start, _status, _stop, _ui_result
from server.rpc.methods import build_method_handlers

JsonObject = dict[str, Any]


class FakeRegistry:
    def __init__(self) -> None:
        self.starts: list[tuple[Any, str, str | None, tuple[str, ...]]] = []
        self.stops: list[str] = []
        self.answers: list[tuple[str, str, JsonObject | None, str | None]] = []
        self.start_error: Exception | None = None

    async def start(
        self,
        service: Any,
        *,
        media: str,
        offer_sdp: str | None = None,
        wake_phrases: tuple[str, ...] = (),
    ) -> Any:
        self.starts.append((service, media, offer_sdp, wake_phrases))
        if self.start_error is not None:
            raise self.start_error
        if media == "relay":
            return SimpleNamespace(id="call-1", media={"type": "relay", "audio": {}})
        return SimpleNamespace(id="call-1", media={"type": "webrtc", "sdp": "answer"})

    def stop(self, call_id: str) -> bool:
        self.stops.append(call_id)
        return call_id == "call-1"

    def resolve_ui_request(
        self,
        call_id: str,
        request_id: str,
        *,
        result: JsonObject | None = None,
        error: str | None = None,
    ) -> bool:
        self.answers.append((call_id, request_id, result, error))
        return True


def state() -> Any:
    service = SimpleNamespace(
        status=lambda: {"configured": True, "usable": False, "target": "openai/x::api-key"}
    )
    return SimpleNamespace(runtime=SimpleNamespace(live_voice=service), live_calls=FakeRegistry())


def test_status_reports_the_live_voice_binding() -> None:
    assert _status(state(), {}) == {
        "configured": True,
        "usable": False,
        "target": "openai/x::api-key",
    }
    with pytest.raises(RpcError):
        _status(state(), {"extra": True})


@pytest.mark.asyncio
async def test_start_passes_the_offer_to_the_registry_with_the_live_voice_service() -> None:
    current = state()
    assert await _start(current, {"media": "webrtc", "sdp": "v=0 offer"}) == {
        "call_id": "call-1",
        "media": {"type": "webrtc", "sdp": "answer"},
    }
    assert current.live_calls.starts == [(current.runtime.live_voice, "webrtc", "v=0 offer", ())]


@pytest.mark.asyncio
async def test_start_a_relay_call_without_an_offer() -> None:
    current = state()
    assert await _start(current, {"media": "relay"}) == {
        "call_id": "call-1",
        "media": {"type": "relay", "audio": {}},
    }
    assert current.live_calls.starts == [(current.runtime.live_voice, "relay", None, ())]


@pytest.mark.asyncio
async def test_start_returns_a_rejection_code_as_its_result() -> None:
    current = state()
    current.live_calls.start_error = LiveStartRejected("access_denied", "private detail")
    assert await _start(current, {"media": "webrtc", "sdp": "v=0"}) == {"error": "access_denied"}


@pytest.mark.asyncio
async def test_start_is_refused_while_the_server_shuts_down() -> None:
    current = state()
    current.live_calls.start_error = LiveRegistryClosedError()
    with pytest.raises(RpcError) as exc_info:
        await _start(current, {"media": "relay"})
    assert exc_info.value.code == "invalid_request"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {},
        {"sdp": "v=0"},
        {"media": "webrtc"},
        {"media": "webrtc", "sdp": ""},
        {"media": "webrtc", "sdp": 1},
        {"media": "webrtc", "sdp": "v=0", "model": "x"},
        {"media": "relay", "sdp": "v=0"},
        {"media": "sip"},
        {"media": 1},
    ],
)
async def test_start_rejects_invalid_params(params: JsonObject) -> None:
    current = state()
    with pytest.raises(RpcError):
        await _start(current, params)
    assert current.live_calls.starts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wake_phrases", "passed"),
    [
        ([], ()),
        (["  Hey Nabu ", "Hey Jarvis"], ("Hey Nabu", "Hey Jarvis")),
        (["Hey Nabu", "hey nabu", "Hey Jarvis", " HEY NABU"], ("Hey Nabu", "Hey Jarvis")),
        ([f"Phrase {index}" for index in range(8)], tuple(f"Phrase {index}" for index in range(8))),
        (["x" * 60], ("x" * 60,)),
        (["Hallo, Jürgen!", 'Say "go"'], ("Hallo, Jürgen!", 'Say "go"')),
    ],
    ids=["empty", "trimmed", "duplicates-keep-first", "eight", "sixty-chars", "printable"],
)
async def test_start_passes_normalized_wake_phrases(
    wake_phrases: list[str], passed: tuple[str, ...]
) -> None:
    current = state()
    await _start(current, {"media": "relay", "wake_phrases": wake_phrases})
    assert current.live_calls.starts == [(current.runtime.live_voice, "relay", None, passed)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wake_phrases",
    [
        "Hey Nabu",
        None,
        {"phrase": "Hey Nabu"},
        [1],
        [None],
        [["Hey Nabu"]],
        [f"Phrase {index}" for index in range(9)],
        ["x" * 61],
        [""],
        ["   "],
        ["Hey\nNabu"],
        ["Hey\tNabu"],
        ["Hey\x00Nabu"],
        ["Hey\u200bNabu"],
        ["Hey\u2028Nabu"],
        ["Hey" + chr(0xD800) + "Nabu"],
    ],
    ids=[
        "string",
        "null",
        "object",
        "number-item",
        "null-item",
        "nested-array",
        "too-many",
        "too-long",
        "empty",
        "blank",
        "newline",
        "tab",
        "nul",
        "format-character",
        "line-separator",
        "lone-surrogate",
    ],
)
async def test_start_rejects_invalid_wake_phrases(wake_phrases: Any) -> None:
    current = state()
    with pytest.raises(RpcError) as exc_info:
        await _start(current, {"media": "relay", "wake_phrases": wake_phrases})
    assert exc_info.value.code == "invalid_request"
    assert "params.wake_phrases" in exc_info.value.message
    assert current.live_calls.starts == []


def test_stop_reports_whether_a_call_is_stopping() -> None:
    current = state()
    assert _stop(current, {"call_id": "call-1"}) == {"stopping": True}
    assert _stop(current, {"call_id": "other"}) == {"stopping": False}
    with pytest.raises(RpcError):
        _stop(current, {})


def test_ui_result_forwards_a_result_or_an_error_code() -> None:
    current = state()
    ids = {"call_id": "call-1", "request_id": "ui-1"}
    assert _ui_result(current, {**ids, "result": {"applied": True}}) == {"accepted": True}
    assert _ui_result(current, {**ids, "error": "unknown_view"}) == {"accepted": True}
    assert current.live_calls.answers == [
        ("call-1", "ui-1", {"applied": True}, None),
        ("call-1", "ui-1", None, "unknown_view"),
    ]


@pytest.mark.parametrize(
    "params",
    [
        {"request_id": "ui-1", "result": {}},
        {"call_id": "call-1", "result": {}},
        {"call_id": "call-1", "request_id": "ui-1"},
        {"call_id": "call-1", "request_id": "ui-1", "result": {}, "error": "x"},
        {"call_id": "call-1", "request_id": "ui-1", "result": ["applied"]},
        {"call_id": "call-1", "request_id": "ui-1", "error": ""},
        {"call_id": "call-1", "request_id": "ui-1", "error": {"code": "x"}},
        {"call_id": "call-1", "request_id": "ui-1", "error": "x" * 65},
        {"call_id": "call-1", "request_id": "ui-1", "result": {}, "extra": 1},
    ],
)
def test_ui_result_rejects_invalid_answers(params: JsonObject) -> None:
    current = state()
    with pytest.raises(RpcError):
        _ui_result(current, params)
    assert current.live_calls.answers == []


def test_live_methods_are_registered() -> None:
    methods = build_method_handlers()
    assert methods["live.status"] is _status
    assert methods["live.start"] is _start
    assert methods["live.stop"] is _stop
    assert methods["live.ui_result"] is _ui_result
    assert "live.create" not in methods
