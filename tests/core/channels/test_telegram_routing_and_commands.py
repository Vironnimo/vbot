"""Tests for TelegramChannelAdapter behavior."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

import core.channels.engine as engine_module
import core.channels.telegram as telegram_module
from core.attachments import AttachmentStore, AttachmentTooLargeError
from core.channels.channels import ChannelConfigError
from core.channels.telegram import (
    TelegramChannelAdapter,
)
from core.chat import MessageSender
from core.chat.commands import (
    CommandFeedback,
    CommandOutcome,
    CommandUnavailability,
    PreparedCommand,
)
from core.chat.content_blocks import MediaBlock
from core.runs import Run
from core.sessions import ChatSessionManager, SessionAddress
from tests.core.channels.engine_test_support import (
    assert_member_trigger,
    make_new_only_dispatcher,
)
from tests.core.channels.telegram_test_support import (
    CHANNEL_GROUP_REPLY_SURFACE,
    drain_chat_queue,
    make_adapter,
    make_command_dispatcher,
    make_completed_run,
    make_config,
    make_photo_update,
    make_update,
)


@pytest.mark.parametrize(
    ("chat_id", "expected_kind"),
    [(12345, "direct"), (-10001, "group")],
)
def test_conversation_facts_classifies_kind_by_chat_id_sign(
    chat_id: int, expected_kind: str
) -> None:
    adapter = TelegramChannelAdapter.__new__(TelegramChannelAdapter)
    adapter._config = make_config(allowed_chat_ids=[chat_id])
    adapter._bot_id = None
    adapter._bot_username = None
    adapter._bot_address_patterns = ()

    conversation = adapter._conversation_facts(make_update(chat_id=chat_id, user_id=50, text="hi"))

    assert conversation is not None
    assert conversation.kind == expected_kind


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        (SimpleNamespace(id=50, full_name="Alice Example", username="alice"), "Alice Example"),
        (SimpleNamespace(id=50, full_name="  ", username="alice"), "alice"),
        (SimpleNamespace(id=50, username="alice"), "alice"),
        (SimpleNamespace(id=50, full_name=None, username=None), None),
        (SimpleNamespace(id=50), None),
    ],
)
def test_conversation_facts_display_name_chain(user: SimpleNamespace, expected: str | None) -> None:
    adapter = TelegramChannelAdapter.__new__(TelegramChannelAdapter)
    adapter._config = make_config(allowed_chat_ids=[12345])
    adapter._bot_id = None
    adapter._bot_username = None
    adapter._bot_address_patterns = ()
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=12345),
        effective_user=user,
        effective_message=SimpleNamespace(text="hi", message_thread_id=None),
    )

    conversation = adapter._conversation_facts(update)

    assert conversation is not None
    assert conversation.user_display_name == expected


@pytest.mark.asyncio
async def test_negative_chat_id_routes_to_shared_group_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant--10001"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        dm_scope="main",
        allowed_chat_ids=[-10001],
        response_mode="all",
        trigger_run=trigger_mock,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=-10001, user_id=50, text="hello"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, -10001)

    # Group chats ignore dm_scope and share one session keyed by the chat id.
    assert chat_sessions.exists(
        SessionAddress(project_id=None, agent_id="assistant", session_id=session_id)
    )
    assert_member_trigger(
        trigger_mock,
        "assistant",
        "hello",
        session_id,
        sender=MessageSender(id="50", display_name="50"),
        reply_surface=CHANNEL_GROUP_REPLY_SURFACE,
    )
    await adapter.stop()


def test_constructor_requires_token_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", raising=False)

    with pytest.raises(ChannelConfigError):
        TelegramChannelAdapter(
            make_config(allowed_chat_ids=[12345]),
            trigger_service=cast(Any, SimpleNamespace(trigger_run=AsyncMock())),
            chat_sessions=cast(Any, ChatSessionManager(tmp_path)),
            credential_resolver=lambda key: os.environ.get(key, ""),
            command_dispatcher=cast(Any, make_command_dispatcher()),
        )


def test_constructor_resolves_token_through_injected_credential_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_TG_ASSISTANT", raising=False)

    adapter = TelegramChannelAdapter(
        make_config(allowed_chat_ids=[12345]),
        trigger_service=cast(Any, SimpleNamespace(trigger_run=AsyncMock())),
        chat_sessions=cast(Any, ChatSessionManager(tmp_path)),
        credential_resolver=lambda _key: "runtime-token",
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
async def test_denied_group_chat_is_recorded_with_chat_title(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=-10099, title="Team Chat"),
        effective_user=SimpleNamespace(id=50),
        effective_message=SimpleNamespace(text="hi", message_thread_id=None),
    )
    await adapter._handle_inbound_message(update, SimpleNamespace())
    await adapter._handle_inbound_message(update, SimpleNamespace())

    trigger_mock.assert_not_awaited()
    entries = adapter.denied_chats()
    assert len(entries) == 1
    assert entries[0].chat_id == "-10099"
    assert entries[0].kind == "group"
    assert entries[0].display_name == "Team Chat"
    assert entries[0].count == 2
    await adapter.stop()


@pytest.mark.asyncio
async def test_denied_direct_chat_is_recorded_with_sender_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=99999),
        effective_user=SimpleNamespace(id=99999, full_name="Julian B."),
        effective_message=SimpleNamespace(text="hi", message_thread_id=None),
    )
    await adapter._handle_inbound_message(update, SimpleNamespace())

    entries = adapter.denied_chats()
    assert len(entries) == 1
    assert entries[0].chat_id == "99999"
    assert entries[0].kind == "direct"
    assert entries[0].display_name == "Julian B."
    await adapter.stop()


@pytest.mark.asyncio
async def test_inbound_session_creation_writes_no_reply_surface_note(
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

    session = chat_sessions.get(
        SessionAddress(project_id=None, agent_id="assistant", session_id=session_id)
    )
    notes = [message for message in session.load() if message.role == "note"]
    metadata = chat_sessions.get_metadata(
        SessionAddress(project_id=None, agent_id="assistant", session_id=session_id)
    )

    assert notes == []
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
async def test_typing_indicator_refreshes_chat_action_and_stops_after_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    async with adapter._typing_indicator("12345"):
        await asyncio.sleep(0.05)

    bot.send_chat_action.assert_awaited_with(chat_id=12345, action="typing")
    awaited_during_block = bot.send_chat_action.await_count
    assert awaited_during_block >= 1

    await asyncio.sleep(0.05)
    assert bot.send_chat_action.await_count == awaited_during_block
    await adapter.stop()


@pytest.mark.asyncio
async def test_ensure_outbound_session_creates_session_without_reminder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    route = adapter.ensure_outbound_session("12345")

    assert route.agent_id == "assistant"
    assert route.session_id == "ch-tg-assistant-12345"
    session = chat_sessions.get(
        SessionAddress(project_id=None, agent_id="assistant", session_id="ch-tg-assistant-12345")
    )
    notes = [message for message in session.load() if message.role == "note"]
    assert notes == []
    await adapter.stop()


@pytest.mark.asyncio
async def test_ensure_outbound_session_writes_channel_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    adapter.ensure_outbound_session("12345")

    metadata = chat_sessions.get_metadata(
        SessionAddress(project_id=None, agent_id="assistant", session_id="ch-tg-assistant-12345")
    )
    assert metadata["source_channel_id"] == "tg-assistant"
    assert metadata["platform"] == "telegram"
    assert metadata["platform_conv_id"] == "12345"
    assert metadata["last_reply_target"] == {
        "channel_id": "tg-assistant",
        "platform_target": "12345",
    }
    # A proactive target has no real sender, so no participant is recorded.
    assert "participants" not in metadata
    await adapter.stop()


@pytest.mark.asyncio
async def test_ensure_outbound_session_reuses_existing_session_without_notes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    adapter.ensure_outbound_session("12345")
    adapter.ensure_outbound_session("12345")

    session = chat_sessions.get(
        SessionAddress(project_id=None, agent_id="assistant", session_id="ch-tg-assistant-12345")
    )
    notes = [message for message in session.load() if message.role == "note"]
    assert notes == []
    await adapter.stop()


@pytest.mark.asyncio
async def test_ensure_outbound_session_rejects_non_integer_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    with pytest.raises(ChannelConfigError):
        adapter.ensure_outbound_session("not-a-chat-id")
    await adapter.stop()


@pytest.mark.asyncio
async def test_plain_text_command_is_dispatched_before_trigger_run(
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
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/stop"),
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
async def test_compact_command_action_replies_without_trigger_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_dispatcher = make_command_dispatcher(
        result=CommandOutcome(
            command="compact",
            feedback=CommandFeedback(kind="notice", text="Context compacted."),
        )
    )
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        command_dispatcher=command_dispatcher,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/compact"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    command_dispatcher.execute.assert_awaited_once()
    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(chat_id=12345, text="Context compacted.")
    await adapter.stop()


@pytest.mark.asyncio
async def test_new_command_starts_fresh_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_dispatcher = make_new_only_dispatcher()
    adapter, chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        command_dispatcher=command_dispatcher,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/new"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    anchor_metadata = chat_sessions.get_metadata(
        SessionAddress(project_id=None, agent_id="assistant", session_id="ch-tg-assistant-12345")
    )
    new_session_id = anchor_metadata[engine_module.ACTIVE_SESSION_METADATA_KEY]
    assert new_session_id.startswith("ch-tg-assistant-12345-")
    assert chat_sessions.exists(
        SessionAddress(project_id=None, agent_id="assistant", session_id=new_session_id)
    )
    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(
        chat_id=12345,
        text=engine_module._NEW_SESSION_STARTED_REPLY,
    )
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["/agent", "/agent planner"])
async def test_agent_command_reports_permanent_channel_limitation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    command_dispatcher = make_command_dispatcher(
        result=CommandOutcome(command="agent"),
        unavailable=CommandUnavailability(command="/agent", surface="channel"),
    )
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        command_dispatcher=command_dispatcher,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text=message),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(
        chat_id=12345,
        text="The /agent command is not available through Telegram.",
    )
    command_dispatcher.execute.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_build_application_configures_rate_limiter_with_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    captured: dict[str, Any] = {}

    class FakeRateLimiter:
        def __init__(self, *, max_retries: int) -> None:
            captured["max_retries"] = max_retries

    class FakeBuilder:
        def token(self, token: str) -> FakeBuilder:
            captured["token"] = token
            return self

        def rate_limiter(self, rate_limiter: Any) -> FakeBuilder:
            captured["rate_limiter"] = rate_limiter
            return self

        def build(self) -> Any:
            return SimpleNamespace()

    fake_ext = SimpleNamespace(
        AIORateLimiter=FakeRateLimiter,
        Application=SimpleNamespace(builder=FakeBuilder),
    )

    adapter._build_application(fake_ext)

    assert captured["token"] == "test-token"
    assert isinstance(captured["rate_limiter"], FakeRateLimiter)
    assert captured["max_retries"] == telegram_module._SEND_MAX_RETRIES
    await adapter.stop()


@pytest.mark.asyncio
async def test_build_application_uses_real_rate_limiter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telegram_ext = pytest.importorskip("telegram.ext")
    pytest.importorskip("aiolimiter")
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    application = adapter._build_application(telegram_ext)

    assert isinstance(application.bot.rate_limiter, telegram_ext.AIORateLimiter)
    await adapter.stop()


@pytest.mark.asyncio
async def test_message_handlers_ignore_edited_messages_and_channel_posts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    telegram = pytest.importorskip("telegram")
    telegram_ext = pytest.importorskip("telegram.ext")
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    def make_real_message(**content: Any) -> Any:
        return telegram.Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=telegram.Chat(id=12345, type="private"),
            from_user=telegram.User(id=50, first_name="A", is_bot=False),
            **content,
        )

    (
        migration_handler,
        text_handler,
        media_handler,
        structured_handler,
        unsupported_handler,
        callback_handler,
    ) = adapter._build_message_handlers(telegram_ext)
    text_message = make_real_message(text="hi")
    photo_message = make_real_message(
        photo=[telegram.PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)]
    )
    voice_message = make_real_message(
        voice=telegram.Voice(file_id="v", file_unique_id="vu", duration=2)
    )
    sticker_message = make_real_message(
        sticker=telegram.Sticker(
            file_id="s",
            file_unique_id="su",
            width=512,
            height=512,
            is_animated=False,
            is_video=False,
            type="regular",
        )
    )
    animation_message = make_real_message(
        animation=telegram.Animation(
            file_id="a",
            file_unique_id="au",
            width=64,
            height=64,
            duration=1,
        )
    )

    assert text_handler.check_update(telegram.Update(update_id=1, message=text_message))
    assert not text_handler.check_update(telegram.Update(update_id=2, edited_message=text_message))
    assert not text_handler.check_update(telegram.Update(update_id=3, channel_post=text_message))
    assert media_handler.check_update(telegram.Update(update_id=4, message=photo_message))
    assert not media_handler.check_update(
        telegram.Update(update_id=5, edited_message=photo_message)
    )
    assert media_handler.check_update(telegram.Update(update_id=6, message=voice_message))
    assert unsupported_handler.check_update(telegram.Update(update_id=7, message=sticker_message))
    assert not unsupported_handler.check_update(telegram.Update(update_id=8, message=voice_message))
    assert not unsupported_handler.check_update(
        telegram.Update(update_id=9, edited_message=sticker_message)
    )

    # The callback handler is additive and matches only the distinct callback_query
    # update type, never a plain message.
    callback_update = telegram.Update(
        update_id=10,
        callback_query=telegram.CallbackQuery(
            id="cb1",
            from_user=telegram.User(id=50, first_name="A", is_bot=False),
            chat_instance="ci",
            data="chk:milk",
            message=text_message,
        ),
    )
    assert callback_handler.check_update(callback_update)
    assert not callback_handler.check_update(telegram.Update(update_id=11, message=text_message))
    assert not text_handler.check_update(callback_update)
    # Animations are media, not an unsupported type.
    assert media_handler.check_update(telegram.Update(update_id=10, message=animation_message))
    assert not unsupported_handler.check_update(
        telegram.Update(update_id=11, message=animation_message)
    )
    # Migration service messages route to the dedicated migration handler only.
    migration_message = make_real_message(migrate_to_chat_id=-100123)
    assert migration_handler.check_update(telegram.Update(update_id=12, message=migration_message))
    assert not text_handler.check_update(telegram.Update(update_id=13, message=migration_message))
    assert not unsupported_handler.check_update(
        telegram.Update(update_id=14, message=migration_message)
    )
    # Location/contact/poll route to the structured handler, not the catch-all.
    location_message = make_real_message(location=telegram.Location(longitude=13.4, latitude=52.5))
    contact_message = make_real_message(
        contact=telegram.Contact(phone_number="+491234", first_name="Max")
    )
    poll_message = make_real_message(
        poll=telegram.Poll.de_json(
            {
                "id": "p1",
                "question": "Lunch?",
                "options": [{"text": "Yes", "voter_count": 0, "persistent_id": "yes"}],
                "total_voter_count": 0,
                "is_closed": False,
                "is_anonymous": True,
                "type": "regular",
                "allows_multiple_answers": False,
                "allows_revoting": False,
                "members_only": False,
            },
            None,
        )
    )
    for update_id, structured in (
        (15, location_message),
        (16, contact_message),
        (17, poll_message),
    ):
        assert structured_handler.check_update(
            telegram.Update(update_id=update_id, message=structured)
        )
        assert not unsupported_handler.check_update(
            telegram.Update(update_id=update_id + 10, message=structured)
        )
    # Anything else that is a real user message falls into the catch-all...
    dice_message = make_real_message(dice=telegram.Dice(value=3, emoji="🎲"))
    assert unsupported_handler.check_update(telegram.Update(update_id=30, message=dice_message))
    assert not unsupported_handler.check_update(telegram.Update(update_id=31, message=text_message))
    # ...but service noise (member joined etc.) matches no reply-producing handler.
    status_message = make_real_message(
        new_chat_members=[telegram.User(id=51, first_name="B", is_bot=False)]
    )
    for handler in (text_handler, media_handler, structured_handler, unsupported_handler):
        assert not handler.check_update(telegram.Update(update_id=32, message=status_message))
    await adapter.stop()


@pytest.mark.asyncio
async def test_compact_action_runs_in_worker_and_keeps_handler_unblocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compact_started = asyncio.Event()
    release_compact = asyncio.Event()

    def prepare(text: str) -> PreparedCommand | None:
        if text == "/compact":
            return PreparedCommand(name="compact", argument=None, execution_mode="serialized")
        if text == "/stop":
            return PreparedCommand(name="stop", argument=None, execution_mode="immediate")
        return None

    async def execute(prepared: PreparedCommand, _context: Any) -> CommandOutcome:
        if prepared.name == "compact":
            compact_started.set()
            await release_compact.wait()
            return CommandOutcome(
                command="compact",
                feedback=CommandFeedback(kind="notice", text="Context compacted."),
            )
        return CommandOutcome(
            command="stop",
            feedback=CommandFeedback(kind="notice", text="Run cancelled."),
        )

    command_dispatcher = SimpleNamespace(
        prepare=prepare,
        unavailability=lambda _prepared, _surface: None,
        execute=AsyncMock(side_effect=execute),
    )
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        command_dispatcher=command_dispatcher,
    )

    await asyncio.wait_for(
        adapter._handle_inbound_message(
            make_update(chat_id=12345, user_id=50, text="/compact"),
            SimpleNamespace(),
        ),
        timeout=1,
    )
    await asyncio.wait_for(compact_started.wait(), timeout=1)

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/stop"),
        SimpleNamespace(),
    )
    await asyncio.sleep(0)
    bot.send_message.assert_awaited_once_with(chat_id=12345, text="Run cancelled.")

    release_compact.set()
    await drain_chat_queue(adapter, 12345)

    sent_texts = [call.kwargs["text"] for call in bot.send_message.await_args_list]
    assert sent_texts == ["Run cancelled.", "Context compacted."]
    trigger_mock.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_media_download_runs_in_worker_not_in_handler(
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

    release_download = asyncio.Event()

    async def slow_download() -> bytearray:
        await release_download.wait()
        return bytearray(b"\x89PNG\r\n\x1a\nIMG")

    bot.get_file.return_value = SimpleNamespace(
        download_as_bytearray=AsyncMock(side_effect=slow_download)
    )

    await asyncio.wait_for(
        adapter._handle_inbound_media(
            make_photo_update(
                chat_id=12345,
                user_id=50,
                file_id="photo-1",
                file_unique_id="uniq-1",
            ),
            SimpleNamespace(),
        ),
        timeout=1,
    )
    trigger_mock.assert_not_awaited()

    release_download.set()
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert isinstance(blocks[0], MediaBlock)
    await adapter.stop()


@pytest.mark.asyncio
async def test_oversized_inbound_media_rejected_before_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attachment_store = AttachmentStore(tmp_path, max_size_bytes=8)
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        attachment_store=attachment_store,
    )
    download_mock = AsyncMock(return_value=bytearray(b"\x89PNG\r\n\x1a\nIMG"))
    # get_file returns metadata (including file_size) only; the body is a separate fetch.
    bot.get_file.return_value = SimpleNamespace(
        file_size=1_000_000,
        download_as_bytearray=download_mock,
    )
    raw_message = make_photo_update(
        chat_id=12345,
        user_id=50,
        file_id="photo-1",
        file_unique_id="uniq-1",
    ).effective_message

    with pytest.raises(AttachmentTooLargeError):
        await adapter.build_media_blocks(raw_message)

    # The oversized file is refused on its reported size; the body is never downloaded.
    download_mock.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_album_flush_window_resets_per_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(telegram_module, "_ALBUM_FLUSH_SECONDS", 0.15)
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
            download_as_bytearray=AsyncMock(
                return_value=bytearray(b"\x89PNG\r\n\x1a\n" + bytes([index]))
            )
        )
        for index in range(3)
    ]

    # Items spaced inside the window but with a cumulative span beyond it: without the
    # per-item reset the album would flush after item 2 and split into two Runs.
    for index in range(3):
        await adapter._handle_inbound_media(
            make_photo_update(
                chat_id=12345,
                user_id=50,
                file_id=f"photo-{index}",
                file_unique_id=f"uniq-{index}",
                media_group_id="album-1",
            ),
            SimpleNamespace(),
        )
        await asyncio.sleep(0.1)

    await asyncio.sleep(0.2)
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    blocks = await_args.args[1]
    assert isinstance(blocks, list)
    assert len(blocks) == 3
    await adapter.stop()


@pytest.mark.asyncio
async def test_stop_command_is_eagerly_dispatched_while_chat_worker_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    command_dispatcher = make_command_dispatcher(
        result=CommandOutcome(
            command="stop",
            feedback=CommandFeedback(kind="notice", text="Run cancelled."),
        )
    )
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
        adapter._engine,
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

    # Plain text never touches the dispatcher; the command dispatched eagerly while
    # the worker was still blocked relaying the first message's run.
    command_dispatcher.execute.assert_awaited_once()
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
        adapter._engine,
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

    # Plain text is queued without any dispatcher involvement.
    command_dispatcher.execute.assert_not_awaited()
    assert trigger_mock.await_count == 1

    queue = adapter._engine._chat_queues.get("12345")
    assert queue is not None
    assert queue.qsize() == 1

    release_relay.set()
    await drain_chat_queue(adapter, 12345)
    await adapter.stop()
