"""Workflow triggers: host trigger sync, webhook secrets and the Cards a fire creates."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

from resources.extensions.factory._triggers import project_repositories, repository_key
from resources.extensions.factory.store import FactoryError, FactoryStore
from tests.resources.extensions.factory.factory_test_support import (
    FactoryHarness,
    FakeTriggers,
    build_factory,
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


def host_triggers(factory: FactoryHarness) -> FakeTriggers:
    assert factory.host.triggers is not None
    return factory.host.triggers


def push_review(**changes: Any) -> Json:
    workflow = template("pushreview", id="pushes", **changes)
    workflow["triggers"].append(
        {"id": "daily", "type": "schedule", "schedule": "0 9 * * *", "project": "other"}
    )
    return workflow


def push(repository: str = "octo/app", commits: int = 1) -> Json:
    return {
        "seq": 1,
        "received_at": "2026-11-14T09:00:00Z",
        "headers": {"x-github-event": "push", "x-hub-signature-256": "sha256=..."},
        "body": {
            "ref": "refs/heads/main",
            "after": "0123456789abcdef",
            "repository": {
                "full_name": repository,
                "clone_url": f"https://github.com/{repository}.git",
            },
            "pusher": {"name": "octocat", "email": "private@example.com"},
            "commits": [
                {"id": f"c{index}", "message": f"Change {index}", "url": "u", "author": {}}
                for index in range(commits)
            ],
        },
    }


def remote(factory: FactoryHarness, project_id: str, url: str) -> None:
    git = factory.directory / project_id / ".git"
    git.mkdir(parents=True)
    (git / "config").write_text(f'[remote "origin"]\n\turl = {url}\n', encoding="utf-8")


async def test_saved_workflows_keep_host_triggers_in_sync(factory: FactoryHarness) -> None:
    triggers = host_triggers(factory)
    saved = (await factory.operation("workflow.save", workflow=push_review()))["workflow"]
    assert {key: (item["schedule"], item["enabled"]) for key, item in triggers.items.items()} == {
        "pushes:push": ("webhook", True),
        "pushes:daily": ("0 9 * * *", True),
    }
    fetched = (await factory.operation("workflow.get", id="pushes"))["workflow"]
    status = {item["id"]: item["status"] for item in fetched["triggers"]}
    assert status["push"]["webhook"]["url"].startswith("http://vbot/hooks/")
    assert status["daily"]["webhook"] is None and status["daily"]["status"] == "active"
    webhook_id = triggers.items["pushes:push"]["id"]
    secret = await factory.operation("workflow.secret", id="pushes", trigger="push")
    assert secret == {"secret": f"secret-{webhook_id}"}
    rotated = await factory.operation("workflow.secret", id="pushes", trigger="push", rotate=True)
    assert rotated == {"secret": f"secret-{webhook_id}-rotated"}
    with pytest.raises(FactoryError) as scheduled:
        await factory.operation("workflow.secret", id="pushes", trigger="daily")
    assert scheduled.value.code == "trigger_not_found"

    saved["enabled"] = False
    saved["triggers"] = [item for item in saved["triggers"] if item["id"] == "push"]
    await factory.operation("workflow.save", workflow=saved, expected_revision=1)
    assert list(triggers.items) == ["pushes:push"]
    # A webhook keeps its URL and secret while its Workflow is paused.
    assert triggers.items["pushes:push"]["id"] == webhook_id
    assert triggers.items["pushes:push"]["enabled"] is False
    await factory.operation("workflow.delete", id="pushes")
    assert triggers.items == {}


async def test_triggers_need_the_hosts_scheduler(factory: FactoryHarness) -> None:
    factory.host.triggers = None
    with pytest.raises(FactoryError) as refused:
        await factory.operation("workflow.save", workflow=template("pushreview"))
    assert refused.value.code == "triggers_unavailable"
    catalog = await factory.operation("catalog")
    assert catalog["triggers_available"] is False


async def test_webhook_fire_creates_a_card_per_request_in_the_repositorys_project(
    factory: FactoryHarness,
) -> None:
    remote(factory, "other", "git@github.com:Octo/App.git")
    await factory.operation("workflow.save", workflow=push_review())
    requests = [push(commits=25), push("someone/else"), push()]
    await factory.service.triggers.fire(
        {"key": "pushes:push", "fired_at": "2026-11-14T09:00:01Z", "requests": requests}
        | {"dropped": 2}
    )
    await factory.settle()
    cards = await factory.store.list_cards()
    assert [(card["title"], card["project_id"], card["origin"]) for card in cards] == [
        ("Review push: octo/app main 0123456", "other", "trigger"),
        ("Review push: someone/else main 0123456", "", "trigger"),
        ("Review push: octo/app main 0123456", "other", "trigger"),
    ]
    assert cards[0]["description"] == "Review this push."
    event = await factory.store.trigger_event(cards[0]["id"])
    assert event is not None
    request = event.pop("request")
    assert event == {
        "workflow_id": "pushes",
        "trigger": "push",
        "type": "webhook",
        "fired_at": "2026-11-14T09:00:01Z",
        "received_at": "2026-11-14T09:00:00Z",
        "skipped_requests": 2,
    }
    assert (request["event"], request["ref"], request["pusher"]) == (
        "push",
        "refs/heads/main",
        {"name": "octocat"},
    )
    assert len(request["commits"]) == 20 and request["more_commits"] == 5
    assert "x-hub-signature-256" not in str(request)
    # An unknown repository leaves the Card without a Project until the user acts.
    unknown = cards[1]
    assert unknown["runtime"]["repository"] == "github.com/someone/else"
    workflow = await factory.store.get_workflow("pushes")
    project, problem = await factory.service.resolve_card_project(unknown, workflow)
    assert project == {} and "github.com/someone/else" in problem
    assert [call[0] for call in factory.execution.calls] == ["start", "start"]


async def test_schedule_fire_creates_one_card_and_paused_workflows_create_none(
    factory: FactoryHarness,
) -> None:
    saved = (await factory.operation("workflow.save", workflow=push_review()))["workflow"]
    fire = {
        "key": "pushes:daily",
        "fired_at": "2026-11-14T09:00:05Z",
        "due_at": "2026-11-14T09:00:00Z",
        "earlier_missed": 3,
        "requests": [],
    }
    await factory.service.triggers.fire(fire)
    cards = await factory.store.list_cards()
    assert [(card["title"], card["project_id"]) for card in cards] == [("Review pushes", "other")]
    event = await factory.store.trigger_event(cards[0]["id"])
    assert event == {
        "workflow_id": "pushes",
        "trigger": "daily",
        "type": "schedule",
        "fired_at": "2026-11-14T09:00:05Z",
        "due_at": "2026-11-14T09:00:00Z",
        "missed": 3,
    }
    await factory.operation(
        "workflow.save", workflow={**saved, "enabled": False}, expected_revision=1
    )
    await factory.service.triggers.fire(fire)
    await factory.service.triggers.fire({**fire, "key": "deleted:daily"})
    assert len(await factory.store.list_cards()) == 1
    await factory.settle()


@pytest.mark.parametrize("choice", ["other", "@repository"])
async def test_trigger_project_resolution_never_falls_back_to_no_project(
    factory: FactoryHarness, choice: str
) -> None:
    definition = push_review()
    definition["triggers"][0]["project"] = choice
    saved = (await factory.operation("workflow.save", workflow=definition))["workflow"]
    factory.host.catalog_data["projects"] = [
        item for item in factory.host.catalog_data["projects"] if item["id"] != "other"
    ]
    await factory.service.triggers.fire(
        {"key": "pushes:push", "requests": [{"headers": {}, "body": {"task": "Run"}}]}
    )
    card = (await factory.store.list_cards())[0]
    project, problem = await factory.service.resolve_card_project(card, saved)
    assert project == {} and problem
    saved["project_id"] = "project"
    project, problem = await factory.service.resolve_card_project(card, saved)
    assert project["id"] == "project" and not problem
    await factory.settle()


async def test_disabled_trigger_cannot_fire_an_enabled_workflow(factory: FactoryHarness) -> None:
    definition = push_review()
    definition["triggers"][0]["enabled"] = False
    await factory.operation("workflow.save", workflow=definition)
    await factory.service.triggers.fire({"key": "pushes:push", "requests": [push()]})
    assert await factory.store.list_cards() == []


async def test_retried_trigger_batch_does_not_duplicate_committed_cards(
    factory: FactoryHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await factory.operation("workflow.save", workflow=push_review())
    fire = {
        "key": "pushes:push",
        "trigger_id": "cron-test",
        "requests": [push(), {**push(), "seq": 2}],
    }
    create = factory.service.create_triggered

    async def interrupted(*args: Any, **kwargs: Any) -> Json:
        await create(*args, **kwargs)
        raise OSError("Stopped after a committed Card")

    monkeypatch.setattr(factory.service, "create_triggered", interrupted)
    with pytest.raises(OSError):
        await factory.service.triggers.fire(fire)
    assert len(await factory.store.list_cards()) == 1
    monkeypatch.setattr(factory.service, "create_triggered", create)
    await factory.service.triggers.fire(fire)
    await factory.service.triggers.fire(fire)
    assert len(await factory.store.list_cards()) == 2
    await factory.settle()
    first = (await factory.store.list_cards())[0]
    await factory.store.change(first["id"], {"state": "discarded"}, actor="user", event="discarded")
    await factory.store.delete_card(first["id"])
    await factory.service.triggers.fire(fire)
    assert len(await factory.store.list_cards()) == 1
    await factory.settle()


async def test_repository_matching_follows_worktree_git_directories(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    common = tmp_path / "main" / ".git"
    private = common / "worktrees" / "checkout"
    private.mkdir(parents=True)
    (checkout / ".git").write_text("gitdir: ../main/.git/worktrees/checkout\n", encoding="utf-8")
    (private / "commondir").write_text("../..\n", encoding="utf-8")
    (common / "config").write_text(
        '[remote "origin"]\nurl = git@github.com:Octo/App.git\n', encoding="utf-8"
    )
    assert project_repositories(str(checkout)) == {"github.com/octo/app"}


@pytest.mark.parametrize("status", ["completed", "failed"])
async def test_trigger_reconciliation_keeps_terminal_and_failed_status(
    factory: FactoryHarness, status: str
) -> None:
    definition = template("single")
    definition["triggers"] = [{"id": "timer", "type": "schedule", "schedule": "in 1h"}]
    saved = (await factory.operation("workflow.save", workflow=definition))["workflow"]
    triggers = host_triggers(factory)
    current = triggers.items["single:timer"]
    current.update(status=status, enabled=False)
    trigger_id = current["id"]
    await factory.service.triggers.sync()
    assert current["status"] == status and not current["enabled"]
    if status == "completed":
        saved["triggers"][0]["schedule"] = "every 1h"
        await factory.operation(
            "workflow.save", workflow=saved, expected_revision=saved["revision"]
        )
        assert triggers.items["single:timer"]["id"] != trigger_id
        assert triggers.items["single:timer"]["status"] == "active"


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://github.com/Octo/App.git", "github.com/octo/app"),
        ("git@github.com:octo/app.git", "github.com/octo/app"),
        ("ssh://git@gitlab.example.com:2222/group/sub/app", "gitlab.example.com/group/sub/app"),
        ("https://token@github.com/octo/app/", "github.com/octo/app"),
        ("", ""),
    ],
)
async def test_repository_key_matches_remotes_regardless_of_form(url: str, key: str) -> None:
    assert repository_key(url) == key
