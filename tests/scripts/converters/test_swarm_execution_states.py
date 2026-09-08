from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest
import pytest_asyncio

from core.sessions import SessionAddress, TemporarySessionBinding
from resources.extensions.swarm.store import SwarmStore, SwarmStoreError
from scripts.converters.swarm_execution_states import convert


@pytest_asyncio.fixture
async def legacy(tmp_path):
    source = tmp_path / "original.db"
    store = SwarmStore(source)
    await store.open()
    try:
        profile = await store.save_profile(
            {
                "schema_version": 1,
                "name": "Retained",
                "tool_access": {"mode": "selected", "allowed": []},
                "participants": [{"model": "fixture/model", "count": 2}],
                "working_directory": {"kind": "directory", "path": str(tmp_path)},
            },
            expected_revision=None,
        )
        started = await store.create_swarm(
            profile["id"],
            "Original goal",
            {"cwd": str(tmp_path)},
            request_id="start",
            expected_profile_revision=1,
        )
        sid = started["swarm_id"]
        peers = (await store.get_swarm(sid))["participants"]
        binding = TemporarySessionBinding(
            SessionAddress(None, "temporary", "session"),
            "generation",
            "swarm",
            sid,
            peers[0]["id"],
            {},
        )
        await store.bind_participant_session(binding)
        post = await store.post_human(sid, text="Keep this Board message", request_id="message")
    finally:
        await store.close()
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("CREATE TABLE lifecycle_intents(id INTEGER PRIMARY KEY)")
        for name in ("wait_reason", "summary_json", "artifacts_json", "completion_call_id"):
            connection.execute(f"ALTER TABLE participants ADD COLUMN {name} TEXT")
        connection.execute(
            "UPDATE participants SET state='done',summary_json=?", ('{"summary":"Keep in backup"}',)
        )
        connection.execute("UPDATE swarms SET state='completed'")
        profile["reminders"].update({"wake": True, "completion": True})
        connection.execute("UPDATE profiles SET payload=?", (json.dumps(profile),))
        connection.execute("UPDATE swarms SET profile_snapshot=?", (json.dumps(profile),))
    return source, sid, peers[0]["id"], post["post_id"]


@pytest.mark.asyncio
async def test_conversion_preserves_original_board_and_session_and_reopens_done_peer(
    legacy, tmp_path
):
    source, sid, pid, post_id = legacy
    original_hash = hashlib.sha256(source.read_bytes()).digest()
    output = tmp_path / "converted.db"
    convert(source, output)
    assert hashlib.sha256(source.read_bytes()).digest() == original_hash
    store = SwarmStore(output)
    await store.open()
    try:
        swarm = await store.get_swarm(sid)
        assert swarm["state"] == "stopped"
        assert {p["state"] for p in swarm["participants"]} == {"idle"}
        assert set(swarm["profile_snapshot"]["reminders"]) == {"delivery", "resume"}
        assert (await store.read_human_posts(sid)).entries[0]["id"] == post_id
        profile = (await store.list_profiles()).entries[0]
        await store.save_profile(profile, expected_revision=profile["revision"])
        resumed = await store.begin_resume(
            sid, request_id="resume", actor="user", participant_id=pid
        )
        assert resumed["participant_ids"] == [pid]
        assert (await store.prepare_inbox_delivery(sid, pid))["entries"][0]["id"] == post_id
    finally:
        await store.close()
    with closing(sqlite3.connect(output)) as connection:
        assert connection.execute(
            "SELECT session_id,generation_id FROM participant_sessions"
        ).fetchone() == ("session", "generation")


@pytest.mark.asyncio
async def test_runtime_rejects_retired_storage_without_mutating_it(legacy):
    source, *_ = legacy
    before = hashlib.sha256(source.read_bytes()).digest()
    store = SwarmStore(source)
    with pytest.raises(SwarmStoreError, match="storage_conversion_required"):
        await store.open()
    assert hashlib.sha256(source.read_bytes()).digest() == before


def test_conversion_never_overwrites_existing_output(tmp_path):
    source = tmp_path / "source.db"
    source.write_bytes(b"source sentinel")
    output = tmp_path / "output.db"
    output.write_bytes(b"output sentinel")
    for target in (source, output):
        with pytest.raises(ValueError):
            convert(source, target)
    assert source.read_bytes() == b"source sentinel"
    assert output.read_bytes() == b"output sentinel"


@pytest.mark.asyncio
async def test_invalid_source_does_not_leave_partial_output(legacy, tmp_path):
    source, *_ = legacy
    with closing(sqlite3.connect(source)) as connection, connection:
        connection.execute("UPDATE participants SET state='unknown'")
    output = tmp_path / "output.db"
    with pytest.raises(ValueError):
        convert(source, output)
    assert not output.exists()
