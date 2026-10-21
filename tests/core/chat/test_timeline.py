"""Durable conversation projection agrees with the live semantic Run view."""

from __future__ import annotations

import json
from typing import Any

import pytest

from core.chat.timeline import history_timeline
from core.runs import RunTimeline

JsonObject = dict[str, Any]


def message(message_id: str, role: str, **fields: Any) -> JsonObject:
    return {
        "id": message_id,
        "role": role,
        "history_run_id": "run",
        "timestamp": "2026-10-15T10:00:00Z",
        **fields,
    }


def run_block(items: list[JsonObject]) -> JsonObject:
    return next(item for item in items if item["type"] == "assistant_run")


def project_live(messages: list[JsonObject]) -> list[JsonObject]:
    """Use the actual public event shapes for persisted execution boundaries."""
    timeline = RunTimeline("run")
    for sequence, item in enumerate(messages, 1):
        role = item["role"]
        payload: JsonObject = {"message": item}
        kind = {
            "user": "user_message_persisted",
            "assistant": "assistant_output",
            "tool": "tool_call_result",
            "error": "error_message_persisted",
            "compaction_checkpoint": "compaction_completed",
            "run_summary": f"run_{item.get('status')}",
        }[role]
        if role == "tool":
            payload.update(
                tool_call={"id": item["tool_call_id"], "name": item["name"]},
                result=item["content"],
                display=item.get("tool_display"),
                timing=item.get("timing"),
            )
        elif role == "run_summary":
            payload = item
        timeline.apply(
            {
                "type": kind,
                "run_id": "run",
                "sequence": sequence,
                "timestamp": item["timestamp"],
                "payload": payload,
            }
        )
    return timeline.snapshot()


def visible_contract(items: list[JsonObject]) -> list[JsonObject]:
    """Compare visible identity and facts, excluding source/transport records."""
    fields = (
        "id",
        "type",
        "content",
        "status",
        "interrupted",
        "reasoningSummary",
        "durationMs",
        "toolCallId",
        "arguments",
        "result",
        "display",
        "contextTokensBefore",
        "contextTokensAfter",
    )
    rows: list[JsonObject] = []
    for item in items:
        row = {key: item[key] for key in fields if key in item}
        if item.get("message"):
            row["message"] = item["message"]
        if item["type"] == "assistant_run":
            row["items"] = visible_contract(item["items"])
        rows.append(row)
    return rows


def test_history_and_live_keep_ordered_phases_steering_tools_and_terminal_identity() -> None:
    call = {"id": "call", "name": "read", "arguments": {"path": "README.md"}}
    messages = [
        message("u", "user", content="Original"),
        message(
            "a",
            "assistant",
            content="Before",
            reasoning="Plan",
            reasoning_summary=["Plan"],
            reasoning_timing={"duration_ms": 120},
            tool_calls=[call],
        ),
        message(
            "tool",
            "tool",
            tool_call_id="call",
            name="read",
            content='{"ok":true}',
            tool_display={"summary": "Read README"},
            timing={"duration_ms": 250},
        ),
        message("steer", "user", content="Correction"),
        message("after", "assistant", content="After"),
        message(
            "summary",
            "run_summary",
            run_id="run",
            status="completed",
            iteration_count=2,
            timing={"duration_ms": 500},
        ),
    ]
    live, history = project_live(messages), history_timeline(messages)
    assert visible_contract(history) == visible_contract(live)
    assert run_block(live)["source"] == "live"
    assert run_block(history)["source"] == "history"
    assert [item["type"] for item in run_block(history)["items"]] == [
        "reasoning",
        "assistant_output",
        "tool_call",
        "user_message",
        "assistant_output",
    ]
    assert run_block(history)["iterationCount"] == 2
    assert history[0]["message"]["content"] == "Original"


def test_first_user_input_during_automatic_run_is_steering_in_chronological_place() -> None:
    messages = [
        message("first", "assistant", content="Automatic work"),
        message("steer", "user", content="Please adjust"),
        message("last", "assistant", content="Adjusted"),
    ]
    for items in (project_live(messages), history_timeline(messages)):
        assert len(items) == 1
        children = run_block(items)["items"]
        assert [item["type"] for item in children] == [
            "assistant_output",
            "user_message",
            "assistant_output",
        ]
        assert children[1]["message"]["content"] == "Please adjust"


def test_history_keeps_reused_tool_ids_separate_and_matches_live_child_ids() -> None:
    messages: list[JsonObject] = []
    for step in ("one", "two"):
        messages.extend(
            [
                message(
                    f"assistant-{step}",
                    "assistant",
                    tool_calls=[{"id": "reused", "name": "read", "arguments": {"path": step}}],
                ),
                message(f"result-{step}", "tool", tool_call_id="reused", name="read", content=step),
            ]
        )
    history = run_block(history_timeline(messages))
    live = run_block(project_live(messages))
    assert [item["result"] for item in history["items"]] == ["one", "two"]
    assert [item["id"] for item in history["items"]] == [item["id"] for item in live["items"]]
    assert len({item["id"] for item in history["items"]}) == 2


@pytest.mark.parametrize(
    "status,unfinished",
    [
        ("completed", "interrupted"),
        ("interrupted", "interrupted"),
        ("failed", "interrupted"),
        ("cancelled", "cancelled"),
    ],
)
def test_durable_summary_settles_pending_calls_without_changing_completed_results(
    status: str, unfinished: str
) -> None:
    messages = [
        message(
            "a",
            "assistant",
            tool_calls=[
                {"id": "read", "name": "read", "arguments": {}},
                {"id": "child", "name": "subagent", "arguments": {}},
            ],
        ),
        message("read-result", "tool", tool_call_id="read", name="read", content='{"ok":true}'),
        message("summary", "run_summary", run_id="run", status=status, iteration_count=3),
    ]
    block = run_block(history_timeline(messages))
    assert block["status"] == status
    assert block["iterationCount"] == 3
    assert [(item["toolCallId"], item["status"]) for item in block["items"]] == [
        ("read", "success"),
        ("child", unfinished),
    ]


@pytest.mark.parametrize(
    "name,result,display,expected",
    [
        ("bash", {"ok": False, "error": {"code": "cancelled_by_user"}}, None, "cancelled"),
        ("bash", {"ok": True, "data": {"status": "stopped"}}, None, "cancelled"),
        ("terminal", {"ok": True, "data": {"status": "stopped"}}, None, "success"),
        ("read", {"ok": False, "error": {"code": "missing"}}, None, "failed"),
        ("read", {"ok": False, "error": {"code": "permission_denied"}}, None, "refused"),
        (
            "apply_patch",
            {"ok": True, "data": {}},
            {"details": [{"type": "notice", "level": "error", "text": "Second edit failed"}]},
            "partial",
        ),
    ],
)
def test_tool_outcomes_survive_reload_without_failing_completed_run(
    name: str, result: JsonObject, display: JsonObject | None, expected: str
) -> None:
    messages = [
        message("a", "assistant", tool_calls=[{"id": "call", "name": name, "arguments": {}}]),
        message(
            "tool",
            "tool",
            tool_call_id="call",
            name=name,
            content=json.dumps(result),
            tool_display=display,
        ),
        message("summary", "run_summary", run_id="run", status="completed"),
    ]
    block = run_block(history_timeline(messages))
    assert block["status"] == "completed"
    assert block["items"][0]["status"] == expected


def test_takeover_boundary_and_hidden_records_preserve_distinct_runs() -> None:
    history = [
        message("first", "assistant", content="Before", history_run_id="one"),
        message("note", "note", content="Internal", history_run_id="one"),
        message("system", "system", content="Internal", history_run_id="one"),
        message("edit", "history_edit", history_run_id="one"),
        {"id": "takeover", "role": "agent_takeover", "content": '{"from":"a","to":"b"}'},
        message("next-user", "user", content="Continue", history_run_id="two"),
        message("next-output", "assistant", content="After", history_run_id="two"),
    ]
    items = history_timeline(history)
    assert [item["type"] for item in items] == [
        "assistant_run",
        "takeover_separator",
        "message",
        "assistant_run",
    ]
    assert [items[0]["runId"], items[-1]["runId"]] == ["one", "two"]
    assert items[1]["message"]["content"] == '{"from":"a","to":"b"}'
    assert [item["content"] for item in items[0]["items"]] == ["Before"]


def test_consecutive_automatic_runs_are_not_joined_without_user_anchors() -> None:
    messages = [
        message("first", "assistant", content="Automatic one", history_run_id="one"),
        message(
            "summary-one", "run_summary", run_id="one", history_run_id="one", status="completed"
        ),
        message("second", "assistant", content="Automatic two", history_run_id="two"),
        message(
            "summary-two", "run_summary", run_id="two", history_run_id="two", status="completed"
        ),
    ]
    items = history_timeline(messages)
    assert [item["runId"] for item in items] == ["one", "two"]
    assert [item["items"][0]["content"] for item in items] == ["Automatic one", "Automatic two"]


@pytest.mark.parametrize(
    "status,visible",
    [("completed", False), ("failed", True), ("interrupted", True), ("cancelled", True)],
)
def test_anchorless_summary_only_shows_a_run_when_its_outcome_needs_attention(
    status: str, visible: bool
) -> None:
    items = history_timeline([message("summary", "run_summary", run_id="run", status=status)])
    assert bool(items) is visible
    if visible:
        assert items[0]["status"] == status


def test_canonical_error_and_partial_output_remain_once_with_cancelled_summary() -> None:
    messages = [
        message("partial", "assistant", reasoning="Half", content="Partial", interrupted=True),
        message("error", "error", content="Connection lost"),
        message(
            "summary", "run_summary", run_id="run", status="cancelled", error="Connection lost"
        ),
    ]
    items = history_timeline(messages)
    block = run_block(items)
    assert block["status"] == "cancelled"
    assert all(item["interrupted"] for item in block["items"])
    assert [item["message"]["id"] for item in items if item["type"] == "message"] == ["error"]


def test_checkpoint_and_fallback_stay_between_their_surrounding_phases() -> None:
    checkpoint = message(
        "checkpoint",
        "compaction_checkpoint",
        usage={
            "context_tokens_before": 120000,
            "context_tokens_after": 30000,
            "compaction_duration_ms": 2000,
        },
    )
    messages = [
        message("before", "assistant", content="Before"),
        checkpoint,
        message(
            "fallback",
            "model_fallback",
            from_model="first/model",
            to_model="second/model",
            retry_at="2026-10-19T12:15:00.000000Z",
        ),
        message("after", "assistant", content="After"),
        message("summary", "run_summary", run_id="run", status="completed"),
    ]
    items = run_block(history_timeline(messages))["items"]
    assert [item["type"] for item in items] == [
        "assistant_output",
        "compaction_separator",
        "model_fallback",
        "assistant_output",
    ]
    assert (
        items[1]["contextTokensBefore"],
        items[1]["contextTokensAfter"],
        items[1]["durationMs"],
    ) == (120000, 30000, 2000)
    assert (items[2]["from_model"], items[2]["to_model"], items[2]["retry_at"]) == (
        "first/model",
        "second/model",
        "2026-10-19T12:15:00.000000Z",
    )


def test_subagent_message_rows_sit_where_the_message_arrived() -> None:
    # A message read inside a Run is one of its items; one stored without a Run
    # is its own row.
    entry = {"subagent_id": "sub_a", "title": "Worker", "content": "Found it"}
    messages = [
        message("ask", "user", content="Fix it"),
        message("before", "assistant", content="Working"),
        message("in-run", "subagent_message", messages=[entry]),
        message("after", "assistant", content="Done"),
        message("summary", "run_summary", status="completed"),
        message("idle", "subagent_message", history_run_id=None, messages=[entry]),
    ]
    items = history_timeline(messages)
    assert [item["type"] for item in items] == ["message", "assistant_run", "subagent_message"]
    assert items[2]["message"]["messages"] == [entry]
    children = run_block(items)["items"]
    assert [child["type"] for child in children] == [
        "assistant_output",
        "subagent_message",
        "assistant_output",
    ]
    assert children[1]["messages"] == [entry]


def test_goal_rows_stay_in_order_without_splitting_a_run() -> None:
    # A Run's goal row is one of its items; a change outside any Run is its own row.
    messages = [
        message("set", "goal_event", event="set", objective="Ship it", max_turns=30),
        message("ask", "user", content="/goal Ship it"),
        message("before", "assistant", content="Working"),
        # The user pauses the goal while the Run works.
        message("pause", "goal_event", history_run_id=None, event="paused", reason="user"),
        message("after", "assistant", content="Done"),
        message("summary", "run_summary", status="completed"),
        message("cleared", "goal_event", history_run_id=None, event="cleared"),
    ]
    items = history_timeline(messages)
    # The user's command stays the Run's input message, ahead of its goal row.
    assert [item["type"] for item in items] == [
        "message",
        "assistant_run",
        "goal_event",
        "goal_event",
    ]
    assert items[0]["message"]["content"] == "/goal Ship it"
    assert [item["message"]["event"] for item in items[2:]] == ["paused", "cleared"]
    children = run_block(items)["items"]
    assert [child["type"] for child in children] == [
        "goal_event",
        "assistant_output",
        "assistant_output",
    ]
    assert {key: children[0][key] for key in ("event", "objective", "max_turns")} == {
        "event": "set",
        "objective": "Ship it",
        "max_turns": 30,
    }


def test_widget_values_show_in_the_run_they_were_sent_with() -> None:
    # The note is stored before the user's message, which stays the Run's input.
    widgets = [{"widget_id": "res_a", "title": "Spacing", "state": {"line_height": 1.6}}]
    messages = [
        message("values", "widget_state", widgets=widgets),
        message("ask", "user", content="Take it like this"),
        message("reply", "assistant", content="Done"),
        message("summary", "run_summary", status="completed"),
    ]
    items = history_timeline(messages)
    assert [item["type"] for item in items] == ["message", "assistant_run"]
    assert items[0]["message"]["content"] == "Take it like this"
    children = run_block(items)["items"]
    assert [child["type"] for child in children] == ["widget_state", "assistant_output"]
    assert children[0]["widgets"] == widgets


@pytest.mark.parametrize("abandoned", ["tool", "compaction"])
def test_abandoned_provisional_rows_do_not_change_successor_identity_after_reload(
    abandoned: str,
) -> None:
    timeline = RunTimeline("run")
    preliminary: list[tuple[str, JsonObject]]
    if abandoned == "tool":
        preliminary = [
            ("tool_call_delta", {"tool_call_id": "abandoned", "name_delta": "read"}),
            ("stream_attempt_restarted", {}),
        ]
        messages = [
            message("a", "assistant", tool_calls=[{"id": "call", "name": "read", "arguments": {}}]),
            message("tool", "tool", tool_call_id="call", name="read", content='{"ok":true}'),
        ]
    else:
        preliminary = [
            ("compaction_started", {}),
            ("compaction_aborted", {"reason": "stale_context"}),
        ]
        messages = [message("checkpoint", "compaction_checkpoint")]
    for kind, payload in preliminary:
        timeline.apply({"type": kind, "payload": payload})
    for item in messages:
        if item["role"] == "assistant":
            kind, payload = "assistant_output", {"message": item}
        elif item["role"] == "tool":
            kind, payload = (
                "tool_call_result",
                {
                    "tool_call": {"id": item["tool_call_id"], "name": item["name"]},
                    "message": item,
                    "result": item["content"],
                },
            )
        else:
            kind, payload = "compaction_completed", {"message": item}
        timeline.apply({"type": kind, "payload": payload})
    assert [item["id"] for item in run_block(timeline.snapshot())["items"]] == [
        item["id"] for item in run_block(history_timeline(messages))["items"]
    ]
