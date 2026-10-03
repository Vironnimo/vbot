"""Extension-owned participant Sessions in the full Statistics report."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from core.sessions import ChatSessionManager, SessionAddress
from core.tools import tool_success
from core.usage import UsageRecorder
from tests.core.sessions.history_fixtures import seed_history
from tests.core.statistics.statistics_test_support import (
    BASE,
    StatisticsFactory,
    _assistant,
    _run_summary,
    _tool,
    _write_session,
)


def _participant(
    manager: ChatSessionManager,
    *,
    group_id: str,
    participant_id: str,
    name: str,
    model: str,
    at: datetime = BASE,
) -> SessionAddress:
    binding = manager.create_bound_temporary_session(
        SessionAddress(None, f"tmp_{participant_id}", f"ses_{participant_id}"),
        owner_name="swarm",
        group_id=group_id,
        participant_id=participant_id,
        config={"name": name, "model": model},
    )
    seed_history(
        manager.get(binding.address),
        [
            _assistant(
                model=model,
                at=at,
                usage={
                    "input_tokens": 10,
                    "output_tokens": 2,
                    "cost": {"amount_usd": 0.25, "source": "provider"},
                },
            ),
            _tool(name="swarm_board", at=at, envelope=tool_success({}), duration_ms=5),
            _run_summary(status="completed", at=at, duration_ms=10, run_id=f"run-{participant_id}"),
        ],
    )
    return binding.address


def test_report_counts_owner_managed_sessions_under_their_extension(
    manager: ChatSessionManager, statistics: StatisticsFactory, ledger: UsageRecorder
) -> None:
    _write_session(
        manager,
        "main",
        [
            _assistant(model="prov/chat", at=BASE, usage={"input_tokens": 1, "output_tokens": 1}),
            _run_summary(status="completed", at=BASE, duration_ms=1, run_id="run-main"),
        ],
    )
    _participant(manager, group_id="swr_a", participant_id="p1", name="Walross", model="prov/a")
    _participant(manager, group_id="swr_a", participant_id="p2", name="Xenia", model="prov/b")
    manager.set_temporary_group_title(owner_name="swarm", group_id="swr_a", title="Parser rework")

    report = statistics(["main"], usage_recorder=ledger).report()

    agents = {row["agent_id"]: row for row in report["runs"]["agents"]}
    assert set(agents) == {"main", "extension:swarm"}
    assert agents["extension:swarm"]["runs"] == 2
    # Extension work is not an Agent of its own, but its Sessions are active.
    assert report["overview"]["active_agents"] == 1
    assert report["overview"]["active_sessions"] == 3
    assert report["overview"]["runs"]["total"] == 3
    assert report["usage"]["totals"]["input_tokens"] == 21
    assert report["tools"]["by_agent"] == [
        {"agent_id": "extension:swarm", "calls": 2, "rejected": 0, "tool_ms": 10}
    ]
    # Report rows name the group and participant instead of synthetic Agent ids.
    session_titles = [row["session_title"] for row in report["usage"]["top_sessions"]]
    for name in ("Walross", "Xenia"):
        assert any("Parser rework" in title and name in title for title in session_titles)
    # Every Session and Run row carries the same title next to its id.
    participant_titles = {
        row["session_id"]: row["session_title"]
        for row in report["usage"]["top_sessions"]
        if row["agent_id"] == "extension:swarm"
    }
    for rows in (report["usage"]["top_runs"], report["runs"]["longest"]):
        titled = {
            row["session_id"]: row["session_title"]
            for row in rows
            if row["agent_id"] == "extension:swarm"
        }
        assert titled == participant_titles

    [extension] = report["extensions"]["extensions"]
    assert extension["name"] == "swarm"
    assert extension["actor_key"] == "extension:swarm"
    assert extension["activity"]["runs"]["total"] == 2
    assert extension["activity"]["totals"]["reported_cost_usd"] == 0.5
    [group] = extension["groups"]
    assert group["title"] == "Parser rework"
    assert group["activity"]["sessions"] == 2
    assert group["activity"]["totals"]["input_tokens"] == 20
    assert group["activity"]["tool_calls"] == 2
    assert [(row["name"], row["model"]) for row in group["participants"]] == [
        ("Walross", "prov/a"),
        ("Xenia", "prov/b"),
    ]
    assert group["participants"][0]["activity"]["runs"]["completed"] == 1


def test_windowed_report_keeps_only_groups_with_in_window_activity(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    old = BASE - timedelta(days=30)
    _participant(manager, group_id="swr_old", participant_id="p1", name="Ada", model="p/m", at=old)
    _participant(manager, group_id="swr_new", participant_id="p2", name="Bo", model="p/m")
    service = statistics([])

    windowed = service.report(since=BASE - timedelta(days=1), sections=["extensions"])
    all_time = service.report(sections=["extensions"])

    [extension] = windowed["extensions"]["extensions"]
    assert [group["group_id"] for group in extension["groups"]] == ["swr_new"]
    assert extension["activity"]["runs"]["total"] == 1
    assert extension["activity"]["sessions"] == 1
    # Without a group title the report leaves naming to the accessor.
    assert extension["groups"][0]["title"] is None
    [extension] = all_time["extensions"]["extensions"]
    assert [group["group_id"] for group in extension["groups"]] == ["swr_new", "swr_old"]


def test_deleted_group_leaves_the_report(
    manager: ChatSessionManager, statistics: StatisticsFactory
) -> None:
    _participant(manager, group_id="swr_a", participant_id="p1", name="Ada", model="p/m")
    manager.set_temporary_group_title(owner_name="swarm", group_id="swr_a", title="Gone soon")
    service = statistics([])
    assert service.report(sections=["extensions"])["extensions"]["extensions"]

    asyncio.run(manager.delete_temporary_group(owner_name="swarm", group_id="swr_a"))

    report = service.report(sections=["extensions", "runs"])
    assert report["extensions"]["extensions"] == []
    assert report["runs"]["agents"] == []
