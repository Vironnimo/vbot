"""Tests for TelegramChannelAdapter behavior."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.channels.adapter import (
    FileData,
)
from core.channels.telegram import (
    TELEGRAM_MESSAGE_LIMIT,
)
from core.chat.commands import CommandFeedback, CommandOutcome
from tests.core.channels.telegram_test_support import (
    drain_chat_queue,
    install_fake_telegram_media,
    make_adapter,
    make_command_dispatcher,
    make_completed_run,
    make_group_update,
    make_update,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/stop@MyBot", "/stop"),
        ("/stop@mybot", "/stop"),
        ("/handoff@MyBot coder", "/handoff coder"),
        ("/stop@OtherBot", "/stop@OtherBot"),
        ("/stop", "/stop"),
        ("hello @MyBot", "hello @MyBot"),
    ],
)
async def test_strip_bot_command_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    expected: str,
) -> None:
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        bot_username="MyBot",
        bot_id=999,
    )

    assert adapter._strip_bot_command_suffix(text) == expected
    await adapter.stop()


@pytest.mark.asyncio
async def test_dm_command_with_own_bot_suffix_is_dispatched_stripped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_dispatcher = make_command_dispatcher(
        result=CommandOutcome(
            command="stop",
            feedback=CommandFeedback(kind="notice", text="Run cancelled."),
        )
    )
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        command_dispatcher=command_dispatcher,
        bot_username="MyBot",
        bot_id=999,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/stop@MyBot"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    command_dispatcher.execute.assert_awaited_once()
    context = command_dispatcher.execute.await_args.args[1]
    assert (context.agent_id, context.session_id) == (
        "assistant",
        "ch-tg-assistant-12345",
    )
    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(chat_id=12345, text="Run cancelled.")
    await adapter.stop()


@pytest.mark.asyncio
async def test_command_addressed_to_other_bot_is_not_stripped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    command_dispatcher = make_command_dispatcher()
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        command_dispatcher=command_dispatcher,
        bot_username="MyBot",
        bot_id=999,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/stop@OtherBot"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    command_dispatcher.prepare.assert_called_once_with("/stop@OtherBot")
    command_dispatcher.execute.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_group_message_without_mention_is_observed_when_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant--10001"
    adapter, chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        bot_username="MyBot",
        bot_id=999,
        observe_unaddressed=True,
    )

    await adapter._handle_inbound_message(
        make_group_update(text="just chatting"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, -10001)

    notes = [
        message.content
        for message in chat_sessions.get("assistant", session_id).load()
        if message.role == "note"
    ]
    assert notes[-1] == "[channel-message] 50 (50): just chatting"
    trigger_mock.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_group_message_without_mention_is_dropped_in_mention_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        bot_username="MyBot",
        bot_id=999,
    )

    await adapter._handle_inbound_message(
        make_group_update(text="just chatting"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, -10001)

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    assert not chat_sessions.exists("assistant", "ch-tg-assistant--10001")
    await adapter.stop()


@pytest.mark.asyncio
async def test_group_message_with_bot_mention_triggers_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant--10001"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        trigger_run=trigger_mock,
        bot_username="MyBot",
        bot_id=999,
    )

    await adapter._handle_inbound_message(
        make_group_update(text="hi @MyBot, are you there?"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, -10001)

    trigger_mock.assert_awaited_once()
    await adapter.stop()


@pytest.mark.asyncio
async def test_group_message_with_visible_bot_name_triggers_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant--10001"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        trigger_run=trigger_mock,
        bot_username="MyBot",
        bot_display_name="Helpful Bot",
        bot_id=999,
    )

    await adapter._handle_inbound_message(
        make_group_update(text="helpful   bot, are you there?"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, -10001)

    trigger_mock.assert_awaited_once()
    await adapter.stop()


@pytest.mark.asyncio
async def test_group_reply_to_bot_message_triggers_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant--10001"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        trigger_run=trigger_mock,
        bot_username="MyBot",
        bot_id=999,
    )

    await adapter._handle_inbound_message(
        make_group_update(text="what did you mean?", reply_to_user_id=999),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, -10001)

    trigger_mock.assert_awaited_once()
    await adapter.stop()


@pytest.mark.asyncio
async def test_mentions_bot_checks_username_name_and_caption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        bot_username="MyBot",
        bot_display_name="Helpful Bot",
        bot_id=999,
    )

    assert adapter._mentions_bot(SimpleNamespace(text=None, caption="@MyBot look at this"))
    assert not adapter._mentions_bot(SimpleNamespace(text=None, caption="@MyBotty look"))
    assert adapter._mentions_bot(SimpleNamespace(text="Hey HELPFUL BOT!", caption=None))
    assert adapter._mentions_bot(SimpleNamespace(text=None, caption="Helpful   Bot, see this"))
    assert not adapter._mentions_bot(SimpleNamespace(text="Unhelpful Bot", caption=None))
    assert not adapter._mentions_bot(SimpleNamespace(text="Helpful Botany", caption=None))
    assert not adapter._mentions_bot(SimpleNamespace(text=None, caption=None))
    await adapter.stop()


@pytest.mark.asyncio
async def test_group_reply_uses_reply_parameters_on_first_chunk_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    session_id = "ch-tg-assistant--10001"
    long_reply = "x" * (TELEGRAM_MESSAGE_LIMIT + 5)
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text=long_reply)
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        response_mode="all",
        trigger_run=trigger_mock,
    )

    await adapter._handle_inbound_message(
        make_group_update(text="hello", message_id=777),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, -10001)

    assert bot.send_message.await_count == 2
    first_call = bot.send_message.await_args_list[0]
    second_call = bot.send_message.await_args_list[1]
    assert first_call.kwargs["reply_parameters"].message_id == 777
    assert first_call.kwargs["reply_parameters"].allow_sending_without_reply is True
    assert "reply_parameters" not in second_call.kwargs
    await adapter.stop()


@pytest.mark.asyncio
async def test_topic_reply_carries_thread_on_all_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    session_id = "ch-tg-assistant--10001"
    long_reply = "x" * (TELEGRAM_MESSAGE_LIMIT + 5)
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text=long_reply)
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        response_mode="all",
        trigger_run=trigger_mock,
    )

    await adapter._handle_inbound_message(
        make_group_update(
            text="hello", message_id=777, message_thread_id=42, is_topic_message=True
        ),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, -10001)

    # Every chunk lands in the forum topic; only the first references the message.
    assert bot.send_message.await_count == 2
    first_call, second_call = bot.send_message.await_args_list
    assert first_call.kwargs["message_thread_id"] == 42
    assert second_call.kwargs["message_thread_id"] == 42
    assert first_call.kwargs["reply_parameters"].message_id == 777
    assert "reply_parameters" not in second_call.kwargs
    await adapter.stop()


@pytest.mark.asyncio
async def test_typing_indicator_targets_topic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
    )

    typing_task = asyncio.create_task(adapter._keep_typing("-10001", "42"))
    await asyncio.sleep(0)
    typing_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await typing_task

    typing_call = bot.send_chat_action.await_args_list[0]
    assert typing_call.kwargs["chat_id"] == -10001
    assert typing_call.kwargs["message_thread_id"] == 42
    await adapter.stop()


@pytest.mark.asyncio
async def test_reply_thread_ignored_outside_forum_topics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    session_id = "ch-tg-assistant--10001"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        response_mode="all",
        trigger_run=trigger_mock,
    )

    # message_thread_id without is_topic_message is a plain reply thread in a
    # non-forum group; sending it back would fail with "message thread not found".
    await adapter._handle_inbound_message(
        make_group_update(text="hello", message_id=777, message_thread_id=42),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, -10001)

    reply_call = bot.send_message.await_args_list[-1]
    assert "message_thread_id" not in reply_call.kwargs
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_with_files_carries_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
    )

    await adapter.send(
        "caption",
        "-10001",
        files=[FileData(filename="a.png", media_type="image/png", data=b"png")],
        thread_id="42",
    )

    photo_call = bot.send_photo.await_args_list[0]
    assert photo_call.kwargs["message_thread_id"] == 42
    assert photo_call.kwargs["caption"] == "caption"
    await adapter.stop()


@pytest.mark.asyncio
async def test_dm_reply_is_sent_without_reply_parameters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
    )

    update = make_update(chat_id=12345, user_id=50, text="hello")
    update.effective_message.message_id = 555
    await adapter._handle_inbound_message(update, SimpleNamespace())
    await drain_chat_queue(adapter, 12345)

    bot.send_message.assert_awaited_once_with(chat_id=12345, text="ok")
    await adapter.stop()


@pytest.mark.asyncio
async def test_unsupported_message_type_in_group_is_silent_when_not_addressed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        bot_username="MyBot",
        bot_id=999,
    )

    await adapter._handle_unsupported_message_type(
        make_group_update(text=None),
        SimpleNamespace(),
    )

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_unsupported_message_type_in_group_replies_when_addressed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        bot_username="MyBot",
        bot_id=999,
    )

    await adapter._handle_unsupported_message_type(
        make_group_update(text=None, reply_to_user_id=999),
        SimpleNamespace(),
    )

    bot.send_message.assert_awaited_once_with(
        chat_id=-10001,
        text="Sorry, this message type isn't supported yet.",
    )
    await adapter.stop()
