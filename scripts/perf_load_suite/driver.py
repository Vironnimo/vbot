"""Seed the workload and drive concurrent turns through the public RPC/SSE edge."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from scripts.perf_load_suite.directive import PerfDirective
from scripts.perf_load_suite.fixture import PROJECT_DISPLAY_NAME
from scripts.perf_load_suite.metrics import RunRecord, ToolTiming, marker_latencies_ms
from scripts.perf_load_suite.rpc import AsyncRpcClient, RpcCaller, RpcCallError

UI_AGENT_ID = "perf-ui"
TURN_PROMPT = "Carry out the scripted performance turn."
DELTA_EVENT = "assistant_output_delta"
TOOL_RESULT_EVENT = "tool_call_result"
TERMINAL_EVENTS = {
    "run_completed": "completed",
    "run_failed": "failed",
    "run_cancelled": "cancelled",
    "run_interrupted": "interrupted",
}


@dataclass(frozen=True)
class SessionTarget:
    """One Session the load driver sends turns to."""

    index: int
    agent_id: str
    session_id: str


@dataclass(frozen=True)
class Workload:
    """Seeded Project, Agents and Sessions for one concurrency level."""

    project_id: str
    agent_ids: tuple[str, ...]
    sessions: tuple[SessionTarget, ...]
    ui_session: SessionTarget | None


def agent_ids_for(count: int) -> tuple[str, ...]:
    return tuple(f"perf-agent-{index + 1:02d}" for index in range(count))


def seed_workload(
    rpc: RpcCaller,
    *,
    fixture_dir: Path,
    sessions: int,
    identity_agents: int,
    dedicated_ui_agent: bool,
) -> Workload:
    """Create the fixture Project, rooted Identity Agents and their Sessions.

    Agents are created through ``agent.create`` with the default Tool Access
    Policy (all Tools) and rooted in the fixture Project so relative Tool paths
    resolve inside it. With ``dedicated_ui_agent`` the first Session belongs to
    its own Agent and is made that Agent's current Session, so the WebUI opens
    exactly that Session.
    """
    project = rpc.call(
        "project.add", {"cwd": str(fixture_dir), "display_name": PROJECT_DISPLAY_NAME}
    )["project"]
    project_id = str(project["project_id"])
    load_agents = agent_ids_for(max(1, min(identity_agents, sessions)))
    all_agents = (UI_AGENT_ID, *load_agents) if dedicated_ui_agent else load_agents
    for agent_id in all_agents:
        rpc.call("agent.create", {"id": agent_id, "name": agent_id})
        rpc.call("agent.update", {"id": agent_id, "root_project_id": project_id})

    targets: list[SessionTarget] = []
    for index in range(sessions):
        if dedicated_ui_agent and index == 0:
            agent_id = UI_AGENT_ID
        else:
            offset = index - 1 if dedicated_ui_agent else index
            agent_id = load_agents[offset % len(load_agents)]
        created = rpc.call("session.create", {"agent_id": agent_id})
        targets.append(SessionTarget(index, agent_id, str(created["session_id"])))

    ui_session = targets[0] if dedicated_ui_agent and targets else None
    if ui_session is not None:
        rpc.call("agent.update", {"id": UI_AGENT_ID, "current_session_id": ui_session.session_id})
    return Workload(project_id, all_agents, tuple(targets), ui_session)


def apply_run_event(
    record: RunRecord,
    event_type: str,
    payload: dict[str, Any],
    received_at: float,
) -> bool:
    """Fold one Run SSE event into ``record``; return whether it was terminal."""
    if record.first_event_at is None:
        record.first_event_at = received_at
    if event_type == DELTA_EVENT:
        text = payload.get("content_delta")
        if isinstance(text, str) and text:
            if record.first_delta_at is None:
                record.first_delta_at = received_at
            record.delta_events += 1
            record.delta_latencies_ms.extend(marker_latencies_ms(text, received_at))
    elif event_type == TOOL_RESULT_EVENT:
        call = _mapping(payload.get("tool_call"))
        result = _mapping(payload.get("result"))
        timing = _mapping(payload.get("timing"))
        duration = timing.get("duration_ms")
        record.tool_timings.append(
            ToolTiming(
                call_id=str(call.get("id", "")),
                name=str(call.get("name", "")),
                duration_ms=float(duration) if isinstance(duration, int | float) else None,
                ok=result.get("ok") is True,
            )
        )
    elif event_type in TERMINAL_EVENTS:
        record.status = TERMINAL_EVENTS[event_type]
        record.finished_at = received_at
        if record.status != "completed":
            error = payload.get("error") or payload.get("cause") or payload.get("reason")
            record.error = str(error) if error else event_type
        return True
    return False


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


async def run_turn(
    client: AsyncRpcClient,
    target: SessionTarget,
    turn_index: int,
    directive: PerfDirective,
    *,
    timeout_seconds: float,
) -> RunRecord:
    """Send one turn with ``chat.stream`` and follow its Run over SSE."""
    record = RunRecord(
        tag=directive.tag,
        session_index=target.index,
        turn_index=turn_index,
        agent_id=target.agent_id,
        session_id=target.session_id,
        directive=directive,
        sent_at=time.time(),
    )
    try:
        async with asyncio.timeout(timeout_seconds):
            response = await client.call(
                "chat.stream",
                {
                    "agent_id": target.agent_id,
                    "session_id": target.session_id,
                    "content": f"{directive.render()} {TURN_PROMPT}",
                },
            )
            record.accepted_at = time.time()
            if response.get("queued"):
                record.status = "error"
                record.error = "turn was queued behind another Run in its Session"
                return record
            record.run_id = str(response["run_id"])
            async for event in client.events(str(response["sse_url"])):
                received_at = time.time()
                if event.event == "heartbeat":
                    continue
                payload = json.loads(event.data).get("payload") or {}
                if apply_run_event(record, event.event, payload, received_at):
                    break
            if record.finished_at is None:
                record.status = "error"
                record.error = "Run stream closed without a terminal event"
    except TimeoutError:
        record.status = "timeout"
        record.error = f"no terminal event within {timeout_seconds:.0f}s"
        if record.run_id is not None:
            cancel_error = await _cancel_run(client, record.run_id)
            if cancel_error is not None:
                record.error = f"{record.error}; {cancel_error}"
    except (RpcCallError, httpx.HTTPError, KeyError, ValueError) as exc:
        record.status = "error"
        record.error = f"{type(exc).__name__}: {exc}"
    return record


async def _cancel_run(client: AsyncRpcClient, run_id: str) -> str | None:
    """Cancel a timed-out Run; return why that failed, if it did."""
    try:
        await client.call("chat.cancel", {"run_id": run_id, "reason": "perf-load timeout"})
    except (RpcCallError, httpx.HTTPError) as exc:
        return f"chat.cancel failed: {type(exc).__name__}: {exc}"
    return None


async def drive_turns(
    base_url: str,
    targets: Sequence[SessionTarget],
    directive_for: Callable[[SessionTarget, int], PerfDirective],
    *,
    turns: int,
    timeout_seconds: float,
) -> list[RunRecord]:
    """Run ``turns`` sequential turns in every Session, all Sessions concurrently.

    Every Session worker waits at a shared gate so all first turns are sent in
    the same instant; later turns follow as soon as the previous Run ends.
    """
    client = AsyncRpcClient(base_url, max_connections=2 * len(targets) + 8)
    gate = asyncio.Event()

    async def session_worker(target: SessionTarget) -> list[RunRecord]:
        await gate.wait()
        records = []
        for turn_index in range(turns):
            records.append(
                await run_turn(
                    client,
                    target,
                    turn_index,
                    directive_for(target, turn_index),
                    timeout_seconds=timeout_seconds,
                )
            )
        return records

    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(session_worker(target)) for target in targets]
            await asyncio.sleep(0)
            gate.set()
    finally:
        await client.aclose()
    return [record for task in tasks for record in task.result()]
