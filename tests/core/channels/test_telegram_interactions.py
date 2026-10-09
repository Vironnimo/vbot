"""Telegram interactive messages: outbound buttons and inbound button taps."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.channels import ChannelConfigError
from core.channels.adapter import FileData, RunButtonBinding, bound_run_callback_data
from core.channels.telegram import TELEGRAM_MESSAGE_LIMIT
from core.extensions import (
    InteractionButton,
    InteractionEvent,
    InteractionResponder,
    purge_extension_modules,
)
from core.utils.timestamps import utc_now_timestamp
from tests.resources.extensions.bundled_test_support import load_bundled

from .engine_test_support import MemoryChannelAccessRegistry, channel_state, make_completed_run
from .telegram_test_support import (
    GatedAccessRegistry,
    drain_chat_queue,
    make_adapter,
    make_callback_update,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")

_ALREADY_HANDLED = "This action was already handled."
_UNAVAILABLE = "This action is no longer available."


class _RecordingDispatcher:
    """Interaction dispatcher double that records events and may answer the tap."""

    def __init__(self, *, answer: bool = True) -> None:
        self.events: list[InteractionEvent] = []
        self._answer = answer

    async def __call__(self, event: InteractionEvent, responder: InteractionResponder) -> bool:
        self.events.append(event)
        if self._answer:
            await responder.answer("Saved", alert=True)
        return True


# --- Outbound buttons -----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["send", "reply", "unreferenced_reply"])
async def test_buttons_ride_on_the_last_chunk_of_a_split_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path, monkeypatch, allowed_chat_ids=[12345]
    )

    text = "x" * (TELEGRAM_MESSAGE_LIMIT * 2 + 9)
    buttons = [[InteractionButton(label="Milk ⬜", data="chk:milk")]]
    if entry == "send":
        await adapter.send(text, "12345", buttons=buttons, thread_id="42")
    else:
        await adapter._transport.send_text(
            "12345",
            text,
            buttons=buttons,
            thread_id="42",
            reply_to_message_id="777" if entry == "reply" else None,
        )

    chunks = [sent.kwargs for sent in bot.send_message.await_args_list]
    assert [len(chunk["text"]) for chunk in chunks] == [
        TELEGRAM_MESSAGE_LIMIT,
        TELEGRAM_MESSAGE_LIMIT,
        9,
    ]
    assert ["reply_markup" in chunk for chunk in chunks] == [False, False, True]
    assert [chunk["message_thread_id"] for chunk in chunks] == [42, 42, 42]
    assert ["reply_parameters" in chunk for chunk in chunks] == (
        [True, False, False] if entry == "reply" else [False, False, False]
    )
    [[button]] = chunks[-1]["reply_markup"].inline_keyboard
    assert (button.text, button.callback_data) == ("Milk ⬜", "chk:milk")
    await adapter.stop()


@pytest.mark.asyncio
async def test_buttons_cannot_be_combined_with_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path, monkeypatch, allowed_chat_ids=[12345]
    )

    with pytest.raises(ChannelConfigError):
        await adapter.send(
            "caption",
            "12345",
            files=[FileData(filename="a.png", media_type="image/png", data=b"a")],
            buttons=[[InteractionButton(label="x", data="chk:x")]],
        )

    bot.send_photo.assert_not_awaited()
    bot.send_document.assert_not_awaited()
    await adapter.stop()


# --- Extension taps -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tap_reaches_the_bundled_checklist_extension_which_flips_the_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = load_bundled("checklist")
    events: list[InteractionEvent] = []

    async def dispatch(event: InteractionEvent, responder: InteractionResponder) -> bool:
        events.append(event)
        return await registry.dispatch_channel_interaction(event, responder)

    trigger = AsyncMock()
    try:
        adapter, _sessions, _trigger, bot = make_adapter(
            tmp_path,
            monkeypatch,
            allowed_chat_ids=[12345],
            trigger_run=trigger,
            interaction_dispatcher=dispatch,
        )
        update = make_callback_update(
            data="chk:milk",
            inline_keyboard=[
                [
                    SimpleNamespace(text="⬜ Milk", callback_data="chk:milk"),
                    SimpleNamespace(text="⬜ Eggs", callback_data="chk:eggs"),
                ]
            ],
        )

        await adapter._handle_callback_query(update, None)

        [event] = events
        assert (
            event.platform,
            event.channel_id,
            event.chat_id,
            event.user_id,
            event.message_id,
            event.data,
            event.text,
            event.user_display_name,
        ) == (
            "telegram",
            "tg-assistant",
            "12345",
            "50",
            "777",
            "chk:milk",
            "Shopping list",
            "Tapper",
        )
        assert event.buttons == (
            (
                InteractionButton(label="⬜ Milk", data="chk:milk"),
                InteractionButton(label="⬜ Eggs", data="chk:eggs"),
            ),
        )
        # Only the tapped item flipped; every callback value survives the edit.
        bot.edit_message_reply_markup.assert_awaited_once()
        edit = bot.edit_message_reply_markup.await_args.kwargs
        assert (edit["chat_id"], edit["message_id"]) == (12345, 777)
        [row] = edit["reply_markup"].inline_keyboard
        assert [(button.text, button.callback_data) for button in row] == [
            ("✅ Milk", "chk:milk"),
            ("⬜ Eggs", "chk:eggs"),
        ]
        # The extension answered the tap, so no second acknowledgement follows,
        # and an extension tap never wakes the agent.
        bot.answer_callback_query.assert_awaited_once()
        trigger.assert_not_awaited()
        await adapter.stop()
    finally:
        purge_extension_modules()


@pytest.mark.asyncio
async def test_a_dispatcher_answer_and_edit_reach_the_tapped_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def dispatch(_event: InteractionEvent, responder: InteractionResponder) -> bool:
        await responder.answer("Saved", alert=True)
        await responder.edit(text="Updated")
        return True

    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path, monkeypatch, allowed_chat_ids=[12345], interaction_dispatcher=dispatch
    )

    await adapter._handle_callback_query(make_callback_update(data="chk:milk"), None)

    bot.answer_callback_query.assert_awaited_once_with(
        callback_query_id="cb1", text="Saved", show_alert=True
    )
    bot.edit_message_text.assert_awaited_once_with(
        text="Updated", chat_id=12345, message_id=777, reply_markup=None
    )
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_id", "data"),
    [(99999, "chk:milk"), (12345, "")],
    ids=["chat-outside-allowlist", "no-callback-data"],
)
async def test_an_undeliverable_tap_is_acknowledged_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chat_id: int, data: str
) -> None:
    dispatcher = _RecordingDispatcher()
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path, monkeypatch, allowed_chat_ids=[12345], interaction_dispatcher=dispatcher
    )
    answer = AsyncMock()

    await adapter._handle_callback_query(
        make_callback_update(chat_id=chat_id, data=data, answer=answer), None
    )

    # The spinner stops through the callback itself, with no toast and no dispatch.
    answer.assert_awaited_once_with()
    bot.answer_callback_query.assert_not_awaited()
    assert dispatcher.events == []
    assert [entry.chat_id for entry in adapter.denied_chats()] == (
        ["99999"] if chat_id == 99999 else []
    )
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dispatcher", [_RecordingDispatcher(answer=False), None], ids=["unanswered", "no-dispatcher"]
)
async def test_an_unanswered_tap_still_gets_one_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dispatcher: _RecordingDispatcher | None
) -> None:
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path, monkeypatch, allowed_chat_ids=[12345], interaction_dispatcher=dispatcher
    )

    await adapter._handle_callback_query(make_callback_update(data="chk:milk"), None)

    bot.answer_callback_query.assert_awaited_once_with(
        callback_query_id="cb1", text=None, show_alert=False
    )
    await adapter.stop()


# --- Run-button taps ------------------------------------------------------------------


def _save_binding(tmp_path: Path, binding_id: str, *, consumed: bool) -> None:
    channel_state(tmp_path).save_run_button_binding(
        "tg-assistant",
        RunButtonBinding(
            id=binding_id,
            platform_target="12345",
            thread_id=None,
            origin_session_id="ch-tg-assistant-12345",
            original_button_data=("run:done",),
            created_at=utc_now_timestamp(),
            consumed=consumed,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chat_id", "data", "access_registry", "answer", "keyboard_closed", "runs"),
    [
        # An accepted tap wakes the agent and closes the keyboard so it reads as submitted.
        (12345, "run:done", None, (None, False), True, 1),
        # A group member may not wake the agent; the shared keyboard stays usable.
        (-10001, "run:done", None, (None, False), False, 0),
        (12345, bound_run_callback_data("used", 0), None, (_ALREADY_HANDLED, False), True, 0),
        (12345, bound_run_callback_data("missing", 0), None, (_UNAVAILABLE, True), True, 0),
        (
            -10001,
            "run:done",
            GatedAccessRegistry(error=RuntimeError("registry unavailable")),
            (_UNAVAILABLE, True),
            False,
            0,
        ),
    ],
    ids=["accepted", "group-member", "already-handled", "unavailable", "lookup-failure"],
)
async def test_a_run_button_tap_wakes_the_agent_instead_of_an_extension(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    chat_id: int,
    data: str,
    access_registry: GatedAccessRegistry | None,
    answer: tuple[str | None, bool],
    keyboard_closed: bool,
    runs: int,
) -> None:
    _save_binding(tmp_path, "used", consumed=True)
    dispatcher = _RecordingDispatcher()
    trigger = AsyncMock(return_value=make_completed_run(output_text="synced"))
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[chat_id],
        trigger_run=trigger,
        interaction_dispatcher=dispatcher,
        run_button_binding_registry=channel_state(tmp_path),
        access_registry=access_registry or MemoryChannelAccessRegistry(),
    )
    caplog.set_level(logging.ERROR, logger="vbot.channels.telegram")

    await adapter._handle_callback_query(
        make_callback_update(
            chat_id=chat_id,
            data=data,
            inline_keyboard=[[SimpleNamespace(text="Fertig ✅", callback_data=data)]],
        ),
        None,
    )
    await drain_chat_queue(adapter, chat_id)

    text, alert = answer
    bot.answer_callback_query.assert_awaited_once_with(
        callback_query_id="cb1", text=text, show_alert=alert
    )
    edits: list[Any] = [edit.kwargs for edit in bot.edit_message_reply_markup.await_args_list]
    assert edits == (
        [{"chat_id": chat_id, "message_id": 777, "reply_markup": None}] if keyboard_closed else []
    )
    assert trigger.await_count == runs
    assert dispatcher.events == []
    assert ("Telegram Run-button handling failed" in caplog.text) == (access_registry is not None)
    await adapter.stop()
