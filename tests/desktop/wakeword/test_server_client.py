"""Tests for the Voice command calls (``desktop.wakeword.server_client``)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from desktop.speech import server_client as server_client_module
from desktop.speech.server_client import (
    MAX_ATTEMPTS,
    SpeechServerInvalidResponse,
    SpeechServerRejected,
    SpeechServerUnreachable,
)
from desktop.wakeword.server_client import VoiceServerClient
from tests.desktop.speech.speech_test_support import (
    Reply,
    ScriptedServer,
    connect_error,
    rpc_error,
    rpc_ok,
)

SERVER = "http://127.0.0.1:8421"


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


# -- Agents and Sessions -----------------------------------------------------------


def test_get_agent_retries_a_transport_failure(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(connect_error(), rpc_ok({"id": "main", "current_session_id": "s1"}))

    assert make_client(server).get_agent("main") == {"id": "main", "current_session_id": "s1"}
    assert server.rpc_calls == [("agent.get", {"id": "main"})] * 2


def test_get_agent_reports_an_unknown_agent(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(rpc_error("agent_not_found"))

    with pytest.raises(SpeechServerRejected) as raised:
        make_client(server).get_agent("gone")

    assert raised.value.error_code == "target_agent_unavailable"
    assert raised.value.rpc_code == "agent_not_found"
    assert len(server.requests) == 1


def test_active_session_uses_the_agents_current_session(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(rpc_ok({"current_session_id": "current-one"}))

    assert make_client(server).resolve_session("main", "active") == "current-one"
    assert server.rpc_calls == [("agent.get", {"id": "main"})]


def test_active_session_falls_back_to_the_newest_conversation(
    make_client: Callable[..., Any],
) -> None:
    server = ScriptedServer(
        rpc_ok({"current_session_id": None}),
        rpc_ok(
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
        rpc_ok({"current_session_id": ""}),
        rpc_ok({"sessions": []}),
        rpc_ok({"agent_id": "main", "session_id": "fresh"}),
    )

    assert make_client(server).resolve_session("main", "active") == "fresh"
    assert server.rpc_calls[-1] == ("session.create", {"agent_id": "main", "make_current": True})


def test_new_session_is_created_and_made_current(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(rpc_ok({"agent_id": "main", "session_id": "fresh"}))

    assert make_client(server).resolve_session("main", "new") == "fresh"
    assert server.rpc_calls == [("session.create", {"agent_id": "main", "make_current": True})]


def test_session_creation_is_never_repeated_after_a_transport_failure(
    make_client: Callable[..., Any],
) -> None:
    server = ScriptedServer(httpx.ReadTimeout("response lost"))

    with pytest.raises(SpeechServerUnreachable) as raised:
        make_client(server).resolve_session("main", "new")

    assert raised.value.error_code == "server_unreachable"
    assert len(server.requests) == 1


@pytest.mark.parametrize(
    ("replies", "error_type", "error_code"),
    [
        ([rpc_error("invalid_request")], SpeechServerRejected, "session_resolution_failed"),
        ([rpc_error("agent_not_found")], SpeechServerRejected, "target_agent_unavailable"),
        ([rpc_ok({"agent_id": "main"})], SpeechServerInvalidResponse, "session_resolution_failed"),
        ([httpx.Response(503)], SpeechServerUnreachable, "server_unreachable"),
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
    server = ScriptedServer(rpc_ok({}), rpc_ok({"sessions": "none"}))

    with pytest.raises(SpeechServerInvalidResponse) as raised:
        make_client(server).resolve_session("main", "active")

    assert raised.value.error_code == "session_resolution_failed"


def test_resolve_session_rejects_an_unknown_behavior(make_client: Callable[..., Any]) -> None:
    with pytest.raises(ValueError):
        make_client(ScriptedServer()).resolve_session("main", "later")


def test_reads_retry_retryable_statuses(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(httpx.Response(503), httpx.Response(429), rpc_ok({"id": "main"}))

    assert make_client(server).get_agent("main") == {"id": "main"}
    assert len(server.requests) == 3


def test_reads_report_an_unreachable_server_when_retries_run_out(
    make_client: Callable[..., Any],
) -> None:
    server = ScriptedServer(*(httpx.Response(502) for _ in range(MAX_ATTEMPTS)))

    with pytest.raises(SpeechServerUnreachable):
        make_client(server).get_agent("main")
    assert len(server.requests) == MAX_ATTEMPTS


# -- Sending ---------------------------------------------------------------------


def test_send_command_streams_the_text_as_spoken_input(make_client: Callable[..., Any]) -> None:
    server = ScriptedServer(rpc_ok({"run_id": "run-one", "sse_url": "/api/runs/run-one/events"}))

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
        (connect_error(), SpeechServerUnreachable, "server_unreachable"),
        (httpx.Response(503), SpeechServerUnreachable, "server_unreachable"),
        (rpc_error("session_busy"), SpeechServerRejected, "send_failed"),
        (rpc_error("internal_error", status_code=500), SpeechServerRejected, "send_failed"),
        (httpx.Response(200, json={"ok": True}), SpeechServerInvalidResponse, "send_failed"),
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
    server = ScriptedServer(rpc_error("internal_error", status_code=500))

    with pytest.raises(SpeechServerRejected) as raised:
        make_client(server).send_command("main", "session-one", "hello")

    assert raised.value.status_code == 500
    assert raised.value.rpc_code == "internal_error"
