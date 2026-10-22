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

from core.chat.timeline import history_timeline, history_tool_call
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


def window_timeline_items(items: list[JsonObject], limit: int) -> list[JsonObject]:
    """Bound each Run's initial delivery without changing its semantic identity."""
    if not limit:
        return items
    result = []
    for item in items:
        if item.get("type") != "assistant_run":
            result.append(item)
            continue
        children = item.get("items", [])
        start = max(0, len(children) - limit)
        visible = [
            {**child, "position": index} for index, child in enumerate(children) if index >= start
        ]
        if start:
            visible.insert(0, history_gap(item, children[start]["id"], start))
        result.append({**item, "items": visible, "window_start": start})
    return result


def history_gap(run: JsonObject, before: str, remaining: int) -> JsonObject:
    return {
        "id": f"{run['id']}-history-gap",
        "type": "history_gap",
        "run_id": run.get("run_id", run.get("runId")),
        "before": before,
        "remaining": remaining,
        "position": -1,
    }


def window_timeline_update(
    previous: JsonObject, current: JsonObject, known_children: set[str]
) -> JsonObject:
    """Sparse updates also work when a client has not loaded earlier children.

    Previously undelivered children are self-contained. Delivered text keeps
    using suffix edits; positions replace an ever-growing full order array.
    """
    patch = timeline_update(previous, current)
    changed = {child["id"] for child in patch["children"]}
    changed.update(
        child["id"] for child in patch["child_updates"] if child["id"] not in known_children
    )
    patch["child_updates"] = [
        child for child in patch["child_updates"] if child["id"] in known_children
    ]
    run = next(item for item in current["items"] if item["type"] == "assistant_run")
    old_run = next(item for item in previous["items"] if item["type"] == "assistant_run")
    old_positions = {child["id"]: index for index, child in enumerate(old_run["items"])}
    patch["positions"] = {
        child["id"]: index
        for index, child in enumerate(run["items"])
        if child["id"] not in changed and old_positions.get(child["id"]) != index
    }
    patch["children"] = [
        {**child, "position": index}
        for index, child in enumerate(run["items"])
        if child["id"] in changed
    ]
    patch.pop("order", None)
    patch["windowed"] = True
    known_children.difference_update(patch["remove_children"])
    known_children.update(changed)
    return patch


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
    child_limit: int = 0,
) -> JsonObject:
    """Add complete semantic elements to an already public History response."""
    response = {**history_response, "timeline_version": TIMELINE_VERSION}
    last_usage = next(
        (
            message["usage"]
            for message in reversed(history_response.get("messages") or [])
            if message.get("role") == "assistant" and isinstance(message.get("usage"), dict)
        ),
        None,
    )
    if last_usage is not None or not history_response.get("incremental"):
        response["last_usage"] = last_usage
    items = history_timeline(history_response.get("messages") or [])
    if active_run_snapshot is not None:
        for field in ("output_speed", "output_latency"):
            if field in active_run_snapshot.get("state", {}):
                response[field] = active_run_snapshot["state"][field]
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
    if child_limit:
        response["items"] = window_timeline_items(items, child_limit)
        # The semantic rows carry the visible bodies. Raw User records remain
        # for existing edit admission bookkeeping; Tool payloads are not sent twice.
        response["messages"] = [
            message for message in response.get("messages", []) if message.get("role") == "user"
        ]
        response["windowed"] = True
    return response


async def timeline_snapshot(state: Any, run: Run, *, child_limit: int = 0) -> JsonObject:
    """Read exactly one Run, including unretained live output or durable completion."""
    snapshot = capture_run_timeline(run)
    if snapshot is None:
        address = SessionAddress(run.project_id, run.agent_id, run.session_id)
        session = await state.runtime.chat_sessions.get_async(address)
        messages = await session.load_run_messages_async(run.id)

        def project() -> tuple[list[JsonObject], JsonObject]:
            from core.chat import output_measurements

            records = [
                {**record, "history_run_id": run.id}
                for message in messages
                if (record := history_message(message, file_delivery=state.file_delivery))
                is not None
            ]
            output: JsonObject = {}
            for message in messages:
                if message.role == "assistant":
                    output.update(
                        output_measurements(
                            message.usage,
                            model=message.model,
                            message_id=message.id,
                        )
                    )
            return history_timeline(records), output

        items, output = await FILE_PREVIEW_WORKERS.run(project)
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
                **output,
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
    if child_limit:
        snapshot = {
            **snapshot,
            "items": window_timeline_items(snapshot["items"], child_limit),
            "windowed": True,
        }
    return await _public_frame(state, snapshot, include_file_urls=False)


async def timeline_children(
    state: Any,
    address: SessionAddress,
    run_id: str,
    before: str,
    limit: int,
    generation: str,
    *,
    include_file_urls: bool = False,
) -> JsonObject:
    """Read an earlier child page of exactly one addressed Run, including live work.

    ``include_file_urls`` lists the page's verified file links for a host that
    grants them per page, such as an Extension page.
    """
    session = await state.runtime.chat_sessions.get_async(address)
    if session.generation_id != generation:
        raise ValueError("The Session changed; reload its history.")
    active = state.chat_runs.active_run(
        agent_id=address.agent_id, session_id=address.session_id, project_id=address.project_id
    )
    if active is not None and active.id == run_id:
        captured = capture_run_timeline(active)
        assert captured is not None
        items = captured["items"]
    else:
        messages = await session.load_run_messages_async(run_id)

        def project() -> list[JsonObject]:
            return history_timeline(
                [
                    {**record, "history_run_id": run_id}
                    for message in messages
                    if (record := history_message(message, file_delivery=state.file_delivery))
                    is not None
                ]
            )

        items = await FILE_PREVIEW_WORKERS.run(project)
    run = next((item for item in items if item.get("type") == "assistant_run"), None)
    children = [] if run is None else run.get("items", [])
    end = next((index for index, child in enumerate(children) if child["id"] == before), None)
    if run is None or end is None:
        raise ValueError("The history position is unavailable; reload the conversation.")
    start = max(0, end - limit)
    page = [
        {**child, "position": index} for index, child in enumerate(children) if start <= index < end
    ]
    if start:
        page.insert(0, history_gap(run, children[start]["id"], start))
    return await _public_frame(
        state,
        {
            "run_id": run_id,
            "history_generation": generation,
            "items": page,
            "before": before,
            "has_more": start > 0,
        },
        include_file_urls=include_file_urls,
    )


async def timeline_tool_call(
    state: Any,
    address: SessionAddress,
    run_id: str,
    tool_id: str,
    generation: str,
    *,
    include_file_urls: bool = False,
) -> JsonObject:
    """Read the complete arguments and result of one Tool row of an addressed Run.

    Timeline rows carry only previews of them; a client reads the raw call when
    the user opens it. ``include_file_urls`` lists its verified file links for a
    host that grants them per read, such as an Extension page.
    """
    session = await state.runtime.chat_sessions.get_async(address)
    if session.generation_id != generation:
        raise ValueError("The Session changed; reload its history.")
    active = state.chat_runs.active_run(
        agent_id=address.agent_id, session_id=address.session_id, project_id=address.project_id
    )
    raw = None
    if active is not None and active.id == run_id and active.timeline is not None:
        raw = active.timeline.raw_tool_call(tool_id)
    if raw is None:
        messages = await session.load_run_messages_async(run_id)

        def project() -> JsonObject | None:
            return history_tool_call(
                [
                    {**record, "history_run_id": run_id}
                    for message in messages
                    if (record := history_message(message, file_delivery=state.file_delivery))
                    is not None
                ],
                tool_id,
            )

        raw = await FILE_PREVIEW_WORKERS.run(project)
    if raw is None:
        raise ValueError("The Tool call is unavailable; reload the conversation.")
    return await _public_frame(
        state,
        {"run_id": run_id, "tool_id": tool_id, "history_generation": generation, **raw},
        include_file_urls=include_file_urls,
    )


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


async def _public_frame(
    state: Any,
    data: JsonObject,
    *,
    include_file_urls: bool,
    file_actor: str | None = None,
    file_expires_at: int | None = None,
) -> JsonObject:
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
    return (
        cast(
            JsonObject,
            state.file_delivery.bind_payload(result, file_actor, expires_at=file_expires_at),
        )
        if state.file_delivery is not None
        else result
    )


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
    child_limit: int = 0,
    file_actor: str | None = None,
    file_expires_at: int | None = None,
) -> AsyncGenerator[str]:
    """Stream one authoritative snapshot, then bounded-rate semantic patches.

    There is deliberately no replay cursor: every connection starts from truth,
    independently of raw Run-event retention. An attached stream holds the
    projection through completion even after Run releases its reference.
    """
    projection = run.timeline
    previous = capture_run_timeline(run, projection)
    if previous is None:
        initial = await timeline_snapshot(state, run, child_limit=child_limit)
    else:
        previous.update(
            timeline_version=TIMELINE_VERSION, history_generation=run.expected_session_generation_id
        )
        initial = previous
        if child_limit:
            initial = {
                **previous,
                "items": window_timeline_items(previous["items"], child_limit),
                "windowed": True,
            }
    initial = await _public_frame(
        state,
        initial,
        include_file_urls=include_file_urls,
        file_actor=file_actor,
        file_expires_at=file_expires_at,
    )
    if shutdown_event is not None and shutdown_event.is_set():
        return
    yield _frame("timeline_snapshot", initial)
    if initial["terminal"] or projection is None or previous is None:
        return
    known_children = {
        child["id"]
        for item in initial["items"]
        if item["type"] == "assistant_run"
        for child in item["items"]
        if child["type"] != "history_gap"
    }
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
            patch = (
                window_timeline_update(previous, current, known_children)
                if child_limit
                else timeline_update(previous, current)
            )
            previous = current
            public = await _public_frame(
                state,
                patch,
                include_file_urls=include_file_urls,
                file_actor=file_actor,
                file_expires_at=file_expires_at,
            )
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
