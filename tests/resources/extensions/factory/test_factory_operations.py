"""Factory public Tool, management and serialized WIP admission contracts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from core.runs import RunExecutionOwner, RunKind
from core.sessions import SessionAddress
from resources.extensions.factory.store import FactoryError, FactoryStore
from tests.resources.extensions.factory.factory_test_support import FactoryHarness, build_factory

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def factory(factory_store: FactoryStore, tmp_path: Path) -> AsyncIterator[FactoryHarness]:
    harness = await build_factory(tmp_path, factory_store)
    try:
        yield harness
    finally:
        await harness.service.close()


@pytest.mark.parametrize(
    ("authority", "context_values", "origin", "initial_state"),
    [
        ("user", {"direct_user_input": True}, "user", "queued"),
        ("authorized_task", {"run_kind": RunKind.CRON}, "automation", "queued"),
        ("none", {"run_kind": RunKind.CRON, "direct_user_input": True}, "agent", "proposal"),
        ("user", {"subagent_session": True, "direct_user_input": True}, "agent", "proposal"),
        (
            "authorized_task",
            {
                "execution_owner": RunExecutionOwner(
                    extension="swarm",
                    group_id="owned-group",
                    participant_id="worker",
                    generation_id="generation",
                    epoch="epoch",
                )
            },
            "agent",
            "proposal",
        ),
    ],
    ids=[
        "user",
        "authorized-task",
        "host-authority-required",
        "subagent",
        "execution-owner",
    ],
)
async def test_public_creation_uses_host_authority_and_preserves_provenance(
    factory: FactoryHarness,
    authority: Any,
    context_values: dict[str, Any],
    origin: str,
    initial_state: str,
) -> None:
    factory.host.authority = authority
    context = factory.context(**context_values)
    result = await factory.call(
        {"action": "create", "project": "project", "title": "Objective", "description": "Scope"},
        context=context,
    )
    assert result["ok"] is True
    assert result["data"]["state"] == initial_state
    card_id = result["data"]["id"]
    await factory.settle()
    saved = await factory.store.get_card(card_id)
    assert saved["origin"] == origin
    assert saved["origin_run"] == context.run_id
    assert (await factory.store.history(card_id))["events"][0]["detail"] == {"origin": origin}
    fetched = await factory.call({"action": "get", "id": card_id})
    assert fetched["ok"] is True
    assert fetched["data"]["card"]["origin_session"] == {
        "project_id": context.project_id,
        "agent_id": context.agent_id,
        "session_id": context.session_id,
    }
    delegated = context.subagent_session or context.execution_owner is not None
    assert factory.host.authority_calls == ([] if delegated else [context])
    assert factory.execution.calls == ([] if origin == "agent" else [("start", card_id, "")])
    assert [item["key"] for item in factory.host.notifications] == (
        [card_id] if origin == "agent" else []
    )
    assert factory.host.badges[-1] == ("board", 1 if origin == "agent" else 0)


async def test_public_schema_cannot_supply_creation_authority(factory: FactoryHarness) -> None:
    tool = factory.tools.get("factory_card")
    assert tool.requires_opt_in is False
    assert tool.family == "extension:factory:factory"
    assert not {"origin", "direct_user_input", "run_kind"} & tool.parameters["properties"].keys()
    result = await factory.call(
        {
            "action": "create",
            "project": "project",
            "title": "Objective",
            "description": "Scope",
            "origin": "operator",
        }
    )
    assert result["ok"] is True
    assert result["data"]["state"] == "proposal"
    assert isinstance(result["data"]["note"], str)
    assert (await factory.store.get_card(result["data"]["id"]))["origin"] == "agent"
    await factory.settle()
    assert factory.execution.calls == []


async def test_public_failure_after_persistence_reports_an_unconfirmed_outcome(
    factory: FactoryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(_resource: str, _ids: list[str], _revision: int) -> None:
        raise RuntimeError("The owner retired during publication")

    monkeypatch.setattr(factory.host, "publish_change", unavailable)
    result = await factory.call(
        {"action": "create", "project": "project", "title": "Objective", "description": "Scope"}
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "factory_failed"
    cards = await factory.store.list_cards()
    assert len(cards) == 1 and cards[0]["state"] == "proposal"
    assert len((await factory.store.history(cards[0]["id"]))["events"]) == 1
    assert factory.execution.calls == []


@pytest.mark.parametrize(
    ("state", "reason", "content_field"),
    [
        ("waiting", "approved", "recommendation"),
        ("waiting", "blocked", "question"),
        ("waiting", "run_failed", "note"),
        ("waiting", "no_agreement", None),
        ("waiting", "unknown_reason", None),
        ("proposal", "", None),
    ],
)
async def test_attention_renders_a_plain_summary_without_rewriting_card_content(
    factory: FactoryHarness, state: str, reason: str, content_field: str | None
) -> None:
    card = await factory.card(state, waiting_reason=reason)
    title = "Objective\nwith a second line"
    content = "\n\nTest-owned summary.\nFurther detail."
    patch: dict[str, Any] = {"title": title}
    if content_field == "recommendation":
        patch[content_field] = content
    elif content_field is not None:
        patch["runtime"] = {**card["runtime"], content_field: content}
    card = await factory.store.change(
        card["id"],
        patch,
        actor="reviewer",
        event="reviewed",
    )
    await factory.service.changed(card, {**card, "state": "reviewing"})
    notice = factory.host.notifications[0]
    assert title.replace("\n", " ") in notice["title"]
    if content_field is not None:
        assert notice["body"] == content.strip().splitlines()[0]
    else:
        assert len(notice["body"].split()) > 3
        if reason:
            assert reason not in notice["body"]
    assert notice["key"] == card["id"] and notice["route"] == f"/card/{card['id']}"
    assert (await factory.store.get_card(card["id"]))["title"] == title
    await factory.settle()


async def test_board_badge_counts_waiting_cards_and_proposals_until_resolved(
    factory: FactoryHarness,
) -> None:
    proposal = await factory.card("proposal")
    waiting = await factory.card("waiting", waiting_reason="approved")
    for state in ("queued", "working", "reviewing", "integrating", "done", "discarded"):
        await factory.card(state)
    await factory.service.changed(proposal)
    assert factory.host.badges[-1] == ("board", 2)
    await factory.operation("card.approve_proposal", id=proposal["id"])
    assert factory.host.badges[-1] == ("board", 1)
    saved = await factory.store.change(
        waiting["id"], {"state": "done"}, actor="worker", event="integrated"
    )
    await factory.service.changed(saved, waiting)
    assert factory.host.badges[-1] == ("board", 0)
    await factory.settle()


async def test_public_reads_filter_cards_without_exposing_execution_runtime(
    factory: FactoryHarness,
) -> None:
    await factory.setup("other")
    proposal = await factory.card("proposal")
    await factory.card("waiting", waiting_reason="approved")
    await factory.card("proposal", project_id="other")
    result = await factory.call({"action": "list", "project": "project", "state": "proposal"})
    assert result["ok"] is True
    assert result["data"]["cards"] == [
        {
            key: proposal[key]
            for key in ("id", "project_id", "title", "state", "waiting_reason", "origin")
        }
    ]
    detail = await factory.call({"action": "get", "id": proposal["id"]})
    assert detail["ok"] is True
    assert "runtime" not in detail["data"]["card"]
    assert "integration_landing" not in detail["data"]["card"]
    landing: dict[str, Any] = {
        "head_sha": "captured-head",
        "base_sha": "captured-base",
        "landing_sha": "landing-commit",
        "branch_head_sha": None,
        "status": "changes_requested",
        "extra_paths": ["src/additional.py"],
        "merge_paths": ["src/merged.py"],
    }
    stored_landing = {
        **landing,
        "private_config": {"not_for_the_board": True},
        "extra_paths": [*landing["extra_paths"], {"not_a_path": "private"}],
    }
    saved = await factory.store.change(
        proposal["id"],
        {
            "head_sha": "newer-submission-head",
            "runtime": {**proposal["runtime"], "integration_landing": stored_landing},
        },
        actor="factory",
        event="test_historical_landing",
    )
    projected = await factory.call({"action": "get", "id": proposal["id"]})
    assert projected["ok"] is True
    assert "runtime" not in projected["data"]["card"]
    assert projected["data"]["card"]["integration_landing"] == landing
    board = await factory.operation("board.get", project_id="project")
    on_board = next(card for card in board["cards"] if card["id"] == proposal["id"])
    assert on_board["integration_landing"] == landing
    assert "runtime" not in on_board
    assert (await factory.store.get_card(proposal["id"])) == saved
    assert saved["runtime"]["integration_landing"] == stored_landing
    invalid = await factory.call({"action": "list", "state": "unknown"})
    assert invalid["ok"] is False and invalid["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize("project_id", ["disabled", "unconfigured"])
async def test_creation_requires_enabled_setup_and_names_available_projects(
    factory: FactoryHarness, project_id: str
) -> None:
    await factory.setup("project", enabled=False)
    await factory.setup("other")
    await factory.setup("disabled", enabled=False)
    factory.host.authority = "user"
    result = await factory.call(
        {"action": "create", "project": project_id, "title": "Objective", "description": "Scope"}
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "project_not_enabled"
    assert "other" in result["error"]["message"]
    assert await factory.store.list_cards() == []
    assert factory.host.published == [] and factory.host.notifications == []
    with pytest.raises(FactoryError) as refused:
        await factory.operation(
            "card.create", project_id=project_id, title="Objective", description="Scope"
        )
    assert refused.value.code == "project_not_enabled"


async def test_management_creation_is_operator_owned(factory: FactoryHarness) -> None:
    result = await factory.operation(
        "card.create", project_id="project", title="Objective", description="Scope"
    )
    card = result["card"]
    assert (card["origin"], card["state"], card["origin_session"], card["origin_run"]) == (
        "operator",
        "queued",
        None,
        "",
    )
    assert factory.host.authority_calls == []
    await factory.settle()
    assert factory.execution.calls == [("start", card["id"], "")]


async def test_card_detail_retains_completed_run_status_beside_live_work(
    factory: FactoryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    card = await factory.card("integrating")
    await factory.store.change(
        card["id"],
        {"runtime": {**card["runtime"], "runs": {"worker": "live", "reviewer-1": "ended"}}},
        actor="factory",
        event="test_runs",
    )

    async def participants(_group: str, **_paging: Any) -> list[Any]:
        return [
            SimpleNamespace(
                participant_id=participant,
                address=SessionAddress("project", participant, f"session-{participant}"),
                config={"name": participant, "model": "test/worker"},
            )
            for participant in ("worker", "reviewer-1")
        ]

    async def owned_runs(_group: str, _runs: list[str]) -> dict[str, Any]:
        return {
            "live": SimpleNamespace(run=SimpleNamespace(status=SimpleNamespace(value="running"))),
            "ended": SimpleNamespace(run=None, record=SimpleNamespace(terminal_status="completed")),
        }

    async def usage(_group: str, _arguments: Any) -> dict[str, Any]:
        return {"cost": {"total": 0.01}}

    monkeypatch.setattr(
        factory.host,
        "temporary_agents",
        SimpleNamespace(list=participants, owned_runs=owned_runs, usage=usage),
        raising=False,
    )
    monkeypatch.setattr(factory.host, "pending_approvals", None, raising=False)
    detail = await factory.operation("card.get", id=card["id"])
    assert [(item["state"], item["run_active"]) for item in detail["participants"]] == [
        ("running", True),
        ("completed", False),
    ]
    assert [run["status"] for run in detail["runs"]] == ["running", "completed"]
    assert detail["usage"] == {"cost": {"total": 0.01}}


@pytest.mark.parametrize("review_rounds", [None, 2], ids=["new-default", "explicit-two"])
async def test_setup_updates_preserve_fields_and_reject_stale_revisions(
    factory: FactoryHarness, review_rounds: int | None
) -> None:
    empty = await factory.operation("setup.get", project_id="other")
    assert empty["setup"]["revision"] == 0
    assert empty["setup"]["review_rounds"] == 3
    created = await factory.operation(
        "setup.update",
        project_id="other",
        worker_profile="worker",
        reviewer_profile="reviewer",
        worker_model="test/worker",
        reviewer_model="test/reviewer",
        expected_revision=0,
        **({"review_rounds": review_rounds} if review_rounds is not None else {}),
    )
    assert created["setup"]["revision"] == 1
    assert created["setup"]["review_rounds"] == (review_rounds if review_rounds is not None else 3)
    updated = await factory.operation(
        "setup.update", project_id="other", base_branch="release", expected_revision=1
    )
    assert updated["setup"]["revision"] == 2
    assert updated["setup"]["base_branch"] == "release"
    assert updated["setup"]["review_rounds"] == created["setup"]["review_rounds"]
    assert updated["setup"]["worker_model"] == "test/worker"
    assert updated["setup"]["reviewer_model"] == "test/reviewer"
    before = list(factory.host.published)
    with pytest.raises(FactoryError) as conflict:
        await factory.operation(
            "setup.update", project_id="other", enabled=False, expected_revision=1
        )
    assert conflict.value.code == "revision_conflict"
    assert (await factory.operation("setup.get", project_id="other")) == updated
    assert factory.host.published == before
    assert before == [("setups", ["other"], 1), ("setups", ["other"], 2)]


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"project_id": "missing"}, "project_not_found"),
        ({"reviewer_profile": "missing"}, "profile_not_found"),
        ({"worker_model": "missing"}, "model_not_found"),
        ({"base_branch": "--all"}, "invalid_base"),
    ],
)
async def test_setup_refuses_unavailable_execution_configuration_without_writes(
    factory: FactoryHarness, patch: dict[str, Any], code: str
) -> None:
    before = await factory.operation("setup.list")
    with pytest.raises(FactoryError) as refused:
        await factory.operation("setup.update", **{"project_id": "project", **patch})
    assert refused.value.code == code
    assert await factory.operation("setup.list") == before
    assert factory.host.published == []


async def test_setup_deletion_requires_all_project_cards_final(factory: FactoryHarness) -> None:
    card = await factory.card("proposal")
    with pytest.raises(FactoryError) as refused:
        await factory.operation("setup.delete", project_id="project")
    assert refused.value.code == "setup_in_use"
    assert factory.host.published == []
    await factory.store.change(card["id"], {"state": "discarded"}, actor="user", event="discarded")
    result = await factory.operation("setup.delete", project_id="project")
    assert result == {"deleted": True, "project_id": "project"}
    assert (await factory.operation("setup.list"))["setups"] == []
    assert factory.host.published == [("setups", ["project"], 0)]


@pytest.mark.parametrize(
    ("action", "state", "reason", "arguments", "code"),
    [
        ("update", "working", "", {"title": "Changed"}, "invalid_state"),
        ("approve_proposal", "queued", "", {}, "invalid_state"),
        ("accept", "waiting", "blocked", {}, "not_reviewed"),
        ("accept", "waiting", "approved", {"expected_revision": 1}, "revision_conflict"),
        ("answer", "waiting", "approved", {"text": "Answer"}, "not_blocked"),
        ("retry", "waiting", "approved", {}, "not_retryable"),
    ],
)
async def test_card_action_guards_refuse_before_effects(
    factory: FactoryHarness,
    action: str,
    state: str,
    reason: str,
    arguments: dict[str, Any],
    code: str,
) -> None:
    card = await factory.card(state, waiting_reason=reason)
    arguments = {
        **({"expected_revision": card["revision"]} if action == "accept" else {}),
        **arguments,
    }
    history = await factory.store.history(card["id"])
    with pytest.raises(FactoryError) as refused:
        await factory.operation(f"card.{action}", id=card["id"], **arguments)
    assert refused.value.code == code
    assert await factory.store.get_card(card["id"]) == card
    assert await factory.store.history(card["id"]) == history
    assert factory.host.published == [] and factory.execution.calls == []


async def test_scheduler_respects_order_enabled_setups_and_all_wip_states(
    factory: FactoryHarness,
) -> None:
    factory.config["wip_limit"] = 4
    await factory.setup("disabled", enabled=False)
    disabled = await factory.card(project_id="disabled")
    proposal = await factory.card("proposal")
    active = [await factory.card(state) for state in ("working", "reviewing", "integrating")]
    await factory.card("waiting", waiting_reason="approved")
    first, second, moved = [await factory.card() for _ in range(3)]
    await factory.store.move_card(moved["id"], first["position"])
    await factory.service.schedule()
    assert factory.execution.calls == [("start", moved["id"], "")]
    board = await factory.operation("board.get", project_id="project")
    assert board["wip"] == {"running": 4, "limit": 4}
    assert disabled["id"] not in [card["id"] for card in board["cards"]]
    assert (await factory.store.get_card(disabled["id"]))["state"] == "queued"
    assert (await factory.store.get_card(proposal["id"]))["state"] == "proposal"
    await factory.store.change(
        active[0]["id"],
        {"state": "waiting", "waiting_reason": "blocked"},
        actor="worker",
        event="blocked",
    )
    await factory.service.schedule()
    assert factory.execution.calls == [("start", moved["id"], ""), ("start", first["id"], "")]
    assert (await factory.store.get_card(second["id"]))["state"] == "queued"


async def test_scheduler_stops_at_max_waiting_until_user_backlog_shrinks(
    factory: FactoryHarness,
) -> None:
    factory.config["max_waiting"] = 1
    waiting = await factory.card("waiting", waiting_reason="approved")
    queued = await factory.card()
    await factory.service.schedule()
    assert factory.execution.calls == []
    await factory.store.change(waiting["id"], {"state": "done"}, actor="worker", event="integrated")
    await factory.service.schedule()
    assert factory.execution.calls == [("start", queued["id"], "")]


async def test_concurrent_schedules_admit_each_card_only_once(factory: FactoryHarness) -> None:
    factory.config["wip_limit"] = 1
    first = await factory.card()
    second = await factory.card()
    entered, released, contender_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    factory.execution.start_gate = (entered, released)

    async def contender() -> None:
        contender_entered.set()
        await factory.service.schedule()

    first_schedule = asyncio.create_task(factory.service.schedule())
    second_schedule: asyncio.Task[None] | None = None
    try:
        await entered.wait()
        second_schedule = asyncio.create_task(contender())
        await contender_entered.wait()
        assert factory.execution.calls == [("start", first["id"], "")]
        assert not second_schedule.done()
    finally:
        released.set()
        await asyncio.gather(
            first_schedule, *([second_schedule] if second_schedule is not None else [])
        )
    await factory.service.schedule()
    assert factory.execution.calls == [("start", first["id"], "")]
    assert (await factory.store.get_card(second["id"]))["state"] == "queued"


async def test_live_wip_config_change_releases_capacity_without_reload(
    factory: FactoryHarness,
) -> None:
    factory.config["wip_limit"] = 1
    cards = [await factory.card() for _ in range(3)]
    await factory.service.schedule()
    assert factory.execution.calls == [("start", cards[0]["id"], "")]
    previous = dict(factory.config)
    factory.config["wip_limit"] = 2
    for callback in factory.declarations.config_change:
        await callback(previous, dict(factory.config))
    assert factory.execution.calls == [("start", card["id"], "") for card in cards[:2]]
    previous = dict(factory.config)
    factory.config["wip_limit"] = 1
    for callback in factory.declarations.config_change:
        await callback(previous, dict(factory.config))
    assert (await factory.store.get_card(cards[2]["id"]))["state"] == "queued"
    assert (await factory.operation("board.get"))["wip"] == {"running": 2, "limit": 1}


@pytest.mark.parametrize(
    ("action", "reason", "arguments", "execution_action", "state", "comment"),
    [
        ("accept", "approved", {}, "accept", "integrating", ""),
        (
            "send_back",
            "approved",
            {"comment": "Please simplify"},
            "send_back",
            "working",
            "Please simplify",
        ),
        ("answer", "blocked", {"text": "Use option A"}, "send_back", "working", "Use option A"),
        ("retry", "run_failed", {}, "retry", "working", ""),
    ],
)
async def test_user_resume_at_full_wip_is_queued_and_admitted_once(
    factory: FactoryHarness,
    action: str,
    reason: str,
    arguments: dict[str, Any],
    execution_action: str,
    state: str,
    comment: str,
) -> None:
    factory.config["wip_limit"] = 1
    running = await factory.card("working")
    waiting = await factory.card("waiting", waiting_reason=reason)
    if action == "accept":
        arguments = {**arguments, "expected_revision": waiting["revision"]}
    result = await factory.operation(f"card.{action}", id=waiting["id"], **arguments)
    assert result["card"]["state"] == "queued"
    assert result["card"]["queued_action"] == execution_action
    await factory.settle()
    assert factory.execution.calls == []
    saved = await factory.store.get_card(waiting["id"])
    assert saved["runtime"]["pending_action"] == {
        "action": execution_action,
        "comment": comment,
        "comment_recorded": bool(comment),
        "waiting_reason": reason,
    }
    history_before = await factory.store.history(waiting["id"])
    published_before = list(factory.host.published)
    with pytest.raises(FactoryError) as edit:
        await factory.operation(
            "card.update",
            id=waiting["id"],
            title="Unreviewed objective",
            description="Changed scope",
        )
    assert edit.value.code == "phase_queued"
    assert await factory.store.get_card(waiting["id"]) == saved
    assert await factory.store.history(waiting["id"]) == history_before
    assert factory.host.published == published_before
    with pytest.raises(FactoryError) as duplicate:
        await factory.operation(f"card.{action}", id=waiting["id"], **arguments)
    assert duplicate.value.code == "invalid_state"
    await factory.store.change(running["id"], {"state": "done"}, actor="worker", event="integrated")
    await factory.service.schedule()
    await factory.service.schedule()
    assert factory.execution.calls == [(execution_action, waiting["id"], comment)]
    resumed = await factory.store.get_card(waiting["id"])
    assert resumed["state"] == state and "pending_action" not in resumed["runtime"]
    detail = await factory.call({"action": "get", "id": waiting["id"]})
    assert "queued_action" not in detail["data"]["card"]
    history = await factory.store.history(waiting["id"])
    assert [event["event"] for event in history["events"]].count("queued_for_slot") == 1
    assert [item["text"] for item in history["comments"]] == ([comment] if comment else [])
