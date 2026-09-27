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
@pytest.mark.parametrize(
    ("params", "offer", "media"),
    [
        pytest.param(
            {"media": "webrtc", "sdp": "v=0 offer"},
            "v=0 offer",
            {"type": "webrtc", "sdp": "answer"},
            id="webrtc-offer",
        ),
        pytest.param({"media": "relay"}, None, {"type": "relay", "audio": {}}, id="relay"),
    ],
)
async def test_start_passes_the_media_request_to_the_registry_with_the_live_voice_service(
    params: JsonObject, offer: str | None, media: JsonObject
) -> None:
    current = state()
    assert await _start(current, params) == {"call_id": "call-1", "media": media}
    assert current.live_calls.starts == [(current.runtime.live_voice, params["media"], offer, ())]


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
        pytest.param({}, id="missing-media"),
        pytest.param({"media": 1}, id="non-string-media"),
        pytest.param({"media": "sip"}, id="unknown-media"),
        pytest.param({"media": "webrtc"}, id="webrtc-without-offer"),
        pytest.param({"media": "webrtc", "sdp": ""}, id="webrtc-empty-offer"),
        pytest.param({"media": "relay", "sdp": "v=0"}, id="relay-with-offer"),
        pytest.param({"media": "webrtc", "sdp": "v=0", "model": "x"}, id="unsupported-field"),
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
        pytest.param([], (), id="empty"),
        # Phrases are trimmed; case-insensitive duplicates keep the first spelling.
        pytest.param(
            ["  Hey Nabu ", "hey nabu", "Hey Jarvis", " HEY NABU"],
            ("Hey Nabu", "Hey Jarvis"),
            id="trimmed-and-deduplicated",
        ),
        pytest.param(
            [f"Phrase {index}" for index in range(7)] + ["x" * 60],
            (*(f"Phrase {index}" for index in range(7)), "x" * 60),
            id="eight-phrases-of-up-to-sixty-chars",
        ),
        pytest.param(
            ["Hallo, Jürgen!", 'Say "go"'],
            ("Hallo, Jürgen!", 'Say "go"'),
            id="printable",
        ),
    ],
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
        pytest.param("Hey Nabu", id="string"),
        pytest.param(None, id="null"),
        pytest.param([1], id="non-string-item"),
        pytest.param([f"Phrase {index}" for index in range(9)], id="too-many"),
        pytest.param(["x" * 61], id="too-long"),
        pytest.param(["   "], id="blank"),
        # Phrases are quoted into the voice instructions.
        pytest.param(["Hey\nNabu"], id="control-character"),
        pytest.param(["Hey\u200bNabu"], id="format-character"),
        pytest.param(["Hey\u2028Nabu"], id="line-separator"),
        pytest.param(["Hey" + chr(0xD800) + "Nabu"], id="lone-surrogate"),
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


_UI_IDS = {"call_id": "call-1", "request_id": "ui-1"}


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"call_id": "call-1", "result": {}}, id="missing-request-id"),
        pytest.param(_UI_IDS, id="neither-result-nor-error"),
        pytest.param({**_UI_IDS, "result": {}, "error": "x"}, id="result-and-error"),
        pytest.param({**_UI_IDS, "result": ["applied"]}, id="non-object-result"),
        pytest.param({**_UI_IDS, "error": {"code": "x"}}, id="non-string-error"),
        pytest.param({**_UI_IDS, "error": "x" * 65}, id="too-long-error"),
        pytest.param({**_UI_IDS, "result": {}, "extra": 1}, id="unsupported-field"),
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
