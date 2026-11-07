"""The Factory's persisted Card, audit history and setup revision contracts."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.database import DatabaseFormatError, open_offline_database
from core.extensions.databases import extension_database_spec
from resources.extensions.factory._values import SETUP_DEFAULTS
from resources.extensions.factory._workflows import templates
from resources.extensions.factory.store import (
    DATABASE_NAME,
    MIGRATIONS,
    SCHEMA_SQL,
    FactoryError,
    FactoryStore,
)

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("origin", ["operator", "user", "automation", "agent"])
async def test_card_origin_and_atomic_history(factory_store: FactoryStore, origin: str) -> None:
    store = factory_store
    card = await store.create_card("project", "Objective", "Acceptance criteria", origin=origin)
    assert card["state"] == ("proposal" if origin == "agent" else "queued")
    assert card["group_id"] == card["id"]
    assert (await store.history(card["id"]))["events"][0]["detail"] == {"origin": origin}
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


async def test_setup_revisions_and_unfinished_card_lifetime(factory_store: FactoryStore) -> None:
    store = factory_store
    setup = {**SETUP_DEFAULTS, "project_id": "project", "workflows": templates()}
    saved = await store.save_setup(setup, expected_revision=0)
    assert saved["revision"] == 1
    assert saved["workflows"] == templates()
    with pytest.raises(FactoryError):
        await store.save_setup(setup, expected_revision=0)
    assert await store.list_setups() == [saved]
    card = await store.create_card("project", "Objective", "Scope", origin="agent")
    with pytest.raises(FactoryError) as in_use:
        await store.delete_setup("project")
    assert in_use.value.code == "setup_in_use"
    with pytest.raises(FactoryError):
        await store.delete_card(card["id"])
    final = await store.change(card["id"], {"state": "discarded"}, actor="user", event="discarded")
    assert final["finished_at"] is not None
    await store.delete_setup("project")
    await store.delete_card(card["id"])
    assert await store.list_cards() == [] and await store.list_setups() == []


async def test_library_versions_survive_archival_and_reject_stale_changes(
    factory_store: FactoryStore,
) -> None:
    store = factory_store
    values = {
        "kind": "workflow",
        "name": "Reusable plan",
        "description": "Shared",
        "workflow": templates()[1],
    }
    first = await store.publish_template(values, expected_revision=0)
    changed = {**values, "id": first["id"], "name": "Reviewed plan"}
    second = await store.publish_template(changed, expected_revision=1)
    assert second["version"] == 2 and second["versions"] == [2, 1]
    original = await store.get_template(first["id"], 1)
    assert (original["name"], original["workflow"]) == (first["name"], first["workflow"])
    assert original["latest_version"] == 2
    with pytest.raises(FactoryError) as stale:
        await store.publish_template(changed, expected_revision=1)
    assert stale.value.code == "revision_conflict"
    assert (await store.get_template(first["id"]))["versions"] == [2, 1]
    with pytest.raises(FactoryError) as changed_kind:
        await store.publish_template({**changed, "kind": "step"}, expected_revision=2)
    assert changed_kind.value.code == "template_kind_changed"
    archived = await store.archive_template(first["id"], expected_revision=2)
    assert archived["archived"] and archived["version"] == 2 and archived["revision"] == 3
    assert (await store.list_templates())["templates"] == []
    listed = (await store.list_templates(include_archived=True))["templates"]
    assert len(listed) == 1 and listed[0]["id"] == first["id"]
    assert "workflow" not in listed[0]
    assert (await store.get_template(first["id"], 1))["workflow"] == first["workflow"]
    with pytest.raises(FactoryError) as hidden:
        await store.publish_template(changed, expected_revision=3)
    assert hidden.value.code == "template_archived"
    restored = await store.archive_template(first["id"], expected_revision=3, archived=False)
    assert not restored["archived"] and restored["revision"] == 4
    assert (
        await store.archive_template(first["id"], expected_revision=4, archived=False)
    ) == restored
    assert (await store.get_template(first["id"]))["versions"] == [2, 1]


async def test_library_listing_pages_without_duplicate_or_missing_templates(
    factory_store: FactoryStore,
) -> None:
    expected = []
    for index in range(3):
        template = await factory_store.publish_template(
            {"kind": "workflow", "name": str(index), "description": "", "workflow": templates()[0]},
            expected_revision=0,
        )
        expected.append(template["id"])
    first = await factory_store.list_templates(limit=2)
    assert len(first["templates"]) == 2 and first["next_cursor"]
    second = await factory_store.list_templates(limit=2, after=first["next_cursor"])
    assert len(second["templates"]) == 1 and not second["next_cursor"]
    assert [item["id"] for item in first["templates"] + second["templates"]] == sorted(expected)


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


_ROLE_SCHEMA = """
CREATE TABLE project_setups (
 project_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
 worker_profile TEXT NOT NULL, worker_model TEXT NOT NULL DEFAULT '',
 worker_effort TEXT NOT NULL DEFAULT '', reviewer_profile TEXT NOT NULL,
 reviewer_model TEXT NOT NULL DEFAULT '', reviewer_effort TEXT NOT NULL DEFAULT '',
 review_rounds INTEGER NOT NULL DEFAULT 2, turn_budget INTEGER NOT NULL DEFAULT 30,
 base_branch TEXT NOT NULL DEFAULT 'main', integration_check TEXT NOT NULL DEFAULT 'merged',
 revision INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL,
 worker_source TEXT NOT NULL DEFAULT 'team', reviewer_source TEXT NOT NULL DEFAULT 'team'
) STRICT;
CREATE TABLE reviews (
 card_id TEXT NOT NULL, round INTEGER NOT NULL,
 kind TEXT NOT NULL, participant_id TEXT NOT NULL, verdict TEXT NOT NULL,
 recommendation TEXT NOT NULL, findings_json TEXT NOT NULL, reviewed_head_sha TEXT NOT NULL,
 created_at TEXT NOT NULL, PRIMARY KEY(card_id, round, participant_id)
) STRICT;
"""


def _stage_workflow() -> dict[str, object]:
    """A Workflow as the stage-based Factory saved it."""
    lane = {"source": "agent", "agent": "coder", "model": "", "effort": ""}
    return {
        "id": "staged",
        "name": "Staged",
        "description": "",
        "stages": [
            {
                "id": "implement",
                "name": "Implement",
                "kind": "work",
                "output": "changes",
                "approval": False,
                "instructions": "",
                "lanes": [lane],
            },
            {
                "id": "review",
                "name": "Review",
                "kind": "review",
                "target": "implement",
                "rounds": 3,
                "approval": False,
                "instructions": "",
                "lanes": [{**lane, "agent": "critic"}],
            },
        ],
    }


async def test_role_and_stage_based_setups_and_reviews_migrate_into_steps(tmp_path: Path) -> None:
    old = extension_database_spec(tmp_path, "factory", DATABASE_NAME, _ROLE_SCHEMA + SCHEMA_SQL)
    database = open_offline_database(old)
    card = await FactoryStore(database).create_card("project", "Objective", "Scope", origin="user")

    def seed(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO project_setups (project_id, worker_profile, worker_model, "
            "reviewer_profile, review_rounds, turn_budget, revision, updated_at, "
            "reviewer_source) VALUES ('project', 'coder', 'test/worker', 'critic', 4, 12, 3, "
            "'2026-01-01T00:00:00Z', 'agent')"
        )
        connection.execute(
            "INSERT INTO reviews VALUES (?, 1, 'review', 'reviewer-1', 'approve', "
            "'Accept it.', '[]', 'abc', '2026-01-01T00:00:00Z')",
            (card["id"],),
        )
        connection.execute(
            "INSERT INTO setups (project_id, enabled, workflows_json, default_workflow, "
            "turn_budget, base_branch, integration_check, revision, updated_at) "
            "VALUES ('staged', 1, ?, 'staged', 30, 'main', 'merged', 1, "
            "'2026-01-01T00:00:00Z')",
            (json.dumps([_stage_workflow()]),),
        )

    database.write(seed)
    database.close()
    spec = extension_database_spec(
        tmp_path, "factory", DATABASE_NAME, SCHEMA_SQL, migrations=MIGRATIONS
    )
    database = open_offline_database(spec)
    try:
        store = FactoryStore(database)
        setup = await store.get_setup("project")
        workflow = setup["workflows"][0]
        assert (setup["default_workflow"], setup["turn_budget"], setup["revision"]) == (
            workflow["id"],
            12,
            3,
        )
        implement, review, integrate = workflow["steps"]
        # Integration after acceptance became a step that waits for approval.
        assert (integrate["after"], integrate["approval"], integrate["agent"]) == (
            [review["id"]],
            True,
            "coder",
        )
        assert (implement["source"], implement["agent"], implement["model"]) == (
            "team",
            "coder",
            "test/worker",
        )
        assert (review["source"], review["agent"]) == ("agent", "critic")
        # Four review passes allowed three send-backs.
        assert (review["returns_to"], review["rounds"]) == (implement["id"], 3)
        output = (await store.history(card["id"]))["outputs"][0]
        assert (output["participant_id"], output["kind"], output["result"]) == (
            "reviewer-1",
            "work",
            "Accept it.",
        )
        implement, review, integrate = (await store.get_setup("staged"))["workflows"][0]["steps"]
        assert (integrate["after"], integrate["approval"]) == (["review"], True)
        assert (implement["id"], implement["agent"], implement["returns_to"]) == (
            "implement",
            "coder",
            "",
        )
        assert (review["after"], review["returns_to"], review["rounds"]) == (
            ["implement"],
            "implement",
            2,
        )
    finally:
        database.close()
    # An older Workflow writer would erase the source links on its next save.
    older_writer = extension_database_spec(
        tmp_path, "factory", DATABASE_NAME, SCHEMA_SQL, migrations=MIGRATIONS[:-1]
    )
    with pytest.raises(DatabaseFormatError):
        open_offline_database(older_writer)
