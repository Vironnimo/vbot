"""Tests for the platform-neutral ChannelConversationEngine behavior."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

import core.channels.engine as engine_module
from core.attachments import AttachmentTooLargeError, AttachmentTypeNotAllowedError
from core.channels.adapter import (
    ConversationFacts,
    MessageFacts,
    ReplyPlanFacts,
    RouteFacts,
)
from core.channels.channels import ChannelConfig
from core.channels.engine import ChannelConversationEngine
from core.chat import MessageSender
from core.chat.commands import CommandAction, CommandHandled, NotACommand
from core.chat.content_blocks import ContentBlock, MediaBlock, TextBlock
from core.runs import ASSISTANT_OUTPUT_EVENT, Run
from core.sessions import ChatSessionManager

SESSION_ID = "ch-tg-assistant-12345"


class FakeTransport:
    """Minimal ConversationTransport for engine tests; records outbound text."""

    platform_display_name = "Telegram"

    def __init__(
        self,
        *,
        media_builder: Callable[[Any], Awaitable[list[ContentBlock]]] | None = None,
    ) -> None:
        self.sent: list[tuple[str, str]] = []
        self.activity_targets: list[str] = []
        self._media_builder = media_builder

    async def send_text(self, platform_target: str, text: str) -> None:
        self.sent.append((platform_target, text))

    @contextlib.asynccontextmanager
    async def activity_indicator(self, platform_target: str) -> AsyncIterator[None]:
        self.activity_targets.append(platform_target)
        yield

    async def build_media_blocks(self, raw_message: Any) -> list[ContentBlock]:
        if self._media_builder is None:
            raise AssertionError("media builder not configured for this test")
        return await self._media_builder(raw_message)

    @property
    def sent_texts(self) -> list[str]:
        return [text for _target, text in self.sent]


def make_config(*, dm_scope: str = "per_conversation") -> ChannelConfig:
    return ChannelConfig(
        id="tg-assistant",
        platform="telegram",
        agent_id="assistant",
        dm_scope=dm_scope,
        allowed_chat_ids=[12345],
        token_env_var="TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
        enabled=True,
    )


def make_conversation(
    *,
    chat_id: int = 12345,
    user_id: int = 50,
    kind: str = "direct",
    user_display_name: str | None = None,
) -> ConversationFacts:
    return ConversationFacts(
        platform="telegram",
        channel_id="tg-assistant",
        chat_id=str(chat_id),
        user_id=str(user_id),
        thread_id=None,
        kind=cast(Any, kind),
        user_display_name=user_display_name,
    )


def make_command_dispatcher(*, result: object | None = None) -> SimpleNamespace:
    dispatch_result = NotACommand() if result is None else result
    return SimpleNamespace(dispatch=Mock(return_value=dispatch_result))


def make_completed_run(*, output_text: str, session_id: str = SESSION_ID) -> Run:
    run = Run(run_id="run-completed", agent_id="assistant", session_id=session_id)
    run.emit(ASSISTANT_OUTPUT_EVENT, {"message": {"content": output_text}})
    run.mark_completed("ok")
    return run


def make_empty_completed_run(*, session_id: str = SESSION_ID) -> Run:
    run = Run(run_id="run-empty", agent_id="assistant", session_id=session_id)
    run.mark_completed("ok")
    return run


def make_failed_run(*, message: str, session_id: str = SESSION_ID) -> Run:
    run = Run(run_id="run-failed", agent_id="assistant", session_id=session_id)
    run.mark_failed(RuntimeError(message))
    return run


def make_cancelled_run(*, session_id: str = SESSION_ID) -> Run:
    run = Run(run_id="run-cancelled", agent_id="assistant", session_id=session_id)
    run.mark_cancelled()
    return run


def make_engine(
    tmp_path: Path,
    *,
    dm_scope: str = "per_conversation",
    trigger_run: AsyncMock | None = None,
    retry_run: AsyncMock | None = None,
    compact_session: AsyncMock | None = None,
    command_dispatcher: object | None = None,
    transport: FakeTransport | None = None,
) -> tuple[ChannelConversationEngine, ChatSessionManager, AsyncMock, FakeTransport]:
    chat_sessions = ChatSessionManager(tmp_path)
    trigger_mock = trigger_run or AsyncMock()
    trigger_service = SimpleNamespace(
        trigger_run=trigger_mock,
        retry_run=retry_run or AsyncMock(),
        compact_session=compact_session or AsyncMock(return_value="Context compacted."),
    )
    resolved_transport = transport or FakeTransport()
    engine = ChannelConversationEngine(
        make_config(dm_scope=dm_scope),
        cast(Any, trigger_service),
        cast(Any, chat_sessions),
        cast(Any, resolved_transport),
        command_dispatcher=cast(Any, command_dispatcher or make_command_dispatcher()),
    )
    return engine, chat_sessions, trigger_mock, resolved_transport


async def drain(engine: ChannelConversationEngine, platform_target: int) -> None:
    queue = engine._chat_queues.get(str(platform_target))
    if queue is None:
        await asyncio.sleep(0)
        return
    await asyncio.wait_for(queue.join(), timeout=1)


@pytest.mark.parametrize(
    ("dm_scope", "kind", "chat_id", "user_id", "expected"),
    [
        ("per_conversation", "direct", 12345, 987, "ch-tg-assistant-12345"),
        ("main", "direct", 12345, 987, "ch-tg-assistant-main"),
        ("per_peer", "direct", 12345, 987, "ch-tg-assistant-u987"),
        ("per_account_channel_peer", "direct", 12345, 987, "ch-tg-assistant-12345-u987"),
        ("main", "group", -10001, 987, "ch-tg-assistant--10001"),
    ],
)
def test_derive_session_id(
    tmp_path: Path,
    dm_scope: str,
    kind: str,
    chat_id: int,
    user_id: int,
    expected: str,
) -> None:
    engine, _sessions, _trigger, _transport = make_engine(tmp_path, dm_scope=dm_scope)

    session_id = engine._derive_session_id(
        make_conversation(chat_id=chat_id, user_id=user_id, kind=kind)
    )

    assert session_id == expected


@pytest.mark.asyncio
async def test_reminder_note_written_once_and_metadata_recorded(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(
        side_effect=[
            make_completed_run(output_text="first"),
            make_completed_run(output_text="second"),
        ]
    )
    engine, chat_sessions, _trigger, _transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)
    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)

    session = chat_sessions.get("assistant", SESSION_ID)
    notes = [message for message in session.load() if message.role == "note"]
    metadata = chat_sessions.get_metadata("assistant", SESSION_ID)

    assert len(notes) == 1
    assert "Telegram" in (notes[0].content or "")
    assert metadata["source_channel_id"] == "tg-assistant"
    assert metadata["platform"] == "telegram"
    assert metadata["platform_conv_id"] == "12345"
    assert metadata["last_reply_target"] == {
        "channel_id": "tg-assistant",
        "platform_target": "12345",
    }
    await engine.stop()


@pytest.mark.asyncio
async def test_ensure_channel_session_reuses_session_without_extra_reminder(
    tmp_path: Path,
) -> None:
    engine, chat_sessions, _trigger, _transport = make_engine(tmp_path)

    route = engine.ensure_channel_session(make_conversation())
    engine.ensure_channel_session(make_conversation())

    assert route == RouteFacts(agent_id="assistant", session_id=SESSION_ID)
    session = chat_sessions.get("assistant", SESSION_ID)
    notes = [message for message in session.load() if message.role == "note"]
    assert len(notes) == 1
    await engine.stop()


@pytest.mark.asyncio
async def test_completed_run_forwards_final_assistant_output(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="final reply"))
    engine, _sessions, _trigger, transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)

    assert transport.sent == [("12345", "final reply")]
    assert transport.activity_targets == ["12345"]
    await engine.stop()


@pytest.mark.asyncio
async def test_completed_run_without_output_sends_empty_reply(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_empty_completed_run())
    engine, _sessions, _trigger, transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)

    assert transport.sent_texts == [engine_module._EMPTY_ASSISTANT_REPLY]
    await engine.stop()


@pytest.mark.asyncio
async def test_failed_run_sends_generic_failure_reply(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_failed_run(message="boom"))
    engine, _sessions, _trigger, transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)

    assert transport.sent_texts == [engine_module._FAILED_REPLY]
    assert "boom" not in transport.sent_texts[0]
    await engine.stop()


@pytest.mark.asyncio
async def test_cancelled_run_sends_cancellation_reply(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_cancelled_run())
    engine, _sessions, _trigger, transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)

    assert transport.sent_texts == [engine_module._CANCELLED_REPLY]
    await engine.stop()


@pytest.mark.asyncio
async def test_trigger_exception_sends_failure_without_leaking_internals(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    trigger_mock = AsyncMock(side_effect=RuntimeError("internal stack trace"))
    engine, _sessions, _trigger, transport = make_engine(tmp_path, trigger_run=trigger_mock)
    caplog.set_level(logging.ERROR, logger="vbot.channels.engine")

    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)

    assert transport.sent_texts == [engine_module._FAILED_REPLY]
    records = [r for r in caplog.records if r.message.startswith("Channel trigger run failed")]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert "internal stack trace" not in transport.sent_texts[0]
    await engine.stop()


@pytest.mark.asyncio
async def test_handled_command_replies_before_trigger(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(result=CommandHandled(reply="Run cancelled."))
    engine, _sessions, trigger_mock, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/stop")
    await drain(engine, 12345)

    command_dispatcher.dispatch.assert_called_once_with("assistant", SESSION_ID, "/stop")
    trigger_mock.assert_not_awaited()
    assert transport.sent == [("12345", "Run cancelled.")]
    await engine.stop()


@pytest.mark.asyncio
async def test_compact_command_action_replies_in_worker(tmp_path: Path) -> None:
    compact_mock = AsyncMock(return_value="Context compacted.")
    command_dispatcher = make_command_dispatcher(result=CommandAction(name="compact"))
    engine, _sessions, trigger_mock, transport = make_engine(
        tmp_path, compact_session=compact_mock, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/compact")
    await drain(engine, 12345)

    compact_mock.assert_awaited_once_with("assistant", SESSION_ID)
    trigger_mock.assert_not_awaited()
    assert transport.sent == [("12345", "Context compacted.")]
    await engine.stop()


@pytest.mark.asyncio
async def test_new_session_command_action_reports_channel_limitation(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(result=CommandAction(name="new_session"))
    engine, _sessions, trigger_mock, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/new")
    await drain(engine, 12345)

    trigger_mock.assert_not_awaited()
    assert transport.sent == [
        ("12345", "Starting a new session is not available from Telegram channels yet.")
    ]
    await engine.stop()


@pytest.mark.asyncio
async def test_retry_command_action_relays_retried_run(tmp_path: Path) -> None:
    retry_mock = AsyncMock(return_value=make_completed_run(output_text="retried reply"))
    command_dispatcher = make_command_dispatcher(result=CommandAction(name="retry_last_turn"))
    engine, _sessions, trigger_mock, transport = make_engine(
        tmp_path, retry_run=retry_mock, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/retry")
    await drain(engine, 12345)

    retry_mock.assert_awaited_once_with("assistant", SESSION_ID)
    trigger_mock.assert_not_awaited()
    assert transport.sent == [("12345", "retried reply")]
    await engine.stop()


@pytest.mark.asyncio
async def test_unsupported_command_action_reports_channel_limitation(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(
        result=CommandAction(name="handoff", argument=None)
    )
    engine, _sessions, trigger_mock, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/handoff")
    await drain(engine, 12345)

    trigger_mock.assert_not_awaited()
    assert transport.sent == [
        ("12345", "This command is not available from Telegram channels yet.")
    ]
    await engine.stop()


@pytest.mark.asyncio
async def test_compact_action_failure_is_logged_and_replies_generically(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    compact_mock = AsyncMock(side_effect=RuntimeError("compact failed"))
    command_dispatcher = make_command_dispatcher(result=CommandAction(name="compact"))
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, compact_session=compact_mock, command_dispatcher=command_dispatcher
    )
    caplog.set_level(logging.ERROR, logger="vbot.channels.engine")

    await engine.handle_inbound_text(make_conversation(), "/compact")
    await drain(engine, 12345)

    assert transport.sent_texts == [engine_module._FAILED_REPLY]
    records = [r for r in caplog.records if r.message.startswith("Channel command action failed")]
    assert len(records) == 1
    assert "action=compact" in records[0].message
    assert records[0].exc_info is not None
    await engine.stop()


@pytest.mark.asyncio
async def test_stop_command_eagerly_dispatched_while_worker_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_dispatcher = make_command_dispatcher()
    command_dispatcher.dispatch.side_effect = [
        NotACommand(),
        CommandHandled(reply="Run cancelled."),
    ]
    trigger_mock = AsyncMock(
        return_value=Run(run_id="run-active", agent_id="assistant", session_id=SESSION_ID)
    )
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, trigger_run=trigger_mock, command_dispatcher=command_dispatcher
    )

    relay_started = asyncio.Event()
    release_relay = asyncio.Event()

    async def block_relay(_run: Run, _platform_target: str) -> None:
        relay_started.set()
        await release_relay.wait()

    monkeypatch.setattr(engine, "_relay_run_events", AsyncMock(side_effect=block_relay))

    await engine.handle_inbound_text(make_conversation(), "hello")
    await asyncio.wait_for(relay_started.wait(), timeout=1)

    await engine.handle_inbound_text(make_conversation(), "/stop")
    await asyncio.sleep(0)

    assert command_dispatcher.dispatch.call_args_list[0].args == ("assistant", SESSION_ID, "hello")
    assert command_dispatcher.dispatch.call_args_list[1].args == ("assistant", SESSION_ID, "/stop")
    assert trigger_mock.await_count == 1
    assert transport.sent == [("12345", "Run cancelled.")]

    release_relay.set()
    await drain(engine, 12345)
    await engine.stop()


@pytest.mark.asyncio
async def test_non_command_text_queues_behind_blocked_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_dispatcher = make_command_dispatcher()
    command_dispatcher.dispatch.side_effect = [NotACommand(), NotACommand()]
    trigger_mock = AsyncMock(
        return_value=Run(run_id="run-active", agent_id="assistant", session_id=SESSION_ID)
    )
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, command_dispatcher=command_dispatcher
    )

    relay_started = asyncio.Event()
    release_relay = asyncio.Event()

    async def block_relay(_run: Run, _platform_target: str) -> None:
        relay_started.set()
        await release_relay.wait()

    monkeypatch.setattr(engine, "_relay_run_events", AsyncMock(side_effect=block_relay))

    await engine.handle_inbound_text(make_conversation(), "hello")
    await asyncio.wait_for(relay_started.wait(), timeout=1)

    await engine.handle_inbound_text(make_conversation(), "still queued")
    await asyncio.sleep(0)

    assert trigger_mock.await_count == 1
    queue = engine._chat_queues.get("12345")
    assert queue is not None
    assert queue.qsize() == 1

    release_relay.set()
    await drain(engine, 12345)
    await engine.stop()


@pytest.mark.asyncio
async def test_block_content_skips_command_dispatch_and_triggers_run(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    command_dispatcher = make_command_dispatcher(result=CommandHandled(reply="Run cancelled."))
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, trigger_run=trigger_mock, command_dispatcher=command_dispatcher
    )

    content: list[ContentBlock] = [TextBlock(type="text", text="/stop")]
    queued = engine_module._QueuedInboundMessage(
        route=RouteFacts(agent_id="assistant", session_id=SESSION_ID),
        reply_plan=ReplyPlanFacts(channel_id="tg-assistant", platform_target="12345"),
        message=MessageFacts(content=content),
    )

    await engine._process_queued_message(queued)

    command_dispatcher.dispatch.assert_not_called()
    trigger_mock.assert_awaited_once_with("assistant", content, SESSION_ID, sender=None)
    assert transport.sent == [("12345", "ok")]
    await engine.stop()


@pytest.mark.asyncio
async def test_media_failure_isolates_siblings_and_triggers_successful_blocks(
    tmp_path: Path,
) -> None:
    block = MediaBlock(
        type="media", attachment_id="att-1", filename="a.png", media_type="image/png"
    )

    async def media_builder(raw_message: Any) -> list[ContentBlock]:
        if raw_message == "ok":
            return [block]
        raise RuntimeError("download failed")

    transport = FakeTransport(media_builder=media_builder)
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, transport=transport
    )

    queued = engine_module._QueuedInboundMedia(
        route=RouteFacts(agent_id="assistant", session_id=SESSION_ID),
        reply_plan=ReplyPlanFacts(channel_id="tg-assistant", platform_target="12345"),
        messages=("ok", "broken"),
    )

    await engine._process_queued_media(queued)

    assert transport.sent_texts == [engine_module._MEDIA_FAILED_REPLY, "ok"]
    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    assert await_args.args[1] == [block]
    await engine.stop()


@pytest.mark.asyncio
async def test_media_duplicate_failure_replies_are_deduped(tmp_path: Path) -> None:
    async def media_builder(_raw_message: Any) -> list[ContentBlock]:
        raise RuntimeError("download failed")

    transport = FakeTransport(media_builder=media_builder)
    trigger_mock = AsyncMock()
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, transport=transport
    )

    queued = engine_module._QueuedInboundMedia(
        route=RouteFacts(agent_id="assistant", session_id=SESSION_ID),
        reply_plan=ReplyPlanFacts(channel_id="tg-assistant", platform_target="12345"),
        messages=("broken-a", "broken-b"),
    )

    await engine._process_queued_media(queued)

    assert transport.sent_texts == [engine_module._MEDIA_FAILED_REPLY]
    trigger_mock.assert_not_awaited()
    await engine.stop()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AttachmentTypeNotAllowedError("nope"), engine_module._UNSUPPORTED_FILE_REPLY),
        (AttachmentTooLargeError("too big"), engine_module._FILE_TOO_LARGE_REPLY),
        (RuntimeError("other"), engine_module._MEDIA_FAILED_REPLY),
    ],
)
def test_media_failure_reply_mapping(error: Exception, expected: str) -> None:
    assert engine_module._media_failure_reply(error) == expected


@pytest.mark.asyncio
async def test_group_message_triggers_run_with_sender(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, _transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(
        make_conversation(kind="group", user_display_name="Alice"),
        "hello",
    )
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once_with(
        "assistant",
        "hello",
        SESSION_ID,
        sender=MessageSender(id="50", display_name="Alice"),
    )
    await engine.stop()


@pytest.mark.asyncio
async def test_direct_message_triggers_run_without_sender(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, _transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(
        make_conversation(kind="direct", user_display_name="Alice"),
        "hello",
    )
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once_with("assistant", "hello", SESSION_ID, sender=None)
    await engine.stop()


@pytest.mark.asyncio
async def test_group_sender_display_name_falls_back_to_user_id(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, _transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(make_conversation(kind="group"), "hello")
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once_with(
        "assistant",
        "hello",
        SESSION_ID,
        sender=MessageSender(id="50", display_name="50"),
    )
    await engine.stop()


@pytest.mark.asyncio
async def test_participants_metadata_written_for_groups_only(tmp_path: Path) -> None:
    engine, chat_sessions, _trigger, _transport = make_engine(tmp_path)

    engine.prepare_inbound_route(make_conversation(kind="direct", user_display_name="Alice"))
    direct_metadata = chat_sessions.get_metadata("assistant", SESSION_ID)
    assert "participants" not in direct_metadata

    engine.prepare_inbound_route(make_conversation(kind="group", user_display_name="Alice"))
    group_metadata = chat_sessions.get_metadata("assistant", SESSION_ID)
    participants = group_metadata["participants"]
    assert set(participants) == {"50"}
    assert participants["50"]["display_name"] == "Alice"
    assert participants["50"]["last_seen_at"].endswith("+00:00")
    await engine.stop()


@pytest.mark.asyncio
async def test_participants_metadata_updated_on_repeat_messages(tmp_path: Path) -> None:
    engine, chat_sessions, _trigger, _transport = make_engine(tmp_path)

    engine.prepare_inbound_route(
        make_conversation(kind="group", user_id=50, user_display_name="Alice")
    )
    engine.prepare_inbound_route(
        make_conversation(kind="group", user_id=51, user_display_name="Bob")
    )
    engine.prepare_inbound_route(
        make_conversation(kind="group", user_id=50, user_display_name="Alice Renamed")
    )

    participants = chat_sessions.get_metadata("assistant", SESSION_ID)["participants"]
    assert set(participants) == {"50", "51"}
    assert participants["50"]["display_name"] == "Alice Renamed"
    assert participants["51"]["display_name"] == "Bob"
    await engine.stop()


@pytest.mark.asyncio
async def test_media_path_carries_group_sender(tmp_path: Path) -> None:
    block = MediaBlock(
        type="media", attachment_id="att-1", filename="a.png", media_type="image/png"
    )

    async def media_builder(_raw_message: Any) -> list[ContentBlock]:
        return [block]

    transport = FakeTransport(media_builder=media_builder)
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, transport=transport
    )
    conversation = make_conversation(kind="group", user_display_name="Alice")
    route, reply_plan = engine.prepare_inbound_route(conversation)

    engine.enqueue_media(route, reply_plan, ("photo",), conversation=conversation)
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once_with(
        "assistant",
        [block],
        SESSION_ID,
        sender=MessageSender(id="50", display_name="Alice"),
    )
    await engine.stop()
