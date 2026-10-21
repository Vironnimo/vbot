"""The semantic, disposable projection of one Run's visible events.

The Run folds events here before its bounded replay window drops them. This is
not another event log: text is accumulated once, Tool rows retain their latest
facts, and only presentation-relevant boundary events survive. Sessions remain
the durable source after the Run releases this projection.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime
from typing import Any

from core.runs._timeline_arguments import ToolArgumentPreview

JsonObject = dict[str, Any]
_TERMINAL = {"completed", "cancelled", "failed", "interrupted"}


class RunTimeline:
    """Fold neutral Run events into keyed conversation elements, without I/O."""

    def __init__(self, run_id: str, *, timestamp: str | None = None) -> None:
        self.run_id = run_id
        self.sequence = 0
        self.state: JsonObject = {}
        self.before: list[JsonObject] = []
        self.after: list[JsonObject] = []
        self.run: JsonObject = {
            "id": f"assistant-run-{run_id}",
            "type": "assistant_run",
            "runId": run_id,
            "run_id": run_id,
            "source": "live",
            "timestamp": timestamp,
            "startTimestamp": timestamp,
            "endTimestamp": None,
            "status": "running",
            "timing": None,
            "durationMs": None,
            "providerHeartbeat": None,
            "providerRequestStatus": None,
            "iterationCount": 0,
            "items": [],
            "events": [],
        }
        self._counts: dict[str, int] = {}
        self._first_user: str | None = None
        self._snapshot: list[JsonObject] | None = None
        self._tool_previews: dict[str, ToolArgumentPreview] = {}
        self._dirty_children: set[str] = set()
        self._child_snapshots: dict[str, JsonObject] = {}
        self._text_parts: dict[str, list[str]] = {}
        self._argument_parts: dict[str, list[str]] = {}

    def _new_id(self, kind: str) -> str:
        count = self._counts.get(kind, 0)
        self._counts[kind] = count + 1
        return f"{self.run['id']}-{kind}-{count}"

    def snapshot(self) -> list[JsonObject]:
        """Return an isolated snapshot; subsequent events cannot mutate it."""
        if self._snapshot is None:
            children = []
            for child in self.run["items"]:
                child_id = child["id"]
                if parts := self._text_parts.pop(child_id, None):
                    child["content"] += "".join(parts)
                if parts := self._argument_parts.pop(child_id, None):
                    child["partialArgumentsText"] = (
                        child.get("partialArgumentsText") or ""
                    ) + "".join(parts)
                if child_id in self._dirty_children or child_id not in self._child_snapshots:
                    self._child_snapshots[child_id] = copy.deepcopy(child)
                children.append(self._child_snapshots[child_id])
            self._child_snapshots = {child["id"]: child for child in children}
            self._dirty_children.clear()
            row = copy.deepcopy({key: value for key, value in self.run.items() if key != "items"})
            row["items"] = children
            self._snapshot = [*copy.deepcopy(self.before), row, *copy.deepcopy(self.after)]
        return self._snapshot

    def apply(self, event: JsonObject) -> None:
        """Apply one event. The Run supplies it once, in sequence order."""
        self.sequence = max(self.sequence, int(event.get("sequence") or 0))
        self._snapshot = None
        kind = event.get("type", "")
        payload = event.get("payload") or {}
        timestamp = event.get("timestamp")
        message = payload.get("message") or {}
        self.run["timestamp"] = self.run.get("timestamp") or timestamp
        self.run["startTimestamp"] = self.run.get("startTimestamp") or timestamp
        for key in ("context_usage", "session_usage", "usage", "iteration_count", "controls"):
            if key in payload:
                self.state[key] = copy.deepcopy(payload[key])
        if kind in {"model_step_usage", "assistant_output"}:
            from core.chat import output_measurements

            source = message if kind == "assistant_output" else payload
            self.state.update(
                output_measurements(
                    source.get("usage"),
                    model=source.get("model"),
                    message_id=source.get("id" if message else "message_id"),
                )
            )
        if "controls" in payload:
            self.state["controls_sequence"] = self.sequence
        if "iteration_count" in payload:
            self.run["iterationCount"] = payload["iteration_count"]
        if "change_stats" in payload:
            self.run["changeStats"] = copy.deepcopy(payload["change_stats"])
        if kind == "run_controls_changed":
            self.state.update(controls=copy.deepcopy(payload), controls_sequence=self.sequence)
        if kind not in {"reasoning_delta", "provider_heartbeat", "provider_request_status"}:
            self._freeze_reasoning(timestamp)
        if kind in {
            "reasoning_delta",
            "assistant_output_delta",
            "tool_call_delta",
            "reasoning",
            "assistant_output",
            "tool_call_started",
        }:
            self.run.update(providerHeartbeat=None, providerRequestStatus=None)

        if kind == "user_message_persisted" and message:
            self._user(message, event)
        elif kind == "error_message_persisted" and message:
            self._message(self.after, message)
        elif kind == "run_started":
            self.run["startTimestamp"] = timestamp
        elif kind in {"reasoning_delta", "assistant_output_delta"}:
            child_type = "reasoning" if kind == "reasoning_delta" else "assistant_output"
            text = payload.get(
                "reasoning_delta" if child_type == "reasoning" else "content_delta", ""
            )
            self._text(child_type, text, event, streaming=True)
        elif kind in {"reasoning", "assistant_output"}:
            if message:
                self._finalize_text(message, event)
            if kind == "assistant_output":
                if message.get("interrupted"):
                    self._drop_tool_drafts()
                if not message:
                    self._text(
                        "assistant_output", payload.get("content", ""), event, streaming=False
                    )
                for index, call in enumerate(message.get("tool_calls") or []):
                    self._tool(
                        {
                            **event,
                            "type": "tool_call_started",
                            "payload": {
                                "assistant_message_id": message.get("id"),
                                "tool_call": {"index": index, **call},
                            },
                        }
                    )
        elif kind in {
            "tool_call_delta",
            "tool_call_started",
            "tool_call_output",
            "tool_call_result",
            "subagent_session_started",
            "speech_playback",
        }:
            self._tool(event)
        elif kind == "stream_attempt_restarted":
            # An abandoned attempt has no durable output. Executed Tool rows
            # survive; the next attempt gets the same vacated text slots.
            self.run["items"] = [item for item in self.run["items"] if not item.get("streaming")]
            self._recount_text_slots()
        elif kind == "model_fallback_activated":
            self._fallback(event)
        elif kind == "goal_event_recorded":
            self._goal_event(event)
        elif kind == "subagent_message_recorded":
            self._subagent_message(event)
        elif kind == "widget_state_recorded":
            self._widget_state(event)
        elif kind.startswith("compaction_"):
            self._compaction(event)
        elif kind == "provider_heartbeat":
            self.run.update(
                providerRequestStatus=None,
                providerHeartbeat={
                    "idleSeconds": payload.get("idle_seconds"),
                    "timestamp": timestamp,
                },
            )
        elif kind == "provider_request_status":
            self.run.update(
                providerHeartbeat=None,
                providerRequestStatus=None
                if payload.get("state") == "finished"
                else {**payload, "timestamp": timestamp},
            )
        elif kind.removeprefix("run_") in _TERMINAL and kind.startswith("run_"):
            self._finish(event)

    def _message(self, target: list[JsonObject], message: JsonObject) -> None:
        item = {
            "id": f"message-{message['id']}",
            "type": "message",
            "runId": self.run_id,
            "run_id": self.run_id,
            "message": copy.deepcopy(message),
        }
        index = next((index for index, row in enumerate(target) if row["id"] == item["id"]), None)
        if index is None:
            target.append(item)
        else:
            target[index] = item

    def _user(self, message: JsonObject, event: JsonObject) -> None:
        # A goal or widget-state row of the Run's input precedes its message and
        # keeps it the Run's input.
        if (
            self._first_user is None
            and all(item["type"] in {"goal_event", "widget_state"} for item in self.run["items"])
            and not (event.get("payload") or {}).get("queue_item_id")
        ):
            self._first_user = message["id"]
        if message["id"] == self._first_user:
            self._message(self.before, message)
            return
        if any(item.get("message", {}).get("id") == message["id"] for item in self.run["items"]):
            return
        self.run["items"].append(
            {
                "id": f"steer-{message['id']}",
                "type": "user_message",
                "content": message.get("content"),
                "message": copy.deepcopy(message),
                "timestamp": message.get("timestamp"),
                "events": [copy.deepcopy(event)],
            }
        )

    def _finalize_text(self, message: JsonObject, event: JsonObject) -> None:
        """Replace this Model step's transient sections with its canonical text.

        Providers may interleave answer and Reasoning, or normalize streamed
        answer text into Reasoning only. Earlier, settled Model steps belong to
        different messages and must survive either kind of finalization.
        """
        items = self.run["items"]
        message_id = message.get("id")
        phase = [
            item
            for item in items
            if item["type"] in {"reasoning", "assistant_output"}
            and (item.get("streaming") or (message_id and item.get("messageId") == message_id))
        ]
        position = items.index(phase[0]) if phase else len(items)
        phase_ids = {item["id"] for item in phase}
        self.run["items"] = [item for item in items if item["id"] not in phase_ids]
        canonical = []
        for kind, content in (
            ("reasoning", message.get("reasoning")),
            ("assistant_output", message.get("content")),
        ):
            if not isinstance(content, str) or not content:
                continue
            child = next((item for item in phase if item["type"] == kind), None)
            if child is not None:
                child["messageId"] = message_id
                child["streaming"] = True
                self.run["items"].append(child)
            self._text(kind, content, event, streaming=False)
            canonical.append(self.run["items"].pop())
        self.run["items"][position:position] = canonical
        retained_ids = {item["id"] for item in canonical}
        for child_id in phase_ids - retained_ids:
            self._text_parts.pop(child_id, None)
            self._dirty_children.discard(child_id)

    def _text(self, kind: str, content: Any, event: JsonObject, *, streaming: bool) -> None:
        if not isinstance(content, str) or not content:
            return
        message = (event.get("payload") or {}).get("message") or {}
        message_id = message.get("id")
        items = self.run["items"]
        matching = [item for item in items if item["type"] == kind]
        child = next(
            (
                item
                for item in reversed(matching)
                if message_id and item.get("messageId") == message_id
            ),
            None,
        )
        if child is None and matching:
            latest = matching[-1]
            later = items[items.index(latest) + 1 :]
            same_phase = all(
                item["type"] == "tool_call"
                or (
                    not streaming
                    and item["type"] in {"reasoning", "assistant_output"}
                    and (item.get("streaming") or item.get("messageId") == message_id)
                )
                for item in later
            )
            if latest.get("streaming") and same_phase:
                child = latest
        if child is None:
            child = {
                "id": self._new_id(kind),
                "type": kind,
                "content": "",
                "durationMs": None,
                "durationEstimateMs": None,
                "timestamp": event.get("timestamp"),
                "streaming": streaming,
                "interrupted": False,
                "events": [],
                "messages": [],
            }
            items.append(child)
        self._dirty_children.add(child["id"])
        if streaming:
            self._text_parts.setdefault(child["id"], []).append(content)
        else:
            self._text_parts.pop(child["id"], None)
            child["content"] = content
        child["streaming"] = streaming
        child["interrupted"] = bool(message.get("interrupted"))
        if message:
            child["messageId"] = message_id
            child["messages"] = [copy.deepcopy(message)]
            child["events"] = [copy.deepcopy(event)]
        if kind == "reasoning":
            if not streaming:
                child["reasoningSummary"] = copy.deepcopy(message.get("reasoning_summary"))
                child["durationMs"] = (message.get("reasoning_timing") or {}).get("duration_ms")
                child["durationEstimateMs"] = None
            else:
                payload = event.get("payload") or {}
                index, text = payload.get("summary_index"), payload.get("summary_text")
                sections = child.setdefault("reasoningSummary", [])
                if isinstance(index, int) and isinstance(text, str) and 0 <= index <= len(sections):
                    if index == len(sections):
                        sections.append("")
                    sections[index] += text

    def _tool(self, event: JsonObject) -> None:
        kind = event["type"]
        payload = event.get("payload") or {}
        call = payload.get("tool_call") or {}
        call_id = call.get("id") or payload.get("tool_call_id") or payload.get("id")
        assistant_id = payload.get("assistant_message_id")
        key = f"id-{call_id}" if call_id else f"index-{call.get('index', 0)}"
        previous_key = f"id-{payload.get('previous_tool_call_id')}"
        child = next(
            (
                item
                for item in reversed(self.run["items"])
                if item["type"] == "tool_call"
                and item["key"] in {key, previous_key}
                and (
                    not assistant_id
                    or not item.get("assistantMessageId")
                    or item["assistantMessageId"] == assistant_id
                )
                and not (kind == "tool_call_delta" and item.get("resultEvent"))
            ),
            None,
        )
        if child is None:
            child = {
                "id": self._new_id("tool_call"),
                "identityKey": key,
                "type": "tool_call",
                "key": key,
                "timestamp": event.get("timestamp"),
                "status": "running",
                "name": "",
                "display": None,
                "partialArgumentsText": None,
                "previewArguments": None,
                "result": None,
                "toolCall": None,
                "startedEvent": None,
                "resultEvent": None,
                "timing": None,
                "durationMs": None,
                "output": "",
                "events": [],
            }
            self.run["items"].append(child)
        child["toolCallId"] = call_id or child.get("toolCallId")
        self._dirty_children.add(child["id"])
        child["key"] = key
        child["identityKey"] = key
        if assistant_id:
            child["assistantMessageId"] = assistant_id
        if kind == "tool_call_delta":
            self._argument_parts.setdefault(child["id"], []).append(
                payload.get("arguments_delta", "")
            )
            child.update(
                streaming=True,
                status="preparing",
                name=child["name"] + payload.get("name_delta", ""),
            )
            if isinstance(payload.get("preview_arguments"), dict):
                child["previewArguments"] = copy.deepcopy(payload["preview_arguments"])
            else:
                scanner = self._tool_previews.setdefault(child["id"], ToolArgumentPreview())
                if scanner.push(payload.get("arguments_delta", "")):
                    child["previewArguments"] = scanner.fields()
            return
        if call:
            child["toolCall"] = {**(child.get("toolCall") or {}), **copy.deepcopy(call)}
            child["name"] = call.get("name") or child["name"]
            child["index"] = call.get("index", child.get("index"))
            if "arguments" in call:
                child["arguments"] = copy.deepcopy(call["arguments"])
        if kind == "tool_call_started":
            self._tool_previews.pop(child["id"], None)
            self._argument_parts.pop(child["id"], None)
            child.update(streaming=False, partialArgumentsText=None, previewArguments=None)
            # A final Assistant boundary may arrive after a Tool completed.
            # Never overwrite its richer dispatch/result presentation.
            if child["startedEvent"] is None or "display" in payload:
                child["startedEvent"] = copy.deepcopy(event)
            if "display" in payload:
                child["display"] = copy.deepcopy(payload["display"])
            if not child["resultEvent"]:
                child["status"] = "running"
        elif kind == "tool_call_result":
            message = payload.get("message") or {}
            child.update(
                streaming=False,
                result=copy.deepcopy(payload.get("result", message.get("content"))),
                resultEvent=copy.deepcopy(event),
            )
            child["display"] = copy.deepcopy(
                payload.get("display", message.get("tool_display", child["display"]))
            )
            child["timing"] = copy.deepcopy(payload.get("timing") or message.get("timing"))
            child["durationMs"] = (child["timing"] or {}).get("duration_ms")
            child["status"] = _tool_status(
                child["result"], child["name"], child["display"], payload.get("error")
            )
        elif kind == "tool_call_output":
            child["output"] = payload.get("output", "")
        elif kind == "subagent_session_started":
            child["subAgentSession"] = copy.deepcopy(payload.get("data") or {})
        elif kind == "speech_playback":
            child["speechPlayback"] = payload.get("url")
        # Keep relevant boundary events, never one event per streamed chunk.
        child["events"] = [
            item for item in (child.get("startedEvent"), child.get("resultEvent")) if item
        ]

    def _fallback(self, event: JsonObject) -> None:
        payload = event.get("payload") or {}
        if any(
            item["type"] == "model_fallback"
            and item.get("from_model") == payload.get("from_model")
            and item.get("to_model") == payload.get("to_model")
            for item in self.run["items"]
        ):
            return
        self.run["items"].append(
            {
                "id": self._new_id("model_fallback"),
                "type": "model_fallback",
                "content": payload.get("to_model", ""),
                "from_model": payload.get("from_model", ""),
                "to_model": payload.get("to_model", ""),
                **({"retry_at": payload["retry_at"]} if payload.get("retry_at") else {}),
                "timestamp": event.get("timestamp"),
                "events": [copy.deepcopy(event)],
            }
        )

    def _goal_event(self, event: JsonObject) -> None:
        payload = event.get("payload") or {}
        note_id = payload.get("id")
        child_id = f"goal-{note_id}" if note_id else self._new_id("goal_event")
        if any(item["id"] == child_id for item in self.run["items"]):
            return
        self.run["items"].append(
            {
                "id": child_id,
                "type": "goal_event",
                **{
                    key: copy.deepcopy(payload[key])
                    for key in ("event", "reason", "objective", "turn", "max_turns")
                    if key in payload
                },
                "timestamp": payload.get("timestamp") or event.get("timestamp"),
                "events": [copy.deepcopy(event)],
            }
        )

    def _subagent_message(self, event: JsonObject) -> None:
        payload = event.get("payload") or {}
        note_id = payload.get("id")
        child_id = f"subagent-message-{note_id}" if note_id else self._new_id("subagent_message")
        if any(item["id"] == child_id for item in self.run["items"]):
            return
        self.run["items"].append(
            {
                "id": child_id,
                "type": "subagent_message",
                "messages": copy.deepcopy(payload.get("messages") or []),
                "timestamp": payload.get("timestamp") or event.get("timestamp"),
                "events": [copy.deepcopy(event)],
            }
        )

    def _widget_state(self, event: JsonObject) -> None:
        payload = event.get("payload") or {}
        note_id = payload.get("id")
        child_id = f"widget-state-{note_id}" if note_id else self._new_id("widget_state")
        if any(item["id"] == child_id for item in self.run["items"]):
            return
        self.run["items"].append(
            {
                "id": child_id,
                "type": "widget_state",
                "widgets": copy.deepcopy(payload.get("widgets") or []),
                "timestamp": payload.get("timestamp") or event.get("timestamp"),
                "events": [copy.deepcopy(event)],
            }
        )

    def _compaction(self, event: JsonObject) -> None:
        payload = event.get("payload") or {}
        kind = event["type"]
        message = payload.get("message") or {}
        child = next(
            (
                item
                for item in reversed(self.run["items"])
                if item["type"] == "compaction_separator"
                and (
                    item.get("status") == "running"
                    or (
                        message.get("id") and (item.get("message") or {}).get("id") == message["id"]
                    )
                )
            ),
            None,
        )
        if kind == "compaction_aborted":
            if child is not None:
                self._dirty_children.add(child["id"])
                if payload.get("reason") == "failed":
                    child.update(status="failed", events=[copy.deepcopy(event)])
                    # A failed attempt has no canonical checkpoint. Keep its
                    # warning addressable without consuming a successful
                    # checkpoint's durable ordinal.
                    child["id"] = f"{child['id']}-failed-{event.get('sequence', 0)}"
                else:
                    self.run["items"].remove(child)
                self._counts["compaction_separator"] = sum(
                    item["type"] == "compaction_separator" and item.get("status") != "failed"
                    for item in self.run["items"]
                )
            return
        if child is None:
            child = {
                "id": self._new_id("compaction_separator"),
                "type": "compaction_separator",
                "timestamp": event.get("timestamp"),
                "message": None,
                "contextTokensBefore": None,
                "contextTokensAfter": None,
            }
            self.run["items"].append(child)
        usage = message.get("usage") or {}
        self._dirty_children.add(child["id"])
        child.update(
            status="completed" if kind == "compaction_completed" else "running",
            events=[copy.deepcopy(event)],
        )
        child["contextTokensBefore"] = payload.get(
            "context_tokens_before",
            usage.get("context_tokens_before", child["contextTokensBefore"]),
        )
        child["contextTokensAfter"] = payload.get(
            "context_tokens_after", usage.get("context_tokens_after")
        )
        child["durationMs"] = payload.get("duration_ms", usage.get("compaction_duration_ms"))
        if message:
            child["message"] = copy.deepcopy(message)

    def _finish(self, event: JsonObject) -> None:
        payload = event.get("payload") or {}
        status = payload.get("status") or event["type"].removeprefix("run_")
        timing = payload.get("timing") or {}
        self.run.update(
            status=status,
            endTimestamp=timing.get("completed_at") or event.get("timestamp"),
            timing=copy.deepcopy(timing) or None,
            durationMs=timing.get("duration_ms"),
            providerHeartbeat=None,
            providerRequestStatus=None,
        )
        self.run["startTimestamp"] = timing.get("started_at") or self.run["startTimestamp"]
        self.run["terminalEvent"] = copy.deepcopy(event)
        self.state["status"] = status
        self.run["items"] = [
            item
            for item in self.run["items"]
            if not (item["type"] == "compaction_separator" and item.get("status") == "running")
        ]
        if status == "interrupted":
            self._drop_tool_drafts()
        for child in self.run["items"]:
            self._dirty_children.add(child["id"])
            child["streaming"] = False
            if child["type"] == "tool_call" and not child.get("resultEvent"):
                child["status"] = "cancelled" if status == "cancelled" else "interrupted"
                child["endTimestamp"] = event.get("timestamp")
        if status == "failed" and not self.after and payload.get("error"):
            self._message(
                self.after,
                {
                    "id": f"run-failure-{self.run_id}",
                    "role": "error",
                    "content": payload["error"],
                    "timestamp": event.get("timestamp"),
                },
            )

    def _drop_tool_drafts(self) -> None:
        self.run["items"] = [
            item
            for item in self.run["items"]
            if item["type"] != "tool_call" or not item.get("streaming")
        ]

    def _recount_text_slots(self) -> None:
        for kind in ("reasoning", "assistant_output", "tool_call"):
            # Canonical text can remove an interleaved section, leaving gaps.
            # Counted rows would reuse an id still held by a later settled step.
            self._counts[kind] = 1 + max(
                (
                    int(item["id"].rsplit("-", 1)[1])
                    for item in self.run["items"]
                    if item["type"] == kind
                ),
                default=-1,
            )
        retained = {item["id"] for item in self.run["items"]}
        self._text_parts = {
            key: value for key, value in self._text_parts.items() if key in retained
        }
        self._argument_parts = {
            key: value for key, value in self._argument_parts.items() if key in retained
        }
        self._tool_previews = {
            key: value for key, value in self._tool_previews.items() if key in retained
        }

    def _freeze_reasoning(self, timestamp: str | None) -> None:
        if not timestamp:
            return
        for child in self.run["items"]:
            if (
                child["type"] != "reasoning"
                or not child.get("streaming")
                or child.get("durationEstimateMs") is not None
                or not child.get("timestamp")
            ):
                continue
            try:
                duration = (
                    datetime.fromisoformat(timestamp) - datetime.fromisoformat(child["timestamp"])
                ).total_seconds() * 1000
            except ValueError, TypeError:
                continue
            child["durationEstimateMs"] = max(0, round(duration))
            self._dirty_children.add(child["id"])


def _tool_status(result: Any, name: str, display: Any, error: Any) -> str:
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except ValueError:
            result = None
    if not isinstance(result, dict):
        return "failed" if error else "success"
    failure = result.get("error") or {}
    if isinstance(failure, dict) and failure.get("code") == "cancelled_by_user":
        return "cancelled"
    # The Agent's Permission Rules refused the call: the user's limit, not a fault.
    if isinstance(failure, dict) and failure.get("code") == "permission_denied":
        return "refused"
    result_data = result.get("data")
    if (
        name == "bash"
        and result.get("ok") is True
        and isinstance(result_data, dict)
        and result_data.get("status") == "stopped"
    ):
        return "cancelled"
    if (
        error
        or failure
        or result.get("ok") is False
        or result.get("success") is False
        or result.get("status") in ("error", "failed")
    ):
        return "failed"
    if (
        isinstance(display, dict)
        and isinstance(display.get("details"), list)
        and any(
            record.get("type") == "notice" and record.get("level") == "error"
            for record in display.get("details", [])
            if isinstance(record, dict)
        )
    ):
        return "partial"
    return "success"
