"""The Factory's persisted Card, audit history and Workflow revision contracts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from core.database import DatabaseFormatError, open_offline_database
from core.extensions.databases import extension_database_spec
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


def workflow(template_id: str, **changes: Any) -> Json:
    found = next(item for item in templates() if item["id"] == template_id)
    return normalize({**found, **changes})


@pytest.mark.parametrize("origin", ["operator", "user", "automation", "agent", "trigger"])
async def test_card_origin_and_atomic_history(factory_store: FactoryStore, origin: str) -> None:
    store = factory_store
    event = {"workflow_id": "single", "trigger": "push", "request": {"ref": "main"}}
    card = await store.create_card(
        "project",
        "Objective",
        "Acceptance criteria",
        origin=origin,
        workflow_id="single",
        trigger_event=event if origin == "trigger" else None,
    )
    assert card["state"] == ("proposal" if origin == "agent" else "queued")
    assert card["group_id"] == card["id"] and card["workflow_id"] == "single"
    assert (await store.history(card["id"]))["events"][0]["detail"] == {"origin": origin}
    assert await store.trigger_event(card["id"]) == (event if origin == "trigger" else None)
    assert await store.list_cards(project_id="other") == []
    assert await store.list_cards(project_id="project", state=card["state"]) == [card]


async def test_card_transition_output_and_comment_commit_together(
    factory_store: FactoryStore,
) -> None:
    store = factory_store
    card = await store.create_card("project", "Objective", "Scope", origin="operator")
    saved = await store.change(
        card["id"],
        {"state": "waiting", "waiting_reason": "finished", "runtime": {"unknown": 7}},
        actor="agent",
        event="changes_requested",
        comment="A comment",
        output={
            "step": "review",
            "step_name": "Review",
            "kind": "changes",
            "pass": 1,
            "participant_id": "review-r1",
            "findings": ["A note"],
            "target": "implement",
        },
        expected_revision=1,
    )
    history = await store.history(card["id"])
    assert saved["revision"] == 2 and saved["runtime"] == {"unknown": 7}
    assert history["comments"][0]["text"] == "A comment"
    output = history["outputs"][0]
    assert (output["step"], output["kind"], output["target"]) == ("review", "changes", "implement")
    assert output["findings"] == ["A note"]
    assert history["events"][-1]["detail"] == {"previous_state": "queued", "state": "waiting"}
    with pytest.raises(FactoryError) as conflict:
        await store.change(
            card["id"], {"title": "Lost update"}, actor="user", event="updated", expected_revision=1
        )
    assert conflict.value.code == "revision_conflict"
    assert await store.get_card(card["id"]) == saved
    assert await store.history(card["id"]) == history


async def test_workflow_revisions_names_and_use(factory_store: FactoryStore) -> None:
    store = factory_store
    implement = await store.save_workflow(workflow("implement"), expected_revision=0)
    assert (implement["revision"], implement["steps"]) == (1, workflow("implement")["steps"])
    with pytest.raises(FactoryError) as conflict:
        await store.save_workflow(workflow("implement"), expected_revision=0)
    assert conflict.value.code == "revision_conflict"
    with pytest.raises(FactoryError) as taken:
        await store.save_workflow(workflow("plan", name="implement"), expected_revision=0)
    assert taken.value.code == "workflow_name_taken"
    parent = workflow("single", id="parent", name="Parent")
    parent["steps"].append(
        {"id": "child", "name": "Child", "kind": "workflow", "after": ["do"], "approval": False}
        | {"workflow": "implement", "wait": True, "task": ""}
    )
    await store.save_workflow(parent, expected_revision=0)
    renamed = await store.save_workflow(
        {**workflow("implement"), "name": "Build"}, expected_revision=1
    )
    assert renamed["revision"] == 2
    assert [item["id"] for item in await store.list_workflows()] == ["implement", "parent"]
    with pytest.raises(FactoryError) as started:
        await store.delete_workflow("implement")
    assert started.value.code == "workflow_in_use"
    await store.delete_workflow("parent")
    card = await store.create_card(
        "project", "Objective", "Scope", origin="agent", workflow_id="implement"
    )
    with pytest.raises(FactoryError) as queued:
        await store.delete_workflow("implement")
    assert queued.value.code == "workflow_in_use"
    with pytest.raises(FactoryError):
        await store.delete_card(card["id"])
    final = await store.change(card["id"], {"state": "discarded"}, actor="user", event="discarded")
    assert final["finished_at"] is not None
    await store.delete_workflow("implement")
    await store.delete_card(card["id"])
    assert await store.list_cards() == [] and await store.list_workflows() == []
    with pytest.raises(FactoryError) as missing:
        await store.get_workflow("implement")
    assert missing.value.code == "workflow_not_found"


async def test_backlog_moves_are_stable_and_audited(factory_store: FactoryStore) -> None:
    store = factory_store
    cards = [
        await store.create_card("project", str(index), "Scope", origin="operator")
        for index in range(3)
    ]
    await store.move_card(cards[0]["id"], cards[2]["position"])
    assert [card["id"] for card in await store.list_cards()] == [
        card["id"] for card in (cards[1], cards[2], cards[0])
    ]
    await store.move_card(cards[0]["id"], 1)
    assert [card["id"] for card in await store.list_cards()] == [card["id"] for card in cards]
    assert (await store.history(cards[0]["id"]))["events"][-1]["event"] == "moved"


_SETUPS_SCHEMA = """
CREATE TABLE setups (
 project_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
 workflows_json TEXT NOT NULL DEFAULT '[]', default_workflow TEXT NOT NULL DEFAULT '',
 turn_budget INTEGER NOT NULL DEFAULT 30, base_branch TEXT NOT NULL DEFAULT 'main',
 integration_check TEXT NOT NULL DEFAULT 'merged',
 revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
) STRICT;
"""


def _setup_workflows() -> list[Json]:
    """Per-Project Workflows as the step-based Factory saved them."""
    steps = [
        {key: value for key, value in step.items() if key != "kind"}
        | {"agent": "coder", "limits": {"runs": 30}}
        for step in workflow("implement")["steps"]
    ]
    implement = {"id": "implement", "name": "Implement", "description": "", "steps": steps}
    plan = {"id": "plan", "name": "Plan", "description": "", "steps": steps[:1]}
    return [plan, implement]


async def test_project_setups_become_global_workflows_and_cards_follow(tmp_path: Path) -> None:
    old = extension_database_spec(
        tmp_path, "factory", DATABASE_NAME, _SETUPS_SCHEMA + SCHEMA_SQL, migrations=MIGRATIONS[:-1]
    )
    database = open_offline_database(old)
    store = FactoryStore(database)
    queued = await store.create_card("project", "Queued", "Scope", origin="user")
    started = await store.create_card("project", "Started", "Scope", origin="user")
    finished = await store.create_card("other", "Finished", "Scope", origin="user")
    snapshot = _setup_workflows()[1]

    def seed(connection: sqlite3.Connection) -> None:
        for project_id in ("project", "other"):
            connection.execute(
                "INSERT INTO setups (project_id, enabled, workflows_json, default_workflow, "
                "revision, updated_at) VALUES (?, ?, ?, 'implement', 3, '2026-01-01T00:00:00Z')",
                (project_id, int(project_id == "project"), json.dumps(_setup_workflows())),
            )
        for card, state, runtime in (
            (queued, "queued", {"cwd": ""}),
            (
                started,
                "working",
                {
                    "workflow": snapshot,
                    "done": ["implement"],
                    "step": "review",
                    "budget": {"runs": ["one", "two"], "tool_calls": 4},
                },
            ),
            (finished, "done", {"workflow": snapshot, "done": ["implement", "review"]}),
        ):
            connection.execute(
                "UPDATE cards SET state = ?, workflow_id = 'implement', runtime_json = ? "
                "WHERE id = ?",
                (state, json.dumps(runtime), card["id"]),
            )

    database.write(seed)
    database.close()
    spec = extension_database_spec(
        tmp_path, "factory", DATABASE_NAME, SCHEMA_SQL, migrations=MIGRATIONS
    )
    database = open_offline_database(spec)
    try:
        store = FactoryStore(database)
        workflows = await store.list_workflows()
        # Each Project's default comes first; a later copy of an id or name gets a new one.
        assert [(item["name"], item["project_id"], item["enabled"]) for item in workflows] == [
            ("Implement", "other", False),
            ("Plan", "other", False),
            ("Implement (project)", "project", True),
            ("Plan (project)", "project", True),
        ]
        assert [item["id"] for item in workflows][:2] == ["implement", "plan"]
        renamed = workflows[2]
        assert renamed["id"] not in {"implement", "plan"}
        assert all(item["final_approval"] and item["triggers"] == [] for item in workflows)
        assert [step["kind"] for step in renamed["steps"]] == ["agent"] * 3
        assert renamed["steps"][0]["limits"] == {"runs": 30}
        cards = {card["title"]: card for card in await store.list_cards()}
        assert (cards["Queued"]["state"], cards["Queued"]["workflow_id"]) == (
            "queued",
            renamed["id"],
        )
        assert cards["Queued"]["runtime"] == {"version": 2, "cwd": ""}
        upgraded = cards["Started"]
        assert (upgraded["state"], upgraded["waiting_reason"]) == ("waiting", "upgraded")
        runtime = upgraded["runtime"]
        assert runtime["workflow"]["id"] == renamed["id"]
        assert {key: value["status"] for key, value in runtime["steps"].items()} == {
            "implement": "done",
            "review": "waiting",
            "integrate": "pending",
        }
        assert runtime["steps"]["review"]["reason"] == "upgraded"
        assert runtime["budget"] == {"runs": ["one", "two"], "tool_calls": 4}
        legacy = cards["Finished"]
        assert (legacy["state"], legacy["runtime"]["legacy"]) == ("done", True)
        assert legacy["runtime"]["workflow"]["steps"][0]["kind"] == "agent"
    finally:
        database.close()
    # Older writers would misread the per-step runtime.
    older_writer = extension_database_spec(
        tmp_path, "factory", DATABASE_NAME, SCHEMA_SQL, migrations=MIGRATIONS[:-1]
    )
    with pytest.raises(DatabaseFormatError):
        open_offline_database(older_writer)
