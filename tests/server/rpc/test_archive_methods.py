"""archive.list/show/restore/purge: params, results, error codes with data, locks, events."""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.agents import AgentPlacement
from core.agents.temporary import TemporaryAgentConfig, TemporaryAgentRegistry
from core.permissions import PermissionRulesError, resolve_permission_profile
from core.sessions import SessionAddress
from core.storage import StorageManager
from core.tools.availability import ToolAccess
from core.utils.timestamps import format_canonical_timestamp
from server.access import bound_principal
from server.access._principal import HOST
from server.events import ServerEventBus
from tests.server.rpc.project_methods_test_support import _make_repo, _make_state
from tests.server.rpc_test_support import resource_changes, rpc_error, rpc_result


async def _archived(tmp_path: Path) -> tuple[SimpleNamespace, dict[str, str]]:
    """A state whose archive holds the Agent coder, a manager Session and the Project vbot.

    ``manager`` delegates to coder. Returns the state and the entry ids by kind.
    """
    state = _make_state(tmp_path)
    runtime = state.runtime
    runtime.agents.create("manager", "Manager", tools={"subagent": {"allowed_agents": ["coder"]}})
    runtime.agents.create("coder", "Coder")
    runtime.sessions.create("coder", session_id="work")
    notes = runtime.sessions.create("manager", session_id="notes").address
    runtime.sessions.set_title(notes, "Notes")
    runtime.sessions.mutate_metadata(
        notes, lambda meta: meta.update(agent_overrides={"thinking_effort": "high"})
    )
    await rpc_result(
        state,
        "project.add",
        cwd=str(_make_repo(tmp_path, "vbot", "builder.md")),
        display_name="vBot",
    )
    runtime.sessions.create("builder", session_id="s1", project_id="vbot")
    archive = runtime.archive
    entries = {
        "agent": (await archive.archive_agent("coder")).entry_id,
        "session": (await archive.archive_session(notes)).entry_id,
        "project": (await archive.archive_project("vbot")).entry_id,
    }
    # Only what the RPC under test publishes.
    state.event_bus = ServerEventBus()
    return state, entries


def _purge_at(state: SimpleNamespace, entry_id: str) -> str:
    entry = state.runtime.sessions.archive_ledger.entry(entry_id)
    start = datetime.fromisoformat(entry.retention_start)
    return format_canonical_timestamp(start + timedelta(days=30))


@pytest.mark.asyncio
async def test_profile_deletion_preserves_live_private_and_archived_assignments(
    tmp_path: Path,
) -> None:
    state = _make_state(tmp_path)
    runtime = state.runtime
    runtime.storage = StorageManager(runtime.agents.data_dir)
    name = "Local only"
    runtime.storage.update_permission_profiles({name: {"read": "work_area"}})
    participants = TemporaryAgentRegistry(
        runtime.sessions,
        profile_rules=lambda selected: resolve_permission_profile(
            selected, runtime.storage.load_settings().get("permission_profiles", {})
        ),
    )
    config = TemporaryAgentConfig(
        model="example/model",
        cwd=tmp_path,
        tool_access=ToolAccess(mode="none"),
        allowed_skills=[],
        tools={},
        name="Worker",
        permission_ceiling_profile=name,
    )
    binding = participants.create(
        owner_name="team",
        group_id="work",
        participant_id="worker",
        config=config,
    )
    runtime.agents.create("owner", permission_profile=name)
    runtime.agents.create(
        "helper", placement=AgentPlacement("private", "owner"), permission_profile=name
    )
    runtime.projects.create("project", "Project", _make_repo(tmp_path, "project", "builder.md"))
    runtime.projects.set_override("project", "builder", "permission_profile", name)
    group = (await runtime.archive.archive_agent("owner")).entry_id
    project = (await runtime.archive.archive_project("project")).entry_id

    with bound_principal(HOST):
        refused = await rpc_error(state, "permissions.delete_profile", name=name)
        assert refused["code"] == "permission_profile_in_use"
        assert set(refused["data"]["agents"]) == {
            f"owner ({group})",
            f"helper ({group})",
            f"builder@project ({project})",
            f"team Session {binding.address.session_id}",
        }

        # Archived owner groups cannot resume. Their snapshots remain readable as history.
        await runtime.sessions.archive_temporary_group(
            owner_name="team", group_id="work", reason="finished"
        )
        await runtime.archive.restore(group, target_id="renamed")
        await runtime.archive.purge(entry_ids=[project])
        refused = await rpc_error(state, "permissions.delete_profile", name=name)
        assert set(refused["data"]["agents"]) == {"renamed", "helper"}
        runtime.agents.update("helper", permission_profile=None)
        runtime.agents.update("renamed", permission_profile=None)

        path = runtime.agents.data_dir / "agents" / "renamed" / "agent.json"
        saved = path.read_bytes()
        path.write_bytes(b"{")
        refused = await rpc_error(state, "permissions.delete_profile", name=name)
        assert refused["code"] == "permission_profile_references_unavailable"
        assert refused["data"]["locations"] == [str(path)]
        path.write_bytes(saved)
        await rpc_result(state, "permissions.delete_profile", name=name)
        with pytest.raises(PermissionRulesError):
            participants.create(
                owner_name="team", group_id="next", participant_id="worker", config=config
            )


@pytest.mark.asyncio
async def test_profile_deletion_checks_persisted_extension_selections_after_disable(
    tmp_path: Path,
) -> None:
    from core.database import open_database
    from core.extensions.databases import extension_database_spec
    from resources.extensions.factory import store as factory_store
    from resources.extensions.swarm import store as swarm_store
    from tests.resources.extensions.swarm.swarm_test_support import _profile

    state = _make_state(tmp_path)
    data_dir = state.runtime.agents.data_dir
    storage = state.runtime.storage = StorageManager(data_dir)
    name, ceiling = "Participant", "Ceiling"
    storage.update_permission_profiles({name: {"read": "work_area"}, ceiling: {"write": "scratch"}})
    swarm_spec = extension_database_spec(data_dir, "swarm", "swarm", swarm_store.SCHEMA_SQL)
    factory_spec = extension_database_spec(
        data_dir,
        "factory",
        "factory",
        factory_store.SCHEMA_SQL,
        migrations=factory_store.MIGRATIONS,
    )
    db = open_database(swarm_spec)
    try:
        swarm = swarm_store.SwarmStore(db)
        await swarm.open()
        with pytest.raises(PermissionRulesError):
            await swarm.save_profile(
                {**_profile(), "permission_ceiling_profile": "Removed"}, expected_revision=None
            )
        profile = await swarm.save_profile(
            {
                **_profile(),
                "permission_ceiling_profile": ceiling,
                "participants": [{"model": "model-a", "count": 1, "permission_profile": name}],
            },
            expected_revision=None,
        )
        run = await swarm.create_swarm(
            profile["id"],
            "goal",
            {"cwd": str(tmp_path)},
            request_id="start",
            expected_profile_revision=profile["revision"],
        )
        # The run keeps its participant selection even after editing the template.
        await swarm.save_profile(
            {**profile, "participants": [{"model": "model-a", "count": 1}]},
            expected_revision=profile["revision"],
        )
    finally:
        db.close()
    # A disabled Factory from before native teams has no configurable group ceiling.
    legacy = extension_database_spec(
        data_dir,
        "factory",
        "factory",
        factory_store.SCHEMA_SQL.replace(factory_store.NATIVE_SCHEMA_SQL, ""),
        migrations=tuple(
            migration
            for migration in factory_store.MIGRATIONS
            if migration.name
            not in {"native_factory_teams", "factory_budget_changes", "agent_profile_models"}
        ),
    )
    open_database(legacy).close()
    storage.update_permission_profiles({"Unused": {}})
    with bound_principal(HOST):
        await rpc_result(state, "permissions.delete_profile", name="Unused")
    db = open_database(factory_spec)
    try:
        factories = factory_store.FactoryStore(db).native
        factory = await factories.create_factory(
            "project", leader_agent_id="leader", config={"permission_ceiling_profile": name}
        )
        with pytest.raises(PermissionRulesError):
            await factories.update_factory(
                factory["id"], {"config": {"permission_ceiling_profile": "Removed"}}
            )
    finally:
        db.close()
    with bound_principal(HOST):
        refused = await rpc_error(state, "permissions.delete_profile", name=name)
        assert refused["code"] == "permission_profile_in_use"
        assert set(refused["data"]["agents"]) == {
            f"Swarm {run['swarm_id']}",
            f"Factory {factory['id']}",
        }
        refused = await rpc_error(state, "permissions.delete_profile", name=ceiling)
        assert set(refused["data"]["agents"]) == {
            f"Swarm {run['swarm_id']}",
            f"Swarm profile {profile['id']}",
        }
        # A missing registered file is unavailable; only unconfigured stores are absent.
        factory_spec.path.rename(factory_spec.path.with_suffix(".removed"))
        storage.update_permission_profiles({"Other": {}})
        refused = await rpc_error(state, "permissions.delete_profile", name="Other")
        assert (refused["code"], refused["data"]["locations"]) == (
            "permission_profile_references_unavailable",
            ["ext.factory.factory"],
        )


@pytest.mark.asyncio
async def test_list_pages_entries_newest_first_with_their_retention(tmp_path: Path) -> None:
    state, entries = await _archived(tmp_path)

    first = await rpc_result(state, "archive.list", limit=2)
    second = await rpc_result(state, "archive.list", limit=2, cursor=first["next_cursor"])

    assert [entry["entry_id"] for entry in first["entries"] + second["entries"]] == [
        entries["project"],
        entries["session"],
        entries["agent"],
    ]
    assert (first["retention_days"], first["retention_unknown"]) == (30, False)
    assert second["next_cursor"] is None
    agent_entry = state.runtime.sessions.archive_ledger.entry(entries["agent"])
    assert second["entries"][0] == {
        "entry_id": entries["agent"],
        "kind": "agent",
        "state": "archived",
        "subject_id": "coder",
        "project_id": None,
        "agent_id": "coder",
        "owner_name": None,
        "label": "Coder",
        "archived_at": agent_entry.archived_at,
        "origin": "operation",
        "purge_at": _purge_at(state, entries["agent"]),
        "session_count": 1,
        "review_fork_count": 0,
        "restorable": True,
        "not_restorable_reason": None,
        "may_hold_user_folders": False,
        "accepts_target_id": True,
        "private_agents": [],
    }
    assert first["entries"][1]["label"] == "Notes"


@pytest.mark.asyncio
async def test_group_archive_payload_names_the_private_agents(tmp_path: Path) -> None:
    state = _make_state(tmp_path)
    agents = state.runtime.agents
    agents.create("owner", "Owner")
    agents.create("worker", "Worker", placement=AgentPlacement("private", "owner"))
    archived = await state.runtime.archive.archive_agent("owner")
    listed = await rpc_result(state, "archive.list")
    shown = await rpc_result(state, "archive.show", entry_id=archived.entry_id)
    assert listed["entries"][0]["private_agents"] == [{"agent_id": "worker", "name": "Worker"}]
    assert shown["entry"]["private_agents"] == listed["entries"][0]["private_agents"]
    restored = await rpc_result(
        state, "archive.restore", entry_id=archived.entry_id, target_id="renamed"
    )
    assert restored["restored"] == {"agent_id": "renamed", "private_agent_ids": ["worker"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filters", "kinds"),
    [
        pytest.param({"kind": "session"}, ["session"], id="kind"),
        # A bare Agent id selects its Identity scope: the Agent and its own Sessions.
        pytest.param({"agent_id": "coder"}, ["agent"], id="identity-agent"),
        pytest.param({"agent_id": "manager"}, ["session"], id="identity-sessions"),
        pytest.param({"project_id": "vbot"}, ["project"], id="project"),
        pytest.param({"agent_id": "builder@vbot"}, [], id="project-agent-address"),
    ],
)
async def test_list_filters_by_kind_agent_and_project(
    tmp_path: Path, filters: dict[str, Any], kinds: list[str]
) -> None:
    state, _entries = await _archived(tmp_path)

    result = await rpc_result(state, "archive.list", **filters)

    assert [entry["kind"] for entry in result["entries"]] == kinds


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"kind": "workspace"}, id="unknown-kind"),
        pytest.param({"limit": 201}, id="limit-too-large"),
        pytest.param({"cursor": {"archived_at": "yesterday", "entry_id": "arc_x"}}, id="cursor"),
        pytest.param({"agent_id": "builder@vbot", "project_id": "other"}, id="two-projects"),
        pytest.param({"sort": "oldest"}, id="unsupported-field"),
    ],
)
async def test_list_rejects_invalid_params(tmp_path: Path, params: dict[str, Any]) -> None:
    state = _make_state(tmp_path)

    error = await rpc_error(state, "archive.list", **params)

    assert error["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_show_returns_sessions_files_details_and_the_restore_check(tmp_path: Path) -> None:
    state, entries = await _archived(tmp_path)
    entry_id = entries["agent"]

    result = await rpc_result(state, "archive.show", entry_id=entry_id)

    assert result["entry"]["entry_id"] == entry_id
    assert "restores_access" not in result
    assert [(session["agent_id"], session["review_fork"]) for session in result["sessions"]] == [
        ("coder", False)
    ]
    assert result["session_count"] == 1
    assert result["files"] == {
        "state": "present",
        "trees": [
            {
                "role": "agent",
                "path": f"archive/entries/{entry_id}/agent",
                "source_path": "agents/coder",
                "user_folder": False,
            }
        ],
    }
    details = result["details"]
    assert (details["name"], details["grants"], details["root_project_id"]) == (
        "Coder",
        ["manager"],
        None,
    )
    assert details["workspace"]["external"] is False
    assert "restore_plan" not in details
    assert result["restore"] == {
        "possible": True,
        "target_id": "coder",
        "blockers": [],
        "warnings": [],
    }


@pytest.mark.asyncio
async def test_restore_refuses_a_taken_id_and_restores_under_a_new_one(tmp_path: Path) -> None:
    state, entries = await _archived(tmp_path)
    entry_id = entries["agent"]
    state.runtime.agents.create("coder", "Second Coder")

    conflict = await rpc_error(state, "archive.restore", entry_id=entry_id)
    restored = await rpc_result(state, "archive.restore", entry_id=entry_id, target_id="coder-2")

    assert conflict == {
        "code": "archive_restore_conflict",
        "message": f"cannot restore archive entry {entry_id}: an Agent with id coder exists; "
        "restore it under another id (target_id)",
        "data": {
            "entry_id": entry_id,
            "kind": "agent",
            "conflicts": [
                {
                    "code": "agent_id_taken",
                    "id": "coder",
                    "message": "an Agent with id coder exists",
                    "agent_id": "coder",
                }
            ],
            "fix": "target_id",
        },
    }
    assert restored == {
        "entry_id": entry_id,
        "kind": "agent",
        "subject_id": "coder",
        "restored": {"agent_id": "coder-2", "private_agent_ids": []},
        "session_count": 1,
        "grant_agent_ids": ["manager"],
        "warnings": [],
    }
    assert state.runtime.agents.get("coder-2").name == "Coder"
    assert state.runtime.agents.get("coder").name == "Second Coder"
    assert [event["kind"] for event in resource_changes(state)] == ["agents"]


@pytest.mark.asyncio
async def test_restore_warnings_carry_their_facts(tmp_path: Path) -> None:
    state, entries = await _archived(tmp_path)
    await state.runtime.archive.archive_agent("manager")

    restored = await rpc_result(state, "archive.restore", entry_id=entries["agent"])

    assert (restored["grant_agent_ids"], restored["warnings"]) == (
        [],
        [
            {
                "code": "grant_target_missing",
                "message": "Agent manager no longer exists; its delegation grant is not restored",
                "agent_id": "manager",
            }
        ],
    )


@pytest.mark.asyncio
async def test_restore_of_a_session_entry_reports_its_address(tmp_path: Path) -> None:
    state, entries = await _archived(tmp_path)

    session_entry = entries["session"]
    shown = await rpc_result(state, "archive.show", entry_id=session_entry)
    assert shown["restores_access"] is False

    restored = await rpc_result(state, "archive.restore", entry_id=session_entry)

    assert restored["restored"] == {
        "session_id": "notes",
        "agent_id": "manager",
        "project_id": None,
    }
    assert state.runtime.sessions.exists(SessionAddress(None, "manager", "notes"))
    assert resource_changes(state) == [
        {"kind": "sessions", "scope": {"agent_id": "manager", "project_id": None}}
    ]


def _missing_payload(data_dir: Path, _state: SimpleNamespace, entry_id: str) -> None:
    shutil.rmtree(data_dir / "archive" / "entries" / entry_id)


def _purging(_data_dir: Path, state: SimpleNamespace, entry_id: str) -> None:
    state.runtime.sessions.archive_ledger.begin_purge(entry_id)


def _stuck_restore(_data_dir: Path, state: SimpleNamespace, entry_id: str) -> None:
    state.runtime.sessions.archive_ledger.begin_restore(
        entry_id, {"target_id": None, "live_path": "/data/agents/coder", "problem": "invalid"}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arrange", "code", "data"),
    [
        pytest.param(
            _missing_payload,
            "archive_not_restorable",
            lambda entry_id: {
                "entry_id": entry_id,
                "blockers": [
                    {
                        "code": "payload_missing",
                        "message": "its files are missing",
                        "path": f"archive/entries/{entry_id}/agent",
                        "source_path": "agents/coder",
                    }
                ],
            },
            id="not-restorable",
        ),
        pytest.param(
            _purging,
            "archive_entry_busy",
            lambda entry_id: {
                "entry_id": entry_id,
                "state": "purging",
                "message": "it is being deleted permanently and will not become restorable",
            },
            id="purging",
        ),
        pytest.param(
            _stuck_restore,
            "archive_entry_busy",
            lambda entry_id: {
                "entry_id": entry_id,
                "state": "restoring",
                "message": "an interrupted restore left the files live at /data/agents/coder "
                "and cannot finish: invalid. Once that is resolved, or the folder is moved "
                "out of the data directory, the next start of vBot finishes or undoes the "
                "restore",
                "path": "/data/agents/coder",
                "problem": "invalid",
            },
            id="stuck-restore",
        ),
    ],
)
async def test_restore_errors_carry_the_blockers_as_data(
    tmp_path: Path, arrange: Any, code: str, data: Any
) -> None:
    state, entries = await _archived(tmp_path)
    arrange(tmp_path / "data", state, entries["agent"])

    error = await rpc_error(state, "archive.restore", entry_id=entries["agent"])

    assert (error["code"], error["data"]) == (code, data(entries["agent"]))
    assert not state.runtime.agents.exists("coder")


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["archive.show", "archive.restore", "archive.purge"])
async def test_an_unknown_entry_is_archive_entry_not_found(tmp_path: Path, method: str) -> None:
    state, entries = await _archived(tmp_path)
    params: dict[str, Any] = (
        {"entry_ids": [entries["agent"], "arc_missing"]}
        if method == "archive.purge"
        else {"entry_id": "arc_missing"}
    )

    error = await rpc_error(state, method, **params)

    assert (error["code"], error["data"]) == (
        "archive_entry_not_found",
        {"entry_ids": ["arc_missing"]},
    )
    # A refused purge deletes nothing, not even the entries that exist.
    assert state.runtime.sessions.archive_ledger.entry(entries["agent"]) is not None


@pytest.mark.asyncio
async def test_restore_waits_for_the_automation_reference_lock(tmp_path: Path) -> None:
    state, entries = await _archived(tmp_path)

    async with state.agent_delete_lock:
        restore = asyncio.create_task(
            rpc_result(state, "archive.restore", entry_id=entries["agent"])
        )
        for _ in range(5):
            await asyncio.sleep(0)
        assert not restore.done()
        assert not state.runtime.agents.exists("coder")

    assert (await restore)["restored"] == {"agent_id": "coder", "private_agent_ids": []}


@pytest.mark.asyncio
async def test_purge_deletes_named_entries_or_all_matching_ones(tmp_path: Path) -> None:
    state, entries = await _archived(tmp_path)
    ledger = state.runtime.sessions.archive_ledger

    named = await rpc_result(state, "archive.purge", entry_ids=[entries["agent"]])
    matching = await rpc_result(state, "archive.purge", all=True, project_id="vbot")

    assert named == {
        "purged": [
            {
                "entry_id": entries["agent"],
                "kind": "agent",
                "subject_id": "coder",
                "session_count": 1,
            }
        ],
        "pending": [],
        "skipped": [],
        "gone": [],
        "kept": [],
    }
    assert [entry["entry_id"] for entry in matching["purged"]] == [entries["project"]]
    assert ledger.entry(entries["agent"]) is None
    assert ledger.entry(entries["project"]) is None
    assert ledger.entry(entries["session"]) is not None
    assert not (tmp_path / "data" / "archive" / "entries" / entries["agent"]).exists()


@pytest.mark.asyncio
async def test_a_purge_whose_usage_import_fails_leaves_every_entry_restorable(
    tmp_path: Path,
) -> None:
    def fail() -> None:
        raise OSError("usage ledger unavailable")

    state = _make_state(tmp_path)
    state.runtime.import_usage = fail
    state.runtime.agents.create("manager")
    state.runtime.agents.create("coder")
    entry_id = (await state.runtime.archive.archive_agent("coder")).entry_id

    result = await rpc_result(state, "archive.purge", entry_ids=[entry_id])

    assert result == {
        "purged": [],
        "pending": [],
        "skipped": [{"entry_id": entry_id, "reason": "usage_import_failed", "state": "archived"}],
        "gone": [],
        "kept": [],
    }
    shown = await rpc_result(state, "archive.show", entry_id=entry_id)
    assert (shown["entry"]["state"], shown["restore"]["possible"]) == ("archived", True)


def _taken_before_the_claim(
    state: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, taken_by: str
) -> None:
    """Let another operation restore or purge an entry just before a purge claims it."""
    ledger = state.runtime.sessions.archive_ledger
    begin_purge = ledger.begin_purge

    def taken_first(entry_id: str) -> Any:
        monkeypatch.setattr(ledger, "begin_purge", begin_purge)
        if taken_by == "restore":
            ledger.begin_restore(entry_id, {"target_id": None})
        else:
            entry = begin_purge(entry_id)
            while ledger.purge_next_session(entry.entry_key):
                pass
            ledger.finish_purge(entry.entry_key)
        return begin_purge(entry_id)

    monkeypatch.setattr(ledger, "begin_purge", taken_first)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("taken_by", "reason"), [("restore", "busy"), ("purge", "gone")], ids=["busy", "gone"]
)
async def test_a_permanent_delete_another_operation_preempts_still_succeeds_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, taken_by: str, reason: str
) -> None:
    state = _make_state(tmp_path)
    state.runtime.agents.create("manager")
    state.runtime.sessions.create("manager", session_id="notes")
    _taken_before_the_claim(state, monkeypatch, taken_by)

    result = await rpc_result(
        state, "session.delete", agent_id="manager", session_id="notes", permanent=True
    )

    # The Session was archived; the purge reports why it did not delete the entry.
    entry_id = result["archive_entry_id"]
    assert entry_id.startswith("arc_")
    assert (result["purged"], result["purge_pending"], result["purge_reason"]) == (
        False,
        False,
        reason,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        pytest.param({}, id="neither-form"),
        pytest.param({"all": True, "entry_ids": ["arc_x"]}, id="both-forms"),
        pytest.param({"entry_ids": []}, id="no-ids"),
        pytest.param({"entry_ids": [f"arc_{index}" for index in range(101)]}, id="too-many-ids"),
        pytest.param({"entry_ids": ["arc_x"], "kind": "agent"}, id="filter-without-all"),
    ],
)
async def test_purge_needs_exactly_one_form(tmp_path: Path, params: dict[str, Any]) -> None:
    state = _make_state(tmp_path)

    error = await rpc_error(state, "archive.purge", **params)

    assert error["code"] == "invalid_request"
