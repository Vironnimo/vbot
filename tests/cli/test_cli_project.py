"""Tests for the ``vbot project`` commands: RPC requests and printed output."""

from __future__ import annotations

from typing import Any

import pytest

from tests.cli.cli_test_support import FakeRpc, RunCli

CLEAN_SCAN = {"team": [], "report": {"clean": True, "findings": []}}
TEAM_MEMBER = {
    "agent_id": "orchestrator",
    "display_name": "Orchestrator",
    "description": "Routes work",
    "model": "openai/gpt-5.2",
    "temperature": None,
    "source": "opencode",
    "status": "limited",
    "translations": [
        {"setting": "model", "status": "not_supported", "detail": "Set a Model mapping."}
    ],
    "source_path": "/repos/vbot/.opencode/agents/orchestrator.md",
}
TEAM_ROW = (
    "    - orchestrator model=openai/gpt-5.2 description=Routes work source=opencode status=limited"
)


def _project(**overrides: Any) -> dict[str, Any]:
    return {
        "project_id": "vbot",
        "display_name": "vBot",
        "cwd": "/repos/vbot",
        "cwd_exists": True,
        "default_agent": "orchestrator",
        "default_model": "openai/gpt-5.2",
        "default_temperature": None,
        "default_thinking_effort": None,
        "sources": [],
        "auto_load": ["AGENTS.md"],
        "created_at": "2026-06-18T08:00:00+00:00",
        "updated_at": "2026-06-18T08:00:00+00:00",
    } | overrides


def test_project_add_sends_every_option_and_previews_the_scan(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    finding = {
        "type": "unconfigured_model",
        "detail": "model not configured: ghost/model",
        "agent_id": "builder",
        "source_path": "/repos/vbot/.opencode/agents/builder.md",
    }
    scan = {"team": [TEAM_MEMBER], "report": {"clean": False, "findings": [finding]}}
    rpc.reply("project.add", {"project": _project(), "scan": scan})

    code, out, _err = run_cli(
        "project", "add", "./my-repo",
        "--name", "vBot",
        "--default-agent", "orchestrator",
        "--default-model", "openai/gpt-5.2",
        "--default-temperature", "0.4",
        "--default-thinking-effort", "high",
        "--sources", '[{"id":"claude.agents","enabled":true}]',
        "--model-mappings", '{"sonnet":"openai/gpt-5.2"}',
        "--auto-load", "AGENTS.md", "docs/guide.md",
        "--allowed-tools", "read", "bash",
        "--enabled-bundled-skills", "vbot-docs",
    )  # fmt: skip

    assert code == 0
    assert rpc.calls == [
        (
            "project.add",
            {
                "cwd": "./my-repo",
                "display_name": "vBot",
                "default_agent": "orchestrator",
                "default_model": "openai/gpt-5.2",
                "default_temperature": 0.4,
                "default_thinking_effort": "high",
                "sources": [{"id": "claude.agents", "enabled": True}],
                "model_mappings": {"sonnet": "openai/gpt-5.2"},
                "auto_load": ["AGENTS.md", "docs/guide.md"],
                "allowed_tools": ["read", "bash"],
                "skills_bundled_enabled": ["vbot-docs"],
            },
        )
    ]
    lines = out.splitlines()
    for line in (
        "  display_name: vBot",
        "  cwd: /repos/vbot",
        "  cwd_exists: yes",
        "  default_agent: orchestrator",
        "  default_model: openai/gpt-5.2",
        "  sources: 0",
        "  auto_load: AGENTS.md",
        TEAM_ROW,
    ):
        assert line in lines
    for text in ("unconfigured_model", "ghost/model", "builder"):
        assert text in out
    assert "model: not_supported - Set a Model mapping." in out


def test_project_add_without_options_lets_the_server_detect_sources(
    rpc: FakeRpc, run_cli: RunCli
) -> None:
    rpc.reply(
        "project.add", {"project": _project(default_model="", auto_load=[]), "scan": CLEAN_SCAN}
    )

    code, out, _err = run_cli("project", "add", "./my-repo")

    assert code == 0
    assert rpc.calls == [("project.add", {"cwd": "./my-repo"})]
    assert "added project vbot" in out
    assert {"  team: (empty)", "  report: clean"} <= set(out.splitlines())


def test_project_add_rejects_malformed_sources(rpc: FakeRpc, run_cli: RunCli) -> None:
    with pytest.raises(SystemExit) as exc_info:
        run_cli("project", "add", "./my-repo", "--sources", "{")

    assert exc_info.value.code == 2
    assert rpc.calls == []


@pytest.mark.parametrize(
    ("projects", "rows"),
    [
        pytest.param(
            [
                _project(),
                _project(
                    project_id="site",
                    display_name="Site",
                    cwd="/repos/site",
                    cwd_exists=False,
                    default_agent="",
                ),
            ],
            [
                "- id=vbot name=vBot cwd=/repos/vbot cwd_exists=yes default_agent=orchestrator",
                "- id=site name=Site cwd=/repos/site cwd_exists=no default_agent=-",
            ],
            id="projects",
        ),
        pytest.param([], None, id="empty"),
    ],
)
def test_project_list_prints_one_row_per_project(
    rpc: FakeRpc, run_cli: RunCli, projects: list[dict[str, Any]], rows: list[str] | None
) -> None:
    rpc.reply("project.list", {"projects": projects})

    code, out, _err = run_cli("project", "list")

    assert code == 0
    assert rpc.calls == [("project.list", {})]
    if rows is None:
        assert out.strip()
    else:
        assert out.splitlines()[1:] == rows


@pytest.mark.parametrize(
    ("knobs", "shown"),
    [
        # 0.0 is a real temperature; "" thinking is the explicit provider default,
        # rendered distinctly from "no default" ("-").
        pytest.param(
            {"default_temperature": 0.0, "default_thinking_effort": ""},
            ("  default_temperature: 0.0", "  default_thinking_effort: (provider default)"),
            id="provider-default-effort",
        ),
        pytest.param(
            {"default_temperature": 0.4, "default_thinking_effort": "high"},
            ("  default_temperature: 0.4", "  default_thinking_effort: high"),
            id="explicit-effort",
        ),
    ],
)
def test_project_show_renders_the_team_and_the_default_knobs(
    rpc: FakeRpc, run_cli: RunCli, knobs: dict[str, Any], shown: tuple[str, ...]
) -> None:
    scan = {"team": [TEAM_MEMBER], "report": {"clean": True, "findings": []}}
    rpc.reply("project.show", {"project": _project(**knobs), "scan": scan})

    code, out, _err = run_cli("project", "show", "vbot")

    assert code == 0
    assert rpc.calls == [("project.show", {"project_id": "vbot"})]
    lines = out.splitlines()
    for line in (TEAM_ROW, *shown):
        assert line in lines


@pytest.mark.parametrize(
    ("options", "changes"),
    [
        pytest.param(("--default-agent", "builder"), {"default_agent": "builder"}, id="agent"),
        pytest.param(
            (
                "--default-temperature", "0.4",
                "--default-thinking-effort", "high",
                "--sources", "[]",
            ),
            {
                "default_temperature": 0.4,
                "default_thinking_effort": "high",
                "sources": [],
            },
            id="default-knobs",
        ),
        pytest.param(
            (
                "--clear-default-agent",
                "--clear-default-model",
                "--clear-default-temperature",
                "--clear-default-thinking-effort",
            ),
            {
                "default_agent": None,
                "default_model": None,
                "default_temperature": None,
                "default_thinking_effort": None,
            },
            id="clear-defaults",
        ),
        pytest.param(
            (
                "--allowed-tools", "read", "bash",
                "--enabled-bundled-skills", "vbot-docs",
                "--enabled-global-skills", "glossary",
                "--disabled-project-skills", "unsafe-skill",
            ),
            {
                "allowed_tools": ["read", "bash"],
                "skills_bundled_enabled": ["vbot-docs"],
                "skills_global_enabled": ["glossary"],
                "skills_project_disabled": ["unsafe-skill"],
            },
            id="tool-and-skill-policy",
        ),
    ],
)  # fmt: skip
def test_project_set_sends_the_requested_changes(
    rpc: FakeRpc, run_cli: RunCli, options: tuple[str, ...], changes: dict[str, Any]
) -> None:
    rpc.reply("project.set", {"project": _project(default_agent="builder"), "scan": CLEAN_SCAN})

    code, out, _err = run_cli("project", "set", "vbot", *options)

    assert code == 0
    assert rpc.calls == [("project.set", {"project_id": "vbot", **changes})]
    assert {"  default_agent: builder", "  report: clean"} <= set(out.splitlines())


def test_project_set_without_changes_lists_every_option(rpc: FakeRpc, run_cli: RunCli) -> None:
    code, out, _err = run_cli("project", "set", "vbot")

    assert code == 1
    assert rpc.calls == []
    for option in (
        "--cwd",
        "--name",
        "--default-agent",
        "--default-model",
        "--default-temperature",
        "--default-thinking-effort",
        "--sources",
        "--model-mappings",
        "--auto-load",
        "--allowed-tools",
        "--enabled-bundled-skills",
        "--enabled-global-skills",
        "--disabled-project-skills",
    ):
        assert option in out


@pytest.mark.parametrize(
    ("field", "raw", "value"),
    [
        pytest.param("temperature", "0.35", 0.35, id="number"),
        pytest.param(
            "tool_access",
            '{"mode":"selected","allowed":["read"]}',
            {"mode": "selected", "allowed": ["read"]},
            id="json-object",
        ),
        pytest.param(
            "tool_loading",
            '{"on_demand":true}',
            {"on_demand": True},
            id="tool-loading",
        ),
    ],
)
def test_project_override_set_coerces_the_value(
    rpc: FakeRpc, run_cli: RunCli, field: str, raw: str, value: object
) -> None:
    rpc.reply("project.set_override", {"project": _project(), "scan": CLEAN_SCAN})

    code, out, _err = run_cli("project", "override", "set", "vbot", "builder", field, raw)

    assert code == 0
    assert rpc.calls == [
        (
            "project.set_override",
            {"project_id": "vbot", "agent_id": "builder", "field": field, "value": value},
        )
    ]
    assert f"set builder.{field} override on" in out and "vbot" in out


@pytest.mark.parametrize(
    ("options", "params", "removed", "shown"),
    [
        pytest.param(
            (),
            {"project_id": "vbot"},
            {"archive_entry_id": "arc_7k2m9q4xw1ab", "session_count": 3},
            [
                "removed project vbot (archived as archive entry arc_7k2m9q4xw1ab, 3 sessions); "
                "restore with: vbot archive restore arc_7k2m9q4xw1ab"
            ],
            id="archived",
        ),
        pytest.param(
            ("--permanent", "--yes"),
            {"project_id": "vbot", "permanent": True},
            {"archive_entry_id": "arc_7k2m9q4xw1ab", "purged": True, "session_count": 3},
            ["removed project vbot permanently (3 sessions); the repo is untouched"],
            id="permanent",
        ),
        pytest.param(
            ("--copy-rooted-agent-files",),
            {"project_id": "vbot", "copy_rooted_agent_identity_files": True},
            {
                "archive_entry_id": "arc_7k2m9q4xw1ab",
                "affected_agent_ids": ["librarian"],
                "copied_files": {"librarian": ["SOUL.md", "MEMORY.md"]},
                "backed_up_files": {"librarian": ["SOUL.md"]},
            },
            ["default_project_cleared_for: librarian", "  librarian: SOUL.md,MEMORY.md"],
            id="rooted-agent-files-copied",
        ),
    ],
)
def test_project_remove_archives_the_project_and_reports_side_effects(
    rpc: FakeRpc,
    run_cli: RunCli,
    options: tuple[str, ...],
    params: dict[str, Any],
    removed: dict[str, Any],
    shown: list[str],
) -> None:
    rpc.reply("project.rm", {"project_id": "vbot"} | removed)

    code, out, _err = run_cli("project", "remove", "vbot", *options)

    assert code == 0
    assert rpc.calls == [("project.rm", params)]
    lines = out.splitlines()
    for line in shown:
        assert line in lines


@pytest.mark.parametrize(
    ("error", "message", "detail"),
    [
        pytest.param(
            "project_busy",
            "cannot remove project with active or queued runs: agent builder",
            "builder",
            id="busy",
        ),
        pytest.param(
            "project_in_use",
            "cannot remove project referenced by cron:job-1",
            "cron:job-1",
            id="in-use",
        ),
    ],
)
def test_project_remove_surfaces_the_block_reason(
    rpc: FakeRpc, run_cli: RunCli, error: str, message: str, detail: str
) -> None:
    rpc.fail("project.rm", error, message, status=200)

    code, out, err = run_cli("project", "rm", "vbot")

    assert code == 1
    assert out.startswith(f"{error}:")
    assert detail in out
    assert "rpc_method: project.rm" in err


@pytest.mark.parametrize(
    ("cwd", "facts", "expected"),
    [
        pytest.param(
            "C:/repos/demo",
            {
                "cwd_exists": True,
                "sources": [
                    {
                        "id": "opencode.agents",
                        "agents": 2,
                        "skills": 0,
                        "paths": [".opencode/agents"],
                    }
                ],
            },
            [
                "detected project facts for C:/repos/demo:",
                "opencode.agents: agents=2 skills=0 paths=.opencode/agents",
            ],
            id="existing-directory",
        ),
        pytest.param(
            "C:/repos/missing",
            {"cwd_exists": False, "sources": []},
            [
                "no directory at C:/repos/missing; nothing to detect "
                "(a nonexistent path is not an error)"
            ],
            id="missing-path-is-not-an-error",
        ),
    ],
)
def test_project_detect_reports_source_facts(
    rpc: FakeRpc, run_cli: RunCli, cwd: str, facts: dict[str, Any], expected: list[str]
) -> None:
    rpc.reply("project.detect", facts)

    code, out, _err = run_cli("project", "detect", cwd)

    assert code == 0
    assert rpc.calls == [("project.detect", {"cwd": cwd})]
    assert out.splitlines() == expected
