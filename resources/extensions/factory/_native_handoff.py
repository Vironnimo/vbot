"""Drain a prior task owner before quoting bounded canonical progress."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from core.runs import RunNotFoundError

from ._native_agent_text import native_input

type Json = dict[str, Any]


_CACHE_VERSION = 1


def _source(execution: Json) -> Json | None:
    runtime = execution["runtime"]
    if not runtime.get("address") or not runtime.get("generation_id"):
        return None
    return {
        "execution_id": execution["id"],
        "factory_id": execution["factory_id"],
        "task_id": execution["scope_id"],
        "group_id": execution["group_id"],
        "participant_id": execution["participant_id"],
        "member_id": execution["member_id"],
        "epoch": execution["epoch"],
        "address": deepcopy(runtime["address"]),
        "generation_id": runtime["generation_id"],
        "run_id": runtime.get("run_id"),
    }


def _cached(execution: Json, source: Json) -> Json | None:
    cache = execution["runtime"].get("handoff_progress")
    if (
        isinstance(cache, dict)
        and cache.get("version") == _CACHE_VERSION
        and cache.get("source") == source
        and isinstance(cache.get("progress"), dict)
    ):
        return deepcopy(dict(cache["progress"]))
    return None


def _progress(snapshot: Any, source: Json) -> Json:
    messages: list[Json] = []
    remaining = 3500
    for message in reversed(snapshot.page.messages):
        if message.role not in {"assistant", "tool", "run_summary"}:
            continue
        if remaining <= 0 or len(messages) >= 8:
            break
        text = message.content if isinstance(message.content, str) else ""
        value: Json = {
            "role": message.role,
            "name": message.name or "",
            "text": text[: min(1000, remaining)],
        }
        if message.tool_calls:
            value["tool_calls"] = [
                {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False)[:600],
                }
                for call in message.tool_calls[:4]
            ]
        if message.role == "run_summary":
            value["status"] = message.status
        # Preserve newest progress within the existing per-execution allowance,
        # including Tool arguments and JSON escaping rather than only body text.
        while len(json.dumps(value, ensure_ascii=False)) > remaining:
            if value.get("tool_calls"):
                value["tool_calls"].pop()
            elif value["text"]:
                excess = len(json.dumps(value, ensure_ascii=False)) - remaining
                value["text"] = value["text"][: max(0, len(value["text"]) - excess)]
            else:
                break
        size = len(json.dumps(value, ensure_ascii=False))
        if size > remaining:
            break
        remaining -= size
        messages.append(value)
    return {
        "member_id": source["member_id"],
        **source["address"],
        "generation_id": source["generation_id"],
        "messages": list(reversed(messages)),
    }


async def capture(native: Any, execution: Json) -> Json | None:
    """Cache settled canonical progress before archival; exact retries reuse it."""
    if execution["kind"] != "task":
        return None
    current = await native.store.get_execution(execution["id"])
    source = _source(current)
    if source is None:
        return None
    cached = _cached(current, source)
    if cached is not None:
        return cached
    bindings = await native.groups.list(current["group_id"], limit=100)
    binding = next(
        (item for item in bindings if item.participant_id == current["participant_id"]), None
    )
    if binding is None or binding.generation_id != source["generation_id"]:
        return None
    address = {
        "agent_id": binding.address.agent_id,
        "session_id": binding.address.session_id,
        "project_id": binding.address.project_id,
    }
    if address != source["address"]:
        return None
    try:
        snapshot = await native.groups.inspect(
            current["group_id"], current["participant_id"], {"limit": 20}
        )
    except RunNotFoundError:
        # An unadmitted/previously archived group has no live transcript to read.
        return None
    if snapshot.generation_id != source["generation_id"]:
        return None
    progress = _progress(snapshot, source)
    async with native.service.lock:
        latest = await native.store.get_execution(current["id"])
        if _source(latest) != source:
            return None
        if cached := _cached(latest, source):
            return cached
        runtime = deepcopy(latest["runtime"])
        runtime["handoff_progress"] = {
            "version": _CACHE_VERSION,
            "source": source,
            "progress": progress,
        }
        await native.store.change_execution(current["id"], {"runtime": runtime}, actor="factory")
    return progress


async def prepare(native: Any, execution: Json, token: str) -> bool:
    if execution["kind"] != "task":
        return True
    previous = [
        item
        for item in await native.all_executions(execution["factory_id"])
        if item["kind"] == "task"
        and item["scope_id"] == execution["scope_id"]
        and item["epoch"] < execution["epoch"]
    ]
    for item in previous:
        await native._close(item)
    async with native.service.lock:
        current = await native.store.get_execution(execution["id"])
        pending = current["runtime"].get("pending") or {}
        if not await native._valid(current) or pending.get("id") != token:
            return False
        if pending.get("history_prepared") or not previous:
            return True
    progress: list[Json] = []
    for item in sorted(previous, key=lambda value: value["epoch"], reverse=True)[:2]:
        captured = await capture(native, item)
        if captured is not None:
            progress.append(captured)
    async with native.service.lock:
        current = await native.store.get_execution(execution["id"])
        runtime = deepcopy(current["runtime"])
        pending = runtime.get("pending") or {}
        if not await native._valid(current) or pending.get("id") != token:
            return False
        factory, member, task = await native._scope(current)
        pending["content"] = native_input(
            "task",
            task,
            factory=factory,
            member=member,
            updates=pending.get("native_updates", []),
            handoff=progress,
        )
        pending["history_prepared"] = True
        await native.store.change_execution(current["id"], {"runtime": runtime}, actor="factory")
    return True
