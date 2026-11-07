"""The Factory's persisted Card, audit history and setup revision contracts."""

from __future__ import annotations

import sqlite3

import pytest

from resources.extensions.factory._values import SETUP_DEFAULTS
from resources.extensions.factory.store import FactoryError, FactoryStore

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


async def test_card_transition_review_and_comment_commit_together(
    factory_store: FactoryStore,
) -> None:
    store = factory_store
    card = await store.create_card("project", "Objective", "Scope", origin="operator")
    saved = await store.change(
        card["id"],
        {"state": "waiting", "waiting_reason": "approved", "runtime": {"unknown": 7}},
        actor="reviewer",
        event="review",
        comment="A comment",
        review={"round": 1, "participant_id": "reviewer-1", "verdict": "approve", "findings": []},
        expected_revision=1,
    )
    history = await store.history(card["id"])
    assert saved["revision"] == 2 and saved["runtime"] == {"unknown": 7}
    assert history["comments"][0]["text"] == "A comment"
    assert history["reviews"][0]["verdict"] == "approve"
    assert history["events"][-1]["detail"] == {"previous_state": "queued", "state": "waiting"}
    with pytest.raises(FactoryError) as conflict:
        await store.change(
            card["id"], {"title": "Lost update"}, actor="user", event="updated", expected_revision=1
        )
    assert conflict.value.code == "revision_conflict"
    assert await store.get_card(card["id"]) == saved
    assert await store.history(card["id"]) == history
    with pytest.raises(sqlite3.IntegrityError):
        await store.change(
            card["id"],
            {"state": "done"},
            actor="reviewer",
            event="duplicate_review",
            review={"round": 1, "participant_id": "reviewer-1", "verdict": "approve"},
        )
    assert await store.get_card(card["id"]) == saved
    assert await store.history(card["id"]) == history


async def test_setup_revisions_and_unfinished_card_lifetime(factory_store: FactoryStore) -> None:
    store = factory_store
    setup = {
        **SETUP_DEFAULTS,
        "project_id": "project",
        "worker_profile": "coder",
        "reviewer_profile": "reviewer",
        "review_rounds": 2,
    }
    saved = await store.save_setup(setup, expected_revision=0)
    assert saved["revision"] == 1
    assert saved["review_rounds"] == 2
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
