"""Tests for TelegramChannelAdapter behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

import core.channels.telegram as telegram_module
from core.attachments import AttachmentStore
from core.channels.adapter import (
    ConversationFacts,
    FileData,
    MessageFacts,
    ReplyPlanFacts,
    RouteFacts,
)
from core.channels.channels import ChannelConfig, ChannelConfigError
from core.channels.telegram import TELEGRAM_MESSAGE_LIMIT, TelegramChannelAdapter
from core.chat.chat import ChatSessionManager
from core.chat.commands import CommandHandled, NotACommand
from core.chat.content_blocks import FileBlock, MediaBlock, TextBlock
from core.chat.runs import ASSISTANT_OUTPUT_EVENT, Run


def make_config(
    *,
    dm_scope: str = "per_conversation",
    allowed_chat_ids: list[int] | None = None,
) -> ChannelConfig:
    return ChannelConfig(
        id="tg-assistant",
        platform="telegram",
        agent_id="assistant",
        dm_scope=dm_scope,
        allowed_chat_ids=list(allowed_chat_ids or []),
        token_env_var="TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
        enabled=True,
    )


def make_update(*, chat_id: int, user_id: int, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(text=text, message_thread_id=None),
    )


def make_photo_update(
    *,
    chat_id: int,
    user_id: int,
    file_id: str,
    file_unique_id: str,
    caption: str | None = None,
    media_group_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(
            text=None,
            caption=caption,
            photo=[SimpleNamespace(file_id=file_id, file_unique_id=file_unique_id)],
            document=None,
            media_group_id=media_group_id,
            message_thread_id=None,
        ),
    )


def make_document_update(
    *,
    chat_id: int,
    user_id: int,
    file_id: str,
    file_unique_id: str,
    file_name: str,
    caption: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        effective_user=SimpleNamespace(id=user_id),
        effective_message=SimpleNamespace(
            text=None,
            caption=caption,
            photo=None,
            document=SimpleNamespace(
                file_id=file_id,
                file_unique_id=file_unique_id,
                file_name=file_name,
            ),
            media_group_id=None,
            message_thread_id=None,
        ),
    )


def make_completed_run(*, session_id: str, output_text: str) -> Run:
    run = Run(run_id="run-completed", agent_id="assistant", session_id=session_id)
    run.emit(ASSISTANT_OUTPUT_EVENT, {"message": {"content": output_text}})
    run.mark_completed("ok")
    return run


def make_failed_run(*, session_id: str, message: str) -> Run:
    run = Run(run_id="run-failed", agent_id="assistant", session_id=session_id)
    run.mark_failed(RuntimeError(message))
    return run


def make_command_dispatcher(*, result: object | None = None) -> SimpleNamespace:
    dispatch_result = NotACommand() if result is None else result
    return SimpleNamespace(dispatch=Mock(return_value=dispatch_result))


def make_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    dm_scope: str = "per_conversation",
    allowed_chat_ids: list[int] | None = None,
    trigger_run: AsyncMock | None = None,
    runtime: object | None = None,
    attachment_store: AttachmentStore | None = None,
    command_dispatcher: object | None = None,
    set_process_token: bool = True,
) -> tuple[TelegramChannelAdapter, ChatSessionManager, AsyncMock, SimpleNamespace]:
    if set_process_token:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", "test-token")
    else:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", raising=False)

    chat_sessions = ChatSessionManager(tmp_path)
    trigger_mock = trigger_run or AsyncMock()
    trigger_service = SimpleNamespace(trigger_run=trigger_mock)
    resolved_command_dispatcher = command_dispatcher or make_command_dispatcher()

    adapter = TelegramChannelAdapter(
        make_config(dm_scope=dm_scope, allowed_chat_ids=allowed_chat_ids),
        cast(Any, trigger_service),
        cast(Any, chat_sessions),
        runtime=runtime if runtime is not None else SimpleNamespace(),
        attachment_store=attachment_store,
        command_dispatcher=cast(Any, resolved_command_dispatcher),
    )

    bot = SimpleNamespace(
        send_message=AsyncMock(),
        send_photo=AsyncMock(),
        send_document=AsyncMock(),
        send_media_group=AsyncMock(),
        get_file=AsyncMock(),
    )
    adapter._application = SimpleNamespace(
        bot=bot,
        updater=None,
        stop=AsyncMock(),
        shutdown=AsyncMock(),
    )
    return adapter, chat_sessions, trigger_mock, bot


async def drain_chat_queue(adapter: TelegramChannelAdapter, chat_id: int) -> None:
    queue = adapter._chat_queues.get(str(chat_id))
    if queue is None:
        await asyncio.sleep(0)
        return
    await asyncio.wait_for(queue.join(), timeout=1)


def install_fake_telegram_media(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeInputFile:
        def __init__(self, data: bytes, *, filename: str | None = None) -> None:
            self.data = data
            self.filename = filename

    class FakeInputMediaPhoto:
        def __init__(self, media: FakeInputFile, caption: str | None = None) -> None:
            self.media = media
            self.caption = caption

    class FakeInputMediaDocument:
        def __init__(self, media: FakeInputFile, caption: str | None = None) -> None:
            self.media = media
            self.caption = caption

    fake_telegram = SimpleNamespace(
        InputFile=FakeInputFile,
        InputMediaPhoto=FakeInputMediaPhoto,
        InputMediaDocument=FakeInputMediaDocument,
    )
    monkeypatch.setattr(telegram_module, "_load_telegram", lambda: fake_telegram)


@pytest.mark.parametrize(
    ("dm_scope", "chat_id", "user_id", "expected"),
    [
        ("per_conversation", 12345, 987, "ch-tg-assistant-12345"),
        ("main", 12345, 987, "ch-tg-assistant-main"),
        ("per_peer", 12345, 987, "ch-tg-assistant-u987"),
        ("per_account_channel_peer", 12345, 987, "ch-tg-assistant-12345-u987"),
        ("main", -10001, 987, "ch-tg-assistant--10001"),
    ],
)
def test_derive_session_id(dm_scope: str, chat_id: int, user_id: int, expected: str) -> None:
    adapter = TelegramChannelAdapter.__new__(TelegramChannelAdapter)
    adapter._config = make_config(dm_scope=dm_scope, allowed_chat_ids=[chat_id])

    session_id = TelegramChannelAdapter._derive_session_id(
        adapter,
        ConversationFacts(
            platform="telegram",
            channel_id="tg-assistant",
            chat_id=str(chat_id),
            user_id=str(user_id),
            thread_id=None,
        ),
    )

    assert session_id == expected


def test_constructor_requires_token_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", raising=False)

    with pytest.raises(ChannelConfigError, match="Missing Telegram token"):
        TelegramChannelAdapter(
            make_config(allowed_chat_ids=[12345]),
            trigger_service=cast(Any, SimpleNamespace(trigger_run=AsyncMock())),
            chat_sessions=cast(Any, ChatSessionManager(tmp_path)),
            runtime=SimpleNamespace(),
            command_dispatcher=cast(Any, make_command_dispatcher()),
        )


def test_constructor_resolves_token_from_runtime_environment_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", raising=False)
    runtime = SimpleNamespace(resolve_environment_credential=lambda _key: "runtime-token")

    adapter = TelegramChannelAdapter(
        make_config(allowed_chat_ids=[12345]),
        trigger_service=cast(Any, SimpleNamespace(trigger_run=AsyncMock())),
        chat_sessions=cast(Any, ChatSessionManager(tmp_path)),
        runtime=runtime,
        command_dispatcher=cast(Any, make_command_dispatcher()),
    )

    assert adapter._token == "runtime-token"


@pytest.mark.asyncio
async def test_allowed_chat_ids_enforced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=99999, user_id=50, text="hi"),
        SimpleNamespace(),
    )
    await asyncio.sleep(0)

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_system_reminder_written_once_for_new_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        side_effect=[
            make_completed_run(session_id=session_id, output_text="first"),
            make_completed_run(session_id=session_id, output_text="second"),
        ]
    )
    adapter, chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
    )

    update = make_update(chat_id=12345, user_id=50, text="hello")
    await adapter._handle_inbound_message(update, SimpleNamespace())
    await drain_chat_queue(adapter, 12345)

    await adapter._handle_inbound_message(update, SimpleNamespace())
    await drain_chat_queue(adapter, 12345)

    session = chat_sessions.get("assistant", session_id)
    notes = [message for message in session.load() if message.role == "note"]
    metadata = chat_sessions.get_metadata("assistant", session_id)

    assert len(notes) == 1
    assert metadata["last_reply_target"] == {
        "channel_id": "tg-assistant",
        "platform_target": "12345",
    }
    await adapter.stop()


@pytest.mark.asyncio
async def test_completed_run_forwards_final_assistant_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="final reply")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="hello"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    bot.send_message.assert_awaited_once_with(chat_id=12345, text="final reply")
    await adapter.stop()


@pytest.mark.asyncio
async def test_plain_text_command_is_dispatched_before_trigger_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_dispatcher = make_command_dispatcher(result=CommandHandled(reply="Run cancelled."))
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        command_dispatcher=command_dispatcher,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/stop"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    command_dispatcher.dispatch.assert_called_once_with(
        "assistant", "ch-tg-assistant-12345", "/stop"
    )
    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(chat_id=12345, text="Run cancelled.")
    await adapter.stop()


@pytest.mark.asyncio
async def test_stop_command_is_eagerly_dispatched_while_chat_worker_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    command_dispatcher = make_command_dispatcher()
    command_dispatcher.dispatch.side_effect = [
        NotACommand(),
        CommandHandled(reply="Run cancelled."),
    ]
    trigger_mock = AsyncMock(
        return_value=Run(run_id="run-active", agent_id="assistant", session_id=session_id)
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        command_dispatcher=command_dispatcher,
    )

    relay_started = asyncio.Event()
    release_relay = asyncio.Event()

    async def block_relay(_run: Run, _platform_target: str) -> None:
        relay_started.set()
        await release_relay.wait()

    monkeypatch.setattr(
        adapter,
        "_relay_run_events",
        AsyncMock(side_effect=block_relay),
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="hello"),
        SimpleNamespace(),
    )
    await asyncio.wait_for(relay_started.wait(), timeout=1)

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/stop"),
        SimpleNamespace(),
    )
    await asyncio.sleep(0)

    first_call = command_dispatcher.dispatch.call_args_list[0]
    second_call = command_dispatcher.dispatch.call_args_list[1]
    assert first_call.args == ("assistant", session_id, "hello")
    assert second_call.args == ("assistant", session_id, "/stop")
    assert trigger_mock.await_count == 1
    bot.send_message.assert_awaited_once_with(chat_id=12345, text="Run cancelled.")

    release_relay.set()
    await drain_chat_queue(adapter, 12345)
    await adapter.stop()


@pytest.mark.asyncio
async def test_non_command_text_still_queues_while_chat_worker_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    command_dispatcher = make_command_dispatcher()
    command_dispatcher.dispatch.side_effect = [
        NotACommand(),
        NotACommand(),
    ]
    trigger_mock = AsyncMock(
        return_value=Run(run_id="run-active", agent_id="assistant", session_id=session_id)
    )
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        command_dispatcher=command_dispatcher,
    )

    relay_started = asyncio.Event()
    release_relay = asyncio.Event()

    async def block_relay(_run: Run, _platform_target: str) -> None:
        relay_started.set()
        await release_relay.wait()

    monkeypatch.setattr(
        adapter,
        "_relay_run_events",
        AsyncMock(side_effect=block_relay),
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="hello"),
        SimpleNamespace(),
    )
    await asyncio.wait_for(relay_started.wait(), timeout=1)

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="still queued"),
        SimpleNamespace(),
    )
    await asyncio.sleep(0)

    first_call = command_dispatcher.dispatch.call_args_list[0]
    second_call = command_dispatcher.dispatch.call_args_list[1]
    assert first_call.args == ("assistant", session_id, "hello")
    assert second_call.args == ("assistant", session_id, "still queued")
    assert trigger_mock.await_count == 1

    queue = adapter._chat_queues.get("12345")
    assert queue is not None
    assert queue.qsize() == 1

    release_relay.set()
    await drain_chat_queue(adapter, 12345)
    await adapter.stop()


@pytest.mark.asyncio
async def test_non_text_content_skips_command_dispatch_and_triggers_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    command_dispatcher = make_command_dispatcher(result=CommandHandled(reply="Run cancelled."))
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        command_dispatcher=command_dispatcher,
    )

    queued = telegram_module._QueuedInboundMessage(
        route=RouteFacts(agent_id="assistant", session_id=session_id),
        reply_plan=ReplyPlanFacts(channel_id="tg-assistant", platform_target="12345"),
        message=MessageFacts(content=[TextBlock(type="text", text="/stop")]),
    )

    await adapter._process_queued_message(queued)

    command_dispatcher.dispatch.assert_not_called()
    trigger_mock.assert_awaited_once_with("assistant", queued.message.content, session_id)
    bot.send_message.assert_awaited_once_with(chat_id=12345, text="ok")
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_splits_message_at_telegram_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    payload = "x" * (TELEGRAM_MESSAGE_LIMIT * 2 + 9)
    await adapter.send(payload, "12345")

    chunks = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert [len(chunk) for chunk in chunks] == [TELEGRAM_MESSAGE_LIMIT, TELEGRAM_MESSAGE_LIMIT, 9]
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_single_image_file_uses_send_photo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    await adapter.send(
        "caption",
        "12345",
        files=[FileData(filename="image.png", media_type="image/png", data=b"img-bytes")],
    )

    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.kwargs["chat_id"] == 12345
    assert bot.send_photo.await_args.kwargs["caption"] == "caption"
    bot.send_document.assert_not_awaited()
    bot.send_media_group.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_mixed_single_image_and_document_sends_separate_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    await adapter.send(
        "batch caption",
        "12345",
        files=[
            FileData(filename="a.png", media_type="image/png", data=b"a"),
            FileData(filename="b.pdf", media_type="application/pdf", data=b"%PDF"),
        ],
    )

    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.await_args.kwargs["chat_id"] == 12345
    assert bot.send_photo.await_args.kwargs["caption"] == "batch caption"

    bot.send_document.assert_awaited_once()
    assert bot.send_document.await_args.kwargs["chat_id"] == 12345
    assert "caption" not in bot.send_document.await_args.kwargs

    bot.send_media_group.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_two_images_uses_single_homogeneous_media_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    await adapter.send(
        "batch caption",
        "12345",
        files=[
            FileData(filename="a.png", media_type="image/png", data=b"a"),
            FileData(filename="b.jpg", media_type="image/jpeg", data=b"b"),
        ],
    )

    bot.send_media_group.assert_awaited_once()
    media = bot.send_media_group.await_args.kwargs["media"]
    assert len(media) == 2
    assert {type(item).__name__ for item in media} == {"FakeInputMediaPhoto"}
    assert media[0].caption == "batch caption"
    assert media[1].caption is None
    bot.send_photo.assert_not_awaited()
    bot.send_document.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_mixed_batches_caption_only_on_first_item_of_first_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    await adapter.send(
        "batch caption",
        "12345",
        files=[
            FileData(filename="a.png", media_type="image/png", data=b"a"),
            FileData(filename="b.jpg", media_type="image/jpeg", data=b"b"),
            FileData(filename="c.pdf", media_type="application/pdf", data=b"c"),
            FileData(filename="d.pdf", media_type="application/pdf", data=b"d"),
        ],
    )

    assert bot.send_media_group.await_count == 2
    first_batch = bot.send_media_group.await_args_list[0].kwargs["media"]
    second_batch = bot.send_media_group.await_args_list[1].kwargs["media"]

    assert {type(item).__name__ for item in first_batch} == {"FakeInputMediaPhoto"}
    assert {type(item).__name__ for item in second_batch} == {"FakeInputMediaDocument"}

    assert first_batch[0].caption == "batch caption"
    assert all(item.caption is None for item in first_batch[1:])
    assert all(item.caption is None for item in second_batch)

    bot.send_photo.assert_not_awaited()
    bot.send_document.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_inbound_photo_stores_attachment_and_triggers_media_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path)
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )

    bot.get_file.return_value = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\nIMG"))
    )

    await adapter._handle_inbound_media(
        make_photo_update(
            chat_id=12345,
            user_id=50,
            file_id="photo-1",
            file_unique_id="uniq-1",
        ),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    trigger_args = await_args.args
    assert trigger_args[0] == "assistant"
    assert trigger_args[2] == session_id
    blocks = trigger_args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 1
    assert isinstance(blocks[0], MediaBlock)
    stored = attachment_store.get(blocks[0].attachment_id)
    assert stored.media_type == "image/png"
    await adapter.stop()


@pytest.mark.asyncio
async def test_inbound_pdf_document_triggers_file_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path)
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )

    bot.get_file.return_value = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"%PDF-1.7\n"))
    )

    await adapter._handle_inbound_media(
        make_document_update(
            chat_id=12345,
            user_id=50,
            file_id="doc-1",
            file_unique_id="docuniq-1",
            file_name="report.pdf",
        ),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 1
    assert isinstance(blocks[0], FileBlock)
    assert blocks[0].media_type == "application/pdf"
    await adapter.stop()


@pytest.mark.asyncio
async def test_inbound_text_document_triggers_text_block_with_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path)
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )

    bot.get_file.return_value = SimpleNamespace(
        download_as_bytearray=AsyncMock(return_value=bytearray(b"hello from text file"))
    )

    await adapter._handle_inbound_media(
        make_document_update(
            chat_id=12345,
            user_id=50,
            file_id="doc-2",
            file_unique_id="docuniq-2",
            file_name="notes.txt",
        ),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 1
    assert isinstance(blocks[0], TextBlock)
    assert blocks[0].text == "hello from text file"
    await adapter.stop()


@pytest.mark.asyncio
async def test_album_messages_are_buffered_into_single_trigger_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path)
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        attachment_store=attachment_store,
    )

    bot.get_file.side_effect = [
        SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\nA"))
        ),
        SimpleNamespace(
            download_as_bytearray=AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\nB"))
        ),
    ]

    await adapter._handle_inbound_media(
        make_photo_update(
            chat_id=12345,
            user_id=50,
            file_id="photo-a",
            file_unique_id="uniq-a",
            media_group_id="album-1",
        ),
        SimpleNamespace(),
    )
    await adapter._handle_inbound_media(
        make_photo_update(
            chat_id=12345,
            user_id=50,
            file_id="photo-b",
            file_unique_id="uniq-b",
            media_group_id="album-1",
        ),
        SimpleNamespace(),
    )

    await asyncio.sleep(0.6)
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 2
    assert isinstance(blocks[0], MediaBlock)
    assert isinstance(blocks[1], MediaBlock)
    await adapter.stop()


@pytest.mark.asyncio
async def test_failed_run_sends_error_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(return_value=make_failed_run(session_id=session_id, message="boom"))
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="hello"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    bot.send_message.assert_awaited_once()
    error_text = bot.send_message.await_args.kwargs["text"]
    assert "try again" in error_text.lower()
    assert "boom" not in error_text
    await adapter.stop()


@pytest.mark.asyncio
async def test_trigger_run_exception_does_not_leak_internal_error_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trigger_mock = AsyncMock(side_effect=RuntimeError("internal stack trace"))
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="hello"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    bot.send_message.assert_awaited_once()
    sent_text = bot.send_message.await_args.kwargs["text"]
    assert "internal stack trace" not in sent_text
    await adapter.stop()
