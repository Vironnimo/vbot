"""Telegram SDK loading, error translation and callback responses."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from importlib import import_module
from typing import Any

from core.channels.config import ChannelError
from core.extensions import (
    InteractionButton,
)
from core.utils.logging import get_logger

_LOGGER = get_logger("channels.telegram")


class _TelegramInteractionResponder:
    """Concrete :class:`InteractionResponder` for one Telegram ``callback_query``.

    Owns the reply channel for a single tap: acknowledge it (stop the tapper's
    spinner) and optionally edit the tapped message's text and/or keyboard. Bot
    API calls are wrapped in the adapter's :func:`_telegram_error_boundary`, so a
    failed edit or ack surfaces as a ``ChannelError`` rather than an unwrapped
    PTB error. ``answered`` lets the adapter guarantee a single fallback ack.
    """

    def __init__(
        self,
        bot: Any,
        *,
        callback_id: str,
        chat_id: int,
        message_id: int,
        channel_id: str,
    ) -> None:
        self._bot = bot
        self._callback_id = callback_id
        self._chat_id = chat_id
        self._message_id = message_id
        self._channel_id = channel_id
        self.answered = False

    async def answer(self, text: str | None = None, *, alert: bool = False) -> None:
        with _telegram_error_boundary(self._channel_id):
            await self._bot.answer_callback_query(
                callback_query_id=self._callback_id, text=text, show_alert=alert
            )
        self.answered = True

    async def edit(
        self,
        *,
        text: str | None = None,
        buttons: list[list[InteractionButton]] | None = None,
    ) -> None:
        # An empty keyboard means "remove the inline keyboard": Telegram clears it
        # only for reply_markup=None, not for an empty InlineKeyboardMarkup.
        markup = _buttons_to_markup(buttons) if buttons else None
        with _telegram_error_boundary(self._channel_id):
            if text is not None:
                await self._bot.edit_message_text(
                    text=text,
                    chat_id=self._chat_id,
                    message_id=self._message_id,
                    reply_markup=markup,
                )
            elif buttons is not None:
                await self._bot.edit_message_reply_markup(
                    chat_id=self._chat_id,
                    message_id=self._message_id,
                    reply_markup=markup,
                )


def _buttons_to_markup(rows: list[list[InteractionButton]]) -> Any:
    """Render neutral button rows into a PTB ``InlineKeyboardMarkup``."""
    telegram = _load_telegram()
    keyboard = [
        [
            telegram.InlineKeyboardButton(text=button.label, callback_data=button.data)
            for button in row
        ]
        for row in rows
    ]
    return telegram.InlineKeyboardMarkup(keyboard)


def _markup_to_buttons(inline_keyboard: Any) -> tuple[tuple[InteractionButton, ...], ...]:
    """Read a PTB ``inline_keyboard`` (rows of buttons) into neutral button rows.

    Returns an empty tuple when the message carries no keyboard. Each button's
    ``.text`` becomes the neutral label and its ``.callback_data`` the neutral
    data; a button missing either is skipped (only tappable buttons round-trip).
    """
    if not inline_keyboard:
        return ()
    rows: list[tuple[InteractionButton, ...]] = []
    for row in inline_keyboard:
        buttons: list[InteractionButton] = []
        for button in row:
            label = getattr(button, "text", None)
            data = getattr(button, "callback_data", None)
            if isinstance(label, str) and isinstance(data, str):
                buttons.append(InteractionButton(label=label, data=data))
        rows.append(tuple(buttons))
    return tuple(rows)


def _load_telegram_ext() -> Any:
    try:
        return import_module("telegram.ext")
    except ModuleNotFoundError as error:
        raise ChannelError(
            "python-telegram-bot is required for Telegram channels; install server dependencies"
        ) from error


def _load_telegram() -> Any:
    try:
        return import_module("telegram")
    except ModuleNotFoundError as error:
        raise ChannelError(
            "python-telegram-bot is required for Telegram channels; install server dependencies"
        ) from error


def _load_telegram_error() -> Any:
    try:
        return import_module("telegram.error")
    except ModuleNotFoundError as error:
        raise ChannelError(
            "python-telegram-bot is required for Telegram channels; install server dependencies"
        ) from error


@contextlib.contextmanager
def _telegram_error_boundary(channel_id: str) -> Iterator[None]:
    """Translate python-telegram-bot API errors into ChannelError at the adapter boundary.

    PTB raises ``telegram.error.TelegramError`` (e.g. ``BadRequest``) when the Bot API rejects
    a request. Channel ingress, ``channel_send``, and the engine relay handle the ChannelError
    family, so an unwrapped PTB error would surface as an unexpected exception instead of a
    clean failure. Transient transport faults (network errors, flood-control ``RetryAfter``)
    are marked retryable so downloads and reply delivery can retry them.
    """
    telegram_error = _load_telegram_error()
    try:
        yield
    except telegram_error.TelegramError as error:
        raise _classify_telegram_error(channel_id, telegram_error, error) from error


def _classify_telegram_error(
    channel_id: str,
    telegram_error_module: Any,
    error: Any,
) -> ChannelError:
    """Translate one PTB TelegramError into a retry-classified ChannelError."""
    channel_error = ChannelError(f"Telegram request failed (channel={channel_id}): {error}")
    network_error = getattr(telegram_error_module, "NetworkError", None)
    if network_error is not None and isinstance(error, network_error):
        # Covers TimedOut as well - both are transient transport faults.
        channel_error.retryable = True
    retry_after = getattr(error, "retry_after", None)
    if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
        channel_error.retryable = True
        channel_error.retry_after = float(retry_after)
    return channel_error
