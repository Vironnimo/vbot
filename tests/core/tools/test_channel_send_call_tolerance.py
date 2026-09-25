"""Channel send: other messaging dialects, targets, and refusals that name the next call.

Every call runs through production dispatch with the calling Agent's Definition Profile
contract, as a Provider cycle would, against a recording Channel service.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from core.providers.adapter import tool_result_text
from core.tools.channel import CHANNEL_SEND_TOOL_NAME, register_channel_send_tool
from core.tools.tools import ToolDefinitionProfileContext, ToolRegistry, tool_failure
from tests.core.tools.channel_send_helpers import (
    make_channel_config,
    make_chat_sessions,
    make_context,
)


@dataclass
class ChannelSend:
    registry: ToolRegistry
    service: Mock
    sessions: Mock
    workspace: Path

    def definition(self) -> dict[str, Any]:
        [definition] = self.registry.provider_definitions(
            [CHANNEL_SEND_TOOL_NAME],
            profile_context=ToolDefinitionProfileContext(agent_id="agent-1"),
        )
        return definition

    def call(self, arguments: Any) -> tuple[dict[str, Any], str]:
        contracts = self.registry.contracts_for_provider_definitions([self.definition()])
        context = replace(
            make_context(self.workspace), input_contract=contracts[CHANNEL_SEND_TOOL_NAME]
        )
        try:
            envelope = asyncio.run(
                self.registry.dispatch(context, arguments, [CHANNEL_SEND_TOOL_NAME])
            )
        except ValueError as error:
            envelope = tool_failure("invalid_arguments", str(error))
        return envelope, str(tool_result_text(json.dumps(envelope, ensure_ascii=False)))

    def sent(self) -> list[tuple[Any, ...]]:
        return [
            (call.args[0], call.args[1], call.args[2], call.kwargs)
            for call in self.service.send.await_args_list
        ]


def channel_send(
    tmp_path: Path, *configs: Mock, reply_target: dict[str, Any] | None = None
) -> ChannelSend:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    service = Mock()
    service.send = AsyncMock()
    service.ensure_outbound_session = AsyncMock(side_effect=RuntimeError("not recorded"))
    service.list_channels.return_value = list(configs) or [
        make_channel_config(channel_id="tg-main", allowed_chat_ids=[111])
    ]
    sessions = make_chat_sessions()
    sessions.get_metadata.return_value = (
        {"last_reply_target": reply_target} if reply_target is not None else {}
    )
    registry = ToolRegistry()
    register_channel_send_tool(registry, service, sessions, max_attachment_size_bytes=1000)
    return ChannelSend(registry, service, sessions, workspace)


def options(**values: Any) -> dict[str, Any]:
    return {"files": None, "thread_id": None, "buttons": None, **values}


class TestDefinition:
    def test_description_lists_the_channels_by_platform(self, tmp_path: Path) -> None:
        tool = channel_send(
            tmp_path,
            make_channel_config(channel_id="tg-main"),
            make_channel_config(channel_id="dc-team", platform="discord"),
        )

        definition = tool.definition()

        assert definition["description"].endswith(
            " Channels: dc-team (Discord), tg-main (Telegram)."
        )
        assert "required" not in definition["parameters"]

    @pytest.mark.parametrize(
        ("platform", "fields"),
        (
            ("slack", {"channel_id", "message", "platform_target", "thread_id", "file_paths"}),
            ("mattermost", {"channel_id", "message", "platform_target", "thread_id", "file_paths"}),
            ("whatsapp", {"channel_id", "message", "platform_target", "file_paths"}),
        ),
    )
    def test_every_platform_gets_the_tool(
        self, tmp_path: Path, platform: str, fields: set[str]
    ) -> None:
        tool = channel_send(
            tmp_path,
            make_channel_config(channel_id="work", platform=platform, allowed_chat_ids=["C1"]),
        )

        definition = tool.definition()
        envelope, _text = tool.call({"message": "Done"})

        assert set(definition["parameters"]["properties"]) == fields
        assert envelope["ok"] is True
        assert tool.sent() == [("work", "Done", "C1", options())]

    def test_a_slack_channel_keeps_the_telegram_channel_visible(self, tmp_path: Path) -> None:
        tool = channel_send(
            tmp_path,
            make_channel_config(channel_id="tg-main"),
            make_channel_config(channel_id="work", platform="slack"),
        )

        assert "buttons" in tool.definition()["parameters"]["properties"]


class TestChannels:
    def test_the_only_channel_is_used_when_channel_id_is_left_out(self, tmp_path: Path) -> None:
        tool = channel_send(tmp_path)

        envelope, text = tool.call({"message": "Hello"})

        assert envelope["ok"] is True
        assert text == "channel_id: tg-main\nplatform_target: 111"
        assert tool.sent() == [("tg-main", "Hello", "111", options())]

    def test_several_channels_need_a_choice(self, tmp_path: Path) -> None:
        tool = channel_send(
            tmp_path,
            make_channel_config(channel_id="tg-main", allowed_chat_ids=[111]),
            make_channel_config(channel_id="tg-family", allowed_chat_ids=[222]),
        )

        envelope, text = tool.call({"message": "Hello"})

        assert envelope["error"]["code"] == "invalid_arguments"
        assert text == (
            'Error (invalid_arguments): channel_send was not run: it needs "channel_id"; you '
            'have several Channels: {"channel_id":"tg-family","message":"Hello"} or '
            '{"channel_id":"tg-main","message":"Hello"}'
        )
        assert tool.sent() == []

    def test_a_platform_name_selects_the_only_channel_on_it(self, tmp_path: Path) -> None:
        tool = channel_send(
            tmp_path,
            make_channel_config(channel_id="tg-main", allowed_chat_ids=[111]),
            make_channel_config(channel_id="dc-team", platform="discord", allowed_chat_ids=[9]),
        )

        envelope, _text = tool.call(
            {"action": "send", "channel": "telegram", "target": "111", "message": "Hi"}
        )

        assert envelope["ok"] is True
        assert tool.sent() == [("tg-main", "Hi", "111", options())]

    def test_a_platform_name_as_channel_id_selects_its_only_channel(self, tmp_path: Path) -> None:
        tool = channel_send(
            tmp_path,
            make_channel_config(channel_id="tg-main", allowed_chat_ids=[111]),
            make_channel_config(channel_id="dc-team", platform="discord", allowed_chat_ids=[9]),
        )

        envelope, text = tool.call({"channel_id": "Telegram", "message": "Hi"})
        _refused, typo = tool.call({"channel_id": "telegrm", "message": "Hi"})

        assert envelope["ok"] is True
        assert text == "channel_id: tg-main\nplatform_target: 111"
        assert '"channel_id" must be one of "dc-team", "tg-main"; received "telegrm"' in typo
        assert tool.sent() == [("tg-main", "Hi", "111", options())]

    def test_a_platform_with_two_channels_is_refused(self, tmp_path: Path) -> None:
        tool = channel_send(
            tmp_path,
            make_channel_config(channel_id="tg-main"),
            make_channel_config(channel_id="tg-family"),
        )

        for arguments in (
            {"channel": "Telegram", "message": "Hi"},
            {"channel_id": "telegram", "message": "Hi"},
        ):
            _envelope, text = tool.call(arguments)

            assert "you have several Telegram Channels:" in text
            assert '{"channel_id":"tg-family","message":"Hi"} or {"channel_id":"tg-main"' in text
        assert tool.sent() == []

    def test_a_platform_without_a_channel_is_refused(self, tmp_path: Path) -> None:
        tool = channel_send(tmp_path)

        _envelope, text = tool.call({"channel": "discord", "message": "Hi"})

        assert text.endswith(
            "you have no Discord Channel; your Channels are tg-main (Telegram): "
            '{"channel_id":"tg-main","message":"Hi"}'
        )
        assert tool.sent() == []

    def test_channel_ids_stay_exact(self, tmp_path: Path) -> None:
        tool = channel_send(tmp_path, make_channel_config(channel_id="tg-main"))

        envelope, text = tool.call({"channel_id": "tg_main", "message": "Hi"})

        assert envelope["ok"] is False
        assert '"channel_id" must be one of "tg-main"' in text
        assert tool.sent() == []

    def test_two_different_channels_in_one_call_are_refused(self, tmp_path: Path) -> None:
        tool = channel_send(
            tmp_path,
            make_channel_config(channel_id="tg-main"),
            make_channel_config(channel_id="tg-family"),
        )

        _envelope, text = tool.call(
            {"channel_id": "tg-main", "target": "tg-family:222", "message": "Hi"}
        )

        assert "the call names different Channels:" in text
        assert '{"channel_id":"tg-main","platform_target":"222","message":"Hi"}' in text
        assert '{"channel_id":"tg-family","platform_target":"222","message":"Hi"}' in text
        assert tool.sent() == []


class TestTargets:
    def test_hermes_target_carries_platform_chat_and_thread(self, tmp_path: Path) -> None:
        tool = channel_send(tmp_path)

        envelope, text = tool.call({"target": "telegram:-1001:17", "message": "Topic news"})

        assert envelope["ok"] is True
        assert text == "channel_id: tg-main\nplatform_target: -1001\nthread_id: 17"
        assert tool.sent() == [("tg-main", "Topic news", "-1001", options(thread_id="17"))]

    def test_chat_id_and_text_spellings(self, tmp_path: Path) -> None:
        tool = channel_send(tmp_path)

        envelope, _text = tool.call({"chat_id": 555, "text": "Hi", "topic_id": "3"})

        assert envelope["ok"] is True
        assert tool.sent() == [("tg-main", "Hi", "555", options(thread_id="3"))]

    def test_a_placeholder_target_means_the_default_chat(self, tmp_path: Path) -> None:
        tool = channel_send(tmp_path)

        envelope, _text = tool.call({"message": "Hi", "platform_target": "none"})

        assert envelope["ok"] is True
        assert tool.sent() == [("tg-main", "Hi", "111", options())]

    def test_this_conversation_chat_comes_first(self, tmp_path: Path) -> None:
        tool = channel_send(
            tmp_path,
            make_channel_config(channel_id="tg-main", allowed_chat_ids=[111, 222]),
            reply_target={"channel_id": "tg-main", "platform_target": "222", "thread_id": "5"},
        )

        envelope, _text = tool.call({"message": "Hi"})

        assert envelope["ok"] is True
        assert tool.sent() == [("tg-main", "Hi", "222", options(thread_id="5"))]

    def test_several_allowed_chats_need_a_choice(self, tmp_path: Path) -> None:
        tool = channel_send(
            tmp_path, make_channel_config(channel_id="tg-main", allowed_chat_ids=[111, 222])
        )

        _envelope, text = tool.call({"message": "Hi"})

        assert text == (
            "Error (invalid_arguments): channel_send was not run: this conversation is not with "
            "a chat on tg-main, and the Channel allows 2 chats, so it is not clear which one is "
            'meant. Choose one: {"channel_id":"tg-main","platform_target":"111","message":"Hi"} '
            'or {"channel_id":"tg-main","platform_target":"222","message":"Hi"}'
        )
        assert tool.sent() == []

    def test_target_and_platform_target_must_agree(self, tmp_path: Path) -> None:
        tool = channel_send(tmp_path)

        _envelope, text = tool.call(
            {"platform_target": "111", "target": "telegram:222", "message": "Hi"}
        )

        assert '"platform_target" "111" and "target" name different values:' in text
        assert tool.sent() == []

    def test_a_target_prefix_that_is_no_channel_is_refused(self, tmp_path: Path) -> None:
        tool = channel_send(tmp_path)

        _envelope, text = tool.call({"target": "signal:+4917", "message": "Hi"})

        assert '"target" "signal:+4917" is not "channel:chat"' in text
        assert text.endswith(
            'Send: {"channel_id":"tg-main","platform_target":"<chat id>","message":"Hi"}'
        )
        assert tool.sent() == []


class TestFiles:
    def test_hermes_media_marker_becomes_a_file(self, tmp_path: Path) -> None:
        tool = channel_send(tmp_path)
        (tool.workspace / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\nchart")

        envelope, _text = tool.call({"message": "Here it is\nMEDIA:chart.png"})

        assert envelope["ok"] is True
        [(channel_id, message, target, sent_options)] = tool.sent()
        assert (channel_id, message, target) == ("tg-main", "Here it is", "111")
        assert [item.filename for item in sent_options["files"]] == ["chart.png"]

    def test_openclaw_attachments_and_a_single_path(self, tmp_path: Path) -> None:
        tool = channel_send(tmp_path)
        (tool.workspace / "a.txt").write_text("a", encoding="utf-8")
        (tool.workspace / "b.txt").write_text("b", encoding="utf-8")

        first, _ = tool.call({"attachments": [{"media": "a.txt", "name": "A"}, {"path": "b.txt"}]})
        second, _ = tool.call({"file": "a.txt", "caption": "One file"})

        assert first["ok"] is True and second["ok"] is True
        assert [item.filename for item in tool.sent()[0][3]["files"]] == ["a.txt", "b.txt"]
        assert tool.sent()[1][1] == "One file"

    def test_a_file_uri_is_a_local_file(self, tmp_path: Path) -> None:
        tool = channel_send(tmp_path)
        path = tool.workspace / "report.txt"
        path.write_text("r", encoding="utf-8")

        envelope, _text = tool.call({"file_paths": [path.as_uri()]})

        assert envelope["ok"] is True
        assert [item.filename for item in tool.sent()[0][3]["files"]] == ["report.txt"]

    @pytest.mark.parametrize(
        "address", ["https://example.test/a.png", "/api/images/artifacts/abc", "data:image/png;x"]
    )
    def test_a_web_address_is_not_a_file(self, tmp_path: Path, address: str) -> None:
        tool = channel_send(tmp_path)

        _envelope, text = tool.call({"file_paths": [address]})

        assert text == (
            f'Error (invalid_arguments): channel_send was not run: file_paths "{address}" is a '
            "web address, not a file on this computer. Send the file's local path, or put the "
            "link in message."
        )
        assert tool.sent() == []

    def test_a_folder_is_not_a_file(self, tmp_path: Path) -> None:
        tool = channel_send(tmp_path)
        (tool.workspace / "shots").mkdir()

        _envelope, text = tool.call({"file_paths": ["shots"]})

        assert 'file_paths "shots" is a folder; list the files in it one by one.' in text
        assert tool.sent() == []

    def test_buttons_and_files_go_in_two_calls(self, tmp_path: Path) -> None:
        tool = channel_send(tmp_path)
        (tool.workspace / "a.txt").write_text("a", encoding="utf-8")
        buttons = [[{"label": "OK", "data": "run:ok"}]]

        _envelope, text = tool.call(
            {"message": "Pick", "file_paths": ["a.txt"], "buttons": buttons}
        )

        assert text.endswith(
            "buttons cannot go with files in one message. Send the files first, then the "
            'buttons, in two calls: {"channel_id":"tg-main","message":"Pick","file_paths":'
            '["a.txt"]} then {"channel_id":"tg-main","message":"Pick","buttons":'
            '"<the buttons from this call>"}'
        )
        assert tool.sent() == []


class TestActions:
    def test_list_shows_channels_this_chat_and_allowed_chats(self, tmp_path: Path) -> None:
        tool = channel_send(
            tmp_path,
            make_channel_config(channel_id="tg-main", allowed_chat_ids=[111, 222]),
            make_channel_config(channel_id="dc-team", platform="discord"),
            reply_target={"channel_id": "tg-main", "platform_target": "222"},
        )

        envelope, text = tool.call({"action": "list_targets"})

        assert envelope["ok"] is True
        assert text == (
            "channels: 2\n\n"
            "dc-team (Discord)\n"
            "  allowed chats: none yet\n\n"
            "tg-main (Telegram)\n"
            "  this conversation's chat: 222\n"
            "  allowed chats: 111, 222"
        )
        assert tool.sent() == []

    def test_list_with_a_message_is_refused(self, tmp_path: Path) -> None:
        tool = channel_send(tmp_path)

        _envelope, text = tool.call({"action": "list", "message": "Hi"})

        assert text.endswith(
            "action list only shows where messages can go, but the call also has message. To "
            'send, leave action out: {"message":"Hi"}'
        )
        assert tool.sent() == []

    def test_another_action_is_refused(self, tmp_path: Path) -> None:
        tool = channel_send(tmp_path)

        _envelope, text = tool.call({"action": "delete", "message": "Hi"})

        assert text == (
            'Error (invalid_arguments): channel_send was not run: action "delete" is not '
            "something channel_send does; it sends a message or files to a chat. To send, "
            "leave action out."
        )
        assert tool.sent() == []

    def test_send_wrappers_still_run(self, tmp_path: Path) -> None:
        tool = channel_send(tmp_path)

        envelope, _text = tool.call(json.dumps({"send": {"message": "Wrapped"}}))

        assert envelope["ok"] is True
        assert tool.sent() == [("tg-main", "Wrapped", "111", options())]


def test_display_labels_other_spellings(tmp_path: Path) -> None:
    tool = channel_send(tmp_path)
    builder = tool.registry.get(CHANNEL_SEND_TOOL_NAME).display.parts_builder
    assert builder is not None

    parts = builder({"channel": "telegram", "text": "Hello there"})

    assert [part.value for part in parts] == ["telegram", "Hello there"]
