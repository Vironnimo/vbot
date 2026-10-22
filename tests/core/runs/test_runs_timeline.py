"""Public semantic Run projection contracts shared by WebUI and terminal."""

from __future__ import annotations

from typing import Any

import pytest

from core.runs import Run, RunTimeline

JsonObject = dict[str, Any]
START = "2026-10-15T10:00:00Z"


def emit(
    timeline: RunTimeline, kind: str, payload: JsonObject | None = None, *, second: int = 0
) -> None:
    timeline.apply(
        {
            "run_id": timeline.run_id,
            "sequence": timeline.sequence + 1,
            "type": kind,
            "timestamp": f"2026-10-15T10:00:{second:02d}Z",
            "payload": payload or {},
        }
    )


def block(timeline: RunTimeline) -> JsonObject:
    return next(item for item in timeline.snapshot() if item["type"] == "assistant_run")


def test_canonical_output_settles_streamed_phase_in_place_after_early_tool_completion() -> None:
    timeline = RunTimeline("run")
    emit(
        timeline,
        "reasoning_delta",
        {"reasoning_delta": "Plan", "summary_index": 0, "summary_text": "Plan"},
    )
    emit(timeline, "assistant_output_delta", {"content_delta": "Inspecting"}, second=2)
    call = {"id": "call", "name": "read", "arguments": {"path": "README.md"}}
    emit(
        timeline,
        "tool_call_started",
        {"tool_call": call, "display": {"summary": "Read README"}},
        second=3,
    )
    emit(
        timeline,
        "tool_call_result",
        {"tool_call": call, "result": {"ok": True, "data": {"content": "Read"}}},
        second=4,
    )
    previous = block(timeline)
    ids = [item["id"] for item in previous["items"]]
    assert previous["items"][0]["durationEstimateMs"] == 2000
    message = {
        "id": "assistant-one",
        "role": "assistant",
        "content": "Inspected.",
        "reasoning": "Plan carefully",
        "reasoning_summary": ["Plan carefully"],
        "reasoning_timing": {"duration_ms": 1750},
        "tool_calls": [call],
    }
    emit(timeline, "reasoning", {"message": message}, second=5)
    emit(timeline, "assistant_output", {"message": message}, second=5)
    settled = block(timeline)
    assert [item["id"] for item in settled["items"]] == ids
    assert [item["type"] for item in settled["items"]] == [
        "reasoning",
        "assistant_output",
        "tool_call",
    ]
    reason, answer, tool = settled["items"]
    assert (reason["content"], reason["reasoningSummary"], reason["durationMs"]) == (
        "Plan carefully",
        ["Plan carefully"],
        1750,
    )
    assert reason["durationEstimateMs"] is None
    assert answer["content"] == "Inspected."
    assert tool["status"] == "success"
    assert tool["display"] == {"summary": "Read README"}
    assert all(not item["streaming"] for item in settled["items"])
    # Snapshots a subscriber already received cannot be changed by finalization.
    assert previous["items"][0]["content"] == "Plan"
    assert previous["items"][1]["content"] == "Inspecting"


@pytest.mark.parametrize(
    ("chunks", "reasoning", "content"),
    [
        (
            [
                ("reasoning", "First thought. "),
                ("assistant_output", "Answer."),
                ("reasoning", "More thought."),
            ],
            "First thought. More thought.",
            "Answer.",
        ),
        ([("assistant_output", "<think>Plan</think>")], "Plan", None),
        ([("reasoning", "discarded"), ("assistant_output", "Answer.")], None, "Answer."),
        ([("assistant_output", "discarded")], None, ""),
    ],
)
def test_canonical_message_replaces_only_its_transient_text_sections(
    chunks: list[tuple[str, str]], reasoning: str | None, content: str | None
) -> None:
    timeline = RunTimeline("run")
    emit(timeline, "assistant_output", {"message": {"id": "earlier", "content": "Earlier step"}})
    earlier = block(timeline)["items"][0]
    for kind, text in chunks:
        emit(
            timeline,
            f"{kind}_delta",
            {"reasoning_delta" if kind == "reasoning" else "content_delta": text},
        )
    previous = block(timeline)
    message = {
        "id": "final",
        "role": "assistant",
        "reasoning": reasoning,
        "content": content,
        "interrupted": True,
    }
    if reasoning:
        emit(timeline, "reasoning", {"message": message})
    emit(timeline, "assistant_output", {"message": message})
    items = block(timeline)["items"]
    assert items[0] == earlier
    expected = [
        (kind, text)
        for kind, text in (("reasoning", reasoning), ("assistant_output", content))
        if text
    ]
    assert [(item["type"], item["content"]) for item in items[1:]] == expected
    assert all(not item["streaming"] and item["interrupted"] for item in items[1:])
    assert any(item["streaming"] for item in previous["items"][1:])
    for item in items[1:]:
        previous_match = next(
            (row for row in previous["items"][1:] if row["type"] == item["type"]), None
        )
        if previous_match:
            assert item["id"] == previous_match["id"]
    emit(timeline, "reasoning_delta", {"reasoning_delta": "Following step"})
    emit(
        timeline,
        "assistant_output",
        {"message": {"id": "following", "reasoning": "Following step"}},
    )
    emit(timeline, "reasoning_delta", {"reasoning_delta": "Abandoned step"})
    emit(timeline, "stream_attempt_restarted")
    emit(timeline, "reasoning_delta", {"reasoning_delta": "Retry step"})
    retained = block(timeline)["items"]
    assert len({item["id"] for item in retained}) == len(retained)


def test_reasoning_and_outputs_remain_separate_across_tool_steps() -> None:
    timeline = RunTimeline("run")
    for step in (1, 2):
        call = {"id": f"call-{step}", "name": "read", "arguments": {"path": str(step)}}
        emit(timeline, "reasoning_delta", {"reasoning_delta": "Inspect again."})
        emit(timeline, "assistant_output_delta", {"content_delta": f"Step {step}"})
        message = {
            "id": f"a-{step}",
            "role": "assistant",
            "reasoning": "Inspect again.",
            "content": f"Step {step}",
            "tool_calls": [call],
        }
        emit(timeline, "reasoning", {"message": message})
        emit(timeline, "assistant_output", {"message": message})
        emit(
            timeline,
            "tool_call_result",
            {"assistant_message_id": message["id"], "tool_call": call, "result": {"ok": True}},
        )
    items = block(timeline)["items"]
    assert [item["type"] for item in items] == ["reasoning", "assistant_output", "tool_call"] * 2
    assert [item["content"] for item in items if item["type"] == "reasoning"] == [
        "Inspect again."
    ] * 2
    assert len({item["id"] for item in items}) == 6


def test_interleaved_tool_previews_are_separate_and_become_canonical_in_place() -> None:
    timeline = RunTimeline("run")
    emit(
        timeline,
        "tool_call_delta",
        {
            "tool_call_id": "one",
            "name_delta": "write",
            "arguments_delta": '{"path":"notes.md","content":"par',
        },
    )
    emit(
        timeline,
        "tool_call_delta",
        {"tool_call_id": "two", "name_delta": "read", "arguments_delta": '{"path":"other.md"}'},
    )
    emit(timeline, "tool_call_delta", {"tool_call_id": "one", "arguments_delta": 'tial"}'})
    previews = block(timeline)["items"]
    assert [(item["toolCallId"], item["name"], item["status"]) for item in previews] == [
        ("one", "write", "preparing"),
        ("two", "read", "preparing"),
    ]
    assert previews[0]["partialArgumentsText"] == '{"path":"notes.md","content":"partial"}'
    assert previews[0]["previewArguments"] == {"path": "notes.md", "content": "partial"}
    for call_id, name in (("one", "write"), ("two", "read")):
        emit(
            timeline,
            "tool_call_started",
            {
                "assistant_message_id": "a",
                "tool_call": {"id": call_id, "name": name, "arguments": {"path": "final"}},
            },
        )
    tools = block(timeline)["items"]
    assert [item["id"] for item in tools] == [item["id"] for item in previews]
    assert all(
        item["status"] == "running"
        and item["partialArgumentsText"] is None
        and item["previewArguments"] is None
        for item in tools
    )


def test_repeated_provider_tool_id_does_not_overwrite_an_earlier_step() -> None:
    timeline = RunTimeline("run")
    for step in (1, 2):
        emit(timeline, "tool_call_delta", {"tool_call_id": "reused", "name_delta": "read"})
        emit(
            timeline,
            "tool_call_started",
            {
                "assistant_message_id": f"a-{step}",
                "tool_call": {"id": "reused", "name": "read", "arguments": {"path": str(step)}},
            },
        )
        emit(
            timeline,
            "tool_call_result",
            {
                "assistant_message_id": f"a-{step}",
                "tool_call": {"id": "reused", "name": "read"},
                "result": {"ok": True, "data": {"content": str(step)}},
            },
        )
    tools = block(timeline)["items"]
    assert len({item["id"] for item in tools}) == 2
    assert [item["result"]["data"]["content"] for item in tools] == ["1", "2"]
    assert [item["assistantMessageId"] for item in tools] == ["a-1", "a-2"]


def test_late_provider_id_adoption_preserves_preview_row_and_dispatch_identity() -> None:
    timeline = RunTimeline("run")
    emit(
        timeline,
        "tool_call_delta",
        {"tool_call_id": "stream-slot-0", "name_delta": "read", "arguments_delta": '{"path":"file'},
    )
    previous_id = block(timeline)["items"][0]["id"]
    emit(
        timeline,
        "tool_call_delta",
        {"tool_call_id": "provider-call", "previous_tool_call_id": "stream-slot-0"},
    )
    emit(timeline, "tool_call_delta", {"tool_call_id": "provider-call", "arguments_delta": '.md"}'})
    emit(
        timeline,
        "tool_call_started",
        {
            "assistant_message_id": "a",
            "tool_call": {"id": "provider-call", "name": "read", "arguments": {"path": "file.md"}},
        },
    )
    tools = block(timeline)["items"]
    assert len(tools) == 1
    assert tools[0]["id"] == previous_id
    assert tools[0]["toolCallId"] == "provider-call"
    assert tools[0]["arguments"] == {"path": "file.md"}


@pytest.mark.parametrize("chunk_size", [1, 7, 1000])
def test_tool_argument_preview_only_exposes_completed_top_level_strings(chunk_size: int) -> None:
    timeline = RunTimeline("run")
    arguments = (
        '{"nested":{"path":"hidden"},"array":["hidden"],"number":42,'
        '"path":"C:\\\\notes\\u002emd","content":"unfinished'
    )
    for index in range(0, len(arguments), chunk_size):
        emit(
            timeline,
            "tool_call_delta",
            {"tool_call_id": "call", "arguments_delta": arguments[index : index + chunk_size]},
        )
    assert block(timeline)["items"][0]["previewArguments"] == {"path": "C:\\notes.md"}
    emit(
        timeline,
        "tool_call_delta",
        {"tool_call_id": "call", "arguments_delta": ' text","escaped":"a\\"b"}'},
    )
    assert block(timeline)["items"][0]["previewArguments"] == {
        "path": "C:\\notes.md",
        "content": "unfinished text",
        "escaped": 'a"b',
    }


def test_tool_argument_preview_bounds_long_values_and_skips_invalid_tokens() -> None:
    timeline = RunTimeline("run")
    emit(
        timeline,
        "tool_call_delta",
        {
            "tool_call_id": "call",
            "arguments_delta": '{"content":"' + "x" * 399 + "\\u0061" + "tail" * 1000,
        },
    )
    assert block(timeline)["items"][0]["previewArguments"] is None
    emit(
        timeline,
        "tool_call_delta",
        {"tool_call_id": "call", "arguments_delta": '","bad":"\\q","path":"good.md"}'},
    )
    assert block(timeline)["items"][0]["previewArguments"] == {
        "content": "x" * 200 + "…",
        "path": "good.md",
    }


def test_restarted_attempt_removes_only_its_uncommitted_output() -> None:
    timeline = RunTimeline("run")
    call = {"id": "executed", "name": "read", "arguments": {}}
    emit(
        timeline,
        "assistant_output",
        {"message": {"id": "a", "role": "assistant", "content": "Before", "tool_calls": [call]}},
    )
    emit(timeline, "tool_call_result", {"tool_call": call, "result": {"ok": True}})
    retained_ids = [item["id"] for item in block(timeline)["items"]]
    emit(timeline, "reasoning_delta", {"reasoning_delta": "Abandoned thought"})
    emit(timeline, "assistant_output_delta", {"content_delta": "Abandoned answer"})
    emit(timeline, "tool_call_delta", {"tool_call_id": "draft", "name_delta": "write"})
    emit(timeline, "stream_attempt_restarted")
    assert [item["id"] for item in block(timeline)["items"]] == retained_ids
    emit(timeline, "reasoning_delta", {"reasoning_delta": "Retry thought"})
    emit(timeline, "assistant_output_delta", {"content_delta": "Retry answer"})
    items = block(timeline)["items"]
    assert [item.get("content") for item in items] == [
        "Before",
        None,
        "Retry thought",
        "Retry answer",
    ]
    assert len({item["id"] for item in items}) == 4


@pytest.mark.parametrize(
    "status,expected",
    [
        ("completed", "interrupted"),
        ("failed", "interrupted"),
        ("interrupted", "interrupted"),
        ("cancelled", "cancelled"),
    ],
)
def test_terminal_status_settles_only_unfinished_calls(status: str, expected: str) -> None:
    timeline = RunTimeline("run")
    for call_id in ("finished", "pending"):
        emit(timeline, "tool_call_started", {"tool_call": {"id": call_id, "name": "read"}})
    emit(
        timeline,
        "tool_call_result",
        {"tool_call": {"id": "finished", "name": "read"}, "result": {"ok": True}},
    )
    emit(timeline, "provider_heartbeat", {"idle_seconds": 30})
    emit(
        timeline,
        f"run_{status}",
        {
            "status": status,
            "timing": {
                "started_at": START,
                "completed_at": "2026-10-15T10:00:10Z",
                "duration_ms": 10000,
            },
        },
    )
    run = block(timeline)
    assert run["status"] == status
    assert run["durationMs"] == 10000
    assert run["providerHeartbeat"] is None
    assert [item["status"] for item in run["items"]] == ["success", expected]
    assert not any(item.get("streaming") for item in run["items"])


def test_interruption_preserves_partial_reasoning_and_drops_unexecuted_tool_preview() -> None:
    timeline = RunTimeline("run")
    emit(timeline, "reasoning_delta", {"reasoning_delta": "Half a thought"})
    emit(
        timeline,
        "tool_call_delta",
        {"tool_call_id": "draft", "name_delta": "write", "arguments_delta": '{"path":'},
    )
    emit(
        timeline,
        "assistant_output",
        {
            "message": {
                "id": "partial",
                "role": "assistant",
                "reasoning": "Half a thought",
                "interrupted": True,
            }
        },
    )
    emit(timeline, "run_interrupted", {"cause": "network"})
    items = block(timeline)["items"]
    assert len(items) == 1
    assert items[0]["type"] == "reasoning"
    assert items[0]["content"] == "Half a thought"
    assert items[0]["interrupted"] is True


@pytest.mark.parametrize(
    "reason,remaining",
    [("failed", "failed"), ("stale_context", None), ("insufficient_reclaim", None)],
)
def test_compaction_abort_cannot_leave_a_running_separator(
    reason: str, remaining: str | None
) -> None:
    timeline = RunTimeline("run")
    emit(timeline, "assistant_output", {"message": {"id": "before", "content": "Before"}})
    emit(timeline, "compaction_started", {"context_tokens_before": 120000})
    emit(timeline, "compaction_aborted", {"reason": reason})
    separators = [
        item for item in block(timeline)["items"] if item["type"] == "compaction_separator"
    ]
    assert [item["status"] for item in separators] == ([] if remaining is None else [remaining])
    # A failed separator has no checkpoint message; a repeated steer still shows once.
    steer = {"id": "steer", "role": "user", "content": "Also check the tests"}
    for _ in range(2):
        emit(timeline, "user_message_persisted", {"message": steer})
    assert [item["type"] for item in block(timeline)["items"]].count("user_message") == 1


def test_compaction_restart_and_completion_keep_the_checkpoint_between_output_steps() -> None:
    timeline = RunTimeline("run")
    emit(timeline, "assistant_output", {"message": {"id": "before", "content": "Before"}})
    emit(timeline, "compaction_started", {"context_tokens_before": 120000})
    emit(timeline, "compaction_aborted", {"reason": "stale_context"})
    emit(timeline, "compaction_started", {"context_tokens_before": 125000})
    before_id = block(timeline)["items"][-1]["id"]
    checkpoint = {
        "id": "checkpoint",
        "role": "compaction_checkpoint",
        "usage": {
            "context_tokens_before": 125000,
            "context_tokens_after": 30000,
            "compaction_duration_ms": 2500,
        },
    }
    emit(timeline, "compaction_completed", {"message": checkpoint})
    emit(timeline, "assistant_output", {"message": {"id": "after", "content": "After"}})
    emit(timeline, "run_completed")
    items = block(timeline)["items"]
    assert [item["type"] for item in items] == [
        "assistant_output",
        "compaction_separator",
        "assistant_output",
    ]
    assert items[1]["id"] == before_id
    assert (
        items[1]["status"],
        items[1]["contextTokensBefore"],
        items[1]["contextTokensAfter"],
        items[1]["durationMs"],
    ) == ("completed", 125000, 30000, 2500)


def test_subagent_and_speech_activity_attach_to_their_tool_without_new_rows() -> None:
    timeline = RunTimeline("run")
    emit(
        timeline,
        "tool_call_started",
        {"tool_call": {"id": "delegate", "name": "subagent", "arguments": {}}},
    )
    emit(
        timeline,
        "subagent_session_started",
        {
            "tool_call_id": "delegate",
            "data": {"id": "sub", "session_id": "child", "agent_id": "helper", "status": "running"},
        },
    )
    emit(
        timeline,
        "tool_call_started",
        {"tool_call": {"id": "speech", "name": "generate_speech", "arguments": {}}},
    )
    emit(
        timeline, "speech_playback", {"tool_call_id": "speech", "url": "/api/speech/playback/early"}
    )
    emit(
        timeline,
        "tool_call_result",
        {
            "tool_call": {"id": "speech", "name": "generate_speech"},
            "result": {
                "ok": True,
                "data": {"artifact": {"kind": "speech", "url": "/api/speech/final"}},
            },
        },
    )
    tools = block(timeline)["items"]
    assert len(tools) == 2
    assert tools[0]["subAgentSession"]["session_id"] == "child"
    assert tools[1]["speechPlayback"] == "/api/speech/playback/early"
    assert tools[1]["status"] == "success"


def test_tool_row_previews_its_call_and_keeps_the_whole_call_for_a_raw_read() -> None:
    timeline = RunTimeline("run")
    command = "echo " + "x" * 5_000
    output = "".join(f"line {index}\n" for index in range(5_000))
    call = {"id": "call", "name": "bash", "arguments": {"command": command, "timeout": 5}}
    details = [
        {"type": "text", "label": label, "source": {"from": origin, "path": path}}
        for label, origin, path in (
            ("command", "arguments", ["command"]),
            ("output", "result", ["data", "output"]),
            ("input", "arguments", ["missing"]),
        )
    ]
    started = {"details": details, "secrets_used": ["TOKEN (environment)"]}
    emit(timeline, "tool_call_started", {"tool_call": call, "display": started}, second=1)
    emit(
        timeline,
        "tool_call_result",
        {
            "tool_call": call,
            "result": {"ok": True, "error": None, "data": {"output": output}, "artifacts": []},
            "display": {"details": details},
        },
        second=3,
    )
    [tool] = block(timeline)["items"]
    assert {"toolCall", "startedEvent", "resultEvent", "events"}.isdisjoint(tool)
    assert (tool["startedAt"], tool["finishedAt"]) == (
        "2026-10-15T10:00:01Z",
        "2026-10-15T10:00:03Z",
    )
    # Small fields stay whole; long strings are shortened.
    assert tool["rawTruncated"] is True
    assert tool["arguments"]["timeout"] == 5
    assert len(tool["arguments"]["command"]) < len(command)
    assert len(tool["result"]["data"]["output"]) < len(output)
    # Detail texts are resolved from the whole call; an output keeps its end,
    # and a source without text drops its block.
    command_block, output_block = tool["display"]["details"]
    assert (command_block["text"], "truncated" in command_block) == (command, False)
    assert output_block["truncated"] is True
    assert output_block["text"].endswith("line 4999\n")
    assert len(output_block["text"]) < len(output)
    # The result's display keeps the Secrets the started display named.
    assert tool["display"]["secrets_used"] == ["TOKEN (environment)"]
    raw = timeline.raw_tool_call(tool["id"])
    assert raw is not None
    assert raw["arguments"] == call["arguments"]
    assert raw["result"]["data"]["output"] == output
    assert timeline.raw_tool_call("unknown") is None


def test_nonvisual_progress_updates_metadata_and_never_adds_rows() -> None:
    timeline = RunTimeline("run")
    speed = {"output_tokens_per_second": 50, "first_token_ms": 900, "basis": "output"}
    emit(timeline, "provider_request_status", {"state": "retrying", "attempt": 2})
    assert block(timeline)["providerRequestStatus"]["attempt"] == 2
    emit(timeline, "provider_heartbeat", {"idle_seconds": 30})
    assert block(timeline)["providerRequestStatus"] is None
    emit(
        timeline,
        "model_step_usage",
        {
            "iteration_count": 2,
            "session_usage": {"total_tokens": 100},
            "context_usage": {"tokens": 50},
            "usage": {"speed": speed},
            "model": "provider/first",
            "message_id": "first",
        },
    )
    emit(timeline, "run_controls_changed", {"compaction": {"state": "idle"}})
    emit(
        timeline,
        "run_change_stats",
        {"change_stats": {"files": 1, "added": 2, "removed": 0, "paths": ["a"]}},
    )
    emit(
        timeline,
        "run_change_stats",
        {"change_stats": {"files": 0, "added": 0, "removed": 0, "paths": []}},
    )
    assert block(timeline)["items"] == []
    assert block(timeline)["iterationCount"] == 2
    assert block(timeline)["changeStats"]["files"] == 0
    assert timeline.state["session_usage"] == {"total_tokens": 100}
    assert timeline.state["controls_sequence"] == 4
    latency = {"first_token_ms": 20, "duration_ms": 25}
    emit(
        timeline,
        "model_step_usage",
        {
            "usage": {"speed": latency},
            "model": "provider/second",
            "message_id": "second",
        },
    )
    assert timeline.state["output_speed"] == {
        **speed,
        "model": "provider/first",
        "message_id": "first",
    }
    assert timeline.state["output_latency"] == {
        **latency,
        "model": "provider/second",
        "message_id": "second",
    }
    emit(timeline, "assistant_output_delta", {"content_delta": "Alive"})
    assert block(timeline)["providerHeartbeat"] is None
    emit(timeline, "run_cancelled", {"status": "cancelled", "usage": {"speed": latency}})
    assert timeline.state["output_latency"]["model"] == "provider/second"


def test_projection_retains_complete_text_beyond_raw_replay_and_releases_at_settlement() -> None:
    run = Run(run_id="run", agent_id="agent", session_id="session", event_retention_limit=8)
    timeline = run.timeline
    assert timeline is not None
    for index in range(33):
        run.emit("assistant_output_delta", {"content_delta": f"{index},"})
    expected = "".join(f"{index}," for index in range(33))
    assert len(run.events) == 8
    assert block(timeline)["items"][0]["content"] == expected
    run.request_cancel(reason="user")
    assert run.emit("assistant_output_delta", {"content_delta": "late"}) is None
    run.emit(
        "assistant_output",
        {"message": {"id": "partial", "content": expected, "interrupted": True}},
        allow_after_cancel=True,
    )
    run.mark_cancelled()
    assert run.timeline is None
    assert block(timeline)["status"] == "cancelled"
    assert block(timeline)["items"][0]["content"] == expected
    assert block(timeline)["items"][0]["interrupted"] is True
    assert timeline.sequence == run.last_sequence
