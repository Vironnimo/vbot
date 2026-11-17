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
from resources.extensions.factory._workflows import normalize
from resources.extensions.factory.store import FactoryError, FactoryStore
from tests.resources.extensions.factory.factory_test_support import (
    FactoryHarness,
    build_factory,
    drafts_workflow,
    implement_workflow,
    template,
)

type Json = dict[str, Any]

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def factory(factory_store: FactoryStore, tmp_path: Path) -> AsyncIterator[FactoryHarness]:
    harness = await build_factory(tmp_path, factory_store)
    try:
        yield harness
    finally:
        await harness.service.close()


def started_runtime(**steps: Json) -> Json:
    """A started implement Card's runtime with the given step states."""
    workflow = normalize(implement_workflow())
    states = {step["id"]: {"status": "pending"} for step in workflow["steps"]}
    states.update(steps)
    return {"workflow": workflow, "steps": states}


async def test_card_limits_are_revision_checked_and_keep_consumption(factory: FactoryHarness):
    budget = {"runs": ["run-one"], "tool_calls": 12}
    runtime = started_runtime(
        implement={"status": "waiting", "reason": "budget_exhausted", "budget": budget}
    )
    runtime["budget_notices"] = {"peer": {"seen": [], "pending": {"id": "old"}}}
    card = await factory.card("waiting", waiting_reason="budget_exhausted", runtime=runtime)
    result = await factory.operation(
        "card.limits",
        id=card["id"],
        expected_revision=card["revision"],
        card_limits={"tool_calls": 20, "runs": 0},
        step_limits={"cost_usd": 0.5},
        step="Implement",
    )
    saved = result["card"]
    assert saved["runtime"]["card_limits"] == {"tool_calls": 20}
    assert saved["runtime"]["steps"]["implement"]["limits"] == {"cost_usd": 0.5}
    assert saved["runtime"]["steps"]["implement"]["budget"] == budget
    assert saved["runtime"]["budget_notices"] == {"peer": {"seen": []}}
    with pytest.raises(FactoryError) as conflict:
        await factory.operation(
            "card.limits", id=card["id"], expected_revision=card["revision"], card_limits={}
        )
    assert conflict.value.code == "revision_conflict"
    with pytest.raises(FactoryError) as stepless:
        await factory.operation(
            "card.limits", id=card["id"], expected_revision=saved["revision"], step_limits={}
        )
    assert stepless.value.code == "missing_field"
    cleared = await factory.operation(
        "card.limits", id=card["id"], expected_revision=saved["revision"], card_limits={}
    )
    assert cleared["card"]["runtime"]["card_limits"] == {}
    assert cleared["card"]["runtime"]["steps"]["implement"]["limits"] == {"cost_usd": 0.5}
    assert cleared["card"]["state"] == "waiting"


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


CONTENT = "\n\nTest-owned summary.\nFurther detail."


@pytest.mark.parametrize(
    ("state", "reason", "runtime", "summary", "body"),
    [
        ("waiting", "finished", None, CONTENT, "Test-owned summary."),
        (
            "waiting",
            "blocked",
            started_runtime(review={"status": "waiting", "question": CONTENT}),
            "",
            "Test-owned summary.",
        ),
        (
            "waiting",
            "checkpoint",
            started_runtime(integrate={"status": "approval"}),
            "",
            "Step Integrate waits for your approval to start.",
        ),
        (
            "waiting",
            "run_failed",
            {"blocker": {"reason": "run_failed", "note": CONTENT}},
            "",
            "Test-owned summary.",
        ),
        ("waiting", "unknown_reason", None, "", None),
        ("proposal", "", None, "", None),
    ],
    ids=["summary", "step-question", "approval", "blocker", "no-content", "proposal"],
)
async def test_attention_renders_a_plain_summary_without_rewriting_card_content(
    factory: FactoryHarness,
    state: str,
    reason: str,
    runtime: Json | None,
    summary: str,
    body: str | None,
) -> None:
    card = await factory.card(state, waiting_reason=reason, runtime=runtime)
    title = "Objective\nwith a second line"
    card = await factory.store.change(
        card["id"],
        {"title": title, **({"summary": summary} if summary else {})},
        actor="reviewer",
        event="reviewed",
    )
    await factory.service.changed(card, {**card, "state": "working"})
    notice = factory.host.notifications[0]
    assert title.replace("\n", " ") in notice["title"]
    if body is not None:
        assert notice["body"] == body
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
    waiting = await factory.card("waiting", waiting_reason="checkpoint")
    for state in ("queued", "working", "done", "discarded"):
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


async def test_public_reads_filter_cards_and_name_running_steps(
    factory: FactoryHarness,
) -> None:
    proposal = await factory.card("proposal")
    working = await factory.card(
        "working",
        runtime=started_runtime(
            implement={"status": "done"}, review={"status": "running"}, integrate={}
        ),
    )
    await factory.card("proposal", project_id="other")
    result = await factory.call({"action": "list", "project": "project"})
    assert result["ok"] is True
    fields = ("id", "project_id", "title", "state", "waiting_reason", "origin")
    assert result["data"]["cards"] == [
        {key: proposal[key] for key in fields},
        {**{key: working[key] for key in fields}, "running_steps": ["Review"]},
    ]
    filtered = await factory.call({"action": "list", "state": "proposal"})
    assert [card["project_id"] for card in filtered["data"]["cards"]] == ["project", "other"]
    detail = await factory.call({"action": "get", "id": working["id"]})
    assert detail["ok"] is True
    card = detail["data"]["card"]
    assert "runtime" not in card
    assert [step["name"] for step in card["workflow"]["steps"]] == [
        "Implement",
        "Review",
        "Integrate",
    ]
    assert card["steps"]["review"] == {"status": "running"}
    # Columns an earlier Factory filled from Git stay off the Board.
    assert not {"branch", "head_sha", "recommendation"} & set(card)
    await factory.store.change(
        working["id"],
        {"state": "queued", "runtime": {**working["runtime"], "queued_action": "retry"}},
        actor="factory",
        event="retried",
    )
    queued = await factory.call({"action": "list", "state": "queued"})
    assert "running_steps" not in queued["data"]["cards"][0]
    detail = await factory.call({"action": "get", "id": working["id"]})
    assert detail["data"]["card"]["steps"]["review"] == {"status": "pending"}
    assert detail["data"]["card"]["queued_action"] == "retry"
    invalid = await factory.call({"action": "list", "state": "unknown"})
    assert invalid["ok"] is False and invalid["error"]["code"] == "invalid_arguments"


@pytest.mark.parametrize(
    ("requested", "project", "selected", "project_id"),
    [
        (None, "", "implement", "project"),
        ("drafts", "", "drafts", ""),
        ("Planning", "other", "drafts", "other"),
        ("implement", "project", "implement", "project"),
    ],
    ids=["first-enabled", "by-id-without-project", "by-name-with-project", "fixed-project"],
)
async def test_management_creation_picks_the_workflow_and_its_project(
    factory: FactoryHarness, requested: str | None, project: str, selected: str, project_id: str
) -> None:
    await factory.workflow(drafts_workflow(name="Planning"))
    result = await factory.operation(
        "card.create",
        title="Objective",
        description="Scope",
        **({"workflow": requested} if requested else {}),
        **({"project_id": project} if project else {}),
    )
    card = result["card"]
    assert (card["workflow_id"], card["project_id"]) == (selected, project_id)
    assert (card["origin"], card["state"], card["origin_session"], card["origin_run"]) == (
        "operator",
        "queued",
        None,
        "",
    )
    assert factory.host.authority_calls == []
    await factory.settle()
    assert factory.execution.calls == [("start", card["id"], "")]


@pytest.mark.parametrize(
    ("workflow", "project", "code"),
    [
        ("Disabled", "", "workflow_not_found"),
        ("missing", "", "workflow_not_found"),
        ("implement", "other", "project_mismatch"),
        ("Planning", "missing", "project_not_found"),
    ],
)
async def test_creation_refuses_unavailable_workflows_and_projects(
    factory: FactoryHarness, workflow: str, project: str, code: str
) -> None:
    await factory.workflow(drafts_workflow(name="Planning"))
    await factory.workflow(template("single", name="Disabled", enabled=False))
    factory.host.authority = "user"
    arguments = {"title": "Objective", "description": "Scope", "workflow": workflow}
    result = await factory.call({"action": "create", "project": project, **arguments})
    assert result["ok"] is False
    assert result["error"]["code"] == code
    with pytest.raises(FactoryError) as refused:
        await factory.operation("card.create", project_id=project, **arguments)
    assert refused.value.code == code
    assert await factory.store.list_cards() == []
    assert factory.host.published == [] and factory.host.notifications == []


async def test_creation_without_an_enabled_workflow_names_the_workflows_page(
    factory: FactoryHarness,
) -> None:
    await factory.workflow(implement_workflow(enabled=False))
    with pytest.raises(FactoryError) as refused:
        await factory.operation("card.create", title="Objective", description="Scope")
    assert refused.value.code == "workflow_not_found"
    assert "Workflows page" in str(refused.value)


async def test_card_detail_reports_participants_of_every_step(
    factory: FactoryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = started_runtime(
        implement={"status": "done", "runs": {"implement": "ended"}},
        review={"status": "running", "runs": {"review-r1": "live"}},
    )
    card = await factory.card("working", runtime=runtime)
    listed: list[str] = []

    async def participants(group: str, **_paging: Any) -> list[Any]:
        listed.append(group)
        participant = {
            f"{card['id']}.implement": "implement",
            f"{card['id']}.review": "review-r1",
        }.get(group)
        if participant is None:
            return []
        return [
            SimpleNamespace(
                participant_id=participant,
                address=SessionAddress("project", participant, f"session-{participant}"),
                config={"name": participant, "model": "test/worker"},
            )
        ]

    async def owned_runs(_group: str, runs: list[str]) -> dict[str, Any]:
        found = {
            "live": SimpleNamespace(run=SimpleNamespace(status=SimpleNamespace(value="running"))),
            "ended": SimpleNamespace(run=None, record=SimpleNamespace(terminal_status="completed")),
        }
        return {run: found[run] for run in runs}

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
    assert listed == [card["group_id"], *(f"{card['id']}.{step}" for step in runtime["steps"])]
    assert [
        (item["step"], item["participant_id"], item["state"], item["run_active"])
        for item in detail["participants"]
    ] == [("implement", "implement", "completed", False), ("review", "review-r1", "running", True)]
    assert [(run["step"], run["status"]) for run in detail["runs"]] == [
        ("implement", "completed"),
        ("review", "running"),
    ]
    assert detail["usage"] == {"cost": {"total": pytest.approx(0.02)}}
    assert set(detail["budget"]) == set(runtime["steps"])
    assert detail["card"]["runtime"] == card["runtime"]


async def test_workflow_saves_are_revision_checked_and_published(factory: FactoryHarness) -> None:
    listed = await factory.operation("workflow.list")
    assert [item["id"] for item in listed["workflows"]] == ["implement"]
    # A copied template whose id is taken becomes a Workflow of its own.
    created = await factory.operation("workflow.save", workflow=implement_workflow(name="Second"))
    second = created["workflow"]
    assert second["id"] != "implement" and second["revision"] == 1
    assert set(created["effective_models"]) == {
        f"{second['id']}/{step['id']}" for step in second["steps"]
    }
    second["limits"] = {"cost_usd": 5}
    second["steps"][0]["limits"] = {"tool_calls": 12}
    updated = await factory.operation("workflow.save", workflow=second, expected_revision=1)
    assert updated["workflow"]["revision"] == 2
    assert updated["workflow"]["limits"] == {"cost_usd": 5}
    assert updated["workflow"]["steps"][0]["limits"] == {"tool_calls": 12}
    before = list(factory.host.published)
    with pytest.raises(FactoryError) as conflict:
        await factory.operation(
            "workflow.save", workflow={**second, "name": "Stale"}, expected_revision=1
        )
    assert conflict.value.code == "revision_conflict"
    fetched = await factory.operation("workflow.get", id=second["id"])
    assert fetched["workflow"]["name"] == "Second" and fetched["workflow"]["revision"] == 2
    assert (
        factory.host.published
        == before
        == [
            ("workflows", [second["id"]], 1),
            ("workflows", [second["id"]], 2),
        ]
    )
    assert [item["id"] for item in (await factory.operation("workflow.list"))["workflows"]] == [
        "implement",
        second["id"],
    ]


def _changed(step: int, **fields: Any) -> Json:
    workflow = implement_workflow()
    workflow["steps"][step].update(fields)
    return workflow


def _review_first() -> Json:
    """Review listed before the step it reviews."""
    workflow = implement_workflow()
    workflow["steps"].reverse()
    return workflow


def _child(target: str) -> Json:
    workflow = template("single", id="parent", name="Parent")
    workflow["steps"].append({"name": "Child", "kind": "workflow", "workflow": target})
    return workflow


@pytest.mark.parametrize(
    ("workflow", "code"),
    [
        (implement_workflow(project_id="missing"), "project_not_found"),
        (
            template(
                "single",
                triggers=[{"type": "schedule", "schedule": "0 9 * * *", "project": "missing"}],
            ),
            "project_not_found",
        ),
        (_changed(1, agent="missing"), "profile_not_found"),
        (_changed(1, source="team", agent="missing"), "profile_not_found"),
        (
            template("single", steps=[{**template("single")["steps"][0], "source": "team"}]),
            "profile_not_found",
        ),
        (_changed(0, agent=""), "profile_not_found"),
        (_changed(0, model="missing"), "model_not_found"),
        (_changed(0, effort="enormous"), "invalid_effort"),
        (_changed(1, after=[]), "invalid_workflow"),
        (_review_first(), "invalid_workflow"),
        (_child("missing"), "workflow_not_found"),
        (_child("parent"), "invalid_workflow"),
        (
            template("single", triggers=[{"type": "schedule", "schedule": "bad"}]),
            "invalid_schedule",
        ),
    ],
    ids=[
        "project",
        "trigger-project",
        "agent",
        "team",
        "team-without-project",
        "unchosen",
        "model",
        "effort",
        "returns-to-unrelated",
        "review-first",
        "child-missing",
        "child-self",
        "schedule",
    ],
)
async def test_workflow_save_refuses_unavailable_configuration_without_writes(
    factory: FactoryHarness, workflow: Json, code: str
) -> None:
    before = await factory.operation("workflow.list")
    with pytest.raises(FactoryError) as refused:
        await factory.operation("workflow.save", workflow=workflow)
    assert refused.value.code == code
    assert await factory.operation("workflow.list") == before
    assert factory.host.published == []


async def test_workflow_save_reports_a_step_whose_agent_has_no_usable_model(
    factory: FactoryHarness,
) -> None:
    async def inspect(config: Any, project_id: str | None) -> Json:
        if config.model_override is None and config.identity_profile == "worker":
            raise ValueError("model '' is not usable in this instance")
        return {"model": config.model_override or "test/reviewer"}

    factory.host.inspect_prompt = inspect
    workflow = implement_workflow(id="other", name="Other")
    preview = await factory.operation("workflow.preview", workflow=workflow)
    assert preview["effective_models"] == {
        "other/implement": "",
        "other/review": "test/reviewer",
        "other/integrate": "",
    }
    with pytest.raises(FactoryError) as refused:
        await factory.operation("workflow.save", workflow=workflow)
    assert refused.value.code == "model_unresolved"
    assert "Step 'Implement'" in str(refused.value)
    for index in (0, 2):
        workflow["steps"][index]["model"] = "test/worker"
    saved = await factory.operation("workflow.save", workflow=workflow)
    assert saved["effective_models"] == {
        "other/implement": "test/worker",
        "other/review": "test/reviewer",
        "other/integrate": "test/worker",
    }


async def test_workflow_deletion_refuses_while_cards_or_workflows_use_it(
    factory: FactoryHarness,
) -> None:
    await factory.workflow(_child("implement"))
    card = await factory.card("proposal")
    with pytest.raises(FactoryError) as started:
        await factory.operation("workflow.delete", id="implement")
    assert started.value.code == "workflow_in_use"
    await factory.operation("workflow.delete", id="parent")
    with pytest.raises(FactoryError) as queued:
        await factory.operation("workflow.delete", id="implement")
    assert queued.value.code == "workflow_in_use"
    await factory.store.change(card["id"], {"state": "discarded"}, actor="user", event="discarded")
    result = await factory.operation("workflow.delete", id="implement")
    assert result == {"deleted": True, "id": "implement"}
    assert (await factory.operation("workflow.list"))["workflows"] == []
    assert factory.host.published == [
        ("workflows", ["parent"], 0),
        ("workflows", ["implement"], 0),
    ]


@pytest.mark.parametrize(
    ("action", "state", "runtime", "arguments", "code"),
    [
        ("update", "working", None, {"title": "Changed"}, "invalid_state"),
        ("approve_proposal", "queued", None, {}, "invalid_state"),
        ("limits", "queued", None, {"expected_revision": 1, "card_limits": {}}, "invalid_state"),
        ("limits", "waiting", {}, {"expected_revision": 1, "card_limits": {}}, "wrong_state"),
        (
            "accept",
            "waiting",
            started_runtime(),
            {"expected_revision": "stale"},
            "revision_conflict",
        ),
        (
            "send_back",
            "waiting",
            started_runtime(),
            {"comment": "No", "step": "deploy"},
            "invalid_step",
        ),
        ("delete", "waiting", None, {}, "invalid_state"),
    ],
)
async def test_card_action_guards_refuse_before_effects(
    factory: FactoryHarness,
    action: str,
    state: str,
    runtime: Json | None,
    arguments: dict[str, Any],
    code: str,
) -> None:
    card = await factory.card(state, waiting_reason="finished", runtime=runtime)
    revisions = {1: card["revision"], "stale": card["revision"] - 1}
    if "expected_revision" in arguments:
        arguments = {**arguments, "expected_revision": revisions[arguments["expected_revision"]]}
    history = await factory.store.history(card["id"])
    with pytest.raises(FactoryError) as refused:
        await factory.operation(f"card.{action}", id=card["id"], **arguments)
    assert refused.value.code == code
    assert await factory.store.get_card(card["id"]) == card
    assert await factory.store.history(card["id"]) == history
    assert factory.host.published == [] and factory.execution.calls == []


@pytest.mark.parametrize(
    ("action", "arguments", "detail"),
    [
        ("accept", {}, ""),
        ("accept", {"step": "Integrate"}, "integrate"),
        (
            "send_back",
            {"comment": "Please simplify", "step": "implement"},
            "implement:Please simplify",
        ),
        ("answer", {"text": "Use option A"}, ":Use option A"),
        ("retry", {"step": "review"}, "review"),
    ],
)
async def test_user_actions_reach_execution_with_the_named_step_at_full_wip(
    factory: FactoryHarness, action: str, arguments: Json, detail: str
) -> None:
    factory.config["wip_limit"] = 1
    await factory.card("working")
    waiting = await factory.card("waiting", waiting_reason="blocked", runtime=started_runtime())
    if action == "accept":
        arguments = {**arguments, "expected_revision": waiting["revision"]}
    result = await factory.operation(f"card.{action}", id=waiting["id"], **arguments)
    assert result["card"]["id"] == waiting["id"] and "runtime" in result["card"]
    assert factory.execution.calls == [(action, waiting["id"], detail)]


async def test_scheduler_respects_order_and_counts_only_running_steps(
    factory: FactoryHarness,
) -> None:
    factory.config["wip_limit"] = 4
    proposal = await factory.card("proposal")
    active = [await factory.card("working") for _ in range(3)]
    await factory.card("working", runtime=started_runtime(implement={"status": "child"}))
    await factory.card("waiting", waiting_reason="checkpoint")
    first, second, moved = [await factory.card() for _ in range(3)]
    await factory.store.move_card(moved["id"], first["position"])
    await factory.service.schedule()
    assert factory.execution.calls == [("start", moved["id"], "")]
    board = await factory.operation("board.get", project_id="project")
    assert board["wip"] == {"running": 4, "limit": 4}
    assert (await factory.store.get_card(proposal["id"]))["state"] == "proposal"
    await factory.store.change(
        active[0]["id"],
        {
            "state": "waiting",
            "waiting_reason": "blocked",
            "runtime": {**active[0]["runtime"], "steps": {"do": {"status": "waiting"}}},
        },
        actor="worker",
        event="blocked",
    )
    await factory.service.schedule()
    assert factory.execution.calls == [("start", moved["id"], ""), ("start", first["id"], "")]
    assert (await factory.store.get_card(second["id"]))["state"] == "queued"
    await factory.operation("card.discard", id=moved["id"])
    await factory.service.schedule()
    await factory.settle()
    assert (await factory.store.get_card(second["id"]))["state"] == "working"
    assert (await factory.operation("board.get"))["wip"] == {"running": 4, "limit": 4}


async def test_scheduler_stops_at_max_waiting_until_user_backlog_shrinks(
    factory: FactoryHarness,
) -> None:
    factory.config["max_waiting"] = 1
    waiting = await factory.card("waiting", waiting_reason="checkpoint")
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


async def test_nested_workflows_stop_after_five_child_levels(
    factory: FactoryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(factory.service, "request_schedule", lambda: None)
    parent = await factory.card("done")
    child_step = {
        "id": "child",
        "name": "Child",
        "kind": "workflow",
        "workflow": "implement",
        "task": "test-owned-nesting",
        "wait": True,
    }
    for depth in range(1, 6):
        parent = await factory.service.create_child(parent, child_step, [])
        assert parent["runtime"]["depth"] == depth
    count = len(await factory.store.list_cards())
    with pytest.raises(FactoryError) as refused:
        await factory.service.create_child(parent, child_step, [])
    assert refused.value.code == "too_deep"
    assert len(await factory.store.list_cards()) == count
