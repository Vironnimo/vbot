"""Tests for extensions catalog and enable/disable CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import extensions_management
from cli import main as cli_main
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


def _extensions_payload() -> list[dict[str, Any]]:
    return [
        {
            "name": "guard_bash",
            "status": "loaded",
            "disabled": False,
            "version": "1.2.0",
            "description": "Guards dangerous bash",
            "error": None,
            "config": {"deny": ["rm -rf"]},
            "capability_errors": [],
            "capabilities": {
                "hooks": {"tool_call": 1},
                "tools": ["word_count"],
                "recall_backends": [],
                "startup": True,
                "shutdown": False,
            },
        },
        {
            "name": "broken",
            "status": "failed",
            "disabled": False,
            "version": None,
            "description": None,
            "error": "import failed: boom",
            "config": {},
            "capability_errors": [],
            "capabilities": {
                "hooks": {},
                "tools": [],
                "recall_backends": [],
                "startup": False,
                "shutdown": False,
            },
        },
        {
            "name": "legacy",
            "status": "disabled",
            "disabled": True,
            "version": None,
            "description": None,
            "error": None,
            "config": {},
            "capability_errors": [],
            "capabilities": {
                "hooks": {},
                "tools": [],
                "recall_backends": [],
                "startup": False,
                "shutdown": False,
            },
        },
    ]


def test_extensions_list_formats_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert json == {"method": "extensions.list", "params": {}}
        return httpx.Response(
            200, json={"ok": True, "result": {"extensions": _extensions_payload()}}
        )

    monkeypatch.setattr(extensions_management.httpx, "post", fake_post)

    result = extensions_management.extensions_list(instance)

    assert result.ok is True
    assert result.message == "\n".join(
        [
            "extensions:",
            "- guard_bash  loaded  v1.2.0  Guards dangerous bash",
            "    hooks: tool_call(1); tools: word_count; startup",
            "- broken  failed",
            "    error: import failed: boom",
            "- legacy  disabled",
        ]
    )


def test_extensions_disable_writes_settings_and_prints_restart_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    posted: list[dict[str, Any]] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        posted.append(json)
        if json["method"] == "extensions.list":
            return httpx.Response(
                200, json={"ok": True, "result": {"extensions": _extensions_payload()}}
            )
        assert json == {
            "method": "settings.update",
            "params": {
                "extensions": {
                    "disabled": ["legacy", "guard_bash"],
                    "config": {"guard_bash": {"deny": ["rm -rf"]}},
                }
            },
        }
        return httpx.Response(200, json={"ok": True, "result": {"restart_required": True}})

    monkeypatch.setattr(extensions_management.httpx, "post", fake_post)

    result = extensions_management.extensions_disable(instance, "guard_bash")

    assert result == CommandResult(
        ok=True,
        message="extension 'guard_bash' disabled\n"
        "restart required: run 'vbot server restart' to apply",
        instance=instance,
    )
    assert [call["method"] for call in posted] == ["extensions.list", "settings.update"]


def test_extensions_enable_removes_from_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        if json["method"] == "extensions.list":
            return httpx.Response(
                200, json={"ok": True, "result": {"extensions": _extensions_payload()}}
            )
        assert json["params"]["extensions"]["disabled"] == []
        return httpx.Response(200, json={"ok": True, "result": {"restart_required": True}})

    monkeypatch.setattr(extensions_management.httpx, "post", fake_post)

    result = extensions_management.extensions_enable(instance, "legacy")

    assert result.ok is True
    assert result.message.splitlines()[0] == "extension 'legacy' enabled"


def test_extensions_disable_unknown_name_suggests_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        assert json["method"] == "extensions.list"
        return httpx.Response(
            200, json={"ok": True, "result": {"extensions": _extensions_payload()}}
        )

    monkeypatch.setattr(extensions_management.httpx, "post", fake_post)

    result = extensions_management.extensions_disable(instance, "guard_bas")

    assert result.ok is False
    assert "extension 'guard_bas' not found" in result.message
    assert "did you mean: guard_bash" in result.message


def test_extensions_disable_already_disabled_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)
    posted: list[str] = []

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        posted.append(json["method"])
        return httpx.Response(
            200, json={"ok": True, "result": {"extensions": _extensions_payload()}}
        )

    monkeypatch.setattr(extensions_management.httpx, "post", fake_post)

    result = extensions_management.extensions_disable(instance, "legacy")

    assert result.ok is True
    assert result.message == "extension 'legacy' is already disabled (no change)"
    assert posted == ["extensions.list"]


def test_run_dispatches_extensions_disable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance = make_instance(tmp_path)
    calls: list[tuple[ServerInstance, str]] = []

    def fake_resolve(*, host: str, port: int | None, data_dir: str | None) -> ServerInstance:
        return instance

    def fake_disable(resolved_instance: ServerInstance, name: str) -> CommandResult:
        calls.append((resolved_instance, name))
        return CommandResult(ok=True, message="extension 'guard_bash' disabled", instance=instance)

    exit_code = cli_main.run(
        ["extensions", "disable", "guard_bash"],
        resolve=fake_resolve,
        disable_extension_fn=fake_disable,
    )

    assert exit_code == 0
    assert calls == [(instance, "guard_bash")]
    assert capsys.readouterr().out.splitlines() == ["extension 'guard_bash' disabled"]
