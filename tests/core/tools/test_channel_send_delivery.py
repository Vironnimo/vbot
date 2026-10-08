"""channel_send delivery: what reaches the Channel service, the note in the chat's Session,
and Reply Targets kept in real Session storage."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.channels import ChannelError, ChannelNotFoundError
from core.channels.adapter import ConversationFacts, RouteFacts
from core.channels.telegram import TelegramChannelAdapter
from core.extensions import InteractionButton
from core.sessions import ChatSessionManager, SessionAddress
from tests.core.channels.channels_test_support import (
    make_config as make_real_channel_config,
)
from tests.core.channels.channels_test_support import (
    make_service as make_channel_service,
)
from tests.core.tools.channel_send_test_support import (
    OUTBOUND_SESSION,
    channel_send,
    delivered,
    make_channel_config,
    model_text,
    options,
    refused,
)

NOTE_HEAD = 'A message was sent to this chat via the channel_send tool by agent "agent-1".'


@pytest.mark.parametrize(
    ("data", "project_id", "run_origin"),
    [
        pytest.param("chk:milk", None, None, id="handler-button"),
        pytest.param(
            "run:done",
            None,
            RouteFacts(agent_id="agent-1", session_id="session-1"),
            id="run-button-wakes-this-session",
        ),
        # A Project Session keeps the raw callback: Channels route Identity Sessions only.
        pytest.param("run:done", "project-1", None, id="run-button-in-project"),
    ],
)
def test_buttons_reach_the_channel(
    tmp_path: Path, data: str, project_id: str | None, run_origin: RouteFacts | None
) -> None:
    tool = channel_send(tmp_path)

    envelope = tool.call(
        {
            "message": "Shopping list",
            "platform_target": "111",
            "buttons": [
                [{"label": "Milk ⬜", "data": data}, {"label": "Eggs", "data": "chk:eggs"}]
            ],
        },
        project_id=project_id,
    )

    delivered(envelope)
    buttons = [
        [InteractionButton(label="Milk ⬜", data=data), InteractionButton("Eggs", "chk:eggs")]
    ]
    expected = options(buttons=buttons)
    if run_origin is not None:
        expected["run_origin"] = run_origin
    assert tool.sent() == [("tg-main", "Shopping list", "111", expected)]


@pytest.mark.parametrize(
    ("error", "code", "message"),
    [
        (
            ChannelNotFoundError("Channel not active: tg-main"),
            "channel_not_found",
            "Channel not active: tg-main",
        ),
        (
            ChannelError("Telegram rejected the message"),
            "channel_error",
            "Nothing was sent: Telegram rejected the message.",
        ),
        # A visible part or an unanswered write: repeating the call can duplicate it.
        (
            ChannelError("Channel request could not be confirmed", possibly_delivered=True),
            "delivery_unconfirmed",
            "It is unknown how much of the message reached the chat: Channel request could "
            "not be confirmed. Sending the same message again can show it twice.",
        ),
        # Raised while sending, so the message may be out; not a refused argument.
        (
            ValueError("Invalid IPv6 URL"),
            "tool_execution_error",
            "channel_send failed while running: Invalid IPv6 URL. It is unknown how much of the "
            "call took effect. Check the current state before you call channel_send again.",
        ),
    ],
    ids=["inactive-channel", "platform-rejection", "possibly-delivered", "failure-while-sending"],
)
def test_a_failed_delivery_is_a_failure_result(
    tmp_path: Path, error: Exception, code: str, message: str
) -> None:
    tool = channel_send(tmp_path)
    tool.service.send.side_effect = error

    envelope = tool.call({"message": "Task finished"})

    assert refused(envelope, code) == message
    tool.service.ensure_outbound_session.assert_not_awaited()


@pytest.mark.parametrize(
    ("arguments", "thread", "note"),
    [
        pytest.param(
            {"message": "Task finished"}, None, f"{NOTE_HEAD}\n\nTask finished", id="message"
        ),
        pytest.param(
            {"message": "See topic", "thread_id": "42"},
            "42",
            f"{NOTE_HEAD}\n\nSee topic",
            id="message-in-thread",
        ),
        pytest.param(
            {"file_paths": ["report.pdf"]},
            None,
            f"{NOTE_HEAD}\n\nAttached file(s): report.pdf",
            id="file",
        ),
    ],
)
def test_the_chats_session_gets_a_note_of_what_was_sent(
    tmp_path: Path, arguments: dict[str, Any], thread: str | None, note: str
) -> None:
    tool = channel_send(tmp_path)
    (tool.workspace / "report.pdf").write_bytes(b"%PDF-1.7\n")

    envelope = tool.call(arguments)

    delivered(envelope)
    tool.service.ensure_outbound_session.assert_awaited_once_with(
        "tg-main", "111", thread_id=thread
    )
    tool.sessions.get_or_create.assert_called_once_with(
        SessionAddress(project_id=None, agent_id="agent-1", session_id=OUTBOUND_SESSION.session_id)
    )
    tool.sessions.get_or_create.return_value.add_note.assert_called_once_with(note)


def test_a_note_that_cannot_be_recorded_keeps_the_send_successful(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tool = channel_send(tmp_path)
    tool.service.ensure_outbound_session.side_effect = RuntimeError("boom")

    with caplog.at_level(logging.WARNING):
        envelope = tool.call({"message": "Task finished"})

    assert delivered(envelope) == {"channel_id": "tg-main", "platform_target": "111"}
    assert tool.sent() == [("tg-main", "Task finished", "111", options())]
    assert "Could not record channel_send outbound note" in caplog.text


def _reply_target(chat: str) -> dict[str, Any]:
    return {"last_reply_target": {"channel_id": "tg-main", "platform_target": chat}}


@pytest.mark.usefixtures("current_format_data_directory")
@pytest.mark.parametrize(
    ("project_chat", "expected"),
    [(None, "111"), ("23456", "23456")],
    ids=["configured-chat", "project-reply-target"],
)
@pytest.mark.asyncio
async def test_a_project_session_never_uses_a_same_named_identity_sessions_chat(
    tmp_path: Path, project_chat: str | None, expected: str
) -> None:
    sessions = ChatSessionManager(tmp_path)
    try:
        project = sessions.create("agent-1", session_id="session-1", project_id="project-one")
        identity = sessions.create("agent-1", session_id="session-1")
        sessions.set_metadata(identity.address, _reply_target("99999"))
        if project_chat is not None:
            sessions.set_metadata(project.address, _reply_target(project_chat))
        tool = channel_send(
            tmp_path, make_channel_config(allowed_chat_ids=[111]), sessions=sessions
        )

        sent = await tool.dispatch({"message": "Project result"}, project_id="project-one")
        listed = await tool.dispatch({"action": "list"}, project_id="project-one")

        assert delivered(sent)["platform_target"] == expected
        assert tool.sent() == [("tg-main", "Project result", expected, options())]
        assert "99999" not in model_text(listed)
        assert ("this conversation's chat: 23456" in model_text(listed)) is (
            project_chat is not None
        )
    finally:
        sessions.close()


@pytest.mark.usefixtures("current_format_data_directory")
def test_session_reads_and_the_note_run_on_the_session_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = ChatSessionManager(tmp_path)
    caller = sessions.create("agent-1", session_id="session-1").address
    sessions.set_metadata(caller, _reply_target("111"))
    threads: dict[str, str] = {}
    get_metadata = sessions.get_metadata
    get_or_create = sessions.get_or_create

    def recorded_metadata(address: SessionAddress) -> Any:
        threads["target"] = threading.current_thread().name
        return get_metadata(address)

    def recorded_get_or_create(address: SessionAddress) -> Any:
        threads["note"] = threading.current_thread().name
        return get_or_create(address)

    monkeypatch.setattr(sessions, "get_metadata", recorded_metadata)
    monkeypatch.setattr(sessions, "get_or_create", recorded_get_or_create)
    tool = channel_send(
        tmp_path, make_channel_config(allowed_chat_ids=[111, 222]), sessions=sessions
    )
    try:
        envelope = tool.call({"message": "Done"})

        assert delivered(envelope)["platform_target"] == "111"
        target = SessionAddress(
            project_id=None, agent_id="agent-1", session_id=OUTBOUND_SESSION.session_id
        )
        assert [entry.role for entry in sessions.get(target).load()] == ["note"]
        # The Reply Target and the outbound note use the Session database's pool.
        assert threads["target"].startswith("vbot-db-sessions")
        assert threads["note"].startswith("vbot-db-sessions")
    finally:
        sessions.close()


@pytest.mark.usefixtures("current_format_data_directory")
@pytest.mark.parametrize("first_target", ["inbound_topic", "explicit_topic"])
@pytest.mark.asyncio
async def test_file_deliveries_keep_the_actual_topic_for_followup_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_target: str
) -> None:
    sessions = ChatSessionManager(tmp_path)
    service = make_channel_service(tmp_path, chat_sessions=sessions)
    config = make_real_channel_config(allowed_chat_ids=[-10001])
    adapter = TelegramChannelAdapter(config, service._new_engine(config), lambda _key: "test-token")
    bot = SimpleNamespace(send_document=AsyncMock())
    adapter._application = SimpleNamespace(
        bot=bot, updater=None, stop=AsyncMock(), shutdown=AsyncMock()
    )
    started = asyncio.Event()

    async def hold_network_listener() -> None:
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(adapter, "start", hold_network_listener)
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)
    tool = channel_send(tmp_path, service=service, sessions=sessions)
    (tool.workspace / "report.pdf").write_bytes(b"%PDF-1.7\n")
    try:
        await service.create_channel(config)
        await asyncio.wait_for(started.wait(), timeout=5)
        route, _ = await adapter._engine._routing._prepare_inbound_route_async(
            ConversationFacts(
                platform="telegram",
                channel_id=config.id,
                chat_id="-10001",
                user_id="1",
                access_scope_id="-10001",
                kind="group",
                thread_id="42",
            )
        )
        caller = {"agent_id": "assistant", "session_id": route.session_id}
        address = SessionAddress(project_id=None, **caller)
        expected_topic = "73" if first_target == "explicit_topic" else "42"
        for index in range(2):
            arguments: dict[str, Any] = {"channel_id": config.id, "file_paths": ["report.pdf"]}
            if index == 0 and first_target == "explicit_topic":
                arguments.update(platform_target="-10001", thread_id=expected_topic)
            result = await tool.dispatch(arguments, **caller)
            assert delivered(result)["thread_id"] == expected_topic

        assert [call.kwargs["message_thread_id"] for call in bot.send_document.await_args_list] == [
            int(expected_topic),
            int(expected_topic),
        ]
        assert sessions.get_metadata(address)["last_reply_target"] == {
            "channel_id": config.id,
            "platform_target": "-10001",
            "thread_id": expected_topic,
        }
        assert len([entry for entry in sessions.get(address).load() if entry.role == "note"]) == 2

        # An explicit chat target without a topic is a new unthreaded delivery;
        # it must clear the previous topic instead of silently inheriting it.
        result = await tool.dispatch(
            {"channel_id": config.id, "platform_target": "-10001", "file_paths": ["report.pdf"]},
            **caller,
        )
        assert delivered(result) == {"channel_id": config.id, "platform_target": "-10001"}
        assert "message_thread_id" not in bot.send_document.await_args.kwargs
        assert "thread_id" not in sessions.get_metadata(address)["last_reply_target"]
    finally:
        await service.aclose()
        service.close()
        sessions.close()
