"""channel_send's definition: the per-Agent profile, the fields each platform offers,
exact Channel ids, and how a call is labeled."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.tools.channel import CHANNEL_SEND_TOOL_DESCRIPTION, CHANNEL_SEND_TOOL_NAME
from core.tools.tools import ToolDefinitionProfileContext
from tests.core.tools.channel_send_test_support import (
    channel_send,
    delivered,
    make_channel_config,
    options,
    refused,
)

ALL_FIELDS = {"channel_id", "message", "platform_target", "thread_id", "file_paths", "buttons"}


def test_profile_offers_the_agents_enabled_channels(tmp_path: Path) -> None:
    tool = channel_send(
        tmp_path,
        make_channel_config(channel_id="tg-main"),
        make_channel_config(channel_id="dc-team", platform="discord"),
        make_channel_config(channel_id="tg-disabled", enabled=False),
        make_channel_config(channel_id="tg-other", agent_id="agent-2"),
    )

    definition = tool.definition()

    assert definition is not None
    assert definition == tool.definition()
    assert definition["description"] == (
        f"{CHANNEL_SEND_TOOL_DESCRIPTION} Channels: dc-team (Discord), tg-main (Telegram)."
    )
    assert "final reply already reaches the chat you are answering" in definition["description"]
    parameters = definition["parameters"]
    # One Channel is enough to know where to send; the Tool asks when there are several.
    assert set(parameters) == {"type", "properties"}
    assert "additionalProperties" not in str(parameters)
    properties = parameters["properties"]
    assert set(properties) == ALL_FIELDS
    assert properties["channel_id"]["enum"] == ["dc-team", "tg-main"]
    button_fields = properties["buttons"]["items"]["items"]["properties"]
    assert all(field["description"] for field in [*properties.values(), *button_fields.values()])


@pytest.mark.parametrize(
    ("platform", "fields"),
    [
        ("discord", {"channel_id", "message", "platform_target", "file_paths"}),
        ("slack", {"channel_id", "message", "platform_target", "thread_id", "file_paths"}),
        ("mattermost", {"channel_id", "message", "platform_target", "thread_id", "file_paths"}),
        ("whatsapp", {"channel_id", "message", "platform_target", "file_paths"}),
    ],
)
def test_each_platform_offers_the_fields_it_delivers(
    tmp_path: Path, platform: str, fields: set[str]
) -> None:
    tool = channel_send(
        tmp_path,
        make_channel_config(channel_id="work", platform=platform, allowed_chat_ids=["C1"]),
    )

    definition = tool.definition()
    envelope = tool.call({"message": "Done"})

    assert definition is not None
    assert set(definition["parameters"]["properties"]) == fields
    assert delivered(envelope) == {"channel_id": "work", "platform_target": "C1"}
    assert tool.sent() == [("work", "Done", "C1", options())]


def test_profile_hides_the_tool_without_an_enabled_owned_channel(tmp_path: Path) -> None:
    tool = channel_send(
        tmp_path,
        make_channel_config(channel_id="tg-disabled", enabled=False),
        make_channel_config(channel_id="tg-other", agent_id="agent-2"),
    )
    profile_context = ToolDefinitionProfileContext(agent_id="agent-1")

    assert (
        tool.registry.provider_definitions(
            [CHANNEL_SEND_TOOL_NAME], profile_context=profile_context
        )
        == []
    )
    assert (
        tool.registry.prompt_definitions([CHANNEL_SEND_TOOL_NAME], profile_context=profile_context)
        == []
    )


@pytest.mark.parametrize("requested", ["TG-TEAM-A", "tg_team_a", "telegrm"])
def test_channel_ids_are_never_matched_loosely(tmp_path: Path, requested: str) -> None:
    tool = channel_send(tmp_path, make_channel_config(channel_id="tg-team-a"))

    envelope = tool.call({"channel_id": requested, "message": "Hi", "platform_target": "123"})

    assert refused(envelope) == (
        f'channel_send was not run: "channel_id" must be one of "tg-team-a"; '
        f'received "{requested}".'
    )
    assert tool.sent() == []


@pytest.mark.parametrize(
    ("arguments", "labels"),
    [
        ({"channel": "telegram", "text": "Hello there"}, ["telegram", "Hello there"]),
        # A call the Tool refuses is labeled as the Model wrote it.
        ({"action": "delete", "message": "Hi"}, ["Hi"]),
    ],
    ids=["other-spellings", "refused-call"],
)
def test_display_labels_what_the_call_meant(
    tmp_path: Path, arguments: dict[str, Any], labels: list[str]
) -> None:
    builder = channel_send(tmp_path).registry.get(CHANNEL_SEND_TOOL_NAME).display.parts_builder
    assert builder is not None

    assert [part.value for part in builder(arguments)] == labels
