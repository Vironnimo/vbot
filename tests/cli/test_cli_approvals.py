"""Permission decisions use exact operator RPCs."""

from __future__ import annotations

import json

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
