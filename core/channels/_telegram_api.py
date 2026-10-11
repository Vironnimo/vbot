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
from core.utils.tls import shared_ssl_context

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


# How long polling may go unanswered before it counts as an outage. PTB retries a
# failed poll after 1 s and that long poll is held up to 10 s, so the network blips
# a home connection sees every few hours end well within it.
_POLLING_OUTAGE_SECONDS = 60.0

# getUpdates statuses PTB answers with InvalidToken, which ends its polling for good.
_REJECTED_TOKEN_STATUSES = frozenset({401, 404})


class _PollingHealth:
    """Report long-polling outages as transitions: one WARNING, one INFO on recovery.

    PTB retries an unanswered ``getUpdates`` forever. A failure reaches :meth:`failed`
    as the updater's ``error_callback``, which also replaces PTB's traceback per
    attempt; PTB retries a timed-out poll without that callback, so the request built
    by :func:`_observed_polling_request` reports timeouts and answers itself. The
    first unanswered poll opens an outage, every unanswered poll logs DEBUG, and the
    outage warns once it has lasted ``_POLLING_OUTAGE_SECONDS``; the next answered
    poll ends it, with one INFO line only after a warning. A rejected token is
    different: PTB stops polling for good, so ``on_rejected`` lets the adapter fail.
    Signals after :meth:`close` are ignored; :meth:`failed` never raises, since an
    exception from PTB's error callback silently ends polling too.
    """

    def __init__(
        self,
        channel_id: str,
        on_rejected: Callable[[], None],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._channel_id = channel_id
        self._on_rejected = on_rejected
        self.clock = clock
        self._failures = 0
        self._unanswered_since: float | None = None
        self._warned = False
        self._closed = False

    def failed(self, error: Exception) -> None:
        if self._closed:
            return
        with contextlib.suppress(Exception):
            self._unanswered(error)

    def responded(self, status: int, started_at: float) -> None:
        """Record a poll Telegram answered; ``started_at`` is when it was sent."""
        if self._closed:
            return
        if status in _REJECTED_TOKEN_STATUSES:
            self._on_rejected()
        elif 200 <= status < 300:
            self._answered(started_at)
        # Any other status reaches failed() through PTB's error callback.

    def _unanswered(self, error: BaseException) -> None:
        now = self.clock()
        self._failures += 1
        if self._unanswered_since is None:
            self._unanswered_since = now
        down_for = now - self._unanswered_since
        if self._warned or down_for < _POLLING_OUTAGE_SECONDS:
            _LOGGER.debug(
                "Telegram poll failed (channel=%s error_type=%s cause=%s failures=%d)",
                self._channel_id,
                type(error).__name__,
                _innermost_cause_name(error),
                self._failures,
            )
            return
        self._warned = True
        _LOGGER.warning(
            "Telegram polling failed (channel=%s error_type=%s cause=%s failures=%d "
            "down_for=%.0fs)",
            self._channel_id,
            type(error).__name__,
            _innermost_cause_name(error),
            self._failures,
            down_for,
        )

    def _answered(self, started_at: float) -> None:
        if self._warned and self._unanswered_since is not None:
            # Up to when the answered poll was sent: a long poll is held open for
            # seconds even when Telegram is reachable again.
            _LOGGER.info(
                "Telegram polling recovered (channel=%s failures=%d down_for=%.1fs)",
                self._channel_id,
                self._failures,
                max(0.0, started_at - self._unanswered_since),
            )
        self._failures = 0
        self._unanswered_since = None
        self._warned = False

    def close(self) -> None:
        self._closed = True


def _innermost_cause_name(error: BaseException) -> str:
    """Name the exception at the bottom of a PTB error's chain, such as the OS error.

    PTB wraps the httpx error, and httpcore drops the explicit cause of its own,
    so the chain continues through the implicit context.
    """
    current = error
    seen = {id(current)}
    while True:
        following = current.__cause__ or current.__context__
        if following is None or id(following) in seen:
            return type(current).__name__
        seen.add(id(following))
        current = following


def _tls_kwargs() -> dict[str, Any]:
    """httpx arguments that verify with the shared TLS context.

    Without them every PTB request builds its own context and parses the CA
    bundle again, about 0.3 s on the Event Loop per request.
    """
    return {"verify": shared_ssl_context()}


def _bot_request() -> Any:
    """Build PTB's default request for the Bot API calls other than ``getUpdates``."""
    # PTB's ApplicationBuilder default for the bot: 256 pooled connections.
    return _load_telegram_request().HTTPXRequest(
        connection_pool_size=256, httpx_kwargs=_tls_kwargs()
    )


def _observed_polling_request(health: _PollingHealth) -> Any:
    """Build PTB's default ``getUpdates`` request, reporting each poll to ``health``."""
    httpx_request: Any = _load_telegram_request().HTTPXRequest
    timed_out: Any = _load_telegram_error().TimedOut

    class _ObservedPollingRequest(httpx_request):  # type: ignore[misc]
        async def do_request(self, *args: Any, **kwargs: Any) -> tuple[int, bytes]:
            started_at = health.clock()
            try:
                code, payload = await super().do_request(*args, **kwargs)
            except timed_out as error:
                health.failed(error)
                raise
            health.responded(code, started_at)
            return code, payload

    # PTB's ApplicationBuilder default for getUpdates: one pooled connection.
    return _ObservedPollingRequest(connection_pool_size=1, httpx_kwargs=_tls_kwargs())


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
        unsent = isinstance(error.__cause__, _UNSENT_REQUEST_ERRORS)
        channel_error.retryable = not write or unsent
        channel_error.possibly_delivered = write and not unsent
    retry_after = getattr(error, "retry_after", None)
    if isinstance(retry_after, timedelta):
        retry_after = retry_after.total_seconds()
    if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
        channel_error.retryable = True
        channel_error.retry_after = float(retry_after)
    return channel_error
