"""Session tree usage: one Session's own Runs and its Sub-Agents' Runs."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from core.sessions import ChatSessionManager, SessionAddress
from tests.core.sessions.history_fixtures import complete_run
from tests.core.statistics.statistics_test_support import (
    BASE,
    StatisticsFactory,
    _assistant,
    _index_path,
    _run_summary,
)

pytestmark = pytest.mark.asyncio


def _ran(manager: ChatSessionManager, address: SessionAddress, input_tokens: int) -> None:
    session = manager.get(address).start_run(f"run-{input_tokens}")
    session.append(
        _assistant(model="m", at=BASE, usage={"input_tokens": input_tokens, "output_tokens": 1})
    )
    complete_run(
        session,
        _run_summary(status="completed", at=BASE, duration_ms=1, run_id=f"run-{input_tokens}"),
    )


async def test_session_tree_usage_splits_own_and_subagent_runs_without_pruning(
    tmp_path: Path, manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    unrelated = manager.create("outside")
    unrelated.append(_assistant(model="unrelated", at=BASE, usage={"input_tokens": 1000}))
    service = statistics(["outside"])
    service.report()
    root = manager.create("coder").address
    child = manager.create("helper").address
    # An Extension-owned Sub-Agent Session keeps its owner in the shared index.
    owned = manager.create_bound_temporary_session(
        SessionAddress(None, "temporary", "participant"),
        owner_name="swarm",
        group_id="group",
        participant_id="peer",
        config={},
    ).address
    for address, tokens in ((root, 10), (child, 5), (owned, 7)):
        _ran(manager, address, tokens)

    usage = await service.session_tree_usage_async(
        root, [child, owned, SessionAddress(None, "helper", "gone")]
    )

    assert usage["session"]["input_tokens"] == 10
    assert usage["subagents"]["input_tokens"] == 12
    assert usage["total"]["input_tokens"] == 22
    assert usage["total"]["calls"] == 3
    with closing(sqlite3.connect(_index_path(tmp_path))) as connection:
        indexed = dict(connection.execute("SELECT session_id, owner_name FROM stat_sessions"))
    assert indexed[unrelated.id] == ""
    assert indexed[owned.session_id] == "swarm"
