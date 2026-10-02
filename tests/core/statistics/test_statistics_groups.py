"""Extension group usage: exact owned-Run slices over the shared Statistics index."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from core.chat.messages import ToolCall
from core.runs import Run, RunExecutionOwner
from core.sessions import ChatSession, ChatSessionManager, SessionAddress
from core.tools import tool_failure
from tests.core.sessions.history_fixtures import complete_run
from tests.core.statistics.statistics_test_support import (
    BASE,
    StatisticsFactory,
    _assistant,
    _compaction,
    _index_path,
    _record_canonical_reads,
    _run_summary,
    _tool,
)

pytestmark = pytest.mark.asyncio


def _participant(
    manager: ChatSessionManager,
    participant_id: str = "peer",
    *,
    agent_id: str = "temporary",
    epoch: str = "epoch",
) -> tuple[SessionAddress, RunExecutionOwner]:
    """Bind a ``swarm`` participant Session in ``group``; return it and its Run owner."""
    binding = manager.create_bound_temporary_session(
        SessionAddress(None, agent_id, "participant"),
        owner_name="swarm",
        group_id="group",
        participant_id=participant_id,
        config={},
    )
    owner = RunExecutionOwner("swarm", "group", participant_id, binding.generation_id, epoch)
    return binding.address, owner


async def _start_owned_run(
    manager: ChatSessionManager, session: ChatSession, run_id: str, owner: RunExecutionOwner
) -> ChatSession:
    """Admit *run_id* with its execution owner, as the Run manager does; return its writer."""
    address = session.address
    await manager.start_run(
        Run(
            run_id=run_id,
            agent_id=address.agent_id,
            session_id=address.session_id,
            project_id=address.project_id,
            execution_owner=owner,
        )
    )
    return session.for_run(run_id)


def _usage(input_tokens: int, output_tokens: int = 1) -> dict[str, Any]:
    return {"input_tokens": input_tokens, "output_tokens": output_tokens}


async def test_group_usage_slices_exact_owned_run_in_reused_session(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    session = manager.create("reused").start_run("outside")
    session.append(_assistant(model="outside", at=BASE, usage=_usage(99)))
    complete_run(
        session, _run_summary(status="completed", at=BASE, duration_ms=1, run_id="outside")
    )
    _address, owner = _participant(manager, epoch="0")
    session = await _start_owned_run(manager, session, "owned", owner)
    session.append(_assistant(model="fallback", at=BASE, usage=_usage(2, 3)))
    session.append(_compaction(at=BASE, before=20, after=10))
    complete_run(session, _run_summary(status="completed", at=BASE, duration_ms=1, run_id="owned"))

    usage = await statistics([]).group_usage(owner_name="swarm", group_id="group")

    assert usage["owned_run_count"] == 1
    assert usage["activity"]["totals"]["input_tokens"] == 2
    assert usage["activity"]["totals"]["output_tokens"] == 3
    assert usage["activity"]["models"][0]["model"] == "fallback"
    assert usage["activity"]["compactions"] == 1


async def test_group_usage_stops_open_owned_run_at_ordinary_successor(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    _address, owner = _participant(manager, epoch="0")
    session = await _start_owned_run(manager, manager.create("reused"), "owned", owner)
    session.append(_assistant(model="owned", at=BASE, usage=_usage(2, 3)))
    session = session.start_run("ordinary")
    session.append(_assistant(model="ordinary", at=BASE, usage=_usage(99)))
    complete_run(
        session, _run_summary(status="completed", at=BASE, duration_ms=1, run_id="ordinary")
    )

    usage = await statistics([]).group_usage(owner_name="swarm", group_id="group")

    assert usage["activity"]["totals"]["input_tokens"] == 2
    assert usage["activity"]["models"][0]["model"] == "owned"


async def test_group_usage_empty_owned_run_does_not_claim_same_sequence_successor(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    _address, owner = _participant(manager)
    session = await _start_owned_run(manager, manager.create("ordinary"), "empty-owned", owner)
    session = session.start_run("ordinary")
    session.append(_assistant(model="outside", at=BASE, usage=_usage(99)))

    usage = await statistics([]).group_usage(owner_name="swarm", group_id="group")

    assert usage["activity"]["models"] == []


@pytest.mark.parametrize(
    ("group_id", "query"),
    [("group", {"owner": "other"}), ("", {}), ("group", {"participant_id": ""})],
    ids=["unknown-query-key", "blank-group", "blank-participant"],
)
async def test_group_usage_rejects_an_invalid_request(
    statistics: StatisticsFactory, group_id: str, query: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        await statistics([]).group_usage(owner_name="swarm", group_id=group_id, query=query)


@pytest.mark.parametrize(
    "runs", [3, pytest.param(1001, marks=[pytest.mark.stress, pytest.mark.timeout(300)])]
)
async def test_group_usage_reads_every_page_of_owned_runs(
    manager: ChatSessionManager, statistics: StatisticsFactory, runs: int
) -> None:
    # Owned Runs are listed in pages of 1000.
    address, owner = _participant(manager)
    session = manager.get(address)
    for index in range(runs):
        session = await _start_owned_run(manager, session, f"run-{index}", owner)
        session.append(_assistant(model="owned", at=BASE, usage=_usage(2, 3)))
        complete_run(
            session,
            _run_summary(status="completed", at=BASE, duration_ms=1, run_id=f"run-{index}"),
        )

    usage = await statistics([]).group_usage(owner_name="swarm", group_id="group")

    assert usage["owned_run_count"] == runs
    assert usage["activity"]["totals"]["input_tokens"] == 2 * runs


async def test_group_usage_combines_peers_resumed_runs_and_rebuilds_exactly(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    for peer, model, tokens in (("one", "primary", 3), ("two", "fallback", 5)):
        address, owner = _participant(manager, peer, agent_id=f"temporary-{peer}")
        session = manager.get(address)
        for index in range(2):
            run_id = f"{peer}-{index}"
            at = BASE + timedelta(seconds=index)
            estimated = (
                {"input_tokens_estimated": True, "output_tokens_estimated": True, "estimated": True}
                if peer == "two" and index == 1
                else {}
            )
            session = await _start_owned_run(manager, session, run_id, owner)
            session.append(
                _assistant(
                    model=model,
                    tool_calls=[
                        ToolCall(id=f"call-owned_tool-{at.isoformat()}", name="owned_tool")
                    ],
                    at=at,
                    usage={
                        **_usage(tokens),
                        **estimated,
                        "cache_read_tokens": 2 if peer == "one" else 0,
                    },
                )
            )
            session.append(
                _tool(
                    name="owned_tool",
                    at=at,
                    envelope=tool_failure("rejected", "fixture"),
                    duration_ms=1,
                )
            )
            if index == 0:
                session.append(_compaction(at=BASE, before=20, after=10))
            complete_run(
                session, _run_summary(status="completed", at=BASE, duration_ms=1, run_id=run_id)
            )
        session = session.start_run(f"outside-{peer}")
        session.append(_assistant(model="outside", at=BASE, usage=_usage(99)))
    service = statistics([])

    first = await service.group_usage(owner_name="swarm", group_id="group")
    rebuilt = await statistics([]).group_usage(owner_name="swarm", group_id="group")

    assert first["owned_run_count"] == rebuilt["owned_run_count"] == 4
    assert first["participant_count"] == rebuilt["participant_count"] == 2
    assert first == rebuilt
    activity = first["activity"]
    assert activity["runs"]["total"] == activity["runs"]["completed"] == 4
    assert activity["totals"]["input_tokens"] == 16
    assert activity["totals"]["estimated_input_tokens"] == 5
    assert activity["totals"]["cache_read_tokens"] == 4
    assert activity["compactions"] == 2
    assert activity["tool_calls"] == activity["tool_rejected"] == 4
    assert {row["model"]: row["runs"] for row in activity["models"]} == {
        "primary": 2,
        "fallback": 2,
    }
    filtered = await service.group_usage(
        owner_name="swarm", group_id="group", query={"participant_id": "two"}
    )
    assert filtered["owned_run_count"] == 2
    assert filtered["activity"]["totals"]["input_tokens"] == 10
    assert filtered["activity"]["totals"]["estimated_input_tokens"] == 5
    assert filtered["activity"]["tool_calls"] == 2
    peers = {peer["participant_id"]: peer["activity"] for peer in first["participants"]}
    assert peers["one"]["totals"]["input_tokens"] == 6
    assert peers["two"] == filtered["activity"]


async def test_group_usage_never_loads_or_prunes_unrelated_indexed_sessions(
    tmp_path: Path,
    manager: ChatSessionManager,
    statistics: StatisticsFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unrelated = manager.create("outside")
    unrelated.append(_assistant(model="unrelated", at=BASE, usage={"input_tokens": 1000}))
    service = statistics(["outside"])
    service.report()
    address, owner = _participant(manager)
    session = await _start_owned_run(manager, manager.get(address), "owned", owner)
    session.append(_assistant(model="owned", at=BASE, usage={"input_tokens": 2}))
    reads = _record_canonical_reads(monkeypatch)

    for expected in (2, 5):
        result = await service.group_usage(owner_name="swarm", group_id="group")
        assert result["activity"]["totals"]["input_tokens"] == expected
        session.append(_assistant(model="owned", at=BASE, usage={"input_tokens": 3}))

    assert {session_id for session_id, _cursor in reads} == {address.session_id}
    with closing(sqlite3.connect(_index_path(tmp_path))) as connection:
        indexed = {row[0] for row in connection.execute("SELECT session_id FROM stat_sessions")}
    assert unrelated.id in indexed
