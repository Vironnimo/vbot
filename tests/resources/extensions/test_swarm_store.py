"""Swarm store: profiles behavior."""

from __future__ import annotations

# mypy: disable-error-code=arg-type
import asyncio

import pytest

from core.sessions import SessionAddress, TemporarySessionBinding
from resources.extensions.swarm._participant_names import _PARTICIPANT_NAMES
from resources.extensions.swarm.store import SwarmStore, SwarmStoreError
from tests.resources.extensions.swarm_store_helpers import (
    _profile,
    _swarm,
)
from tests.resources.extensions.swarm_store_helpers import (
    store as store,
)


@pytest.mark.asyncio
async def test_prompt_selection_and_reminders_are_snapshotted(store):
    from resources.extensions.swarm.agent_text import DEFAULT_REMINDERS

    saved = await store.save_profile(
        {
            **_profile(),
            "instructions": "editable-sentinel",
            "prompt_blocks": ["core:tools", "core:working_project"],
            "reminders": dict.fromkeys(DEFAULT_REMINDERS, False),
        },
        expected_revision=None,
    )
    started = await store.create_swarm(
        saved["id"],
        "goal",
        {"cwd": "C:/work"},
        request_id="start",
        expected_profile_revision=saved["revision"],
    )
    await store.save_profile(
        {**saved, "prompt_blocks": [], "instructions": "changed"},
        expected_revision=saved["revision"],
    )
    snapshot = (await store.get_swarm(started["swarm_id"]))["profile_snapshot"]
    assert snapshot["prompt_blocks"] == ["core:tools", "core:working_project"]
    assert snapshot["instructions"] == "editable-sentinel"
    assert not any(snapshot["reminders"].values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"prompt_blocks": "core:runtime"},
        {"prompt_blocks": ["core:runtime", "core:runtime"]},
        {"prompt_blocks": [2]},
        {"prompt_blocks": ["core:agent_body"]},
        {"reminders": {"resume": False}},
        {"reminders": {"delivery": True, "wake": True, "resume": 1}},
    ],
)
async def test_prompt_controls_reject_invalid_values(store, fields):
    with pytest.raises(SwarmStoreError):
        await store.save_profile({**_profile(), **fields}, expected_revision=None)


@pytest.mark.asyncio
async def test_current_store_reopens_with_profile_board_and_execution_state(tmp_path):
    path = tmp_path / "swarm.db"
    original = SwarmStore(path)
    await original.open()
    try:
        profile = await original.save_profile(_profile(), expected_revision=None)
        started = await original.create_swarm(
            profile["id"],
            "Keep the current work",
            {"cwd": "C:/work"},
            request_id="create",
            expected_profile_revision=profile["revision"],
        )
        sid = started["swarm_id"]
        pid = (await original.get_swarm(sid))["participants"][0]["id"]
        await original.bind_participant_session(
            TemporarySessionBinding(
                SessionAddress(None, "temporary", "session"),
                "generation",
                "swarm",
                sid,
                pid,
                {},
            )
        )
        await original.set_participant_state(sid, pid, "failed")
        post = await original.post_human(sid, text="Still pending", request_id="post")
        snapshot = await original.get_swarm(sid)
        schema = original._database._connection.execute(
            "SELECT name,sql FROM sqlite_master ORDER BY name"
        ).fetchall()
    finally:
        await original.close()
    reopened = SwarmStore(path)
    await reopened.open()
    try:
        assert await reopened.get_profile(profile["id"]) == profile
        assert await reopened.get_swarm(sid) == snapshot
        assert (await reopened.read_human_posts(sid)).entries[0]["id"] == post["post_id"]
        assert (await reopened.prepare_inbox_delivery(sid, pid))["entries"][0]["id"] == post[
            "post_id"
        ]
        assert (
            reopened._database._connection.execute(
                "SELECT name,sql FROM sqlite_master ORDER BY name"
            ).fetchall()
            == schema
        )
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_profiles_validate_revision_slug_and_are_immutable(store: SwarmStore) -> None:
    saved = await store.save_profile(_profile(), expected_revision=None)
    assert saved["revision"] == 1
    saved["name"] = "Mutated"
    assert (await store.get_profile(saved["id"]))["name"] == "Research"
    changed = await store.get_profile(saved["id"])
    changed["name"] = "Changed"
    assert (await store.save_profile(changed, expected_revision=1))["revision"] == 2
    with pytest.raises(SwarmStoreError, match="revision_conflict"):
        await store.save_profile(changed, expected_revision=1)
    with pytest.raises(SwarmStoreError, match="slug_unavailable"):
        await store.save_profile(_profile(), expected_revision=None)


@pytest.mark.asyncio
async def test_automatic_profile_shortcuts_are_unique_and_stable(store: SwarmStore) -> None:
    profile = _profile()
    profile.pop("slug")
    first, second = await asyncio.gather(
        store.save_profile(profile, expected_revision=None),
        store.save_profile(profile, expected_revision=None),
    )
    assert {first["slug"], second["slug"]} == {"research", "research-2"}
    original_slug = first.pop("slug")
    first["name"] = "Renamed"
    updated = await store.save_profile(first, expected_revision=1)
    assert updated["slug"] == original_slug
    assert (await store.get_profile(updated["id"]))["slug"] == updated["slug"]
    profile["name"] = "研究"
    assert (await store.save_profile(profile, expected_revision=None))["slug"] == "swarm"


@pytest.mark.asyncio
async def test_profile_defaults_and_strict_nested_validation(store: SwarmStore) -> None:
    profile = _profile()
    profile.pop("delivery")
    saved = await store.save_profile(profile, expected_revision=None)
    assert saved["delivery"] == {
        "main": {"mode": "all", "wake_idle": True},
        "discussion": {"mode": "all", "wake_idle": True},
        "ping": {"mode": "all", "wake_idle": True},
        "coalesce_ms": 250,
        "batch_messages": 20,
        "batch_chars": 24_000,
    }
    invalid_profiles = [
        {**_profile(slug="bad-schema"), "schema_version": True},
        {**_profile(slug="bad-cwd"), "working_directory": {"kind": "other", "path": "x"}},
        {
            **_profile(slug="bad-formation"),
            "participants": [{"model": "model-a", "count": 1, "extra": True}],
        },
        {**_profile(slug="bad-delivery"), "delivery": {"main": {"mode": "later"}}},
        {
            **_profile(slug="bad-tools"),
            "tool_access": {"mode": "selected", "allowed": [], "extra": True},
        },
        {**_profile(slug="bad-skills"), "allowed_skills": ["ok", 1]},
        {
            **_profile(slug="bad-thinking"),
            "participants": [{"model": "model-a", "count": 1, "thinking_effort": "impossible"}],
        },
        {
            **_profile(slug="bad-temperature"),
            "participants": [{"model": "model-a", "count": 1, "temperature": True}],
        },
        {
            **_profile(slug="bad-fallback"),
            "participants": [{"model": "model-a", "count": 1, "fallback_models": ["a", "a"]}],
        },
    ]
    for invalid in invalid_profiles:
        with pytest.raises(SwarmStoreError, match="invalid_arguments"):
            await store.save_profile(invalid, expected_revision=None)


@pytest.mark.asyncio
async def test_delete_profile_and_start_require_the_current_profile_revision(
    store: SwarmStore,
) -> None:
    saved = await store.save_profile(_profile(), expected_revision=None)
    with pytest.raises(SwarmStoreError, match="revision_conflict"):
        await store.create_swarm(
            saved["id"],
            "Investigate",
            {"cwd": "C:/work"},
            request_id="stale-start",
            expected_profile_revision=2,
        )
    started = await store.create_swarm(
        saved["id"],
        "Investigate",
        {"cwd": "C:/work"},
        request_id="fresh-start",
        expected_profile_revision=1,
    )
    with pytest.raises(SwarmStoreError, match="revision_conflict"):
        await store.delete_profile(saved["id"], expected_revision=2)
    await store.delete_profile(saved["id"], expected_revision=1)
    with pytest.raises(SwarmStoreError, match="profile_not_found"):
        await store.get_profile(saved["id"])
    snapshot = await store.get_swarm(started["swarm_id"])
    assert snapshot["profile_snapshot"] == saved


@pytest.mark.asyncio
async def test_swarm_snapshots_profile_and_has_stable_roster(store: SwarmStore) -> None:
    started = await _swarm(store)
    swarm = await store.get_swarm(started["swarm_id"])
    assert swarm["state"] == "preparing"
    names = [item["display_name"] for item in swarm["participants"]]
    assert len(set(names)) == 2
    assert set(names) <= set(_PARTICIPANT_NAMES)
    profile = await store.get_profile(swarm["profile_snapshot"]["id"])
    profile["name"] = "Later"
    await store.save_profile(profile, expected_revision=1)
    assert (await store.get_swarm(started["swarm_id"]))["profile_snapshot"]["name"] == "Research"


def test_participant_name_pool_is_short_and_unique():
    assert len(_PARTICIPANT_NAMES) == 300
    assert len({name.casefold() for name in _PARTICIPANT_NAMES}) == 300
    assert all(
        name.isascii() and name.isalpha() and 1 <= len(name) <= 8 for name in _PARTICIPANT_NAMES
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [300, 601])
async def test_names_are_unique_across_formations_and_persist(store, count, monkeypatch):
    import secrets

    shuffles = []

    def shuffle(_random, names):
        shuffles.append(tuple(names))
        names.reverse()

    monkeypatch.setattr(secrets.SystemRandom, "shuffle", shuffle)
    saved = await store.save_profile(
        {
            **_profile(),
            "participants": [
                {"model": "model-a", "count": 150},
                {"model": "model-b", "count": count - 150},
            ],
        },
        expected_revision=None,
    )

    async def start(request_id):
        return await store.create_swarm(
            saved["id"],
            "goal",
            {},
            request_id=request_id,
            expected_profile_revision=saved["revision"],
        )

    started = await start("names")
    swarm_id = started["swarm_id"]
    original = (await store.get_swarm(swarm_id))["participants"]
    names = [p["display_name"] for p in original]
    assert names[:300] == list(reversed(_PARTICIPANT_NAMES))
    assert len(set(names)) == count
    assert all(len(name) <= 8 for name in names)
    assert [p["ordinal"] for p in original] == list(range(1, count + 1))
    assert (await start("names"))["replayed"]
    assert len(shuffles) == 1
    await store.close()
    await store.open()
    assert (await store.get_swarm(swarm_id))["participants"] == original
    await start("another-swarm")
    assert len(shuffles) == 2
