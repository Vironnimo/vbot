"""Tests for cron CLI parsing, RPC commands, and output."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import cron_management
from cli import main as cli_main
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


def test_parse_args_supports_cron_create_recurring() -> None:
    args = cli_main.parse_args(
        [
            "cron",
            "create",
            "assistant",
            "--prompt",
            "Check the news",
            "--cron",
            "0 9 * * *",
            "--timezone",
            "Europe/Berlin",
            "--session",
            "session-one",
        ]
    )

    assert args.area == "cron"
    assert args.command == "create"
    assert args.agent == "assistant"
    assert args.prompt == "Check the news"
    assert args.cron == "0 9 * * *"
    assert args.at is None
    assert args.timezone == "Europe/Berlin"
    assert args.session == "session-one"


def test_parse_args_cron_create_rejects_cron_and_at_together(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_main.parse_args(
            [
                "cron",
                "create",
                "assistant",
                "--prompt",
                "x",
                "--cron",
                "0 9 * * *",
                "--at",
                "2026-07-01T09:00:00+00:00",
            ]
        )

    assert exc_info.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


def test_parse_args_cron_create_requires_schedule(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli_main.parse_args(["cron", "create", "assistant", "--prompt", "x"])

    assert exc_info.value.code == 2
    assert "--cron" in capsys.readouterr().err


def test_parse_args_supports_cron_update_status() -> None:
    args = cli_main.parse_args(["cron", "update", "job-1", "--status", "paused"])

    assert args.area == "cron"
    assert args.command == "update"
    assert args.id == "job-1"
    assert args.status == "paused"


def test_cron_create_posts_recurring_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        calls.append(json)
        return httpx.Response(200, json={"ok": True, "result": {"id": "job-1"}})

    monkeypatch.setattr(cron_management.httpx, "post", fake_post)

    result = cron_management.cron_create(
        instance,
        {
            "agent_id": "assistant",
            "prompt": "Check the news",
            "schedule_type": "cron",
            "cron_expression": "0 9 * * *",
        },
    )

    assert result == CommandResult(ok=True, message="created cron job job-1", instance=instance)
    assert calls == [
        {
            "method": "cron.create",
            "params": {
                "agent_id": "assistant",
                "prompt": "Check the news",
                "schedule_type": "cron",
                "cron_expression": "0 9 * * *",
            },
        }
    ]


def test_cron_list_formats_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert json == {"method": "cron.list", "params": {}}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "jobs": [
                        {
                            "id": "job-1",
                            "agent_id": "assistant",
                            "prompt": "Check the news",
                            "schedule_type": "cron",
                            "cron_expression": "0 9 * * *",
                            "run_at": None,
                            "status": "active",
                            "next_fire_at": "2026-06-12T07:00:00+00:00",
                        },
                        {
                            "id": "job-2",
                            "agent_id": "coder",
                            "prompt": "A" * 100,
                            "schedule_type": "once",
                            "cron_expression": None,
                            "run_at": "2026-07-01T09:00:00+00:00",
                            "status": "paused",
                            "next_fire_at": None,
                        },
                    ]
                },
            },
        )

    monkeypatch.setattr(cron_management.httpx, "post", fake_post)

    result = cron_management.cron_list(instance)

    assert result.ok is True
    assert result.message.splitlines() == [
        "cron jobs:",
        (
            "- id=job-1 agent=assistant status=active schedule=cron[0 9 * * *] "
            "next_fire_at=2026-06-12T07:00:00+00:00 prompt=Check the news"
        ),
        (
            "- id=job-2 agent=coder status=paused "
            "schedule=once[2026-07-01T09:00:00+00:00] next_fire_at=- prompt=" + "A" * 57 + "..."
        ),
    ]


def test_cron_list_reports_empty_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"jobs": []}})

    monkeypatch.setattr(cron_management.httpx, "post", fake_post)

    result = cron_management.cron_list(instance)

    assert result == CommandResult(ok=True, message="no cron jobs configured", instance=instance)


def test_cron_update_rejects_empty_changes(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    result = cron_management.cron_update(instance, "job-1", {})

    assert result == CommandResult(
        ok=False,
        message=(
            "no cron fields provided; use one of: --agent, --prompt, --cron, --at, "
            "--timezone, --session, --status"
        ),
        instance=instance,
    )


@pytest.mark.parametrize(
    ("function_name", "method", "expected_message"),
    [
        ("cron_delete", "cron.delete", "deleted cron job job-1"),
        ("cron_enable", "cron.enable", "enabled cron job job-1"),
        ("cron_disable", "cron.disable", "disabled cron job job-1"),
    ],
)
def test_cron_simple_id_commands_post_expected_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    function_name: str,
    method: str,
    expected_message: str,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        calls.append(json)
        return httpx.Response(200, json={"ok": True, "result": {"ok": True}})

    monkeypatch.setattr(cron_management.httpx, "post", fake_post)

    result = getattr(cron_management, function_name)(instance, "job-1")

    assert result == CommandResult(ok=True, message=expected_message, instance=instance)
    assert calls == [{"method": method, "params": {"id": "job-1"}}]


def test_run_dispatches_cron_create_with_once_schedule(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path, port=8765)

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert json == {
            "method": "cron.create",
            "params": {
                "agent_id": "assistant",
                "prompt": "One-off reminder",
                "schedule_type": "once",
                "run_at": "2026-07-01T09:00:00+00:00",
            },
        }
        return httpx.Response(200, json={"ok": True, "result": {"id": "job-9"}})

    monkeypatch.setattr(cron_management.httpx, "post", fake_post)

    exit_code = cli_main.run(
        [
            "cron",
            "create",
            "assistant",
            "--prompt",
            "One-off reminder",
            "--at",
            "2026-07-01T09:00:00+00:00",
            "--port",
            "8765",
        ],
        resolve=fake_resolve,
    )

    assert exit_code == 0
    assert capsys.readouterr().out.splitlines() == ["created cron job job-9"]


def test_run_dispatches_cron_update_schedule_change(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path, port=8765)

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert json == {
            "method": "cron.update",
            "params": {
                "id": "job-1",
                "schedule_type": "cron",
                "cron_expression": "30 7 * * 1-5",
            },
        }
        return httpx.Response(200, json={"ok": True, "result": {"ok": True}})

    monkeypatch.setattr(cron_management.httpx, "post", fake_post)

    exit_code = cli_main.run(
        ["cron", "update", "job-1", "--cron", "30 7 * * 1-5", "--port", "8765"],
        resolve=fake_resolve,
    )

    assert exit_code == 0
    assert capsys.readouterr().out.splitlines() == ["updated cron job job-1"]
