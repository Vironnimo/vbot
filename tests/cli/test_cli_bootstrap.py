"""Tests for Bootstrap CLI parsing, current-Run context, RPC, and output."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import bootstrap_management
from cli import main as cli_main
from cli.server_management import ServerInstance
from core.utils.logging import resolve_daily_log_path


def make_instance(tmp_path: Path, *, host: str = "127.0.0.1") -> ServerInstance:
    data_dir = tmp_path / "data"
    return ServerInstance(
        host=host,
        port=8420,
        data_dir=data_dir,
        url=f"http://{host}:8420",
        log_path=resolve_daily_log_path(data_dir),
    )


def test_parse_args_supports_current_session_create() -> None:
    args = cli_main.parse_args(
        [
            "bootstrap",
            "create",
            "--current-session",
            "--name",
            "Verify update",
            "--prompt",
            "Check status and logs",
            "--mode",
            "once",
        ]
    )

    assert args.area == "bootstrap"
    assert args.agent is None
    assert args.current_session is True
    assert args.mode == "once"


def test_dispatch_create_requires_agent_or_current_session(tmp_path: Path) -> None:
    args = cli_main.parse_args(["bootstrap", "create", "--prompt", "Check", "--mode", "once"])

    result = cli_main.dispatch_bootstrap_command(args, make_instance(tmp_path))

    assert result.ok is False
    assert "<agent>" in result.message
    assert "--current-session" in result.message


def test_current_session_create_posts_injected_project_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setenv("VBOT_RUN_AGENT_ID", "builder")
    monkeypatch.setenv("VBOT_RUN_SESSION_ID", "session-one")
    monkeypatch.setenv("VBOT_RUN_PROJECT_ID", "vbot")

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(200, json={"ok": True, "result": {"id": "job-one"}})

    monkeypatch.setattr(bootstrap_management.httpx, "post", fake_post)

    result = bootstrap_management.bootstrap_create(
        instance,
        {"name": "Verify update", "prompt": "Check", "mode": "once"},
        current_session=True,
    )

    assert result.ok is True
    assert result.instance is instance
    assert "job-one" in result.message
    assert calls == [
        {
            "method": "bootstrap.create",
            "params": {
                "name": "Verify update",
                "prompt": "Check",
                "mode": "once",
                "agent_id": "builder@vbot",
                "session_id": "session-one",
            },
        }
    ]


def test_current_session_fails_outside_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VBOT_RUN_AGENT_ID", raising=False)
    monkeypatch.delenv("VBOT_RUN_SESSION_ID", raising=False)

    result = bootstrap_management.bootstrap_create(
        make_instance(tmp_path),
        {"prompt": "Check", "mode": "once"},
        current_session=True,
    )

    assert result.ok is False
    assert result.message.strip()


def test_current_session_rejects_remote_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VBOT_RUN_AGENT_ID", "main")
    monkeypatch.setenv("VBOT_RUN_SESSION_ID", "session-one")

    result = bootstrap_management.bootstrap_create(
        make_instance(tmp_path, host="server.example"),
        {"prompt": "Check", "mode": "once"},
        current_session=True,
    )

    assert result.ok is False
    assert "--current-session" in result.message


def test_bootstrap_list_formats_health_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
                            "id": "job-one",
                            "target": "main",
                            "name": "Verify update",
                            "prompt": "Check status and logs",
                            "mode": "once",
                            "status": "completed",
                            "session_id": "session-one",
                            "last_outcome": "success",
                            "last_error": None,
                        }
                    ]
                },
            },
        )

    monkeypatch.setattr(bootstrap_management.httpx, "post", fake_post)

    result = bootstrap_management.bootstrap_list(instance)

    assert result.message.splitlines()[1:] == [
        "- name=Verify update id=job-one agent=main mode=once status=completed "
        "session=session-one last_outcome=success last_error=- prompt=Check status and logs",
    ]
