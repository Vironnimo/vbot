"""Tests for the Voice server client (``desktop.wakeword.server_client``)."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from desktop.wakeword import server_client as server_client_module
from desktop.wakeword.server_client import (
    DEFAULT_SPEECH_UPLOAD_LIMIT_BYTES,
    MAX_ATTEMPTS,
    VoiceRequestCancelled,
    VoiceServerClient,
    VoiceServerInvalidResponse,
    VoiceServerRejected,
    VoiceServerUnreachable,
)

SERVER = "http://127.0.0.1:8421"
# Captured before the autouse fixture replaces the seam.
_BACKOFF_DELAY = server_client_module._backoff_delay
Reply = httpx.Response | Exception | Callable[[httpx.Request], httpx.Response]


class ScriptedServer:
    """A MockTransport handler that answers requests from a script, in order."""

    def __init__(self, *replies: Reply) -> None:
        self._replies = list(replies)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._replies:
            raise AssertionError(f"unexpected request: {request.method} {request.url}")
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        return reply(request)

    @property
    def rpc_calls(self) -> list[tuple[str, dict[str, Any]]]:
        calls = []
        for request in self.requests:
            body = json.loads(request.content)
            calls.append((body["method"], body["params"]))
        return calls


def _ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def _rpc_error(code: str, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code, json={"ok": False, "error": {"code": code, "message": f"{code} happened"}}
    )


def _connect_error() -> httpx.ConnectError:
    return httpx.ConnectError("connection refused")


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_client_module, "_backoff_delay", lambda _attempt: 0.0)


@pytest.fixture
def cancel() -> threading.Event:
    return threading.Event()


@pytest.fixture
def make_client(cancel: threading.Event) -> Iterator[Callable[[ScriptedServer], VoiceServerClient]]:
    clients: list[VoiceServerClient] = []

    def factory(server: ScriptedServer) -> VoiceServerClient:
        client = VoiceServerClient(
            f"{SERVER}/", cancel=cancel, transport=httpx.MockTransport(server)
        )
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.close()


# -- Construction ----------------------------------------------------------------


def test_client_requires_a_server_url(cancel: threading.Event) -> None:
    with pytest.raises(ValueError):
        VoiceServerClient("  ", cancel=cancel)


def test_client_ignores_environment_proxies(
    cancel: threading.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:9")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:9")

    with VoiceServerClient(f" {SERVER}/ ", cancel=cancel) as client:
        assert client.server_url == SERVER
        assert client._http.trust_env is False


def test_backoff_grows_exponentially_with_jitter_up_to_the_cap() -> None:
    assert 1.0 <= _BACKOFF_DELAY(0) < 2.0
    assert 2.0 <= _BACKOFF_DELAY(1) < 3.0
    assert _BACKOFF_DELAY(10) == server_client_module.MAX_BACKOFF_SECONDS


# -- Speech-to-text readiness ------------------------------------------------------


def test_speech_readiness_reports_a_usable_model(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(_ok({"configured": True, "usable": True}))

    assert make_client(server).speech_readiness() is None
    assert str(server.requests[0].url) == f"{SERVER}/api/rpc"
    assert server.rpc_calls == [("task_model.status", {"task_type": "speech_to_text"})]


@pytest.mark.parametrize(
    ("reply", "problem"),
    [
        (_ok({"configured": False, "usable": False}), "speech_to_text_unconfigured"),
        (_ok({"configured": True, "usable": False}), "speech_to_text_unavailable"),
        (_ok({"configured": True}), "speech_to_text_readiness_failed"),
        (_ok(["not", "an", "object"]), "speech_to_text_readiness_failed"),
        (_rpc_error("invalid_request"), "speech_to_text_readiness_failed"),
        (_rpc_error("internal_error", status_code=500), "speech_to_text_readiness_failed"),
        (httpx.Response(200, content=b"<html>"), "speech_to_text_readiness_failed"),
        (httpx.Response(404), "speech_to_text_readiness_failed"),
    ],
)
def test_speech_readiness_reports_problems(
    make_client: Callable[..., Any], reply: httpx.Response, problem: str
) -> None:
    assert make_client(ScriptedServer(reply)).speech_readiness() == problem


def test_speech_readiness_reports_an_unreachable_server_after_retries(
    make_client: Callable[..., Any],
) -> None:
    server = ScriptedServer(*(_connect_error() for _ in range(MAX_ATTEMPTS)))

    assert make_client(server).speech_readiness() == "server_unreachable"
    assert len(server.requests) == MAX_ATTEMPTS


# -- Agents and Sessions -----------------------------------------------------------


def test_get_agent_retries_a_transport_failure(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(_connect_error(), _ok({"id": "main", "current_session_id": "s1"}))

    assert make_client(server).get_agent("main") == {"id": "main", "current_session_id": "s1"}
    assert server.rpc_calls == [("agent.get", {"id": "main"})] * 2


def test_get_agent_reports_an_unknown_agent(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(_rpc_error("agent_not_found"))

    with pytest.raises(VoiceServerRejected) as raised:
        make_client(server).get_agent("gone")

    assert raised.value.error_code == "target_agent_unavailable"
    assert raised.value.rpc_code == "agent_not_found"
    assert len(server.requests) == 1


def test_active_session_uses_the_agents_current_session(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(_ok({"current_session_id": "current-one"}))

    assert make_client(server).resolve_session("main", "active") == "current-one"
    assert server.rpc_calls == [("agent.get", {"id": "main"})]


def test_active_session_falls_back_to_the_newest_conversation(
    make_client: Callable[..., Any],
) -> None:
    server = ScriptedServer(
        _ok({"current_session_id": None}),
        _ok(
            {
                "sessions": [
                    {"id": "older", "last_active_at": "2026-05-30T10:00:00+00:00"},
                    {"id": "newer", "last_active_at": "2026-05-31T10:00:00+00:00"},
                    {"id": "", "last_active_at": "2026-06-30T10:00:00+00:00"},
                    "garbage",
                ]
            }
        ),
    )

    assert make_client(server).resolve_session("main", "active") == "newer"
    assert server.rpc_calls == [
        ("agent.get", {"id": "main"}),
        (
            "session.list",
            {
                "agent_id": "main",
                "limit": 1,
                "include_subagents": False,
                "include_memory_reflections": False,
                "include_skill_reflections": False,
                "include_cron": False,
                "include_channels": False,
            },
        ),
    ]


def test_active_session_creates_one_when_the_agent_has_none(
    make_client: Callable[..., Any],
) -> None:
    server = ScriptedServer(
        _ok({"current_session_id": ""}),
        _ok({"sessions": []}),
        _ok({"agent_id": "main", "session_id": "fresh"}),
    )

    assert make_client(server).resolve_session("main", "active") == "fresh"
    assert server.rpc_calls[-1] == ("session.create", {"agent_id": "main", "make_current": True})


def test_new_session_is_created_and_made_current(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(_ok({"agent_id": "main", "session_id": "fresh"}))

    assert make_client(server).resolve_session("main", "new") == "fresh"
    assert server.rpc_calls == [("session.create", {"agent_id": "main", "make_current": True})]


def test_session_creation_is_never_repeated_after_a_transport_failure(
    make_client: Callable[..., Any],
) -> None:
    server = ScriptedServer(httpx.ReadTimeout("response lost"))

    with pytest.raises(VoiceServerUnreachable) as raised:
        make_client(server).resolve_session("main", "new")

    assert raised.value.error_code == "server_unreachable"
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    ("replies", "error_type", "error_code"),
    [
        ([_rpc_error("invalid_request")], VoiceServerRejected, "session_resolution_failed"),
        ([_rpc_error("agent_not_found")], VoiceServerRejected, "target_agent_unavailable"),
        ([_ok({"agent_id": "main"})], VoiceServerInvalidResponse, "session_resolution_failed"),
        ([httpx.Response(503)], VoiceServerUnreachable, "server_unreachable"),
    ],
)
def test_new_session_failures_carry_stable_codes(
    make_client: Callable[..., Any],
    replies: list[httpx.Response],
    error_type: type[Exception],
    error_code: str,
) -> None:
    server = ScriptedServer(*replies)

    with pytest.raises(error_type) as raised:
        make_client(server).resolve_session("main", "new")

    assert getattr(raised.value, "error_code", None) == error_code
    assert len(server.requests) == 1


def test_active_session_rejects_a_malformed_session_list(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(_ok({}), _ok({"sessions": "none"}))

    with pytest.raises(VoiceServerInvalidResponse) as raised:
        make_client(server).resolve_session("main", "active")

    assert raised.value.error_code == "session_resolution_failed"


def test_resolve_session_rejects_an_unknown_behavior(make_client: Callable[..., Any]) -> None:
    with pytest.raises(ValueError):
        make_client(ScriptedServer()).resolve_session("main", "later")


def test_reads_retry_retryable_statuses(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(httpx.Response(503), httpx.Response(429), _ok({"id": "main"}))

    assert make_client(server).get_agent("main") == {"id": "main"}
    assert len(server.requests) == 3


def test_reads_report_an_unreachable_server_when_retries_run_out(
    make_client: Callable[..., Any],
) -> None:
    server = ScriptedServer(*(httpx.Response(502) for _ in range(MAX_ATTEMPTS)))

    with pytest.raises(VoiceServerUnreachable):
        make_client(server).get_agent("main")
    assert len(server.requests) == MAX_ATTEMPTS


# -- Sending ---------------------------------------------------------------------


def test_send_command_streams_the_text_as_spoken_input(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(_ok({"run_id": "run-one", "sse_url": "/api/runs/run-one/events"}))

    make_client(server).send_command("main", "session-one", "hello")

    assert server.rpc_calls == [
        (
            "chat.stream",
            {
                "agent_id": "main",
                "session_id": "session-one",
                "content": "hello",
                "input_origin": "speech_transcription",
            },
        )
    ]


@pytest.mark.parametrize(
    ("reply", "error_type", "error_code"),
    [
        (_connect_error(), VoiceServerUnreachable, "server_unreachable"),
        (httpx.Response(503), VoiceServerUnreachable, "server_unreachable"),
        (_rpc_error("session_busy"), VoiceServerRejected, "send_failed"),
        (_rpc_error("internal_error", status_code=500), VoiceServerRejected, "send_failed"),
        (httpx.Response(200, json={"ok": True}), VoiceServerInvalidResponse, "send_failed"),
    ],
)
def test_send_command_is_attempted_once(
    make_client: Callable[..., Any],
    reply: Reply,
    error_type: type[Exception],
    error_code: str,
) -> None:
    server = ScriptedServer(reply)

    with pytest.raises(error_type) as raised:
        make_client(server).send_command("main", "session-one", "hello")

    assert getattr(raised.value, "error_code", None) == error_code
    assert len(server.requests) == 1


def test_rejections_keep_status_and_rpc_code(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(_rpc_error("internal_error", status_code=500))

    with pytest.raises(VoiceServerRejected) as raised:
        make_client(server).send_command("main", "session-one", "hello")

    assert raised.value.status_code == 500
    assert raised.value.rpc_code == "internal_error"


# -- Transcription -----------------------------------------------------------------


def test_transcribe_uploads_the_recording(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(httpx.Response(200, json={"text": "turn on the lights"}))

    transcript = make_client(server).transcribe(b"RIFF-audio")

    assert transcript == "turn on the lights"
    request = server.requests[0]
    assert str(request.url) == f"{SERVER}/api/speech/transcribe"
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert b'name="file"; filename="recording.wav"' in request.content
    assert b"Content-Type: audio/wav" in request.content
    assert b"RIFF-audio" in request.content


def test_transcribe_accepts_another_container(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(httpx.Response(200, json={"text": ""}))

    transcript = make_client(server).transcribe(
        b"fLaC", filename="recording.flac", media_type="audio/flac"
    )

    assert transcript == ""
    assert b'filename="recording.flac"' in server.requests[0].content
    assert b"Content-Type: audio/flac" in server.requests[0].content


def test_transcribe_retries_retryable_failures(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(
        httpx.Response(503),
        _connect_error(),
        httpx.Response(200, json={"text": "hi"}),
    )

    assert make_client(server).transcribe(b"audio") == "hi"
    assert len(server.requests) == 3


def test_transcribe_reports_a_persistent_provider_failure(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(*(httpx.Response(502, text="provider down") for _ in range(3)))

    with pytest.raises(VoiceServerRejected) as raised:
        make_client(server).transcribe(b"audio")

    assert raised.value.error_code == "transcription_failed"
    assert raised.value.status_code == 502
    assert "provider down" in str(raised.value)
    assert len(server.requests) == MAX_ATTEMPTS


@pytest.mark.parametrize("status_code", [400, 409, 413, 422])
def test_transcribe_does_not_retry_a_rejected_upload(
    make_client: Callable[..., Any], status_code: int
) -> None:
    server = ScriptedServer(httpx.Response(status_code))

    with pytest.raises(VoiceServerRejected) as raised:
        make_client(server).transcribe(b"audio")

    assert raised.value.error_code == "transcription_failed"
    assert raised.value.status_code == status_code
    assert len(server.requests) == 1


def test_transcribe_does_not_repeat_an_upload_after_a_read_timeout(
    make_client: Callable[..., Any],
) -> None:
    server = ScriptedServer(httpx.ReadTimeout("still transcribing"))

    with pytest.raises(VoiceServerUnreachable):
        make_client(server).transcribe(b"audio")
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"transcript": "old key"}),
        httpx.Response(200, json=["text"]),
    ],
)
def test_transcribe_rejects_an_invalid_success_response(
    make_client: Callable[..., Any], reply: httpx.Response
) -> None:
    with pytest.raises(VoiceServerInvalidResponse) as raised:
        make_client(ScriptedServer(reply)).transcribe(b"audio")

    assert raised.value.error_code == "transcription_failed"


# -- Upload budget -------------------------------------------------------------------


def test_upload_budget_follows_the_server_limit_once(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(_ok({"setting": {"value": 1_000_044}}))
    client = make_client(server)

    assert client.upload_budget_bytes() == 900_000
    assert client.upload_budget_bytes() == 900_000
    assert server.rpc_calls == [("settings.get_path", {"path": "speech.upload_max_size_bytes"})]


@pytest.mark.parametrize(
    "replies",
    [
        [_rpc_error("invalid_request")],
        [_ok({"setting": {"value": "big"}})],
        [_ok({"setting": {"value": True}})],
        [_ok({"setting": {"value": 44}})],
        [_ok({})],
        [_connect_error() for _ in range(MAX_ATTEMPTS)],
    ],
)
def test_upload_budget_falls_back_to_the_default_limit(
    make_client: Callable[..., Any], replies: list[Reply]
) -> None:
    client = make_client(ScriptedServer(*replies))

    assert client.upload_budget_bytes() == int((DEFAULT_SPEECH_UPLOAD_LIMIT_BYTES - 44) * 0.9)


# -- Cancellation --------------------------------------------------------------------


def test_a_cancelled_client_sends_nothing(
    make_client: Callable[..., Any], cancel: threading.Event
) -> None:
    server = ScriptedServer()
    client = make_client(server)
    cancel.set()

    with pytest.raises(VoiceRequestCancelled):
        client.get_agent("main")
    with pytest.raises(VoiceRequestCancelled):
        client.upload_budget_bytes()
    with pytest.raises(VoiceRequestCancelled):
        client.speech_readiness()
    assert server.requests == []


def test_cancellation_interrupts_a_retry_backoff(
    make_client: Callable[..., Any], cancel: threading.Event
) -> None:
    def fail_and_cancel(_request: httpx.Request) -> httpx.Response:
        cancel.set()
        return httpx.Response(503)

    server = ScriptedServer(fail_and_cancel)

    with pytest.raises(VoiceRequestCancelled):
        make_client(server).transcribe(b"audio")
    assert len(server.requests) == 1
