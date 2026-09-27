"""Tests for the ``vbot memory`` commands (pinned Memory)."""

from __future__ import annotations

from typing import Any

import pytest

from cli import memory_management
from cli.server_management import ServerInstance
from tests.cli.cli_test_support import FakeRpc, RunCli

AGENTS = {"agents": [{"id": "assistant"}, {"id": "coder"}]}


def memory_response(entry: dict[str, Any] | None = None) -> dict[str, Any]:
    scopes = {"agent": [{"id": 1, "scope": "agent", "content": "Keep answers short"}], "user": []}
    result: dict[str, Any] = {"agent_id": "assistant", "scopes": scopes}
    if entry is not None:
        result["entry"] = entry
    return result


def test_memory_list_prints_both_scopes(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("memory.list", memory_response())

    code, out, _err = run_cli("memory", "list", "assistant")

    assert code == 0
    assert rpc.calls == [("memory.list", {"agent_id": "assistant"})]
    assert out.splitlines() == [
        "pinned memory for assistant:",
        "agent scope:",
        "  #1: Keep answers short",
        "user scope:",
        "  (no entries)",
    ]


def test_memory_add_defaults_to_the_agent_scope_and_reports_the_entry(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply("memory.add", memory_response(entry={"id": 2, "scope": "agent", "content": "New"}))

    code, out, _err = run_cli("memory", "add", "assistant", "--content", "New")

    assert code == 0
    assert rpc.calls == [
        ("memory.add", {"agent_id": "assistant", "scope": "agent", "content": "New"})
    ]
    assert out.splitlines() == [
        "added memory entry in assistant (scope: agent)",
        "#2: New",
        "remaining entries: agent=1 user=0",
    ]


def test_memory_remove_requires_confirmation(rpc: FakeRpc, run_cli: RunCli) -> None:
    code, out, err = run_cli("memory", "remove", "assistant", "1")

    assert code == 1
    assert "--yes" in out + err
    assert rpc.calls == []


@pytest.mark.parametrize(
    ("command", "code", "lookup", "shown", "hidden"),
    [
        pytest.param(
            "list",
            "invalid_request",
            None,
            ["entry 99 does not exist"],
            ["available agents"],
            id="list-other-failure-needs-no-lookup",
        ),
        pytest.param(
            "list",
            "agent_not_found",
            ("agent.list", AGENTS),
            ["did you mean: assistant", "available agents: assistant, coder"],
            [],
            id="list-unknown-agent",
        ),
        pytest.param(
            "remove",
            "agent_not_found",
            ("agent.list", AGENTS),
            ["did you mean: assistant", "available agents: assistant, coder"],
            [],
            id="remove-unknown-agent",
        ),
        pytest.param(
            "remove",
            "not_found",
            ("memory.list", {"ok": True, "result": memory_response()}),
            ["entry 99 does not exist", "existing agent-scope entries: 1"],
            [],
            id="remove-unknown-entry",
        ),
        pytest.param(
            "remove",
            "domain_error",
            ("memory.list", {"ok": True, "result": {}}),
            ["entry 99 does not exist", "entry lookup"],
            ["has no agent-scope entries"],
            id="malformed-entry-lookup",
        ),
        pytest.param(
            "remove",
            "domain_error",
            ("memory.list", {"ok": False}),
            ["entry 99 does not exist", "entry lookup"],
            ["has no agent-scope entries"],
            id="failed-entry-lookup",
        ),
    ],
)
def test_memory_failure_lookup_follows_the_rpc_code_and_keeps_the_failure(
    rpc: FakeRpc,
    instance: ServerInstance,
    command: str,
    code: str,
    lookup: tuple[str, dict[str, Any]] | None,
    shown: list[str],
    hidden: list[str],
) -> None:
    # The structured code selects the lookup; the message wording is never parsed.
    rpc.fail(f"memory.{command}", code, "entry 99 does not exist")
    lookups = [] if lookup is None else [lookup[0]]
    if lookup is not None and lookup[0] == "agent.list":
        rpc.reply(*lookup)
    elif lookup is not None:
        rpc.respond(*lookup)

    if command == "list":
        result = memory_management.memory_list(instance, "assistnt")
    else:
        result = memory_management.memory_remove(instance, "assistnt", "agent", 99, True)

    assert result.ok is False
    assert rpc.methods == [f"memory.{command}", *lookups]
    assert result.failure is not None
    assert (result.failure.method, result.failure.code) == (f"memory.{command}", code)
    assert all(text in result.message for text in shown)
    assert not any(text in result.message for text in hidden)
