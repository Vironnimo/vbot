"""Tests for the ``vbot agent`` commands: option mapping, RPC requests and output."""

from __future__ import annotations

from typing import Any

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli


def agent_payload(agent_id: str = "coder") -> dict[str, Any]:
    return {
        "id": agent_id,
        "name": "Coder",
        "model": "openai/gpt-5.2",
        "fallback_models": ["anthropic/claude-sonnet-4"],
        "workspace": "C:/data/workspace-coder",
        "root_project_id": "vbot",
        "temperature": 0.4,
        "thinking_effort": "high",
        "memory_prompt_mode": "agent_user",
        "custom_system_prompt_enabled": False,
        "librarian_enabled": True,
        "tool_access": {"mode": "all"},
        "allowed_skills": ["debugging"],
        "excluded_skills": ["pdf"],
        "current_session_id": "session-one",
        "context_window": 256000,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-02T00:00:00+00:00",
    }


def test_agent_list_prints_one_row_per_agent(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("agent.list", {"agents": [agent_payload("writer"), agent_payload()]})

    code, out, _err = run_cli("agent", "list")

    assert code == 0
    assert rpc.calls == [("agent.list", {})]
    row = (
        "name=Coder model=openai/gpt-5.2 fallback_models=anthropic/claude-sonnet-4 "
        "temperature=0.4 thinking_effort=high current_session_id=session-one "
        "context_window=256000"
    )
    assert out.splitlines()[1:] == [f"- id=writer {row}", f"- id=coder {row}"]


def test_agent_show_prints_every_agent_field(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("agent.get", agent_payload())
    rpc.reply("agent.get", agent_payload("librarian") | {"builtin": "librarian"})

    code, out, _err = run_cli("agent", "show", "coder")

    assert code == 0
    assert rpc.calls == [("agent.get", {"id": "coder"})]
    assert out.splitlines()[1:] == [
        "id: coder",
        "name: Coder",
        "model: openai/gpt-5.2",
        "fallback_models: anthropic/claude-sonnet-4",
        "workspace: C:/data/workspace-coder",
        "project: vbot",
        "temperature: 0.4",
        "thinking_effort: high",
        "memory_prompt_mode: agent_user",
        "custom_system_prompt_enabled: no",
        "librarian_enabled: yes",
        'tool_access: {"mode":"all"}',
        "allowed_skills: debugging",
        "excluded_skills: pdf",
        "current_session_id: session-one",
        "context_window: 256000",
        "created_at: 2026-01-01T00:00:00+00:00",
        "updated_at: 2026-01-02T00:00:00+00:00",
    ]

    # The built-in Librarian says what it is and what can change.
    code, out, _err = run_cli("agent", "show", "librarian")

    assert out.splitlines()[1:4] == [
        "id: librarian",
        "name: Coder",
        "builtin: librarian (curates the skills of your other agents; only model, "
        "fallback_models, temperature and thinking_effort can change)",
    ]


def test_agent_create_sends_the_given_fields(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("agent.create", {"id": "writer"})

    code, out, _err = run_cli(
        "agent", "create", "writer", "Writer",
        "--model", "openai/gpt-5.2",
        "--tool-access-mode", "selected",
        "--tool-allow", "read_file",
        "--allowed-skills", "debugging",
    )  # fmt: skip

    assert code == 0
    assert rpc.calls == [
        (
            "agent.create",
            {
                "id": "writer",
                "name": "Writer",
                "model": "openai/gpt-5.2",
                "tool_access": {"mode": "selected", "allowed": ["read_file"]},
                "allowed_skills": ["debugging"],
            },
        )
    ]
    assert "writer" in out


LIBRARIAN = {
    "id": "librarian",
    "name": "Librarian",
    "fallback_models": [],
    "workspace": "C:/agents/librarian/workspace",
    "memory_prompt_mode": "full",
    "allowed_skills": [],
    "current_session_id": "session-1",
    "created_at": "now",
    "updated_at": "now",
}
MODEL_HINTS = ("vbot model list --task chat", "vbot agent update librarian --model <model-id>")


@pytest.mark.parametrize(
    ("saved", "shown", "hidden"),
    [
        pytest.param(
            {
                "model": "openai/gpt-5",
                "default_workspace": "C:/agents/librarian/workspace",
                "root_project_id": "second-brain",
                "temperature": 0.2,
                "thinking_effort": "high",
                "custom_system_prompt_enabled": True,
                "tool_access": {"mode": "selected", "allowed": ["read"]},
                "tools": {"subagent": {"allowed_agents": []}},
                "compaction_policy": None,
                "effective_compaction_policy": {"enabled": True},
                "effective": {"model": {"value": "openai/gpt-5", "source": "agent"}},
                "context_window": 128000,
            },
            (
                "workspace: C:/agents/librarian/workspace",
                "project: second-brain",
                'effective_sources: {"model":"agent"}',
            ),
            MODEL_HINTS[:1],
            id="saved-state",
        ),
        pytest.param(
            {
                "model": "",
                "root_project_id": None,
                "temperature": None,
                "thinking_effort": None,
                "custom_system_prompt_enabled": False,
                "tool_access": {"mode": "none"},
                "context_window": None,
            },
            MODEL_HINTS,
            (),
            id="no-effective-model",
        ),
    ],
)
def test_agent_create_confirms_the_saved_agent(
    rpc: FakeRpc,
    run_cli: RunCli,
    saved: dict[str, Any],
    shown: tuple[str, ...],
    hidden: tuple[str, ...],
) -> None:
    rpc.reply("agent.create", LIBRARIAN | saved)

    code, out, _err = run_cli("agent", "create", "librarian", "Librarian")

    assert code == 0
    assert rpc.calls == [("agent.create", {"id": "librarian", "name": "Librarian"})]
    for text in shown:
        assert text in out
    for text in hidden:
        assert text not in out


@pytest.mark.parametrize(
    ("options", "changes"),
    [
        pytest.param(
            (
                "--clear-temperature",
                "--thinking-effort", "none",
                "--tool-access-mode", "none",
                "--allowed-skills", "*",
                "--excluded-skills", "pdf",
                "--workspace", "C:/agents/coder",
                "--copy-workspace-files",
                "--project", "vbot",
            ),
            {
                "temperature": None,
                "thinking_effort": "none",
                "tool_access": {"mode": "none"},
                "allowed_skills": ["*"],
                "excluded_skills": ["pdf"],
                "workspace": "C:/agents/coder",
                "copy_workspace_identity_files": True,
                "root_project_id": "vbot",
            },
            id="values-nulls-and-lists",
        ),
        pytest.param(
            (
                "--clear-model",
                "--clear-fallback-models",
                "--subagent-allow", "reviewer", "librarian",
                "--compaction-policy", '{"enabled":false}',
                "--librarian", "false",
            ),
            {
                "model": "",
                "fallback_models": [],
                "tools": {"subagent": {"allowed_agents": ["reviewer", "librarian"]}},
                "compaction_policy": {"enabled": False},
                "librarian_enabled": False,
            },
            id="clears-delegation-and-policy",
        ),
        pytest.param(
            (
                "--name", "Coder Two",
                "--tool-access-mode", "selected",
                "--tool-allow", "read_file",
                "--tool-deny", "memory",
                "--default-workspace",
                "--copy-workspace-files",
                "--clear-project",
            ),
            {
                "name": "Coder Two",
                "tool_access": {"mode": "selected", "allowed": ["read_file"], "denied": ["memory"]},
                "workspace": None,
                "copy_workspace_identity_files": True,
                "root_project_id": None,
            },
            id="selected-tools-and-default-workspace",
        ),
        pytest.param(
            ("--tool-access-mode", "selected"),
            {"tool_access": {"mode": "selected", "allowed": []}},
            id="explicit-empty-selection",
        ),
    ],
)  # fmt: skip
def test_agent_update_sends_only_the_given_changes(
    rpc: FakeRpc, run_cli: RunCli, options: tuple[str, ...], changes: dict[str, Any]
) -> None:
    rpc.reply("agent.update", {"id": "coder"})

    code, out, _err = run_cli("agent", "update", "coder", *options)

    assert code == 0
    assert rpc.calls == [("agent.update", {"id": "coder", **changes})]
    assert "coder" in out


@pytest.mark.parametrize(
    ("options", "named_options"),
    [
        pytest.param(
            (),
            (
                "--name",
                "--model",
                "--fallback-models",
                "--temperature",
                "--thinking-effort",
                "--memory-prompt-mode",
                "--tool-access-mode",
                "--tool-allow",
                "--tool-deny",
                "--allowed-skills",
                "--excluded-skills",
                "--workspace",
                "--project",
                "--current-session-id",
            ),
            id="no-changes",
        ),
        pytest.param(
            ("--copy-workspace-files",),
            ("--copy-workspace-files", "--workspace", "--default-workspace"),
            id="copy-without-workspace-target",
        ),
        pytest.param(
            ("--tool-deny", "memory"),
            ("require --tool-access-mode",),
            id="tool-names-without-mode",
        ),
    ],
)
def test_agent_update_rejects_incomplete_changes_before_any_request(
    rpc: FakeRpc, run_cli: RunCli, options: tuple[str, ...], named_options: tuple[str, ...]
) -> None:
    code, out, _err = run_cli("agent", "update", "coder", *options)

    assert code == 1
    assert rpc.calls == []
    for option in named_options:
        assert option in out


@pytest.mark.parametrize(
    ("options", "params", "deleted", "shown"),
    [
        pytest.param(
            (),
            {"id": "writer"},
            {"archive_entry_id": "arc_7k2m9q4xw1ab", "purged": False},
            [
                "archived agent writer as archive entry arc_7k2m9q4xw1ab (12 sessions); "
                "restore with: vbot archive restore arc_7k2m9q4xw1ab"
            ],
            id="archived",
        ),
        pytest.param(
            ("--permanent", "--yes"),
            {"id": "writer", "permanent": True},
            {
                "archive_entry_id": "arc_7k2m9q4xw1ab",
                "purged": True,
                "external_workspace": "C:/notes/writer",
            },
            [
                "deleted agent writer permanently (12 sessions)",
                "external Workspace left in place: C:/notes/writer",
            ],
            id="permanent",
        ),
    ],
)
def test_agent_delete_archives_or_deletes_the_agent(
    rpc: FakeRpc,
    run_cli: RunCli,
    options: tuple[str, ...],
    params: dict[str, Any],
    deleted: dict[str, Any],
    shown: list[str],
) -> None:
    rpc.reply(
        "agent.delete",
        {
            "agent_id": "writer",
            "session_count": 12,
            "external_workspace": None,
            "purge_pending": False,
        }
        | deleted,
    )

    code, out, _err = run_cli("agent", "delete", "writer", *options)

    assert code == 0
    assert rpc.calls == [("agent.delete", params)]
    assert out.splitlines() == shown


def test_agent_rename_renames_the_agent(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("agent.rename", {"id": "researcher"})

    code, out, _err = run_cli("agent", "rename", "writer", "researcher")

    assert code == 0
    assert rpc.calls == [("agent.rename", {"id": "writer", "new_id": "researcher"})]
    assert "writer" in out and "researcher" in out


def test_agent_rename_to_the_same_id_sends_nothing(rpc: FakeRpc, run_cli: RunCli) -> None:
    code, out, _err = run_cli("agent", "rename", "writer", "writer")

    assert code == 1
    assert out.strip()
    assert rpc.calls == []


def test_agent_reorder_reads_the_revision_and_appends_unlisted_agents(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply(
        "agent.list",
        {
            "agents": [{"id": "assistant"}, {"id": "coder"}, {"id": "researcher"}],
            "order_revision": 4,
        },
    )
    rpc.reply("agent.reorder", {"agents": [], "order_revision": 5})

    code, out, _err = run_cli("agent", "reorder", "researcher", "assistant")

    assert code == 0
    assert rpc.params("agent.reorder") == {
        "agent_ids": ["researcher", "assistant", "coder"],
        "expected_revision": 4,
    }
    assert out.startswith("agent roster reordered (revision 5): ")
    assert out.rstrip().endswith("researcher, assistant, coder")


def test_agent_reorder_rejects_duplicate_ids_before_any_request(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    code, out, _err = run_cli("agent", "reorder", "assistant", "assistant")

    assert code == 1
    assert "duplicate" in out
    assert rpc.calls == []


def test_agent_reorder_suggests_a_close_match_for_an_unknown_id(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply(
        "agent.list", {"agents": [{"id": "assistant"}, {"id": "researcher"}], "order_revision": 2}
    )

    code, out, _err = run_cli("agent", "reorder", "assistent")

    assert code == 1
    assert rpc.methods == ["agent.list"]
    for text in (
        "unknown agent id: assistent",
        "did you mean: assistant",
        "available agents: assistant, researcher",
    ):
        assert text in out
