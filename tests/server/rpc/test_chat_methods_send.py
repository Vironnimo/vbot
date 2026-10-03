"""``chat.send``, ``chat.stream`` and ``chat.edit``: accessor submission contracts.

Both chat methods share one submission path (address, content, ``@``-mentions,
current-Session pointer, Queue fallback); ``chat.send`` then waits for the Run
while ``chat.stream`` returns its SSE location. Fast tests drive the RPC with a
recording chat loop; the real-stack tests prove the pipeline end to end.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient  # type: ignore[import-not-found]

from core.chat import ReplySurface
from core.chat.content_blocks import FileBlock, FileMentionBlock, MediaBlock, TextBlock
from core.sessions import SessionAddress
from core.tools import FileReadState, register_read_tool, tool_success
from server.app import create_app
from tests.server.rpc.chat_methods_test_support import (
    CurrentSessionAgents,
    _RecordingLoop,
    bridged_run_ids,
    call,
    chat_state,
    finished_run,
    resource_changes,
)
from tests.server.rpc_test_support import JsonObject, StubAdapter, StubRuntime, make_state

WEBUI = ReplySurface.webui()


# ---------------------------------------------------------------------------
# Submission path (recording chat loop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "agent_address", "agent_id", "project_id"),
    [
        ("chat.send", "builder", "builder", None),
        ("chat.send", "builder@vbot", "builder", "vbot"),
        ("chat.stream", "tester", "tester", None),
        ("chat.stream", "tester@vbot", "tester", "vbot"),
    ],
)
async def test_submission_starts_the_run_on_the_method_loop_for_the_addressed_agent(
    method: str, agent_address: str, agent_id: str, project_id: str | None
) -> None:
    send_loop, stream_loop = _RecordingLoop(), _RecordingLoop()
    state = chat_state(send_loop, streaming_loop=stream_loop)

    response = await call(state, method, agent_id=agent_address, session_id="s1", content="hi")

    used, unused = (send_loop, stream_loop) if method == "chat.send" else (stream_loop, send_loop)
    assert used.start_calls == [
        {
            "agent_id": agent_id,
            "content": "hi",
            "session_id": "s1",
            "reply_surface": WEBUI,
            "project_id": project_id,
        }
    ]
    assert unused.start_calls == []
    result = response["result"]
    if method == "chat.send":
        assert result["status"] == "completed"
        assert result["message"]["content"] == "Done"
        assert "sse_url" not in result
    else:
        assert result["sse_url"] == f"/api/runs/{result['run_id']}/events"
        assert "message" not in result
    assert await bridged_run_ids(state) == {result["run_id"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_submission_parses_content_blocks_and_forwards_the_input_origin(
    method: str,
) -> None:
    loop = _RecordingLoop()
    state = chat_state(loop)

    await call(
        state,
        method,
        agent_id="coder",
        session_id="s1",
        content=[
            {"type": "text", "text": "Please inspect these."},
            {
                "type": "media",
                "attachment_id": "att-123",
                "filename": "screen.png",
                "media_type": "image/png",
            },
            {
                "type": "file",
                "attachment_id": "att-456",
                "filename": "report.pdf",
                "media_type": "application/pdf",
            },
        ],
        input_origin="speech_transcription",
    )

    assert loop.start_calls == [
        {
            "agent_id": "coder",
            "session_id": "s1",
            "content": [
                TextBlock(type="text", text="Please inspect these."),
                MediaBlock(
                    type="media",
                    attachment_id="att-123",
                    filename="screen.png",
                    media_type="image/png",
                ),
                FileBlock(
                    type="file",
                    attachment_id="att-456",
                    filename="report.pdf",
                    media_type="application/pdf",
                ),
            ],
            "input_origin": "speech_transcription",
            "reply_surface": WEBUI,
            "project_id": None,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "named_field"),
    [
        ("chat.send", {"agent_id": "coder", "content": 123}, None),
        (
            "chat.stream",
            {"agent_id": "coder", "content": "Hi", "input_origin": "paste"},
            "input_origin",
        ),
        ("chat.send", {"agent_id": "builder@bad project", "content": "hi"}, None),
    ],
)
async def test_submission_rejects_malformed_params_before_starting_anything(
    method: str, params: JsonObject, named_field: str | None
) -> None:
    loop = _RecordingLoop()
    state = chat_state(loop)

    response = await call(state, method, session_id="s1", **params)

    assert response["ok"] is False
    assert response["error"]["code"] == "invalid_request"
    if named_field is not None:
        assert named_field in response["error"]["message"]
    assert loop.start_calls == []


@pytest.mark.asyncio
async def test_send_snapshots_mentioned_files_into_the_content(tmp_path: Path) -> None:
    # An @-mentioned file is snapshotted before the loop sees the content: the
    # string message becomes blocks (original text first, then the snapshot),
    # and the file is stamped as read for the Session.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "notes.md").write_text("snapshot body", encoding="utf-8")
    file_state = FileReadState()
    loop = _RecordingLoop()
    state = chat_state(
        loop,
        projects=SimpleNamespace(get=lambda project_id: SimpleNamespace(cwd="")),
        agent_resolver=SimpleNamespace(
            resolve_agent=lambda project_id, agent_id: SimpleNamespace(workspace=str(workspace))
        ),
        storage=SimpleNamespace(data_dir=str(tmp_path)),
        file_read_state=file_state,
    )

    await call(
        state,
        "chat.send",
        agent_id="builder",
        session_id="s1",
        content="look at @notes.md",
        file_mentions=["notes.md"],
    )

    content = loop.start_calls[0]["content"]
    assert content[0] == TextBlock(type="text", text="look at @notes.md")
    assert isinstance(content[1], FileMentionBlock)
    assert content[1].status == "inlined"
    assert content[1].text == "snapshot body"
    assert file_state.check_stale("s1", (workspace / "notes.md").resolve()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "agent_id", "current_session_id", "busy", "marked"),
    [
        pytest.param("chat.send", "builder", "", False, True, id="send"),
        pytest.param("chat.stream", "builder", "", True, True, id="queued-stream"),
        pytest.param("chat.edit", "builder", "", False, True, id="edit"),
        pytest.param("chat.send", "builder", "s1", False, False, id="already-current"),
        pytest.param("chat.send", "builder@vbot", "", False, False, id="project-agent"),
    ],
)
async def test_user_message_makes_the_identity_session_current(
    method: str, agent_id: str, current_session_id: str, busy: bool, marked: bool
) -> None:
    # A restart re-opens the Session the user last wrote to, even when the
    # message was queued. Re-marking an already current Session would tear down
    # the chat view in every window, and project agents have no pointer.
    agents = CurrentSessionAgents(current_session_id)
    state = chat_state(_RecordingLoop(busy=busy), agents=agents)
    params: JsonObject = {"agent_id": agent_id, "session_id": "s1", "content": "hi"}
    if method == "chat.edit":
        params["message_id"] = "message-1"

    response = await call(state, method, **params)

    assert response["ok"] is True
    assert agents.updates == ([("builder", "s1")] if marked else [])
    assert resource_changes(state, "agents") == ([{"kind": "agents"}] if marked else [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "origin"),
    [("chat.send", None), ("chat.stream", "speech_transcription")],
)
async def test_busy_session_queues_the_message_and_signals_the_queue(
    method: str, origin: str | None
) -> None:
    loop = _RecordingLoop(busy=True)
    state = chat_state(loop)
    params: JsonObject = {"agent_id": "builder@vbot", "session_id": "s1", "content": "Queued"}
    if origin is not None:
        params["input_origin"] = origin

    response = await call(state, method, **params)

    item = loop.queued_items[0]
    assert response == {"ok": True, "result": {"queued": True, "item": item.to_dict()}}
    expected_origin = {} if origin is None else {"input_origin": origin}
    assert loop.queue_calls == [
        {
            "agent_id": "builder",
            "content": "Queued",
            "session_id": "s1",
            **expected_origin,
            "reply_surface": WEBUI,
            "project_id": "vbot",
        }
    ]
    assert resource_changes(state, "queue") == [
        {"kind": "queue", "scope": {"agent_id": "builder", "session_id": "s1"}}
    ]
    # The Run the item starts later reaches the event bus like a direct start.
    dequeued = finished_run("run-dequeued", project_id="vbot")
    item.future.set_result(dequeued)
    assert await bridged_run_ids(state) == {"run-dequeued"}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_enqueue_that_wins_the_idle_race_returns_the_started_run(method: str) -> None:
    state = chat_state(_RecordingLoop(busy=True, queued_run=finished_run("run-race")))

    response = await call(state, method, agent_id="builder", session_id="s1", content="hi")

    result = response["result"]
    assert result["run_id"] == "run-race"
    assert "queued" not in result
    if method == "chat.send":
        assert result["message"]["content"] == "Done"
    else:
        assert result["sse_url"] == "/api/runs/run-race/events"
    assert resource_changes(state, "queue") == []
    assert await bridged_run_ids(state) == {"run-race"}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["chat.send", "chat.stream"])
async def test_cancelled_enqueue_is_a_run_cancelled_error(method: str) -> None:
    state = chat_state(_RecordingLoop(busy=True, cancel_queued=True))

    response = await call(state, method, agent_id="agent-1", session_id="s1", content="hi")

    assert response["ok"] is False
    assert response["error"]["code"] == "run_cancelled"
    assert "queued-1" in response["error"]["message"]


@pytest.mark.asyncio
async def test_edit_starts_an_idle_streaming_run() -> None:
    send_loop, stream_loop = _RecordingLoop(), _RecordingLoop()
    state = chat_state(send_loop, streaming_loop=stream_loop)

    response = await call(
        state,
        "chat.edit",
        agent_id="agent-1@vbot",
        session_id="session-1",
        message_id="message-1",
        content="edited request",
    )

    assert response["result"]["run_id"] == "run-edit"
    assert response["result"]["sse_url"] == "/api/runs/run-edit/events"
    assert stream_loop.edit_calls == [
        {
            "agent_id": "agent-1",
            "content": "edited request",
            "session_id": "session-1",
            "message_id": "message-1",
            "reply_surface": WEBUI,
            "project_id": "vbot",
        }
    ]
    assert send_loop.edit_calls == []
    assert await bridged_run_ids(state) == {"run-edit"}


@pytest.mark.asyncio
async def test_edit_of_a_busy_session_is_rejected_without_queueing() -> None:
    loop = _RecordingLoop(busy=True)
    state = chat_state(loop)

    response = await call(
        state,
        "chat.edit",
        agent_id="agent-1",
        session_id="session-1",
        message_id="message-1",
        content="edited request",
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "active_run"
    assert loop.queue_calls == []


# ---------------------------------------------------------------------------
# Real stack: chat loops, Run manager, Sessions database
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_waits_for_the_run_and_returns_its_visible_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = StubAdapter(
        [
            {
                "content": None,
                "reasoning_meta": {"secret": "opaque"},
                "tool_calls": [
                    {"id": "call_read", "name": "read", "arguments": {"path": "note.txt"}}
                ],
            },
            {
                "content": "Read the file",
                "reasoning": "Readable thinking",
                "reasoning_meta": {"secret": "opaque"},
                "tool_calls": None,
            },
        ]
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = make_state(tmp_path, adapter)
    register_read_tool(
        state.runtime.tools,
        attachment_store=None,
        speech_service=None,
        file_state=FileReadState(),
        speech_max_size_bytes=20_971_520,
    )
    state.runtime.agents.update("coder", workspace=str(tmp_path / "workspace"))
    workspace = Path(state.runtime.agents.get("coder").workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    workspace.joinpath("note.txt").write_text("rpc content", encoding="utf-8")
    state.runtime.chat_sessions.create("coder", session_id="session-one")

    response = await call(
        state, "chat.send", agent_id="coder", session_id="session-one", content="Read note"
    )

    result = response["result"]
    assert result["status"] == "completed"
    assert result["message"]["content"] == "Read the file"
    assert result["message"]["reasoning"] == "Readable thinking"
    assert "reasoning_meta" not in str(result)
    assert "batch" not in str(result["events"])
    assert [
        event["type"] for event in result["events"] if event["type"] != "provider_request_status"
    ] == [
        "run_started",
        "user_message_persisted",
        "model_step_usage",
        "tool_call_started",
        "tool_call_result",
        "model_step_usage",
        "reasoning",
        "assistant_output",
        "run_completed",
    ]
    # chat.send drives the non-streaming loop.
    assert len(adapter.requests) == 2
    assert adapter.stream_requests == []

    tool_started = next(event for event in result["events"] if event["type"] == "tool_call_started")
    tool_result = next(event for event in result["events"] if event["type"] == "tool_call_result")
    fingerprint = state.runtime.tools.schema_fingerprint("read")
    assistant = next(
        message
        for message in state.runtime.chat_sessions.get(
            SessionAddress(project_id=None, agent_id="coder", session_id="session-one")
        ).load()
        if message.role == "assistant" and message.tool_calls
    )
    started_payload = dict(tool_started["payload"])
    assert started_payload.pop("display")["summary"] == "note.txt"
    assert started_payload == {
        "assistant_message_id": assistant.id,
        "tool_call": {
            "id": "call_read",
            "index": 0,
            "name": "read",
            "arguments": {"path": "note.txt"},
        },
        "schema_fingerprint": fingerprint,
    }
    assert tool_result["payload"]["assistant_message_id"] == assistant.id
    assert tool_result["payload"]["tool_call"] == {"id": "call_read", "index": 0, "name": "read"}
    assert tool_result["payload"]["result"] == {
        "ok": True,
        "error": None,
        "data": {"content": "1| rpc content"},
        "artifacts": [],
    }
    assert tool_result["payload"]["schema_fingerprint"] == fingerprint
    assert tool_result["payload"]["error_code"] is None


@pytest.mark.asyncio
async def test_stream_returns_the_running_run_without_waiting_for_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = StubAdapter(
        stream_deltas=[
            {"type": "content_delta", "text": "Streamed response"},
            {"type": "finish", "reason": "stop"},
        ],
        block=True,
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = make_state(tmp_path, adapter)
    state.runtime.chat_sessions.create("coder", session_id="session-one")

    response = await call(
        state, "chat.stream", agent_id="coder", session_id="session-one", content="Hi"
    )
    await adapter.request_started.wait()

    result = response["result"]
    assert result["status"] == "running"
    assert result["sse_url"] == f"/api/runs/{result['run_id']}/events"
    # chat.stream drives the streaming loop.
    assert adapter.requests == []
    assert len(adapter.stream_requests) == 1
    adapter.release.set()
    final_message = await state.chat_runs.get(result["run_id"]).wait()
    assert final_message.content == "Streamed response"


@pytest.mark.asyncio
async def test_send_to_a_missing_session_is_a_domain_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    state = make_state(tmp_path, StubAdapter())

    response = await call(state, "chat.send", agent_id="coder", session_id="missing", content="Hi")

    assert response["ok"] is False
    assert response["error"]["code"] == "domain_error"


@pytest.mark.asyncio
async def test_busy_session_queues_while_other_sessions_keep_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    adapter = StubAdapter()
    state = make_state(tmp_path, adapter)
    state.runtime.chat_sessions.create("coder", session_id="session-one")
    state.runtime.chat_sessions.create("coder", session_id="session-two")
    release = asyncio.Event()

    async def occupy(_run: Any) -> str:
        await release.wait()
        return "done"

    occupant = await state.chat_runs.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"), occupy
    )
    try:
        queued = await call(
            state, "chat.stream", agent_id="coder", session_id="session-one", content="Again"
        )
        parallel = await call(
            state, "chat.send", agent_id="coder", session_id="session-two", content="Parallel"
        )

        assert queued["result"]["queued"] is True
        item = queued["result"]["item"]
        assert item["content"] == "Again"
        assert item["id"]
        assert parallel["result"]["message"]["content"] == "OK"
        assert len(adapter.requests) == 1
        assert adapter.stream_requests == []
        assert resource_changes(state, "queue") == [
            {"kind": "queue", "scope": {"agent_id": "coder", "session_id": "session-one"}}
        ]
        assert state.chat_runs.remove_queued("coder", "session-one", item["id"], project_id=None)
    finally:
        release.set()
        await occupant.wait()


def test_http_send_persists_the_run_and_serves_its_timeline_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    adapter = StubAdapter(
        [
            {
                "content": None,
                "reasoning": "Need the lookup tool.",
                "reasoning_meta": {"encrypted_content": "opaque"},
                "tool_calls": [
                    {"id": "call_lookup", "name": "lookup", "arguments": {"query": "vBot"}}
                ],
            },
            {"content": "Lookup complete.", "tool_calls": None},
        ]
    )
    runtime = StubRuntime(tmp_path, adapter)
    runtime.tools.register(
        "lookup",
        "Look up a value.",
        {"type": "object"},
        lambda _context, arguments: tool_success({"result": f"found {arguments['query']}"}),
    )
    app = create_app(runtime=cast(Any, runtime))

    with TestClient(app) as client:
        create_response = client.post(
            "/api/rpc",
            json={
                "method": "session.create",
                "params": {"agent_id": "coder", "session_id": "session-one"},
            },
        )
        send_response = client.post(
            "/api/rpc",
            json={
                "method": "chat.send",
                "params": {"agent_id": "coder", "session_id": "session-one", "content": "Go"},
            },
        )
        send_result = send_response.json()["result"]
        sse_response = client.get(f"/api/runs/{send_result['run_id']}/events")
        history_result = client.post(
            "/api/rpc",
            json={
                "method": "chat.history",
                "params": {"agent_id": "coder", "session_id": "session-one"},
            },
        ).json()["result"]

    assert create_response.json() == {
        "ok": True,
        "result": {"agent_id": "coder", "session_id": "session-one"},
    }
    assert send_result["message"]["content"] == "Lookup complete."
    timeline = [
        "run_started",
        "user_message_persisted",
        "model_step_usage",
        "reasoning",
        "tool_call_started",
        "tool_call_result",
        "model_step_usage",
        "assistant_output",
        "run_completed",
    ]
    assert [
        event["type"]
        for event in send_result["events"]
        if event["type"] != "provider_request_status"
    ] == timeline
    assert [
        event["event"]
        for event in _parse_sse(sse_response.text)
        if event["event"] != "provider_request_status"
    ] == timeline
    assert "reasoning_meta" not in json.dumps(send_result)
    assert "reasoning_meta" not in sse_response.text

    messages = runtime.chat_sessions.get(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one")
    ).load()
    assert [message.role for message in messages] == [
        "note",
        "user",
        "assistant",
        "tool",
        "assistant",
        "run_summary",
    ]
    assert str(messages[0].content).startswith('[reply-surface] {"kind":"webui"}')
    assert messages[-1].status == "completed"
    assert messages[-1].timing is not None
    # The provider's opaque metadata is persisted for the next request only.
    assert messages[2].reasoning_meta == {"encrypted_content": "opaque"}
    assert messages[4].usage is not None
    assert history_result["context_usage"] == messages[4].usage["context_usage"]
    assert history_result["context_usage"]["estimated"] is True
    assert history_result["context_usage"]["tokens"] > 0
    # The Run records the window of the Model that answered with the Context.
    assert history_result["context_usage"]["context_window"] > 0
    tool_message_content = messages[3].content
    assert isinstance(tool_message_content, str)
    assert json.loads(tool_message_content) == {
        "ok": True,
        "error": None,
        "data": {"result": "found vBot"},
        "artifacts": [],
    }


def _parse_sse(body: str) -> list[JsonObject]:
    events: list[JsonObject] = []
    for block in body.strip().split("\n\n"):
        if not block:
            continue
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        events.append({"event": fields["event"], "data": json.loads(fields["data"])})
    return events
