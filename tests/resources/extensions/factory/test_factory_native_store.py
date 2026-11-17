"""Native persisted ownership, agreement, hierarchy and delivery contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.database import open_offline_database
from core.extensions.databases import extension_database_spec
from resources.extensions.factory._native_schema import SCHEMA_SQL as NATIVE_SCHEMA_SQL
from resources.extensions.factory._workflows import normalize, templates
from resources.extensions.factory.store import (
    DATABASE_NAME,
    MIGRATIONS,
    SCHEMA_SQL,
    FactoryError,
    FactoryStore,
)

type Json = dict[str, Any]
pytestmark = pytest.mark.asyncio


async def test_native_schema_addition_preserves_workflow_cards_and_reopens(tmp_path: Path) -> None:
    before = extension_database_spec(
        tmp_path,
        "factory",
        DATABASE_NAME,
        SCHEMA_SQL.removesuffix(NATIVE_SCHEMA_SQL),
        migrations=tuple(
            item
            for item in MIGRATIONS
            if item.name not in {"native_factory_teams", "factory_budget_changes"}
        ),
    )
    database = open_offline_database(before)
    try:
        old = FactoryStore(database)
        card = await old.create_card("project", "Workflow work", "Scope", origin="operator")
        card = await old.change(
            card["id"], {"runtime": {"future": {"keep": True}}}, actor="user", event="updated"
        )
    finally:
        database.close()
    after = extension_database_spec(
        tmp_path, "factory", DATABASE_NAME, SCHEMA_SQL, migrations=MIGRATIONS
    )
    database = open_offline_database(after)
    try:
        store = FactoryStore(database)
        assert await store.get_card(card["id"]) == card
        factory, _, _ = await team(store)
        reservation = await store.native.reserve_identity("create", {"role": "Reviewer"})
    finally:
        database.close()
    database = open_offline_database(after)
    try:
        store = FactoryStore(database)
        assert await store.native.get_factory(factory["id"]) == factory
        assert await store.native.reserve_identity("create", {"role": "Reviewer"}) == reservation
        assert await store.get_card(card["id"]) == card
        workflow = await store.save_workflow(normalize(templates()[0]), expected_revision=0)
        task = await store.native.create_task(
            factory["id"], "Selected workflow", "Deliver", workflow_id=workflow["id"]
        )
        for state in ("backlog", "cancelled"):
            task = await store.native.change_task(task["id"], {"state": state})
            with pytest.raises(FactoryError) as selected:
                await store.delete_workflow(workflow["id"])
            assert selected.value.code == "workflow_in_use"
            assert (await store.get_workflow(workflow["id"]))["id"] == workflow["id"]
        await store.native.change_task(task["id"], {"workflow_id": ""})
        await store.delete_workflow(workflow["id"])
    finally:
        database.close()


async def team(store: FactoryStore, project: str = "project") -> tuple[Json, Json, Json]:
    native = store.native
    factory = await native.create_factory(
        project, leader_agent_id="leader", leader_agent_proof="inc-1"
    )
    leader = await native.get_member(factory["leader_member_id"])
    worker = await native.add_member(
        factory["id"], "worker", agent_proof="inc-2", role="Implementer"
    )
    return factory, leader, worker


async def test_native_project_membership_and_revision_contract(factory_store: FactoryStore) -> None:
    native = factory_store.native
    factory, leader, worker = await team(factory_store)
    configured = await native.update_factory(
        factory["id"],
        {"config": {"allowed_models": ["model"], "permission_ceiling": {"paths": []}, "future": 7}},
        expected_revision=factory["revision"],
    )
    cleared = await native.update_factory(
        factory["id"],
        {},
        clear_config_fields=["allowed_models", "permission_ceiling"],
        expected_revision=configured["revision"],
    )
    assert cleared["config"] == {"future": 7}
    with pytest.raises(FactoryError):
        await native.update_factory(factory["id"], {}, clear_config_fields=["future"])
    with pytest.raises(FactoryError):
        await native.update_factory(
            factory["id"],
            {},
            clear_config_fields=["model"],
            expected_revision=configured["revision"],
        )
    assert await native.get_factory(factory["id"]) == cleared
    capped = await native.update_factory(
        factory["id"],
        {"limits": {"runs": 3, "tokens": 50, "future_cap": 7}, "consumption": {"runs": 2}},
    )
    capped = await native.update_factory(
        factory["id"], {"limits": {"runs": 0, "tokens": 50}}, expected_revision=capped["revision"]
    )
    assert capped["limits"] == {"tokens": 50, "future_cap": 7}
    unlimited = await native.update_factory(
        factory["id"], {"limits": {}}, expected_revision=capped["revision"]
    )
    assert unlimited["limits"] == {"future_cap": 7}
    assert unlimited["consumption"] == {"runs": 2}
    with pytest.raises(FactoryError) as duplicate:
        await native.create_factory("project", leader_agent_id="another")
    assert duplicate.value.code == "factory_exists"
    with pytest.raises(FactoryError):
        await native.add_member(factory["id"], "worker", agent_proof="different-incarnation")
    saved = await native.update_member(
        worker["id"], {"config": {"future": {"x": 1}}, "unknown": 7}, expected_revision=1
    )
    saved = await native.update_member(
        worker["id"], {"config": {"model": "model"}}, expected_revision=saved["revision"]
    )
    assert saved["config"] == {"future": {"x": 1}, "model": "model"}
    assert saved["unknown"] == 7
    with pytest.raises(FactoryError) as stale:
        await native.update_member(worker["id"], {"role": "Old edit"}, expected_revision=1)
    assert stale.value.code == "revision_conflict"
    with pytest.raises(FactoryError):
        await native.update_member(worker["id"], {"agent_proof": "replacement"})
    with pytest.raises(FactoryError):
        await native.update_member(leader["id"], {"state": "retired"})
    page = await native.list_members(factory["id"], limit=1)
    remaining = await native.list_members(factory["id"], after=page[-1]["id"], limit=1)
    assert {page[0]["id"], remaining[0]["id"]} == {leader["id"], worker["id"]}
    assert await native.get_member(worker["id"]) == saved


async def test_task_graph_completion_and_stale_execution_are_atomic(
    factory_store: FactoryStore,
) -> None:
    native = factory_store.native
    factory, leader, worker = await team(factory_store)
    other, _, outsider = await team(factory_store, "other")
    parent = await native.create_task(
        factory["id"], "Parent", "Integrate", assignee_id=leader["id"], conditions="Verified"
    )
    first = await native.create_task(
        factory["id"], "First", "Implement", parent_id=parent["id"], assignee_id=worker["id"]
    )
    second = await native.create_task(
        factory["id"],
        "Second",
        "Review",
        parent_id=parent["id"],
        assignee_id=leader["id"],
        dependencies=[first["id"]],
    )
    for patch in (
        {"parent_id": first["id"]},
        {"dependencies": [second["id"]]},
        {"dependencies": [parent["id"]]},
        {"assignee_id": outsider["id"]},
    ):
        target = parent if "parent_id" in patch else first
        before = await native.get_task(target["id"])
        events = await native.events("task", target["id"])
        with pytest.raises(FactoryError):
            await native.change_task(target["id"], patch)
        assert await native.get_task(target["id"]) == before
        assert await native.events("task", target["id"]) == events
    with pytest.raises(FactoryError) as unfinished:
        await native.submit_task(parent["id"], "Ready", "Evidence", actor="user")
    assert unfinished.value.code == "children_unfinished"
    with pytest.raises(FactoryError) as dependency:
        await native.submit_task(second["id"], "Ready", "Evidence", actor="user")
    assert dependency.value.code == "dependencies_unfinished"
    first = await native.change_task(
        first["id"], {"runtime": {"pending": {"id": "input"}}, "consumption": {"runs": 2}}
    )
    historical_card = await factory_store.create_card(
        factory["project_id"],
        "Workflow attempt",
        "Scope",
        origin="operator",
        runtime={"native_task_id": first["id"], "native_task_epoch": first["epoch"]},
    )
    first = await native.change_task(first["id"], {"workflow_card_id": historical_card["id"]})
    first = await native.change_task(first["id"], {"state": "paused"})
    assert first["workflow_card_id"] == historical_card["id"]
    first = await native.change_task(first["id"], {"state": "ready", "limits": {"runs": 5}})
    assert first["workflow_card_id"] == historical_card["id"]
    reassigned = await native.assign_task(
        first["id"], leader["id"], expected_revision=first["revision"]
    )
    assert reassigned["epoch"] == first["epoch"] + 1
    assert reassigned["consumption"] == {"runs": 2}
    assert reassigned["workflow_card_id"] == ""
    assert await factory_store.get_card(historical_card["id"]) == historical_card
    linked = await native.change_task(first["id"], {"workflow_card_id": historical_card["id"]})
    selected = await native.change_task(first["id"], {"workflow_id": "another-workflow"})
    assert selected["epoch"] == linked["epoch"] + 1
    assert selected["workflow_card_id"] == "" and selected["submission"] is None
    assert selected["consumption"] == {"runs": 2}
    assert await factory_store.get_card(historical_card["id"]) == historical_card
    capped = await native.change_task(
        first["id"], {"limits": {"runs": 3, "tokens": 50, "future_cap": 7}}
    )
    capped = await native.change_task(
        first["id"], {"limits": {"runs": 0, "tokens": 50}}, expected_revision=capped["revision"]
    )
    assert capped["limits"] == {"tokens": 50, "future_cap": 7}
    unlimited = await native.change_task(
        first["id"], {"limits": {}}, expected_revision=capped["revision"]
    )
    assert unlimited["limits"] == {"future_cap": 7}
    assert unlimited["consumption"] == {"runs": 2}
    with pytest.raises(FactoryError) as stale:
        await native.submit_task(
            first["id"],
            "Old",
            "Old evidence",
            actor="member:" + worker["id"],
            expected_epoch=first["epoch"],
        )
    assert stale.value.code == "stale_execution"
    await native.submit_task(first["id"], "Implemented", "Checks", actor="user")
    await native.submit_task(second["id"], "Reviewed", "Review", actor="user")
    parent = await native.submit_task(parent["id"], "Integrated", "Checks", actor="user")
    assert parent["state"] == "done"
    assert (await native.results(parent["id"]))[0]["outcome"] == "Integrated"
    with pytest.raises(FactoryError) as finished_parent:
        await native.create_task(factory["id"], "More", "New child", parent_id=parent["id"])
    assert finished_parent.value.code == "parent_finished"
    paused = await native.create_task(
        factory["id"], "Later", "Wait", assignee_id=leader["id"], dependencies=[first["id"]]
    )
    paused = await native.change_task(paused["id"], {"state": "paused", "reason": "operator"})
    await native.change_task(first["id"], {"state": "ready"})
    grandchild = await native.create_task(
        factory["id"], "More", "Extend reopened work", parent_id=first["id"]
    )
    assert grandchild["parent_id"] == first["id"]
    after = await native.get_task(paused["id"])
    assert after["state"] == "paused" and after["reason"] == "operator"
    assert after["epoch"] > paused["epoch"]
    assert (await native.get_task(second["id"]))["state"] == "done"
    retiree = await native.add_member(factory["id"], "retiring")
    cleanup = await native.create_task(
        factory["id"], "Cleanup", "Cancel", assignee_id=retiree["id"]
    )
    await native.update_member(retiree["id"], {"state": "retired"})
    assert (await native.change_task(cleanup["id"], {"state": "cancelled"}))["state"] == "cancelled"
    assert await native.list_tasks(other["id"]) == []


async def test_completion_review_tracks_current_agreement(factory_store: FactoryStore) -> None:
    native = factory_store.native
    factory, leader, worker = await team(factory_store)
    task = await native.create_task(
        factory["id"],
        "Work",
        "Deliver",
        assignee_id=worker["id"],
        reviewer_id=leader["id"],
        user_approval=True,
    )
    with pytest.raises(FactoryError) as gate:
        await native.change_task(
            task["id"], {"state": "done"}, result={"outcome": "Claim", "evidence": "Checks"}
        )
    assert gate.value.code == "review_required"
    submitted = await native.submit_task(
        task["id"], "Delivered", "Tests", actor="member:" + worker["id"], expected_epoch=1
    )
    assert submitted["state"] == "review"
    reviewed = await native.review_task(
        task["id"],
        True,
        actor="member:" + leader["id"],
        reviewer_id=leader["id"],
        expected_revision=submitted["revision"],
    )
    assert reviewed["state"] == "review"
    finished = await native.review_task(
        task["id"], True, actor="user", user=True, expected_revision=reviewed["revision"]
    )
    assert finished["state"] == "done"
    assert finished["result"] == {"outcome": "Delivered", "evidence": "Tests"}
    assert [item.get("kind", "submission") for item in await native.results(task["id"])] == [
        "submission",
        "review",
        "review",
    ]
    reopened = await native.change_task(task["id"], {"state": "ready", "conditions": "New checks"})
    assert reopened["epoch"] > finished["epoch"] and reopened["submission"] is None
    with pytest.raises(FactoryError):
        await native.review_task(task["id"], True, actor="user", user=True)


async def test_topics_posts_invitations_and_read_markers_are_durable(
    factory_store: FactoryStore,
) -> None:
    native = factory_store.native
    factory, leader, worker = await team(factory_store)
    other, _, outsider = await team(factory_store, "other")
    topic = await native.create_topic(
        factory["id"], "Discuss", participants=[leader["id"]], initial_text="Question"
    )
    assert (await native.list_posts(topic["id"]))[0]["text"] == "Question"
    with pytest.raises(FactoryError):
        await native.invite_topic(topic["id"], [worker["id"], outsider["id"]], text="Invitation")
    assert await native.get_topic(topic["id"]) == topic
    assert len(await native.list_posts(topic["id"])) == 1
    topic = await native.invite_topic(topic["id"], [worker["id"]], text="Help please")
    assert topic["participants"] == sorted([leader["id"], worker["id"]])
    post = await native.add_post(
        topic["id"],
        "Answer",
        author="member:" + worker["id"],
        recipients=[leader["id"]],
        receipt="answer:1",
    )
    assert (
        await native.add_post(
            topic["id"],
            "Answer",
            author="member:" + worker["id"],
            recipients=[leader["id"]],
            receipt="answer:1",
        )
        == post
    )
    with pytest.raises(FactoryError):
        await native.add_post(topic["id"], "Different", author="user", receipt="answer:1")
    await native.mark_topic_read(topic["id"], leader["id"], post["sequence"])
    await native.mark_topic_read(topic["id"], leader["id"], 1)
    markers = await native.topic_participants(topic["id"])
    assert (
        next(item for item in markers if item["member_id"] == leader["id"])["read_sequence"]
        == post["sequence"]
    )
    assert await native.list_posts(topic["id"], after=2, limit=1) == [post]
    assert await native.recent_posts(topic["id"], limit=1) == [post]
    assert [item["text"] for item in await native.recent_posts(topic["id"], limit=2)] == [
        "Help please",
        "Answer",
    ]
    assert len(await native.list_posts(topic["id"])) == 3
    with pytest.raises(FactoryError) as invalid_page:
        await native.recent_posts(topic["id"], limit=0)
    assert invalid_page.value.code == "invalid_limit"
    await native.close_topic(topic["id"])
    assert (
        await native.add_post(
            topic["id"],
            "Answer",
            author="member:" + worker["id"],
            recipients=[leader["id"]],
            receipt="answer:1",
        )
        == post
    )
    with pytest.raises(FactoryError):
        await native.add_post(topic["id"], "New", author="user")
    assert await native.list_topics(other["id"]) == []


async def test_execution_delivery_and_identity_receipts_do_not_retarget(
    factory_store: FactoryStore,
) -> None:
    native = factory_store.native
    factory, _, worker = await team(factory_store)
    task = await native.create_task(factory["id"], "Work", "Deliver", assignee_id=worker["id"])
    kwargs: dict[str, Any] = {
        "group_id": "",
        "participant_id": "owner",
        "input_id": "input-1",
        "runtime": {"future": 7},
    }
    execution = await native.create_execution(
        factory["id"], "task", task["id"], worker["id"], task["epoch"], **kwargs
    )
    assert (
        await native.create_execution(
            factory["id"], "task", task["id"], worker["id"], task["epoch"], **kwargs
        )
        == execution
    )
    execution = await native.change_execution(
        execution["id"],
        {"group_id": "native." + execution["id"], "runtime": {"pending": {"id": "input-1"}}},
    )
    assert execution["runtime"]["future"] == 7
    execution = await native.change_execution(execution["id"], {"runtime": {"pending": None}})
    assert execution["runtime"]["pending"] is None
    delivery = await native.add_delivery(execution["id"], "note-1", payload={"text": "Task"})
    assert (
        await native.add_delivery(execution["id"], "note-1", payload={"text": "Task"}) == delivery
    )
    with pytest.raises(FactoryError):
        await native.add_delivery(execution["id"], "note-1", payload={"text": "Other"})
    acknowledged = await native.acknowledge_delivery(
        delivery["id"], generation_id="generation", sequence=3
    )
    assert acknowledged["state"] == "acknowledged"
    assert await native.list_deliveries(execution_id=execution["id"], state="pending") == []
    first, second = await asyncio.gather(
        native.reserve_identity("create-1", {"name": "Worker"}),
        native.reserve_identity("create-1", {"name": "Worker"}),
    )
    assert first == second
    await native.save_identity_reservation("create-1", {"proof": "incarnation"})
    completed = await native.complete_identity_reservation("create-1", worker["id"])
    assert completed["resource_id"] == worker["id"] and completed["proof"] == "incarnation"
    assert (await native.reserve_identity("create-1", {"name": "Worker"}))["agent_id"] == first[
        "agent_id"
    ]
    with pytest.raises(FactoryError):
        await native.reserve_identity("create-1", {"name": "Another"})


async def test_budget_feed_coalesces_updates_and_retains_delete_tombstones(
    factory_store: FactoryStore,
) -> None:
    factory, _, worker = await team(factory_store)
    task = await factory_store.native.create_task(
        factory["id"], "Scope", "Work", assignee_id=worker["id"]
    )
    first = await factory_store.budget_changes(limit=1)
    records = list(first["records"])
    while first["more"]:
        first = await factory_store.budget_changes(
            first["revision"], ledger_id=first["ledger_id"], limit=1
        )
        records.extend(first["records"])
    assert any(item["id"] == task["id"] for item in records)
    for priority in range(5):
        await factory_store.native.change_task(task["id"], {"priority": priority})
    changed = await factory_store.budget_changes(first["revision"], ledger_id=first["ledger_id"])
    assert [(item["kind"], item["id"]) for item in changed["records"]] == [("task", task["id"])]
    card = await factory_store.create_card("project", "Deleted", "Scope", origin="operator")
    await factory_store.change(card["id"], {"state": "discarded"}, actor="user", event="discarded")
    await factory_store.delete_card(card["id"])
    deleted = await factory_store.budget_changes(
        changed["revision"], ledger_id=changed["ledger_id"]
    )
    assert deleted["records"] == [{"kind": "card", "id": card["id"], "value": None}]
    assert (
        await factory_store.budget_changes(deleted["revision"], ledger_id=deleted["ledger_id"])
    )["records"] == []
