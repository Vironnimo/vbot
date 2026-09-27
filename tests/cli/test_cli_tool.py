"""Tests for the ``vbot tool`` catalog command."""

from __future__ import annotations

from tests.cli.cli_test_support import FakeRpc, RunCli


def test_tool_list_prints_each_public_tool(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "tool.list",
        {
            "tools": [
                {"name": "read_file", "description": "Read a file"},
                {"name": "edit_file", "description": "Edit a file"},
            ]
        },
    )

    code, out, _err = run_cli("tool", "list")

    assert code == 0
    assert rpc.calls == [("tool.list", {})]
    assert out.splitlines()[1:] == ["- read_file  Read a file", "- edit_file  Edit a file"]


def test_tool_list_fails_on_a_malformed_result(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("tool.list", {})

    code, _out, err = run_cli("tool", "list")

    assert code == 1
    assert err.strip()
