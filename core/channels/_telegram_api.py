"""Telegram SDK loading, error translation, polling health and callback responses."""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from importlib import import_module
from typing import Any

import httpx

from core.channels.config import ChannelError
from core.extensions import (
    InteractionButton,
)
from core.utils.logging import get_logger

_LOGGER = get_logger("channels.telegram")

# A run of six or more digits in a PTB error text is taken for a Telegram chat or
# user id: Telegram's descriptions and PTB's "unknown parameters" suffix are server
# text vBot cannot enumerate, and no id may reach a message vBot logs.
_EXTERNAL_ID_PATTERN = re.compile(r"-?\d{6,}")

# httpx failures raised before a request reached Telegram, so a write can repeat.
_UNSENT_REQUEST_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


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


def _load_telegram_request() -> Any:
    try:
        return import_module("telegram.request")
    except ModuleNotFoundError as error:
        raise ChannelError(
            "python-telegram-bot is required for Telegram channels; install server dependencies"
        ) from error


# Consecutive timed-out polls that make a failing stretch. A single timeout is an
# ordinary blip PTB retries at once; a run of them means Telegram is unreachable.
_SUSTAINED_POLL_TIMEOUTS = 3


class _PollingHealth:
    """Report long-polling health as transitions: one failing line, one recovered line.

    PTB retries a failed ``getUpdates`` forever. Each failure reaches :meth:`failed`
    as the updater's ``error_callback``, which also replaces PTB's own traceback per
    attempt. PTB retries a timed-out poll without that callback, so the request built
    by :func:`_observed_polling_request` reports each timeout to :meth:`timed_out`
    and each answered poll to :meth:`answered`. A failure, or
    ``_SUSTAINED_POLL_TIMEOUTS`` consecutive timeouts, starts a failing stretch; the
    next answered poll ends it. Signals after :meth:`close` are ignored.
    """

    def __init__(self, channel_id: str) -> None:
        self._channel_id = channel_id
        # Unanswered polls since the last answered one, and how many of them timed out.
        self._failures = 0
        self._timeouts = 0
        self._unanswered_since: float | None = None
        self._failing = False
        self._closed = False

    def failed(self, error: Exception) -> None:
        if self._closed:
            return
        self._unanswered(type(error).__name__, starts_failing=True)

    def timed_out(self) -> None:
        if self._closed:
            return
        self._timeouts += 1
        self._unanswered("TimedOut", starts_failing=self._timeouts >= _SUSTAINED_POLL_TIMEOUTS)

    def _unanswered(self, error_type: str, *, starts_failing: bool) -> None:
        self._failures += 1
        if self._unanswered_since is None:
            self._unanswered_since = time.monotonic()
        if self._failing or not starts_failing:
            _LOGGER.debug(
                "Telegram poll failed (channel=%s error_type=%s failures=%d)",
                self._channel_id,
                error_type,
                self._failures,
            )
            return
        self._failing = True
        _LOGGER.warning(
            "Telegram polling failed (channel=%s error_type=%s failures=%d)",
            self._channel_id,
            error_type,
            self._failures,
        )

    def answered(self) -> None:
        if self._closed:
            return
        if self._failing and self._unanswered_since is not None:
            _LOGGER.info(
                "Telegram polling recovered (channel=%s failures=%d down_for=%.1fs)",
                self._channel_id,
                self._failures,
                time.monotonic() - self._unanswered_since,
            )
        self._failures = 0
        self._timeouts = 0
        self._unanswered_since = None
        self._failing = False

    def close(self) -> None:
        self._closed = True


def _observed_polling_request(on_answer: Callable[[], None], on_timeout: Callable[[], None]) -> Any:
    """Build PTB's default ``getUpdates`` request, reporting answered and timed-out polls."""
    httpx_request: Any = _load_telegram_request().HTTPXRequest
    timed_out: Any = _load_telegram_error().TimedOut

    class _ObservedPollingRequest(httpx_request):  # type: ignore[misc]
        async def do_request(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
            try:
                code, payload = await super().do_request(*args, **kwargs)
            except timed_out:
                on_timeout()
                raise
            if 200 <= code < 300:
                on_answer()
            return code, payload

    # PTB's ApplicationBuilder default for getUpdates: one pooled connection.
    return _ObservedPollingRequest(connection_pool_size=1)


def _load_telegram_error() -> Any:
    try:
        return import_module("telegram.error")
    except ModuleNotFoundError as error:
        raise ChannelError(
            "python-telegram-bot is required for Telegram channels; install server dependencies"
        ) from error


@contextlib.contextmanager
def _telegram_error_boundary(channel_id: str, *, write: bool = False) -> Iterator[None]:
    """Translate python-telegram-bot API errors into ChannelError at the adapter boundary.

    PTB raises ``telegram.error.TelegramError`` (e.g. ``BadRequest``) when the Bot API rejects
    a request. Channel ingress, ``channel_send``, and the engine relay handle the ChannelError
    family, so an unwrapped PTB error would surface as an unexpected exception instead of a
    clean failure. Transient faults are marked retryable so downloads and reply delivery can
    retry them; for a ``write`` only those after which Telegram cannot have posted anything
    (see ``_classify_telegram_error``).
    """
    telegram_error = _load_telegram_error()
    try:
        yield
    except telegram_error.TelegramError as error:
        text = _loggable_telegram_error_text(telegram_error, error)
        channel_error = _classify_telegram_error(
            channel_id, telegram_error, error, text, write=write
        )
        if text == str(error):
            raise channel_error from error
        # A traceback prints the cause's own text, which names what ``text`` left out.
        raise channel_error from None


def _loggable_telegram_error_text(telegram_error_module: Any, error: Any) -> str:
    """Return a PTB error's text without the chat ids or bot token some of them name."""
    chat_migrated = getattr(telegram_error_module, "ChatMigrated", None)
    if chat_migrated is not None and isinstance(error, chat_migrated):
        # PTB's text names the new chat id.
        return "Group migrated to supergroup"
    invalid_token = getattr(telegram_error_module, "InvalidToken", None)
    if invalid_token is not None and isinstance(error, invalid_token):
        # PTB's text for a token rejected at start quotes the token.
        return "The bot token was rejected by Telegram"
    return _EXTERNAL_ID_PATTERN.sub("<id>", str(error))


def _classify_telegram_error(
    channel_id: str,
    telegram_error_module: Any,
    error: Any,
    text: str,
    *,
    write: bool = False,
) -> ChannelError:
    """Translate one PTB TelegramError into a retry-classified ChannelError.

    A read retries every transport fault. A write retries only flood control and
    a connection that never opened: after a timeout, a dropped connection or a
    server error Telegram may already have posted the message, and a repeat
    would duplicate it.
    """
    channel_error = ChannelError(
        f"Telegram request failed (channel={channel_id} error_type={type(error).__name__}): {text}"
    )
    # PTB's permanent BadRequest also inherits NetworkError. Reject it before
    # classifying actual transport failures, or invalid requests retry unchanged.
    bad_request = getattr(telegram_error_module, "BadRequest", None)
    if bad_request is not None and isinstance(error, bad_request):
        return channel_error
    network_error = getattr(telegram_error_module, "NetworkError", None)
    if network_error is not None and isinstance(error, network_error):
        # Covers TimedOut as well; PTB chains the httpx error it translated.
        channel_error.retryable = not write or isinstance(error.__cause__, _UNSENT_REQUEST_ERRORS)
    retry_after = getattr(error, "retry_after", None)
    if isinstance(retry_after, timedelta):
        retry_after = retry_after.total_seconds()
    if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
        channel_error.retryable = True
        channel_error.retry_after = float(retry_after)
    return channel_error
