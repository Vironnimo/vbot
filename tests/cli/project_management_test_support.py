"""Shared fixtures and fakes for project management behavior tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
