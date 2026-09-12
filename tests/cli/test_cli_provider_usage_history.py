"""Tests for cli provider usage history."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import main as cli_main
from cli import provider_management
from tests.cli.cli_provider_test_support import (
    make_instance,
)


def test_provider_usage_history_formats_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {
            "method": "provider.usage_history",
            "params": {"since": "2026-08-01T00:00:00Z"},
        }
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "generated_at": "2026-08-25T12:00:00+00:00",
                    "samples": [
                        {
                            "sampled_at": "2026-08-01T10:00:00+00:00",
                            "providers": [
                                {
                                    "connection": "openai:subscription",
                                    "windows": [
                                        {"label": "5h", "used_percent": 42.0, "reset_at": "-"}
                                    ],
                                },
                                {"connection": "broken:api-key", "error": "timeout"},
                            ],
                        }
                    ],
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_usage_history(instance, since="2026-08-01T00:00:00Z")

    assert result.ok is True
    assert result.message.splitlines() == [
        "provider usage history:",
        "generated_at: 2026-08-25T12:00:00+00:00",
        "- 2026-08-01T10:00:00+00:00",
        "  openai:subscription: - 5h: used=42% remaining=58% reset_at=-",
        "  broken:api-key: error: timeout",
    ]


def test_provider_usage_history_reports_empty_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={"ok": True, "result": {"generated_at": "t0", "samples": []}},
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_usage_history(instance)

    assert result.ok is True
    assert "no recorded usage samples" in result.message


def test_provider_usage_history_clear_requires_confirmation(tmp_path: Path) -> None:
    instance = make_instance(tmp_path)

    result = provider_management.provider_usage_history_clear(instance, False)

    assert result.ok is False
    assert "--yes" in result.message


def test_provider_usage_history_clear_posts_clear_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = make_instance(tmp_path)
    calls: list[dict[str, Any]] = []

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        calls.append(json)
        return httpx.Response(
            200,
            json={"ok": True, "result": {"deleted_samples": 12, "deleted_files": 2}},
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_usage_history_clear(instance, True)

    assert result.ok is True
    assert result.message == "deleted provider usage history: 12 samples across 2 files"
    assert calls == [{"method": "provider.usage_history.clear", "params": {}}]


def test_parse_args_supports_usage_history_commands() -> None:
    history = cli_main.parse_args(["provider", "usage-history"])
    clear = cli_main.parse_args(["provider", "usage-history-clear", "--yes"])

    assert (history.command, history.since, history.until) == ("usage-history", None, None)
    assert (clear.command, clear.yes) == ("usage-history-clear", True)
