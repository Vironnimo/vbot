"""Explicit identity grants survive lifecycle changes without transferring to replacements."""

from typing import Any

import pytest

from core.agents import AgentError, AgentStore
from tests.core.agents.agents_test_support import persisted, rewrite
from tests.core.agents.agents_test_support import store as store
from tests.core.agents.agents_test_support import template_dir as template_dir


def _tools(*targets: str) -> dict:
    return {"subagent": {"allowed_agents": list(targets)}}


@pytest.mark.parametrize("future", [False, True])
def test_grant_binds_first_identity_even_when_cleanup_fails(
    store: AgentStore, monkeypatch: pytest.MonkeyPatch, future: bool
) -> None:
    monkeypatch.setattr(
        "core.agents._delegation.utc_now_timestamp", lambda: "2026-01-01T00:00:00.000000Z"
    )
    if not future:
        store.create("target")
    holder = store.create("holder", tools=_tools("target"))
    if future:
        store.create("target")
    original = store.get("target")
    binding = holder.delegation_bindings["target"]
    assert store.delegation_target_allowed("target", binding)
    payload = store.data_dir / "archive" / "old-target"
    with store.archive_files("target", payload):
        pass
    # Deliberately leave the holder's config untouched, as with failed cleanup.
    replacement = store.create("target")
    assert replacement.created_at != original.created_at
    assert not store.delegation_target_allowed("target", binding)
    assert store.get("holder").tools == _tools("target")
    assert store.update("holder", tools=_tools("target"), name="Edited").delegation_bindings == {
        "target": binding
    }
    store.update("holder", tools=_tools())
    renewed = store.update("holder", tools=_tools("target"))
    assert store.delegation_target_allowed("target", renewed.delegation_bindings["target"])


def test_aborted_creation_does_not_consume_future_grant(
    store: AgentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    holder = store.create("holder", tools=_tools("future"))
    seed = store._seed_workspace
    with monkeypatch.context() as patch:
        patch.setattr(store, "_seed_workspace", lambda _: (_ for _ in ()).throw(OSError("full")))
        with pytest.raises(OSError, match="full"):
            store.create("future")
    assert store._seed_workspace == seed
    store.create("future")
    assert store.delegation_target_allowed("future", holder.delegation_bindings["future"])


def test_repeated_retirement_of_restored_identity_consumes_new_future_grant(
    store: AgentStore,
) -> None:
    store.create("target")
    payload = store.data_dir / "archive" / "old"
    with store.archive_files("target", payload):
        pass
    holder = store.create("holder", tools=_tools("target"))
    binding = holder.delegation_bindings["target"]
    with store.restore_files(payload, "target", workspace=None, root_project_id=None):
        pass
    assert store.delegation_target_allowed("target", binding)
    with store.archive_files("target", payload):
        pass
    store.create("target")
    assert not store.delegation_target_allowed("target", binding)


@pytest.mark.parametrize("future", [False, True])
def test_rename_moves_bound_grant_and_old_snapshot_cannot_target_reused_id(
    store: AgentStore, future: bool
) -> None:
    if not future:
        store.create("target")
    holder = store.create("holder", tools=_tools("target"))
    if future:
        store.create("target")
    old_binding = holder.delegation_bindings["target"]
    rename = store.rename("target", "renamed")
    store.finish_rename(rename.rename)
    moved = store.get("holder")
    assert moved.tools == _tools("renamed")
    assert store.delegation_target_allowed("renamed", moved.delegation_bindings["renamed"])
    store.create("target")
    assert not store.delegation_target_allowed("target", old_binding)


@pytest.mark.parametrize("damage", ["missing", "malformed", "newer", "shorter"])
def test_future_grant_refuses_unavailable_or_inconsistent_history(
    store: AgentStore, damage: str
) -> None:
    holder = store.create("holder", tools=_tools("target"))
    store.create("target")
    path = store.data_dir / "agents" / "retirements.json"
    binding = holder.delegation_bindings["target"]
    if damage == "missing":
        path.unlink()
    elif damage == "malformed":
        path.write_text("{", encoding="utf-8")
    elif damage == "newer":
        path.write_text('{"format_version": 2, "retired": {}}', encoding="utf-8")
    else:
        assert isinstance(binding, int)
        binding += 1
    assert not store.delegation_target_allowed("target", binding)


def test_legacy_grants_are_not_reinterpreted_by_unrelated_writes(store: AgentStore) -> None:
    store.create("holder", tools=_tools("future"))
    rewrite(store, "holder", "delegation_bindings")
    store.update("holder", tools=_tools("future"), name="Changed")
    assert "delegation_bindings" not in persisted(store, "holder")
    with pytest.raises(AgentError, match="Unknown agent fields"):
        store.update("holder", delegation_bindings={"future": 0})


@pytest.mark.parametrize("stamp", [None, "bad"])
def test_new_grant_establishes_missing_legacy_target_identity_once(
    store: AgentStore, stamp: str | None
) -> None:
    store.create("target")
    rewrite(store, "target", created_at=stamp)
    holder = store.create("holder", tools=_tools("target"))
    binding = holder.delegation_bindings["target"]
    assert binding == persisted(store, "target")["created_at"]
    assert store.delegation_target_allowed("target", binding)


def test_history_write_failure_prevents_identity_detachment(
    store: AgentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.create("target")
    holder = store.create("holder", tools=_tools("target"))

    def fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("core.agents._delegation.write_json_document", fail)
    with (
        pytest.raises(OSError, match="disk full"),
        store.archive_files("target", store.data_dir / "archive" / "old"),
    ):
        pytest.fail("the identity must remain live")
    assert store.delegation_target_allowed("target", holder.delegation_bindings["target"])


def test_recreated_history_cannot_reactivate_an_old_future_grant(store: AgentStore) -> None:
    holder = store.create("holder", tools=_tools("target"))
    binding = holder.delegation_bindings["target"]
    store.create("target")
    with store.archive_files("target", store.data_dir / "archive" / "old"):
        pass
    store.create("target")
    (store.data_dir / "agents" / "retirements.json").unlink()
    store.update("holder", tools=_tools("target", "another-future"))
    assert not store.delegation_target_allowed("target", binding)


@pytest.mark.parametrize("stamp", [None, "malformed"])
def test_future_grant_needs_a_persisted_target_identity(
    store: AgentStore, stamp: str | None
) -> None:
    holder = store.create("holder", tools=_tools("target"))
    store.create("target")
    rewrite(store, "target", created_at=stamp)
    assert not store.delegation_target_allowed("target", holder.delegation_bindings["target"])


def test_restore_follows_renamed_holder_without_granting_its_replacement(store: AgentStore) -> None:
    store.create("target")
    store.create("holder", tools=_tools("target"))
    payload = store.data_dir / "archive" / "target"
    with store.archive_files("target", payload):
        pass
    recorded: list[Any] = []
    store.remove_delegation_grants("target", recorded.extend)
    renamed = store.rename("holder", "renamed-holder")
    store.finish_rename(renamed.rename)
    store.create("holder", tools=_tools())
    with store.restore_files(payload, "restored-target", workspace=None, root_project_id=None):
        pass
    assert store.restore_delegation_grants("restored-target", recorded) == ("renamed-holder",)
    assert store.get("holder").tools == _tools()
    restored_holder = store.get("renamed-holder")
    assert store.delegation_target_allowed(
        "restored-target", restored_holder.delegation_bindings["restored-target"]
    )


@pytest.mark.parametrize("future", [False, True])
def test_restoring_replacement_cannot_turn_a_stale_grant_into_a_new_grant(
    store: AgentStore, future: bool
) -> None:
    if not future:
        store.create("target")
    store.create("holder", tools=_tools("target"))
    if future:
        store.create("target")
    with store.archive_files("target", store.data_dir / "archive" / "original"):
        pass
    # The original holder cleanup failed; the same string now names a replacement.
    store.create("target")
    replacement_payload = store.data_dir / "archive" / "replacement"
    with store.archive_files("target", replacement_payload):
        pass
    recorded: list[Any] = []
    store.remove_delegation_grants("target", recorded.extend)
    with store.restore_files(replacement_payload, "restored", workspace=None, root_project_id=None):
        pass
    store.restore_delegation_grants("restored", recorded)
    assert store.get("holder").tools == _tools()
    assert "restored" not in store.get("holder").delegation_bindings


def test_rename_destination_grants_are_revoked_in_captured_run_policy(store: AgentStore) -> None:
    holder = store.create("holder", tools=_tools("future"))
    binding = holder.delegation_bindings["future"]
    store.create("source")
    rename = store.rename("source", "future")
    store.finish_rename(rename.rename)
    assert store.get("holder").tools == _tools()
    assert not store.delegation_target_allowed("future", binding)
    # A later explicit grant still selects the renamed current identity.
    renewed = store.update("holder", tools=_tools("future"))
    assert store.delegation_target_allowed("future", renewed.delegation_bindings["future"])
