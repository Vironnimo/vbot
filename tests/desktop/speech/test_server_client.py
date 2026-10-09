"""Tests for the Desktop speech server client (``desktop.speech.server_client``)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from desktop.speech import server_client as server_client_module
from desktop.speech.server_client import (
    DEFAULT_SPEECH_UPLOAD_LIMIT_BYTES,
    MAX_ATTEMPTS,
    SpeechRequestCancelled,
    SpeechServerClient,
    SpeechServerInvalidResponse,
    SpeechServerRejected,
    SpeechServerUnreachable,
)
from tests.desktop.speech.speech_test_support import (
    Reply,
    ScriptedServer,
    connect_error,
    rpc_error,
    rpc_ok,
)

SERVER = "http://127.0.0.1:8421"
# Captured before the autouse fixture replaces the seam.
_BACKOFF_DELAY = server_client_module._backoff_delay


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_client_module, "_backoff_delay", lambda _attempt: 0.0)


@pytest.fixture
def cancel() -> threading.Event:
    return threading.Event()


@pytest.fixture
def make_client(
    cancel: threading.Event,
) -> Iterator[Callable[[ScriptedServer], SpeechServerClient]]:
    clients: list[SpeechServerClient] = []

    def factory(server: ScriptedServer) -> SpeechServerClient:
        client = SpeechServerClient(
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
        SpeechServerClient("  ", cancel=cancel)


def test_client_ignores_environment_proxies(
    cancel: threading.Event, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:9")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:9")

    with SpeechServerClient(f" {SERVER}/ ", cancel=cancel) as client:
        assert client.server_url == SERVER
        assert client._http.trust_env is False


def test_backoff_grows_exponentially_with_jitter_up_to_the_cap() -> None:
    assert 1.0 <= _BACKOFF_DELAY(0) < 2.0
    assert 2.0 <= _BACKOFF_DELAY(1) < 3.0
    assert _BACKOFF_DELAY(10) == server_client_module.MAX_BACKOFF_SECONDS


# -- Speech-to-text readiness ------------------------------------------------------


def test_speech_readiness_reports_a_usable_model(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(rpc_ok({"configured": True, "usable": True}))

    assert make_client(server).speech_readiness() is None
    assert str(server.requests[0].url) == f"{SERVER}/api/rpc"
    assert server.rpc_calls == [("task_model.status", {"task_type": "speech_to_text"})]


@pytest.mark.parametrize(
    ("reply", "problem"),
    [
        (rpc_ok({"configured": False, "usable": False}), "speech_to_text_unconfigured"),
        (rpc_ok({"configured": True, "usable": False}), "speech_to_text_unavailable"),
        (rpc_ok({"configured": True}), "speech_to_text_readiness_failed"),
        (rpc_ok(["not", "an", "object"]), "speech_to_text_readiness_failed"),
        (rpc_error("invalid_request"), "speech_to_text_readiness_failed"),
        (rpc_error("internal_error", status_code=500), "speech_to_text_readiness_failed"),
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
    server = ScriptedServer(*(connect_error() for _ in range(MAX_ATTEMPTS)))

    assert make_client(server).speech_readiness() == "server_unreachable"
    assert len(server.requests) == MAX_ATTEMPTS


@pytest.mark.parametrize(
    ("reply", "state"),
    [
        (rpc_ok({"state": "loading"}), "loading"),
        (rpc_error("method_not_found"), None),
        (connect_error(), None),
    ],
)
def test_speech_preparation_is_a_single_best_effort_request(
    make_client: Callable[..., Any], reply: Reply, state: str | None
) -> None:
    server = ScriptedServer(reply)

    assert make_client(server).prepare_transcription() == state
    assert server.rpc_calls == [("speech.prepare_transcription", {})]


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
        connect_error(),
        httpx.Response(200, json={"text": "hi"}),
    )

    assert make_client(server).transcribe(b"audio") == "hi"
    assert len(server.requests) == 3


def test_transcribe_reports_a_persistent_provider_failure(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(*(httpx.Response(502, text="provider down") for _ in range(3)))

    with pytest.raises(SpeechServerRejected) as raised:
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

    with pytest.raises(SpeechServerRejected) as raised:
        make_client(server).transcribe(b"audio")

    assert raised.value.error_code == "transcription_failed"
    assert raised.value.status_code == status_code
    assert len(server.requests) == 1


def test_transcribe_does_not_repeat_an_upload_after_a_read_timeout(
    make_client: Callable[..., Any],
) -> None:
    server = ScriptedServer(httpx.ReadTimeout("still transcribing"))

    with pytest.raises(SpeechServerUnreachable):
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
    with pytest.raises(SpeechServerInvalidResponse) as raised:
        make_client(ScriptedServer(reply)).transcribe(b"audio")

    assert raised.value.error_code == "transcription_failed"


# -- Upload budget -------------------------------------------------------------------


def test_upload_budget_follows_the_server_limit_once(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(rpc_ok({"setting": {"value": 1_000_044}}))
    client = make_client(server)

    assert client.upload_budget_bytes() == 900_000
    assert client.upload_budget_bytes() == 900_000
    assert server.rpc_calls == [("settings.get_path", {"path": "speech.upload_max_size_bytes"})]


@pytest.mark.parametrize(
    "replies",
    [
        [rpc_error("invalid_request")],
        [rpc_ok({"setting": {"value": "big"}})],
        [rpc_ok({"setting": {"value": True}})],
        [rpc_ok({"setting": {"value": 44}})],
        [rpc_ok({})],
        [connect_error() for _ in range(MAX_ATTEMPTS)],
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

    with pytest.raises(SpeechRequestCancelled):
        client.upload_budget_bytes()
    with pytest.raises(SpeechRequestCancelled):
        client.speech_readiness()
    assert client.prepare_transcription() is None
    assert server.requests == []


def test_cancellation_interrupts_a_retry_backoff(
    make_client: Callable[..., Any], cancel: threading.Event
) -> None:
    def fail_and_cancel(_request: httpx.Request) -> httpx.Response:
        cancel.set()
        return httpx.Response(503)

    server = ScriptedServer(fail_and_cancel)

    with pytest.raises(SpeechRequestCancelled):
        make_client(server).transcribe(b"audio")
    assert len(server.requests) == 1


@pytest.mark.parametrize("stop", ["cancel", "close"])
def test_cancellation_closes_an_in_flight_request_without_waiting_for_a_reply(
    cancel: threading.Event, stop: str
) -> None:
    started, request_closed = threading.Event(), threading.Event()
    failures: list[Exception] = []

    async def respond(_request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Future()
        finally:
            request_closed.set()
        raise AssertionError("cancelled request resumed")

    with SpeechServerClient(
        SERVER, cancel=cancel, transport=httpx.MockTransport(respond)
    ) as client:

        def transcribe() -> None:
            try:
                client.transcribe(b"audio")
            except Exception as exc:
                failures.append(exc)

        worker = threading.Thread(target=transcribe)
        worker.start()
        try:
            assert started.wait(1)
            if stop == "cancel":
                cancel.set()
            else:
                client.close()
            assert request_closed.wait(1)
            worker.join(1)
            assert not worker.is_alive()
            assert len(failures) == 1
            assert isinstance(failures[0], SpeechRequestCancelled)
        finally:
            cancel.set()
            client.close()
            worker.join(1)
