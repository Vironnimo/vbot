"""Shared conversation snapshots and batched semantic Run streams.

Snapshots replace the named Run. Updates are ordered, keyed edits against that
snapshot; a reconnect always starts with a new snapshot, so a slow reader needs
neither an unbounded event queue nor client-side History reconciliation.
"""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import AsyncGenerator
from typing import Any, cast, override

from core.chat.timeline import history_timeline
from core.runs import Run, RunTimeline
from core.sessions import SessionAddress
from core.utils.workers import finish_despite_cancel
from server._http_dependencies import StreamingResponse
from server.file_delivery import FILE_PREVIEW_WORKERS
from server.rpc.payloads import (
    file_url_candidates,
    history_message,
    remove_opaque_provider_metadata_async,
    verified_file_urls,
)

JsonObject = dict[str, Any]
TIMELINE_VERSION = 1
TIMELINE_BATCH_INTERVAL_SECONDS = 0.05
_sleep = asyncio.sleep


class TimelineStreamingResponse(StreamingResponse):  # type: ignore[misc]
    """Close the projection stream also when the socket fails during a yield."""

    def __init__(self, content: AsyncGenerator[str], **kwargs: Any) -> None:
        super().__init__(content, **kwargs)
        self._timeline_stream = content

    @override
    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await finish_despite_cancel(self._timeline_stream.aclose())


def capture_run_timeline(run: Run, projection: RunTimeline | None = None) -> JsonObject | None:
    """Freeze the live projection and lifecycle facts before any asynchronous I/O."""
    projection = projection or run.timeline
    if projection is None:
        return None
    return {
        "run_id": run.id,
        "sequence": projection.sequence,
        "items": projection.snapshot(),
        "state": {
            **copy.deepcopy(projection.state),
            "status": projection.run["status"],
            "controls": copy.deepcopy(run.controls()),
            "controls_sequence": run.last_sequence,
            "iteration_count": projection.run["iterationCount"],
            "started_at": projection.run["startTimestamp"],
        },
        "terminal": projection.run["status"] != "running",
    }


def project_timeline_history(
    history_response: JsonObject,
    active_run_snapshot: JsonObject | None = None,
) -> JsonObject:
    """Add complete semantic elements to an already public History response."""
    response = {**history_response, "timeline_version": TIMELINE_VERSION}
    items = history_timeline(history_response.get("messages") or [])
    if active_run_snapshot is not None:
        run_id = active_run_snapshot["run_id"]
        positions = [index for index, item in enumerate(items) if item.get("run_id") == run_id]
        position = positions[0] if positions else len(items)
        items = [item for item in items if item.get("run_id") != run_id]
        items[position:position] = active_run_snapshot["items"]
        response["timeline_sequence"] = active_run_snapshot["sequence"]
        messages = {message["id"]: message for message in history_response.get("messages", [])}
        for item in items:
            message = item.get("message")
            if item.get("type") == "message" and message and message["id"] in messages:
                item["message"] = {**message, **messages[message["id"]]}
    _synchronize_public_text(items)
    response["items"] = items
    response["replace_run_ids"] = list(
        dict.fromkeys(
            [row["run_id"] for row in history_response.get("runs", [])]
            + [item["run_id"] for item in items if item.get("run_id")]
        )
    )
    return response


async def timeline_snapshot(state: Any, run: Run) -> JsonObject:
    """Read exactly one Run, including unretained live output or durable completion."""
    snapshot = capture_run_timeline(run)
    if snapshot is None:
        address = SessionAddress(run.project_id, run.agent_id, run.session_id)
        session = await state.runtime.chat_sessions.get_async(address)
        messages = await session.load_run_messages_async(run.id)

        def records() -> list[JsonObject]:
            return [
                {**record, "history_run_id": run.id}
                for message in messages
                if (record := history_message(message, file_delivery=state.file_delivery))
                is not None
            ]

        items = await FILE_PREVIEW_WORKERS.run(lambda: history_timeline(records()))
        if not any(item["type"] == "assistant_run" for item in items):
            fallback = RunTimeline(run.id, timestamp=run.created_at)
            for event in run.events:
                if event.type in {
                    "run_completed",
                    "run_failed",
                    "run_cancelled",
                    "run_interrupted",
                }:
                    fallback.apply(event.to_dict())
            items.extend(fallback.snapshot())
        snapshot = {
            "run_id": run.id,
            "sequence": run.last_sequence,
            "items": items,
            "state": {
                **copy.deepcopy(run.terminal_payload_extras),
                "status": run.status.value,
                "controls": run.controls(),
                "controls_sequence": run.last_sequence,
                "iteration_count": run.iteration_count,
                "started_at": run.created_at,
            },
            "terminal": run.status.value != "running",
        }
    snapshot["timeline_version"] = TIMELINE_VERSION
    snapshot["history_generation"] = run.expected_session_generation_id
    return await _public_frame(state, snapshot, include_file_urls=False)


def timeline_update(previous: JsonObject, current: JsonObject) -> JsonObject:
    """Return only changed elements/fields; growing text travels as a suffix."""
    old_items = {item["id"]: item for item in previous["items"]}
    new_items = {item["id"]: item for item in current["items"]}
    old_run: JsonObject = next(
        (item for item in old_items.values() if item["type"] == "assistant_run"), {}
    )
    run = next(item for item in new_items.values() if item["type"] == "assistant_run")
    old_children = {item["id"]: item for item in old_run.get("items", [])}
    new_children = {item["id"]: item for item in run["items"]}
    patch: JsonObject = {
        "run_id": current["run_id"],
        "sequence": current["sequence"],
        "base_sequence": previous["sequence"],
        "run": {
            "id": run["id"],
            **{
                key: value
                for key, value in run.items()
                if key not in {"items", "reasoning", "outputs", "tools"}
                and old_run.get(key) != value
            },
        },
        "upsert": [
            item
            for key, item in new_items.items()
            if item["type"] != "assistant_run" and old_items.get(key) != item
        ],
        "remove": list(old_items.keys() - new_items.keys()),
        "children": [],
        "child_updates": [],
        "remove_children": list(old_children.keys() - new_children.keys()),
        "state": current["state"],
        "terminal": current["terminal"],
    }
    for child_id, child in new_children.items():
        old = old_children.get(child_id)
        if old is None:
            patch["children"].append(child)
            continue
        if old == child:
            continue
        append, changed = {}, {}
        for key, value in child.items():
            if old.get(key) == value:
                continue
            if (
                key in {"content", "partialArgumentsText", "name"}
                and isinstance(value, str)
                and isinstance(old.get(key), str)
                and value.startswith(old[key])
            ):
                append[key] = value[len(old[key]) :]
            else:
                changed[key] = value
        patch["child_updates"].append(
            {"id": child_id, "type": child["type"], "append": append, "set": changed}
        )
    if list(old_children) != list(new_children):
        patch["order"] = list(new_children)
    if list(old_items) != list(new_items):
        patch["item_order"] = list(new_items)
    return patch


async def _public_frame(state: Any, data: JsonObject, *, include_file_urls: bool) -> JsonObject:
    result = cast(
        JsonObject,
        await remove_opaque_provider_metadata_async(data, file_delivery=state.file_delivery),
    )
    _synchronize_public_text(result)
    if include_file_urls:
        candidates = file_url_candidates(result)
        result["file_urls"] = (
            await FILE_PREVIEW_WORKERS.run(verified_file_urls, candidates, state.file_delivery)
            if candidates
            else []
        )
    return result


def _synchronize_public_text(value: Any) -> None:
    """A text node displays the same signed content as its canonical Message."""
    if isinstance(value, list):
        for item in value:
            _synchronize_public_text(item)
    elif isinstance(value, dict):
        for item in value.values():
            _synchronize_public_text(item)
        if value.get("type") != "assistant_output":
            return
        target = value.get("set", value)
        messages = target.get("messages") or []
        if messages and isinstance(messages[-1].get("content"), str):
            target["content"] = messages[-1]["content"]
            if isinstance(value.get("append"), dict):
                value["append"].pop("content", None)


def _frame(kind: str, data: JsonObject) -> str:
    encoded = json.dumps(data, separators=(",", ":"))
    return f"id: {data['sequence']}\nevent: {kind}\ndata: {encoded}\n\n"


async def sse_timeline(
    state: Any,
    run: Run,
    *,
    shutdown_event: asyncio.Event | None = None,
    heartbeat_interval_seconds: float = 10.0,
    include_file_urls: bool = False,
    batch_interval_seconds: float = TIMELINE_BATCH_INTERVAL_SECONDS,
) -> AsyncGenerator[str]:
    """Stream one authoritative snapshot, then bounded-rate semantic patches.

    There is deliberately no replay cursor: every connection starts from truth,
    independently of raw Run-event retention. An attached stream holds the
    projection through completion even after Run releases its reference.
    """
    projection = run.timeline
    previous = capture_run_timeline(run, projection)
    if previous is None:
        initial = await timeline_snapshot(state, run)
    else:
        previous.update(
            timeline_version=TIMELINE_VERSION, history_generation=run.expected_session_generation_id
        )
        initial = previous
    initial = await _public_frame(state, initial, include_file_urls=include_file_urls)
    if shutdown_event is not None and shutdown_event.is_set():
        return
    yield _frame("timeline_snapshot", initial)
    if initial["terminal"] or projection is None or previous is None:
        return
    changed_task: asyncio.Task[None] | None = None
    shutdown_task = (
        asyncio.create_task(shutdown_event.wait(), name=f"timeline-shutdown:{run.id}")
        if shutdown_event is not None
        else None
    )
    try:
        while True:
            changed_task = asyncio.create_task(
                run.wait_timeline_change(previous["sequence"]), name=f"timeline-next:{run.id}"
            )
            waiting: set[asyncio.Task[Any]] = {changed_task}
            if shutdown_task is not None:
                waiting.add(shutdown_task)
            while True:
                done, _ = await asyncio.wait(
                    waiting, timeout=heartbeat_interval_seconds, return_when=asyncio.FIRST_COMPLETED
                )
                if shutdown_task in done:
                    return
                if changed_task in done:
                    changed_task.result()
                    changed_task = None
                    break
                yield "event: heartbeat\ndata: {}\n\n"
            if batch_interval_seconds > 0:
                await _sleep(batch_interval_seconds)
            current = capture_run_timeline(run, projection)
            assert current is not None
            patch = timeline_update(previous, current)
            previous = current
            public = await _public_frame(state, patch, include_file_urls=include_file_urls)
            if shutdown_event is not None and shutdown_event.is_set():
                return
            yield _frame("timeline_update", public)
            if current["terminal"]:
                return
    finally:
        tasks = [task for task in (changed_task, shutdown_task) if task is not None]
        for task in tasks:
            task.cancel()
        await finish_despite_cancel(asyncio.gather(*tasks, return_exceptions=True))
