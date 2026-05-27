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

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert url == f"{instance.url}/api/rpc"
        assert json == {"method": "prompt.list", "params": {}}
        assert timeout == 10.0
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "fragments": [
                        {
                            "name": "system.md",
                            "is_modified": False,
                            "variables": [{"placeholder": "{app_version}"}],
                        },
                        {"name": "tools.md", "is_modified": True, "variables": []},
                    ]
                },
            },
        )

    monkeypatch.setattr(prompt_management.httpx, "post", fake_post)

    result = prompt_management.prompt_list(instance)

    assert result == CommandResult(
        ok=True,
        message=(
            "prompts:\n"
            "- system.md modified=no variables={app_version}\n"
            "- tools.md modified=yes variables=-"
        ),
        instance=instance,
    )


def test_prompt_update_posts_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        calls.append(json)
        return httpx.Response(200, json={"ok": True, "result": {"name": "tools.md"}})

    monkeypatch.setattr(prompt_management.httpx, "post", fake_post)

    result = prompt_management.prompt_update(instance, "tools.md", "# Custom tools")

    assert result == CommandResult(ok=True, message="updated tools.md", instance=instance)
    assert calls == [
        {"method": "prompt.update", "params": {"name": "tools.md", "content": "# Custom tools"}}
    ]


def test_prompt_reset_posts_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert json == {"method": "prompt.reset", "params": {"name": "skills.md"}}
        return httpx.Response(200, json={"ok": True, "result": {"name": "skills.md"}})

    monkeypatch.setattr(prompt_management.httpx, "post", fake_post)

    result = prompt_management.prompt_reset(instance, "skills.md")

    assert result == CommandResult(ok=True, message="reset skills.md", instance=instance)


def test_prompt_preview_posts_rpc_and_includes_rendered_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
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
    args = cli_main.parse_args(["prompt", "update", "--name", "tools.md", "--file", "tools.txt"])

    assert args.area == "prompt"
    assert args.command == "update"
    assert args.name == "tools.md"
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
        name: str,
        content: str,
    ) -> CommandResult:
        calls.append(("update", {"instance": resolved_instance, "name": name, "content": content}))
        return CommandResult(ok=True, message="updated tools.md", instance=resolved_instance)

    exit_code = cli_main.run(
        ["prompt", "update", "--name", "tools.md", "--file", str(content_file)],
        resolve=fake_resolve,
        update_prompt_fn=fake_update,
    )

    assert exit_code == 0
    assert calls == [
        ("update", {"instance": instance, "name": "tools.md", "content": "# Custom tools"})
    ]
    assert capsys.readouterr().out.splitlines() == ["updated tools.md"]
