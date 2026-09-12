"""Tests for project management."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import main as cli_main
from cli import project_management
from tests.cli.project_management_test_support import (
    _project_response,
    make_instance,
)


# --- parsing -----------------------------------------------------------------
def test_parse_args_supports_project_add_options() -> None:
    # Arrange / Act
    args = cli_main.parse_args(
        [
            "project",
            "add",
            "./my-repo",
            "--name",
            "vBot",
            "--default-agent",
            "orchestrator",
            "--default-model",
            "openai/gpt-5.2",
            "--auto-load",
            "AGENTS.md",
            "docs/guide.md",
        ]
    )

    # Assert
    assert args.area == "project"
    assert args.command == "add"
    assert args.cwd == "./my-repo"
    assert args.name == "vBot"
    assert args.default_agent == "orchestrator"
    assert args.default_model == "openai/gpt-5.2"
    assert args.auto_load == ["AGENTS.md", "docs/guide.md"]


def test_parse_args_supports_project_set_and_rm() -> None:
    # Arrange / Act
    set_args = cli_main.parse_args(["project", "set", "vbot", "--default-agent", "builder"])
    rm_args = cli_main.parse_args(["project", "rm", "vbot"])

    # Assert
    assert (set_args.command, set_args.id, set_args.default_agent) == ("set", "vbot", "builder")
    assert (rm_args.command, rm_args.id) == ("rm", "vbot")


def test_parse_args_supports_project_format_flag() -> None:
    add_args = cli_main.parse_args(["project", "add", "./my-repo", "--format", "claude"])
    set_args = cli_main.parse_args(["project", "set", "vbot", "--format", "opencode"])

    assert add_args.format == "claude"
    assert set_args.format == "opencode"


def test_parse_args_rejects_unknown_project_format() -> None:
    with pytest.raises(SystemExit):
        cli_main.parse_args(["project", "add", "./my-repo", "--format", "cursor"])


def test_project_format_flag_maps_to_source_format_field() -> None:
    # The user-facing flag is --format; the RPC field is source_format.
    add_args = cli_main.parse_args(["project", "add", "./my-repo", "--format", "claude"])
    set_args = cli_main.parse_args(["project", "set", "vbot", "--format", "claude"])

    assert cli_main._project_add_fields_from_args(add_args)["source_format"] == "claude"
    assert cli_main._project_set_changes_from_args(set_args)["source_format"] == "claude"


def test_project_add_without_format_flag_sends_no_source_format() -> None:
    # No flag → the server auto-detects; the CLI must not send the field at all.
    args = cli_main.parse_args(["project", "add", "./my-repo"])

    assert "source_format" not in cli_main._project_add_fields_from_args(args)


def test_parse_args_supports_project_default_knobs() -> None:
    args = cli_main.parse_args(
        [
            "project",
            "add",
            "./my-repo",
            "--default-temperature",
            "0.4",
            "--default-thinking-effort",
            "high",
        ]
    )

    assert args.default_temperature == 0.4
    assert args.default_thinking_effort == "high"
    assert args.clear_default_temperature is False
    assert args.clear_default_thinking_effort is False


def test_parse_args_supports_project_clear_default_knobs() -> None:
    args = cli_main.parse_args(
        [
            "project",
            "set",
            "vbot",
            "--clear-default-agent",
            "--clear-default-model",
            "--clear-default-temperature",
            "--clear-default-thinking-effort",
        ]
    )

    assert args.clear_default_agent is True
    assert args.clear_default_model is True
    assert args.clear_default_temperature is True
    assert args.clear_default_thinking_effort is True


def test_parse_args_supports_project_tool_access_overrides() -> None:
    set_args = cli_main.parse_args(
        [
            "project",
            "set-override",
            "vbot",
            "builder",
            "tool_access",
            '{"mode":"selected","allowed":["read"]}',
        ]
    )
    clear_args = cli_main.parse_args(
        ["project", "clear-override", "vbot", "builder", "tool_access"]
    )

    assert set_args.field == "tool_access"
    assert clear_args.field == "tool_access"


# --- project add -------------------------------------------------------------
def test_project_add_posts_rpc_and_renders_scan_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "project": _project_response(),
                    "scan": {
                        "team": [
                            {
                                "agent_id": "orchestrator",
                                "display_name": "Orchestrator",
                                "description": "Routes work",
                                "model": "openai/gpt-5.2",
                                "temperature": None,
                                "source_format": "opencode",
                                "source_path": "/repos/vbot/.opencode/agents/orchestrator.md",
                            }
                        ],
                        "report": {
                            "clean": False,
                            "findings": [
                                {
                                    "type": "unconfigured_model",
                                    "detail": "model not configured: ghost/model",
                                    "agent_id": "builder",
                                    "source_path": "/repos/vbot/.opencode/agents/builder.md",
                                }
                            ],
                        },
                    },
                },
            },
        )

    monkeypatch.setattr(project_management.httpx, "post", fake_post)

    # Act
    result = project_management.project_add(
        instance,
        "./my-repo",
        {"display_name": "vBot", "default_agent": "orchestrator", "auto_load": ["AGENTS.md"]},
    )

    # Assert
    assert result.ok is True
    assert "  display_name: vBot" in result.message
    assert "  cwd: /repos/vbot" in result.message
    assert "  cwd_exists: yes" in result.message
    assert "  default_agent: orchestrator" in result.message
    assert "  default_model: openai/gpt-5.2" in result.message
    assert "  format: opencode" in result.message
    assert "  auto_load: AGENTS.md" in result.message
    assert "orchestrator model=openai/gpt-5.2 description=Routes work" in result.message
    assert "unconfigured_model" in result.message
    assert "ghost/model" in result.message
    assert "builder" in result.message
    assert calls == [
        {
            "method": "project.add",
            "params": {
                "cwd": "./my-repo",
                "display_name": "vBot",
                "default_agent": "orchestrator",
                "auto_load": ["AGENTS.md"],
            },
        }
    ]


def test_project_add_renders_empty_team_and_clean_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "project": _project_response(default_model="", auto_load=[]),
                    "scan": {"team": [], "report": {"clean": True, "findings": []}},
                },
            },
        )

    monkeypatch.setattr(project_management.httpx, "post", fake_post)

    # Act
    result = project_management.project_add(instance, "./empty-repo", {})

    # Assert
    assert result.ok is True
    assert "  team: (empty)" in result.message.splitlines()
    assert "  report: clean" in result.message.splitlines()


# --- project list / show -----------------------------------------------------
def test_project_list_formats_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "project.list", "params": {}}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "projects": [
                        _project_response(),
                        _project_response(
                            project_id="site",
                            display_name="Site",
                            cwd="/repos/site",
                            cwd_exists=False,
                            default_agent="",
                        ),
                    ]
                },
            },
        )

    monkeypatch.setattr(project_management.httpx, "post", fake_post)

    # Act
    result = project_management.project_list(instance)

    # Assert
    assert result.ok is True
    assert result.message.splitlines()[1:] == [
        "- id=vbot name=vBot cwd=/repos/vbot cwd_exists=yes default_agent=orchestrator",
        "- id=site name=Site cwd=/repos/site cwd_exists=no default_agent=-",
    ]


def test_project_list_reports_empty_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"projects": []}})

    monkeypatch.setattr(project_management.httpx, "post", fake_post)

    # Act
    result = project_management.project_list(instance)

    # Assert
    assert result.ok is True
    assert result.instance is instance
    assert result.message.strip()


def test_project_show_posts_rpc_and_renders_team(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "project": _project_response(),
                    "scan": {
                        "team": [
                            {
                                "agent_id": "orchestrator",
                                "display_name": "Orchestrator",
                                "description": "Routes work",
                                "model": "openai/gpt-5.2",
                                "temperature": None,
                                "source_format": "opencode",
                                "source_path": "/repos/vbot/.opencode/agents/orchestrator.md",
                            }
                        ],
                        "report": {"clean": True, "findings": []},
                    },
                },
            },
        )

    monkeypatch.setattr(project_management.httpx, "post", fake_post)

    # Act
    result = project_management.project_show(instance, "vbot")

    # Assert
    assert result.ok is True
    assert "    - orchestrator model=openai/gpt-5.2 description=Routes work" in (
        result.message.splitlines()
    )
    assert calls == [{"method": "project.show", "params": {"project_id": "vbot"}}]


def test_project_show_renders_default_knob_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 0.0 is a real temperature (renders as a number, not "-"); "" thinking is the
    # explicit provider default (rendered distinctly from "no default" = "-").
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "project": _project_response(
                        default_temperature=0.0, default_thinking_effort=""
                    ),
                    "scan": {"team": [], "report": {"clean": True, "findings": []}},
                },
            },
        )

    monkeypatch.setattr(project_management.httpx, "post", fake_post)

    result = project_management.project_show(instance, "vbot")

    lines = result.message.splitlines()
    assert "  default_temperature: 0.0" in lines
    assert "  default_thinking_effort: (provider default)" in lines
