"""Tests for TelegramChannelAdapter behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

import core.channels.engine as engine_module
import core.channels.telegram as telegram_module
from tests.core.channels.telegram_test_support import (
    CHANNEL_REPLY_SURFACE,
    drain_chat_queue,
    make_adapter,
    make_command_dispatcher,
    make_completed_run,
    make_group_update,
    make_migration_update,
    make_update,
)


@pytest.mark.parametrize(
    ("message_fields", "expected"),
    [
        (
            {"location": SimpleNamespace(latitude=52.5, longitude=13.4)},
            "[location shared] latitude 52.5, longitude 13.4",
        ),
        (
            {
                "venue": SimpleNamespace(
                    title="Cafe",
                    address="Main St 1",
                    location=SimpleNamespace(latitude=52.5, longitude=13.4),
                )
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
    ],
)
def test_render_structured_message_variants(
    message_fields: dict[str, Any],
    expected: str,
) -> None:
    base_fields: dict[str, Any] = {"venue": None, "location": None, "contact": None, "poll": None}
    message = SimpleNamespace(**{**base_fields, **message_fields})

    assert telegram_module._render_structured_message(message) == expected


def test_render_structured_message_returns_none_without_payload() -> None:
    message = SimpleNamespace(venue=None, location=None, contact=None, poll=None)

    assert telegram_module._render_structured_message(message) is None


@pytest.mark.asyncio
async def test_location_message_triggers_run_with_rendered_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="ok")
    )
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=12345),
        effective_user=SimpleNamespace(id=50),
        effective_message=SimpleNamespace(
            text=None,
            venue=None,
            location=SimpleNamespace(latitude=52.5, longitude=13.4),
            contact=None,
            poll=None,
            message_thread_id=None,
        ),
    )

    await adapter._handle_inbound_structured_message(update, SimpleNamespace())
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    assert trigger_mock.await_args is not None
    assert trigger_mock.await_args.args[1] == "[location shared] latitude 52.5, longitude 13.4"
    await adapter.stop()


@pytest.mark.asyncio
async def test_structured_message_ignores_disallowed_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trigger_mock = AsyncMock()
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
    )
    update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=99999),
        effective_user=SimpleNamespace(id=50),
        effective_message=SimpleNamespace(
            text=None,
            venue=None,
            location=SimpleNamespace(latitude=52.5, longitude=13.4),
            contact=None,
            poll=None,
            message_thread_id=None,
        ),
    )

    await adapter._handle_inbound_structured_message(update, SimpleNamespace())
    await drain_chat_queue(adapter, 99999)

    trigger_mock.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_dm_start_command_triggers_internal_greeting_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="Hi, I'm vBot!")
    )
    command_dispatcher = make_command_dispatcher()
    adapter, chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
        command_dispatcher=command_dispatcher,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/start"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    # The literal "/start" never reaches command dispatch or the model as user text;
    # an internal note-driven run carries the greeting instruction instead.
    command_dispatcher.execute.assert_not_awaited()
    trigger_mock.assert_awaited_once()
    assert trigger_mock.await_args is not None
    assert trigger_mock.await_args.kwargs.get("internal") is True
    assert trigger_mock.await_args.kwargs.get("reply_surface") == CHANNEL_REPLY_SURFACE
    prompt = trigger_mock.await_args.args[1]
    assert "/start" in prompt
    assert "Greet them" in prompt
    bot.send_message.assert_awaited_once_with(chat_id=12345, text="Hi, I'm vBot!")

    # The instruction is persisted as a note, not as a user message.
    messages = chat_sessions.get("assistant", session_id).load()
    assert not any(message.role == "user" for message in messages)
    await adapter.stop()


@pytest.mark.asyncio
async def test_dm_start_command_forwards_deep_link_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_id = "ch-tg-assistant-12345"
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=session_id, output_text="Welcome!")
    )
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger_mock,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/start promo-2026"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_awaited_once()
    assert trigger_mock.await_args is not None
    assert 'start parameter "promo-2026"' in trigger_mock.await_args.args[1]
    await adapter.stop()


@pytest.mark.asyncio
async def test_group_start_command_keeps_normal_command_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trigger_mock = AsyncMock()
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-10001],
        trigger_run=trigger_mock,
    )

    # No owner_user_ids: a group /start is an unauthorized group command and is dropped.
    await adapter._handle_inbound_message(
        make_group_update(text="/start"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, -10001)

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    await adapter.stop()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/start", ""),
        ("/start promo", "promo"),
        ("/start  promo  ", "promo"),
        ("/started", None),
        ("start", None),
        ("hello /start", None),
    ],
)
def test_parse_start_command(text: str, expected: str | None) -> None:
    assert telegram_module._parse_start_command(text) == expected


@pytest.mark.asyncio
async def test_chat_migration_swaps_allowlist_bridges_session_and_confirms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_anchor = "ch-tg-assistant--500"
    persister = Mock()
    trigger_mock = AsyncMock(
        return_value=make_completed_run(session_id=old_anchor, output_text="ok")
    )
    adapter, chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-500],
        response_mode="all",
        trigger_run=trigger_mock,
        chat_migration_persister=persister,
    )

    # Seed the group conversation with history under the old chat id.
    await adapter._handle_inbound_message(
        make_update(chat_id=-500, user_id=50, text="hello"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, -500)

    await adapter._handle_chat_migration(
        make_migration_update(chat_id=-500, migrate_to=-100500),
        SimpleNamespace(),
    )

    assert adapter._is_chat_allowed("-100500")
    assert not adapter._is_chat_allowed("-500")
    persister.assert_called_once_with("-500", "-100500")

    # The new chat id's anchor points at the old conversation's session.
    new_anchor_metadata = chat_sessions.get_metadata("assistant", "ch-tg-assistant--100500")
    assert new_anchor_metadata[engine_module.ACTIVE_SESSION_METADATA_KEY] == old_anchor

    # The live session's channel sidecar targets the new chat id.
    session_metadata = chat_sessions.get_metadata("assistant", old_anchor)
    assert session_metadata["platform_conv_id"] == "-100500"
    assert session_metadata["last_reply_target"]["platform_target"] == "-100500"

    # The model learns about the migration through a session note.
    notes = [
        message.content
        for message in chat_sessions.get("assistant", old_anchor).load()
        if message.role == "note"
    ]
    assert any("migrated" in (content or "") for content in notes)

    # The confirmation courtesy note goes to the new chat.
    confirmation = bot.send_message.await_args_list[-1]
    assert confirmation.kwargs["chat_id"] == -100500
    assert "new chat id" in confirmation.kwargs["text"]

    # An inbound message in the new supergroup continues the same session.
    trigger_mock.reset_mock()
    await adapter._handle_inbound_message(
        make_update(chat_id=-100500, user_id=50, text="still here?"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, -100500)
    trigger_mock.assert_awaited_once()
    assert trigger_mock.await_args is not None
    assert trigger_mock.await_args.args[2] == old_anchor
    await adapter.stop()


@pytest.mark.asyncio
async def test_chat_migration_second_event_is_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persister = Mock()
    adapter, chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-500],
        chat_migration_persister=persister,
    )

    # Telegram announces a migration twice: once in the old chat, once in the new.
    await adapter._handle_chat_migration(
        make_migration_update(chat_id=-500, migrate_to=-100500),
        SimpleNamespace(),
    )
    await adapter._handle_chat_migration(
        make_migration_update(chat_id=-100500, migrate_from=-500),
        SimpleNamespace(),
    )

    persister.assert_called_once_with("-500", "-100500")
    assert adapter._is_chat_allowed("-100500")
    # No prior conversation existed, so no anchor session is fabricated.
    assert not chat_sessions.exists("assistant", "ch-tg-assistant--100500")
    await adapter.stop()


@pytest.mark.asyncio
async def test_chat_migration_from_new_chat_event_swaps_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-500],
    )

    await adapter._handle_chat_migration(
        make_migration_update(chat_id=-100500, migrate_from=-500),
        SimpleNamespace(),
    )

    assert adapter._is_chat_allowed("-100500")
    assert not adapter._is_chat_allowed("-500")
    await adapter.stop()


@pytest.mark.asyncio
async def test_chat_migration_ignores_unallowlisted_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    persister = Mock()
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        chat_migration_persister=persister,
    )

    await adapter._handle_chat_migration(
        make_migration_update(chat_id=-500, migrate_to=-100500),
        SimpleNamespace(),
    )

    persister.assert_not_called()
    assert not adapter._is_chat_allowed("-100500")
    bot.send_message.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
async def test_unsupported_message_type_ignores_disallowed_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    voice_update = SimpleNamespace(
        effective_chat=SimpleNamespace(id=99999),
        effective_user=SimpleNamespace(id=50),
        effective_message=SimpleNamespace(text=None, message_thread_id=None),
    )

    await adapter._handle_unsupported_message_type(voice_update, SimpleNamespace())

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_not_awaited()
    await adapter.stop()
