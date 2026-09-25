"""Tests for the built-in memory tool, dispatched through the production executor."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.memory import MemoryService
from core.tools.memory import (
    _MAX_MEMORY_FAILURES_PER_RUN,
    MEMORY_TOOL_DESCRIPTION,
    MEMORY_TOOL_NAME,
    MEMORY_TOOL_PARAMETERS,
    _MemoryThrashTracker,
    memory_handler,
    register_memory_tool,
)
from core.tools.tools import (
    ToolCall,
    ToolContext,
    ToolExecutionConfig,
    ToolExecutor,
    ToolRegistry,
    is_tool_result_envelope,
)

JsonObject = dict[str, Any]

AGENT = (
    "- Project Atlas uses pytest with xdist.\n"
    "- The staging host is reachable only over the VPN.\n"
    "- Deploys run from the release branch.\n"
)
USER = "- User prefers concise answers.\n- User's name is Sam.\n"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "MEMORY.md").write_bytes(AGENT.encode("utf-8"))
    (root / "USER.md").write_bytes(USER.encode("utf-8"))
    return root


def files(workspace: Path) -> tuple[str, str]:
    return (
        (workspace / "MEMORY.md").read_bytes().decode("utf-8"),
        (workspace / "USER.md").read_bytes().decode("utf-8"),
    )


def run(workspace: Path, *calls: JsonObject) -> list[JsonObject]:
    """Dispatch calls as one Assistant turn through the production executor."""
    registry = ToolRegistry()
    register_memory_tool(registry, MemoryService())
    config = ToolExecutionConfig(
        agent_id="main",
        session_id="session-1",
        run_id="run-1",
        workspace=workspace,
        data_root=workspace.parent,
        vbot_root=workspace.parent,
        allowed_tools=[MEMORY_TOOL_NAME],
    )
    tool_calls = [
        ToolCall(id=f"call-{index}", name=MEMORY_TOOL_NAME, arguments=arguments)
        for index, arguments in enumerate(calls)
    ]
    results = asyncio.run(ToolExecutor(registry).execute_many(tool_calls, config))
    assert all(is_tool_result_envelope(result) for result in results)
    return results


def one(workspace: Path, arguments: JsonObject) -> JsonObject:
    return run(workspace, arguments)[0]


def content(result: JsonObject) -> str:
    assert result["ok"] is True, result
    return str(result["data"]["content"])


def failure(result: JsonObject, code: str) -> str:
    assert result["ok"] is False, result
    assert result["error"]["code"] == code, result
    assert "retryable" not in result["error"] or result["error"]["retryable"] is False
    return str(result["error"]["message"])


def test_registration_offers_text_addressing_and_orders_calls() -> None:
    registry = ToolRegistry()
    register_memory_tool(registry, MemoryService())

    tool = registry.get(MEMORY_TOOL_NAME)
    definition = registry.provider_definitions([MEMORY_TOOL_NAME])[0]
    parameters = definition["parameters"]

    assert tool.description == MEMORY_TOOL_DESCRIPTION
    assert tool.parameters == MEMORY_TOOL_PARAMETERS
    assert tool.parallel_safe is False
    assert set(parameters["properties"]) == {"action", "scope", "content", "old_text"}
    assert parameters["required"] == ["action"]
    assert "entry_id" not in str(definition)
    assert all(schema.get("description") for schema in parameters["properties"].values())
    listed = registry.display_for_call(
        MEMORY_TOOL_NAME,
        {"action": "list"},
        result={"ok": True, "data": {"count": 2, "content": ""}, "error": None, "artifacts": []},
    )
    added = registry.display_for_call(
        MEMORY_TOOL_NAME,
        {"action": "add", "scope": "user", "content": "private"},
        result={"ok": True, "data": {"content": "Added"}, "error": None, "artifacts": []},
    )
    assert listed["facts"] == [{"kind": "count", "value": 2, "unit": "results", "at_least": False}]
    assert added["facts"] == []
    assert "private" not in str(added)


def test_list_renders_scopes_like_the_memory_block(workspace: Path) -> None:
    both = one(workspace, {"action": "list"})
    agent = one(workspace, {"action": "list", "scope": "agent"})

    assert content(both) == (
        "# Agent Memory (121/4000 chars used)\n" + AGENT + "\n# User Profile (48/3000 chars used)\n"
        "- User prefers concise answers.\n- User's name is Sam."
    )
    assert both["data"]["count"] == 5
    assert content(agent) == "# Agent Memory (121/4000 chars used)\n" + AGENT.rstrip("\n")
    assert agent["data"]["count"] == 3


def test_add_confirms_compactly_and_writes_lf(workspace: Path) -> None:
    result = one(workspace, {"action": "add", "scope": "user", "content": "User works in UTC+2."})

    assert content(result) == "Added to user Memory (68/3000 chars used)."
    assert set(result["data"]) == {"content"}
    assert files(workspace) == (AGENT, USER + "- User works in UTC+2.\n")


def test_duplicate_add_changes_nothing(workspace: Path) -> None:
    result = one(
        workspace, {"action": "add", "scope": "user", "content": "User  prefers concise answers."}
    )

    assert content(result) == "User Memory already has this entry; nothing changed."
    assert files(workspace) == (AGENT, USER)


def test_replace_and_remove_address_entries_by_text(workspace: Path) -> None:
    replaced, removed = run(
        workspace,
        {
            "action": "replace",
            "scope": "agent",
            "old_text": "staging host",
            "content": "The staging host needs VPN and an SSH key.",
        },
        {"action": "remove", "scope": "agent", "old_text": "Deploys run from the release branch."},
    )

    assert content(replaced) == (
        "Replaced in agent Memory (115/4000 chars used). Previous text: "
        '"The staging host is reachable only over the VPN."'
    )
    assert content(removed) == (
        "Removed from agent Memory (79/4000 chars used). Previous text: "
        '"Deploys run from the release branch."'
    )
    assert files(workspace) == (
        "- Project Atlas uses pytest with xdist.\n- The staging host needs VPN and an SSH key.\n",
        USER,
    )


def test_replace_without_scope_uses_the_only_matching_scope(workspace: Path) -> None:
    result = one(
        workspace,
        {"action": "replace", "old_text": "concise answers", "content": "User wants code first."},
    )

    assert "user Memory" in content(result)
    assert files(workspace) == (AGENT, "- User wants code first.\n- User's name is Sam.\n")


def test_old_text_matching_both_scopes_changes_nothing(workspace: Path) -> None:
    message = failure(one(workspace, {"action": "remove", "old_text": "s"}), "memory_ambiguous")

    assert "both user and agent Memory" in message
    assert "Set scope" in message
    assert files(workspace) == (AGENT, USER)


@pytest.mark.parametrize(
    "old_text",
    [
        "User's name is Sam.",
        "- User's name is Sam.",
        "  User's   name is Sam.  ",
        "User’s name is Sam.",
        "user's NAME is sam",
        "name is Sam",
    ],
)
def test_copied_entry_text_variants_remove_the_same_entry(workspace: Path, old_text: str) -> None:
    content(one(workspace, {"action": "remove", "scope": "user", "old_text": old_text}))

    assert files(workspace) == (AGENT, "- User prefers concise answers.\n")


def test_exact_match_wins_over_loose_equivalents(workspace: Path) -> None:
    (workspace / "USER.md").write_bytes("- User's name is Sam.\n- User’s name is Sam.\n".encode())

    content(
        one(workspace, {"action": "remove", "scope": "user", "old_text": "User's name is Sam."})
    )
    assert files(workspace)[1] == "- User’s name is Sam.\n"

    content(one(workspace, {"action": "remove", "scope": "user", "old_text": "user's name"}))
    assert files(workspace)[1] == ""


def test_loose_whole_matches_of_different_entries_are_ambiguous(workspace: Path) -> None:
    (workspace / "USER.md").write_bytes(b"- Uses tabs.\n- uses TABS.\n")

    message = failure(
        one(workspace, {"action": "remove", "scope": "user", "old_text": "USES tabs."}),
        "memory_ambiguous",
    )

    assert "- Uses tabs.\n- uses TABS." in message
    assert files(workspace)[1] == "- Uses tabs.\n- uses TABS.\n"


def test_ambiguous_and_unmatched_text_show_what_can_be_addressed(workspace: Path) -> None:
    ambiguous = one(workspace, {"action": "remove", "scope": "agent", "old_text": "the"})
    unmatched = one(
        workspace,
        {"action": "replace", "scope": "agent", "old_text": "Postgres", "content": "Postgres 16."},
    )

    assert failure(ambiguous, "memory_ambiguous") == (
        'old_text "the" matches 2 agent Memory entries; nothing changed. Use a part that '
        "appears in only one of them:\n- The staging host is reachable only over the VPN.\n"
        "- Deploys run from the release branch."
    )
    assert failure(unmatched, "memory_no_match") == (
        'No agent Memory entry contains old_text "Postgres"; nothing changed. Copy old_text '
        "from a current entry:\n\n# Agent Memory (121/4000 chars used)\n" + AGENT.rstrip("\n")
    )
    assert files(workspace) == (AGENT, USER)


@pytest.mark.parametrize(
    "arguments",
    [
        {"action": "replace", "target": "memory", "old_text": "release", "new_text": "Main."},
        {
            "request": {
                "operation": "update",
                "scope": "agent",
                "old_string": "release",
                "text": "Main.",
            }
        },
        {"action": "Edit", "scope": "Agent", "match": "release", "content": "Main."},
        {"replace": {"scope": "MEMORY.md", "old_text": "release", "new_string": "Main."}},
    ],
)
def test_other_memory_dialects_replace_the_same_entry(
    workspace: Path, arguments: JsonObject
) -> None:
    content(one(workspace, arguments))

    assert files(workspace) == (
        AGENT.replace("Deploys run from the release branch.", "Main."),
        USER,
    )


def test_conflicting_replacement_spellings_fail_before_writing(workspace: Path) -> None:
    result = one(
        workspace,
        {
            "action": "replace",
            "scope": "agent",
            "old_text": "release",
            "content": "A",
            "new_text": "B",
        },
    )

    assert result["ok"] is False
    assert "content" in result["error"]["message"]
    assert files(workspace) == (AGENT, USER)


def test_remove_accepts_the_entry_text_as_content(workspace: Path) -> None:
    content(one(workspace, {"action": "remove", "scope": "user", "content": "User's name is Sam."}))

    assert files(workspace) == (AGENT, "- User prefers concise answers.\n")


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {"action": "remove", "scope": "agent", "old_text": "release", "content": "Main."},
            '{"action": "replace", "scope": "agent", "old_text": "release", "content": "Main."}',
        ),
        (
            {"action": "replace", "scope": "agent", "old_text": "release"},
            '{"action": "remove", "scope": "agent", "old_text": "release"}',
        ),
        ({"action": "replace", "scope": "agent", "content": "Main."}, "# Agent Memory"),
        ({"action": "add", "content": "User likes tea."}, 'add needs scope: "user"'),
        (
            {"action": "add", "scope": "agent", "old_text": "release", "content": "Main."},
            "use action replace",
        ),
    ],
)
def test_unclear_mutations_name_the_corrected_call_and_change_nothing(
    workspace: Path, arguments: JsonObject, expected: str
) -> None:
    message = failure(one(workspace, arguments), "invalid_arguments")

    assert expected in message
    assert files(workspace) == (AGENT, USER)


def test_entry_id_from_older_results_is_refused_with_the_entry_text(workspace: Path) -> None:
    positional = one(workspace, {"action": "remove", "scope": "agent", "entry_id": 2})
    missing = one(workspace, {"action": "remove", "scope": "agent", "entry_id": 9})
    unscoped = one(workspace, {"action": "remove", "entry_id": 2})

    assert failure(positional, "invalid_arguments") == (
        "Memory entries are addressed by text, not entry_id: positions change as entries change. "
        'Entry 2 of agent Memory currently reads "The staging host is reachable only over the '
        'VPN.". If that is the entry you mean, call memory with {"action": "remove", "scope": '
        '"agent", "old_text": "The staging host is reachable only over the VPN."}. Nothing changed.'
    )
    assert "entry 9 does not exist" in failure(missing, "invalid_arguments")
    assert "# User Profile" in failure(unscoped, "invalid_arguments")
    assert files(workspace) == (AGENT, USER)


def test_entry_id_agreeing_with_old_text_executes(workspace: Path) -> None:
    agreeing = one(
        workspace, {"action": "remove", "scope": "agent", "entry_id": 3, "old_text": "release"}
    )
    disagreeing = one(
        workspace, {"action": "remove", "scope": "agent", "entry_id": 1, "old_text": "VPN"}
    )

    content(agreeing)
    assert "Entry 1 of agent Memory" in failure(disagreeing, "invalid_arguments")
    assert files(workspace) == (AGENT.replace("- Deploys run from the release branch.\n", ""), USER)


def test_full_scope_names_the_excess_and_shows_entries(workspace: Path) -> None:
    big = "x" * 1990
    content(one(workspace, {"action": "add", "scope": "user", "content": big}))

    message = failure(
        one(workspace, {"action": "add", "scope": "user", "content": big + "y"}), "memory_full"
    )

    assert message.startswith(
        "User Memory would hold 4029/3000 characters; free at least 1029 characters first. "
        "Nothing changed."
    )
    assert "calls in one message run in order" in message
    assert "- User prefers concise answers." in message
    assert files(workspace)[1] == USER + f"- {big}\n"


def test_one_turn_can_free_space_and_then_add(workspace: Path) -> None:
    big = "y" * 1990
    content(one(workspace, {"action": "add", "scope": "user", "content": big}))

    removed, added = run(
        workspace,
        {"action": "remove", "scope": "user", "old_text": big},
        {"action": "add", "scope": "user", "content": "z" * 1990},
    )

    content(removed)
    content(added)
    assert files(workspace)[1] == USER + "- " + "z" * 1990 + "\n"


def test_shrinking_an_over_budget_scope_is_allowed(workspace: Path) -> None:
    (workspace / "USER.md").write_bytes((f"- {'a' * 1900}\n- {'b' * 1900}\n").encode())

    result = one(
        workspace, {"action": "replace", "scope": "user", "old_text": "a" * 50, "content": "short"}
    )

    content(result)
    assert files(workspace)[1] == f"- short\n- {'b' * 1900}\n"


def _context(workspace: Path, run_id: str = "run-1") -> ToolContext:
    return ToolContext(
        agent_id="main",
        session_id="session-1",
        run_id=run_id,
        tool_call_id="call-1",
        tool_name=MEMORY_TOOL_NAME,
        tool_call_index=0,
        workspace=workspace,
        vbot_root=workspace.parent,
        data_root=workspace.parent,
    )


def test_thrash_guard_cuts_off_repeated_mutation_failures(workspace: Path) -> None:
    service = MemoryService()
    tracker = _MemoryThrashTracker()
    failing = {"action": "remove", "scope": "agent", "old_text": "Postgres"}

    for _ in range(_MAX_MEMORY_FAILURES_PER_RUN):
        failure(memory_handler(_context(workspace), failing, service, tracker), "memory_no_match")

    error = memory_handler(_context(workspace), failing, service, tracker)["error"]
    assert error["code"] == "memory_error"
    assert error["retryable"] is False
    assert "Stop retrying" in error["message"]
    assert error["attempts_made"] == _MAX_MEMORY_FAILURES_PER_RUN + 1
    other_run = memory_handler(_context(workspace, "run-2"), failing, service, tracker)
    failure(other_run, "memory_no_match")


def test_thrash_guard_resets_on_successful_mutation(workspace: Path) -> None:
    service = MemoryService()
    tracker = _MemoryThrashTracker()
    failing = {"action": "remove", "scope": "agent", "old_text": "Postgres"}
    for _ in range(_MAX_MEMORY_FAILURES_PER_RUN):
        memory_handler(_context(workspace), failing, service, tracker)

    content(
        memory_handler(
            _context(workspace),
            {"action": "add", "scope": "agent", "content": "x"},
            service,
            tracker,
        )
    )

    failure(memory_handler(_context(workspace), failing, service, tracker), "memory_no_match")
