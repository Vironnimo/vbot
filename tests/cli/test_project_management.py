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

from cli import cron_management, project_management, session_management
from cli import main as cli_main
from cli.server_management import ServerInstance
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
        "default_temperature": None,
        "default_thinking_effort": None,
        "source_format": "opencode",
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

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
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
    assert "vbot" in capsys.readouterr().out


# --- agent@projekt forwarding (additive address support) ---------------------


def test_session_list_forwards_project_qualified_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange: a project-qualified positional agent argument must reach the RPC
    # verbatim; the server parses ``agent@projekt``, the CLI does not.
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(200, json={"ok": True, "result": {"sessions": []}})

    monkeypatch.setattr(session_management.httpx, "post", fake_post)

    # Act
    result = session_management.session_list(instance, "orchestrator@vbot")

    # Assert
    assert result.ok is True
    assert calls == [
        {
            "method": "session.list",
            "params": {
                "agent_id": "orchestrator@vbot",
                "limit": 100,
                "include_subagents": True,
                "include_memory_reflections": True,
                "include_skill_reflections": True,
                "include_cron": True,
            },
        }
    ]


def test_session_list_bare_agent_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange: a bare agent argument (no ``@``) keeps identity behavior verbatim.
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(200, json={"ok": True, "result": {"sessions": []}})

    monkeypatch.setattr(session_management.httpx, "post", fake_post)

    # Act
    session_management.session_list(instance, "assistant")

    # Assert
    assert calls == [
        {
            "method": "session.list",
            "params": {
                "agent_id": "assistant",
                "limit": 100,
                "include_subagents": True,
                "include_memory_reflections": True,
                "include_skill_reflections": True,
                "include_cron": True,
            },
        }
    ]


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

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {
            "method": "cron.create",
            "params": {
                "agent_id": "builder@vbot",
                "name": "Nightly build",
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
            "--name",
            "Nightly build",
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
    assert "job-7" in capsys.readouterr().out


def test_cron_list_renders_project_target_address(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange: cron list displays the server-provided address form so a project
    # target reads as ``builder@vbot`` and a bare target stays ``assistant``.
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
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
