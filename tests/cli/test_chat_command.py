"""Tests for ``vbot chat``: Session choice, overrides, the followed Run and its report."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from cli import rpc_client
from tests.cli.cli_test_support import FakeRpc, RunCli

RUN_ID = "run-1"
SSE_URL = f"/api/runs/{RUN_ID}/events"
MODEL = "openai/gpt-5"
LATEST_SESSIONS = {
    "agent_id": "main",
    "limit": 1,
    "include_subagents": False,
    "include_memory_reflections": False,
    "include_skill_reflections": False,
    "include_cron": False,
    "include_channels": False,
}


def event(sequence: int, kind: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"sequence": sequence, "run_id": RUN_ID, "type": kind, "payload": payload or {}}


def answer(sequence: int, text: str) -> dict[str, Any]:
    message = {"role": "assistant", "content": text, "model": MODEL}
    return event(sequence, "assistant_output", {"message": message})


def completed(sequence: int) -> dict[str, Any]:
    return event(
        sequence, "run_completed", {"status": "completed", "timing": {"duration_ms": 1234}}
    )


# The Run a chat.stream reply starts: what it already recorded, then its SSE stream.
STARTED = [event(1, "run_started"), event(2, "user_message")]
TOOL_RUN = [
    event(3, "provider_request_status", {"state": "waiting", "model": MODEL}),
    event(
        4,
        "tool_call_started",
        {"tool_call": {"id": "call-1", "name": "read_file", "arguments": {"path": "README.md"}}},
    ),
    event(
        5,
        "tool_call_result",
        {
            "tool_call": {"id": "call-1", "name": "read_file"},
            "result": {"ok": True, "data": {"content": "# vBot"}},
        },
    ),
    event(6, "model_step_usage", {"usage": {"input_tokens": 100, "output_tokens": 20}}),
    answer(7, "The answer."),
    event(8, "model_step_usage", {"usage": {"input_tokens": 50, "output_tokens": 10}}),
    completed(9),
]


class FakeRunStream:
    """Serves scripted SSE connections of one Run and records each connection's query."""

    def __init__(self) -> None:
        self.queries: list[dict[str, Any]] = []
        self._connections: list[Callable[[], Iterator[str]] | int] = []

    def connection(self, *events: dict[str, Any], interrupt: bool = False) -> FakeRunStream:
        """Queue one connection that sends ``events``, optionally followed by Ctrl-C."""

        def lines() -> Iterator[str]:
            yield ": transport comment"
            yield from ("event: heartbeat", "data: {}", "")
            for item in events:
                yield from (
                    f"id: {item['sequence']}",
                    f"event: {item['type']}",
                    f"data: {json.dumps(item)}",
                    "",
                )
            if interrupt:
                raise KeyboardInterrupt

        self._connections.append(lines)
        return self

    def refuse(self, status: int) -> FakeRunStream:
        """Queue one connection the server answers with an HTTP error status."""
        self._connections.append(status)
        return self

    @contextmanager
    def stream(
        self, method: str, url: str, *, params: dict[str, Any], timeout: Any, trust_env: bool
    ) -> Iterator[SimpleNamespace]:
        assert (method, trust_env) == ("GET", False)
        assert url.endswith(SSE_URL)
        self.queries.append(dict(params))
        if not self._connections:
            raise AssertionError(f"unexpected Run event stream connection {params}")
        lines = self._connections.pop(0)
        if isinstance(lines, int):
            yield SimpleNamespace(status_code=lines, iter_lines=lambda: iter(()))
        else:
            yield SimpleNamespace(status_code=200, iter_lines=lines)


@pytest.fixture
def run_stream(monkeypatch: pytest.MonkeyPatch) -> FakeRunStream:
    fake = FakeRunStream()
    monkeypatch.setattr(rpc_client.httpx, "stream", fake.stream)
    return fake


def reply_run(rpc: FakeRpc, session_id: str) -> None:
    rpc.reply(
        "chat.stream",
        {
            "run_id": RUN_ID,
            "session_id": session_id,
            "status": "running",
            "events": STARTED,
            "sse_url": SSE_URL,
        },
    )


@pytest.mark.parametrize(
    ("flags", "overrides"),
    [
        pytest.param((), None, id="no-overrides"),
        pytest.param(
            ("--model", MODEL, "--thinking-effort", "high", "--temperature", "0.2"),
            {"model": MODEL, "thinking_effort": "high", "temperature": 0.2},
            id="overrides",
        ),
    ],
)
def test_chat_starts_a_session_and_prints_the_answer(
    rpc: FakeRpc,
    run_stream: FakeRunStream,
    run_cli: RunCli,
    flags: tuple[str, ...],
    overrides: dict[str, Any] | None,
) -> None:
    created = {"agent_id": "main", "session_id": "s-new"}
    rpc.reply("session.create", created | ({"agent_overrides": overrides} if overrides else {}))
    reply_run(rpc, "s-new")
    run_stream.connection(*TOOL_RUN)

    code, out, err = run_cli("chat", "What is vBot?", *flags)

    assert code == 0
    assert out == "The answer.\n"
    expected_create = {"agent_id": "main"} | ({"agent_overrides": overrides} if overrides else {})
    assert rpc.calls == [
        ("session.create", expected_create),
        ("chat.stream", {"agent_id": "main", "session_id": "s-new", "content": "What is vBot?"}),
    ]
    # Events the chat.stream reply already carried are not requested again.
    assert run_stream.queries == [{"after_sequence": 2}]
    assert f"completed in Session s-new (Agent main, Model {MODEL})" in err
    assert "Continue: vbot chat --session s-new <message>" in err
    assert ("Session overrides:" in err) is bool(overrides)


@pytest.mark.parametrize(
    ("flags", "saved"),
    [
        pytest.param((), None, id="keeps-saved-overrides"),
        pytest.param(("--temperature", "0.5"), {"temperature": 0.5}, id="updates-overrides"),
    ],
)
def test_chat_continue_uses_the_latest_session_and_saves_only_given_overrides(
    rpc: FakeRpc,
    run_stream: FakeRunStream,
    run_cli: RunCli,
    flags: tuple[str, ...],
    saved: dict[str, Any] | None,
) -> None:
    rpc.reply("session.list", {"sessions": [{"id": "s-latest"}, {"id": "s-older"}]})
    rpc.reply(
        "session.set_agent_overrides",
        {
            "agent_id": "main",
            "session_id": "s-latest",
            "agent_overrides": {"model": MODEL, **(saved or {})},
        },
    )
    reply_run(rpc, "s-latest")
    run_stream.connection(answer(3, "Continued."), completed(4))

    code, out, _err = run_cli("chat", "-c", "Go on", *flags)

    assert code == 0
    assert out == "Continued.\n"
    assert rpc.params("session.list") == LATEST_SESSIONS
    expected_overrides = [
        (
            "session.set_agent_overrides",
            {"agent_id": "main", "session_id": "s-latest", "agent_overrides": saved},
        )
    ]
    assert [call for call in rpc.calls if call[0] == "session.set_agent_overrides"] == (
        expected_overrides if saved else []
    )
    assert rpc.params("chat.stream")["session_id"] == "s-latest"


def test_chat_continue_without_a_conversation_fails_before_sending(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply("session.list", {"sessions": []})

    code, out, err = run_cli("chat", "--agent", "coder@vbot", "-c", "Go on")

    assert code == 1
    assert out == ""
    assert rpc.methods == ["session.list"]
    assert "coder@vbot has no conversation Session to continue" in err
    assert "Next: vbot session list coder@vbot" in err


@pytest.mark.parametrize("argv", [("chat",), ("chat", "-")], ids=["omitted", "dash"])
def test_chat_reads_the_message_from_piped_stdin(
    rpc: FakeRpc,
    run_stream: FakeRunStream,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    argv: tuple[str, ...],
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("Summarize this\nsecond line\n"))
    reply_run(rpc, "s-given")
    run_stream.connection(answer(3, "Summary."), completed(4))

    code, _out, _err = run_cli(*argv, "--session", "s-given")

    assert code == 0
    assert rpc.params("chat.stream") == {
        "agent_id": "main",
        "session_id": "s-given",
        "content": "Summarize this\nsecond line",
    }


def test_chat_without_a_message_on_a_terminal_is_a_usage_error(
    rpc: FakeRpc, run_cli: RunCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))

    with pytest.raises(SystemExit) as exit_info:
        run_cli("chat", "--session", "s-given")

    assert exit_info.value.code == 2
    assert rpc.calls == []


@pytest.mark.parametrize(
    ("terminal", "expected"),
    [
        pytest.param(
            event(4, "run_failed", {"status": "failed", "error": "Provider rejected the key"}),
            "Run run-1 ended failed in Session s-new (Agent main, Model openai/gpt-5): "
            "Provider rejected the key",
            id="failed",
        ),
        pytest.param(
            event(4, "run_cancelled", {"status": "cancelled", "reason": "user"}),
            "Run run-1 was cancelled in Session s-new",
            id="cancelled",
        ),
    ],
)
def test_chat_reports_an_unsuccessful_run_on_stderr_with_a_failure_exit(
    rpc: FakeRpc,
    run_stream: FakeRunStream,
    run_cli: RunCli,
    terminal: dict[str, Any],
    expected: str,
) -> None:
    rpc.reply("session.create", {"agent_id": "main", "session_id": "s-new"})
    reply_run(rpc, "s-new")
    run_stream.connection(
        event(3, "provider_request_status", {"state": "waiting", "model": MODEL}), terminal
    )

    code, out, err = run_cli("chat", "Hello")

    assert code == 1
    assert out == ""
    assert expected in err


def test_chat_reports_a_queued_message_without_waiting(
    rpc: FakeRpc, run_stream: FakeRunStream, run_cli: RunCli
) -> None:
    rpc.reply("session.list", {"sessions": [{"id": "s-busy"}]})
    rpc.reply("chat.stream", {"queued": True, "item": {"id": "queue-7", "content": "Go on"}})

    code, out, err = run_cli("chat", "-c", "Go on")

    assert code == 1
    assert out == ""
    assert run_stream.queries == []
    assert "Session s-busy is busy with another Run" in err
    assert "queued as queue-7" in err


def test_chat_prints_a_slash_command_reply(
    rpc: FakeRpc, run_stream: FakeRunStream, run_cli: RunCli
) -> None:
    rpc.reply(
        "chat.stream",
        {
            "command_handled": True,
            "reply": "Session s-given: 12 messages",
            "output": "toast",
            "data": {"command": "status"},
        },
    )

    code, out, err = run_cli("chat", "--session", "s-given", "/status")

    assert code == 0
    assert out == "Session s-given: 12 messages\n"
    assert run_stream.queries == []
    assert "status handled in Session s-given" in err


def test_chat_json_reports_the_run_as_one_object(
    rpc: FakeRpc, run_stream: FakeRunStream, run_cli: RunCli
) -> None:
    rpc.reply(
        "session.create",
        {"agent_id": "main", "session_id": "s-new", "agent_overrides": {"model": MODEL}},
    )
    reply_run(rpc, "s-new")
    run_stream.connection(*TOOL_RUN)

    code, out, _err = run_cli("chat", "--json", "--model", MODEL, "What is vBot?")

    assert code == 0
    assert json.loads(out) == {
        "agent_id": "main",
        "session_id": "s-new",
        "run_id": RUN_ID,
        "status": "completed",
        "model": MODEL,
        "message": "The answer.",
        "tool_calls": [
            {
                "id": "call-1",
                "name": "read_file",
                "arguments": {"path": "README.md"},
                "is_error": False,
                "error": None,
                "result_preview": '{"content":"# vBot"}',
            }
        ],
        "usage": {"input_tokens": 150, "output_tokens": 30},
        "duration_ms": 1234,
        "error": None,
        "agent_overrides": {"model": MODEL},
        "queue_item": None,
        "command": None,
    }


def test_chat_reports_tool_calls_errors_and_retries_as_progress_on_stderr(
    rpc: FakeRpc, run_stream: FakeRunStream, run_cli: RunCli
) -> None:
    rpc.reply("session.create", {"agent_id": "main", "session_id": "s-new"})
    reply_run(rpc, "s-new")
    retry = {
        "state": "retrying",
        "model": MODEL,
        "attempt": 2,
        "max_attempts": 4,
        "delay_seconds": 1.5,
        "error_kind": "rate_limit",
    }
    failure = {"code": "not_found", "message": "No such file"}
    run_stream.connection(
        event(3, "provider_request_status", retry),
        event(
            4,
            "tool_call_started",
            {"tool_call": {"id": "c1", "name": "read_file", "arguments": {"path": "x.md"}}},
        ),
        event(
            5,
            "tool_call_result",
            {
                "tool_call": {"id": "c1", "name": "read_file"},
                "result": {"ok": False, "error": failure},
            },
        ),
        answer(6, "The file is missing."),
        completed(7),
    )

    code, out, err = run_cli("chat", "--output", "human", "Read x.md")

    assert code == 0
    assert out == "The file is missing.\n"
    assert f"Model request of {MODEL} retrying (rate_limit, attempt 2/4, in 1.5s)" in err
    assert 'Tool read_file: {"path":"x.md"}' in err
    assert "Tool read_file failed: not_found: No such file" in err


def test_chat_streams_answer_text_on_a_terminal_without_repeating_it(
    rpc: FakeRpc,
    run_stream: FakeRunStream,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    rpc.reply("session.create", {"agent_id": "main", "session_id": "s-new"})
    reply_run(rpc, "s-new")
    run_stream.connection(
        event(3, "assistant_output_delta", {"content_delta": "Let me "}),
        event(4, "assistant_output_delta", {"content_delta": "check."}),
        answer(5, "Let me check."),
        event(6, "assistant_output_delta", {"content_delta": "Done."}),
        answer(7, "Done."),
        completed(8),
    )

    code, out, _err = run_cli("chat", "Check it")

    assert code == 0
    assert out == "Let me check.\nDone.\n"


def test_chat_reconnects_to_the_run_after_the_stream_closes_early(
    rpc: FakeRpc, run_stream: FakeRunStream, run_cli: RunCli
) -> None:
    rpc.reply("session.create", {"agent_id": "main", "session_id": "s-new"})
    reply_run(rpc, "s-new")
    run_stream.connection(answer(3, "Part"))
    # The replayed event 3 is applied once; the answer continues from event 4.
    run_stream.connection(answer(3, "Part"), answer(4, "Whole answer."), completed(5))

    code, out, _err = run_cli("chat", "Hello")

    assert code == 0
    assert out == "Whole answer.\n"
    assert run_stream.queries == [{"after_sequence": 2}, {"after_sequence": 3}]


@pytest.mark.parametrize(
    ("script", "connections"),
    [
        pytest.param(lambda stream: stream.refuse(404), 1, id="run-unknown"),
        pytest.param(
            lambda stream: stream.connection().connection().connection(), 3, id="no-progress"
        ),
    ],
)
def test_chat_reports_a_lost_run_stream_without_resending(
    rpc: FakeRpc,
    run_stream: FakeRunStream,
    run_cli: RunCli,
    script: Callable[[FakeRunStream], object],
    connections: int,
) -> None:
    rpc.reply("session.create", {"agent_id": "main", "session_id": "s-new"})
    reply_run(rpc, "s-new")
    script(run_stream)

    code, out, err = run_cli("chat", "Hello")

    assert code == 1
    assert out == ""
    assert run_stream.queries == [{"after_sequence": 2}] * connections
    assert rpc.methods == ["session.create", "chat.stream"]
    assert f"lost the event stream of Run {RUN_ID}" in err
    assert "check it with vbot chat --session s-new /status" in err


def test_chat_ctrl_c_cancels_the_run_and_exits_130(
    rpc: FakeRpc, run_stream: FakeRunStream, run_cli: RunCli
) -> None:
    rpc.reply("session.create", {"agent_id": "main", "session_id": "s-new"})
    reply_run(rpc, "s-new")
    rpc.reply("chat.cancel", {"run_id": RUN_ID, "status": "cancelled"})
    run_stream.connection(
        event(3, "provider_request_status", {"state": "waiting", "model": MODEL}), interrupt=True
    )

    code, out, err = run_cli("chat", "Hello")

    assert code == 130
    assert out == ""
    assert rpc.params("chat.cancel") == {"run_id": RUN_ID, "reason": "user"}
    assert f"requested cancellation of Run {RUN_ID}" in err
