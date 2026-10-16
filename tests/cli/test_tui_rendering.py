"""Terminal timeline presentation contracts for semantic rows and artifacts."""

from __future__ import annotations

import json
from typing import Any, override

import pytest
from textual.app import App, ComposeResult

from cli.tui.rendering import (
    TimelineEntry,
    file_links,
    item_heading,
    item_text,
    tool_detail,
    tool_title,
)


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


class _TimelineHost(App[None]):
    @override
    def compose(self) -> ComposeResult:
        yield TimelineEntry()


@pytest.mark.asyncio
async def test_timeline_entry_retains_immutable_child_references_between_frames() -> None:
    child = {"id": "output", "type": "assistant_output", "content": "Stable output"}
    app = _TimelineHost()
    async with app.run_test(size=(100, 30)):
        entry = app.query_one(TimelineEntry)
        first_frame = {
            "id": "run",
            "type": "assistant_run",
            "status": "running",
            "items": [child],
        }
        await entry.update_item(first_frame)
        child_entry = entry.entries["output"]
        body = child_entry.body

        second_frame = {
            "id": "run",
            "type": "assistant_run",
            "status": "completed",
            "items": [child],
        }
        await entry.update_item(second_frame)

        assert entry.item is second_frame
        assert entry.entries["output"] is child_entry
        assert child_entry.item is child
        assert child_entry.body is body
        assert entry.entry_order == ("output",)
