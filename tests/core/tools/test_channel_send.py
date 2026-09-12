"""Channel send: contract behavior."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from core.tools.channel import (
    CHANNEL_SEND_TOOL_DESCRIPTION,
    CHANNEL_SEND_TOOL_NAME,
    CHANNEL_SEND_TOOL_PARAMETERS,
    register_channel_send_tool,
)
from core.tools.tools import (
    ToolDefinitionProfileContext,
    ToolRegistry,
)
from tests.core.tools.channel_send_helpers import (
    _TEST_MAX_ATTACHMENT_SIZE_BYTES,
    make_channel_config,
    make_chat_sessions,
    make_context,
)


def test_channel_send_agent_guidance_requires_tool_for_channel_files() -> None:
    assert CHANNEL_SEND_TOOL_DESCRIPTION
    assert CHANNEL_SEND_TOOL_PARAMETERS["required"] == ["channel_id"]
    assert "request" not in CHANNEL_SEND_TOOL_PARAMETERS["properties"]
    assert "action" not in CHANNEL_SEND_TOOL_PARAMETERS["properties"]
    properties = CHANNEL_SEND_TOOL_PARAMETERS["properties"]
    assert isinstance(properties, dict)
    assert all(
        isinstance(property_schema.get("description"), str) and property_schema["description"]
        for property_schema in properties.values()
    )
    file_paths = properties["file_paths"]
    assert isinstance(file_paths, dict)
    buttons = properties["buttons"]
    assert isinstance(buttons, dict)
    button_properties = buttons["items"]["items"]["properties"]
    assert all(
        isinstance(property_schema.get("description"), str) and property_schema["description"]
        for property_schema in button_properties.values()
    )


def test_channel_send_profile_uses_enabled_agent_channels_and_platform_capabilities(
    tmp_path: Path,
) -> None:
    channel_service = Mock()
    channel_service.list_channels.return_value = [
        make_channel_config(channel_id="tg-primary"),
        make_channel_config(channel_id="discord-primary", platform="discord"),
        make_channel_config(channel_id="tg-disabled", enabled=False),
        make_channel_config(channel_id="tg-other", agent_id="agent-2"),
    ]
    registry = ToolRegistry()
    register_channel_send_tool(
        registry,
        channel_service,
        make_chat_sessions(),
        max_attachment_size_bytes=_TEST_MAX_ATTACHMENT_SIZE_BYTES,
    )
    profile_context = ToolDefinitionProfileContext(agent_id="agent-1")

    first = registry.provider_definitions(
        [CHANNEL_SEND_TOOL_NAME],
        profile_context=profile_context,
    )
    second = registry.provider_definitions(
        [CHANNEL_SEND_TOOL_NAME],
        profile_context=profile_context,
    )

    assert first == second
    parameters = first[0]["parameters"]
    assert "oneOf" not in parameters
    assert "anyOf" not in parameters
    assert "not" not in parameters
    assert "additionalProperties" not in str(parameters)
    assert parameters["properties"]["channel_id"]["enum"] == [
        "discord-primary",
        "tg-primary",
    ]
    assert set(parameters["properties"]) == {
        "channel_id",
        "message",
        "platform_target",
        "thread_id",
        "file_paths",
        "buttons",
    }
    assert all(value in first[0]["description"] for value in ("discord-primary", "tg-primary"))

    contract = registry.contracts_for_provider_definitions(first)[CHANNEL_SEND_TOOL_NAME]
    result = asyncio.run(
        registry.dispatch(
            replace(make_context(tmp_path), input_contract=contract),
            {
                "channel_id": "discord-primary",
                "message": "Hello",
                "buttons": [[{"label": "Go", "data": "run:go"}]],
            },
            [CHANNEL_SEND_TOOL_NAME],
        )
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"


def test_channel_send_discord_profile_omits_telegram_only_fields() -> None:
    channel_service = Mock()
    channel_service.list_channels.return_value = [
        make_channel_config(channel_id="discord-primary", platform="discord")
    ]
    registry = ToolRegistry()
    register_channel_send_tool(
        registry,
        channel_service,
        make_chat_sessions(),
        max_attachment_size_bytes=_TEST_MAX_ATTACHMENT_SIZE_BYTES,
    )

    definition = registry.provider_definitions(
        [CHANNEL_SEND_TOOL_NAME],
        profile_context=ToolDefinitionProfileContext(agent_id="agent-1"),
    )[0]

    assert set(definition["parameters"]["properties"]) == {
        "channel_id",
        "message",
        "platform_target",
        "file_paths",
    }
    assert "discord-primary" in definition["description"]


def test_channel_send_profile_hides_tool_without_enabled_owned_channel() -> None:
    channel_service = Mock()
    channel_service.list_channels.return_value = [
        make_channel_config(channel_id="tg-disabled", enabled=False),
        make_channel_config(channel_id="tg-other", agent_id="agent-2"),
    ]
    registry = ToolRegistry()
    register_channel_send_tool(
        registry,
        channel_service,
        make_chat_sessions(),
        max_attachment_size_bytes=_TEST_MAX_ATTACHMENT_SIZE_BYTES,
    )
    profile_context = ToolDefinitionProfileContext(agent_id="agent-1")

    assert (
        registry.provider_definitions(
            [CHANNEL_SEND_TOOL_NAME],
            profile_context=profile_context,
        )
        == []
    )
    assert (
        registry.prompt_definitions(
            [CHANNEL_SEND_TOOL_NAME],
            profile_context=profile_context,
        )
        == []
    )
