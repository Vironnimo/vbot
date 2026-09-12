"""Tests for cli provider oauth."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from cli import main as cli_main
from cli import provider_management
from cli.server_management import CommandResult
from tests.cli.cli_provider_test_support import (
    make_instance,
)


def test_parse_args_supports_provider_oauth_commands() -> None:
    connect_args = cli_main.parse_args(
        ["provider", "connect", "openai", "--connection", "openai:subscription"]
    )
    status_args = cli_main.parse_args(
        ["provider", "connect-status", "openai", "--connection", "openai:subscription"]
    )

    assert connect_args.command == "connect"
    assert connect_args.provider == "openai"
    assert connect_args.connection == "openai:subscription"
    assert status_args.command == "connect-status"


def test_provider_connect_prints_device_flow_instructions(
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
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://example.com/device",
                    "expires_in": 900,
                    "account": "default",
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_connect(instance, "openai", "openai:subscription")

    assert result.ok is True
    assert "openai:subscription" in result.message
    assert "account: default" in result.message
    assert "user_code: ABCD-1234" in result.message
    assert "verification_uri: https://example.com/device" in result.message
    assert "expires_in_seconds: 900" in result.message
    assert "provider connect-status openai --connection openai:subscription" in result.message
    assert calls == [
        {
            "method": "provider.connect",
            "params": {"provider_id": "openai", "connection_id": "openai:subscription"},
        }
    ]


def test_provider_connect_passes_account_and_suggests_account_status_command(
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
                    "user_code": "ABCD-1234",
                    "verification_uri": "https://example.com/device",
                    "expires_in": 900,
                    "account": "work",
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_connect(
        instance, "openai", "openai:subscription", account="work"
    )

    assert result.ok is True
    lines = result.message.splitlines()
    assert "openai:subscription" in result.message
    assert "account: work" in result.message
    assert (
        "provider connect-status openai --connection openai:subscription --account work"
        in lines[-1]
    )
    assert calls == [
        {
            "method": "provider.connect",
            "params": {
                "provider_id": "openai",
                "connection_id": "openai:subscription",
                "account": "work",
            },
        }
    ]


def test_provider_disconnect_posts_disconnect_rpc(
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
                    "provider_id": "openai",
                    "connection_id": "openai:subscription",
                    "account": "default",
                    "status": "disconnected",
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_disconnect(instance, "openai", "openai:subscription")

    assert result.ok is True
    assert result.instance is instance
    assert "openai:subscription" in result.message
    assert "account: default" in result.message
    assert calls == [
        {
            "method": "provider.disconnect",
            "params": {"provider_id": "openai", "connection_id": "openai:subscription"},
        }
    ]


def test_provider_disconnect_passes_account_through(
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
                    "provider_id": "openai",
                    "connection_id": "openai:subscription",
                    "account": "work",
                    "status": "disconnected",
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_disconnect(
        instance, "openai", "openai:subscription", account="work"
    )

    assert result.ok is True
    assert result.instance is instance
    assert "openai:subscription" in result.message
    assert "account: work" in result.message
    assert calls == [
        {
            "method": "provider.disconnect",
            "params": {
                "provider_id": "openai",
                "connection_id": "openai:subscription",
                "account": "work",
            },
        }
    ]


def test_provider_connect_status_formats_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {
            "method": "provider.connection_status",
            "params": {"provider_id": "openai", "connection_id": "openai:subscription"},
        }
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"connected": True, "flow_active": False, "account": "default"},
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_connect_status(instance, "openai", "openai:subscription")

    assert result == CommandResult(
        ok=True,
        message="openai:subscription: account=default connected=yes flow_active=no",
        instance=instance,
    )


def test_provider_connect_status_passes_account_through(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance = make_instance(tmp_path)

    def fake_post(
        url: str, *, json: dict[str, Any], timeout: float, trust_env: bool
    ) -> httpx.Response:
        assert json == {
            "method": "provider.connection_status",
            "params": {
                "provider_id": "openai",
                "connection_id": "openai:subscription",
                "account": "work",
            },
        }
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"connected": False, "flow_active": True, "account": "work"},
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_connect_status(
        instance, "openai", "openai:subscription", account="work"
    )

    assert result == CommandResult(
        ok=True,
        message="openai:subscription: account=work connected=no flow_active=yes",
        instance=instance,
    )


def test_provider_oauth_commands_surface_rpc_errors(
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
                "error": {
                    "code": "oauth_not_supported",
                    "message": "provider connection 'openai:api-key' is not an OAuth connection",
                },
            },
        )

    monkeypatch.setattr(provider_management.httpx, "post", fake_post)

    result = provider_management.provider_connect(instance, "openai", "openai:api-key")

    assert result.ok is False
    assert result.instance is instance
    assert result.message.startswith("oauth_not_supported:")
    assert "openai:api-key" in result.message
