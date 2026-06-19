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
        self.sent_reply_targets: list[str | None] = []
        self.activity_targets: list[str] = []
        self._media_builder = media_builder

    async def send_text(
        self,
        platform_target: str,
        text: str,
        *,
        reply_to_message_id: str | None = None,
    ) -> None:
        self.sent.append((platform_target, text))
        self.sent_reply_targets.append(reply_to_message_id)

    @contextlib.asynccontextmanager
    async def activity_indicator(self, platform_target: str) -> AsyncIterator[None]:
        self.activity_targets.append(platform_target)
        yield

    async def build_media_blocks(self, raw_message: Any) -> list[ContentBlock]:
        if self._media_builder is None:
            raise AssertionError("media builder not configured for this test")
        return await self._media_builder(raw_message)

    def caption_text(self, raw_message: Any) -> str | None:
        return getattr(raw_message, "caption", None)

    @property
    def sent_texts(self) -> list[str]:
        return [text for _target, text in self.sent]


def make_config(
    *,
    dm_scope: str = "per_conversation",
    response_mode: str = "mention",
    mention_patterns: list[str] | None = None,
    owner_user_ids: list[str] | None = None,
    observe_unaddressed: bool = False,
) -> ChannelConfig:
    return ChannelConfig(
        id="tg-assistant",
        platform="telegram",
        agent_id="assistant",
        dm_scope=dm_scope,
        allowed_chat_ids=["12345"],
        token_env_var="TELEGRAM_BOT_TOKEN_TG_ASSISTANT",
        enabled=True,
        response_mode=response_mode,
        mention_patterns=list(mention_patterns or []),
        owner_user_ids=list(owner_user_ids or []),
        observe_unaddressed=observe_unaddressed,
    )


def make_conversation(
    *,
    chat_id: int = 12345,
    user_id: int | str = 50,
    kind: str = "direct",
    user_display_name: str | None = None,
    message_id: str | None = None,
    mentioned_bot: bool = False,
    is_reply_to_bot: bool = False,
) -> ConversationFacts:
    return ConversationFacts(
        platform="telegram",
        channel_id="tg-assistant",
        chat_id=str(chat_id),
        user_id=str(user_id),
        thread_id=None,
        kind=cast(Any, kind),
        user_display_name=user_display_name,
        message_id=message_id,
        mentioned_bot=mentioned_bot,
        is_reply_to_bot=is_reply_to_bot,
    )


def make_command_dispatcher(*, result: object | None = None) -> SimpleNamespace:
    dispatch_result = NotACommand() if result is None else result
    return SimpleNamespace(
        dispatch=Mock(return_value=dispatch_result),
        # Mirrors the real dispatcher closely enough for engine tests: slash-prefixed
        # text counts as a recognized command.
        recognizes=Mock(side_effect=lambda text: text.strip().startswith("/")),
    )


def make_new_only_dispatcher() -> SimpleNamespace:
    """Dispatcher mapping /new to the new_session action and other text to a run."""

    def dispatch_for(_agent_id: str, _session_id: str, text: str) -> object:
        if text.strip() == "/new":
            return CommandAction(name="new_session")
        return NotACommand()

    return SimpleNamespace(
        dispatch=Mock(side_effect=dispatch_for),
        recognizes=Mock(side_effect=lambda text: text.strip().startswith("/")),
    )


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
    response_mode: str = "mention",
    mention_patterns: list[str] | None = None,
    owner_user_ids: list[str] | None = None,
    observe_unaddressed: bool = False,
    trigger_run: AsyncMock | None = None,
    retry_run: AsyncMock | None = None,
    compact_session: AsyncMock | None = None,
    has_active_run: Mock | None = None,
    command_dispatcher: object | None = None,
    transport: FakeTransport | None = None,
) -> tuple[ChannelConversationEngine, ChatSessionManager, AsyncMock, FakeTransport]:
    chat_sessions = ChatSessionManager(tmp_path)
    trigger_mock = trigger_run or AsyncMock()
    trigger_service = SimpleNamespace(
        trigger_run=trigger_mock,
        retry_run=retry_run or AsyncMock(),
        compact_session=compact_session or AsyncMock(return_value="Context compacted."),
        # Synchronous on purpose: the real has_active_run returns a bool, not a
        # coroutine. An AsyncMock would return a truthy coroutine -> always "busy".
        has_active_run=has_active_run or Mock(return_value=False),
    )
    resolved_transport = transport or FakeTransport()
    engine = ChannelConversationEngine(
        make_config(
            dm_scope=dm_scope,
            response_mode=response_mode,
            mention_patterns=mention_patterns,
            owner_user_ids=owner_user_ids,
            observe_unaddressed=observe_unaddressed,
        ),
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

    compact_mock.assert_awaited_once_with("assistant", SESSION_ID, None)
    trigger_mock.assert_not_awaited()
    assert transport.sent == [("12345", "Context compacted.")]
    await engine.stop()


@pytest.mark.asyncio
async def test_compact_command_action_forwards_instruction(tmp_path: Path) -> None:
    compact_mock = AsyncMock(return_value="Context compacted.")
    command_dispatcher = make_command_dispatcher(
        result=CommandAction(name="compact", argument="keep the API design")
    )
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, compact_session=compact_mock, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/compact keep the API design")
    await drain(engine, 12345)

    compact_mock.assert_awaited_once_with("assistant", SESSION_ID, "keep the API design")
    await engine.stop()


@pytest.mark.asyncio
async def test_new_session_command_starts_fresh_session_and_redirects_followups(
    tmp_path: Path,
) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, transport = make_engine(
        tmp_path, trigger_run=trigger_mock, command_dispatcher=make_new_only_dispatcher()
    )

    await engine.handle_inbound_text(make_conversation(), "/new")
    await drain(engine, 12345)

    new_session_id = chat_sessions.get_metadata("assistant", SESSION_ID)[
        engine_module.ACTIVE_SESSION_METADATA_KEY
    ]
    # A distinct session, anchored to the conversation for grouping, was created.
    assert new_session_id != SESSION_ID
    assert new_session_id.startswith(f"{SESSION_ID}-")
    assert chat_sessions.exists("assistant", new_session_id)
    # The previous (anchor) session is left intact and still loadable.
    assert chat_sessions.get("assistant", SESSION_ID).load()
    # /new confirms without triggering a run.
    assert transport.sent_texts == [engine_module._NEW_SESSION_STARTED_REPLY]
    trigger_mock.assert_not_awaited()

    # A later message follows the pointer into the new session.
    await engine.handle_inbound_text(make_conversation(), "after new")
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once()
    assert trigger_mock.await_args is not None
    assert trigger_mock.await_args.args[2] == new_session_id
    await engine.stop()


@pytest.mark.asyncio
async def test_new_session_tags_fresh_session_with_reminder_and_metadata(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(result=CommandAction(name="new_session"))
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/new")
    await drain(engine, 12345)

    new_session_id = chat_sessions.get_metadata("assistant", SESSION_ID)[
        engine_module.ACTIVE_SESSION_METADATA_KEY
    ]
    notes = [
        message
        for message in chat_sessions.get("assistant", new_session_id).load()
        if message.role == "note"
    ]
    assert len(notes) == 1
    assert "Telegram" in (notes[0].content or "")
    metadata = chat_sessions.get_metadata("assistant", new_session_id)
    assert metadata["source_channel_id"] == "tg-assistant"
    assert metadata["platform"] == "telegram"
    assert metadata["platform_conv_id"] == "12345"
    assert metadata["last_reply_target"] == {
        "channel_id": "tg-assistant",
        "platform_target": "12345",
    }
    # The fresh session is not itself a pointer anchor, and tracks no participant.
    assert engine_module.ACTIVE_SESSION_METADATA_KEY not in metadata
    assert "participants" not in metadata
    await engine.stop()


@pytest.mark.asyncio
async def test_new_session_command_refused_while_run_active(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(result=CommandAction(name="new_session"))
    engine, chat_sessions, trigger_mock, transport = make_engine(
        tmp_path,
        command_dispatcher=command_dispatcher,
        has_active_run=Mock(return_value=True),
    )

    await engine.handle_inbound_text(make_conversation(), "/new")
    await drain(engine, 12345)

    assert transport.sent_texts == [engine_module._NEW_SESSION_BUSY_REPLY]
    trigger_mock.assert_not_awaited()
    # No new session and no pointer: the anchor is unchanged.
    metadata = chat_sessions.get_metadata("assistant", SESSION_ID)
    assert engine_module.ACTIVE_SESSION_METADATA_KEY not in metadata
    await engine.stop()


@pytest.mark.asyncio
async def test_channel_without_new_routes_to_derived_anchor(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, _transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)

    # Byte-for-byte the pre-pointer behavior: route straight to the derived id.
    trigger_mock.assert_awaited_once_with("assistant", "hello", SESSION_ID, sender=None)
    metadata = chat_sessions.get_metadata("assistant", SESSION_ID)
    assert engine_module.ACTIVE_SESSION_METADATA_KEY not in metadata
    await engine.stop()


@pytest.mark.asyncio
async def test_ensure_channel_session_follows_pointer_after_new(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(result=CommandAction(name="new_session"))
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/new")
    await drain(engine, 12345)

    new_session_id = chat_sessions.get_metadata("assistant", SESSION_ID)[
        engine_module.ACTIVE_SESSION_METADATA_KEY
    ]
    # Proactive channel_send resolves to the active (pointer) session, not the anchor.
    route = engine.ensure_channel_session(make_conversation())
    assert route == RouteFacts(agent_id="assistant", session_id=new_session_id)
    await engine.stop()


@pytest.mark.asyncio
async def test_new_session_in_one_chat_leaves_other_chat_untouched(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, command_dispatcher=make_new_only_dispatcher()
    )

    await engine.handle_inbound_text(make_conversation(chat_id=12345), "/new")
    await drain(engine, 12345)

    await engine.handle_inbound_text(make_conversation(chat_id=67890), "hello B")
    await drain(engine, 67890)

    # Chat B is unaffected: it routes to its own derived anchor and has no pointer.
    trigger_mock.assert_awaited_once_with(
        "assistant", "hello B", "ch-tg-assistant-67890", sender=None
    )
    metadata_b = chat_sessions.get_metadata("assistant", "ch-tg-assistant-67890")
    assert engine_module.ACTIVE_SESSION_METADATA_KEY not in metadata_b
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
async def test_observed_message_waits_behind_active_channel_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trigger_mock = AsyncMock(
        return_value=Run(run_id="run-active", agent_id="assistant", session_id=SESSION_ID)
    )
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        observe_unaddressed=True,
    )
    relay_started = asyncio.Event()
    release_relay = asyncio.Event()

    async def block_relay(_run: Run, _reply_plan: ReplyPlanFacts) -> None:
        relay_started.set()
        await release_relay.wait()

    monkeypatch.setattr(engine, "_relay_run_events", AsyncMock(side_effect=block_relay))

    await engine.handle_inbound_text(
        make_conversation(kind="group", mentioned_bot=True),
        "hello bot",
    )
    await asyncio.wait_for(relay_started.wait(), timeout=1)

    await engine.handle_inbound_text(
        make_conversation(kind="group", user_display_name="Alice"),
        "side conversation",
    )
    await asyncio.sleep(0)

    notes_before_release = [
        message.content
        for message in chat_sessions.get("assistant", SESSION_ID).load()
        if message.role == "note"
    ]
    assert not any(
        isinstance(content, str) and content.startswith("[channel-message] ")
        for content in notes_before_release
    )
    queue = engine._chat_queues.get("12345")
    assert queue is not None
    assert queue.qsize() == 1

    release_relay.set()
    await drain(engine, 12345)

    notes_after_release = [
        message.content
        for message in chat_sessions.get("assistant", SESSION_ID).load()
        if message.role == "note"
    ]
    assert notes_after_release[-1] == "[channel-message] Alice (50): side conversation"
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
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, response_mode="all"
    )

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
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, response_mode="all"
    )

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
        tmp_path, trigger_run=trigger_mock, transport=transport, response_mode="all"
    )
    conversation = make_conversation(kind="group", user_display_name="Alice")

    await engine.handle_inbound_media(conversation, ("photo",))
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once_with(
        "assistant",
        [block],
        SESSION_ID,
        sender=MessageSender(id="50", display_name="Alice"),
    )
    await engine.stop()


@pytest.mark.asyncio
async def test_group_unaddressed_text_is_dropped_in_mention_mode(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher()
    engine, chat_sessions, trigger_mock, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(kind="group"), "hello everyone")
    await drain(engine, 12345)

    trigger_mock.assert_not_awaited()
    command_dispatcher.dispatch.assert_not_called()
    assert transport.sent == []
    # Dropped messages must not create a Session either.
    assert not chat_sessions.exists("assistant", SESSION_ID)
    await engine.stop()


@pytest.mark.asyncio
async def test_group_unaddressed_text_is_observed_as_note(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher()
    engine, chat_sessions, trigger_mock, transport = make_engine(
        tmp_path,
        command_dispatcher=command_dispatcher,
        observe_unaddressed=True,
    )

    await engine.handle_inbound_text(
        make_conversation(
            kind="group",
            user_id="|50]\r",
            user_display_name="[Alice]\n|",
        ),
        "hello\nworld",
    )
    await drain(engine, 12345)

    notes = [
        message.content
        for message in chat_sessions.get("assistant", SESSION_ID).load()
        if message.role == "note"
    ]
    assert notes == [
        (
            "This session is receiving messages via Telegram "
            "(channel: tg-assistant, chat: 12345).\n"
            "Respond in a style appropriate for Telegram messaging."
        ),
        "[channel-message] Alice (50): hello\nworld",
    ]
    trigger_mock.assert_not_awaited()
    command_dispatcher.dispatch.assert_not_called()
    assert transport.sent == []
    await engine.stop()


@pytest.mark.asyncio
async def test_observed_group_message_updates_metadata_and_participant(tmp_path: Path) -> None:
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path,
        observe_unaddressed=True,
    )

    await engine.handle_inbound_text(
        make_conversation(kind="group", user_display_name="Alice"),
        "hello everyone",
    )
    await drain(engine, 12345)

    metadata = chat_sessions.get_metadata("assistant", SESSION_ID)
    assert metadata["last_reply_target"] == {
        "channel_id": "tg-assistant",
        "platform_target": "12345",
    }
    assert metadata["participants"]["50"]["display_name"] == "Alice"
    assert metadata["participants"]["50"]["last_seen_at"].endswith("+00:00")
    await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mentioned_bot", "is_reply_to_bot"),
    [(True, False), (False, True)],
)
async def test_group_addressed_text_triggers_in_mention_mode(
    tmp_path: Path,
    mentioned_bot: bool,
    is_reply_to_bot: bool,
) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        observe_unaddressed=True,
    )

    await engine.handle_inbound_text(
        make_conversation(
            kind="group", mentioned_bot=mentioned_bot, is_reply_to_bot=is_reply_to_bot
        ),
        "hello bot",
    )
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once()
    notes = chat_sessions.get("assistant", SESSION_ID).load()
    assert not any(
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith("[channel-message] ")
        for message in notes
    )
    await engine.stop()


@pytest.mark.asyncio
async def test_group_wake_word_pattern_matches_case_insensitively(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        mention_patterns=[r"\bvbot\b"],
        observe_unaddressed=True,
    )

    await engine.handle_inbound_text(make_conversation(kind="group"), "Hey VBOT, status?")
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once()
    notes = chat_sessions.get("assistant", SESSION_ID).load()
    assert not any(
        message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith("[channel-message] ")
        for message in notes
    )
    await engine.stop()


@pytest.mark.asyncio
async def test_direct_message_always_triggers_in_mention_mode(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        observe_unaddressed=True,
    )

    await engine.handle_inbound_text(make_conversation(kind="direct"), "hello")
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once()
    note_contents = [
        message.content
        for message in chat_sessions.get("assistant", SESSION_ID).load()
        if message.role == "note"
    ]
    assert not any(
        isinstance(content, str) and content.startswith("[channel-message] ")
        for content in note_contents
    )
    await engine.stop()


@pytest.mark.asyncio
async def test_group_command_from_owner_is_dispatched(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(result=CommandHandled(reply="Run cancelled."))
    engine, _sessions, trigger_mock, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher, owner_user_ids=["50"]
    )

    await engine.handle_inbound_text(make_conversation(kind="group"), "/stop")
    await drain(engine, 12345)

    command_dispatcher.dispatch.assert_called_once_with("assistant", SESSION_ID, "/stop")
    trigger_mock.assert_not_awaited()
    assert transport.sent_texts == ["Run cancelled."]
    await engine.stop()


@pytest.mark.asyncio
async def test_group_command_from_non_owner_is_denied_without_dispatch(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(result=CommandHandled(reply="Run cancelled."))
    engine, chat_sessions, trigger_mock, transport = make_engine(
        tmp_path,
        command_dispatcher=command_dispatcher,
        owner_user_ids=["99"],
        observe_unaddressed=True,
    )

    await engine.handle_inbound_text(make_conversation(kind="group", user_id=50), "/stop")
    await drain(engine, 12345)

    command_dispatcher.dispatch.assert_not_called()
    trigger_mock.assert_not_awaited()
    assert transport.sent == []
    assert not chat_sessions.exists("assistant", SESSION_ID)
    await engine.stop()


@pytest.mark.asyncio
async def test_group_command_denied_for_everyone_when_owner_list_is_empty(
    tmp_path: Path,
) -> None:
    command_dispatcher = make_command_dispatcher(result=CommandHandled(reply="Run cancelled."))
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(kind="group"), "/stop")
    await drain(engine, 12345)

    command_dispatcher.dispatch.assert_not_called()
    assert transport.sent == []
    await engine.stop()


@pytest.mark.asyncio
async def test_group_command_auth_applies_in_all_response_mode(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(result=CommandHandled(reply="Run cancelled."))
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher, response_mode="all"
    )

    await engine.handle_inbound_text(make_conversation(kind="group"), "/stop")
    await drain(engine, 12345)

    command_dispatcher.dispatch.assert_not_called()
    assert transport.sent == []
    await engine.stop()


@pytest.mark.asyncio
async def test_dm_command_is_authorized_without_owner_list(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(result=CommandHandled(reply="Run cancelled."))
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(kind="direct"), "/stop")
    await drain(engine, 12345)

    command_dispatcher.dispatch.assert_called_once_with("assistant", SESSION_ID, "/stop")
    assert transport.sent_texts == ["Run cancelled."]
    await engine.stop()


@pytest.mark.asyncio
async def test_group_reply_references_triggering_message(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, trigger_run=trigger_mock, response_mode="all"
    )

    await engine.handle_inbound_text(
        make_conversation(kind="group", message_id="777"),
        "hello",
    )
    await drain(engine, 12345)

    assert transport.sent == [("12345", "ok")]
    assert transport.sent_reply_targets == ["777"]
    await engine.stop()


@pytest.mark.asyncio
async def test_direct_reply_does_not_reference_message(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(
        make_conversation(kind="direct", message_id="777"),
        "hello",
    )
    await drain(engine, 12345)

    assert transport.sent == [("12345", "ok")]
    assert transport.sent_reply_targets == [None]
    await engine.stop()


@pytest.mark.asyncio
async def test_group_media_without_addressing_is_dropped(tmp_path: Path) -> None:
    transport = FakeTransport()
    trigger_mock = AsyncMock()
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, transport=transport
    )

    await engine.handle_inbound_media(
        make_conversation(kind="group"),
        (SimpleNamespace(caption=None),),
    )
    await drain(engine, 12345)

    trigger_mock.assert_not_awaited()
    assert transport.sent == []
    assert not chat_sessions.exists("assistant", SESSION_ID)
    await engine.stop()


@pytest.mark.asyncio
async def test_group_unaddressed_media_is_observed_without_download(tmp_path: Path) -> None:
    media_builder = AsyncMock(return_value=[])
    transport = FakeTransport(media_builder=media_builder)
    engine, chat_sessions, trigger_mock, _transport = make_engine(
        tmp_path,
        trigger_run=AsyncMock(),
        transport=transport,
        observe_unaddressed=True,
    )

    await engine.handle_inbound_media(
        make_conversation(kind="group", user_display_name="Alice"),
        (SimpleNamespace(caption="look"), SimpleNamespace(caption=None)),
    )
    await drain(engine, 12345)

    notes = [
        message.content
        for message in chat_sessions.get("assistant", SESSION_ID).load()
        if message.role == "note"
    ]
    assert notes[-2:] == [
        "[channel-message] Alice (50): [media] look",
        "[channel-message] Alice (50): [media message]",
    ]
    media_builder.assert_not_awaited()
    trigger_mock.assert_not_awaited()
    assert transport.sent == []
    await engine.stop()


@pytest.mark.asyncio
async def test_group_media_caption_wake_word_triggers(tmp_path: Path) -> None:
    block = MediaBlock(
        type="media", attachment_id="att-1", filename="a.png", media_type="image/png"
    )

    async def media_builder(_raw_message: Any) -> list[ContentBlock]:
        return [block]

    transport = FakeTransport(media_builder=media_builder)
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        transport=transport,
        mention_patterns=[r"\bvbot\b"],
    )

    await engine.handle_inbound_media(
        make_conversation(kind="group"),
        (SimpleNamespace(caption=None), SimpleNamespace(caption="vbot look at this")),
    )
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once()
    await engine.stop()
