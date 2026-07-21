"""Tests for TelegramChannelAdapter behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

import core.channels.telegram as telegram_module
from core.channels.adapter import (
    FileData,
)
from core.channels.channels import ChannelConfigError
from core.channels.telegram import (
    TELEGRAM_MESSAGE_LIMIT,
)
from core.extensions import ExtensionRegistry, InteractionButton, purge_extension_modules
from tests.core.channels.telegram_test_support import (
    install_fake_telegram_media,
    make_adapter,
    make_callback_update,
)


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


# --- Interactive messages: outbound buttons ----------------------------------


@pytest.mark.asyncio
async def test_send_with_buttons_attaches_markup_to_last_chunk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    # Two chunks: the keyboard belongs only on the final visible message.
    payload = "x" * (TELEGRAM_MESSAGE_LIMIT + 5)
    await adapter.send(
        payload,
        "12345",
        buttons=[[InteractionButton(label="Milk ⬜", data="chk:milk")]],
    )

    calls = bot.send_message.await_args_list
    assert len(calls) == 2
    assert "reply_markup" not in calls[0].kwargs
    markup = calls[1].kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].text == "Milk ⬜"
    assert markup.inline_keyboard[0][0].callback_data == "chk:milk"
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_rejects_buttons_with_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )

    with pytest.raises(ChannelConfigError, match="cannot be combined with file"):
        await adapter.send(
            "caption",
            "12345",
            files=[FileData(filename="a.png", media_type="image/png", data=b"a")],
            buttons=[[InteractionButton(label="x", data="chk:x")]],
        )

    bot.send_photo.assert_not_awaited()
    bot.send_document.assert_not_awaited()
    await adapter.stop()


# --- Interactive messages: inbound taps --------------------------------------


@pytest.mark.asyncio
async def test_callback_from_allowed_chat_dispatches_interaction_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, Any] = {}

    async def dispatcher(event: Any, responder: Any) -> bool:
        received["event"] = event
        await responder.answer()
        return True

    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    adapter._interaction_dispatcher = dispatcher

    update = make_callback_update(
        chat_id=12345,
        user_id=50,
        data="chk:milk",
        inline_keyboard=[
            [
                SimpleNamespace(text="Milk ⬜", callback_data="chk:milk"),
                SimpleNamespace(text="Eggs ✅", callback_data="chk:eggs"),
            ]
        ],
    )
    await adapter._handle_callback_query(update, SimpleNamespace())

    event = received["event"]
    assert event.platform == "telegram"
    assert event.channel_id == "tg-assistant"
    assert event.chat_id == "12345"
    assert event.user_id == "50"
    assert event.message_id == "777"
    assert event.data == "chk:milk"
    assert event.text == "Shopping list"
    assert event.buttons == (
        (
            InteractionButton(label="Milk ⬜", data="chk:milk"),
            InteractionButton(label="Eggs ✅", data="chk:eggs"),
        ),
    )
    # The handler answered through the responder; no duplicate fallback ack.
    bot.answer_callback_query.assert_awaited_once()
    await adapter.stop()


@pytest.mark.asyncio
async def test_callback_from_denied_chat_is_recorded_and_silently_acked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatched: list[Any] = []

    async def dispatcher(event: Any, responder: Any) -> bool:
        dispatched.append(event)
        return True

    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    adapter._interaction_dispatcher = dispatcher

    answer = AsyncMock()
    update = make_callback_update(chat_id=999, user_id=50, data="chk:milk", answer=answer)
    await adapter._handle_callback_query(update, SimpleNamespace())

    # Denied: recorded, silently acked via the callback shorthand, never dispatched.
    answer.assert_awaited_once()
    bot.answer_callback_query.assert_not_awaited()
    assert dispatched == []
    assert any(entry.chat_id == "999" for entry in adapter.denied_chats())
    await adapter.stop()


@pytest.mark.asyncio
async def test_callback_fallback_ack_fires_when_handler_does_not_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def dispatcher(event: Any, responder: Any) -> bool:
        # Handled, but the handler never answered.
        return True

    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    adapter._interaction_dispatcher = dispatcher

    update = make_callback_update(chat_id=12345, user_id=50, data="chk:milk")
    await adapter._handle_callback_query(update, SimpleNamespace())

    bot.answer_callback_query.assert_awaited_once()
    await adapter.stop()


@pytest.mark.asyncio
async def test_callback_without_dispatcher_still_acks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    # No dispatcher wired (registry not present): the tap is still acknowledged.
    assert adapter._interaction_dispatcher is None

    update = make_callback_update(chat_id=12345, user_id=50, data="chk:milk")
    await adapter._handle_callback_query(update, SimpleNamespace())

    bot.answer_callback_query.assert_awaited_once()
    await adapter.stop()


@pytest.mark.asyncio
async def test_callback_without_data_is_silently_acked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatched: list[Any] = []

    async def dispatcher(event: Any, responder: Any) -> bool:
        dispatched.append(event)
        return True

    adapter, _chat_sessions, _trigger_mock, _bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    adapter._interaction_dispatcher = dispatcher

    answer = AsyncMock()
    update = make_callback_update(chat_id=12345, user_id=50, data="", answer=answer)
    await adapter._handle_callback_query(update, SimpleNamespace())

    answer.assert_awaited_once()
    assert dispatched == []
    await adapter.stop()


@pytest.mark.asyncio
async def test_run_prefix_tap_wakes_agent_and_bypasses_dispatcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatched: list[Any] = []

    async def dispatcher(event: Any, responder: Any) -> bool:
        dispatched.append(event)
        return True

    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    adapter._interaction_dispatcher = dispatcher
    wake = AsyncMock(return_value="enqueued")
    adapter._engine.trigger_interaction_reply = wake  # type: ignore[method-assign]

    update = make_callback_update(
        chat_id=12345,
        user_id=50,
        data="run:done",
        inline_keyboard=[[SimpleNamespace(text="Fertig ✅", callback_data="run:done")]],
    )
    await adapter._handle_callback_query(update, SimpleNamespace())

    # Reserved-prefix tap: acked once so the spinner stops, routed to the engine to
    # wake the agent, extension dispatcher bypassed, and — authorized — the keyboard
    # is closed (reply_markup=None) so the message reads as submitted.
    bot.answer_callback_query.assert_awaited_once()
    wake.assert_called_once()
    call_args = wake.call_args
    assert call_args is not None
    conversation, event = call_args.args
    assert event.data == "run:done"
    assert conversation.chat_id == "12345"
    assert dispatched == []
    bot.edit_message_reply_markup.assert_awaited_once()
    assert bot.edit_message_reply_markup.await_args.kwargs["reply_markup"] is None
    await adapter.stop()


@pytest.mark.asyncio
async def test_run_prefix_tap_denied_does_not_close_keyboard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    # Engine reports the tap unauthorized (a non-owner group tap): the adapter still
    # acks the spinner but must NOT close the shared message's keyboard.
    wake = AsyncMock(return_value="denied")
    adapter._engine.trigger_interaction_reply = wake  # type: ignore[method-assign]

    update = make_callback_update(chat_id=12345, user_id=99, data="run:done")
    await adapter._handle_callback_query(update, SimpleNamespace())

    bot.answer_callback_query.assert_awaited_once()
    wake.assert_called_once()
    bot.edit_message_reply_markup.assert_not_awaited()
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_text", "expected_alert"),
    [
        ("already_handled", "This action was already handled.", False),
        ("unavailable", "This action is no longer available.", True),
    ],
)
async def test_terminal_run_button_outcome_closes_keyboard_with_clear_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    expected_text: str,
    expected_alert: bool,
) -> None:
    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    adapter._engine.trigger_interaction_reply = AsyncMock(  # type: ignore[method-assign]
        return_value=outcome
    )

    update = make_callback_update(chat_id=12345, user_id=50, data="run:done")
    await adapter._handle_callback_query(update, SimpleNamespace())

    bot.answer_callback_query.assert_awaited_once_with(
        callback_query_id="cb1",
        text=expected_text,
        show_alert=expected_alert,
    )
    bot.edit_message_reply_markup.assert_awaited_once()
    await adapter.stop()


@pytest.mark.asyncio
async def test_non_run_prefix_tap_still_dispatches_to_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dispatched: list[str] = []

    async def dispatcher(event: Any, responder: Any) -> bool:
        dispatched.append(event.data)
        await responder.answer()
        return True

    adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    adapter._interaction_dispatcher = dispatcher
    wake = Mock()
    adapter._engine.trigger_interaction_reply = wake  # type: ignore[method-assign]

    update = make_callback_update(chat_id=12345, user_id=50, data="chk:milk")
    await adapter._handle_callback_query(update, SimpleNamespace())

    # A non-reserved prefix is unchanged: dispatched to the extension, engine untouched.
    assert dispatched == ["chk:milk"]
    wake.assert_not_called()
    bot.answer_callback_query.assert_awaited_once()
    await adapter.stop()


@pytest.mark.asyncio
async def test_responder_answer_calls_bot_and_marks_answered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    responder = telegram_module._TelegramInteractionResponder(
        bot,
        callback_id="cb1",
        chat_id=12345,
        message_id=777,
        channel_id="tg-assistant",
    )

    assert responder.answered is False
    await responder.answer("Saved", alert=True)

    assert responder.answered is True
    bot.answer_callback_query.assert_awaited_once()
    kwargs = bot.answer_callback_query.await_args.kwargs
    assert kwargs["callback_query_id"] == "cb1"
    assert kwargs["text"] == "Saved"
    assert kwargs["show_alert"] is True


@pytest.mark.asyncio
async def test_responder_edit_renders_buttons_and_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_fake_telegram_media(monkeypatch)
    _adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    responder = telegram_module._TelegramInteractionResponder(
        bot,
        callback_id="cb1",
        chat_id=12345,
        message_id=777,
        channel_id="tg-assistant",
    )

    await responder.edit(buttons=[[InteractionButton(label="Milk ✅", data="chk:milk")]])

    bot.edit_message_reply_markup.assert_awaited_once()
    kwargs = bot.edit_message_reply_markup.await_args.kwargs
    assert kwargs["chat_id"] == 12345
    assert kwargs["message_id"] == 777
    assert kwargs["reply_markup"].inline_keyboard[0][0].text == "Milk ✅"
    assert kwargs["reply_markup"].inline_keyboard[0][0].callback_data == "chk:milk"

    await responder.edit(text="Updated")

    bot.edit_message_text.assert_awaited_once()
    text_kwargs = bot.edit_message_text.await_args.kwargs
    assert text_kwargs["text"] == "Updated"
    assert text_kwargs["chat_id"] == 12345
    assert text_kwargs["message_id"] == 777
    assert text_kwargs["reply_markup"] is None


@pytest.mark.asyncio
async def test_responder_edit_empty_buttons_removes_keyboard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
    )
    responder = telegram_module._TelegramInteractionResponder(
        bot,
        callback_id="cb1",
        chat_id=12345,
        message_id=777,
        channel_id="tg-assistant",
    )

    await responder.edit(buttons=[])

    # An empty keyboard removes the inline keyboard (reply_markup=None), not an empty
    # InlineKeyboardMarkup which Telegram would not clear.
    bot.edit_message_reply_markup.assert_awaited_once()
    assert bot.edit_message_reply_markup.await_args.kwargs["reply_markup"] is None


def test_markup_to_buttons_empty_without_keyboard() -> None:
    assert telegram_module._markup_to_buttons(None) == ()
    assert telegram_module._markup_to_buttons([]) == ()


@pytest.mark.asyncio
async def test_tap_flows_through_real_checklist_extension_end_to_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End-to-end: a real callback_query on an allowlisted chat → the adapter's
    # callback handler → the registry dispatcher (as the runtime injects it) → the
    # shipped checklist extension → the responder edits the keyboard on the wire.
    install_fake_telegram_media(monkeypatch)
    bundled_dir = Path(__file__).resolve().parents[3] / "resources" / "extensions"
    registry = ExtensionRegistry.load(
        tmp_path / "does-not-exist-data-extensions",
        bundled_dir=bundled_dir,
    )
    try:
        adapter, _chat_sessions, _trigger_mock, bot = make_adapter(
            tmp_path,
            monkeypatch,
            allowed_chat_ids=[12345],
        )
        adapter._interaction_dispatcher = registry.dispatch_channel_interaction

        update = make_callback_update(
            chat_id=12345,
            user_id=50,
            data="chk:milk",
            inline_keyboard=[
                [
                    SimpleNamespace(text="⬜ Milk", callback_data="chk:milk"),
                    SimpleNamespace(text="⬜ Eggs", callback_data="chk:eggs"),
                ]
            ],
        )
        await adapter._handle_callback_query(update, SimpleNamespace())

        # Only the tapped "Milk" flipped ⬜→✅; "Eggs" and every callback data survive.
        bot.edit_message_reply_markup.assert_awaited_once()
        markup = bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
        row = markup.inline_keyboard[0]
        assert (row[0].text, row[0].callback_data) == ("✅ Milk", "chk:milk")
        assert (row[1].text, row[1].callback_data) == ("⬜ Eggs", "chk:eggs")
        # The tap was acknowledged (spinner stops), exactly once.
        bot.answer_callback_query.assert_awaited_once()
        await adapter.stop()
    finally:
        # This file has no vbot_ext cleanup fixture; drop the loaded extension
        # modules so a later extension-loading test starts clean.
        purge_extension_modules()
