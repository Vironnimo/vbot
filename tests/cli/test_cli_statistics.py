"""Tests for statistics CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import main as cli_main
from cli import statistics_management
from cli.server_management import CommandResult, ServerInstance
from core.utils.logging import resolve_daily_log_path


def make_instance(tmp_path: Path) -> ServerInstance:
    data_dir = tmp_path / "data"
    return ServerInstance(
        host="127.0.0.1",
        port=8420,
        data_dir=data_dir,
        url="http://127.0.0.1:8420",
        log_path=resolve_daily_log_path(data_dir),
    )


def _skills_report(skills_section: dict[str, Any], window: dict[str, Any] | None = None) -> dict:
    """Wrap a skills section into a minimal full-report result payload."""

    return {
        "window": window if window is not None else {"since": None, "until": None},
        "skills": skills_section,
    }


def _populated_skills_section() -> dict[str, Any]:
    return {
        "total_skills": 3,
        "used_skills": 1,
        "never_used_skills": 2,
        "skills": [
            {
                "name": "vbot-cli",
                "origins": ["bundled"],
                "offered_sessions": 10,
                "activated_sessions": 4,
                "usage_rate": 0.4,
                "first_offered": "2026-06-01T09:00:00+00:00",
                "last_offered": "2026-07-01T09:00:00+00:00",
                "first_activated": "2026-06-02T09:00:00+00:00",
                "last_activated": "2026-07-01T10:00:00+00:00",
                "by_agent": [{"key": "assistant", "count": 4}],
            },
            {
                "name": "glossary",
                "origins": ["global", "project:vBot"],
                "offered_sessions": 8,
                "activated_sessions": 0,
                "usage_rate": None,
                "first_offered": "2026-06-01T09:00:00+00:00",
                "last_offered": "2026-06-20T09:00:00+00:00",
                "first_activated": None,
                "last_activated": None,
                "by_agent": [],
            },
            {
                "name": "deep-research",
                "origins": ["global"],
                "offered_sessions": 0,
                "activated_sessions": 0,
                "usage_rate": None,
                "first_offered": None,
                "last_offered": None,
                "first_activated": None,
                "last_activated": None,
                "by_agent": [],
            },
        ],
    }


def _fake_post_returning(result: dict, captured: dict[str, Any]):
    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return httpx.Response(200, json={"ok": True, "result": result})

    return fake_post


def test_statistics_skills_posts_report_rpc_and_formats_section(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    captured: dict[str, Any] = {}
    result_payload = _skills_report(_populated_skills_section())
    monkeypatch.setattr(
        statistics_management.httpx, "post", _fake_post_returning(result_payload, captured)
    )

    result = statistics_management.statistics_report(instance, "skills")

    assert captured["url"] == f"{instance.url}/api/rpc"
    assert captured["json"] == {"method": "statistics.report", "params": {}}
    assert captured["timeout"] == 10.0
    assert result.ok is True
    assert result.instance is instance
    assert result.message == (
        "skills:\n"
        "window: all time\n"
        "total skills: 3\n"
        "used skills: 1\n"
        "never used skills: 2\n"
        "\n"
        "never used:\n"
        "  glossary [global, project:vBot]\n"
        "  deep-research [global]\n"
        "\n"
        "per skill:\n"
        "  vbot-cli [bundled]: offered=10 activated=4 usage_rate=0.40 "
        "last_activated=2026-07-01T10:00:00+00:00\n"
        "  glossary [global, project:vBot]: offered=8 activated=0 usage_rate=- last_activated=-\n"
        "  deep-research [global]: offered=0 activated=0 usage_rate=- last_activated=-"
    )


def test_statistics_skills_empty_section_shows_explicit_empty_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    captured: dict[str, Any] = {}
    empty_section = {
        "total_skills": 0,
        "used_skills": 0,
        "never_used_skills": 0,
        "skills": [],
    }
    result_payload = _skills_report(empty_section)
    monkeypatch.setattr(
        statistics_management.httpx, "post", _fake_post_returning(result_payload, captured)
    )

    result = statistics_management.statistics_report(instance, "skills")

    assert result.ok is True
    assert result.message == (
        "skills:\n"
        "window: all time\n"
        "total skills: 0\n"
        "used skills: 0\n"
        "never used skills: 0\n"
        "\n"
        "never used:\n"
        "  no unused skills\n"
        "\n"
        "per skill:\n"
        "  no skills recorded"
    )


def test_statistics_report_passes_window_only_when_flags_given(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    captured: dict[str, Any] = {}
    window = {"since": "2026-06-01T00:00:00+00:00", "until": "2026-07-01T00:00:00+00:00"}
    result_payload = _skills_report(_populated_skills_section(), window=window)
    monkeypatch.setattr(
        statistics_management.httpx, "post", _fake_post_returning(result_payload, captured)
    )

    result = statistics_management.statistics_report(
        instance, "skills", since="2026-06-01", until="2026-07-01"
    )

    assert captured["json"] == {
        "method": "statistics.report",
        "params": {"since": "2026-06-01", "until": "2026-07-01"},
    }
    assert result.ok is True
    assert "window: since=2026-06-01T00:00:00+00:00 until=2026-07-01T00:00:00+00:00" in (
        result.message
    )


def test_statistics_report_omits_absent_window_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    captured: dict[str, Any] = {}
    result_payload = _skills_report(_populated_skills_section())
    monkeypatch.setattr(
        statistics_management.httpx, "post", _fake_post_returning(result_payload, captured)
    )

    statistics_management.statistics_report(instance, "skills", since="2026-06-01")

    assert captured["json"] == {
        "method": "statistics.report",
        "params": {"since": "2026-06-01"},
    }


def test_statistics_overview_formats_from_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    captured: dict[str, Any] = {}
    result_payload = {
        "window": {"since": None, "until": None},
        "overview": {
            "total_agents": 2,
            "total_sessions": 5,
            "total_runs": 12,
            "open_run_groups": 1,
            "total_chat_messages": 85,
            "chat_messages_by_role": {"user": 40, "assistant": 45},
            "total_session_records": 200,
            "session_records_by_role": {
                "user": 40,
                "assistant": 45,
                "note": 55,
                "run_summary": 60,
            },
            "last_activity": "2026-07-01T10:00:00+00:00",
            "run_status": {"completed": 10, "failed": 1, "cancelled": 1},
            "average_run_duration_ms": 1234.5,
            "median_run_duration_ms": 900.0,
            "runs_with_tool_calls": 8,
            "total_tool_calls": 30,
            "agents": [
                {
                    "agent_id": "assistant",
                    "sessions": 5,
                    "runs": 12,
                    "chat_messages": 85,
                    "session_records": 200,
                    "errors": 2,
                    "last_activity": "2026-07-01T10:00:00+00:00",
                }
            ],
            "daily_trend": [],
        },
    }
    monkeypatch.setattr(
        statistics_management.httpx, "post", _fake_post_returning(result_payload, captured)
    )

    result = statistics_management.statistics_report(instance, "overview")

    assert captured["json"] == {"method": "statistics.report", "params": {}}
    assert result.ok is True
    assert "overview:" in result.message
    assert "agents: 2" in result.message
    assert "chat messages: 85" in result.message
    assert "stored session records: 200" in result.message
    assert "chat messages by role:\n  user: 40\n  assistant: 45" in result.message
    assert "stored session records by role:" in result.message
    assert "  note: 55" in result.message
    assert "  run_summary: 60" in result.message
    assert "run status: completed=10 failed=1 cancelled=1" in result.message
    assert (
        "  assistant: sessions=5 runs=12 chat_messages=85 session_records=200 errors=2 "
        "last_activity=2026-07-01T10:00:00+00:00"
    ) in result.message


def test_statistics_report_surfaces_rpc_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "ok": False,
                "error": {
                    "code": "invalid_request",
                    "message": "params.since must be an ISO 8601 timestamp string",
                },
            },
        )

    monkeypatch.setattr(statistics_management.httpx, "post", fake_post)

    result = statistics_management.statistics_report(instance, "skills", since="not-a-date")

    assert result == CommandResult(
        ok=False,
        message="invalid_request: params.since must be an ISO 8601 timestamp string",
        instance=instance,
    )


def test_statistics_report_reports_missing_section(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    captured: dict[str, Any] = {}
    # A report result without the requested section is a broken contract, not an
    # empty state — the CLI must fail explicitly rather than print nothing.
    result_payload = {"window": {"since": None, "until": None}}
    monkeypatch.setattr(
        statistics_management.httpx, "post", _fake_post_returning(result_payload, captured)
    )

    result = statistics_management.statistics_report(instance, "skills")

    assert result.ok is False
    assert result.message == "RPC result missing 'skills' section"


def test_run_dispatches_statistics_skills(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)
    calls: list[tuple[ServerInstance, str, str | None, str | None]] = []

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_statistics_report(
        resolved_instance: ServerInstance,
        section: str,
        since: str | None,
        until: str | None,
    ) -> CommandResult:
        calls.append((resolved_instance, section, since, until))
        return CommandResult(ok=True, message="skills:\ntotal skills: 0", instance=instance)

    exit_code = cli_main.run(
        ["statistics", "skills", "--since", "2026-06-01"],
        resolve=fake_resolve,
        statistics_report_fn=fake_statistics_report,
    )

    assert exit_code == 0
    assert calls == [(instance, "skills", "2026-06-01", None)]
    assert capsys.readouterr().out.splitlines() == ["skills:", "total skills: 0"]


def test_run_statistics_failure_exits_non_zero(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_statistics_report(
        resolved_instance: ServerInstance,
        section: str,
        since: str | None,
        until: str | None,
    ) -> CommandResult:
        return CommandResult(ok=False, message="invalid_request: bad window", instance=instance)

    exit_code = cli_main.run(
        ["statistics", "runs"],
        resolve=fake_resolve,
        statistics_report_fn=fake_statistics_report,
    )

    assert exit_code == 1
    assert capsys.readouterr().out.splitlines() == ["invalid_request: bad window"]
