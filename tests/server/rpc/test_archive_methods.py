"""archive.list/show/restore/purge: params, results, error codes with data, locks, events."""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.sessions import SessionAddress
from core.utils.timestamps import format_canonical_timestamp
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
    notes = runtime.sessions.create("manager", session_id="notes").address
    runtime.sessions.set_title(notes, "Notes")
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
async def test_list_pages_entries_newest_first_with_their_retention(tmp_path: Path) -> None:
    state, entries = await _archived(tmp_path)

    first = await rpc_result(state, "archive.list", limit=2)
    second = await rpc_result(state, "archive.list", limit=2, cursor=first["next_cursor"])

    assert [entry["entry_id"] for entry in first["entries"] + second["entries"]] == [
        entries["project"],
        entries["session"],
        entries["agent"],
    ]
    assert first["retention_days"] == 30
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
        "purge_at": _purge_at(state, entries["agent"]),
        "session_count": 1,
        "restorable": True,
        "not_restorable_reason": None,
    }
    assert first["entries"][1]["label"] == "Notes"


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
    assert [session["agent_id"] for session in result["sessions"]] == ["coder"]
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
        "restored": {"agent_id": "coder-2"},
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

    restored = await rpc_result(state, "archive.restore", entry_id=entries["session"])

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
            lambda entry_id: {"entry_id": entry_id, "state": "purging"},
            id="busy",
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

    assert (await restore)["restored"] == {"agent_id": "coder"}


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
    }
    assert [entry["entry_id"] for entry in matching["purged"]] == [entries["project"]]
    assert ledger.entry(entries["agent"]) is None
    assert ledger.entry(entries["project"]) is None
    assert ledger.entry(entries["session"]) is not None
    assert not (tmp_path / "data" / "archive" / "entries" / entries["agent"]).exists()


@pytest.mark.asyncio
async def test_a_purge_whose_usage_import_fails_reports_every_entry_pending(
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
        "pending": [{"entry_id": entry_id, "reason": "usage_import_failed"}],
    }
    assert state.runtime.sessions.archive_ledger.entry(entry_id) is not None


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
