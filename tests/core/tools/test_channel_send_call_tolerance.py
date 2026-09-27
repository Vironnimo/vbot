"""channel_send: which Channel and chat a call names, other messaging dialects, and
refusals that name the next call."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock, call

import pytest

from core.sessions import SessionAddress
from tests.core.tools.channel_send_test_support import (
    channel_send,
    delivered,
    make_channel_config,
    model_text,
    options,
    refused,
)

CALLING_SESSION = SessionAddress(project_id=None, agent_id="agent-1", session_id="session-1")


def _channels(name: str) -> tuple[Mock, ...]:
    return {
        "one": (make_channel_config(allowed_chat_ids=[111]),),
        "two-telegram": (
            make_channel_config(allowed_chat_ids=[111]),
            make_channel_config(channel_id="tg-family", allowed_chat_ids=[222]),
        ),
        "telegram-discord": (
            make_channel_config(allowed_chat_ids=[111]),
            make_channel_config(channel_id="dc-team", platform="discord", allowed_chat_ids=[9]),
        ),
        "discord": (make_channel_config(channel_id="dc-team", platform="discord"),),
        "other-agent": (make_channel_config(agent_id="agent-2"),),
        "none": (),
    }[name]


def _tool(tmp_path: Path, channels: str, **options: Any) -> Any:
    configs = _channels(channels)
    tool = channel_send(tmp_path, *configs, **options)
    tool.service.list_channels.return_value = list(configs)
    return tool


@pytest.mark.parametrize(
    ("channels", "arguments", "channel", "chat"),
    [
        pytest.param(
            "telegram-discord",
            {"action": "send", "channel": "telegram", "target": "111", "message": "Hi"},
            "tg-main",
            "111",
            id="platform-as-channel",
        ),
        pytest.param(
            "telegram-discord",
            {"channel_id": "Telegram", "message": "Hi"},
            "tg-main",
            "111",
            id="platform-as-channel-id",
        ),
    ],
)
def test_the_call_selects_one_channel(
    tmp_path: Path, channels: str, arguments: dict[str, Any], channel: str, chat: str
) -> None:
    tool = _tool(tmp_path, channels)

    envelope = tool.call(arguments)

    assert model_text(envelope) == f"channel_id: {channel}\nplatform_target: {chat}"
    assert tool.sent() == [(channel, "Hi", chat, options())]


@pytest.mark.parametrize(
    ("channels", "arguments", "code", "message"),
    [
        pytest.param(
            "two-telegram",
            {"message": "Hi"},
            "invalid_arguments",
            'channel_send was not run: it needs "channel_id"; you have several Channels: '
            '{"channel_id":"tg-family","message":"Hi"} or {"channel_id":"tg-main","message":"Hi"}',
            id="several-channels",
        ),
        pytest.param(
            "two-telegram",
            {"channel": "Telegram", "message": "Hi"},
            "invalid_arguments",
            "channel_send was not run: you have several Telegram Channels: "
            '{"channel_id":"tg-family","message":"Hi"} or {"channel_id":"tg-main","message":"Hi"}',
            id="platform-with-several-channels",
        ),
        pytest.param(
            "one",
            {"channel": "discord", "message": "Hi"},
            "invalid_arguments",
            "channel_send was not run: you have no Discord Channel; your Channels are tg-main "
            '(Telegram): {"channel_id":"tg-main","message":"Hi"}',
            id="platform-without-channel",
        ),
        pytest.param(
            "one",
            {"channel": "signal", "message": "Hi"},
            "invalid_arguments",
            'channel_send was not run: you have no Channel "signal"; your Channels are tg-main '
            '(Telegram): {"channel_id":"tg-main","message":"Hi"}',
            id="unknown-channel-name",
        ),
        pytest.param(
            "two-telegram",
            {"channel_id": "tg-main", "target": "tg-family:222", "message": "Hi"},
            "invalid_arguments",
            "channel_send was not run: the call names different Channels: "
            '{"channel_id":"tg-main","platform_target":"222","message":"Hi"} or '
            '{"channel_id":"tg-family","platform_target":"222","message":"Hi"}',
            id="two-channels",
        ),
        pytest.param(
            "other-agent",
            {"channel_id": "tg-main", "message": "Hi"},
            "invalid_arguments",
            "Channel tg-main belongs to agent agent-2, not agent-1",
            id="other-agents-channel",
        ),
        pytest.param(
            "none",
            {"channel_id": "tg-gone", "message": "Hi"},
            "channel_not_found",
            "Channel not found: tg-gone",
            id="removed-channel",
        ),
        pytest.param(
            "none",
            {"message": "Hi"},
            "channel_not_found",
            "Agent agent-1 has no enabled Channel to send through.",
            id="no-channel-left",
        ),
    ],
)
def test_a_call_without_one_clear_channel_is_refused(
    tmp_path: Path, channels: str, arguments: dict[str, Any], code: str, message: str
) -> None:
    tool = _tool(tmp_path, channels)

    envelope = tool.call(arguments)

    assert refused(envelope, code) == message
    assert tool.sent() == []
    assert tool.sessions.get_metadata.call_count == 0


@pytest.mark.parametrize(
    ("allowed", "reply_target", "arguments", "chat", "thread", "reads_metadata"),
    [
        pytest.param(
            [],
            None,
            {"message": "Hi", "platform_target": "12345"},
            "12345",
            None,
            False,
            id="explicit-chat",
        ),
        pytest.param(
            [111],
            None,
            {"target": "telegram:-1001:17", "message": "Hi"},
            "-1001",
            "17",
            False,
            id="platform-chat-thread-target",
        ),
        pytest.param(
            [111],
            None,
            {"chat_id": 555, "text": "Hi", "topic_id": "3"},
            "555",
            "3",
            False,
            id="chat-id-spellings",
        ),
        pytest.param(
            [111],
            None,
            {"message": "Hi", "platform_target": "none"},
            "111",
            None,
            True,
            id="placeholder-chat",
        ),
        pytest.param(
            [111, 222],
            {"channel_id": "tg-main", "platform_target": "222", "thread_id": "5"},
            {"message": "Hi"},
            "222",
            "5",
            True,
            id="this-conversations-chat-and-thread",
        ),
        pytest.param(
            [111, 222],
            {"channel_id": "tg-main", "platform_target": "222", "thread_id": "5"},
            {"message": "Hi", "thread_id": "9"},
            "222",
            "9",
            True,
            id="explicit-thread-wins",
        ),
        pytest.param(
            [111],
            {"channel_id": "tg-other", "platform_target": "12345"},
            {"message": "Hi"},
            "111",
            None,
            True,
            id="other-channels-conversation-ignored",
        ),
        pytest.param([111], None, {"message": "Hi"}, "111", None, True, id="only-allowed-chat"),
    ],
)
def test_the_call_selects_one_chat(
    tmp_path: Path,
    allowed: list[int],
    reply_target: dict[str, str] | None,
    arguments: dict[str, Any],
    chat: str,
    thread: str | None,
    reads_metadata: bool,
) -> None:
    tool = channel_send(
        tmp_path, make_channel_config(allowed_chat_ids=allowed), reply_target=reply_target
    )

    envelope = tool.call(arguments)

    expected = {"channel_id": "tg-main", "platform_target": chat}
    if thread is not None:
        expected["thread_id"] = thread
    assert delivered(envelope) == expected
    assert model_text(envelope) == "\n".join(f"{key}: {value}" for key, value in expected.items())
    assert tool.sent() == [("tg-main", "Hi", chat, options(thread_id=thread))]
    assert tool.sessions.get_metadata.call_args_list == (
        [call(CALLING_SESSION)] if reads_metadata else []
    )


@pytest.mark.parametrize(
    ("allowed", "arguments", "message"),
    [
        pytest.param(
            [111, 222],
            {"message": "Hi"},
            "channel_send was not run: this conversation is not with a chat on tg-main, and "
            "the Channel allows 2 chats, so it is not clear which one is meant. Choose one: "
            '{"channel_id":"tg-main","platform_target":"111","message":"Hi"} or '
            '{"channel_id":"tg-main","platform_target":"222","message":"Hi"}',
            id="several-allowed-chats",
        ),
        pytest.param(
            [],
            {"message": "Hi"},
            "channel_send was not run: this conversation is not with a chat on tg-main, and "
            "the Channel allows no chats yet, so there is no chat to send to by default. Give "
            "the chat's id on Telegram. Send: "
            '{"channel_id":"tg-main","platform_target":"<chat id>","message":"Hi"}',
            id="no-allowed-chat",
        ),
        pytest.param(
            [111],
            {"platform_target": "111", "target": "telegram:222", "message": "Hi"},
            'channel_send was not run: "platform_target" "111" and "target" name different '
            'values: {"channel_id":"tg-main","platform_target":"111","message":"Hi"} or '
            '{"channel_id":"tg-main","platform_target":"222","message":"Hi"}',
            id="target-disagrees",
        ),
        pytest.param(
            [111],
            {"target": "signal:+4917", "message": "Hi"},
            'channel_send was not run: "target" "signal:+4917" is not "channel:chat" with one '
            "of your Channels (tg-main). Name the Channel and the chat separately. Send: "
            '{"channel_id":"tg-main","platform_target":"<chat id>","message":"Hi"}',
            id="target-without-channel",
        ),
    ],
)
def test_a_call_without_one_clear_chat_is_refused(
    tmp_path: Path, allowed: list[int], arguments: dict[str, Any], message: str
) -> None:
    tool = channel_send(tmp_path, make_channel_config(allowed_chat_ids=allowed))

    envelope = tool.call(arguments)

    assert refused(envelope) == message
    assert tool.sent() == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"request": {"operation": "send", "message": "Hi"}},
        {"send": {"message": "Hi"}},
        json.dumps({"send": {"message": "Hi"}}),
        {"message": "Hi", "file_paths": []},
        {"message": "Hi", "buttons": []},
    ],
    ids=[
        "request-wrapper",
        "send-wrapper",
        "json-text",
        "no-files",
        "no-buttons",
    ],
)
def test_other_call_shapes_still_send(tmp_path: Path, arguments: Any) -> None:
    tool = channel_send(tmp_path)

    envelope = tool.call(arguments)

    assert delivered(envelope) == {"channel_id": "tg-main", "platform_target": "111"}
    assert tool.sent() == [("tg-main", "Hi", "111", options())]


@pytest.mark.parametrize(
    ("channels", "arguments", "message"),
    [
        pytest.param(
            "one",
            {"platform_target": "111"},
            'channel_send was not run: it needs "message", "file_paths", or both. Send: '
            '{"channel_id":"tg-main","platform_target":"111","message":"<text to send>"}',
            id="nothing-to-send",
        ),
        pytest.param(
            "one",
            {"message": "Hi", "buttons": [[]]},
            'channel_send was not run: "buttons[0]" must not be empty.',
            id="empty-button-row",
        ),
        pytest.param(
            "one",
            {"message": "Hi", "buttons": [[{"label": "Milk"}]]},
            "channel_send was not run:\n"
            "- \"buttons[0][0].data\" is required: Callback payload as '<prefix>:<payload>'; "
            "max 64 UTF-8 bytes.\n"
            "channel_send parameters: channel_id, message, platform_target, thread_id, "
            "file_paths, buttons.",
            id="button-without-data",
        ),
        pytest.param(
            "one",
            {"message": "Hi", "buttons": [[{"label": "Milk", "data": "chk:milk", "x": True}]]},
            "buttons[0][0] has unknown field(s): x",
            id="unknown-button-field",
        ),
        pytest.param(
            "discord",
            {"message": "Hello", "platform_target": "12345", "thread_id": "42"},
            "channel_send was not run:\n"
            '- "thread_id" is not a parameter.\n'
            "channel_send parameters: channel_id, message, platform_target, file_paths.",
            id="field-no-channel-offers",
        ),
        pytest.param(
            "telegram-discord",
            {
                "channel_id": "dc-team",
                "message": "Hello",
                "platform_target": "12345",
                "thread_id": "42",
            },
            "channel_send was not run: thread_id does not work on the Discord Channel dc-team. "
            "The call below sends without it; send it only if that is meant. Send: "
            '{"channel_id":"dc-team","platform_target":"12345","message":"Hello"}',
            id="telegram-field-on-discord",
        ),
        pytest.param(
            "telegram-discord",
            {
                "channel_id": "dc-team",
                "message": "Hello",
                "platform_target": "12345",
                "thread_id": "42",
                "buttons": [[{"label": "Go", "data": "run:go"}]],
            },
            "channel_send was not run: buttons and thread_id do not work on the Discord "
            "Channel dc-team. The call below sends without them; send it only if that is "
            'meant. Send: {"channel_id":"dc-team","platform_target":"12345","message":"Hello"}',
            id="telegram-fields-on-discord",
        ),
    ],
)
def test_malformed_or_unsupported_fields_are_refused(
    tmp_path: Path, channels: str, arguments: dict[str, Any], message: str
) -> None:
    tool = _tool(tmp_path, channels)

    envelope = tool.call(arguments)

    assert refused(envelope) == message
    assert tool.sent() == []


@pytest.mark.parametrize("message", ["<text to send>", " <the message from this call> "])
def test_a_stand_in_message_is_never_sent(tmp_path: Path, message: str) -> None:
    tool = channel_send(tmp_path)

    envelope = tool.call({"channel_id": "tg-main", "message": message})

    assert refused(envelope) == (
        f'channel_send was not run: message "{message.strip()}" is a stand-in, so nothing '
        "was sent. Send the actual text in its place."
    )
    assert tool.sent() == []


def test_a_stand_in_message_with_files_offers_the_files_alone(tmp_path: Path) -> None:
    tool = channel_send(tmp_path)
    (tool.workspace / "a.txt").write_text("a", encoding="utf-8")

    refusal = tool.call({"target": "telegram:111", "message": "<text to send>\nMEDIA:a.txt"})
    envelope = tool.call(
        {"channel_id": "tg-main", "platform_target": "111", "file_paths": ["a.txt"]}
    )

    assert refused(refusal) == (
        'channel_send was not run: message "<text to send>" is a stand-in, so nothing was '
        "sent. Put the actual text in its place, or send the files alone. Send: "
        '{"channel_id":"tg-main","platform_target":"111","file_paths":["a.txt"]}'
    )
    delivered(envelope)
    [(channel_id, message, target, sent_options)] = tool.sent()
    assert (channel_id, message, target) == ("tg-main", None, "111")
    assert [item.filename for item in sent_options["files"]] == ["a.txt"]


def test_list_shows_channels_this_chat_and_allowed_chats(tmp_path: Path) -> None:
    tool = channel_send(
        tmp_path,
        make_channel_config(allowed_chat_ids=[111, 222]),
        make_channel_config(channel_id="dc-team", platform="discord"),
        reply_target={"channel_id": "tg-main", "platform_target": "222", "thread_id": "7"},
    )

    envelope = tool.call({"action": "list_targets"})

    assert delivered(envelope)["channels"] == 2
    assert model_text(envelope) == (
        "channels: 2\n\n"
        "dc-team (Discord)\n"
        "  allowed chats: none yet\n\n"
        "tg-main (Telegram)\n"
        "  this conversation's chat: 222, thread 7\n"
        "  allowed chats: 111, 222"
    )
    assert tool.sent() == []


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"action": "list", "message": "Hi"},
            "channel_send was not run: action list only shows where messages can go, but the "
            'call also has message. To send, leave action out: {"message":"Hi"}',
        ),
        (
            {"action": "delete", "message": "Hi"},
            'channel_send was not run: action "delete" is not something channel_send does; it '
            "sends a message or files to a chat. To send, leave action out.",
        ),
    ],
    ids=["list-with-message", "unknown-action"],
)
def test_other_actions_are_refused(tmp_path: Path, arguments: dict[str, Any], message: str) -> None:
    tool = channel_send(tmp_path)

    envelope = tool.call(arguments)

    assert refused(envelope) == message
    assert tool.sent() == []
