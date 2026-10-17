"""Conversation read projection shared by every interactive Accessor.

History and a live Run use the same semantic reducer. The adapter here gives
persisted records their canonical Run identity; rendering is left to clients.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any

from core.runs import RunTimeline

JsonObject = dict[str, Any]


def _record_key(message: JsonObject) -> str:
    sequence = message.get("history_sequence")
    return f"record-{sequence}" if isinstance(sequence, int) else str(message["id"])


def history_timeline(messages: Sequence[JsonObject]) -> list[JsonObject]:
    """Project complete Run segments from public Session History records."""
    items: list[JsonObject] = []
    run: RunTimeline | None = None
    # Goal changes outside a Run that land between its records, such as a
    # `/goal pause` while it works, follow that Run instead of splitting it.
    held: list[JsonObject] = []

    def flush() -> None:
        nonlocal run
        if run is not None:
            items.extend(
                item
                for item in run.snapshot()
                if item["type"] != "assistant_run"
                or item.get("items")
                or item.get("status") in {"cancelled", "interrupted", "failed"}
            )
            run = None
        items.extend(held)
        held.clear()

    for message in messages:
        role = message.get("role")
        record_key = _record_key(message)
        run_id = message.get("history_run_id") or message.get("run_id")
        if not run_id and run is not None and role in {"assistant", "tool", "run_summary"}:
            run_id = run.run_id
        if role == "goal_event" and not run_id:
            row = {
                "id": f"goal-{message['id']}",
                "type": "goal_event",
                "timestamp": message.get("timestamp"),
                "message": copy.deepcopy(message),
            }
            if run is None:
                items.append(row)
            else:
                held.append(row)
            continue
        if run is not None and run.run_id != run_id:
            flush()
        if role in {"note", "history_edit", "system"}:
            continue
        if role == "agent_takeover":
            flush()
            items.append(
                {
                    "id": f"takeover-{record_key}",
                    "type": "takeover_separator",
                    "timestamp": message.get("timestamp"),
                    "message": copy.deepcopy(message),
                }
            )
            continue
        if not run_id:
            if role == "compaction_checkpoint":
                items.append(
                    {
                        "id": f"compaction-{record_key}",
                        "type": "compaction_separator",
                        "timestamp": message.get("timestamp"),
                        "message": copy.deepcopy(message),
                        "durationMs": (message.get("usage") or {}).get("compaction_duration_ms"),
                    }
                )
                continue
            previous_role = (items[-1].get("message") or {}).get("role") if items else None
            if role == "assistant" and (
                message.get("tool_calls") or previous_role in {"user", "assistant"}
            ):
                run_id = f"message-{record_key}"
            else:
                items.append(
                    {
                        "id": f"message-{record_key}",
                        "type": "message",
                        "message": copy.deepcopy(message),
                    }
                )
                continue
        if run is None:
            run = RunTimeline(str(run_id), timestamp=message.get("timestamp"))
            run.run["status"] = "completed"
            run.run["source"] = "history"
        event: JsonObject = {
            "run_id": run.run_id,
            "sequence": 0,
            "timestamp": message.get("timestamp"),
            "payload": {"message": message},
        }
        if role == "user":
            event["type"] = "user_message_persisted"
        elif role == "assistant":
            event["type"] = "assistant_output"
        elif role == "tool":
            event.update(
                type="tool_call_result",
                payload={
                    "message": message,
                    "result": message.get("content"),
                    "tool_call": {"id": message.get("tool_call_id"), "name": message.get("name")},
                    "display": message.get("tool_display"),
                    "timing": message.get("timing"),
                },
            )
        elif role == "error":
            event["type"] = "error_message_persisted"
        elif role == "model_fallback":
            event.update(type="model_fallback_activated", payload=message)
        elif role == "goal_event":
            event.update(type="goal_event_recorded", payload=message)
        elif role == "compaction_checkpoint":
            event["type"] = "compaction_completed"
        elif role == "run_summary":
            event.update(type=f"run_{message.get('status', 'completed')}", payload=message)
            run.run["runSummaryMessage"] = copy.deepcopy(message)
        else:
            flush()
            items.append(
                {
                    "id": f"message-{record_key}",
                    "type": "message",
                    "runId": run_id,
                    "run_id": run_id,
                    "message": copy.deepcopy(message),
                }
            )
            continue
        run.apply(event)
    flush()
    return items
