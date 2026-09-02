"""Regression tests for the exact Provider reasoning-replay probe."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.providers.openai import OpenAIAdapter
from core.providers.token_getter import OAuthTokenGetter, StaticTokenGetter
from core.providers.token_store import OAuthToken, TokenStore
from core.providers.xai import XAIAdapter
from scripts.probe_reasoning_replay_exact import (
    _build_adapter,
    _build_tool_calls,
    _client_tools_expected,
    _encrypted_reasoning_expected,
)


def test_build_tool_calls_accepts_canonical_vbot_shape() -> None:
    calls = _build_tool_calls(
        [{"id": "call_1", "name": "get_weather", "arguments": {"city": "Berlin"}}]
    )

    assert len(calls) == 1
    assert calls[0].id == "call_1"
    assert calls[0].name == "get_weather"
    assert calls[0].arguments == {"city": "Berlin"}


def test_build_tool_calls_still_accepts_openai_wire_shape() -> None:
    calls = _build_tool_calls(
        [
            {
                "id": "call_2",
                "function": {
                    "name": "get_weather",
                    "arguments": '{"city":"Berlin"}',
                },
            }
        ]
    )

    assert len(calls) == 1
    assert calls[0].id == "call_2"
    assert calls[0].name == "get_weather"
    assert calls[0].arguments == {"city": "Berlin"}


def test_build_adapter_selects_xai_oauth_connection(tmp_path) -> None:
    TokenStore(tmp_path).save(
        "xai",
        "subscription",
        OAuthToken(
            access_token="oauth-access",
            refresh_token="oauth-refresh",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )

    built = _build_adapter("xai", "grok-4.6", data_dir=tmp_path)

    assert isinstance(built.adapter, XAIAdapter)
    assert isinstance(built.token_getter, OAuthTokenGetter)
    assert built.connection_id == "subscription"
    assert built.credential_source == "oauth"
    assert _encrypted_reasoning_expected(built.adapter, "grok-4.6") is True
    assert _encrypted_reasoning_expected(built.adapter, "grok-4.20-0309-non-reasoning") is False
    assert _client_tools_expected(built.adapter, "grok-4.6") is True
    assert _client_tools_expected(built.adapter, "grok-4.20-multi-agent-0309") is False


def test_build_adapter_honors_explicit_openai_subscription(tmp_path) -> None:
    TokenStore(tmp_path).save(
        "openai",
        "subscription",
        OAuthToken(
            access_token="header.payload.signature",
            refresh_token="oauth-refresh",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
    )

    built = _build_adapter(
        "openai",
        "gpt-5.6-luna",
        data_dir=tmp_path,
        connection_id="subscription",
    )

    assert isinstance(built.adapter, OpenAIAdapter)
    assert isinstance(built.token_getter, OAuthTokenGetter)
    assert built.connection_id == "subscription"


def test_build_adapter_keeps_api_key_connection_for_legacy_probe(tmp_path) -> None:
    (tmp_path / ".env").write_text("OLLAMA_API_KEY=secret\n", encoding="utf-8")

    built = _build_adapter(
        "ollama-cloud",
        "glm-5.3",
        data_dir=tmp_path,
        api_key_env="OLLAMA_API_KEY",
    )

    assert isinstance(built.token_getter, StaticTokenGetter)
    assert built.connection_id == "api-key"
    assert built.credential_source == "api_key:OLLAMA_API_KEY"
