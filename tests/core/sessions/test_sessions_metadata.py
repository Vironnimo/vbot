"""Session metadata, prompt state, titles and identity-reference updates."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3

import pytest

from core.chat import ChatMessage
from core.chat.errors import ChatSessionError
from core.runs import RunKind
from core.sessions import SESSION_RUN_KINDS_META_KEY, SeenSkillsUpdate, SessionAddress
from core.sessions.errors import SessionNotFoundError
from tests.core.sessions.history_fixtures import admit_run, history_revision, settle_run
from tests.core.sessions.sessions_test_support import _address, _continuation_start


def _count_writes(manager, monkeypatch) -> list[object]:
    database = manager._store.database
    original = database.write
    writes: list[object] = []

    def counted(fn, **kwargs):
        writes.append(fn)
        return original(fn, **kwargs)

    monkeypatch.setattr(database, "write", counted)
    return writes


def _state_revision(manager, address: SessionAddress) -> int:
    return int(
        manager._store._read(
            lambda connection: connection.execute(
                "SELECT state_revision FROM sessions WHERE project_id = ? AND agent_id = ? "
                "AND session_id = ? AND state = 'live'",
                (address.project_id or "", address.agent_id, address.session_id),
            ).fetchone()[0]
        )
    )


def test_metadata_value_reads_one_projected_or_residual_value(manager, monkeypatch) -> None:
    address = _address("coder", "narrow")
    manager.create(address.agent_id, session_id=address.session_id)
    policy = {"enabled": False}
    manager.set_metadata(address, {"compaction_policy": policy, "title": "Named", "flag": None})

    def no_metadata_decode(_address):
        raise AssertionError("a single value must not decode the complete metadata")

    with monkeypatch.context() as patch:
        patch.setattr(manager._store, "metadata", no_metadata_decode)
        assert manager.metadata_value(address, "compaction_policy") == policy
        assert manager.metadata_value(address, "title") == "Named"
        assert manager.metadata_value(address, "flag") is None
        assert manager.metadata_value(address, "missing") is None
    # A projected value must fit its column; a failed write changes nothing.
    with pytest.raises(ChatSessionError, match="compaction_policy must be an object"):
        manager.set_metadata(address, {"compaction_policy": "not-an-object"})
    assert manager.metadata_value(address, "compaction_policy") == policy
    manager.set_metadata(address, {"flag": True})
    assert manager.metadata_value(address, "flag") is True
    assert manager.metadata_value(address, "title") is None
    with pytest.raises(SessionNotFoundError):
        manager.metadata_value(_address("coder", "missing"), "title")


def test_listable_metadata_is_normalized_out_of_open_ended_metadata(manager) -> None:
    address = _address("coder", "normalized-metadata")
    manager.create(address.agent_id, session_id=address.session_id)
    metadata = {
        "title": "Release planning",
        "auto_title": "Automatic title",
        "source_channel_id": "tg-main",
        "platform": "telegram",
        "platform_conv_id": "chat-42",
        "is_subagent_session": True,
        "subagent_parent": {
            "id": "work",
            "agent_id": "parent",
            "session_id": "root",
            "run_id": "parent-run",
            "tool_call_id": "call",
            "tool_call_index": 1,
            "project_id": None,
        },
        "compaction_policy": {"enabled": False},
        "extension_state": "x" * 100_000,
    }

    manager.set_metadata(address, metadata)
    asyncio.run(admit_run(manager, address, RunKind.SUBAGENT))

    assert manager.get_metadata(address) == {**metadata, "run_kinds": ["subagent"]}
    with sqlite3.connect(manager._store.path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT session_key, metadata_json, title, subagent_parent_session_id, "
            "subagent_parent_tool_call_index, compaction_policy_json FROM sessions "
            "WHERE agent_id = ? AND session_id = ?",
            (address.agent_id, address.session_id),
        ).fetchone()
        assert row is not None
        run_kinds = connection.execute(
            "SELECT run_kind FROM session_run_kinds WHERE session_key = ?",
            (row["session_key"],),
        ).fetchall()
    assert json.loads(row["metadata_json"]) == {"extension_state": "x" * 100_000}
    assert row["title"] == "Release planning"
    assert row["subagent_parent_session_id"] == "root"
    assert row["subagent_parent_tool_call_index"] == 1
    assert json.loads(row["compaction_policy_json"]) == {"enabled": False}
    assert [tuple(kind) for kind in run_kinds] == [("subagent",)]


def test_prompt_cache_affinity_id_is_prompt_state_not_metadata(manager, monkeypatch) -> None:
    address = _address("coder", "affinity")
    session = manager.create(address.agent_id, session_id=address.session_id)
    default = manager.prompt_cache_affinity_id(address)
    assert manager.prompt_cache_affinity_id(address) == default
    user = ChatMessage.user("first")
    session.append(user)
    edited = session.apply_edit(user.id, [ChatMessage.user("second")])

    def no_metadata_decode(_address):
        raise AssertionError("the affinity id must not decode the complete metadata")

    with monkeypatch.context() as patch:
        patch.setattr(manager._store, "metadata", no_metadata_decode)
        assert manager.prompt_cache_affinity_id(address) == edited.prompt_cache_affinity_id
    assert edited.prompt_cache_affinity_id != default
    assert "prompt_cache_affinity_id" not in manager.get_metadata(address)
    with pytest.raises(ChatSessionError, match="dedicated APIs"):
        manager.set_metadata(address, {"prompt_cache_affinity_id": "chosen"})
    with pytest.raises(ChatSessionError, match="dedicated APIs"):
        manager.set_metadata(address, {"pinned_skill_catalog": {"catalog_text": "x"}})
    with pytest.raises(SessionNotFoundError):
        manager.prompt_cache_affinity_id(_address("coder", "missing"))


def test_metadata_activity_and_continuation_change_state_not_history(manager) -> None:
    address = _address("coder", "session-one")
    session = manager.create("coder", session_id=address.session_id)
    session.append(ChatMessage.user("hello"))
    settle_run(manager, address, "run-1")
    session.start_run("run-one")
    revision = history_revision(manager, address)

    manager.set_metadata(address, {"project": "vbot"})
    session.append_continuation_records([_continuation_start()])

    assert history_revision(manager, address) == revision
    assert manager.get_metadata(address)["project"] == "vbot"
    assert manager.get_metadata(address)[SESSION_RUN_KINDS_META_KEY] == [RunKind.USER.value]
    continuation = session.load_continuation()
    assert continuation is not None
    assert continuation.checkpoint_id == "checkpoint-one"
    assert manager.mark_terminal_run_read(address, "wrong")["marked_read"] is False
    assert manager.mark_terminal_run_read(address, "run-1")["marked_read"] is True
    assert history_revision(manager, address) == revision


def test_unchanged_metadata_and_activity_mutations_write_nothing(manager) -> None:
    address = _address("coder", "unchanged-mutations")
    manager.create(address.agent_id, session_id=address.session_id)
    manager.set_metadata(address, {"title": "Kept"})
    manager.record_seen_skills(address, SeenSkillsUpdate(baseline=("alpha",)))
    settle_run(manager, address, "run-1")
    assert manager.mark_terminal_run_read(address, "run-1")["marked_read"] is True
    writer = manager._store._writer
    revision = _state_revision(manager, address)
    changes = writer.total_changes

    previous, updated = manager.mutate_metadata_with_previous(
        address, lambda metadata: metadata.update(title="Kept")
    )
    manager.record_seen_skills(address, SeenSkillsUpdate(baseline=(), added=("alpha",)))
    assert manager.mark_terminal_run_read(address, "run-1")["marked_read"] is False

    assert previous == updated
    assert writer.total_changes == changes
    assert _state_revision(manager, address) == revision

    asyncio.run(admit_run(manager, address, RunKind.CRON))
    assert _state_revision(manager, address) > revision
    # Run kinds form a set, reported in name order.
    assert manager.get_metadata(address)[SESSION_RUN_KINDS_META_KEY] == [
        RunKind.CRON.value,
        RunKind.USER.value,
    ]


def test_ensure_metadata_writes_only_a_real_change(manager, monkeypatch) -> None:
    address = _address("coder", "channel")
    writes = _count_writes(manager, monkeypatch)

    def route(metadata):
        metadata["platform"] = "telegram"

    with pytest.raises(SessionNotFoundError):
        manager.ensure_metadata(address, route)
    assert writes == []

    previous, updated = manager.ensure_metadata(address, route, create_missing=True)
    # A missing Session and its metadata commit together.
    assert len(writes) == 1
    assert previous == {}
    assert updated == {"platform": "telegram"}
    assert manager.get_metadata(address)["platform"] == "telegram"

    revision = _state_revision(manager, address)
    previous, updated = manager.ensure_metadata(address, route, create_missing=True)
    assert len(writes) == 1
    assert previous == updated == manager.get_metadata(address)
    assert _state_revision(manager, address) == revision

    manager.set_title(address, "Concurrent title")
    writes.clear()
    previous, updated = manager.ensure_metadata(
        address, lambda metadata: metadata.__setitem__("platform", "discord")
    )
    # The writer reapplies the mutation to the latest row, keeping other edits.
    assert len(writes) == 1
    assert previous["platform"] == "telegram"
    assert manager.get_metadata(address)["platform"] == "discord"
    assert manager.get_metadata(address)["title"] == "Concurrent title"


def test_callback_failure_does_not_turn_a_committed_title_into_an_error(
    manager, caplog: pytest.LogCaptureFixture
) -> None:
    address = _address("coder", "session-one")
    manager.create("coder", session_id=address.session_id)

    def fail(_address: SessionAddress) -> None:
        raise RuntimeError("observer failed")

    manager.add_title_changed_callback(fail)
    caplog.set_level(logging.ERROR)

    assert manager.set_title(address, "Persisted") == "Persisted"
    assert manager.get_metadata(address)["title"] == "Persisted"
    # The failure reaches the daily log: a vbot logger, with traceback and Session.
    [record] = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert record.name.startswith("vbot.")
    assert record.exc_info is not None
    assert "session-one" in record.getMessage()


@pytest.mark.parametrize("restoring", [False, True])
def test_identity_reference_changes_roll_back_together(manager, monkeypatch, restoring) -> None:
    from core.sessions import _store_values

    children = [manager.create("child", session_id=f"child-{index}") for index in range(2)]
    for child in children:
        manager.set_metadata(
            child.address,
            {"subagent_parent": {"agent_id": "old", "project_id": None, "session_id": "parent"}},
        )
    updates = manager.retarget_identity_agent_references("old", "new") if restoring else ()
    before = [manager.get_metadata(child.address) for child in children]
    original = _store_values._subagent_parent_columns
    calls = 0

    def fail_second_write(parent):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected metadata write failure")
        return original(parent)

    monkeypatch.setattr(_store_values, "_subagent_parent_columns", fail_second_write)
    with pytest.raises(OSError):
        if restoring:
            manager.restore_identity_agent_references(updates)
        else:
            manager.retarget_identity_agent_references("old", "new")

    assert [manager.get_metadata(child.address) for child in children] == before


def test_identity_reference_retarget_skips_unrelated_sessions(manager) -> None:
    changed = manager.create("child", session_id="changed")
    unrelated = manager.create("child", session_id="unrelated")
    qualified = manager.create("child", session_id="qualified")
    manager.set_metadata(changed.address, {"subagent_parent": {"agent_id": "old"}})
    manager.set_metadata(
        qualified.address, {"subagent_parent": {"agent_id": "old", "project_id": "project"}}
    )

    def state(session):
        return manager._store._read(
            lambda connection: tuple(
                connection.execute(
                    "SELECT state_revision, subagent_parent_agent_id, subagent_parent_project_id "
                    "FROM sessions WHERE session_id = ? AND state = 'live'",
                    (session.id,),
                ).fetchone()
            )
        )

    before = [state(session) for session in (unrelated, qualified)]

    updates = manager.retarget_identity_agent_references("old", "new")

    assert [update.address for update in updates] == [changed.address]
    assert manager.get_metadata(changed.address)["subagent_parent"]["agent_id"] == "new"
    assert [state(session) for session in (unrelated, qualified)] == before


@pytest.mark.parametrize("new_parent", [False, True])
def test_rename_compensation_only_restores_its_unchanged_parent_reference(manager, new_parent):
    manager.create("worker", session_id="child")
    address = _address("worker", "child")
    # The shape the Sub-Agent service records; the store keeps these fields as columns.
    original_parent = {
        "id": "original-work",
        "agent_id": "before",
        "session_id": "parent-session",
        "run_id": "parent-run",
        "tool_call_id": "parent-call",
        "tool_call_index": 0,
        "project_id": None,
    }
    manager.set_metadata(address, {"title": "Before", "subagent_parent": original_parent})

    updates = manager.retarget_identity_agent_references("before", "after")
    assert manager.get_metadata(address)["subagent_parent"]["agent_id"] == "after"

    def concurrent_edit(metadata):
        metadata["title"] = "Concurrent title"
        metadata["unrelated"] = {"retained": True}
        if new_parent:
            # A later delegation can retain the same renamed Agent but establish
            # a different parent Run. Compensation must not rewrite that link.
            metadata["subagent_parent"] = {
                **metadata["subagent_parent"],
                "id": "new-work",
                "run_id": "new-parent-run",
            }

    manager.mutate_metadata(address, concurrent_edit)
    expected_parent = (
        manager.get_metadata(address)["subagent_parent"] if new_parent else original_parent
    )
    manager.restore_identity_agent_references(updates)

    assert manager.get_metadata(address) == {
        "title": "Concurrent title",
        "unrelated": {"retained": True},
        "subagent_parent": expected_parent,
    }
