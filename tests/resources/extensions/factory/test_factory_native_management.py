"""Native management guards exercised through registered operations and private Tools."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.permissions import BoundaryContext, PermissionBoundary, normalize_permission_rules
from resources.extensions.factory.store import FactoryError, FactoryStore
from tests.resources.extensions.factory.factory_test_support import (
    build_factory,
    implement_workflow,
)

type Json = dict[str, Any]
pytestmark = pytest.mark.asyncio


async def test_native_operation_revision_review_and_task_bound_hand_in(
    factory_store: FactoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    harness = await build_factory(tmp_path, factory_store)
    monkeypatch.setattr(harness.service, "request_schedule", lambda: None)
    native = factory_store.native
    factory = await native.create_factory(
        "project", leader_agent_id="leader", leader_agent_proof="proof"
    )
    leader = await native.get_member(factory["leader_member_id"])
    worker = await native.add_member(factory["id"], "worker", agent_proof="worker-proof")
    reviewer = await native.add_member(factory["id"], "reviewer", agent_proof="reviewer-proof")
    task = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Implement",
            objective="Make the change",
            conditions="Checks pass",
            assignee_id=worker["id"],
            reviewer_id=reviewer["id"],
            user_approval=True,
        )
    )["task"]
    other_workflow = await harness.workflow(
        implement_workflow(id="other-project", name="Other Project", project_id="another-project")
    )
    disabled_workflow = await harness.workflow(
        implement_workflow(id="disabled", name="Disabled", enabled=False)
    )
    for workflow_id, code in (
        ("missing-workflow", "workflow_not_found"),
        (other_workflow["id"], "project_mismatch"),
        (disabled_workflow["id"], "workflow_not_found"),
    ):
        for action in ("create", "update"):
            arguments = (
                {"factory_id": factory["id"], "title": "Not saved", "objective": "No effects"}
                if action == "create"
                else {"id": task["id"], "expected_revision": task["revision"]}
            )
            with pytest.raises(FactoryError) as refused:
                await harness.operation(f"task.{action}", **arguments, workflow_id=workflow_id)
            assert refused.value.code == code
            assert await native.get_task(task["id"]) == task
            assert len(await native.list_tasks(factory["id"])) == 1
    global_workflow = await harness.workflow(
        implement_workflow(id="global", name="Global", project_id="")
    )
    selected = (
        await harness.operation(
            "task.create",
            factory_id=factory["id"],
            title="Optional workflow",
            objective="Native work",
            workflow_id=global_workflow["id"],
        )
    )["task"]
    assert selected["workflow_id"] == global_workflow["id"]
    with pytest.raises(FactoryError) as referenced:
        await harness.operation("workflow.delete", id=global_workflow["id"])
    assert referenced.value.code == "workflow_in_use"
    await harness.operation(
        "task.update", id=selected["id"], expected_revision=selected["revision"], workflow_id=""
    )
    await harness.operation("workflow.delete", id=global_workflow["id"])
    actor: Json = {
        "factory": factory,
        "member": worker,
        "task": task,
        "epoch": task["epoch"],
        "topic": {},
    }

    async def actor_context(context: Any) -> Json:
        return actor

    monkeypatch.setattr(harness.service.native, "actor", actor_context)
    context = harness.context(
        tool_name="factory_tasks",
        session_tool_grants=("factory_tasks",),
        request_turn_end_hook=lambda call_id: None,
    )

    async def call(**args: Any) -> Json:
        return await harness.tools.dispatch(context, args)

    denied = await call(action="update", id=task["id"], conditions="No checks needed")
    assert not denied["ok"]
    assert (await native.get_task(task["id"]))["conditions"] == "Checks pass"
    actor["member"] = leader
    actor["task"] = {}
    denied = await call(
        action="complete", id=task["id"], outcome="Finished", evidence="Checks passed"
    )
    assert not denied["ok"]
    assert (await native.get_task(task["id"]))["state"] == "ready"
    actor["member"] = worker
    actor["task"] = task
    submitted = await call(
        action="complete", id=task["id"], outcome="Finished", evidence="Checks passed"
    )
    assert submitted["ok"], submitted
    task = submitted["data"]["task"]
    assert task["state"] == "review"
    actor.update(member=reviewer, task=task)
    reviewed = await call(action="review", id=task["id"], approve=True)
    assert reviewed["ok"], reviewed
    task = reviewed["data"]["task"]
    assert task["state"] == "review"
    assert task["submission"]["reviewer_approved"]
    topic = (
        await harness.operation(
            "topic.create", factory_id=factory["id"], title="Question", member_ids=[reviewer["id"]]
        )
    )["topic"]
    topic_context = harness.context(
        tool_name="factory_topics",
        session_tool_grants=("factory_topics",),
        request_turn_end_hook=lambda call_id: None,
    )
    for _ in range(2):
        posted = await harness.tools.dispatch(
            topic_context, {"action": "post", "id": topic["id"], "text": "Reviewed evidence"}
        )
        assert posted["ok"], posted
    posts = (await harness.operation("topic.get", id=topic["id"]))["posts"]
    assert len(posts) == 1
    assert posts[0]["author"] == f"member:{reviewer['id']}"
    with pytest.raises(FactoryError, match="changed"):
        await harness.operation(
            "task.review", id=task["id"], expected_revision=task["revision"] - 1, approve=True
        )
    done = await harness.operation(
        "task.review", id=task["id"], expected_revision=task["revision"], approve=True
    )
    assert done["task"]["state"] == "done"
    await harness.service.close()


async def test_new_leader_creation_reuses_durable_request_after_partial_failure(
    factory_store: FactoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    harness = await build_factory(tmp_path, factory_store)
    monkeypatch.setattr(harness.service, "request_schedule", lambda: None)
    created: dict[str, Json] = {}

    async def create(agent_id: str, **config: Any) -> Json:
        if agent_id not in created:
            created[agent_id] = {
                "agent_id": agent_id,
                "incarnation": "proof",
                "name": config["name"],
                "model": config["model"],
                "created_by_owner": True,
            }
        return created[agent_id]

    async def get(agent_id: str) -> Json:
        return created[agent_id]

    cast(Any, harness.host).identity_agents = SimpleNamespace(create=create, get=get)
    original = factory_store.native.complete_identity_reservation
    fail = True

    async def finish(request_id: str, resource_id: str) -> Json:
        nonlocal fail
        if fail:
            fail = False
            raise FactoryError("interrupted", "Simulated response interruption")
        return await original(request_id, resource_id)

    ceiling = cast(
        Json,
        PermissionBoundary.build(
            normalize_permission_rules({"write": "scratch"}),
            BoundaryContext(workdir=tmp_path, home=tmp_path, data_dir=tmp_path / "data"),
        ).to_ceiling_dict(source="extension"),
    )
    invalid_ceiling = deepcopy(ceiling)
    invalid_ceiling["layers"][0]["source"] = "factory-test"
    with pytest.raises(FactoryError) as invalid_creation:
        await harness.operation(
            "factory.create",
            project_id="project",
            request_id="invalid-ceiling",
            leader_name="Leader",
            leader_model="model",
            config={"permission_ceiling": invalid_ceiling},
        )
    assert invalid_creation.value.code == "invalid_config"
    assert not created
    assert not await factory_store.native.list_factories()
    monkeypatch.setattr(factory_store.native, "complete_identity_reservation", finish)
    args = {
        "project_id": "project",
        "request_id": "new-leader",
        "leader_name": "Leader",
        "leader_model": "model",
    }
    with pytest.raises(FactoryError, match="interruption"):
        await harness.operation("factory.create", **args)
    retried = await harness.operation("factory.create", **args)
    assert len(created) == 1
    assert len(await factory_store.native.list_factories()) == 1
    assert retried["factory"]["leader_member_id"]
    leader = await factory_store.native.get_member(retried["factory"]["leader_member_id"])
    assert leader["created_by_factory"]
    assert leader["name"] == "Leader"
    listed = await harness.operation("factory.list")
    assert [record["id"] for record in listed["factories"]] == [retried["factory"]["id"]]
    assert harness.host.published[-1][0] == "factories"
    before = await factory_store.native.get_factory(retried["factory"]["id"])
    with pytest.raises(FactoryError) as invalid_update:
        await harness.operation(
            "factory.update",
            id=before["id"],
            expected_revision=before["revision"],
            config={"permission_ceiling": invalid_ceiling},
        )
    assert invalid_update.value.code == "invalid_config"
    assert await factory_store.native.get_factory(before["id"]) == before
    leader = await factory_store.native.update_member(
        leader["id"],
        {
            "config": {
                "allowed_tools": ["read"],
                "allowed_skills": ["review"],
                "future_setting": "retained",
            }
        },
        expected_revision=leader["revision"],
    )
    reset = await harness.operation(
        "member.update",
        id=leader["id"],
        expected_revision=leader["revision"],
        clear_fields=["allowed_tools", "allowed_skills"],
    )
    assert reset["member"]["config"] == {"future_setting": "retained"}
    inspected: list[tuple[str | None, str | None]] = []

    async def preview(config: Any, project_id: str | None) -> Json:
        inspected.append((config.model_override, config.thinking_effort_override))
        return {"model": config.model_override or "global-default"}

    harness.host.inspect_prompt = preview
    factory = retried["factory"]
    configured = (
        await harness.operation(
            "factory.update",
            id=factory["id"],
            expected_revision=factory["revision"],
            config={
                "model": "factory-model",
                "thinking_effort": "high",
                "allowed_models": ["factory-model"],
                "permission_ceiling": ceiling,
                "future_setting": "retained",
            },
        )
    )["factory"]
    assert inspected[-1] == ("factory-model", "high")
    assert configured["config"]["permission_ceiling"] == ceiling
    with pytest.raises(FactoryError) as outside:
        await harness.operation(
            "factory.update",
            id=factory["id"],
            expected_revision=configured["revision"],
            config={"allowed_models": ["other-model"]},
        )
    assert outside.value.code == "configuration_boundary"
    assert (await factory_store.native.get_factory(factory["id"]))["revision"] == configured[
        "revision"
    ]
    cleared = (
        await harness.operation(
            "factory.update",
            id=factory["id"],
            expected_revision=configured["revision"],
            clear_fields=["model", "thinking_effort", "allowed_models", "permission_ceiling"],
        )
    )["factory"]
    assert cleared["config"] == {"future_setting": "retained"}
    assert inspected[-1] == (None, None)

    await harness.service.close()


@pytest.mark.parametrize(
    "field,value",
    [
        ("state", "archived"),
        ("state", "paused"),
        ("limits", {}),
        ("config", {}),
        ("capacity", 2),
        ("remit", "new remit"),
        ("clear_fields", []),
        ("archive", None),
    ],
)
async def test_leader_cannot_change_operator_boundaries(
    factory_store: FactoryStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
) -> None:
    harness = await build_factory(tmp_path, factory_store)
    monkeypatch.setattr(harness.service, "request_schedule", lambda: None)
    native = factory_store.native
    factory = await native.create_factory("project", leader_agent_id="leader")
    leader = await native.get_member(factory["leader_member_id"])
    actor = {"factory": factory, "member": leader, "task": {}, "topic": {}}
    try:
        with pytest.raises(FactoryError) as refused:
            await harness.service.native_management.operation(
                "factory.archive" if field == "archive" else "factory.update",
                {"id": factory["id"], "expected_revision": factory["revision"]}
                | ({} if field == "archive" else {field: value}),
                actor,
            )
        assert refused.value.code == "operator_boundary"
        assert await native.get_factory(factory["id"]) == factory
    finally:
        await harness.service.close()


async def test_member_creation_validates_before_identity_and_reconciles_membership(
    factory_store: FactoryStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await build_factory(tmp_path, factory_store)
    monkeypatch.setattr(harness.service, "request_schedule", lambda: None)
    created: dict[str, Json] = {}

    async def create(agent_id: str, **config: Any) -> Json:
        assert agent_id not in created
        created[agent_id] = {
            "agent_id": agent_id,
            "incarnation": "proof",
            "name": config["name"],
            "model": config["model"],
        }
        return created[agent_id]

    async def get(agent_id: str) -> Json:
        return created[agent_id]

    cast(Any, harness.host).identity_agents = SimpleNamespace(create=create, get=get)
    factory = await factory_store.native.create_factory(
        "project", leader_agent_id="leader", config={"allowed_models": ["allowed"]}
    )
    args = {
        "factory_id": factory["id"],
        "request_id": "member-create",
        "name": "Specialist",
        "model": "outside",
    }
    try:
        with pytest.raises(FactoryError) as refused:
            await harness.operation("member.create", **args)
        assert refused.value.code == "configuration_boundary"
        assert created == {}
        assert await factory_store.native.identity_reservation(args["request_id"]) is None
        finish = factory_store.native.complete_identity_reservation
        interrupted = True

        async def complete(request_id: str, resource_id: str) -> Json:
            nonlocal interrupted
            if interrupted:
                interrupted = False
                raise FactoryError("interrupted", "test-owned-interruption")
            return await finish(request_id, resource_id)

        monkeypatch.setattr(factory_store.native, "complete_identity_reservation", complete)
        args["model"] = "allowed"
        with pytest.raises(FactoryError) as failure:
            await harness.operation("member.create", **args)
        assert failure.value.code == "interrupted"
        member = (await harness.operation("member.create", **args))["member"]
        assert len(created) == 1
        assert len(await factory_store.native.list_members(factory["id"])) == 2
        assert (await harness.operation("member.create", **args))["member"] == member
    finally:
        await harness.service.close()


@pytest.mark.parametrize(
    "operation,fields,code",
    [
        ("factory.update", {"name": "Changed"}, "leader_required"),
        ("member.add", {"agent_id": "other"}, "leader_required"),
        ("task.update", {"conditions": "Weaker"}, "agreement_boundary"),
        ("task.create", {"parent_id": "parent"}, "outside_remit"),
        ("task.complete", {"outcome": "Done", "evidence": "Evidence"}, "owner_required"),
        ("task.review", {"approve": True}, "reviewer_required"),
        ("task.get", {"outside": True}, "outside_factory"),
    ],
)
async def test_native_authority_refusals_leave_records_unchanged(
    factory_store: FactoryStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    fields: Json,
    code: str,
) -> None:
    harness = await build_factory(tmp_path, factory_store)
    monkeypatch.setattr(harness.service, "request_schedule", lambda: None)
    native = factory_store.native
    factory = await native.create_factory("project", leader_agent_id="leader")
    worker = await native.add_member(factory["id"], "worker")
    task = await native.create_task(
        factory["id"], "Owned", "Agreed", assignee_id=factory["leader_member_id"]
    )
    other = await native.create_factory("other", leader_agent_id="other-leader")
    outside = await native.create_task(other["id"], "Other", "Other scope")
    args = {
        "id": factory["id"] if operation.startswith("factory.") else task["id"],
        "factory_id": factory["id"],
        "expected_revision": 1,
        **fields,
    }
    if args.pop("outside", False):
        args["id"] = outside["id"]
    if args.get("parent_id") == "parent":
        args["parent_id"] = task["id"]
    actor = {"factory": factory, "member": worker, "task": {}, "topic": {}}
    try:
        with pytest.raises(FactoryError) as refused:
            await harness.service.native_management.operation(operation, args, actor)
        assert refused.value.code == code
        assert await native.get_factory(factory["id"]) == factory
        assert await native.get_task(task["id"]) == task
        assert len(await native.list_members(factory["id"])) == 2
    finally:
        await harness.service.close()
