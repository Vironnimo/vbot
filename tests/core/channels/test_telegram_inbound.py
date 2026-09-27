"""Telegram inbound messages: allowlist, routing, group addressing, commands and migrations."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

import core.channels._telegram_inbound as telegram_inbound
from core.attachments import AttachmentStore
from core.channels import ChannelConfigError
from core.chat import MessageSender
from core.chat.content_blocks import MediaBlock
from core.sessions import SessionAddress

from .engine_test_support import (
    CHANNEL_GROUP_REPLY_SURFACE,
    CHANNEL_REPLY_SURFACE,
    QUEUE_DRAIN_TIMEOUT_SECONDS,
    SESSION_ID,
    HeldRuns,
    MemoryChannelAccessRegistry,
    assert_member_trigger,
    channel_state,
    make_completed_run,
)
from .telegram_test_support import (
    PNG_BYTES,
    ManualClock,
    drain_chat_queue,
    make_adapter,
    make_media_update,
    make_migration_update,
    make_update,
    make_workflow_dispatcher,
    photo,
    sent_texts,
    telegram_file,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")

_GROUP_SESSION = "ch-tg-assistant--10001"
_UNSUPPORTED_REPLY = "Sorry, this message type isn't supported yet."


def _address(session_id: str) -> SessionAddress:
    return SessionAddress(project_id=None, agent_id="assistant", session_id=session_id)


def _run_texts(trigger: AsyncMock) -> list[Any]:
    return [awaited.args[1] for awaited in trigger.await_args_list]


# --- Allowlist and routing ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("update", "kind", "display_name"),
    [
        (make_update(chat_id=-10099, chat_title=" Team Chat "), "group", "Team Chat"),
        (make_update(chat_id=-10099), "group", None),
        (
            make_update(chat_id=99999, user_full_name="Julian B.", user_username="jb"),
            "direct",
            "Julian B.",
        ),
        (make_update(chat_id=99999, user_full_name="  ", user_username="alice"), "direct", "alice"),
        (make_update(chat_id=99999), "direct", None),
    ],
    ids=["group-title", "group-untitled", "sender-name", "sender-username", "sender-unnamed"],
)
async def test_denied_chats_are_recorded_for_status_without_logging_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    update: SimpleNamespace,
    kind: str,
    display_name: str | None,
) -> None:
    adapter, _sessions, trigger, bot = make_adapter(tmp_path, monkeypatch, allowed_chat_ids=[12345])
    caplog.set_level(logging.DEBUG, logger="vbot.channels.telegram")

    await adapter._handle_inbound_message(update, None)
    await adapter._handle_inbound_message(update, None)

    chat_id = str(update.effective_chat.id)
    [entry] = adapter.denied_chats()
    assert (entry.chat_id, entry.kind, entry.display_name, entry.count) == (
        chat_id,
        kind,
        display_name,
        2,
    )
    trigger.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    # Chat ids and names stay in channel status; the log names only the kind.
    assert chat_id not in caplog.text
    assert display_name is None or display_name not in caplog.text
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler",
    [
        "_handle_inbound_media",
        "_handle_inbound_structured_message",
        "_handle_unsupported_message_type",
    ],
)
async def test_every_message_handler_refuses_chats_outside_the_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handler: str
) -> None:
    adapter, _sessions, trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        attachment_store=AttachmentStore(tmp_path),
    )
    update = make_media_update(
        chat_id=99999, photo=photo("u1"), location=SimpleNamespace(latitude=1.0, longitude=2.0)
    )

    await getattr(adapter, handler)(update, None)
    await drain_chat_queue(adapter, 99999)

    assert [entry.chat_id for entry in adapter.denied_chats()] == ["99999"]
    trigger.assert_not_awaited()
    bot.get_file.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_a_forum_topic_shares_its_groups_session_and_access_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trigger = AsyncMock(
        return_value=make_completed_run(output_text="ok", session_id=_GROUP_SESSION)
    )
    registry = MemoryChannelAccessRegistry()
    adapter, sessions, _trigger, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        dm_scope="main",
        allowed_chat_ids=[-10001],
        response_mode="all",
        trigger_run=trigger,
        access_registry=registry,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=-10001, message_thread_id=42, is_topic_message=True), None
    )
    await drain_chat_queue(adapter, -10001)

    # Groups (negative chat ids) ignore dm_scope and share one Session per chat.
    assert sessions.exists(_address(_GROUP_SESSION))
    assert_member_trigger(
        trigger,
        "assistant",
        "hello",
        _GROUP_SESSION,
        sender=MessageSender(id="50", display_name="50"),
        reply_surface=CHANNEL_GROUP_REPLY_SURFACE,
    )
    assert registry.participants == {"-10001": {"50": "50"}}
    await adapter.stop()


@pytest.mark.asyncio
async def test_outbound_sessions_are_routed_by_the_target_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, sessions, _trigger, _bot = make_adapter(
        tmp_path, monkeypatch, allowed_chat_ids=[12345]
    )

    direct = await adapter.ensure_outbound_session("12345")
    await adapter.ensure_outbound_session("12345")
    group = await adapter.ensure_outbound_session("-10001", thread_id="42")

    assert (direct.agent_id, direct.session_id, group.session_id) == (
        "assistant",
        SESSION_ID,
        _GROUP_SESSION,
    )
    metadata = sessions.get_metadata(_address(SESSION_ID))
    assert (metadata["source_channel_id"], metadata["platform"], metadata["platform_conv_id"]) == (
        "tg-assistant",
        "telegram",
        "12345",
    )
    assert metadata["last_reply_target"] == {
        "channel_id": "tg-assistant",
        "platform_target": "12345",
    }
    # A proactive target has no real sender, so no participant is recorded.
    assert "participants" not in metadata
    assert [
        message for message in sessions.get(_address(SESSION_ID)).load() if message.role == "note"
    ] == []
    assert sessions.get_metadata(_address(_GROUP_SESSION))["last_reply_target"] == {
        "channel_id": "tg-assistant",
        "platform_target": "-10001",
        "thread_id": "42",
    }
    with pytest.raises(ChannelConfigError):
        await adapter.ensure_outbound_session("not-a-chat-id")
    await adapter.stop()


# --- Group addressing -----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("update", "addressed"),
    [
        (make_update(chat_id=-10001, text="hi @MyBot, are you there?"), True),
        (make_update(chat_id=-10001, text="hey HELPFUL   bot!"), True),
        (make_update(chat_id=-10001, text="what did you mean?", reply_to_user_id=999), True),
        (make_media_update(chat_id=-10001, caption="@MyBot look at this", photo=photo("u1")), True),
        (make_update(chat_id=-10001, text="hi @MyBotty"), False),
        (make_update(chat_id=-10001, text="Unhelpful Bot"), False),
        (make_update(chat_id=-10001, text="Helpful Botany"), False),
        (make_update(chat_id=-10001, text="just chatting"), False),
    ],
    ids=[
        "username",
        "visible-name",
        "reply-to-bot",
        "caption",
        "longer-username",
        "name-inside-word",
        "name-prefix",
        "unaddressed",
    ],
)
async def test_group_messages_are_addressed_by_the_bot_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, update: SimpleNamespace, addressed: bool
) -> None:
    trigger = AsyncMock(
        return_value=make_completed_run(output_text="ok", session_id=_GROUP_SESSION)
    )
    adapter, sessions, _trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        trigger_run=trigger,
        attachment_store=AttachmentStore(tmp_path),
        bot_username="MyBot",
        bot_display_name="Helpful Bot",
        bot_id=999,
    )
    bot.get_file.return_value = telegram_file(PNG_BYTES)
    is_media = hasattr(update.effective_message, "photo")
    handler = adapter._handle_inbound_media if is_media else adapter._handle_inbound_message

    await handler(update, None)
    await drain_chat_queue(adapter, -10001)

    assert trigger.await_count == int(addressed)
    assert sessions.exists(_address(_GROUP_SESSION)) is addressed
    await adapter.stop()


# --- Commands and /start --------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "run_text", "reply"),
    [
        ("/workflow@mybot coder", None, "Workflow coder complete."),
        ("/workflow@OtherBot coder", "/workflow@OtherBot coder", "ok"),
        ("/agent@MyBot planner", None, "The /agent command is not available through Telegram."),
    ],
    ids=["own-bot", "other-bot", "unavailable-on-telegram"],
)
async def test_a_command_suffix_addresses_only_this_bot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, run_text: str | None, reply: str
) -> None:
    trigger = AsyncMock(return_value=make_completed_run(output_text="ok"))
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger,
        command_dispatcher=make_workflow_dispatcher(),
        bot_username="MyBot",
        bot_id=999,
    )

    await adapter._handle_inbound_message(make_update(text=text), None)
    await drain_chat_queue(adapter, 12345)

    assert _run_texts(trigger) == ([run_text] if run_text else [])
    bot.send_message.assert_awaited_once_with(chat_id=12345, text=reply)
    await adapter.stop()


@pytest.mark.asyncio
async def test_held_text_runs_on_its_own_once_the_settle_window_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Telegram sends a comment typed in the forwarding UI as its own message just
    # before the forwarded media, so text waits briefly for a following forward.
    clock = ManualClock()
    trigger = AsyncMock(return_value=make_completed_run(output_text="ok"))
    adapter, _sessions, _trigger, _bot = make_adapter(
        tmp_path, monkeypatch, allowed_chat_ids=[12345], trigger_run=trigger, clock=clock
    )

    await adapter._handle_inbound_message(make_update(text="hello", message_id=700), None)
    await clock.advance(telegram_inbound._FORWARD_COMMENT_SETTLE_SECONDS)
    await drain_chat_queue(adapter, 12345)

    assert _run_texts(trigger) == ["hello"]
    await adapter.stop()


@pytest.mark.asyncio
async def test_a_command_skips_the_settle_window_after_releasing_held_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = ManualClock()
    trigger = AsyncMock(return_value=make_completed_run(output_text="ok"))
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger,
        command_dispatcher=make_workflow_dispatcher(),
        clock=clock,
    )

    await adapter._handle_inbound_message(make_update(text="look at this", message_id=700), None)
    await adapter._handle_inbound_message(make_update(text="/ping", message_id=701), None)
    # The clock never advances: the held text reached the engine only because
    # the command released it, and the command answered without waiting.
    await drain_chat_queue(adapter, 12345)

    assert _run_texts(trigger) == ["look at this"]
    assert sorted(sent_texts(bot)) == ["ok", "pong"]
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_id", "text", "start_payload"),
    [
        (12345, "/start", ""),
        (12345, "/start  promo-2026  ", "promo-2026"),
        (12345, "/started", None),
        # Groups keep the normal message path for /start.
        (-10001, "/start", None),
    ],
    ids=["bare", "deep-link", "other-text", "group"],
)
async def test_start_in_a_direct_chat_asks_the_agent_to_greet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    chat_id: int,
    text: str,
    start_payload: str | None,
) -> None:
    trigger = AsyncMock(return_value=make_completed_run(output_text="Hi, I'm vBot!"))
    adapter, sessions, _trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[chat_id],
        response_mode="all",
        trigger_run=trigger,
    )

    await adapter._handle_inbound_message(make_update(chat_id=chat_id, text=text), None)
    await drain_chat_queue(adapter, chat_id)

    trigger.assert_awaited_once()
    assert trigger.await_args is not None
    prompt, options = trigger.await_args.args[1], trigger.await_args.kwargs
    if start_payload is None:
        assert prompt == text
        assert "internal" not in options
    else:
        # The literal "/start" never reaches the model as a user message; an
        # internal note asks the agent to greet in its own voice.
        assert "Greet them" in prompt
        # A deep link's parameter is worth surfacing; a bare /start has none.
        assert (f'start parameter "{start_payload}"' in prompt) == bool(start_payload)
        assert options["internal"] is True
        assert options["reply_surface"] == CHANNEL_REPLY_SURFACE
        messages = sessions.get(_address(SESSION_ID)).load()
        assert not any(message.role == "user" for message in messages)
    bot.send_message.assert_awaited_once_with(chat_id=chat_id, text="Hi, I'm vBot!")
    await adapter.stop()


@pytest.mark.asyncio
async def test_handlers_return_once_their_work_is_queued_behind_a_busy_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PTB runs one handler at a time, so no handler may wait for a Run, a queued
    # turn or a media download; immediate commands still answer at once.
    held = HeldRuns()
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=held.trigger,
        command_dispatcher=make_workflow_dispatcher(),
        attachment_store=AttachmentStore(tmp_path),
    )
    bot.get_file.return_value = telegram_file(PNG_BYTES)

    def handled(work: Any) -> Any:
        return asyncio.wait_for(work, timeout=QUEUE_DRAIN_TIMEOUT_SECONDS)

    await handled(adapter._handle_inbound_message(make_update(text="hello"), None))
    await held.wait_started()
    await handled(adapter._handle_inbound_message(make_update(text="still queued"), None))
    await handled(adapter._handle_inbound_media(make_media_update(photo=photo("u1")), None))
    bot.get_file.assert_not_awaited()
    await handled(adapter._handle_inbound_message(make_update(text="/ping"), None))
    assert sent_texts(bot) == ["pong"]

    held.release()
    await drain_chat_queue(adapter, 12345)
    first, second, media = held.contents
    assert (first, second) == ("hello", "still queued")
    assert [type(block) for block in media] == [MediaBlock]
    await adapter.stop()


# --- Location, contact and poll messages; unsupported types --------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "rendered"),
    [
        (
            {"location": SimpleNamespace(latitude=52.5, longitude=13.4)},
            "[location shared] latitude 52.5, longitude 13.4",
        ),
        (
            # A venue also carries its location; the venue rendering wins.
            {
                "venue": SimpleNamespace(
                    title="Cafe",
                    address="Main St 1",
                    location=SimpleNamespace(latitude=52.5, longitude=13.4),
                ),
                "location": SimpleNamespace(latitude=52.5, longitude=13.4),
            },
            "[location shared] Cafe, Main St 1 (latitude 52.5, longitude 13.4)",
        ),
        (
            {
                "contact": SimpleNamespace(
                    first_name="Max", last_name="Muster", phone_number="+49123"
                )
            },
            "[contact shared] Max Muster, phone: +49123",
        ),
        (
            {"contact": SimpleNamespace(first_name="Max", last_name=None, phone_number=None)},
            "[contact shared] Max",
        ),
        (
            {
                "poll": SimpleNamespace(
                    question="Lunch?",
                    options=[SimpleNamespace(text="Yes"), SimpleNamespace(text="No")],
                )
            },
            "[poll] Lunch?\n- Yes\n- No",
        ),
        ({}, None),
    ],
    ids=["location", "venue", "contact", "contact-name-only", "poll", "nothing-renderable"],
)
async def test_structured_messages_reach_the_agent_as_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: dict[str, Any],
    rendered: str | None,
) -> None:
    trigger = AsyncMock(return_value=make_completed_run(output_text="ok"))
    adapter, _sessions, _trigger, _bot = make_adapter(
        tmp_path, monkeypatch, allowed_chat_ids=[12345], trigger_run=trigger
    )

    await adapter._handle_inbound_structured_message(make_media_update(**content), None)
    await drain_chat_queue(adapter, 12345)

    assert _run_texts(trigger) == ([rendered] if rendered else [])
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("update", "reply"),
    [
        (make_update(text=None), (12345, None)),
        # A reply to every unaddressed sticker in a group would be spam.
        (make_update(chat_id=-10001, text=None), None),
        (
            make_update(chat_id=-10001, text=None, message_id=777, reply_to_user_id=999),
            (-10001, 777),
        ),
    ],
    ids=["direct", "unaddressed-group", "addressed-group"],
)
async def test_unsupported_message_types_get_a_polite_reply_when_addressed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    update: SimpleNamespace,
    reply: tuple[int, int | None] | None,
) -> None:
    adapter, _sessions, trigger, bot = make_adapter(
        tmp_path, monkeypatch, allowed_chat_ids=[12345, -10001], bot_username="MyBot", bot_id=999
    )

    await adapter._handle_unsupported_message_type(update, None)

    trigger.assert_not_awaited()
    if reply is None:
        bot.send_message.assert_not_awaited()
    else:
        [sent] = bot.send_message.await_args_list
        reply_parameters = sent.kwargs.get("reply_parameters")
        assert (
            sent.kwargs["chat_id"],
            sent.kwargs["text"],
            getattr(reply_parameters, "message_id", None),
        ) == (reply[0], _UNSUPPORTED_REPLY, reply[1])
    await adapter.stop()


# --- Group to supergroup migration ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_migration_moves_the_allowance_and_conversation_to_the_new_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_anchor = "ch-tg-assistant--500"
    persister = Mock()
    trigger = AsyncMock(return_value=make_completed_run(output_text="ok", session_id=old_anchor))
    adapter, sessions, _trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-500],
        response_mode="all",
        trigger_run=trigger,
        chat_migration_persister=persister,
    )
    await adapter._handle_inbound_message(make_update(chat_id=-500, text="hello"), None)
    await drain_chat_queue(adapter, -500)

    await adapter._handle_chat_migration(
        make_migration_update(chat_id=-500, migrate_to=-100500), None
    )

    persister.assert_called_once_with("-500", "-100500")
    # The new chat id's conversation points at the old conversation's Session,
    # without creating an empty anchor Session for the new chat id.
    assert (
        channel_state(tmp_path).active_session_id("tg-assistant", "ch-tg-assistant--100500")
        == old_anchor
    )
    assert not sessions.exists(_address("ch-tg-assistant--100500"))
    metadata = sessions.get_metadata(_address(old_anchor))
    assert metadata["platform_conv_id"] == "-100500"
    assert metadata["last_reply_target"]["platform_target"] == "-100500"
    notes = [
        message.content
        for message in sessions.get(_address(old_anchor)).load()
        if message.role == "note"
    ]
    assert any("migrated" in (content or "") for content in notes)
    confirmation = bot.send_message.await_args_list[-1].kwargs
    assert confirmation["chat_id"] == -100500
    assert "new chat id" in confirmation["text"]

    trigger.reset_mock()
    await adapter._handle_inbound_message(make_update(chat_id=-100500, text="still here?"), None)
    await adapter._handle_inbound_message(make_update(chat_id=-500, text="old chat"), None)
    await drain_chat_queue(adapter, -100500)
    assert trigger.await_args is not None
    assert trigger.await_args.args[1:3] == ("still here?", old_anchor)
    assert [entry.chat_id for entry in adapter.denied_chats()] == ["-500"]
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "announcements",
    [
        [(-500, {"migrate_to": -100500}), (-100500, {"migrate_from": -500})],
        [(-100500, {"migrate_from": -500}), (-500, {"migrate_to": -100500})],
    ],
    ids=["old-chat-first", "new-chat-first"],
)
async def test_a_migration_announced_in_both_chats_is_applied_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    announcements: list[tuple[int, dict[str, int]]],
) -> None:
    # Without a config persister the swap still applies to the running adapter.
    trigger = AsyncMock(return_value=make_completed_run(output_text="ok"))
    adapter, sessions, _trigger, bot = make_adapter(
        tmp_path, monkeypatch, allowed_chat_ids=[-500], response_mode="all", trigger_run=trigger
    )

    for chat_id, migration in announcements:
        await adapter._handle_chat_migration(
            make_migration_update(chat_id=chat_id, **migration), None
        )

    # One confirmation: the second announcement found the old chat id gone.
    assert [sent.kwargs["chat_id"] for sent in bot.send_message.await_args_list] == [-100500]
    # No earlier conversation existed, so no anchor Session is fabricated.
    assert not sessions.exists(_address("ch-tg-assistant--100500"))
    await adapter._handle_inbound_message(make_update(chat_id=-100500, text="new chat"), None)
    await adapter._handle_inbound_message(make_update(chat_id=-500, text="old chat"), None)
    await drain_chat_queue(adapter, -100500)
    assert _run_texts(trigger) == ["new chat"]
    assert [entry.chat_id for entry in adapter.denied_chats()] == ["-500"]
    await adapter.stop()


@pytest.mark.asyncio
async def test_a_migration_of_a_chat_outside_the_allowlist_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    persister = Mock()
    adapter, _sessions, trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        response_mode="all",
        chat_migration_persister=persister,
    )

    await adapter._handle_chat_migration(
        make_migration_update(chat_id=-500, migrate_to=-100500), None
    )
    await adapter._handle_inbound_message(make_update(chat_id=-100500, text="hello"), None)

    persister.assert_not_called()
    bot.send_message.assert_not_awaited()
    trigger.assert_not_awaited()
    assert [entry.chat_id for entry in adapter.denied_chats()] == ["-100500"]
    await adapter.stop()
