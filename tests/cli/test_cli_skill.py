"""Tests for the ``vbot skill`` commands: install, catalog, editing, sharing and recovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli

REMOTE_SKILL = "https://example.org/research.skill"
AGENTS = {"agents": [{"id": "assistant"}, {"id": "coder"}]}


def _run_as(monkeypatch: pytest.MonkeyPatch, agent_id: str, project_id: str | None) -> None:
    monkeypatch.setenv("VBOT_RUN_AGENT_ID", agent_id)
    monkeypatch.setenv("VBOT_RUN_SESSION_ID", "ses_test")
    if project_id is None:
        monkeypatch.delenv("VBOT_RUN_PROJECT_ID", raising=False)
    else:
        monkeypatch.setenv("VBOT_RUN_PROJECT_ID", project_id)


def test_skill_install_for_the_own_scope_targets_the_current_identity_agent(
    rpc: FakeRpc, run_cli: RunCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_as(monkeypatch, "builder", None)
    rpc.reply(
        "skill.install",
        {
            "operation": "installed",
            "name": "research",
            "source": REMOTE_SKILL,
            "files": 3,
            "package_path": ".",
            "sha256": "a" * 64,
            "warnings": [],
        },
    )

    code, _out, _err = run_cli(
        "skill", "install", REMOTE_SKILL, "--scope", "own",
        "--path", "skills/research", "--ref", "feature/docs",
    )  # fmt: skip

    assert code == 0
    assert rpc.calls == [
        (
            "skill.install",
            {
                "scope": "agent:builder",
                "source": REMOTE_SKILL,
                "replace": False,
                "dry_run": False,
                "path": "skills/research",
                "ref": "feature/docs",
            },
        )
    ]
    # Downloading and unpacking a package may exceed the default RPC timeout.
    assert rpc.timeouts[0].read is None


@pytest.mark.parametrize(
    ("run_context", "options"),
    [
        pytest.param(("builder", "project"), ("--scope", "own"), id="own-in-project-run"),
        pytest.param(("", ""), ("--scope", "own"), id="own-outside-identity-run"),
        pytest.param(None, ("--scope", "global", "--replace"), id="replace-without-yes"),
    ],
)
def test_skill_install_refuses_before_any_request(
    rpc: FakeRpc,
    run_cli: RunCli,
    monkeypatch: pytest.MonkeyPatch,
    run_context: tuple[str, str] | None,
    options: tuple[str, ...],
) -> None:
    # An own scope never falls back to the global scope.
    if run_context is not None:
        _run_as(monkeypatch, *run_context)

    code, _out, _err = run_cli("skill", "install", REMOTE_SKILL, *options)

    assert code == 1
    assert rpc.calls == []


def test_skill_install_resolves_a_local_relative_source_before_the_request(
    rpc: FakeRpc, run_cli: RunCli, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    rpc.reply(
        "skill.install",
        {"operation": "preview", "name": "research", "candidates": [], "warnings": []},
    )

    code, _out, _err = run_cli(
        "skill", "install", "./research.skill", "--scope", "global", "--dry-run"
    )

    assert code == 0
    assert rpc.params("skill.install")["source"] == str(tmp_path / "research.skill")
    assert rpc.params("skill.install")["dry_run"] is True
    assert not (tmp_path / "research.skill").exists()


@pytest.mark.parametrize(
    ("skills", "invalid", "expected"),
    [
        pytest.param(
            [
                {"name": "draft-email", "description": "Draft concise replies"},
                {"name": "release-notes", "description": "Write release notes"},
            ],
            [],
            ["- draft-email  Draft concise replies", "- release-notes  Write release notes"],
            id="skills",
        ),
        pytest.param(
            [
                {
                    "name": "native-build",
                    "description": "Build native projects",
                    "state": "unavailable",
                    "requirements": {
                        "missing": ["missing binary 'gcc'"],
                        "optional_missing": ["missing binary 'jq'"],
                    },
                }
            ],
            [],
            [
                "- native-build  Build native projects "
                "(unavailable: missing binary 'gcc'; optional missing: missing binary 'jq')"
            ],
            id="requirement-status",
        ),
    ],
)
def test_skill_list_prints_one_row_per_skill(
    rpc: FakeRpc,
    run_cli: RunCli,
    skills: list[dict[str, Any]],
    invalid: list[dict[str, Any]],
    expected: list[str],
) -> None:
    rpc.reply("skill.list", {"skills": skills, "invalid_skills": invalid})

    code, out, _err = run_cli("skill", "list")

    assert code == 0
    assert rpc.calls == [("skill.list", {})]
    assert out.splitlines()[1:] == expected


def test_skill_list_adds_an_invalid_skills_section(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "skill.list",
        {
            "skills": [{"name": "summarize", "description": "Summarize long text"}],
            "invalid_skills": [
                {
                    "name": "broken-skill",
                    "path": "C:/skills/broken-skill/SKILL.md",
                    "warnings": ["missing description"],
                }
            ],
        },
    )

    code, out, _err = run_cli("skill", "list")

    assert code == 0
    for text in (
        "skills:",
        "- summarize  Summarize long text",
        "invalid skills:",
        "- broken-skill (C:/skills/broken-skill/SKILL.md): missing description",
    ):
        assert text in out


def test_skill_list_reports_the_empty_catalog(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("skill.list", {"skills": [], "invalid_skills": []})

    code, out, _err = run_cli("skill", "list")

    assert code == 0
    assert out.strip()


def test_skill_list_reports_a_server_error(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.fail("skill.list", "rpc_error", "server exploded", status=500)

    code, out, _err = run_cli("skill", "list")

    assert code == 1
    assert "rpc_error: server exploded" in out


def test_skill_editable_scope_commands_send_their_requests(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "skill.read",
        {
            "skills": [
                {
                    "name": "librarian",
                    "description": "Maintain the catalog",
                    "content": "---\nname: librarian\n---\n",
                }
            ]
        },
    )
    operations = {
        "create": "created",
        "update": "updated",
        "write_file": "wrote_file",
        "remove_file": "removed_file",
        "delete": "deleted",
    }
    for method, operation in operations.items():
        result = {"name": "librarian", "operation": operation, "warnings": []}
        if method == "delete":
            result["archive_id"] = "librarian_01"
        rpc.reply(f"skill.{method}", result)
    scope = ("--scope", "agent:assistant")
    commands = {
        "read": ("read", *scope),
        "create": ("create", "librarian", "--content", "# Librarian", "--source", "cli", *scope),
        "update": ("update", "librarian", "--content", "# Updated", *scope),
        "write_file": (
            "write-file", "librarian", "references/schema.md", "--content", "# Schema", *scope,
        ),
        "remove_file": ("remove-file", "librarian", "references/schema.md", "--yes", *scope),
        "delete": ("delete", "librarian", "--yes", *scope),
    }  # fmt: skip

    outputs = {name: run_cli("skill", *argv) for name, argv in commands.items()}

    assert {name: code for name, (code, _out, _err) in outputs.items()} == dict.fromkeys(
        commands, 0
    )
    assert "agent:assistant" in outputs["read"][1]
    for method, operation in operations.items():
        assert operation in outputs[method][1] and "librarian" in outputs[method][1]
    # A delete names the archive entry and how to restore it.
    assert (
        "restore with: vbot skill restore librarian_01 --scope agent:assistant"
        in (outputs["delete"][1])
    )
    assert rpc.methods == [f"skill.{name}" for name in commands]


def test_skill_history_archive_and_pin_commands_send_their_requests(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    revision = {
        "id": 7,
        "at": "2026-09-30T08:00:00.000000Z",
        "skill": "librarian",
        "kind": "archive",
        "actor": "reflection",
        "reason": "absorbed",
        "absorbed_into": "catalog",
        "run_id": "run-1",
        "files": [{"path": "SKILL.md", "change": "deleted"}],
    }
    archived = {
        "archive_id": "librarian_01",
        "name": "librarian",
        "archived_at": "2026-09-30T08:00:00.000000Z",
        "reason": "deleted",
        "absorbed_into": None,
        "archived_by": "human",
    }
    rpc.reply("skill.history", {"scope": "global", "revisions": [revision]})
    rpc.reply(
        "skill.revert",
        {"scope": "global", "revisions": [{**revision, "id": 8, "kind": "revert", "reverts": [7]}]},
    )
    rpc.reply("skill.archived", {"scope": "global", "archived": [archived]})
    rpc.reply("skill.restore", {"name": "librarian", "operation": "restore", "warnings": []})
    rpc.reply("skill.purge", {"scope": "global", "purged": archived})
    rpc.reply("skill.set_pinned", {"name": "librarian", "operation": "pin", "warnings": []})
    scope = ("--scope", "global")
    commands = {
        "history": ("history", "librarian", "--limit", "5", *scope),
        "revert": ("revert", "7", *scope),
        "archived": ("archived", *scope),
        "restore": ("restore", "librarian_01", *scope),
        "purge": ("purge", "librarian_01", "--yes", *scope),
        "pin": ("pin", "librarian", *scope),
        "unpin": ("unpin", "librarian", *scope),
    }

    outputs = {name: run_cli("skill", *argv) for name, argv in commands.items()}

    assert {name: code for name, (code, _out, _err) in outputs.items()} == dict.fromkeys(
        commands, 0
    )
    assert rpc.calls == [
        ("skill.history", {"scope": "global", "limit": 5, "name": "librarian"}),
        ("skill.revert", {"scope": "global", "revisions": [7]}),
        ("skill.archived", {"scope": "global"}),
        ("skill.restore", {"scope": "global", "archive_id": "librarian_01"}),
        ("skill.purge", {"scope": "global", "archive_id": "librarian_01"}),
        ("skill.set_pinned", {"scope": "global", "name": "librarian", "pinned": True}),
        ("skill.set_pinned", {"scope": "global", "name": "librarian", "pinned": False}),
    ]
    assert (
        "revision 7  2026-09-30T08:00:00.000000Z  librarian  archived (merged into catalog) "
        "by reflection (run run-1)\n  deleted SKILL.md"
    ) in outputs["history"][1]
    assert "librarian  revert of revision 7 by reflection" in outputs["revert"][1]
    assert (
        "- librarian_01  librarian  archived 2026-09-30T08:00:00.000000Z (deleted) by human"
    ) in outputs["archived"][1]
    assert "permanently deleted archived skill librarian (librarian_01)" in outputs["purge"][1]


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(("delete", "librarian"), id="delete"),
        pytest.param(("remove-file", "librarian", "references/schema.md"), id="remove-file"),
        pytest.param(("purge", "librarian_01"), id="purge"),
    ],
)
def test_skill_destructive_commands_require_confirmation(
    rpc: FakeRpc, run_cli: RunCli, argv: tuple[str, ...]
) -> None:
    code, _out, _err = run_cli("skill", *argv, "--scope", "global")

    assert code == 1
    assert rpc.calls == []


def test_skill_read_prints_only_the_named_skill(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "skill.read",
        {
            "skills": [
                {"name": "wanted", "content": "wanted-content"},
                {"name": "other", "content": "excluded-content"},
            ]
        },
    )

    code, out, _err = run_cli("skill", "read", "wanted", "--scope", "global")
    missing_code, _missing_out, _err = run_cli("skill", "read", "absent", "--scope", "global")

    assert code == 0
    assert "wanted-content" in out and "excluded-content" not in out
    assert missing_code == 1


def test_skill_inspect_prints_the_exact_source_package(rpc: FakeRpc, run_cli: RunCli) -> None:
    content = "# Skill\n" + "complete instruction\n" * 80
    rpc.reply("skill.inspect", {"id": "source-package-id", "content": content})

    code, out, _err = run_cli("skill", "inspect", "source-package-id")

    assert code == 0
    assert rpc.calls == [("skill.inspect", {"id": "source-package-id"})]
    assert content in out and "source-package-id" in out


def test_skill_inventory_prints_every_source_stale_share_and_diagnostic(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply(
        "skill.inventory",
        {
            "skills": [
                {
                    "name": "librarian",
                    "description": "Maintain the catalog",
                    "origin": "agent",
                    "owner_id": "assistant",
                    "editable_scope": "agent:assistant",
                    "created_by": "reflection",
                    "pinned": True,
                    "uses": 3,
                    "last_used_at": "2026-09-30T08:00:00.000000Z",
                    "status": "available",
                    "shared_with": ["researcher"],
                    "missing": [],
                    "optional_missing": [],
                    "warnings": [],
                },
                {
                    "name": "native-build",
                    "description": "Build native projects",
                    "origin": "bundled",
                    "owner_id": None,
                    "status": "disabled",
                    "shared_with": [],
                    "missing": ["missing binary 'gcc'"],
                    "optional_missing": ["missing binary 'jq'"],
                    "warnings": ["duplicate name"],
                },
            ],
            "archived": [
                {
                    "scope": "global",
                    "archive_id": "notes_01",
                    "name": "notes",
                    "archived_at": "2026-09-29T08:00:00.000000Z",
                    "reason": "deleted",
                    "archived_by": "human",
                }
            ],
            "stale_shared": [{"agent_id": "ghost", "name": "gone"}],
            "policy_diagnostics": ["policy file warning"],
        },
    )

    code, out, _err = run_cli("skill", "inventory")

    assert code == 0
    assert rpc.calls == [("skill.inventory", {})]
    for text in (
        "- librarian  Maintain the catalog  [agent]",
        "status: available; owner: assistant; shared_with: researcher; created_by: reflection; "
        "pinned: yes; last_used: 2026-09-30T08:00:00.000000Z (3 sessions)",
        "- notes_01  notes  archived 2026-09-29T08:00:00.000000Z (deleted) by human; scope: global",
        "- native-build  Build native projects  [bundled]",
        "status: disabled; owner: -; shared_with: -; "
        "optional missing: missing binary 'jq'; warnings: duplicate name",
        "stale shared entries (owner or package no longer exists):",
        "- ghost: gone",
        "policy diagnostics:",
        "- policy file warning",
    ):
        assert text in out


def test_skill_inventory_reports_the_empty_state(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply("skill.inventory", {"skills": [], "stale_shared": [], "policy_diagnostics": []})

    code, out, _err = run_cli("skill", "inventory")

    assert code == 0
    assert out.splitlines() == ["no skills found in any source"]


@pytest.mark.parametrize(("command", "disabled"), [("disable", True), ("enable", False)])
def test_skill_disable_and_enable_toggle_the_policy(
    rpc: FakeRpc, run_cli: RunCli, command: str, disabled: bool
) -> None:
    rpc.reply("skill.set_disabled", {"name": "librarian"})

    code, out, _err = run_cli("skill", command, "librarian")

    assert code == 0
    assert rpc.calls == [("skill.set_disabled", {"name": "librarian", "disabled": disabled})]
    assert out.splitlines() == [f"{command}d skill librarian"]


def test_skill_share_and_unshare_send_the_share_state(rpc: FakeRpc, run_cli: RunCli) -> None:
    rpc.reply(
        "skill.share", {"agent_id": "assistant", "name": "librarian", "receivers": ["researcher"]}
    )

    shared = run_cli(
        "skill", "share", "assistant", "librarian", "--to", "researcher", "--to", "coder"
    )
    unshared = run_cli("skill", "unshare", "assistant", "librarian")

    assert (shared[0], unshared[0]) == (0, 0)
    assert shared[1].splitlines() == ["shared skill librarian from assistant to: researcher"]
    assert unshared[1].splitlines() == ["unshared skill librarian from assistant"]
    assert [params for _method, params in rpc.calls] == [
        {
            "agent_id": "assistant",
            "name": "librarian",
            "shared": True,
            "receivers": ["researcher", "coder"],
        },
        {"agent_id": "assistant", "name": "librarian", "shared": False},
    ]


def test_skill_share_requires_a_receiver(rpc: FakeRpc, run_cli: RunCli) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run_cli("skill", "share", "assistant", "librarian")

    assert exc_info.value.code == 2
    assert rpc.calls == []


def test_skill_disable_of_an_unknown_name_lists_the_candidates(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.fail("skill.set_disabled", "skill_not_found", "unknown skill: 'librrarian'")
    rpc.reply(
        "skill.inventory",
        {
            "skills": [
                {"name": "librarian", "origin": "bundled"},
                {"name": "summarize", "origin": "global"},
            ],
            "stale_shared": [],
            "policy_diagnostics": [],
        },
    )

    code, out, _err = run_cli("skill", "disable", "librrarian")

    assert code == 1
    for text in (
        "unknown skill: 'librrarian'",
        "did you mean: librarian",
        "known skills: librarian, summarize",
    ):
        assert text in out


@pytest.mark.parametrize(
    ("owner", "receiver", "message", "suggestion"),
    [
        pytest.param(
            "assistnt",
            "coder",
            "unknown agent: 'assistnt' (sharing is identity-agent-only)",
            "did you mean: assistant",
            id="unknown-owner",
        ),
        # The structured code routes the lookup; the suggestion targets the first requested
        # id missing from the Agent list, never text parsed from the message.
        pytest.param("assistant", "codr", "sentinel", "did you mean: coder", id="unknown-receiver"),
    ],
)
def test_skill_share_with_an_unknown_agent_suggests_the_available_agents(
    rpc: FakeRpc, run_cli: RunCli, owner: str, receiver: str, message: str, suggestion: str
) -> None:
    rpc.fail("skill.share", "agent_not_found", message)
    rpc.reply("agent.list", AGENTS)

    code, out, err = run_cli("skill", "share", owner, "librarian", "--to", receiver)

    assert code == 1
    assert rpc.methods == ["skill.share", "agent.list"]
    assert suggestion in out
    assert "available agents: assistant, coder" in out
    assert "rpc_method: skill.share" in err


def test_skill_share_of_a_wrong_private_skill_lists_the_owned_skills(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.fail(
        "skill.share", "skill_not_found", "agent 'assistant' owns no private skill named 'ghost'"
    )
    rpc.reply(
        "skill.inventory",
        {
            "skills": [
                {"name": "notes", "owner_id": "assistant"},
                {"name": "other", "owner_id": "coder"},
            ],
            "stale_shared": [],
            "policy_diagnostics": [],
        },
    )

    code, out, _err = run_cli("skill", "share", "assistant", "ghost", "--to", "coder")

    assert code == 1
    assert "owns no private skill named 'ghost'" in out
    assert "assistant's private skills: notes" in out


@pytest.mark.parametrize(
    "listing",
    [
        pytest.param({"ok": True, "result": {}}, id="malformed-inventory"),
        pytest.param({"ok": False}, id="failed-inventory"),
    ],
)
def test_skill_share_does_not_claim_no_private_skills_when_the_inventory_lookup_fails(
    rpc: FakeRpc, run_cli: RunCli, listing: dict[str, Any]
) -> None:
    rpc.fail("skill.share", "skill_not_found", "assistant owns no private skill named 'x'")
    rpc.respond("skill.inventory", listing)

    code, out, err = run_cli("skill", "share", "assistant", "x", "--to", "reviewer")

    assert code == 1
    assert rpc.methods == ["skill.share", "skill.inventory"]
    assert "inventory lookup" in out
    assert "assistant owns no private skills" not in out
    assert "rpc_method: skill.share" in err
