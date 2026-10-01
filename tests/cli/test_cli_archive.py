"""Tests for ``vbot archive`` and permanent deletes: RPC requests, printed output, exit codes."""

from __future__ import annotations

from typing import Any

import pytest

from cli import main
from tests.cli.cli_test_support import FakeRpc, RunCli

AGENT_ENTRY = {
    "entry_id": "arc_7k2m9q4xw1ab",
    "kind": "agent",
    "state": "archived",
    "subject_id": "coder",
    "project_id": None,
    "agent_id": "coder",
    "owner_name": None,
    "label": "Coder",
    "archived_at": "2026-09-30T14:02:11.120000Z",
    "origin": "operation",
    "purge_at": "2026-10-30T14:02:11.120000Z",
    "session_count": 12,
    "restorable": True,
    "not_restorable_reason": None,
    "may_hold_user_folders": False,
}
# Found at a start without its record; it holds a Workspace an older vBot moved.
RECOVERED_ENTRY = AGENT_ENTRY | {
    "entry_id": "arc_legacy01",
    "subject_id": "writer",
    "agent_id": "writer",
    "label": "Writer",
    "origin": "recovered",
    "purge_at": None,
    "session_count": 0,
    "may_hold_user_folders": True,
}
GROUP_ENTRY = {
    "entry_id": "arc_h2q9c4m7r1sd",
    "kind": "owner_group",
    "state": "purging",
    "subject_id": "swm_k2",
    "project_id": "vbot",
    "agent_id": "builder",
    "owner_name": "swarm",
    "label": "Docs swarm",
    "archived_at": "2026-09-29T08:00:00.000000Z",
    "origin": "operation",
    "purge_at": None,
    "session_count": 6,
    "restorable": False,
    "not_restorable_reason": "kind_not_restorable",
    "may_hold_user_folders": False,
}
CURSOR = {"archived_at": "2026-09-29T08:00:00.000000Z", "entry_id": "arc_h2q9c4m7r1sd"}


def test_archive_list_prints_each_entry_and_the_next_page(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "archive.list",
        {
            "entries": [AGENT_ENTRY, RECOVERED_ENTRY, GROUP_ENTRY],
            "next_cursor": CURSOR,
            "retention_days": 30,
        },
    )

    code, out, _err = run_cli("archive", "list", "--project", "vbot")

    assert code == 0
    assert rpc.calls == [("archive.list", {"project_id": "vbot", "limit": 50})]
    assert out.splitlines() == [
        "archive entries (automatic deletion after 30 days):",
        '- id=arc_7k2m9q4xw1ab kind=agent subject=coder label="Coder" sessions=12 '
        "archived_at=2026-09-30T14:02:11.120000Z purge_at=2026-10-30T14:02:11.120000Z "
        "restorable=yes",
        '- id=arc_legacy01 kind=agent subject=writer label="Writer" sessions=0 '
        "archived_at=2026-09-30T14:02:11.120000Z origin=recovered purge_at=- user_folders=yes "
        "restorable=yes",
        "- id=arc_h2q9c4m7r1sd kind=owner_group subject=swm_k2 agent=builder project=vbot "
        'owner=swarm label="Docs swarm" sessions=6 state=purging '
        "archived_at=2026-09-29T08:00:00.000000Z purge_at=- restorable=no "
        "reason=kind_not_restorable",
        "next page: vbot archive list --project vbot --cursor "
        '\'{"archived_at":"2026-09-29T08:00:00.000000Z","entry_id":"arc_h2q9c4m7r1sd"}\'',
    ]


@pytest.mark.parametrize(
    ("tree", "shown"),
    [
        pytest.param(
            {"role": "workspace", "path": "archive/agents/coder/workspace"},
            "- workspace: archive/agents/coder/workspace (from C:/notes/coder) "
            "(may be your own folder; a restore brings it back, a purge deletes it)",
            id="moved-workspace",
        ),
        pytest.param(
            {"role": "files", "path": "archive/old-notes"},
            "- files: archive/old-notes (from C:/notes/coder) "
            "(may be your own folder; a purge deletes it)",
            id="older-files",
        ),
    ],
)
def test_archive_show_marks_folders_that_may_be_the_users_own(
    rpc: FakeRpc, run_cli: RunCli, tree: dict[str, Any], shown: str
) -> None:
    rpc.reply(
        "archive.show",
        {
            "entry": AGENT_ENTRY,
            "sessions": [],
            "session_count": 0,
            "files": {
                "state": "present",
                "trees": [tree | {"source_path": "C:/notes/coder", "user_folder": True}],
            },
            "details": {},
            "restore": {"possible": True, "blockers": [], "warnings": []},
        },
    )

    code, out, _err = run_cli("archive", "show", "arc_7k2m9q4xw1ab")

    assert code == 0
    assert shown in out.splitlines()


def test_archive_list_says_when_the_retention_period_cannot_be_read(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply(
        "archive.list",
        {
            "entries": [AGENT_ENTRY | {"purge_at": None}],
            "next_cursor": None,
            "retention_days": None,
            "retention_unknown": True,
        },
    )

    code, out, err = run_cli("archive", "list")

    assert code == 0
    # Not "automatic deletion off": the period is unknown, and nothing is deleted meanwhile.
    assert out.splitlines()[0] == (
        "archive entries (automatic deletion paused: the retention period cannot be read):"
    )
    assert (
        "vBot cannot read the archive.retention_days setting, so it deletes no archive entry "
        "automatically until the setting can be read; 'vbot doctor settings' shows the "
        "problem in settings.json"
    ) in err


def test_archive_list_all_follows_every_page(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "archive.list", {"entries": [AGENT_ENTRY], "next_cursor": CURSOR, "retention_days": None}
    )
    rpc.reply(
        "archive.list", {"entries": [GROUP_ENTRY], "next_cursor": None, "retention_days": None}
    )

    code, out, _err = run_cli("archive", "list", "--kind", "agent", "--all")

    assert code == 0
    assert rpc.calls == [
        ("archive.list", {"kind": "agent", "limit": 50}),
        ("archive.list", {"kind": "agent", "limit": 50, "cursor": CURSOR}),
    ]
    lines = out.splitlines()
    assert lines[0] == "archive entries (automatic deletion off):"
    assert [line.split()[1] for line in lines[1:]] == [
        "id=arc_7k2m9q4xw1ab",
        "id=arc_h2q9c4m7r1sd",
    ]


def test_an_empty_archive_says_so(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("archive.list", {"entries": [], "next_cursor": None, "retention_days": 30})

    code, out, _err = run_cli("archive", "list")

    assert code == 0
    assert out == "no archive entries\n"


def test_archive_show_prints_facts_sessions_files_and_the_restore_check(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply(
        "archive.show",
        {
            "entry": AGENT_ENTRY,
            "sessions": [
                {
                    "session_id": "ses_review",
                    "project_id": None,
                    "agent_id": "coder",
                    "title": "Draft review",
                    "created_at": "2026-09-01T10:00:00.000000Z",
                    "last_activity_at": "2026-09-30T13:00:00.000000Z",
                }
            ],
            "session_count": 12,
            "files": {
                "state": "present",
                "trees": [
                    {
                        "role": "agent",
                        "path": "archive/entries/arc_7k2m9q4xw1ab/agent",
                        "source_path": "agents/coder",
                        "user_folder": False,
                    }
                ],
            },
            "details": {
                "name": "Coder",
                "workspace": {"path": "C:/notes/coder", "external": True},
                "grants": ["planner", "orchestrator"],
            },
            "restore": {
                "possible": False,
                "target_id": "coder",
                "blockers": [
                    {
                        "code": "agent_id_taken",
                        "message": "an Agent with id coder exists",
                        "agent_id": "coder",
                    }
                ],
                "warnings": [],
            },
        },
    )

    code, out, _err = run_cli("archive", "show", "arc_7k2m9q4xw1ab")

    assert code == 0
    assert rpc.calls == [("archive.show", {"entry_id": "arc_7k2m9q4xw1ab"})]
    assert out.splitlines() == [
        "archive entry arc_7k2m9q4xw1ab",
        "kind: agent",
        'subject: coder ("Coder")',
        "state: archived",
        "archived_at: 2026-09-30T14:02:11.120000Z",
        "purge_at: 2026-10-30T14:02:11.120000Z",
        "sessions: 12 (showing 1)",
        '- id=ses_review title="Draft review" last_active_at=2026-09-30T13:00:00.000000Z',
        "files: present",
        "- agent: archive/entries/arc_7k2m9q4xw1ab/agent (from agents/coder)",
        "workspace: C:/notes/coder (outside the Agent's own directory, left in place)",
        "delegation grants: planner, orchestrator",
        "restore: blocked",
        "- agent_id_taken: an Agent with id coder exists; restore it under a new id with "
        "--as <new-agent-id>",
    ]


@pytest.mark.parametrize(
    ("options", "params", "restored", "shown"),
    [
        pytest.param(
            (),
            {"entry_id": "arc_7k2m9q4xw1ab"},
            {"kind": "agent", "restored": {"agent_id": "coder"}, "grant_agent_ids": []},
            "restored agent coder from archive entry arc_7k2m9q4xw1ab (12 sessions)",
            id="agent",
        ),
        pytest.param(
            ("--as", "coder-2"),
            {"entry_id": "arc_7k2m9q4xw1ab", "target_id": "coder-2"},
            {"kind": "agent", "restored": {"agent_id": "coder-2"}, "grant_agent_ids": ["planner"]},
            "restored agent coder as coder-2 from archive entry arc_7k2m9q4xw1ab "
            "(12 sessions; delegation grants: planner)",
            id="agent-as",
        ),
        pytest.param(
            (),
            {"entry_id": "arc_7k2m9q4xw1ab"},
            {
                "kind": "session",
                "restored": {"session_id": "coder", "agent_id": "builder", "project_id": "vbot"},
                "grant_agent_ids": [],
            },
            "restored session coder for builder@vbot from archive entry arc_7k2m9q4xw1ab",
            id="session",
        ),
    ],
)
def test_archive_restore_names_what_came_back(
    rpc: FakeRpc,
    run_cli: RunCli,
    options: tuple[str, ...],
    params: dict[str, Any],
    restored: dict[str, Any],
    shown: str,
) -> None:
    rpc.reply(
        "archive.restore",
        {
            "entry_id": "arc_7k2m9q4xw1ab",
            "subject_id": "coder",
            "session_count": 12,
            "warnings": [],
        }
        | restored,
    )

    code, out, _err = run_cli("archive", "restore", "arc_7k2m9q4xw1ab", *options)

    assert code == 0
    assert rpc.calls == [("archive.restore", params)]
    assert out.splitlines() == [shown]


def test_archive_restore_warnings_need_attention(rpc: FakeRpc, run_cli: RunCli) -> None:
    warning = "the Workspace folder C:/notes/coder is gone; the Agent uses its default Workspace"
    rpc.reply(
        "archive.restore",
        {
            "entry_id": "arc_7k2m9q4xw1ab",
            "kind": "agent",
            "subject_id": "coder",
            "restored": {"agent_id": "coder"},
            "session_count": 1,
            "grant_agent_ids": [],
            "warnings": [{"code": "external_workspace_missing", "message": warning}],
        },
    )

    code, out, err = run_cli("archive", "restore", "arc_7k2m9q4xw1ab")

    assert code == 0
    assert out.splitlines() == [
        "restored agent coder from archive entry arc_7k2m9q4xw1ab (1 session)",
        f"warning: {warning}",
    ]
    assert "[WARN]" in err and warning in err


@pytest.mark.parametrize(
    ("code", "data", "shown", "guidance"),
    [
        pytest.param(
            "archive_restore_conflict",
            {
                "entry_id": "arc_7k2m9q4xw1ab",
                "kind": "agent",
                "conflicts": [
                    {
                        "code": "agent_id_taken",
                        "id": "coder",
                        "message": "an Agent with id coder exists",
                    }
                ],
                "fix": "target_id",
            },
            [
                "archive_restore_conflict: cannot restore archive entry arc_7k2m9q4xw1ab: "
                "an Agent with id coder exists; restore it under a new id: "
                "vbot archive restore arc_7k2m9q4xw1ab --as <new-agent-id>"
            ],
            ["Next: vbot archive list"],
            id="conflict",
        ),
        pytest.param(
            "archive_not_restorable",
            {
                "entry_id": "arc_7k2m9q4xw1ab",
                "blockers": [
                    {
                        "code": "scope_missing",
                        "message": "Agent coder does not exist; restore it first",
                        "agent_id": "coder",
                        "entry_id": "arc_p0c1x8n3w5yt",
                    }
                ],
            },
            [
                "archive_not_restorable: cannot restore archive entry arc_7k2m9q4xw1ab:",
                "- scope_missing: Agent coder does not exist; restore it first "
                "(vbot archive restore arc_p0c1x8n3w5yt)",
            ],
            [
                "Resolve the blockers above, then restore the entry again.",
                "Next: vbot archive show arc_7k2m9q4xw1ab",
            ],
            id="not-restorable",
        ),
        pytest.param(
            "archive_not_restorable",
            {
                "entry_id": "arc_7k2m9q4xw1ab",
                "blockers": [
                    {"code": "older_format", "message": "an older vBot archived it"},
                    {"code": "scope_missing", "message": "Agent coder does not exist"},
                ],
            },
            [
                "archive_not_restorable: cannot restore archive entry arc_7k2m9q4xw1ab:",
                "- older_format: an older vBot archived it",
                "- scope_missing: Agent coder does not exist",
            ],
            [
                "This entry can never be restored (older_format). If it is no longer needed, "
                "'vbot archive purge arc_7k2m9q4xw1ab --yes' deletes it permanently.",
                "Next: vbot archive show arc_7k2m9q4xw1ab",
            ],
            id="never-restorable",
        ),
        pytest.param(
            "archive_entry_busy",
            {
                "entry_id": "arc_7k2m9q4xw1ab",
                "state": "restoring",
                "message": "an interrupted restore left the files live at ...",
                "path": "/data/agents/coder",
                "problem": "invalid",
            },
            ["archive_entry_busy: cannot restore archive entry arc_7k2m9q4xw1ab"],
            [
                "An interrupted restore of this entry cannot finish. Resolve the problem the "
                "message names, or move the folder it names out of the data directory, then "
                "restart the server ('vbot server restart'): its next start finishes or undoes "
                "the restore.",
                "Next: vbot archive show arc_7k2m9q4xw1ab",
            ],
            id="stuck-restore",
        ),
        pytest.param(
            "archive_entry_busy",
            {"entry_id": "arc_7k2m9q4xw1ab", "state": "purging", "message": "..."},
            ["archive_entry_busy: cannot restore archive entry arc_7k2m9q4xw1ab"],
            [
                "This entry is being deleted permanently and will not become restorable. "
                "vBot finishes the deletion in the background; "
                "'vbot archive purge arc_7k2m9q4xw1ab --yes' finishes it now.",
                "Next: vbot archive show arc_7k2m9q4xw1ab",
            ],
            id="purging",
        ),
        pytest.param(
            "archive_entry_busy",
            {"entry_id": "arc_7k2m9q4xw1ab", "state": "archiving"},
            ["archive_entry_busy: cannot restore archive entry arc_7k2m9q4xw1ab"],
            [
                "Another operation is using this archive entry. Retry after it finished.",
                "Next: vbot archive show arc_7k2m9q4xw1ab",
            ],
            id="busy",
        ),
    ],
)
def test_a_refused_restore_names_the_corrected_call(
    rpc: FakeRpc,
    run_cli: RunCli,
    code: str,
    data: dict[str, Any],
    shown: list[str],
    guidance: list[str],
) -> None:
    rpc.fail("archive.restore", code, "cannot restore archive entry arc_7k2m9q4xw1ab", data=data)

    exit_code, out, err = run_cli("archive", "restore", "arc_7k2m9q4xw1ab")

    assert exit_code == 1
    assert out.splitlines() == shown
    lines = err.splitlines()
    for line in guidance:
        assert any(item.startswith(line) for item in lines), (line, err)


@pytest.mark.parametrize(
    ("selection", "params", "kept"),
    [
        pytest.param(
            ("arc_7k2m9q4xw1ab", "arc_3d8n0v6tz2kc"),
            {"entry_ids": ["arc_7k2m9q4xw1ab", "arc_3d8n0v6tz2kc"]},
            [],
            id="ids",
        ),
        pytest.param(
            ("--all", "--kind", "agent", "--agent", "coder"),
            {"all": True, "kind": "agent", "agent_id": "coder"},
            [{"entry_id": "arc_legacy01", "reason": "user_folders"}],
            id="all",
        ),
    ],
)
def test_archive_purge_deletes_the_selected_entries(
    rpc: FakeRpc,
    run_cli: RunCli,
    selection: tuple[str, ...],
    params: dict[str, Any],
    kept: list[dict[str, str]],
) -> None:
    rpc.reply(
        "archive.purge",
        {
            "kept": kept,
            "purged": [
                {
                    "entry_id": "arc_7k2m9q4xw1ab",
                    "kind": "agent",
                    "subject_id": "coder",
                    "session_count": 12,
                },
                {
                    "entry_id": "arc_3d8n0v6tz2kc",
                    "kind": "session",
                    "subject_id": "ses_x4m2",
                    "session_count": 1,
                },
            ],
            "pending": [],
        },
    )

    code, out, err = run_cli("archive", "purge", *selection, "--yes")

    assert code == 0
    assert rpc.calls == [("archive.purge", params)]
    # A purge of the whole archive is one request; it may take as long as it needs.
    assert rpc.timeouts[0].read is None
    assert out.splitlines() == [
        "purged 2 archive entries (13 sessions)",
        "- id=arc_7k2m9q4xw1ab kind=agent subject=coder sessions=12",
        "- id=arc_3d8n0v6tz2kc kind=session subject=ses_x4m2 sessions=1",
        *(f"- id={entry['entry_id']} kept reason={entry['reason']}" for entry in kept),
    ]
    if kept:
        # --all never deletes folders the user may own; only naming the entry does.
        assert (
            "kept archive entry arc_legacy01, which may hold the user's own folders: purge --all "
            "never deletes such entries; to delete it as well, run "
            "'vbot archive purge arc_legacy01 --yes'"
        ) in err


def test_a_pending_purge_fails_and_names_the_call_that_continues_it(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply(
        "archive.purge",
        {"purged": [], "pending": [{"entry_id": "arc_7k2m9q4xw1ab", "reason": "PermissionError"}]},
    )

    code, out, err = run_cli("archive", "purge", "arc_7k2m9q4xw1ab", "--yes")

    assert code == 1
    assert out.splitlines() == [
        "purged no archive entries",
        "- id=arc_7k2m9q4xw1ab pending reason=PermissionError",
    ]
    assert (
        "deletion of arc_7k2m9q4xw1ab is pending (PermissionError); vBot retries it "
        "automatically, or run 'vbot archive purge arc_7k2m9q4xw1ab --yes' to continue it now"
    ) in err


@pytest.mark.parametrize(
    ("result", "code", "shown", "attention"),
    [
        pytest.param(
            {"gone": ["arc_7k2m9q4xw1ab"]},
            0,
            "- id=arc_7k2m9q4xw1ab gone (no longer in the archive: another operation deleted "
            "or restored it)",
            None,
            id="gone",
        ),
        pytest.param(
            {"skipped": [{"entry_id": "arc_7k2m9q4xw1ab", "reason": "busy", "state": "restoring"}]},
            1,
            "- id=arc_7k2m9q4xw1ab skipped reason=busy state=restoring",
            "arc_7k2m9q4xw1ab was not deleted: another operation, such as a restore, holds it "
            "(restoring); 'vbot archive show arc_7k2m9q4xw1ab' shows its state",
            id="busy",
        ),
    ],
)
def test_a_purge_names_entries_another_operation_took_first(
    rpc: FakeRpc,
    run_cli: RunCli,
    result: dict[str, Any],
    code: int,
    shown: str,
    attention: str | None,
) -> None:
    rpc.reply("archive.purge", {"purged": [], "pending": [], "skipped": [], "gone": []} | result)

    exit_code, out, err = run_cli("archive", "purge", "arc_7k2m9q4xw1ab", "--yes")

    assert exit_code == code
    assert out.splitlines() == ["purged no archive entries", shown]
    # Nothing retries an entry another operation holds or already removed.
    assert "retries" not in err
    if attention is not None:
        assert attention in err


@pytest.mark.parametrize(
    ("purge", "code", "clause", "attention"),
    [
        pytest.param(
            {"purge_reason": "gone"},
            0,
            "but another operation deleted or restored the entry before vBot could delete it "
            "permanently",
            "archive entry arc_7k2m9q4xw1ab is no longer in the archive; if another operation "
            "restored it, it is live again",
            id="gone",
        ),
        pytest.param(
            {"purge_reason": "busy"},
            1,
            "but it was not deleted permanently",
            "arc_7k2m9q4xw1ab was not deleted: another operation, such as a restore, holds it; "
            "'vbot archive show arc_7k2m9q4xw1ab' shows its state",
            id="busy",
        ),
        pytest.param(
            {"purge_reason": "usage_import_failed"},
            1,
            "but it was not deleted permanently",
            "arc_7k2m9q4xw1ab was not deleted (usage_import_failed) and stays in the archive "
            "unchanged; run 'vbot archive purge arc_7k2m9q4xw1ab --yes' to try again",
            id="unchanged",
        ),
    ],
)
def test_a_permanent_deletion_whose_purge_did_not_delete_the_entry_says_why(
    rpc: FakeRpc,
    run_cli: RunCli,
    purge: dict[str, Any],
    code: int,
    clause: str,
    attention: str,
) -> None:
    rpc.reply(
        "agent.delete",
        {
            "agent_id": "coder",
            "session_count": 12,
            "archive_entry_id": "arc_7k2m9q4xw1ab",
            "purged": False,
            "purge_pending": False,
        }
        | purge,
    )

    exit_code, out, err = run_cli("agent", "delete", "coder", "--permanent", "--yes")

    assert exit_code == code
    assert out.splitlines()[0] == (
        f"archived agent coder as archive entry arc_7k2m9q4xw1ab (12 sessions), {clause}"
    )
    assert attention in err
    assert "retries" not in err


@pytest.mark.parametrize(
    ("command", "corrected"),
    [
        pytest.param(
            ("archive", "purge", "--all", "--kind", "session"),
            "vbot archive purge --all --kind session --yes",
            id="archive-purge",
        ),
        pytest.param(
            ("agent", "delete", "coder", "--permanent"),
            "vbot agent delete coder --permanent --yes",
            id="agent-delete",
        ),
        pytest.param(
            ("project", "remove", "vbot", "--permanent"),
            "vbot project remove vbot --permanent --yes",
            id="project-remove",
        ),
        pytest.param(
            ("session", "delete", "coder", "ses_x4m2", "--permanent"),
            "vbot session delete coder ses_x4m2 --permanent --yes",
            id="session-delete",
        ),
    ],
)
def test_a_permanent_deletion_without_yes_sends_nothing_and_names_the_confirmed_call(
    rpc: FakeRpc, run_cli: RunCli, command: tuple[str, ...], corrected: str
) -> None:
    code, out, _err = run_cli(*command)

    assert code == 1
    assert rpc.calls == []
    assert out.startswith("refusing to delete ")
    assert f"re-run with --yes: {corrected}" in out


@pytest.mark.parametrize(
    ("command", "method", "result", "shown"),
    [
        pytest.param(
            ("agent", "delete", "coder"),
            "agent.delete",
            {"agent_id": "coder", "session_count": 12},
            "archived agent coder as archive entry arc_7k2m9q4xw1ab (12 sessions), "
            "but deleting it permanently did not finish",
            id="agent-delete",
        ),
        pytest.param(
            ("project", "remove", "vbot"),
            "project.rm",
            {"project_id": "vbot", "session_count": 3},
            "removed project vbot (archived as archive entry arc_7k2m9q4xw1ab, 3 sessions), "
            "but deleting it permanently did not finish",
            id="project-remove",
        ),
        pytest.param(
            ("session", "delete", "coder", "ses_x4m2"),
            "session.delete",
            {"agent_id": "coder", "session_id": "ses_x4m2", "next_session_id": "ses_y"},
            "deleted session ses_x4m2 for coder (archived as archive entry arc_7k2m9q4xw1ab), "
            "but deleting it permanently did not finish; next session: ses_y",
            id="session-delete",
        ),
    ],
)
def test_a_permanent_deletion_left_pending_fails_and_names_the_purge_that_finishes_it(
    rpc: FakeRpc,
    run_cli: RunCli,
    command: tuple[str, ...],
    method: str,
    result: dict[str, Any],
    shown: str,
) -> None:
    rpc.reply(
        method,
        result
        | {
            "archive_entry_id": "arc_7k2m9q4xw1ab",
            "purged": False,
            "purge_pending": True,
            "purge_reason": "stopped",
        },
    )

    code, out, err = run_cli(*command, "--permanent", "--yes")

    assert code == 1
    assert rpc.params(method)["permanent"] is True
    assert out.splitlines()[0] == shown
    assert (
        "deletion of arc_7k2m9q4xw1ab is pending (stopped); vBot retries it automatically, or "
        "run 'vbot archive purge arc_7k2m9q4xw1ab --yes' to continue it now"
    ) in err


@pytest.mark.parametrize(
    ("tokens", "reason"),
    [
        pytest.param(
            ("arc_7k2m9q4xw1ab", "--all"),
            "not both; to delete the named entries, run "
            "'vbot archive purge arc_7k2m9q4xw1ab --yes'",
            id="ids-and-all",
        ),
        pytest.param(
            (),
            "'vbot archive list' shows the entries and their ids, then run "
            "'vbot archive purge <entry-id>... --yes'",
            id="neither",
        ),
        pytest.param(
            ("--kind", "session"),
            "'vbot archive list --kind session' shows the matching entries",
            id="filters-only",
        ),
        pytest.param(
            ("arc_7k2m9q4xw1ab", "--kind", "agent"),
            "--kind only apply with --all",
            id="filters-without-all",
        ),
    ],
)
def test_archive_purge_takes_entry_ids_or_all(
    tokens: tuple[str, ...], reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as done:
        main.run(
            ["archive", "purge", *tokens, "--yes"],
            resolve=lambda **_target: pytest.fail("a selection error must not dispatch"),
        )

    assert done.value.code == 2
    error = capsys.readouterr().err
    assert "No command was executed." in error
    assert reason in error
    # Deleting a whole selection stays an explicit choice: no error offers an --all call.
    assert "purge --all" not in error
