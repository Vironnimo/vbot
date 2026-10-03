"""Tests for the ``vbot librarian`` commands."""

from __future__ import annotations

from typing import Any, cast

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli

_LAST_PASS = {
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
}
_EARLIER_PASS = {
    **_LAST_PASS,
    "finished_at": "2026-09-23T10:01:00Z",
    "trigger": "manual",
    "archived": 0,
    "consolidation": "unchanged",
    "session_id": None,
    "run_id": None,
}
_HINT_SESSION = (
    "read a pass's session in the WebUI under Settings > Memory > Skill maintenance, or ask "
    'the Librarian about it with: vbot chat --agent librarian --session <session-id> "<message>"'
)
_STATUS = {
    "agent_id": "assistant",
    "settings": {
        "enabled": True,
        "interval_days": 7,
        "archive_after_days": 90,
        "consolidate": True,
    },
    "available": True,
    "unscheduled_reason": None,
    "librarian_problem": None,
    "running": False,
    "running_since": None,
    "last_pass": _LAST_PASS,
    "next_due_at": "2026-10-07T10:02:00Z",
    "passes": [],
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


def test_librarian_status_reports_the_passes_and_how_to_undo_the_last_one(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    failed = {**_LAST_PASS, "outcome": "failed", "consolidation": "failed"}
    rpc.reply("librarian.status", {**_STATUS, "passes": [_LAST_PASS, _EARLIER_PASS]})
    rpc.reply("librarian.status", {**_STATUS, "last_pass": failed, "changes": []})

    code, out, _err = run_cli("librarian", "status", "assistant")

    assert code == 0
    assert rpc.calls == [("librarian.status", {"agent_id": "assistant"})]
    assert out.splitlines() == [
        "Librarian of assistant",
        "scheduled passes: every 7 days; skills made in the background are archived after "
        "90 days unused; merging overlapping skills: on",
        "next scheduled pass: 2026-10-07T10:02:00Z (when the agent is idle)",
        "last pass: 2026-09-30T10:02:00Z (scheduled)",
        "  archived 1 unused skills",
        "  merge: merged and fixed overlapping skills (3 skills it may change)",
        "  created 0, changed 1, merged away 1",
        "  session lib-1, run r-9",
        "earlier passes, newest first:",
        "- 2026-09-23T10:01:00Z; started by hand; archived 0; merge: skipped, no skill it may "
        "change has changed since the last merge",
        "changes of the last pass: 2 revisions, newest first",
        "revision 9  2026-09-30T10:01:30Z  deploy-api  archived (merged into deploy-web) "
        "by librarian (session lib-1, run r-9)",
        "revision 7  2026-09-30T10:00:01Z  old-fix  archived (retired after long disuse) "
        "by librarian",
        "undo them together with: vbot skill revert 9 7 --scope agent:assistant",
        _HINT_SESSION,
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
            "no_skills",
            "not scheduled: the agent has no skills of its own, so the Librarian has nothing to "
            "curate",
        ),
        (
            True,
            "librarian_unavailable",
            "not scheduled: the Librarian is unavailable: one of your agents uses its id "
            "librarian; rename that agent with: vbot agent rename librarian <new-id>, then "
            "restart vBot to create the Librarian",
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
    settings["consolidate"] = False
    problem = "agent_id_taken" if reason == "librarian_unavailable" else None
    rpc.reply(
        "librarian.status",
        {
            **_STATUS,
            "settings": settings,
            "unscheduled_reason": reason,
            "librarian_problem": problem,
            "next_due_at": None,
        },
    )

    code, out, _err = run_cli("librarian", "status", "assistant")

    assert code == 0
    assert out.splitlines()[:3] == [
        "Librarian of assistant",
        line,
        *(
            [
                "scheduled passes: every 7 days; skills made in the background are archived "
                "after 90 days unused; merging overlapping skills: off"
            ]
            if enabled
            else ["last pass: 2026-09-30T10:02:00Z (scheduled)"]
        ),
    ]


def test_librarian_status_without_an_agent_lists_the_recent_passes_of_all_agents(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    overview = {
        "agent_id": "librarian",
        "available": True,
        "problem": None,
        "settings": _STATUS["settings"],
        "running": "coder",
        "passes": [
            {**_LAST_PASS, "agent_id": "assistant", "agent_name": "Assistant"},
            {**_EARLIER_PASS, "agent_id": "coder", "agent_name": "coder"},
        ],
    }
    rpc.reply("librarian.overview", overview)
    rpc.reply(
        "librarian.overview",
        {
            **overview,
            "available": False,
            "problem": "invalid_config",
            "settings": {**cast(dict[str, Any], _STATUS["settings"]), "enabled": False},
            "running": None,
            "passes": [],
        },
    )

    code, out, _err = run_cli("librarian", "status")

    assert code == 0
    assert rpc.calls == [("librarian.overview", {})]
    model_hint = (
        "the Librarian is an agent with its own model; change it with: vbot agent update "
        "librarian --model <provider/model> --thinking-effort <level>"
    )
    assert out.splitlines() == [
        "Librarian (agent librarian)",
        "scheduled passes: every 7 days; skills made in the background are archived after "
        "90 days unused; merging overlapping skills: on",
        "a pass of coder is running",
        "recent passes, newest first:",
        "- 2026-09-30T10:02:00Z; Assistant (assistant); scheduled; archived 1; merge: merged "
        "and fixed overlapping skills; created 0, changed 1, merged away 1; session lib-1",
        "- 2026-09-23T10:01:00Z; coder; started by hand; archived 0; merge: skipped, no skill "
        "it may change has changed since the last merge",
        "see an agent's passes and undo their changes with: vbot librarian status <agent-id>",
        _HINT_SESSION,
        model_hint,
    ]

    # An unavailable Librarian says why and how to fix it.
    code, out, _err = run_cli("librarian", "status")

    assert out.splitlines() == [
        "Librarian (agent librarian)",
        "unavailable: agents/librarian/agent.json cannot be loaded; vbot doctor config names "
        "the problem; fix the file and restart vBot",
        "scheduled passes: off (librarian.enabled); start one with: vbot librarian run <agent-id>",
        "no pass has run yet",
        "see an agent's passes and undo their changes with: vbot librarian status <agent-id>",
        model_hint,
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
