"""Tests for prompt CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import main as cli_main
from cli import prompt_management
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


def test_prompt_list_posts_rpc_and_formats_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert url == f"{instance.url}/api/rpc"
        assert json == {"method": "prompt.list", "params": {}}
        assert timeout == 10.0
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "blocks": [
                        {
                            "id": "core:tools",
                            "owner": "always",
                            "kind": "text",
                            "editable": True,
                            "enabled": True,
                            "rank": 0,
                            "source": "core",
                            "text": "# Tools",
                            "is_modified": False,
                        },
                        {
                            "id": "user:my-rules",
                            "owner": "always",
                            "kind": "text",
                            "editable": True,
                            "enabled": True,
                            "rank": 1,
                            "source": "user",
                            "text": "# Rules",
                            "is_modified": True,
                        },
                        {
                            "id": "memory:guidance",
                            "owner": "memory",
                            "kind": "data",
                            "editable": False,
                            "enabled": False,
                            "rank": 2,
                            "source": "memory",
                        },
                    ],
                    "scopes": [{"type": "default", "label": "Default"}],
                },
            },
        )

    monkeypatch.setattr(prompt_management.httpx, "post", fake_post)

    result = prompt_management.prompt_list(instance)

    assert result == CommandResult(
        ok=True,
        message=(
            "prompts:\n"
            "- core:tools owner=always kind=text enabled=yes editable=yes "
            "source=core modified=no\n"
            "- user:my-rules owner=always kind=text enabled=yes editable=yes "
            "source=user modified=yes\n"
            "- memory:guidance owner=memory kind=data enabled=no editable=no "
            "source=memory modified=-"
        ),
        instance=instance,
    )


def test_prompt_list_reports_empty_block_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"blocks": [], "scopes": []}})

    monkeypatch.setattr(prompt_management.httpx, "post", fake_post)

    result = prompt_management.prompt_list(instance)

    assert result == CommandResult(ok=True, message="no prompt blocks", instance=instance)


def test_prompt_update_posts_rpc(
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
                "result": {"id": "core:tools", "text": "# Custom tools", "is_modified": True},
            },
        )

    monkeypatch.setattr(prompt_management.httpx, "post", fake_post)

    result = prompt_management.prompt_update(instance, "core:tools", "# Custom tools")

    assert result == CommandResult(ok=True, message="updated core:tools", instance=instance)
    assert calls == [
        {"method": "prompt.update", "params": {"id": "core:tools", "content": "# Custom tools"}}
    ]


def test_prompt_reset_posts_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "prompt.reset", "params": {"id": "core:skills"}}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"id": "core:skills", "text": "# Skills", "is_modified": False},
            },
        )

    monkeypatch.setattr(prompt_management.httpx, "post", fake_post)

    result = prompt_management.prompt_reset(instance, "core:skills")

    assert result == CommandResult(ok=True, message="reset core:skills", instance=instance)


def test_prompt_preview_posts_rpc_and_includes_rendered_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {"method": "prompt.preview", "params": {"agent_id": "coder"}}
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"text": "System for coder", "tokens": 12, "estimated": True},
            },
        )

    monkeypatch.setattr(prompt_management.httpx, "post", fake_post)

    result = prompt_management.prompt_preview(instance, "coder")

    assert result == CommandResult(
        ok=True,
        message="tokens: 12 estimated=yes\n---\nSystem for coder",
        instance=instance,
    )


def test_parse_args_supports_prompt_update_file() -> None:
    args = cli_main.parse_args(["prompt", "update", "core:tools", "--file", "tools.txt"])

    assert args.area == "prompt"
    assert args.command == "update"
    assert args.block_id == "core:tools"
    assert args.content_file == "tools.txt"


def test_run_dispatches_prompt_update_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)
    content_file = tmp_path / "tools.txt"
    content_file.write_text("# Custom tools", encoding="utf-8")
    calls: list[tuple[str, Any]] = []

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_update(
        resolved_instance: ServerInstance,
        block_id: str,
        content: str,
    ) -> CommandResult:
        calls.append(
            ("update", {"instance": resolved_instance, "block_id": block_id, "content": content})
        )
        return CommandResult(ok=True, message="updated core:tools", instance=resolved_instance)

    exit_code = cli_main.run(
        ["prompt", "update", "core:tools", "--file", str(content_file)],
        resolve=fake_resolve,
        update_prompt_fn=fake_update,
    )

    assert exit_code == 0
    assert calls == [
        ("update", {"instance": instance, "block_id": "core:tools", "content": "# Custom tools"})
    ]
    assert capsys.readouterr().out.splitlines() == ["updated core:tools"]
