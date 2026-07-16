"""Tests for Channels-owned ``channel.json`` validation."""

from __future__ import annotations

from core.channels import validate_channel_data

_AGENT_ID_SLUG_ERROR = "must be 1-64 characters using only letters, numbers, hyphen, or underscore"


def _diagnostics(data: object) -> list[tuple[str, str, str]]:
    return [
        (diagnostic.severity, diagnostic.path, diagnostic.message)
        for diagnostic in validate_channel_data(data)
    ]


def test_validate_channel_data_rejects_non_boolean_observe_unaddressed() -> None:
    diagnostics = _diagnostics(
        {
            "id": "tg-assistant",
            "platform": "telegram",
            "agent_id": "assistant",
            "token_env_var": "TELEGRAM_BOT_TOKEN",
            "observe_unaddressed": "true",
        }
    )

    assert diagnostics == [("error", "$.observe_unaddressed", "must be a boolean")]


def test_validate_channel_data_rejects_path_traversal_agent_id() -> None:
    diagnostics = _diagnostics(
        {
            "id": "tg-assistant",
            "platform": "telegram",
            "agent_id": "../escape",
            "token_env_var": "TELEGRAM_BOT_TOKEN",
        }
    )

    assert diagnostics == [("error", "$.agent_id", _AGENT_ID_SLUG_ERROR)]
