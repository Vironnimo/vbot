"""Tests for project management dispatch."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import cron_management, project_management, session_management
from cli import main as cli_main
from cli.server_management import ServerInstance
from tests.cli.project_management_test_support import (
    _project_response,
    make_instance,
)


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
