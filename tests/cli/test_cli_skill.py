"""Tests for skill CLI parsing and RPC commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import main as cli_main
from cli import skill_management
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


def test_parse_args_supports_skill_list() -> None:
    args = cli_main.parse_args(
        ["skill", "list", "--host", "localhost", "--port", "8700", "--data-dir", "dev"]
    )

    assert args.area == "skill"
    assert args.command == "list"
    assert args.host == "localhost"
    assert args.port == 8700
    assert args.data_dir == "dev"


def test_skill_list_posts_skill_list_rpc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "skills": [{"name": "summarize", "description": "Summarize long text"}],
                    "invalid_skills": [],
                },
            },
        )

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    result = skill_management.skill_list(instance)

    assert result == CommandResult(
        ok=True,
        message="skills:\n- summarize  Summarize long text",
        instance=instance,
    )
    assert calls == [
        {
            "url": f"{instance.url}/api/rpc",
            "json": {"method": "skill.list", "params": {}},
            "timeout": 10.0,
        }
    ]


def test_skill_list_formats_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert url == f"{instance.url}/api/rpc"
        assert json == {"method": "skill.list", "params": {}}
        assert timeout == 10.0
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "skills": [
                        {"name": "draft-email", "description": "Draft concise replies"},
                        {"name": "release-notes", "description": "Write release notes"},
                    ],
                    "invalid_skills": [],
                },
            },
        )

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    result = skill_management.skill_list(instance)

    assert result.ok is True
    assert result.instance == instance
    assert result.message.splitlines() == [
        "skills:",
        "- draft-email  Draft concise replies",
        "- release-notes  Write release notes",
    ]


def test_skill_list_returns_empty_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "result": {"skills": [], "invalid_skills": []}},
        )

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    result = skill_management.skill_list(instance)

    assert result == CommandResult(ok=True, message="no skills configured", instance=instance)


def test_skill_list_formats_requirement_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "skills": [
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
                    "invalid_skills": [],
                },
            },
        )

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    result = skill_management.skill_list(instance)

    assert result.ok is True
    assert result.message.splitlines() == [
        "skills:",
        "- native-build  Build native projects "
        "(unavailable: missing binary 'gcc'; optional missing: missing binary 'jq')",
    ]


def test_skill_list_includes_invalid_section_when_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "skills": [
                        {"name": "summarize", "description": "Summarize long text"},
                    ],
                    "invalid_skills": [
                        {
                            "name": "broken-skill",
                            "path": "C:/skills/broken-skill/SKILL.md",
                            "warnings": ["missing description"],
                        }
                    ],
                },
            },
        )

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    result = skill_management.skill_list(instance)

    assert result.ok is True
    assert result.instance == instance
    assert "skills:" in result.message
    assert "invalid skills:" in result.message
    assert "- broken-skill (C:/skills/broken-skill/SKILL.md): missing description" in result.message


def test_skill_list_returns_error_on_rpc_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        return httpx.Response(
            500,
            json={
                "ok": False,
                "error": {"code": "rpc_error", "message": "server exploded"},
            },
        )

    monkeypatch.setattr(skill_management.httpx, "post", fake_post)

    result = skill_management.skill_list(instance)

    assert result == CommandResult(
        ok=False,
        message="rpc_error: server exploded",
        instance=instance,
    )
