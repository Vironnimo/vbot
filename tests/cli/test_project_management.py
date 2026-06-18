"""Tests for project CLI parsing, RPC commands, output, and address forwarding.

The project area is an accessor over the ``project.*`` server RPC: every command
posts its parsed args and renders the deterministic, agent-facing response. The
address-form tests cover the cross-cutting requirement that a positional
``agent@projekt`` argument is forwarded verbatim to the session/cron RPC (the
server parses it), while a bare agent argument keeps its current behavior.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import cron_management
from cli import main as cli_main
from cli import project_management, session_management
from cli.server_management import CommandResult, ServerInstance
from core.utils.logging import resolve_daily_log_path


def make_instance(tmp_path: Path, *, port: int = 8420) -> ServerInstance:
    data_dir = tmp_path / "data"
    return ServerInstance(
        host="127.0.0.1",
        port=port,
        data_dir=data_dir,
        url=f"http://127.0.0.1:{port}",
        log_path=resolve_daily_log_path(data_dir),
    )


def _project_response(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "project_id": "vbot",
        "display_name": "vBot",
        "cwd": "/repos/vbot",
        "cwd_exists": True,
        "default_agent": "orchestrator",
        "default_model": "openai/gpt-5.2",
        "auto_load": ["AGENTS.md"],
        "created_at": "2026-06-18T08:00:00+00:00",
        "updated_at": "2026-06-18T08:00:00+00:00",
    }
    base.update(overrides)
    return base


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


# --- project add -------------------------------------------------------------


def test_project_add_posts_rpc_and_renders_scan_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
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
    assert result.message.splitlines() == [
        "added project vbot",
        "  display_name: vBot",
        "  cwd: /repos/vbot",
        "  cwd_exists: yes",
        "  default_agent: orchestrator",
        "  default_model: openai/gpt-5.2",
        "  auto_load: AGENTS.md",
        "  team:",
        "    - orchestrator model=openai/gpt-5.2 description=Routes work",
        "  report:",
        "    - [unconfigured_model] model not configured: ghost/model (agent builder)",
    ]
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

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
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

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
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
    assert result.message.splitlines() == [
        "projects:",
        "- id=vbot name=vBot cwd=/repos/vbot cwd_exists=yes default_agent=orchestrator",
        "- id=site name=Site cwd=/repos/site cwd_exists=no default_agent=-",
    ]


def test_project_list_reports_empty_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"projects": []}})

    monkeypatch.setattr(project_management.httpx, "post", fake_post)

    # Act
    result = project_management.project_list(instance)

    # Assert
    assert result == CommandResult(
        ok=True, message="no projects configured", instance=instance
    )


def test_project_show_posts_rpc_and_renders_team(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
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
    assert result.message.splitlines()[0] == "project vbot:"
    assert "    - orchestrator model=openai/gpt-5.2 description=Routes work" in (
        result.message.splitlines()
    )
    assert calls == [{"method": "project.show", "params": {"project_id": "vbot"}}]


# --- project set -------------------------------------------------------------


def test_project_set_posts_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
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
    assert result == CommandResult(
        ok=True, message="updated project vbot", instance=instance
    )
    assert calls == [
        {
            "method": "project.set",
            "params": {"project_id": "vbot", "default_agent": "builder"},
        }
    ]


def test_project_set_rejects_empty_changes(tmp_path: Path) -> None:
    # Arrange
    instance = make_instance(tmp_path)

    # Act
    result = project_management.project_set(instance, "vbot", {})

    # Assert
    assert result == CommandResult(
        ok=False,
        message=(
            "no project fields provided; use one of: "
            "--cwd, --name, --default-agent, --default-model, --auto-load"
        ),
        instance=instance,
    )


# --- project rm --------------------------------------------------------------


def test_project_rm_reports_archive_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
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
    assert result == CommandResult(
        ok=True,
        message="removed project vbot (archived to /data/projects/_archive/vbot-2026.zip)",
        instance=instance,
    )
    assert calls == [{"method": "project.rm", "params": {"project_id": "vbot"}}]


def test_project_rm_surfaces_busy_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
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
    assert result == CommandResult(
        ok=False,
        message=(
            "project_busy: cannot remove project with active or queued runs: agent builder"
        ),
        instance=instance,
    )


def test_project_rm_surfaces_in_use_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
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
    assert result.message == "project_in_use: cannot remove project referenced by cron:job-1"


# --- run() dispatch ----------------------------------------------------------


def test_run_dispatches_project_add(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    instance = make_instance(tmp_path, port=8765)

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert json == {
            "method": "project.add",
            "params": {"cwd": "./my-repo", "display_name": "vBot"},
        }
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
    exit_code = cli_main.run(
        ["project", "add", "./my-repo", "--name", "vBot", "--port", "8765"],
        resolve=fake_resolve,
    )

    # Assert
    assert exit_code == 0
    assert capsys.readouterr().out.splitlines()[0] == "added project vbot"


# --- agent@projekt forwarding (additive address support) ---------------------


def test_session_list_forwards_project_qualified_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange: a project-qualified positional agent argument must reach the RPC
    # verbatim; the server parses ``agent@projekt``, the CLI does not.
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        calls.append(json)
        return httpx.Response(200, json={"ok": True, "result": {"sessions": []}})

    monkeypatch.setattr(session_management.httpx, "post", fake_post)

    # Act
    result = session_management.session_list(instance, "orchestrator@vbot")

    # Assert
    assert result.ok is True
    assert calls == [
        {"method": "session.list", "params": {"agent_id": "orchestrator@vbot"}}
    ]


def test_session_list_bare_agent_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange: a bare agent argument (no ``@``) keeps identity behavior verbatim.
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        calls.append(json)
        return httpx.Response(200, json={"ok": True, "result": {"sessions": []}})

    monkeypatch.setattr(session_management.httpx, "post", fake_post)

    # Act
    session_management.session_list(instance, "assistant")

    # Assert
    assert calls == [{"method": "session.list", "params": {"agent_id": "assistant"}}]


def test_run_forwards_cron_create_project_address(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange: the cron positional agent argument carries ``agent@projekt`` to the
    # RPC unchanged (the server parses and stores the project dimension).
    instance = make_instance(tmp_path, port=8765)

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert json == {
            "method": "cron.create",
            "params": {
                "agent_id": "builder@vbot",
                "prompt": "Nightly build",
                "schedule_type": "cron",
                "cron_expression": "0 2 * * *",
            },
        }
        return httpx.Response(200, json={"ok": True, "result": {"id": "job-7"}})

    monkeypatch.setattr(cron_management.httpx, "post", fake_post)

    # Act
    exit_code = cli_main.run(
        [
            "cron",
            "create",
            "builder@vbot",
            "--prompt",
            "Nightly build",
            "--cron",
            "0 2 * * *",
            "--port",
            "8765",
        ],
        resolve=fake_resolve,
    )

    # Assert
    assert exit_code == 0
    assert capsys.readouterr().out.splitlines() == ["created cron job job-7"]


def test_cron_list_renders_project_target_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange: cron list displays the server-provided address form so a project
    # target reads as ``builder@vbot`` and a bare target stays ``assistant``.
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "jobs": [
                        {
                            "id": "job-1",
                            "agent_id": "builder",
                            "project_id": "vbot",
                            "target": "builder@vbot",
                            "prompt": "Nightly build",
                            "schedule_type": "cron",
                            "cron_expression": "0 2 * * *",
                            "run_at": None,
                            "status": "active",
                            "next_fire_at": "2026-06-19T00:00:00+00:00",
                        },
                        {
                            "id": "job-2",
                            "agent_id": "assistant",
                            "project_id": None,
                            "target": "assistant",
                            "prompt": "Daily digest",
                            "schedule_type": "cron",
                            "cron_expression": "0 9 * * *",
                            "run_at": None,
                            "status": "active",
                            "next_fire_at": "2026-06-19T07:00:00+00:00",
                        },
                    ]
                },
            },
        )

    monkeypatch.setattr(cron_management.httpx, "post", fake_post)

    # Act
    result = cron_management.cron_list(instance)

    # Assert
    assert result.ok is True
    rows = result.message.splitlines()
    assert "agent=builder@vbot" in rows[1]
    assert "agent=assistant" in rows[2]
