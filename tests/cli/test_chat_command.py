"""Tests for ``vbot chat``: Session choice, overrides, the followed Run and its report."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY

import httpx
import pytest

from cli import chat_management, main
from cli._server_target import resolve_instance, selected_server_url
from cli.parser import parse_args
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
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any],
        timeout: Any,
        trust_env: bool,
        verify: Any,
        auth: httpx.Auth,
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
    monkeypatch.setattr(httpx, "stream", fake.stream)
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
    ("flags", "overrides", "project"),
    [
        pytest.param((), None, {}, id="no-overrides"),
        pytest.param(
            ("--model", MODEL, "--thinking-effort", "high", "--temperature", "0.2"),
            {"model": MODEL, "thinking_effort": "high", "temperature": 0.2},
            {},
            id="overrides",
        ),
        pytest.param(
            ("--project", "vbot"), None, {"working_project_id": "vbot"}, id="in-a-project"
        ),
        pytest.param(("--workspace",), None, {"working_project_id": None}, id="in-the-workspace"),
        pytest.param(("--ephemeral",), None, {"ephemeral": True}, id="ephemeral"),
        # The words of other chat CLIs reach the same Ephemeral Session.
        pytest.param(("--temporary",), None, {"ephemeral": True}, id="temporary-alias"),
    ],
)
def test_chat_starts_a_session_and_prints_the_answer(
    rpc: FakeRpc,
    run_stream: FakeRunStream,
    run_cli: RunCli,
    flags: tuple[str, ...],
    overrides: dict[str, Any] | None,
    project: dict[str, Any],
) -> None:
    reply_run(rpc, "s-new")
    run_stream.connection(*TOOL_RUN)

    code, out, err = run_cli("chat", "What is vBot?", *flags)

    assert code == 0
    assert out == "The answer.\n"
    # The server creates the Session with the accepted message; none is created up front.
    # Without --project or --workspace it works in the Agent's default Project.
    new_session = {"agent_overrides": overrides} if overrides else {}
    new_session.update(project)
    assert rpc.calls == [
        (
            "chat.stream",
            {
                "agent_id": "main",
                "content": "What is vBot?",
                "new_session": new_session,
                "input_id": ANY,
            },
        ),
    ]
    # Events the chat.stream reply already carried are not requested again.
    assert run_stream.queries == [{"after_sequence": 2}]
    kind = "Ephemeral Session" if "ephemeral" in project else "Session"
    assert f"completed in {kind} s-new (Agent main, Model {MODEL})" in err
    assert ("deleted permanently 24 hours after its last message" in err) is (
        "ephemeral" in project
    )
    assert "Continue: vbot chat --session s-new " in err
    assert err.rstrip().endswith("<message>")
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


@pytest.mark.parametrize(
    ("server_says", "note"),
    [
        pytest.param(True, False, id="ephemeral-session"),
        pytest.param(False, True, id="normal-session"),
    ],
)
def test_chat_ephemeral_names_a_new_session_and_a_continued_one_keeps_its_kind(
    rpc: FakeRpc, run_stream: FakeRunStream, run_cli: RunCli, server_says: bool, note: bool
) -> None:
    rpc.reply(
        "chat.stream",
        {
            "run_id": RUN_ID,
            "session_id": "s-given",
            "status": "running",
            "events": STARTED,
            "sse_url": SSE_URL,
            "ephemeral": server_says,
        },
    )
    run_stream.connection(answer(3, "Continued."), completed(4))

    code, _out, err = run_cli("chat", "--session", "s-given", "--ephemeral", "Go on")

    assert code == 0
    assert "new_session" not in rpc.params("chat.stream")
    assert ("completed in Ephemeral Session s-given" in err) is server_says
    assert ("Note: Session s-given is not ephemeral" in err) is note


def test_chat_ephemeral_with_continue_is_a_usage_error(rpc: FakeRpc, run_cli: RunCli) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run_cli("chat", "-c", "--ephemeral", "Go on")

    assert exit_info.value.code == 2
    assert rpc.calls == []


@pytest.mark.parametrize("prompt", [("Compare these",), ()], ids=["with-text", "files-only"])
def test_chat_sends_attached_files_as_content_blocks(
    rpc: FakeRpc,
    run_stream: FakeRunStream,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prompt: tuple[str, ...],
) -> None:
    # A terminal stdin supplies no text: the files alone are the message.
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    image, notes = tmp_path / "screen.png", tmp_path / "Notizen.md"
    image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    notes.write_text("# Ärger", encoding="utf-8")
    for attachment_id, name, media_type in (
        ("att_image", "screen.png", "image/png"),
        ("att_notes", "Notizen.md", "text/markdown"),
    ):
        rpc.respond(
            "upload",
            {
                "attachment_id": attachment_id,
                "filename": name,
                "media_type": media_type,
                "size_bytes": 10,
            },
        )
    reply_run(rpc, "s-new")
    run_stream.connection(answer(3, "Both differ."), completed(4))

    code, out, _err = run_cli(
        "chat", "--attach", str(image), "--attach", str(notes), "--json", *prompt
    )

    assert code == 0, _err
    uploads = [params for method, params in rpc.calls if method == "upload"]
    assert uploads == [
        {"filename": "screen.png", "data": image.read_bytes()},
        {"filename": "Notizen.md", "data": notes.read_bytes()},
    ]
    text = [{"type": "text", "text": "Compare these"}] if prompt else []
    assert rpc.params("chat.stream")["content"] == [
        *text,
        {
            "type": "media",
            "attachment_id": "att_image",
            "filename": "screen.png",
            "media_type": "image/png",
        },
        {
            "type": "file",
            "attachment_id": "att_notes",
            "filename": "Notizen.md",
            "media_type": "text/markdown",
        },
    ]
    assert [item["attachment_id"] for item in json.loads(out)["attachments"]] == [
        "att_image",
        "att_notes",
    ]


@pytest.mark.parametrize("refusal", ["missing-file", "server-refuses"])
def test_chat_sends_nothing_when_an_attachment_cannot_be_uploaded(
    rpc: FakeRpc, run_cli: RunCli, tmp_path: Path, refusal: str
) -> None:
    archive = tmp_path / "setup.exe"
    if refusal == "server-refuses":
        archive.write_bytes(b"MZ")
        rpc.respond("upload", {"detail": "File type is not allowed"}, status=415)

    code, _out, err = run_cli("chat", "--attach", str(archive), "Install this")

    assert code == 1
    assert "chat.stream" not in rpc.methods
    assert "No message was sent." in err
    assert ("does not exist" in err) is (refusal == "missing-file")
    assert ("File type is not allowed" in err) is (refusal == "server-refuses")


@pytest.mark.parametrize(
    ("flags", "session"),
    [
        pytest.param((), None, id="new-conversation"),
        pytest.param(("--session", "s-given"), "s-given", id="session"),
        pytest.param(("-c",), "s-latest", id="continue"),
    ],
)
def test_chat_without_a_message_in_an_interactive_terminal_opens_the_tui(
    rpc: FakeRpc,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    flags: tuple[str, ...],
    session: str | None,
) -> None:
    from cli import _dispatch_tui

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    opened: list[Any] = []

    def open_tui(args: Any, _instance: Any) -> int:
        opened.append(args)
        return 0

    monkeypatch.setattr(_dispatch_tui, "dispatch_tui_command", open_tui)
    rpc.reply("session.list", {"sessions": [{"id": "s-latest"}]})

    code, _out, _err = run_cli("chat", "--agent", "coder", *flags)

    assert code == 0
    assert [(args.agent, args.session, args.project) for args in opened] == [
        ("coder", session, None)
    ]
    assert "chat.stream" not in rpc.methods


def test_chat_continue_without_a_conversation_starts_a_new_session(
    rpc: FakeRpc, run_stream: FakeRunStream, run_cli: RunCli
) -> None:
    rpc.reply("session.list", {"sessions": []})
    reply_run(rpc, "s-new")
    run_stream.connection(answer(3, "Started."), completed(4))

    code, out, err = run_cli("chat", "--agent", "coder@vbot", "-c", "Go on")

    assert code == 0
    assert out == "Started.\n"
    assert rpc.methods == ["session.list", "chat.stream"]
    assert rpc.params("chat.stream") == {
        "agent_id": "coder@vbot",
        "content": "Go on",
        "new_session": {},
        "input_id": ANY,
    }
    assert "s-new" in err


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
        "input_id": ANY,
    }


@pytest.mark.parametrize("valid_file", [True, False])
def test_chat_prompt_file_is_decoded_before_sending(
    rpc: FakeRpc,
    run_stream: FakeRunStream,
    run_cli: RunCli,
    tmp_path: Path,
    valid_file: bool,
) -> None:
    prompt = 'Prüfe den Build.\nAntworte mit "fertig".\n'
    source = tmp_path / "prompt.txt"
    source.write_bytes(prompt.encode("utf-16") if valid_file else b"\xffnot-valid")
    reply_run(rpc, "s-given")
    run_stream.connection(answer(3, "Fertig."), completed(4))

    code, _out, _err = run_cli("chat", "--file", str(source), "--session", "s-given")

    assert code == (0 if valid_file else 1)
    if valid_file:
        assert rpc.params("chat.stream")["content"] == prompt
    else:
        assert rpc.calls == []
        assert run_stream.queries == []


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


@pytest.mark.parametrize(
    ("flags", "session", "outcome"),
    [
        pytest.param(
            ("--session", "s-given"),
            {"session_id": "s-given"},
            "status handled in Session s-given",
            id="existing-session",
        ),
        # A session-less command for a new Session creates none, so none is named.
        pytest.param((), {}, "status handled without a Session", id="new-session"),
    ],
)
def test_chat_prints_a_slash_command_reply(
    rpc: FakeRpc,
    run_stream: FakeRunStream,
    run_cli: RunCli,
    flags: tuple[str, ...],
    session: dict[str, str],
    outcome: str,
) -> None:
    rpc.reply(
        "chat.stream",
        {
            "command_handled": True,
            "reply": "Agent main: idle",
            "output": "toast",
            "data": {"command": "status"},
        }
        | session,
    )

    code, out, err = run_cli("chat", *flags, "/status")

    assert code == 0
    assert out == "Agent main: idle\n"
    assert run_stream.queries == []
    assert outcome in err
    assert ("Continue:" in err) is bool(session)


def test_chat_json_reports_the_run_as_one_object(
    rpc: FakeRpc, run_stream: FakeRunStream, run_cli: RunCli
) -> None:
    reply_run(rpc, "s-new")
    run_stream.connection(*TOOL_RUN)

    code, out, _err = run_cli("chat", "--json", "--model", MODEL, "What is vBot?")

    assert code == 0
    assert json.loads(out) == {
        "agent_id": "main",
        "session_id": "s-new",
        "ephemeral": False,
        "attachments": [],
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
        "input_id": rpc.params("chat.stream")["input_id"],
    }


@pytest.mark.parametrize(
    ("prompt", "refused", "attempts"),
    [("Hello", False, 2), ("/status", False, 1), ("Hello", True, 1)],
)
def test_chat_retries_only_ordinary_unknown_submissions(
    rpc: FakeRpc,
    run_stream: FakeRunStream,
    run_cli: RunCli,
    prompt: str,
    refused: bool,
    attempts: int,
) -> None:
    if refused:
        rpc.fail("chat.stream", "input_conflict", "Input already used for different content")
    else:
        rpc.respond("chat.stream", [])
        reply_run(rpc, "s-new")
    if attempts == 2:
        run_stream.connection(answer(3, "Recovered."), completed(4))

    code, out, _err = run_cli("chat", prompt)

    assert code == (0 if attempts == 2 else 1)
    assert len(rpc.calls) == attempts
    params = rpc.calls[0][1]
    if prompt.startswith("/"):
        assert "input_id" not in params
    else:
        assert isinstance(params["input_id"], str) and params["input_id"]
    if attempts == 2:
        assert rpc.calls[1][1] == params
        assert out == "Recovered.\n"


@pytest.mark.parametrize(("run_id", "available"), [(None, True), (RUN_ID, True), (RUN_ID, False)])
def test_chat_reconciles_a_historical_receipt_without_new_work(
    rpc: FakeRpc, run_cli: RunCli, run_id: str | None, available: bool
) -> None:
    rpc.respond("chat.stream", [])
    receipt: dict[str, Any] = {
        "accepted": True,
        "session_id": "s-new",
        "input_state": "removed" if run_id is None else "run",
        "session_available": available,
        "agent_id": "renamed",
    }
    if run_id:
        receipt["run_id"] = run_id
        rpc.reply(
            "chat.run_result", {"found": True, "content": "Earlier answer", "truncated": True}
        )
    rpc.reply("chat.stream", receipt)
    rpc.reply("chat.timeline", {"items": []})
    rpc.reply("chat.queue_list", {"items": []})

    code, out, _err = run_cli("chat", "--json", "Hello")

    assert code == 0
    report = json.loads(out)
    assert report["status"] == "accepted"
    assert report["run_id"] == run_id
    assert report["input_state"] == receipt["input_state"]
    assert report["input_id"] == rpc.calls[0][1]["input_id"]
    assert rpc.calls[0] == rpc.calls[1]
    assert rpc.methods == ["chat.stream", "chat.stream"] + (
        ["chat.timeline", "chat.queue_list"] + (["chat.run_result"] if run_id else [])
        if available
        else []
    )
    assert report["agent_id"] == "renamed"
    assert report["session_available"] is available
    if available:
        assert rpc.params("chat.timeline")["agent_id"] == "renamed"
    assert report["message"] == ("Earlier answer" if run_id and available else "")
    assert report["truncated"] is bool(run_id and available)


def test_chat_reports_tool_calls_errors_and_retries_as_progress_on_stderr(
    rpc: FakeRpc, run_stream: FakeRunStream, run_cli: RunCli
) -> None:
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
    reply_run(rpc, "s-new")
    script(run_stream)

    code, out, err = run_cli("chat", "Hello")

    assert code == 1
    assert out == ""
    assert run_stream.queries == [{"after_sequence": 2}] * connections
    assert rpc.methods == ["chat.stream"]
    assert f"lost the event stream of Run {RUN_ID}" in err
    assert "check it with vbot chat --session s-new " in err
    assert "/status" in err


@pytest.mark.parametrize("outcome", ["completed", "handled", "lost"])
@pytest.mark.parametrize("url", ["http://127.0.0.1:9123", "http://192.0.2.8:9123", "https://[::1]"])
def test_chat_followup_preserves_the_resolved_target(
    rpc: FakeRpc,
    run_stream: FakeRunStream,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    url: str,
) -> None:
    target = resolve_instance(url=url, data_dir=tmp_path / "data with spaces")
    commands: list[tuple[str, ...]] = []
    format_command = chat_management.format_command

    def record_command(tokens: tuple[str, ...]) -> str:
        commands.append(tokens)
        return format_command(tokens)

    monkeypatch.setattr(chat_management, "format_command", record_command)
    if outcome == "handled":
        rpc.reply(
            "chat.stream",
            {
                "command_handled": True,
                "session_id": "s-new",
                "reply": "Moved",
                "data": {"command": "switch", "agent_id": "other@project", "session_id": "s-other"},
            },
        )
    else:
        reply_run(rpc, "s-new")
        if outcome == "lost":
            run_stream.refuse(404)
        else:
            run_stream.connection(*TOOL_RUN)

    code = main.run(
        ["chat", "--agent", "builder@project", "Hello"], resolve=lambda **_options: target
    )
    _out, err = capsys.readouterr()

    assert code == (1 if outcome == "lost" else 0)
    assert len(commands) == 1
    assert format_command(commands[0]) in err
    followup = parse_args([*commands[0][1:], *([] if outcome == "lost" else ["Next message"])])
    assert followup.agent == ("other@project" if outcome == "handled" else "builder@project")
    assert followup.session == ("s-other" if outcome == "handled" else "s-new")
    if outcome == "lost":
        assert followup.prompt == "/status"
    monkeypatch.setenv("VBOT_SERVER_URL", "https://another.example")
    assert (
        resolve_instance(
            host=followup.host,
            port=followup.port,
            data_dir=followup.data_dir,
            url=selected_server_url(followup),
        )
        == target
    )


def test_chat_ctrl_c_cancels_the_run_and_exits_130(
    rpc: FakeRpc, run_stream: FakeRunStream, run_cli: RunCli
) -> None:
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
