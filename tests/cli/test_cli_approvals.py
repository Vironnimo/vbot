"""Permission decisions use exact operator RPCs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli


def _request(identifier: str) -> dict[str, object]:
    return {
        "id": identifier,
        "run_id": "run_example",
        "agent_id": "builder",
        "session_id": "ses_example",
        "tool_call_id": "call_1",
        "tool_name": "bash",
        "execution": {"computer_id": "server"},
        "cwd": "/work",
        "reason": "Read notes\x1b]0;untrusted\x07",
        "requirements": [{"kind": "path", "access": "read", "target": "/notes.txt"}],
        "arguments": {"command": "cat /notes.txt"},
    }


def test_pending_pages_and_exact_call_details(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "permissions.pending",
        {"revision": 3, "requests": [_request("apr_first"), _request("apr_second")]},
    )
    listed = run_cli("approvals", "list", "--limit", "1")
    shown = run_cli("approvals", "show", "apr_second", "--json")
    missing = run_cli("approvals", "show", "apr_stale", "--json")
    assert listed[0] == shown[0] == 0
    assert "apr_first" in listed[1] and "apr_second" not in listed[1]
    assert "--offset 1" in listed[1] and "\x1b" not in listed[1]
    assert json.loads(shown[1])["arguments"] == {"command": "cat /notes.txt"}
    assert missing[0] == 1 and json.loads(missing[1])["error"]["code"] == "approval_not_found"
    assert rpc.methods == ["permissions.pending"] * 3


@pytest.mark.parametrize("decision", ["allow", "reject"])
def test_decisions_target_one_request_and_refuse_inside_agent_runs(
    rpc: FakeRpc, run_cli: RunCli, monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    rpc.reply("permissions.resolve", {"resolved": True})
    answered = run_cli("approvals", decision, "apr_first", "--json")
    assert answered[0] == 0 and json.loads(answered[1])["decision"] == decision
    assert rpc.params("permissions.resolve") == {"id": "apr_first", "decision": decision}
    monkeypatch.setenv("VBOT_RUN_AGENT_ID", "builder")
    refused = run_cli("approvals", decision, "apr_first", "--json")
    assert refused[0] == 1 and json.loads(refused[1])["error"]["code"] == "user_decision_required"
    assert rpc.methods == ["permissions.resolve"]


def test_stale_server_decision_is_not_replayed(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.fail(
        "permissions.resolve", "approval_not_found", "Request is no longer pending", status=200
    )
    code, out, _err = run_cli("approvals", "allow", "apr_stale", "--json")
    assert code == 1 and json.loads(out)["error"]["code"] == "approval_not_found"
    assert rpc.methods == ["permissions.resolve"]


def test_permission_profiles_read_pages_and_write_an_exact_name(
    tmp_path: Path, rpc: FakeRpc, run_cli: RunCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    rules = {"deny": ["Read(secret)"]}
    profiles = [
        {"name": "restricted", "rules": {}, "builtin": True},
        {"name": "Für Zürich", "rules": rules, "builtin": False},
    ]
    rpc.reply("permissions.profiles", {"profiles": profiles})
    rpc.reply("permissions.save_profile", {"profiles": profiles})
    rpc.reply("permissions.delete_profile", {"profiles": profiles[:1]})
    file = tmp_path / "rules.json"
    file.write_text(json.dumps(rules), encoding="utf-16")
    listed = run_cli("permission-profiles", "list", "--limit", "1", "--offset", "1", "--json")
    shown = run_cli("permission-profiles", "show", "Für Zürich", "--json")
    missing = run_cli("permission-profiles", "show", "Zürich", "--json")
    saved = run_cli("permission-profiles", "save", "Für Zürich", "--file", str(file), "--json")
    deleted = run_cli("permission-profiles", "delete", "Für Zürich", "--json")
    assert listed[0] == shown[0] == saved[0] == deleted[0] == 0
    assert json.loads(listed[1]) == {"total": 2, "profiles": profiles[1:]}
    assert json.loads(shown[1]) == json.loads(saved[1]) == profiles[1]
    assert json.loads(deleted[1]) == {"name": "Für Zürich", "deleted": True}
    assert missing[0] == 1 and json.loads(missing[1])["error"]["code"] == "profile_not_found"
    monkeypatch.setenv("VBOT_RUN_AGENT_ID", "builder")
    rpc.reply("agent.update", {})
    rpc.reply("project.set_override", {"project": {}, "scan": {}})
    assert run_cli("permission-profiles", "delete", "Für Zürich")[0] == 0
    assert run_cli("agent", "update", "builder", "--permission-profile", "full_access")[0] == 0
    assert (
        run_cli(
            "project", "override", "set", "repo", "builder", "permission_profile", "full_access"
        )[0]
        == 0
    )
    assert rpc.calls == [
        ("permissions.profiles", {}),
        ("permissions.profiles", {}),
        ("permissions.profiles", {}),
        ("permissions.save_profile", {"name": "Für Zürich", "permission_rules": rules}),
        ("permissions.delete_profile", {"name": "Für Zürich"}),
        ("permissions.delete_profile", {"name": "Für Zürich"}),
        ("agent.update", {"id": "builder", "permission_profile": "full_access"}),
        (
            "project.set_override",
            {
                "project_id": "repo",
                "agent_id": "builder",
                "field": "permission_profile",
                "value": "full_access",
            },
        ),
    ]
