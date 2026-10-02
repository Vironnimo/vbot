"""Tests for the ``vbot librarian`` commands."""

from __future__ import annotations

from tests.cli.cli_test_support import FakeRpc, RunCli

_STATUS = {
    "agent_id": "assistant",
    "settings": {
        "enabled": True,
        "interval_days": 7,
        "archive_after_days": 90,
        "consolidate": True,
    },
    "available": True,
    "running": False,
    "running_since": None,
    "last_pass": {
        "started_at": "2026-09-30T10:00:00Z",
        "finished_at": "2026-09-30T10:02:00Z",
        "trigger": "schedule",
        "archived": 1,
        "archived_revisions": [7],
        "candidates": 3,
        "consolidation": "ran",
        "session_id": "lib-1",
        "run_id": "r-9",
        "created": 0,
        "changed": 1,
        "merged": 1,
    },
    "next_due_at": "2026-10-07T10:02:00Z",
    "changes": [
        {
            "id": 9,
            "at": "2026-09-30T10:01:30Z",
            "skill": "deploy-api",
            "kind": "archive",
            "actor": "librarian",
            "files": [],
            "session_id": "lib-1",
            "run_id": "r-9",
            "reason": "absorbed",
            "absorbed_into": "deploy-web",
        },
        {
            "id": 7,
            "at": "2026-09-30T10:00:01Z",
            "skill": "old-fix",
            "kind": "archive",
            "actor": "librarian",
            "files": [],
            "reason": "inactive",
        },
    ],
}


def test_librarian_status_reports_the_last_pass_and_how_to_undo_it(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply("librarian.status", _STATUS)

    code, out, _err = run_cli("librarian", "status", "assistant")

    assert code == 0
    assert rpc.calls == [("librarian.status", {"agent_id": "assistant"})]
    assert out.splitlines() == [
        "Librarian of assistant",
        "scheduled passes: every 7 days; skills made in the background are archived after "
        "90 days unused; merging overlapping skills: on",
        "last pass: 2026-09-30T10:02:00Z (scheduled)",
        "  archived 1 unused skills",
        "  merge: merged and fixed overlapping skills with the agent's model "
        "(3 skills it may change)",
        "  created 0, changed 1, merged away 1",
        "  session lib-1, run r-9",
        "next scheduled pass: 2026-10-07T10:02:00Z (when the agent is idle)",
        "changes of the last pass: 2 revisions, newest first",
        "revision 9  2026-09-30T10:01:30Z  deploy-api  archived (merged into deploy-web) "
        "by librarian (session lib-1, run r-9)",
        "revision 7  2026-09-30T10:00:01Z  old-fix  archived (retired after long disuse) "
        "by librarian",
        "undo them together with: vbot skill revert 9 7 --scope agent:assistant",
    ]


def test_librarian_run_starts_a_pass(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("librarian.run", {**_STATUS, "running": True})

    code, out, _err = run_cli("librarian", "run", "assistant")

    assert code == 0
    assert rpc.calls == [("librarian.run", {"agent_id": "assistant"})]
    assert out.splitlines() == [
        "Librarian pass of assistant started",
        "see its result with: vbot librarian status assistant",
    ]
