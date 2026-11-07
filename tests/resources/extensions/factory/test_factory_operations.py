"""Factory public Tool, management and serialized WIP admission contracts."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

from core.runs import RunExecutionOwner, RunKind
from core.sessions import SessionAddress
from resources.extensions.factory._workflows import templates
from resources.extensions.factory.store import FactoryError, FactoryStore
from tests.resources.extensions.factory.factory_test_support import (
    FactoryHarness,
    build_factory,
    drafts_workflow,
    implement_workflow,
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
        ("waiting", "finished", "summary"),
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
    if content_field == "summary":
        patch[content_field] = content
    elif content_field is not None:
        patch["runtime"] = {**card["runtime"], content_field: content}
    card = await factory.store.change(
        card["id"],
        patch,
        actor="reviewer",
        event="reviewed",
    )
    await factory.service.changed(card, {**card, "state": "working"})
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
    # Columns an earlier Factory filled from Git stay off the Board.
    assert not {"branch", "head_sha", "recommendation"} & set(detail["data"]["card"])
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


@pytest.mark.parametrize(
    ("requested", "selected"), [(None, "implement"), ("drafts", "drafts"), ("Planning", "drafts")]
)
async def test_management_creation_is_operator_owned(
    factory: FactoryHarness, requested: str | None, selected: str
) -> None:
    default, other = implement_workflow(), drafts_workflow()
    default["name"] = other["id"]
    other["name"] = "Planning"
    await factory.setup(workflows=[default, other])
    result = await factory.operation(
        "card.create",
        project_id="project",
        title="Objective",
        description="Scope",
        **({"workflow": requested} if requested else {}),
    )
    card = result["card"]
    assert card["workflow_id"] == selected
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
    card = await factory.card("working")
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


async def test_setup_updates_preserve_fields_and_reject_stale_revisions(
    factory: FactoryHarness,
) -> None:
    empty = await factory.operation("setup.get", project_id="other")
    assert empty["setup"]["revision"] == 0
    assert empty["setup"]["workflows"] == templates()[:1]
    workflows = [implement_workflow(), drafts_workflow()]
    created = await factory.operation(
        "setup.update",
        project_id="other",
        workflows=workflows,
        default_workflow="drafts",
        expected_revision=0,
    )
    assert created["setup"]["revision"] == 1
    assert created["setup"]["workflows"] == workflows
    assert created["setup"]["default_workflow"] == "drafts"
    assert set(created["effective_models"]) == {
        "implement/implement",
        "implement/review",
        "implement/integrate",
        "drafts/draft1",
        "drafts/draft2",
        "drafts/draft3",
        "drafts/merge",
        "drafts/review",
    }
    updated = await factory.operation(
        "setup.update", project_id="other", turn_budget=12, expected_revision=1
    )
    assert updated["setup"]["revision"] == 2
    assert updated["setup"]["turn_budget"] == 12
    assert updated["setup"]["workflows"] == workflows
    before = list(factory.host.published)
    with pytest.raises(FactoryError) as conflict:
        await factory.operation(
            "setup.update", project_id="other", enabled=False, expected_revision=1
        )
    assert conflict.value.code == "revision_conflict"
    assert (await factory.operation("setup.get", project_id="other")) == updated
    assert factory.host.published == before
    assert before == [("setups", ["other"], 1), ("setups", ["other"], 2)]


def _changed(step: int, **fields: Any) -> list[Json]:
    workflow = implement_workflow()
    workflow["steps"][step].update(fields)
    return [workflow]


def _review_first() -> list[Json]:
    """Review listed before the step it reviews."""
    workflow = implement_workflow()
    workflow["steps"].reverse()
    return [workflow]


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"project_id": "missing"}, "project_not_found"),
        ({"workflows": _changed(1, agent="missing")}, "profile_not_found"),
        ({"workflows": _changed(1, source="team", agent="missing")}, "profile_not_found"),
        ({"workflows": _changed(0, agent="")}, "profile_not_found"),
        ({"workflows": _changed(0, model="missing")}, "model_not_found"),
        ({"workflows": _changed(1, after=[])}, "invalid_workflow"),
        ({"workflows": _review_first()}, "invalid_workflow"),
    ],
    ids=[
        "project",
        "agent",
        "team",
        "unchosen",
        "model",
        "returns-to-unrelated",
        "review-first",
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


async def test_setup_reports_a_step_whose_agent_has_no_usable_model(
    factory: FactoryHarness,
) -> None:
    async def inspect(config, project_id):
        if config.model_override is None and config.identity_profile == "worker":
            raise ValueError("model '' is not usable in this instance")
        return {"model": config.model_override or "test/reviewer"}

    factory.host.inspect_prompt = inspect
    choices = {"project_id": "project", "workflows": [implement_workflow()]}
    preview = await factory.operation("setup.preview", **choices)
    assert preview["effective_models"] == {
        "implement/implement": "",
        "implement/review": "test/reviewer",
        "implement/integrate": "",
    }
    with pytest.raises(FactoryError) as refused:
        await factory.operation("setup.update", **choices)
    assert refused.value.code == "model_unresolved"
    assert "Implement / Implement" in str(refused.value)
    fixed = implement_workflow()
    for index in (0, 2):
        fixed["steps"][index]["model"] = "test/worker"
    saved = await factory.operation("setup.update", project_id="project", workflows=[fixed])
    assert saved["effective_models"] == {
        "implement/implement": "test/worker",
        "implement/review": "test/reviewer",
        "implement/integrate": "test/worker",
    }


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
        ("accept", "waiting", "blocked", {}, "not_acceptable"),
        ("accept", "waiting", "finished", {"expected_revision": 1}, "revision_conflict"),
        ("answer", "waiting", "finished", {"text": "Answer"}, "not_blocked"),
        ("retry", "waiting", "checkpoint", {}, "not_retryable"),
        ("send_back", "waiting", "finished", {"comment": "No", "step": "deploy"}, "invalid_step"),
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
    active = [await factory.card("working") for _ in range(3)]
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
        ("accept", "finished", {}, "accept", "working", ""),
        (
            "send_back",
            "finished",
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
        "step": "",
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


async def test_shared_template_adoption_is_explicit_versioned_and_preserves_local_edits(
    factory: FactoryHarness,
) -> None:
    original = implement_workflow()
    template = (
        await factory.operation(
            "library.publish", name="Shared engineering", kind="workflow", workflow=original
        )
    )["template"]
    before = await factory.store.get_setup("project")
    draft = (await factory.operation("library.insert", id=template["id"]))["workflow"]
    source_id = draft["sources"][0]["id"]
    draft["steps"][0].update(task="Project-specific implementation", agent="reviewer")
    saved = (
        await factory.operation(
            "setup.update",
            project_id="project",
            expected_revision=before["revision"],
            workflows=[draft],
            default_workflow=draft["id"],
        )
    )["setup"]
    assert saved["workflows"][0]["sources"][0]["version"] == 1
    upstream = deepcopy(original)
    upstream["steps"][0]["task"] = "Template implementation changed"
    upstream["steps"][1]["task"] = "Template review changed"
    second = (
        await factory.operation(
            "library.publish",
            id=template["id"],
            expected_revision=template["revision"],
            name=template["name"],
            kind="workflow",
            workflow=upstream,
        )
    )["template"]
    preview = await factory.operation("library.preview_update", workflow=draft, source_id=source_id)
    assert preview["version"] == 2 and len(preview["conflicts"]) == 1
    assert preview["conflicts"][0]["field"] == "task"
    with pytest.raises(FactoryError) as unresolved:
        await factory.operation(
            "library.apply_update", workflow=draft, source_id=source_id, version=2
        )
    assert unresolved.value.code == "update_conflicts"
    # Another publication must not silently change the version the user previewed.
    third = deepcopy(upstream)
    third["steps"][1]["task"] = "Newer template review"
    await factory.operation(
        "library.publish",
        id=template["id"],
        expected_revision=second["revision"],
        name=template["name"],
        kind="workflow",
        workflow=third,
    )
    applied = await factory.operation(
        "library.apply_update",
        workflow=draft,
        source_id=source_id,
        version=preview["version"],
        resolutions={preview["conflicts"][0]["id"]: "local"},
    )
    assert applied["workflow"]["steps"][0]["task"] == "Project-specific implementation"
    assert applied["workflow"]["steps"][0]["agent"] == "reviewer"
    assert applied["workflow"]["steps"][1]["task"] == "Template review changed"
    assert applied["workflow"]["sources"][0]["version"] == 2
    assert await factory.store.get_setup("project") == saved
    with pytest.raises(FactoryError) as stale:
        await factory.operation(
            "setup.update",
            project_id="project",
            expected_revision=before["revision"],
            workflows=[applied["workflow"]],
        )
    assert stale.value.code == "revision_conflict"
    updated = (
        await factory.operation(
            "setup.update",
            project_id="project",
            expected_revision=saved["revision"],
            workflows=[applied["workflow"]],
        )
    )["setup"]
    assert updated["workflows"][0]["sources"][0]["version"] == 2
    assert (await factory.operation("library.get", id=template["id"], version=1))["template"][
        "workflow"
    ]["steps"] == original["steps"]
    assert any(resource == "library" for resource, _ids, _revision in factory.host.published)


async def test_selected_steps_publish_and_link_without_duplication_and_archive_keeps_history(
    factory: FactoryHarness,
) -> None:
    workflow = implement_workflow()
    extracted = (
        await factory.operation("library.extract", workflow=workflow, step_ids=["review"])
    )["workflow"]
    assert len(extracted["steps"]) == 1
    assert extracted["steps"][0]["after"] == []
    assert extracted["steps"][0]["returns_to"] == ""
    template = (
        await factory.operation(
            "library.publish", name="Shared review", kind="step", workflow=extracted
        )
    )["template"]
    linked = (
        await factory.operation(
            "library.link", id=template["id"], workflow=workflow, step_ids=["review"]
        )
    )["workflow"]
    assert linked["steps"] == workflow["steps"]
    assert linked["sources"][0]["template_id"] == template["id"]
    republish = (
        await factory.operation(
            "library.extract", workflow=linked, source_id=linked["sources"][0]["id"]
        )
    )["workflow"]
    assert republish["steps"] == extracted["steps"]
    whole_draft = (await factory.operation("library.extract", workflow=linked))["workflow"]
    whole = (
        await factory.operation(
            "library.publish", name="Project process", kind="workflow", workflow=whole_draft
        )
    )["template"]
    untouched = deepcopy(linked)
    with pytest.raises(FactoryError) as overlap:
        await factory.operation("library.link", id=whole["id"], workflow=linked)
    assert overlap.value.code == "source_overlap"
    replaced = (
        await factory.operation(
            "library.link", id=whole["id"], workflow=linked, replace_sources=True
        )
    )["workflow"]
    assert linked == untouched
    assert replaced["steps"] == linked["steps"]
    assert [source["template_id"] for source in replaced["sources"]] == [whole["id"]]
    untouched = deepcopy(replaced)
    with pytest.raises(FactoryError) as partial:
        await factory.operation(
            "library.link",
            id=template["id"],
            workflow=replaced,
            step_ids=["review"],
            replace_sources=True,
        )
    assert partial.value.code == "source_overlap"
    assert replaced == untouched
    # Draft operations neither change the Project nor rewrite the previous template.
    assert (await factory.store.get_setup("project"))["workflows"] == [workflow]
    assert (await factory.operation("library.get", id=template["id"]))["template"] == template
    archived = (
        await factory.operation(
            "library.archive", id=template["id"], expected_revision=template["revision"]
        )
    )["template"]
    assert archived["archived"]
    assert [item["id"] for item in (await factory.operation("library.list"))["templates"]] == [
        whole["id"]
    ]
    assert (await factory.operation("library.get", id=template["id"]))["template"]["workflow"]
    with pytest.raises(FactoryError) as hidden:
        await factory.operation("library.insert", id=template["id"], workflow=workflow)
    assert hidden.value.code == "template_archived"
