"""Worker: network behavior."""

from __future__ import annotations

import httpx
import pytest

from desktop.wakeword.engine import MockWakewordEngine
from tests.desktop.worker_helpers import (
    FakeBridge,
)
from tests.desktop.worker_helpers import (
    fake_bridge as fake_bridge,
)
from tests.desktop.worker_helpers import (
    no_real_neural_speech_detector as no_real_neural_speech_detector,
)
from tests.desktop.worker_helpers import (
    ready_speech_to_text as ready_speech_to_text,
)


def test_resolve_session_uses_agent_current_session(fake_bridge: FakeBridge) -> None:
    from desktop.wakeword.worker import WakewordWorker

    calls: list[tuple[str, dict[str, object]]] = []
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )

    def rpc_call(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        if method == "agent.get":
            return {"current_session_id": "current-one"}
        raise AssertionError(f"unexpected method: {method}")

    worker._rpc_call = rpc_call  # type: ignore[method-assign]

    assert worker._resolve_session("main", "active") == "current-one"
    assert calls == [("agent.get", {"id": "main"})]


def test_resolve_session_falls_back_to_latest_activity(fake_bridge: FakeBridge) -> None:
    from desktop.wakeword.worker import WakewordWorker

    calls: list[tuple[str, dict[str, object]]] = []
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )

    def rpc_call(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        if method == "agent.get":
            return {"current_session_id": ""}
        if method == "session.list":
            return {
                "sessions": [
                    {"id": "older", "last_active_at": "2026-05-30T10:00:00+00:00"},
                    {"id": "newer", "last_active_at": "2026-05-31T10:00:00+00:00"},
                ]
            }
        raise AssertionError(f"unexpected method: {method}")

    worker._rpc_call = rpc_call  # type: ignore[method-assign]

    assert worker._resolve_session("main", "active") == "newer"
    assert calls == [
        ("agent.get", {"id": "main"}),
        (
            "session.list",
            {
                "agent_id": "main",
                "limit": 1,
                "include_subagents": True,
                "include_memory_reflections": True,
                "include_skill_reflections": True,
                "include_cron": True,
            },
        ),
    ]


def test_send_transcript_uses_streaming_rpc(fake_bridge: FakeBridge) -> None:
    from desktop.wakeword.worker import WakewordWorker

    calls: list[tuple[str, dict[str, object]]] = []
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )

    def rpc_call(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        return {"run_id": "run-one", "sse_url": "/api/runs/run-one/events"}

    worker._rpc_call = rpc_call  # type: ignore[method-assign]

    sent = worker._send_transcript("hello", "main", "session-one")

    assert sent is True
    assert calls == [
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


def test_worker_http_calls_ignore_environment_proxies(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.wakeword import worker as worker_module
    from desktop.wakeword.worker import WakewordWorker

    calls: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"ok": True, "result": {"id": "main"}, "text": "hello"}

    def post(_url: str, **kwargs: object) -> FakeResponse:
        calls.append(kwargs)
        return FakeResponse()

    monkeypatch.setattr(worker_module.httpx, "post", post)
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )

    assert worker._transcribe(b"audio") == "hello"
    assert worker._rpc_call("agent.get", {"id": "main"}) == {"id": "main"}
    assert len(calls) == 2
    assert all(call["trust_env"] is False for call in calls)


def test_rpc_call_returns_empty_for_rpc_error(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.wakeword import worker as worker_module
    from desktop.wakeword.worker import WakewordWorker

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"ok": False, "error": {"message": "bad request"}}

    monkeypatch.setattr(worker_module.httpx, "post", lambda *args, **kwargs: FakeResponse())
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )

    assert worker._rpc_call("agent.get", {"id": "main"}) == {}


def test_transcribe_returns_none_for_invalid_success_json(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from desktop.wakeword import worker as worker_module
    from desktop.wakeword.worker import WakewordWorker

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            raise ValueError("invalid json")

    monkeypatch.setattr(worker_module.httpx, "post", lambda *args, **kwargs: FakeResponse())
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )

    assert worker._transcribe(b"audio") is None
    assert "invalid JSON" in caplog.text


def test_rpc_call_returns_empty_for_invalid_success_json(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from desktop.wakeword import worker as worker_module
    from desktop.wakeword.worker import WakewordWorker

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            raise ValueError("invalid json")

    monkeypatch.setattr(worker_module.httpx, "post", lambda *args, **kwargs: FakeResponse())
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )

    assert worker._rpc_call("agent.get", {"id": "main"}) == {}
    assert "invalid JSON" in caplog.text


@pytest.mark.parametrize("method", ["session.create", "chat.stream"])
def test_rpc_call_does_not_retry_mutation_after_ambiguous_transport_failure(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    from desktop.wakeword import worker as worker_module
    from desktop.wakeword.worker import WakewordWorker

    calls = 0

    def ambiguous_failure(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        request = httpx.Request("POST", "http://127.0.0.1:8420/api/rpc")
        raise httpx.ReadTimeout("response lost after send", request=request)

    monkeypatch.setattr(worker_module.httpx, "post", ambiguous_failure)
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._running.set()

    assert worker._rpc_call(method, {}) == {}
    assert calls == 1


def test_rpc_call_retries_safe_read_after_transport_failure(
    fake_bridge: FakeBridge,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from desktop.wakeword import worker as worker_module
    from desktop.wakeword.worker import WakewordWorker

    calls = 0

    class SuccessfulResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"ok": True, "result": {"id": "main"}}

    def post(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            request = httpx.Request("POST", "http://127.0.0.1:8420/api/rpc")
            raise httpx.ReadTimeout("temporary read failure", request=request)
        return SuccessfulResponse()

    monkeypatch.setattr(worker_module.httpx, "post", post)
    monkeypatch.setattr(worker_module, "_backoff_sleep", lambda *_args: None)
    worker = WakewordWorker(
        engine=MockWakewordEngine(),
        bridge=fake_bridge,
        server_url="http://127.0.0.1:8420",
    )
    worker._running.set()

    assert worker._rpc_call("agent.get", {"id": "main"}) == {"id": "main"}
    assert calls == 2
