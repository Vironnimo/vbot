"""Tests for statistics groups."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest

from core.runs import RunExecutionOwner
from core.sessions import ChatSessionManager, SessionAddress
from core.statistics import (
    AgentDirectory,
    StatisticsService,
)
from core.tools import tool_failure
from tests.core.statistics.statistics_test_support import (
    BASE,
    _assistant,
    _compaction,
    _FakeAgents,
    _run_summary,
    _tool,
)


@pytest.mark.asyncio
async def test_group_usage_slices_exact_owned_run_in_reused_session(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    service = StatisticsService(manager, cast(AgentDirectory, _FakeAgents([])))
    session = manager.create("reused")
    session.append(
        _assistant(model="outside", at=BASE, usage={"input_tokens": 99, "output_tokens": 1})
    )
    session.append(_run_summary(status="completed", at=BASE, duration_ms=1, run_id="outside"))
    binding = manager.create_bound_temporary_session(
        SessionAddress(None, "temporary", "participant"),
        owner_name="swarm",
        group_id="group",
        participant_id="peer",
        config={},
    )
    owner = RunExecutionOwner("swarm", "group", "peer", binding.generation_id, "0")
    await manager.record_run_owner_async(session.address, run_id="owned", owner=owner)
    session.append(
        _assistant(model="fallback", at=BASE, usage={"input_tokens": 2, "output_tokens": 3})
    )
    session.append(_compaction(at=BASE, before=20, after=10))
    session.append(_run_summary(status="completed", at=BASE, duration_ms=1, run_id="owned"))

    usage = await service.group_usage(owner_name="swarm", group_id="group")

    assert usage["owned_run_count"] == 1
    assert usage["usage"]["totals"]["measured_input_tokens"] == 2
    assert usage["usage"]["totals"]["measured_output_tokens"] == 3
    assert usage["usage"]["models"][0]["model"] == "fallback"
    assert usage["compactions"]["total_compactions"] == 1


@pytest.mark.asyncio
async def test_group_usage_stops_open_owned_run_at_ordinary_successor(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    service = StatisticsService(manager, cast(AgentDirectory, _FakeAgents([])))
    session = manager.create("reused")
    binding = manager.create_bound_temporary_session(
        SessionAddress(None, "temporary", "participant"),
        owner_name="swarm",
        group_id="group",
        participant_id="peer",
        config={},
    )
    owner = RunExecutionOwner("swarm", "group", "peer", binding.generation_id, "0")
    await manager.record_run_owner_async(session.address, run_id="owned", owner=owner)
    session.append(
        _assistant(model="owned", at=BASE, usage={"input_tokens": 2, "output_tokens": 3})
    )
    await manager.record_run_start_async(session.address, run_id="ordinary")
    session.append(
        _assistant(model="ordinary", at=BASE, usage={"input_tokens": 99, "output_tokens": 1})
    )
    session.append(_run_summary(status="completed", at=BASE, duration_ms=1, run_id="ordinary"))

    usage = await service.group_usage(owner_name="swarm", group_id="group")

    assert usage["usage"]["totals"]["measured_input_tokens"] == 2
    assert usage["usage"]["models"][0]["model"] == "owned"


@pytest.mark.asyncio
async def test_group_usage_empty_owned_run_does_not_claim_same_sequence_successor(tmp_path):
    manager = ChatSessionManager(tmp_path)
    service = StatisticsService(manager, cast(AgentDirectory, _FakeAgents([])))
    binding = manager.create_bound_temporary_session(
        SessionAddress(None, "temporary", "participant"),
        owner_name="swarm",
        group_id="group",
        participant_id="peer",
        config={},
    )
    session = manager.create("ordinary")
    owner = RunExecutionOwner("swarm", "group", "peer", binding.generation_id, "epoch")
    await manager.record_run_owner_async(session.address, run_id="empty-owned", owner=owner)
    await manager.record_run_start_async(session.address, run_id="ordinary")
    session.append(
        _assistant(model="outside", at=BASE, usage={"input_tokens": 99, "output_tokens": 1})
    )
    usage = await service.group_usage(owner_name="swarm", group_id="group")
    assert usage["usage"]["models"] == []
    with pytest.raises(ValueError):
        await service.group_usage(owner_name="swarm", group_id="group", query={"owner": "other"})


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_group_usage_pages_canonical_boundaries_above_one_hundred_participants(tmp_path):
    # This paging test creates 101 durable Sessions; Windows disk flushes can exceed 30 seconds.
    manager = ChatSessionManager(tmp_path)
    service = StatisticsService(manager, cast(AgentDirectory, _FakeAgents([])))
    for index in range(101):
        binding = manager.create_bound_temporary_session(
            SessionAddress(None, f"temporary-{index}", "participant"),
            owner_name="swarm",
            group_id="group",
            participant_id=f"peer-{index}",
            config={},
        )
        owner = RunExecutionOwner(
            "swarm", "group", binding.participant_id, binding.generation_id, "epoch"
        )
        await manager.record_run_owner_async(binding.address, run_id=f"run-{index}", owner=owner)
        manager.get(binding.address).append(
            _assistant(model="owned", at=BASE, usage={"input_tokens": 2, "output_tokens": 3})
        )
    usage = await service.group_usage(owner_name="swarm", group_id="group")
    assert usage["participant_count"] == 101
    assert usage["usage"]["totals"]["measured_input_tokens"] == 202


@pytest.mark.asyncio
async def test_group_usage_combines_peers_resumed_runs_and_rebuilds_exactly(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    service = StatisticsService(manager, cast(AgentDirectory, _FakeAgents([])))
    for peer, model, tokens in (("one", "primary", 3), ("two", "fallback", 5)):
        binding = manager.create_bound_temporary_session(
            SessionAddress(None, f"temporary-{peer}", "session"),
            owner_name="swarm",
            group_id="group",
            participant_id=peer,
            config={},
        )
        owner = RunExecutionOwner("swarm", "group", peer, binding.generation_id, "epoch")
        session = manager.get(binding.address)
        for index in range(2):
            run_id = f"{peer}-{index}"
            await manager.record_run_owner_async(session.address, run_id=run_id, owner=owner)
            session.append(
                _assistant(
                    model=model,
                    at=BASE + timedelta(seconds=index),
                    usage={
                        "input_tokens": tokens,
                        "output_tokens": 1,
                        "estimated": peer == "two" and index == 1,
                        "cache_read_tokens": 2 if peer == "one" else 0,
                    },
                )
            )
            session.append(
                _tool(
                    name="owned_tool",
                    at=BASE + timedelta(seconds=index),
                    envelope=tool_failure("rejected", "fixture"),
                    duration_ms=1,
                )
            )
            if index == 0:
                session.append(_compaction(at=BASE, before=20, after=10))
            session.append(_run_summary(status="completed", at=BASE, duration_ms=1, run_id=run_id))
        await manager.record_run_start_async(session.address, run_id=f"outside-{peer}")
        session.append(
            _assistant(model="outside", at=BASE, usage={"input_tokens": 99, "output_tokens": 1})
        )
    first = await service.group_usage(owner_name="swarm", group_id="group")
    rebuilt = await StatisticsService(manager, cast(AgentDirectory, _FakeAgents([]))).group_usage(
        owner_name="swarm", group_id="group"
    )
    assert first["owned_run_count"] == rebuilt["owned_run_count"] == 4
    assert first["participant_count"] == rebuilt["participant_count"] == 2
    assert first["usage"]["totals"]["measured_input_tokens"] == 11
    assert rebuilt["usage"]["totals"]["measured_input_tokens"] == 11
    assert first["usage"]["totals"]["estimated_input_tokens"] == 5
    assert first["usage"]["totals"]["cache_read_tokens"] == 4
    assert (
        first["compactions"]["total_compactions"]
        == rebuilt["compactions"]["total_compactions"]
        == 2
    )
    assert first["tools"]["total_calls"] == rebuilt["tools"]["total_calls"] == 4
    assert {row["model"] for row in first["usage"]["models"]} == {"primary", "fallback"}
    filtered = await service.group_usage(
        owner_name="swarm", group_id="group", query={"participant_id": "two"}
    )
    assert filtered["owned_run_count"] == 2
    assert filtered["usage"]["totals"]["measured_input_tokens"] == 5
    assert filtered["usage"]["totals"]["estimated_input_tokens"] == 5
    assert filtered["tools"]["total_calls"] == 2
