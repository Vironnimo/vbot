"""Tests for session CLI parsing, RPC commands, and output."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import main as cli_main
from cli import session_management
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


def test_parse_args_supports_session_list() -> None:
    args = cli_main.parse_args(["session", "list", "assistant", "--port", "8700"])

    assert args.area == "session"
    assert args.command == "list"
    assert args.agent == "assistant"
    assert args.port == 8700


def test_parse_args_supports_session_create_options() -> None:
    args = cli_main.parse_args(
        ["session", "create", "assistant", "--id", "session-two", "--make-current"]
    )

    assert args.area == "session"
    assert args.command == "create"
    assert args.agent == "assistant"
    assert args.id == "session-two"
    assert args.make_current is True


def test_parse_args_supports_session_link_channel_options() -> None:
    args = cli_main.parse_args(
        [
            "session",
            "link-channel",
            "assistant",
            "session-one",
            "--channel",
            "tg-main",
            "--conversation",
            "12345",
        ]
    )

    assert args.area == "session"
    assert args.command == "link-channel"
    assert args.agent == "assistant"
    assert args.session == "session-one"
    assert args.channel == "tg-main"
    assert args.conversation == "12345"


def test_session_list_posts_rpc_and_formats_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "sessions": [
                        {
                            "id": "session-one",
                            "created_at": "2026-06-01T08:00:00+00:00",
                            "last_active_at": "2026-06-02T09:00:00+00:00",
                        },
                        {
                            "id": "session-two",
                            "created_at": "2026-06-03T10:00:00+00:00",
                            "last_active_at": "2026-06-03T11:00:00+00:00",
                            "source_channel_id": "tg-main",
                        },
                    ]
                },
            },
        )

    monkeypatch.setattr(session_management.httpx, "post", fake_post)

    result = session_management.session_list(instance, "assistant")

    assert result.ok is True
    assert result.message.splitlines()[1:] == [
        (
            "- id=session-one created_at=2026-06-01T08:00:00+00:00 "
            "last_active_at=2026-06-02T09:00:00+00:00"
        ),
        (
            "- id=session-two created_at=2026-06-03T10:00:00+00:00 "
            "last_active_at=2026-06-03T11:00:00+00:00 channel=tg-main"
        ),
    ]
    assert calls == [
        {
            "url": f"{instance.url}/api/rpc",
            "json": {
                "method": "session.list",
                "params": {
                    "agent_id": "assistant",
                    "limit": 100,
                    "include_subagents": True,
                    "include_memory_reflections": True,
                    "include_skill_reflections": True,
                    "include_cron": True,
                },
            },
            "timeout": 10.0,
        }
    ]


def test_session_list_reports_empty_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"sessions": []}})

    monkeypatch.setattr(session_management.httpx, "post", fake_post)

    result = session_management.session_list(instance, "assistant")

    assert result.ok is True
    assert result.instance is instance
    assert "assistant" in result.message


def test_session_list_follows_server_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []
    cursor = {
        "active_sort": 2460000,
        "agent_id": "assistant",
        "session_id": "session-one",
    }

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        if len(calls) == 1:
            result = {
                "sessions": [{"id": "session-one"}],
                "next_cursor": cursor,
                "total_count": 2,
            }
        else:
            result = {
                "sessions": [{"id": "session-two"}],
                "next_cursor": None,
                "total_count": 2,
            }
        return httpx.Response(200, json={"ok": True, "result": result})

    monkeypatch.setattr(session_management.httpx, "post", fake_post)

    result = session_management.session_list(instance, "assistant")

    assert result.ok is True
    assert "id=session-one" in result.message
    assert "id=session-two" in result.message
    assert calls[1]["params"]["cursor"] == cursor


def test_session_create_posts_optional_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"agent_id": "assistant", "session_id": "session-two"}},
        )

    monkeypatch.setattr(session_management.httpx, "post", fake_post)

    result = session_management.session_create(instance, "assistant", "session-two", True)

    assert result.ok is True
    assert result.instance is instance
    assert "session-two" in result.message
    assert "assistant" in result.message
    assert calls == [
        {
            "method": "session.create",
            "params": {
                "agent_id": "assistant",
                "session_id": "session-two",
                "make_current": True,
            },
        }
    ]


def test_session_create_omits_unset_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"agent_id": "assistant", "session_id": "generated-id"}},
        )

    monkeypatch.setattr(session_management.httpx, "post", fake_post)

    result = session_management.session_create(instance, "assistant", None, False)

    assert result.ok is True
    assert result.instance is instance
    assert "generated-id" in result.message
    assert "assistant" in result.message
    assert calls == [{"method": "session.create", "params": {"agent_id": "assistant"}}]


def test_session_link_channel_posts_link_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(200, json={"ok": True, "result": {"ok": True}})

    monkeypatch.setattr(session_management.httpx, "post", fake_post)

    result = session_management.session_link_channel(
        instance, "assistant", "session-one", "tg-main", "12345"
    )

    assert result.ok is True
    assert result.instance is instance
    assert "session-one" in result.message
    assert "tg-main" in result.message
    assert "12345" in result.message
    assert calls == [
        {
            "method": "session.link_channel",
            "params": {
                "agent_id": "assistant",
                "session_id": "session-one",
                "channel_id": "tg-main",
                "platform_conv_id": "12345",
            },
        }
    ]


def test_session_commands_surface_rpc_domain_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": False,
                "error": {"code": "not_found", "message": "Unknown agent: missing"},
            },
        )

    monkeypatch.setattr(session_management.httpx, "post", fake_post)

    result = session_management.session_list(instance, "missing")

    assert result.ok is False
    assert result.instance is instance
    assert result.message.startswith("not_found:")
    assert "missing" in result.message


def test_run_dispatches_session_list(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path, port=8765)

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {
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
        return httpx.Response(200, json={"ok": True, "result": {"sessions": []}})

    monkeypatch.setattr(session_management.httpx, "post", fake_post)

    exit_code = cli_main.run(
        ["session", "list", "assistant", "--port", "8765"],
        resolve=fake_resolve,
    )

    assert exit_code == 0


def test_parse_args_supports_session_delete() -> None:
    args = cli_main.parse_args(["session", "delete", "assistant", "session-one", "--yes"])

    assert args.area == "session"
    assert args.command == "delete"
    assert args.agent == "assistant"
    assert args.session == "session-one"
    assert args.yes is True


def test_session_delete_requires_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(200, json={"ok": True, "result": {}})

    monkeypatch.setattr(session_management.httpx, "post", fake_post)

    result = session_management.session_delete(instance, "assistant", "session-one", False)

    assert result.ok is False
    assert "--yes" in result.message
    # Refuses before any RPC call — nothing is deleted without confirmation.
    assert calls == []


def test_session_delete_posts_rpc_when_confirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
                    "agent_id": "assistant",
                    "session_id": "session-one",
                    "next_session_id": "session-two",
                },
            },
        )

    monkeypatch.setattr(session_management.httpx, "post", fake_post)

    result = session_management.session_delete(instance, "assistant", "session-one", True)

    assert result.ok is True
    assert "archived" in result.message
    assert "session-two" in result.message
    assert calls == [
        {
            "method": "session.delete",
            "params": {"agent_id": "assistant", "session_id": "session-one"},
        }
    ]


def test_session_fork_rename_and_policy_commands_post_verifiable_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        method = json["method"]
        result: dict[str, Any]
        if method == "session.fork":
            result = {
                "session": {
                    "id": "session-fork",
                    "agent_id": "reviewer@vbot",
                    "fork_source": {"session_id": "session-one"},
                }
            }
        elif method == "session.rename":
            result = {
                "agent_id": "assistant",
                "session_id": "session-one",
                "title": "Research notes",
            }
        else:
            result = {
                "agent_id": "assistant",
                "session_id": "session-one",
                "override": {"enabled": False},
                "effective": {"enabled": False},
                "source": "session",
            }
        return httpx.Response(200, json={"ok": True, "result": result})

    monkeypatch.setattr(session_management.httpx, "post", fake_post)

    forked = session_management.session_fork(instance, "assistant", "session-one", "reviewer@vbot")
    renamed = session_management.session_rename(
        instance, "assistant", "session-one", "Research notes"
    )
    policy = session_management.session_set_compaction_policy(
        instance, "assistant", "session-one", {"enabled": False}
    )

    assert "session-fork" in forked.message
    assert "title: Research notes" in renamed.message
    assert "source: session" in policy.message
    assert calls == [
        {
            "method": "session.fork",
            "params": {
                "agent_id": "assistant",
                "session_id": "session-one",
                "target_agent_id": "reviewer@vbot",
            },
        },
        {
            "method": "session.rename",
            "params": {
                "agent_id": "assistant",
                "session_id": "session-one",
                "title": "Research notes",
            },
        },
        {
            "method": "session.set_compaction_policy",
            "params": {
                "agent_id": "assistant",
                "session_id": "session-one",
                "policy": {"enabled": False},
            },
        },
    ]


def test_parse_args_supports_session_policy_clear() -> None:
    args = cli_main.parse_args(
        ["session", "set-compaction-policy", "assistant", "session-one", "--clear"]
    )

    assert args.command == "set-compaction-policy"
    assert args.policy is None
    assert args.clear is True
