"""Extension-owned participant Sessions in the full Statistics report."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import cast

from core.sessions import ChatSessionManager, SessionAddress
from core.statistics import AgentDirectory, StatisticsService
from core.tools import tool_success
from tests.core.sessions.history_fixtures import seed_history
from tests.core.statistics.statistics_test_support import (
    BASE,
    _assistant,
    _FakeAgents,
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
    at=BASE,
    input_tokens: int = 10,
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
                    "input_tokens": input_tokens,
                    "output_tokens": 2,
                    "cost": {"amount_usd": 0.25, "source": "provider"},
                },
            ),
            _tool(name="swarm_board", at=at, envelope=tool_success({}), duration_ms=5),
            _run_summary(status="completed", at=at, duration_ms=10, run_id=f"run-{participant_id}"),
        ],
    )
    return binding.address


def _service(manager: ChatSessionManager, agents: list[str]) -> StatisticsService:
    return StatisticsService(manager, cast(AgentDirectory, _FakeAgents(agents)))


def test_report_counts_owner_managed_sessions_under_their_extension(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
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

    report = _service(manager, ["main"]).report().to_dict()

    agents = {row["agent_id"]: row for row in report["overview"]["agents"]}
    assert set(agents) == {"main", "extension:swarm"}
    assert agents["extension:swarm"]["sessions"] == 2
    assert agents["extension:swarm"]["runs"] == 2
    assert report["overview"]["total_runs"] == 3
    assert report["usage"]["totals"]["measured_input_tokens"] == 21
    assert report["tools"]["by_agent"] == [{"key": "extension:swarm", "count": 2}]
    # Report rows name the group and participant instead of synthetic Agent ids.
    session_titles = [row["session_title"] for row in report["costs"]["top_sessions"]]
    for name in ("Walross", "Xenia"):
        assert any("Parser rework" in title and name in title for title in session_titles)

    [extension] = report["extensions"]["extensions"]
    assert extension["name"] == "swarm"
    assert extension["actor_key"] == "extension:swarm"
    assert extension["activity"]["runs"] == 2
    assert extension["activity"]["costs"]["reported_usd"] == 0.5
    [group] = extension["groups"]
    assert group["title"] == "Parser rework"
    assert group["activity"]["sessions"] == 2
    assert group["activity"]["measured_input_tokens"] == 20
    assert group["activity"]["tool_calls"] == 2
    assert [(row["name"], row["model"]) for row in group["participants"]] == [
        ("Walross", "prov/a"),
        ("Xenia", "prov/b"),
    ]
    assert group["participants"][0]["activity"]["run_status"]["completed"] == 1


def test_windowed_report_keeps_only_groups_with_in_window_activity(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    old = BASE - timedelta(days=30)
    _participant(manager, group_id="swr_old", participant_id="p1", name="Ada", model="p/m", at=old)
    _participant(manager, group_id="swr_new", participant_id="p2", name="Bo", model="p/m")
    service = _service(manager, [])

    windowed = service.report(since=BASE - timedelta(days=1)).to_dict()
    all_time = service.report().to_dict()

    [extension] = windowed["extensions"]["extensions"]
    assert [group["group_id"] for group in extension["groups"]] == ["swr_new"]
    assert extension["activity"]["runs"] == 1
    assert extension["activity"]["sessions"] == 1
    # Without a group title the report leaves naming to the accessor.
    assert extension["groups"][0]["title"] is None
    [extension] = all_time["extensions"]["extensions"]
    assert [group["group_id"] for group in extension["groups"]] == ["swr_new", "swr_old"]


def test_deleted_group_leaves_the_report(tmp_path: Path) -> None:
    manager = ChatSessionManager(tmp_path)
    _participant(manager, group_id="swr_a", participant_id="p1", name="Ada", model="p/m")
    manager.set_temporary_group_title(owner_name="swarm", group_id="swr_a", title="Gone soon")
    service = _service(manager, [])
    assert service.report().to_dict()["extensions"]["extensions"]

    asyncio.run(manager.delete_temporary_group(owner_name="swarm", group_id="swr_a"))

    report = service.report().to_dict()
    assert report["extensions"]["extensions"] == []
    assert report["overview"]["agents"] == []
    assert manager.list_owned_session_summaries() == []
