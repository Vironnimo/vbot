"""Agent rename: its Agent-owned half, its rollback, and its recovery after a crash."""

import asyncio
import os
import sqlite3
import sys
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import pytest

from core.agents import (
    SKILL_AGENT_CREATED_AT_KEY,
    SKILL_AGENT_ID_KEY,
    Agent,
    AgentAlreadyExistsError,
    AgentError,
    AgentReferencedError,
    AgentRename,
    AgentStore,
    InvalidAgentIdError,
)
from core.sessions import (
    ARCHIVE_KIND_AGENT,
    ArchiveEntryFilter,
    ArchiveScope,
    ChatSessionManager,
    SessionAddress,
)
from tests.core.agents.agents_test_support import create_with_session, persisted
from tests.core.agents.agents_test_support import store as store
from tests.core.agents.agents_test_support import template_dir as template_dir

_CHILD = SessionAddress(None, "manager", "child")
_CURATING = SessionAddress(None, "librarian", "curating")
_CURATED = SessionAddress(None, "librarian", "curated")
_SKILL_SUBJECT = f"json_extract(metadata_json, '$.{SKILL_AGENT_ID_KEY}')"
_ARCHIVED = SessionAddress(None, "coder", "archived")
_ARCHIVED_CHILD = SessionAddress(None, "manager", "archived-child")


class _Killed(BaseException):
    """The process dying at one step: no ``except Exception`` compensation runs."""


def _record(store: AgentStore) -> Path:
    return store.data_dir / "agents" / "rename-pending.json"


def _seed(store: AgentStore) -> Agent:
    """``coder``, a ``manager`` that delegates to it, and Sessions that name it.

    A ``manager`` Session links to ``coder`` as its Sub-Agent parent; a Librarian
    Session maintains the Skills of ``coder``. The archive holds a deleted
    Session of ``coder``, a deleted ``manager`` Session that was its Sub-Agent
    child, a deleted Librarian Session that maintained its Skills, and an
    earlier ``researcher`` Agent with its own Session.
    """
    sessions = store._session_manager()
    ledger = sessions.archive_ledger
    earlier = ledger.begin(ARCHIVE_KIND_AGENT, subject_id="researcher", agent_id="researcher")
    sessions.create("researcher", session_id="theirs")
    ledger.commit_scope(earlier.entry_key, ArchiveScope(agent_id="researcher"))
    created = store.create(
        "coder", "Coder Agent", tools={"subagent": {"allowed_agents": ["coder", "coder@project"]}}
    )
    store.create("manager", "Manager", tools={"subagent": {"allowed_agents": ["coder"]}})
    sessions.create("coder", session_id="kept")
    sessions.create("manager", session_id="child")
    sessions.set_metadata(
        _CHILD,
        {"subagent_parent": {"agent_id": "coder", "session_id": "kept", "project_id": None}},
    )
    sessions.create("coder", session_id=_ARCHIVED.session_id)
    sessions.create("manager", session_id=_ARCHIVED_CHILD.session_id)
    sessions.set_metadata(
        _ARCHIVED_CHILD,
        {"subagent_parent": {"agent_id": "coder", "session_id": "archived", "project_id": None}},
    )
    asyncio.run(sessions.archive(_ARCHIVED_CHILD))
    asyncio.run(sessions.archive(_ARCHIVED))
    for curating in (_CURATED, _CURATING):
        _bind_librarian(store, curating, "coder", created.created_at)
    asyncio.run(sessions.archive(_CURATED))
    return store.update(created.id, current_session_id="kept")


def _bind_librarian(
    store: AgentStore, address: SessionAddress, agent_id: str, created_at: str
) -> None:
    """Create a Librarian Session bound to the Agent ``agent_id`` created at ``created_at``."""
    sessions = store._session_manager()
    sessions.create(address.agent_id, session_id=address.session_id)
    sessions.set_metadata(
        address, {SKILL_AGENT_ID_KEY: agent_id, SKILL_AGENT_CREATED_AT_KEY: created_at}
    )


def _archive_scopes(store: AgentStore) -> set[tuple[str, str, tuple[str, ...]]]:
    """Each archive entry's kind and Agent scope, with the Agents of its member Sessions."""
    ledger = store._session_manager().archive_ledger
    return {
        (
            entry.kind,
            entry.agent_id,
            tuple(member.address.agent_id for member in ledger.members(entry.entry_key)),
        )
        for entry in ledger.page(ArchiveEntryFilter()).entries
    }


def _archived_value(store: AgentStore, address: SessionAddress, expression: str) -> str:
    """One stored value of an archived Session: the SQL ``expression`` over its row."""
    with closing(sqlite3.connect(store.data_dir / "sessions.db")) as connection:
        row = connection.execute(
            f"SELECT {expression} FROM sessions WHERE agent_id = ? "
            "AND session_id = ? AND state = 'archived'",
            (address.agent_id, address.session_id),
        ).fetchone()
    return str(row[0])


def _link_to(store: AgentStore, session_id: str, parent_agent_id: str) -> SessionAddress:
    """Give a new ``manager`` Session a Sub-Agent link to a parent Session that does not exist."""
    sessions = store._session_manager()
    address = sessions.create("manager", session_id=session_id).address
    sessions.set_metadata(
        address,
        {
            "subagent_parent": {
                "agent_id": parent_agent_id,
                "session_id": "gone",
                "project_id": None,
            }
        },
    )
    return address


def _link_agent_id(store: AgentStore, address: SessionAddress) -> str:
    return str(store._session_manager().get_metadata(address)["subagent_parent"]["agent_id"])


def _assert_agent_is(store: AgentStore, created: Agent, agent_id: str, other_id: str) -> None:
    """Every Agent-owned trace of ``created`` names ``agent_id`` and none ``other_id``."""
    sessions = store._session_manager()
    assert store.find(other_id) is None
    agent = store.get(agent_id)
    assert agent.name == "Coder Agent"
    assert agent.current_session_id == created.current_session_id
    assert agent.workspace == store.default_workspace(agent_id)
    assert agent.tools["subagent"]["allowed_agents"] == [agent_id, "coder@project"]
    assert store.get("manager").tools["subagent"]["allowed_agents"] == [agent_id]
    assert sessions.exists(SessionAddress(None, agent_id, "kept"))
    assert sessions.list_addresses(None, agent_id=other_id) == []
    assert sessions.get_metadata(_CHILD)["subagent_parent"]["agent_id"] == agent_id
    # Archived Sessions and their links follow, so a restore returns them to the
    # Agent; the earlier ``researcher`` keeps its own Session for its restore.
    assert _archive_scopes(store) == {
        ("session", agent_id, (agent_id,)),
        ("session", "manager", ("manager",)),
        ("session", "librarian", ("librarian",)),
        ("agent", "researcher", ("researcher",)),
    }
    assert _archived_value(store, _ARCHIVED_CHILD, "subagent_parent_agent_id") == agent_id
    assert sessions.metadata_value(_CURATING, SKILL_AGENT_ID_KEY) == agent_id
    assert _archived_value(store, _CURATED, _SKILL_SUBJECT) == agent_id
    assert sessions.metadata_value(_CURATING, SKILL_AGENT_CREATED_AT_KEY) == created.created_at
    assert [listed.id for listed in store.list()] == [agent_id, "manager"]
    assert sorted(path.name for path in (store.data_dir / "agents").iterdir()) == sorted(
        [agent_id, "manager", "order.json", "retirements.json"]
    )


def test_rename_moves_complete_agent_tree_and_rebases_internal_workspace(
    store: AgentStore,
) -> None:
    created = create_with_session(store, "coder", "Coder Agent")
    old_dir = store.data_dir / "agents" / "coder"
    custom_workspace = old_dir / "homes" / "primary"
    store.update("coder", workspace=custom_workspace)
    (old_dir / "prompts").mkdir()
    (old_dir / "prompts" / "runtime.md").write_text("custom prompt", encoding="utf-8")
    (old_dir / "skills" / "private-skill").mkdir(parents=True)
    (old_dir / "skills" / "private-skill" / "SKILL.md").write_text("# Private\n", encoding="utf-8")
    ChatSessionManager(store.data_dir).create("coder", session_id="kept-session")

    result = store.rename("coder", "researcher")

    new_dir = store.data_dir / "agents" / "researcher"
    assert not old_dir.exists()
    assert result.agent.id == "researcher"
    assert result.agent.name == "Coder Agent"
    assert result.agent.created_at == created.created_at
    assert result.agent.updated_at != created.updated_at
    assert result.agent.workspace == str((new_dir / "homes" / "primary").resolve())
    assert sorted(result.session_ids) == sorted([created.current_session_id, "kept-session"])
    assert store._session_manager().exists(
        SessionAddress(project_id=None, agent_id="researcher", session_id="kept-session")
    )
    assert not store._session_manager().exists(
        SessionAddress(project_id=None, agent_id="coder", session_id="kept-session")
    )
    assert (new_dir / "prompts" / "runtime.md").read_text(encoding="utf-8") == "custom prompt"
    assert (new_dir / "skills" / "private-skill" / "SKILL.md").is_file()
    data = persisted(store, "researcher")
    assert data["id"] == "researcher"
    assert data["workspace"] == "agents/researcher/homes/primary"
    # The rename stays recorded until the caller has moved every other reference.
    assert result.rename == AgentRename(source_id="coder", target_id="researcher")
    assert _record(store).is_file()
    store.finish_rename(result.rename)
    assert not _record(store).exists()


def test_rename_preserves_external_workspace(store: AgentStore, tmp_path: Path) -> None:
    workspace = tmp_path / "external-workspace"
    store.create("coder", "Coder Agent", workspace=workspace)

    renamed = store.rename("coder", "researcher").agent

    assert renamed.workspace == str(workspace.resolve())
    assert workspace.is_dir()
    assert (workspace / "SOUL.md").is_file()


def test_rename_retargets_allowed_agent_ids_sub_agent_links_and_librarian_sessions(
    store: AgentStore,
) -> None:
    _seed(store)
    # A link follows its parent Session; one to a Session that did not move stays.
    orphan = _link_to(store, "orphan", "coder")
    # A Librarian binding follows the Agent, not its id: one to an earlier
    # ``coder`` stays for that Agent's restore.
    earlier = SessionAddress(None, "librarian", "earlier")
    _bind_librarian(store, earlier, "coder", "2020-01-01T00:00:00+00:00")

    result = store.rename("coder", "researcher")

    assert result.policy_agent_ids == ("manager", "researcher")
    # The live and the archived child: an archived Session moves like a live one.
    assert result.session_link_count == 2
    assert store.get("manager").tools["subagent"]["allowed_agents"] == ["researcher"]
    assert store.get("researcher").tools["subagent"]["allowed_agents"] == [
        "researcher",
        "coder@project",
    ]
    assert _link_agent_id(store, _CHILD) == "researcher"
    assert _link_agent_id(store, orphan) == "coder"
    # The Librarian Sessions go on maintaining the renamed Agent's Skills, a
    # deleted one too once restored.
    sessions = store._session_manager()
    assert sessions.metadata_value(_CURATING, SKILL_AGENT_ID_KEY) == "researcher"
    assert _archived_value(store, _CURATED, _SKILL_SUBJECT) == "researcher"
    assert sessions.metadata_value(earlier, SKILL_AGENT_ID_KEY) == "coder"


def test_revert_rename_restores_the_original_agent(store: AgentStore) -> None:
    created = _seed(store)
    # Left by an earlier ``researcher``: the revert moves back only what the rename moved.
    leftover = _link_to(store, "leftover", "researcher")
    earlier = SessionAddress(None, "librarian", "earlier")
    _bind_librarian(store, earlier, "researcher", "2020-01-01T00:00:00+00:00")
    result = store.rename("coder", "researcher")

    reverse = store.revert_rename(result.rename)

    assert reverse == AgentRename(source_id="researcher", target_id="coder", rollback=True)
    store.finish_rename(reverse)
    _assert_agent_is(store, created, "coder", "researcher")
    assert _link_agent_id(store, leftover) == "researcher"
    assert store._session_manager().metadata_value(earlier, SKILL_AGENT_ID_KEY) == "researcher"


def test_rename_supports_case_only_id_change(store: AgentStore) -> None:
    store.create("coder", "Coder Agent")

    result = store.rename("coder", "Coder")
    store.finish_rename(result.rename)

    assert result.agent.id == "Coder"
    assert store.get("Coder").id == "Coder"
    assert sorted(path.name for path in (store.data_dir / "agents").iterdir()) == [
        "Coder",
        "order.json",
        "retirements.json",
    ]


def test_rename_preserves_agent_order_position(store: AgentStore) -> None:
    store.create("alpha", "Alpha")
    store.create("beta", "Beta")
    store.create("gamma", "Gamma")
    listing = store.list_with_order()
    store.reorder(
        ["gamma", "alpha", "beta"],
        expected_revision=listing.order_revision,
    )

    store.rename("alpha", "renamed")

    assert [agent.id for agent in store.list()] == ["gamma", "renamed", "beta"]


def _occupy_with_agent(store: AgentStore) -> tuple[str, ...]:
    store.create("researcher", "Researcher Agent")
    return ()


def _occupy_with_grant(store: AgentStore) -> tuple[str, ...]:
    store.create("manager", tools={"subagent": {"allowed_agents": ["researcher"]}})
    return ()


def _occupy_with_archived_session(store: AgentStore) -> tuple[str, ...]:
    """A deleted Session of an earlier ``researcher``, which its archive entry keeps."""
    sessions = store._session_manager()
    address = sessions.create("researcher", session_id="left").address
    return (f"archive:{asyncio.run(sessions.archive(address)).entry_id}",)


@pytest.mark.parametrize(
    ("occupy", "destination", "refused"),
    [
        pytest.param(_occupy_with_agent, "researcher", AgentAlreadyExistsError, id="agent"),
        # A reference another owner holds would pass to the renamed Agent, and a
        # revert would take it along. The refusal changes nothing, so it keeps
        # even the delegation grant a rename would otherwise remove.
        pytest.param(_occupy_with_grant, "researcher", AgentReferencedError, id="references"),
        # Archived Sessions move with the renamed Agent's, and back with a revert.
        pytest.param(
            _occupy_with_archived_session,
            "researcher",
            AgentReferencedError,
            id="archived-sessions",
        ),
        # Windows reserves the name for a device; it is refused on every platform.
        pytest.param(lambda _store: (), "con", InvalidAgentIdError, id="windows-reserved"),
    ],
)
def test_rename_rejects_an_unusable_destination(
    store: AgentStore,
    occupy: Callable[[AgentStore], tuple[str, ...]],
    destination: str,
    refused: type[AgentError],
) -> None:
    store.create("coder", "Coder Agent")
    archived = occupy(store)
    before = {agent.id: agent for agent in store.list()}

    with pytest.raises(refused) as raised:
        store.rename("coder", destination, external_references=("channel:tg-old",))

    if isinstance(raised.value, AgentReferencedError):
        assert raised.value.agent_id == "researcher"
        assert raised.value.references == tuple(sorted(("channel:tg-old", *archived)))
    assert {agent.id: agent for agent in store.list()} == before
    assert not _record(store).exists()


def test_rename_removes_delegation_grants_left_for_the_new_id(store: AgentStore) -> None:
    # Left by a deleted ``researcher``, or given before any Agent had the id: it
    # grants nothing, so the renamed Agent does not inherit it and a revert does
    # not move it to ``coder``. A qualified address names a Team Agent and stays.
    store.create("coder", "Coder Agent")
    store.create(
        "writer", "Writer", tools={"subagent": {"allowed_agents": ["researcher", "researcher@p"]}}
    )

    result = store.rename("coder", "researcher")

    assert result.policy_agent_ids == ("writer",)
    assert store.get("writer").tools["subagent"]["allowed_agents"] == ["researcher@p"]
    store.finish_rename(store.revert_rename(result.rename))
    assert store.get("writer").tools["subagent"]["allowed_agents"] == ["researcher@p"]
    assert [agent.id for agent in store.list()] == ["coder", "writer"]


def test_a_pending_rename_blocks_other_renames_and_its_ids(store: AgentStore) -> None:
    store.create("coder", "Coder Agent")
    store.create("writer", "Writer")
    result = store.rename("coder", "researcher")

    with pytest.raises(AgentError):
        store.rename("writer", "author")
    with pytest.raises(AgentError):
        store.create("coder")

    store.finish_rename(result.rename)
    assert store.create("coder").id == "coder"
    assert store.rename("writer", "author").agent.id == "author"


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_rename_rolls_tree_back_when_config_write_fails(
    store: AgentStore,
    monkeypatch: pytest.MonkeyPatch,
    template_dir: Path,
    rollback_fails: bool,
) -> None:
    created = _seed(store)
    original_write = store._write_agent

    def fail_new_config(agent: Agent) -> None:
        if agent.id == "researcher":
            raise OSError("disk full")
        original_write(agent)

    monkeypatch.setattr(store, "_write_agent", fail_new_config)
    if rollback_fails:
        # The tree cannot move back (a file held open); the next start finishes.
        _fail_replace(monkeypatch, lambda source: source.name == "researcher", PermissionError)

    with pytest.raises(OSError, match="disk full"):
        store.rename("coder", "researcher")

    if rollback_fails:
        monkeypatch.undo()
        store.close()
        store = AgentStore(store.data_dir, template_dir=template_dir)
        reverse = store.recover_rename()
        assert reverse == AgentRename(source_id="researcher", target_id="coder", rollback=True)
        store.finish_rename(reverse)
    assert not _record(store).exists()
    _assert_agent_is(store, created, "coder", "researcher")


def _fail_replace(
    monkeypatch: pytest.MonkeyPatch,
    matches: Callable[[Path], bool],
    error: type[BaseException],
) -> None:
    """Make ``os.replace`` of a matching source path raise ``error``."""
    original = os.replace

    def replace(source: Any, destination: Any) -> None:
        if matches(Path(source)):
            raise error(f"cannot move {source}")
        original(source, destination)

    monkeypatch.setattr(os, "replace", replace)


def _die(*_args: Any) -> None:
    raise _Killed


def _kill_on_sessions(store: AgentStore, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    monkeypatch.setattr(store._session_manager(), method, _die)


def _kill_on_write(store: AgentStore, monkeypatch: pytest.MonkeyPatch, agent_id: str) -> None:
    original = store._write_agent

    def write(agent: Agent) -> None:
        if agent.id == agent_id:
            raise _Killed
        original(agent)

    monkeypatch.setattr(store, "_write_agent", write)


def _kill_on_order(store: AgentStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store._roster, "_write_agent_order", _die)


_KILL_POINTS: dict[str, Callable[[AgentStore, pytest.MonkeyPatch], None]] = {
    "before-sessions": lambda store, patch: _kill_on_sessions(
        store, patch, "retarget_identity_agent_sessions"
    ),
    "before-sub-agent-links": lambda store, patch: _kill_on_sessions(
        store, patch, "retarget_identity_agent_references"
    ),
    "before-tree": lambda _store, patch: _fail_replace(
        patch, lambda source: source.name == "coder", _Killed
    ),
    "between-case-only-moves": lambda _store, patch: _fail_replace(
        patch, lambda source: source.name.startswith(".coder.rename-"), _Killed
    ),
    "before-config": lambda store, patch: _kill_on_write(store, patch, "researcher"),
    "before-order": _kill_on_order,
    "before-policies": lambda store, patch: _kill_on_write(store, patch, "manager"),
}


@contextmanager
def _killed_at(store: AgentStore, point: str) -> Iterator[None]:
    with pytest.MonkeyPatch.context() as patch:
        _KILL_POINTS[point](store, patch)
        with pytest.raises(_Killed):
            yield


@pytest.mark.parametrize(
    "point",
    [
        pytest.param(
            point,
            marks=pytest.mark.skipif(
                point == "between-case-only-moves" and sys.platform != "win32",
                reason="only case-insensitive filesystems stage a case-only rename",
            ),
        )
        for point in _KILL_POINTS
    ],
)
def test_a_rename_killed_at_any_step_completes_on_the_next_start(
    store: AgentStore, template_dir: Path, point: str
) -> None:
    created = _seed(store)
    new_id = "Coder" if point == "between-case-only-moves" else "researcher"

    with _killed_at(store, point):
        store.rename("coder", new_id)
    store.close()

    restarted = AgentStore(store.data_dir, template_dir=template_dir)
    rename = restarted.recover_rename()
    assert rename is not None
    assert (rename.source_id, rename.target_id, rename.rollback) == ("coder", new_id, False)
    restarted.finish_rename(rename)

    _assert_agent_is(restarted, created, new_id, "coder")


def test_a_rename_killed_while_reverting_rolls_back_on_the_next_start(
    store: AgentStore, template_dir: Path
) -> None:
    created = _seed(store)
    result = store.rename("coder", "researcher")

    with pytest.MonkeyPatch.context() as patch:
        _fail_replace(patch, lambda source: source.name == "researcher", _Killed)
        with pytest.raises(_Killed):
            store.revert_rename(result.rename)
    store.close()

    restarted = AgentStore(store.data_dir, template_dir=template_dir)
    rename = restarted.recover_rename()
    assert rename == AgentRename(source_id="researcher", target_id="coder", rollback=True)
    restarted.finish_rename(rename)

    _assert_agent_is(restarted, created, "coder", "researcher")
