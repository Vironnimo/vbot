"""Terminal timeline presentation contracts for semantic rows and artifacts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from cli.tui.rendering import (
    file_links,
    item_heading,
    item_text,
    tool_detail,
    tool_time,
    tool_title,
    truncated,
)


def test_truncated_keeps_a_one_line_row_inside_the_room_it_has() -> None:
    path = "Workspace · C:/Users/Viro/.vbot-dev/agents/main/workspace"

    assert truncated(path, len(path)) == path
    assert truncated(path, 120) == path
    assert truncated(path, 20) == "Workspace · C:/User…"
    assert len(truncated(path, 20)) == 20


def _search_tool_call(**overrides: Any) -> JsonObject:
    item: JsonObject = {
        "id": "tool-one",
        "type": "tool_call",
        "name": "session_search",
        "status": "success",
        "arguments": {"query": "telegram", "limit": 20},
        "result": json.dumps({"ok": True, "data": {"items": []}}),
        "display": {
            "summary": "telegram",
            "hidden_argument_keys": ["query"],
            "details": [
                {
                    "type": "text",
                    "label": "query",
                    "source": {"from": "arguments", "path": ["query"]},
                },
                {
                    "type": "results",
                    "items": [
                        {
                            "title": "Telegram photos",
                            "meta": "Assistant",
                            "time": "2026-06-06T10:00:00Z",
                            "text": "sent a logo",
                        }
                    ],
                },
                {"type": "notice", "level": "warning", "text": "Skipped one conversation."},
            ],
        },
    }
    item.update(overrides)
    return item


def test_tool_row_shows_what_ran_and_names_only_the_calls_that_went_wrong() -> None:
    item = _search_tool_call()

    assert tool_title(item) == "session_search · telegram"
    assert tool_title(item | {"status": "failed"}) == "session_search · telegram · failed"

    # Every call says how long it ran, as the WebUI writes durations; a running
    # one counts up from its start and a call still being prepared has none.
    started = "2026-10-16T18:00:00Z"
    assert tool_time(item | {"durationMs": 420}) == "0.4s"
    timing = {"started_at": started, "completed_at": "2026-10-16T18:01:15Z"}
    assert tool_time(item | {"timing": timing}) == "1m 15s"
    running = item | {"status": "running", "startedEvent": {"timestamp": started}}
    assert tool_time(running, datetime(2026, 10, 16, 18, 0, 12, tzinfo=UTC)) == "12s"
    assert tool_time(item | {"status": "preparing", "durationMs": 5}) == ""


def test_tool_detail_reads_its_blocks_and_keeps_hidden_arguments_out() -> None:
    detail = tool_detail(_search_tool_call())

    assert "limit: 20" in detail
    assert "query: telegram" in detail
    assert "Telegram photos · Assistant · 2026-06-06T10:00:00Z" in detail
    assert "  sent a logo" in detail
    assert "Warning: Skipped one conversation." in detail


type JsonObject = dict[str, Any]


def test_semantic_timeline_rows_name_user_steer_takeover_and_compaction_states() -> None:
    steer = {"id": "steer-one", "type": "user_message", "content": "Keep going"}
    takeover = {
        "id": "takeover-one",
        "type": "takeover_separator",
        "message": {"content": '{"from":"worker","to":"worker@project"}'},
    }
    running = {"id": "compact-running", "type": "compaction_separator", "status": "running"}
    failed = {"id": "compact-failed", "type": "compaction_separator", "status": "failed"}
    completed = {
        "id": "compact-completed",
        "type": "compaction_separator",
        "message": {
            "usage": {
                "context_tokens_before": 8000,
                "context_tokens_after": 2000,
                "compaction_duration_ms": 1500,
            }
        },
    }

    assert item_heading(steer) == "User Steer · delivered"
    assert item_text(steer) == "Keep going"
    assert item_heading(takeover) == "Agent takeover"
    assert item_text(takeover) == "worker → worker@project"
    assert item_heading(running) == "Compaction · running"
    assert item_text(running).startswith("Context compaction is running.")
    assert item_heading(failed) == "Compaction · failed"
    assert item_text(failed).startswith("Context compaction failed.")
    assert item_heading(completed) == "Compaction · completed"
    assert item_text(completed) == (
        "Context compaction completed.\nContext: 8,000 → 2,000 tokens\nDuration: 1.5s"
    )


def test_file_links_find_canonical_speech_artifacts_and_original_download_urls() -> None:
    live_result = {
        "ok": True,
        "data": {
            "artifact": {
                "kind": "speech",
                "filename": "spoken.wav",
                "url": "/api/speech/artifacts/speech-one",
            }
        },
        "artifacts": [
            {
                "kind": "speech",
                "filename": "spoken.wav",
                "url": "/api/speech/artifacts/speech-one",
            }
        ],
    }
    history_result = {
        "ok": True,
        "data": {
            "artifact": {
                "kind": "speech",
                "filename": "archived.wav",
                "url": "/api/speech/artifacts/speech-two",
            }
        },
        "artifacts": [
            {
                "kind": "read_media",
                "attachment_id": "att_mediaone",
                "filename": "reference.png",
                "media_type": "image/png",
            }
        ],
    }
    items: list[JsonObject] = [
        {
            "id": "run-one",
            "type": "assistant_run",
            "items": [
                {
                    "id": "speech-live",
                    "type": "tool_call",
                    "name": "generate_speech",
                    "result": live_result,
                },
                {
                    "id": "speech-history",
                    "type": "tool_call",
                    "name": "generate_speech",
                    "result": json.dumps(history_result),
                },
                {"id": "compact", "type": "compaction_separator", "message": None},
            ],
        },
        {
            "id": "file-message",
            "type": "message",
            "message": {
                "role": "assistant",
                "content": "[Original report](/api/files/file-token?version=2)",
            },
        },
    ]

    assert file_links(items) == [
        ("spoken.wav", "/api/speech/artifacts/speech-one"),
        ("reference.png", "/api/attachments/att_mediaone"),
        ("archived.wav", "/api/speech/artifacts/speech-two"),
        ("Original report", "/api/files/file-token?version=2"),
    ]
