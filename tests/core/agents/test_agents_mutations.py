"""Agent updates, current-Session repair and deletion."""

import errno
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from core.agents import AgentError, AgentStore
from core.agents import agents as agents_module
from core.chat import ChatMessage
from core.sessions import ChatSessionManager, SessionAddress
from core.tools.availability import ToolAccess
from core.utils import tree_move
from tests.core.agents.agents_test_support import persisted
from tests.core.agents.agents_test_support import store as store
from tests.core.agents.agents_test_support import template_dir as template_dir

EARLY_TIMESTAMP = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
LATE_TIMESTAMP = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("repair", [False, True])
def test_concurrent_updates_and_current_session_repairs_do_not_lose_state(
    store: AgentStore, monkeypatch: pytest.MonkeyPatch, repair: bool
) -> None:
    agent = store.create("coder", "Original")
    if repair:
        store._session_manager().delete(SessionAddress(None, "coder", agent.current_session_id))
    first_write = Event()
    release_first = Event()
    second_started = Event()
    second_read = Event()
    original_write = store._write_agent
    original_read = agents_module._validated_agent_data

    def write(updated):
        if not first_write.is_set():
            first_write.set()
            assert release_first.wait(5)
        original_write(updated)

    def read(path):
        result = original_read(path)
        if second_started.is_set():
            second_read.set()
        return result

    def second():
        second_started.set()
        return store.get("coder") if repair else store.update("coder", model="other/model")

    monkeypatch.setattr(store, "_write_agent", write)
    monkeypatch.setattr(agents_module, "_validated_agent_data", read)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = (
            executor.submit(store.get, "coder")
            if repair
            else executor.submit(store.update, "coder", name="Changed")
        )
        try:
            assert first_write.wait(5)
            following = executor.submit(second)
            assert second_started.wait(5)
            second_read.wait(0.2)
        finally:
            release_first.set()
        first.result(timeout=5)
        following.result(timeout=5)

    if repair:
        sessions = store._session_manager().list_summaries("coder")
        assert [session["id"] for session in sessions] == [store.get("coder").current_session_id]
    else:
        persisted = store.get("coder")
        assert (persisted.name, persisted.model) == ("Changed", "other/model")


def test_failed_current_session_reset_removes_new_session(
    store: AgentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = store.create("coder")
    store._session_manager().delete(SessionAddress(None, "coder", agent.current_session_id))

    def fail_write(_agent):
        raise OSError("config unavailable")

    monkeypatch.setattr(store, "_write_agent", fail_write)
    with pytest.raises(OSError, match="config unavailable"):
        store.reset_current_after_session_removed("coder", agent.current_session_id)

    assert store._session_manager().list_summaries("coder") == []


def test_roster_verifies_every_current_session_in_one_read(
    store: AgentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    for agent_id in ("alpha", "beta", "gamma"):
        store.create(agent_id)
    manager = store._session_manager()
    dangling = store.get("beta").current_session_id
    manager.delete(SessionAddress(None, "beta", dangling))
    original = manager.existing_addresses
    probes: list[int] = []
    point_probes: list[SessionAddress] = []

    def counted(addresses):
        probes.append(len(addresses))
        return original(addresses)

    monkeypatch.setattr(manager, "existing_addresses", counted)
    monkeypatch.setattr(manager, "exists", point_probes.append)

    agents = {agent.id: agent for agent in store.list()}

    assert probes == [3]
    assert point_probes == []
    # The dangling pointer self-heals to a fresh live Session.
    healed = SessionAddress(None, "beta", agents["beta"].current_session_id)
    assert healed.session_id != dangling
    assert original([healed]) == {healed}

    # Provenance reads return the stored pointer without verifying it.
    alpha = SessionAddress(None, "alpha", agents["alpha"].current_session_id)
    manager.delete(alpha)
    probes.clear()
    assert store.get_raw("alpha").current_session_id == alpha.session_id
    assert probes == []
    assert store.get("alpha").current_session_id != alpha.session_id
    assert probes == [1]


def test_update_changes_mutable_fields_and_preserves_id(store: AgentStore) -> None:
    original = store.create("coder", "Coder Agent")
    current_session_id = original.current_session_id
    updated = store.update(
        "coder",
        name="Updated Coder",
        model="openai/gpt-5.2",
        tool_access={"mode": "selected", "allowed": ["read_file"]},
        tools={"subagent": {"allowed_agents": []}},
        memory_prompt_mode="off",
        custom_system_prompt_enabled=True,
        excluded_skills=["pdf"],
    )

    assert updated.id == "coder"
    assert updated.created_at == original.created_at
    assert updated.updated_at >= original.updated_at
    assert updated.name == "Updated Coder"
    assert updated.model == "openai/gpt-5.2"
    assert updated.tool_access == ToolAccess(mode="selected", allowed=("read_file",))
    assert updated.tools == {"subagent": {"allowed_agents": []}}
    assert updated.memory_prompt_mode == "off"
    assert updated.custom_system_prompt_enabled is True
    assert updated.excluded_skills == ["pdf"]
    assert updated.current_session_id == current_session_id
    assert store.get("coder") == updated
    # Clearing the exclusions removes the optional field from agent.json.
    store.update("coder", excluded_skills=[])
    assert "excluded_skills" not in persisted(store, "coder")


@pytest.mark.parametrize("name", [None, "   "])
def test_update_empty_optional_name_restores_id_default(
    store: AgentStore,
    name: str | None,
) -> None:
    store.create("coder", "Coder Agent")

    updated = store.update("coder", name=name)

    assert updated.name == "coder"


def test_update_moves_workspace_and_an_empty_workspace_restores_the_default(
    store: AgentStore,
    tmp_path: Path,
) -> None:
    store.create("coder", "Coder Agent")
    workspace = tmp_path / "updated-workspace"
    default = store.default_workspace("coder")

    updated = store.update("coder", workspace=workspace)

    assert updated.workspace == str(workspace.resolve())
    assert (workspace / "SOUL.md").exists()
    assert persisted(store, "coder")["workspace"] == str(workspace.resolve())
    assert store.update("coder", workspace=None).workspace == default
    store.update("coder", workspace=workspace)
    assert store.update("coder", workspace="").workspace == default


def test_explicit_root_project_round_trips_independently_of_workspace(
    store: AgentStore,
) -> None:
    created = store.create("coder", "Coder Agent")

    rooted = store.update("coder", root_project_id="vbot")
    assert rooted.root_project_id == "vbot"
    assert rooted.workspace == created.workspace

    unrooted = store.update("coder", root_project_id=None)
    assert unrooted.root_project_id is None
    assert unrooted.workspace == created.workspace


def test_workspace_copy_preserves_sources_and_backs_up_destinations(
    store: AgentStore,
    tmp_path: Path,
) -> None:
    agent = store.create("coder", "Coder Agent")
    source = Path(agent.workspace)
    (source / "SOUL.md").write_text("source soul", encoding="utf-8")
    (source / "USER.md").write_text("source user", encoding="utf-8")
    (source / "MEMORY.md").write_text("source memory", encoding="utf-8")
    destination = tmp_path / "new-workspace"
    destination.mkdir()
    (destination / "SOUL.md").write_text("old soul", encoding="utf-8")

    result = store.update_with_metadata(
        "coder",
        workspace=destination,
        copy_workspace_identity_files=True,
    )

    assert result.copied_files == ("SOUL.md", "USER.md", "MEMORY.md")
    assert result.backed_up_files == ("SOUL.md",)
    assert (destination / "SOUL.md").read_text(encoding="utf-8") == "source soul"
    assert (destination / "USER.md").read_text(encoding="utf-8") == "source user"
    assert (destination / "MEMORY.md").read_text(encoding="utf-8") == "source memory"
    assert (source / "SOUL.md").read_text(encoding="utf-8") == "source soul"
    assert result.backup_dir is not None
    assert Path(result.backup_dir, "SOUL.md").read_text(encoding="utf-8") == "old soul"


def test_workspace_copy_rolls_back_destination_when_agent_write_fails(
    store: AgentStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = store.create("coder", "Coder Agent")
    source = Path(agent.workspace)
    (source / "SOUL.md").write_text("source soul", encoding="utf-8")
    destination = tmp_path / "new-workspace"
    destination.mkdir()
    (destination / "SOUL.md").write_text("destination soul", encoding="utf-8")

    monkeypatch.setattr(
        store, "_write_agent", lambda _agent: (_ for _ in ()).throw(OSError("disk"))
    )

    with pytest.raises(OSError, match="disk"):
        store.update_with_metadata(
            "coder",
            workspace=destination,
            copy_workspace_identity_files=True,
        )

    assert (destination / "SOUL.md").read_text(encoding="utf-8") == "destination soul"
    assert persisted(store, "coder")["workspace"] == "agents/coder/workspace"


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"name": 123}, "name must be a string or null"),
        ({"model": 123}, "model must be a string"),
        ({"fallback_models": 123}, "fallback_models must be a list of strings"),
        ({"temperature": True}, "temperature must be a number"),
        ({"thinking_effort": "turbo"}, "thinking_effort must be one of"),
        ({"memory_prompt_mode": 1}, "memory_prompt_mode must be a string"),
        (
            {
                "tool_access": {
                    "mode": "selected",
                    "allowed": ["read_file"],
                    "denied": ["read_file"],
                }
            },
            "overlap",
        ),
        ({"allowed_skills": "debugging"}, "allowed_skills must be a list of strings"),
        ({"excluded_skills": ["*"]}, 'excluded_skills cannot contain "*"'),
        (
            {"tools": {"bash": {"allowed_env": ["OPENAI_API_KEY", "bad-key"]}}},
            "invalid environment key name",
        ),
        ({"custom_system_prompt_enabled": 1}, "custom_system_prompt_enabled must be a boolean"),
        ({"current_session_id": "missing"}, "current session does not exist: missing"),
        ({"id": "other"}, "Agent id is immutable"),
        ({"unknown": True}, "Unknown agent fields: unknown"),
    ],
)
def test_update_rejects_invalid_changes_and_keeps_the_agent(
    store: AgentStore, changes: dict[str, Any], message: str
) -> None:
    created = store.create("coder", "Coder Agent")

    with pytest.raises(AgentError, match=re.escape(message)):
        store.update("coder", **changes)

    assert store.get("coder") == created


def test_update_can_set_current_session_id_to_existing_session(store: AgentStore) -> None:
    original = store.create("coder", "Coder Agent")
    store._session_manager().create("coder", session_id="session-two")

    updated = store.update("coder", current_session_id="session-two")

    assert updated.current_session_id == "session-two"
    assert updated.current_session_id != original.current_session_id


def test_reset_current_after_session_removed_lands_on_newest_or_a_fresh_session(
    store: AgentStore,
) -> None:
    first = store.create("alpha", "Alpha").current_session_id
    manager = ChatSessionManager(store.data_dir)

    # A removed non-current Session leaves the pointer alone.
    manager.create("alpha", session_id="other")
    manager.delete(SessionAddress(None, "alpha", "other"))
    assert store.reset_current_after_session_removed("alpha", "other").current_session_id == first

    # A removed current Session (for example moved away) lands on the newest remaining one.
    manager.get(SessionAddress(None, "alpha", first)).append(
        ChatMessage.user("old", timestamp=EARLY_TIMESTAMP)
    )
    manager.create("alpha", session_id="newer").append(
        ChatMessage.user("recent", timestamp=LATE_TIMESTAMP)
    )
    manager.create("alpha", session_id="moved")
    store.update("alpha", current_session_id="moved")
    manager.delete(SessionAddress(None, "alpha", "moved"))
    assert store.reset_current_after_session_removed("alpha", "moved").current_session_id == (
        "newer"
    )

    # Without a remaining Session it creates a fresh one.
    manager.delete(SessionAddress(None, "alpha", first))
    manager.delete(SessionAddress(None, "alpha", "newer"))
    fresh = store.reset_current_after_session_removed("alpha", "newer").current_session_id
    assert fresh not in {first, "newer"}
    assert [session.id for session in manager.list("alpha")] == [fresh]


def test_delete_archives_agent_trees_and_external_workspaces_and_leaves_the_order(
    store: AgentStore, tmp_path: Path
) -> None:
    coder = store.create("coder", "Coder Agent")
    store._session_manager().create("coder", session_id="session")
    # A workspace outside the Agent tree (e.g. a repo an identity Agent is rooted in)
    # is not swept up by the Agent-directory move, so it is archived beside it.
    external_workspace = tmp_path / "rooted-repo"
    store.create("rooted", "Rooted Agent", workspace=external_workspace)
    store.create("beta", "Beta")

    archive_dir = store.delete("coder")
    rooted_archive = store.delete("rooted")

    assert archive_dir == store.data_dir / "archive" / "agents" / "coder"
    assert not (store.data_dir / "agents" / "coder").exists()
    assert not Path(coder.workspace).exists()
    assert (archive_dir / "agent" / "agent.json").exists()
    assert not store._session_manager().exists(
        SessionAddress(project_id=None, agent_id="coder", session_id="session")
    )
    # The default workspace lives inside the Agent directory and is archived within it.
    assert (archive_dir / "agent" / "workspace" / "SOUL.md").exists()
    assert not external_workspace.exists()
    assert (rooted_archive / "agent" / "agent.json").exists()
    assert (rooted_archive / "workspace" / "SOUL.md").exists()
    assert [agent.id for agent in store.list_with_order().agents] == ["beta"]
    order = json.loads((store.data_dir / "agents" / "order.json").read_text(encoding="utf-8"))
    assert order["agent_ids"] == ["beta"]


def test_delete_restores_active_agent_and_previous_archive_on_session_failure(
    store: AgentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.create("coder", "First")
    previous_archive = store.delete("coder")
    marker = previous_archive / "keep.txt"
    marker.write_text("previous", encoding="utf-8")
    store.create("coder", "Second")

    def fail_archive(_agent_id: str) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(store._session_manager(), "archive_identity_agent_sessions", fail_archive)

    with pytest.raises(RuntimeError, match="database unavailable"):
        store.delete("coder")

    assert store.get("coder").name == "Second"
    assert marker.read_text(encoding="utf-8") == "previous"


@pytest.mark.parametrize("failed_restore", ["active", "previous"])
def test_delete_keeps_previous_archive_when_compensation_fails(
    store: AgentStore, monkeypatch: pytest.MonkeyPatch, failed_restore: str
) -> None:
    store.create("coder", "First")
    archive = store.delete("coder")
    (archive / "keep.txt").write_text("previous", encoding="utf-8")
    store.create("coder", "Second")
    real_replace = os.replace

    def fail_archive(_agent_id):
        raise RuntimeError("database unavailable")

    def replace(source, destination):
        if (
            failed_restore == "active" and Path(destination) == store.data_dir / "agents" / "coder"
        ) or (failed_restore == "previous" and Path(source).name == "previous"):
            raise OSError("rollback unavailable")
        real_replace(source, destination)

    monkeypatch.setattr(store._session_manager(), "archive_identity_agent_sessions", fail_archive)
    monkeypatch.setattr(tree_move, "_replace", replace)
    with pytest.raises(AgentError, match="rollback unavailable"):
        store.delete("coder")

    retained = list(archive.parent.glob(".coder-archive-*/previous/keep.txt"))
    assert len(retained) == 1
    assert retained[0].read_text(encoding="utf-8") == "previous"
    if failed_restore == "active":  # the Agent's files stay in the archive, never deleted
        assert (archive / "agent" / "agent.json").is_file()


@pytest.mark.parametrize(
    "blocker",
    [
        "refused-rename",
        pytest.param(
            "open-file",
            marks=pytest.mark.skipif(
                os.name != "nt", reason="only Windows refuses to rename a tree with open files"
            ),
        ),
    ],
)
@pytest.mark.parametrize("blocked", ["agent-tree", "external-workspace"])
def test_delete_keeps_every_tree_when_a_move_cannot_complete(
    store: AgentStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocker: str,
    blocked: str,
) -> None:
    store.create("coder", "First")
    previous_archive = store.delete("coder")
    (previous_archive / "keep.txt").write_text("previous", encoding="utf-8")
    workspace = tmp_path / "rooted-repo"
    workspace.mkdir()
    (workspace / "README.md").write_text("repository", encoding="utf-8")
    store.create("coder", "Second", workspace=workspace)
    blocked_tree = store.data_dir / "agents" / "coder" if blocked == "agent-tree" else workspace
    real_replace = os.replace

    def replace(source, destination):
        if Path(source) == blocked_tree:
            raise PermissionError(errno.EACCES, "held open by another program")
        real_replace(source, destination)

    if blocker == "refused-rename":
        monkeypatch.setattr(tree_move, "_replace", replace)
        held = None
    else:
        held = (blocked_tree / "held.txt").open("w", encoding="utf-8")
    try:
        with pytest.raises(AgentError, match="Agent archival failed"):
            store.delete("coder")
    finally:
        if held is not None:
            held.close()

    # A move that fails means nothing moved: no partial copy replaces or removes a whole tree.
    assert store.get("coder").name == "Second"
    assert (workspace / "README.md").read_text(encoding="utf-8") == "repository"
    assert (previous_archive / "keep.txt").read_text(encoding="utf-8") == "previous"
    assert [path.name for path in previous_archive.parent.iterdir()] == ["coder"]


def test_delete_across_volumes_archives_the_agent_though_the_original_stays_partly(
    store: AgentStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.create("coder", "Coder")
    store.create("beta", "Beta")
    archive_root = store.data_dir / "archive"
    real_replace = os.replace

    def replace(source, destination):
        if archive_root in Path(destination).parents:  # the archive is on another volume
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_replace(source, destination)

    def remove_nothing(_path):
        raise PermissionError(errno.EACCES, "held open by another program")

    monkeypatch.setattr(tree_move, "_replace", replace)
    monkeypatch.setattr(tree_move, "_remove_tree", remove_nothing)

    archive_dir = store.delete("coder")

    assert (archive_dir / "agent" / "agent.json").is_file()
    assert not (store.data_dir / "agents" / "coder").exists()
    assert [agent.id for agent in store.list_with_order().agents] == ["beta"]


def test_delete_agent_named_like_sibling_archive_roots_never_touches_them(
    store: AgentStore,
) -> None:
    # ``archive/sessions`` and ``archive/projects`` are the session/project archive
    # roots. An agent id equal to those names must archive under the agents subtree
    # instead of replace-deleting the sibling roots wholesale.
    for reserved_like_id in ("sessions", "projects"):
        store.create(reserved_like_id, f"Agent {reserved_like_id}")

    archived_session = store.data_dir / "archive" / "sessions" / "agents" / "other" / "s1.jsonl"
    archived_session.parent.mkdir(parents=True)
    archived_session.write_text('{"role":"user"}\n', encoding="utf-8")
    archived_project = store.data_dir / "archive" / "projects" / "vbot" / "project.json"
    archived_project.parent.mkdir(parents=True)
    archived_project.write_text("{}\n", encoding="utf-8")

    for reserved_like_id in ("sessions", "projects"):
        archive_dir = store.delete(reserved_like_id)
        assert archive_dir == store.data_dir / "archive" / "agents" / reserved_like_id
        assert (archive_dir / "agent" / "agent.json").exists()

    assert archived_session.exists()
    assert archived_project.exists()
