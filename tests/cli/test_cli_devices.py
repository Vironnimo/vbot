"""Tests for the ``vbot devices`` commands: approval, listing and revocation over RPC."""

from __future__ import annotations

import pytest

from cli import device_management
from tests.cli.cli_test_support import FakeRpc, RunCli

_PENDING = {
    "code": "K7QM-4XPA",
    "name": "Anna's phone",
    "kind": "browser",
    "client": "203.0.113.7",
    "requested_at": "2026-10-17T10:00:00.000Z",
    "expires_in": 540,
}


@pytest.mark.parametrize(
    ("answer", "approved"), [pytest.param("y", True, id="yes"), pytest.param("", False, id="no")]
)
def test_approve_shows_the_waiting_device_before_approving_it(
    rpc: FakeRpc, run_cli: RunCli, monkeypatch: pytest.MonkeyPatch, answer: str, approved: bool
) -> None:
    prompts: list[str] = []
    monkeypatch.setattr(device_management.sys.stdin, "isatty", lambda: True)

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return answer

    monkeypatch.setattr("builtins.input", ask)
    rpc.reply("devices.pending", _PENDING).reply(
        "devices.approve", {"status": "approved", "name": "Anna's phone"}
    )

    code, out, _err = run_cli("devices", "approve", "k7qm4xpa")

    assert code == 0
    assert "Requested from: 203.0.113.7" in prompts[0] and "Kind: browser" in prompts[0]
    assert rpc.methods == (
        ["devices.pending", "devices.approve"] if approved else ["devices.pending"]
    )
    assert rpc.params("devices.pending") == {"code": "k7qm4xpa"}
    assert ("Approved Anna's phone" in out) is approved


def test_approve_needs_a_terminal_or_yes_and_never_runs_inside_an_agent_run(
    rpc: FakeRpc, run_cli: RunCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(device_management.sys.stdin, "isatty", lambda: False)
    rpc.reply("devices.pending", _PENDING).reply("devices.approve", {"name": "Anna's phone"})

    unconfirmed = run_cli("devices", "approve", "K7QM-4XPA")
    confirmed = run_cli("devices", "approve", "K7QM-4XPA", "--yes")
    monkeypatch.setenv("VBOT_RUN_AGENT_ID", "builder")
    from_agent = run_cli("devices", "approve", "K7QM-4XPA", "--yes")

    assert unconfirmed[0] == 1 and "--yes" in unconfirmed[1] + unconfirmed[2]
    assert confirmed[0] == 0
    assert from_agent[0] == 1 and "Agent's Run" in from_agent[1] + from_agent[2]
    assert rpc.methods == ["devices.pending", "devices.pending", "devices.approve"]


def test_list_rename_and_revoke_address_devices_by_id(rpc: FakeRpc, run_cli: RunCli) -> None:
    device = {
        "id": "dev_ABCDEFGHJKMNPQRS",
        "name": "Laptop",
        "kind": "cli",
        "created_at": "2026-10-01T10:00:00.000Z",
        "last_used_at": "2026-10-17T09:00:00.000Z",
        "last_client": "203.0.113.7",
        "approved_by": "host",
        "current": False,
    }
    rpc.reply("devices.list", {"devices": [device]})
    rpc.reply("devices.rename", {"device": {**device, "name": "Work laptop"}})
    rpc.reply("devices.revoke", {"revoked": [device["id"]]})

    listed = run_cli("devices", "list")
    renamed = run_cli("devices", "rename", device["id"], "Work laptop")
    revoked = run_cli("devices", "revoke", device["id"])

    assert listed[0] == renamed[0] == revoked[0] == 0
    assert "dev_ABCDEFGHJKMNPQRS" in listed[1] and "kind=CLI" in listed[1]
    assert "name=Work laptop" in renamed[1]
    assert "Revoked dev_ABCDEFGHJKMNPQRS" in revoked[1]
    assert rpc.params("devices.rename") == {"id": device["id"], "name": "Work laptop"}


def test_a_server_without_login_explains_where_pairing_exists(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.fail("devices.pending", "auth_disabled", "Pairing exists only with login", status=200)

    code, out, err = run_cli("devices", "approve", "K7QM-4XPA", "--yes")

    assert code == 1
    assert "auth_disabled" in out + err
    assert "vbot application access show" in out + err
