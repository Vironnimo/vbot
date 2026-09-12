"""Tests for project management mutations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import main as cli_main
from cli import project_management
from cli.server_management import ServerInstance
from tests.cli.project_management_test_support import (
    _project_response,
    make_instance,
)


# --- project set -------------------------------------------------------------
def test_project_set_posts_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
                    "project": _project_response(default_agent="builder"),
                    "scan": {"team": [], "report": {"clean": True, "findings": []}},
                },
            },
        )

    monkeypatch.setattr(project_management.httpx, "post", fake_post)

    # Act
    result = project_management.project_set(instance, "vbot", {"default_agent": "builder"})

    # Assert
    assert result.ok is True
    assert result.instance == instance
    assert "  default_agent: builder" in result.message
    assert "  report: clean" in result.message
    assert calls == [
        {
            "method": "project.set",
            "params": {"project_id": "vbot", "default_agent": "builder"},
        }
    ]


def test_run_project_set_maps_default_knobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `project set --default-temperature 0.4 --default-thinking-effort high` must
    # map to the matching RPC params (the args→changes wiring).
    instance = make_instance(tmp_path, port=8765)

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

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
                    "project": _project_response(
                        default_temperature=0.4, default_thinking_effort="high"
                    ),
                    "scan": {"team": [], "report": {"clean": True, "findings": []}},
                },
            },
        )

    monkeypatch.setattr(project_management.httpx, "post", fake_post)

    exit_code = cli_main.run(
        [
            "project",
            "set",
            "vbot",
            "--default-temperature",
            "0.4",
            "--default-thinking-effort",
            "high",
            "--port",
            "8765",
        ],
        resolve=fake_resolve,
    )

    assert exit_code == 0
    assert calls == [
        {
            "method": "project.set",
            "params": {
                "project_id": "vbot",
                "default_temperature": 0.4,
                "default_thinking_effort": "high",
            },
        }
    ]


def test_run_project_set_clear_flags_send_null(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The clear flags map to explicit null (fall through to the global default).
    instance = make_instance(tmp_path, port=8765)

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

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
                    "scan": {"team": [], "report": {"clean": True, "findings": []}},
                },
            },
        )

    monkeypatch.setattr(project_management.httpx, "post", fake_post)

    exit_code = cli_main.run(
        [
            "project",
            "set",
            "vbot",
            "--clear-default-agent",
            "--clear-default-model",
            "--clear-default-temperature",
            "--clear-default-thinking-effort",
            "--port",
            "8765",
        ],
        resolve=fake_resolve,
    )

    assert exit_code == 0
    assert calls == [
        {
            "method": "project.set",
            "params": {
                "project_id": "vbot",
                "default_agent": None,
                "default_model": None,
                "default_temperature": None,
                "default_thinking_effort": None,
            },
        }
    ]


def test_project_set_rejects_empty_changes(tmp_path: Path) -> None:
    # Arrange
    instance = make_instance(tmp_path)

    # Act
    result = project_management.project_set(instance, "vbot", {})

    # Assert
    assert result.ok is False
    assert result.instance is instance
    for option in (
        "--cwd",
        "--name",
        "--default-agent",
        "--default-model",
        "--default-temperature",
        "--default-thinking-effort",
        "--format",
        "--auto-load",
        "--allowed-tools",
        "--enabled-bundled-skills",
        "--enabled-global-skills",
        "--disabled-project-skills",
    ):
        assert option in result.message


# --- project rm --------------------------------------------------------------
def test_project_rm_reports_archive_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
                    "project_id": "vbot",
                    "archived": True,
                    "archive_path": "/data/projects/_archive/vbot-2026.zip",
                },
            },
        )

    monkeypatch.setattr(project_management.httpx, "post", fake_post)

    # Act
    result = project_management.project_remove(instance, "vbot")

    # Assert
    assert result.ok is True
    assert result.instance is instance
    assert "vbot" in result.message
    assert "/data/projects/_archive/vbot-2026.zip" in result.message
    assert calls == [{"method": "project.rm", "params": {"project_id": "vbot"}}]


def test_project_rm_surfaces_busy_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": False,
                "error": {
                    "code": "project_busy",
                    "message": "cannot remove project with active or queued runs: agent builder",
                },
            },
        )

    monkeypatch.setattr(project_management.httpx, "post", fake_post)

    # Act
    result = project_management.project_remove(instance, "vbot")

    # Assert
    assert result.ok is False
    assert result.instance is instance
    assert result.message.startswith("project_busy:")
    assert "builder" in result.message


def test_project_rm_surfaces_in_use_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": False,
                "error": {
                    "code": "project_in_use",
                    "message": "cannot remove project referenced by cron:job-1",
                },
            },
        )

    monkeypatch.setattr(project_management.httpx, "post", fake_post)

    # Act
    result = project_management.project_remove(instance, "vbot")

    # Assert
    assert result.ok is False
    assert result.message.startswith("project_in_use:")
    assert "cron:job-1" in result.message


def test_project_set_maps_tool_and_skill_policy_flags() -> None:
    args = cli_main.parse_args(
        [
            "project",
            "set",
            "vbot",
            "--allowed-tools",
            "read",
            "bash",
            "--enabled-bundled-skills",
            "vbot-cli",
            "--enabled-global-skills",
            "glossary",
            "--disabled-project-skills",
            "unsafe-skill",
        ]
    )

    assert cli_main._project_set_changes_from_args(args) == {
        "allowed_tools": ["read", "bash"],
        "skills_bundled_enabled": ["vbot-cli"],
        "skills_global_enabled": ["glossary"],
        "skills_project_disabled": ["unsafe-skill"],
    }


def test_project_set_override_coerces_value_and_returns_refreshed_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
                    "scan": {"team": [], "report": {"clean": True, "findings": []}},
                },
            },
        )

    monkeypatch.setattr(project_management.httpx, "post", fake_post)

    result = project_management.project_set_override(
        instance, "vbot", "builder", "temperature", "0.35"
    )

    assert result.ok is True
    assert "builder" in result.message
    assert "temperature" in result.message
    assert "vbot" in result.message
    assert calls == [
        {
            "method": "project.set_override",
            "params": {
                "project_id": "vbot",
                "agent_id": "builder",
                "field": "temperature",
                "value": 0.35,
            },
        }
    ]


def test_project_set_override_coerces_tool_access_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
                    "scan": {"team": [], "report": {"clean": True, "findings": []}},
                },
            },
        )

    monkeypatch.setattr(project_management.httpx, "post", fake_post)

    result = project_management.project_set_override(
        instance,
        "vbot",
        "builder",
        "tool_access",
        '{"mode":"selected","allowed":["read"]}',
    )

    assert result.ok is True
    assert calls[0]["params"]["value"] == {
        "mode": "selected",
        "allowed": ["read"],
    }


def test_project_remove_can_preserve_rooted_agent_files_and_reports_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
                    "project_id": "vbot",
                    "archive_path": "C:/data/projects/.archive/vbot",
                    "affected_agent_ids": ["librarian"],
                    "copied_files": {"librarian": ["SOUL.md", "MEMORY.md"]},
                    "backed_up_files": {"librarian": ["SOUL.md"]},
                },
            },
        )

    monkeypatch.setattr(project_management.httpx, "post", fake_post)

    result = project_management.project_remove(instance, "vbot", True)

    assert result.ok is True
    assert "affected_rooted_agents: librarian" in result.message
    assert "  librarian: SOUL.md,MEMORY.md" in result.message
    assert calls[0]["params"]["copy_rooted_agent_identity_files"] is True


def test_project_detect_reports_format_and_context_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "project.detect", "params": {"cwd": "C:/repos/demo"}}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "cwd_exists": True,
                    "formats": {
                        "opencode": {"agents": 2, "skills": 1},
                        "claude": {"agents": 0, "skills": 0},
                    },
                    "context_files": {"agents_md": True, "claude_md": None},
                },
            },
        )

    monkeypatch.setattr(project_management.httpx, "post", fake_post)

    result = project_management.project_detect(instance, "C:/repos/demo")

    assert result.ok is True
    assert result.message.splitlines() == [
        "detected project facts for C:/repos/demo:",
        "claude: agents=0 skills=0",
        "opencode: agents=2 skills=1",
        "AGENTS.md present: yes",
        "CLAUDE.md: none",
    ]


def test_project_detect_treats_missing_path_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "project.detect", "params": {"cwd": "C:/repos/missing"}}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "cwd_exists": False,
                    "formats": {},
                    "context_files": {"agents_md": False, "claude_md": None},
                },
            },
        )

    monkeypatch.setattr(project_management.httpx, "post", fake_post)

    result = project_management.project_detect(instance, "C:/repos/missing")

    assert result.ok is True
    assert "no directory at C:/repos/missing" in result.message


def test_parse_args_supports_project_detect() -> None:
    args = cli_main.parse_args(["project", "detect", "C:/repos/demo"])

    assert args.area == "project"
    assert args.command == "detect"
    assert args.cwd == "C:/repos/demo"
