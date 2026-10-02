"""Tests for the ``vbot librarian`` commands."""

from __future__ import annotations

from typing import Any, cast

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli

_STATUS = {
    "agent_id": "assistant",
    "settings": {
        "enabled": True,
        "interval_days": 7,
        "archive_after_days": 90,
        "consolidate": True,
        "model": "",
    },
    "available": True,
    "unscheduled_reason": None,
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
    failed = {**cast(dict[str, Any], _STATUS["last_pass"]), "outcome": "failed"}
    failed["consolidation"] = "failed"
    rpc.reply("librarian.status", _STATUS)
    rpc.reply("librarian.status", {**_STATUS, "last_pass": failed, "changes": []})

    code, out, _err = run_cli("librarian", "status", "assistant")

    assert code == 0
    assert rpc.calls == [("librarian.status", {"agent_id": "assistant"})]
    assert out.splitlines() == [
        "Librarian of assistant",
        "scheduled passes: every 7 days; skills made in the background are archived after "
        "90 days unused; merging overlapping skills: on, with the agent's model",
        "next scheduled pass: 2026-10-07T10:02:00Z (when the agent is idle)",
        "last pass: 2026-09-30T10:02:00Z (scheduled)",
        "  archived 1 unused skills",
        "  merge: merged and fixed overlapping skills (3 skills it may change)",
        "  created 0, changed 1, merged away 1",
        "  session lib-1, run r-9",
        "changes of the last pass: 2 revisions, newest first",
        "revision 9  2026-09-30T10:01:30Z  deploy-api  archived (merged into deploy-web) "
        "by librarian (session lib-1, run r-9)",
        "revision 7  2026-09-30T10:00:01Z  old-fix  archived (retired after long disuse) "
        "by librarian",
        "undo them together with: vbot skill revert 9 7 --scope agent:assistant",
    ]

    # A pass that stopped early says so.
    code, out, _err = run_cli("librarian", "status", "assistant")

    assert out.splitlines()[3:6] == [
        "last pass: 2026-09-30T10:02:00Z (scheduled)",
        "  stopped early by an error",
        "  archived 1 unused skills",
    ]
    assert "  merge: did not finish (3 skills it may change)" in out.splitlines()


@pytest.mark.parametrize(
    ("enabled", "reason", "line"),
    [
        (
            True,
            "agent_disabled",
            "not scheduled: the Librarian is off for this agent, so no pass runs; turn it on "
            "with: vbot agent update assistant --librarian true",
        ),
        (
            True,
            "skill_tools_unavailable",
            "not scheduled: the agent cannot call skill and skill_manage, so no pass runs",
        ),
        (
            False,
            "schedule_disabled",
            "not scheduled: scheduled passes are off (librarian.enabled); a pass can still be "
            "started",
        ),
    ],
)
def test_librarian_status_names_why_an_agent_is_not_scheduled(
    rpc: FakeRpc, run_cli: RunCli, enabled: bool, reason: str, line: str
) -> None:
    settings = {**cast(dict[str, Any], _STATUS["settings"]), "enabled": enabled}
    settings["model"] = "openai/gpt-mini"
    rpc.reply(
        "librarian.status",
        {**_STATUS, "settings": settings, "unscheduled_reason": reason, "next_due_at": None},
    )

    code, out, _err = run_cli("librarian", "status", "assistant")

    assert code == 0
    assert out.splitlines()[:3] == [
        "Librarian of assistant",
        line,
        *(
            [
                "scheduled passes: every 7 days; skills made in the background are archived "
                "after 90 days unused; merging overlapping skills: on, with model openai/gpt-mini"
            ]
            if enabled
            else ["last pass: 2026-09-30T10:02:00Z (scheduled)"]
        ),
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
