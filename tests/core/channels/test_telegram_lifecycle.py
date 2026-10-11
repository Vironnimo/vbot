"""Telegram adapter lifecycle: token, start/stop, update routing and the polling watermark."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import threading
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, call

import httpx
import pytest
import telegram
import telegram.error
import telegram.ext as telegram_ext
import telegram.request

import core.channels._telegram_api as telegram_api
import core.channels.telegram as telegram_module
from core.channels import ChannelConfigError, ChannelError
from core.channels.telegram import TelegramChannelAdapter
from core.utils.tls import shared_ssl_context

from .engine_test_support import (
    QUEUE_DRAIN_TIMEOUT_SECONDS,
    channel_state,
    make_completed_run,
)
from .telegram_test_support import (
    drain_chat_queue,
    make_adapter,
    make_update,
    make_workflow_dispatcher,
    sent_texts,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")

_BOT = 7001
_OTHER_BOT = 7002
_IDENTITY = SimpleNamespace(id=_BOT, username="MyBot", full_name="Helpful Bot")


class _FakeApplication:
    """python-telegram-bot's Application as the adapter drives it, with PTB's state rules."""

    def __init__(
        self, bot: SimpleNamespace, *identities: object, with_updater: bool = True
    ) -> None:
        self.bot = bot
        self.events: list[str] = []
        self.handlers: list[Any] = []
        self.error_handlers: list[Callable[[object, Any], Awaitable[None]]] = []
        self.polling = asyncio.Event()
        self.drain_on_stop: Callable[[], Awaitable[None]] | None = None
        self.error_callback: Callable[[telegram.error.TelegramError], None] | None = None
        self.polling_options: dict[str, Any] = {}
        # What each initialize's identity lookup (getMe) answers; the last one repeats.
        self._identities = list(identities)
        self._running = False
        self.updater = (
            SimpleNamespace(start_polling=self._start_polling, stop=self._stop_polling)
            if with_updater
            else None
        )

        async def shutdown_bot() -> None:
            self.events.append("bot.shutdown")

        bot.shutdown = shutdown_bot

    def add_handler(self, handler: Any) -> None:
        self.handlers.append(handler)

    def add_error_handler(self, handler: Callable[[object, Any], Awaitable[None]]) -> None:
        self.error_handlers.append(handler)

    async def initialize(self) -> None:
        # PTB's initialize fetches the bot's identity and caches it as ``bot.bot``.
        self.events.append("initialize")
        identity = self._identities.pop(0) if len(self._identities) > 1 else self._identities[0]
        if isinstance(identity, Exception):
            raise identity
        self.bot.bot = identity

    async def start(self) -> None:
        self.events.append("start")
        self._running = True

    async def stop(self) -> None:
        self.events.append("stop")
        if not self._running:
            raise RuntimeError("This Application is not running!")
        if self.drain_on_stop is not None:
            # PTB hands still-queued updates to their handlers inside Application.stop.
            await self.drain_on_stop()
        self._running = False

    async def shutdown(self) -> None:
        self.events.append("shutdown")

    async def _start_polling(
        self,
        *,
        error_callback: Callable[[telegram.error.TelegramError], None] | None = None,
        **options: Any,
    ) -> None:
        self.events.append("updater.start_polling")
        self.error_callback = error_callback
        self.polling_options = options
        self.polling.set()

    async def _stop_polling(self) -> None:
        self.events.append("updater.stop")
        if not self.polling.is_set():
            raise RuntimeError("This Updater is not running!")


class _RecordingBuilder:
    """The real ApplicationBuilder chain; only ``build()`` returns the fake application."""

    def __init__(self, application: _FakeApplication) -> None:
        self._real = telegram_ext.Application.builder()
        self._application = application
        self.received_token: str | None = None
        self.received_rate_limiter: Any = None
        self.received_request: Any = None
        self.received_get_updates_request: Any = None

    def token(self, token: str) -> _RecordingBuilder:
        self._real.token(token)
        self.received_token = token
        return self

    def rate_limiter(self, rate_limiter: Any) -> _RecordingBuilder:
        self._real.rate_limiter(rate_limiter)
        self.received_rate_limiter = rate_limiter
        return self

    def request(self, request: Any) -> _RecordingBuilder:
        self._real.request(request)
        self.received_request = request
        return self

    def get_updates_request(self, request: Any) -> _RecordingBuilder:
        self._real.get_updates_request(request)
        self.received_get_updates_request = request
        return self

    def build(self) -> _FakeApplication:
        return self._application


def _install_ptb(
    monkeypatch: pytest.MonkeyPatch, application: _FakeApplication
) -> tuple[_RecordingBuilder, list[dict[str, Any]]]:
    """Serve ``telegram.ext`` with the real handlers, filters and rate limiter.

    Only building the Application (and its HTTP clients) is replaced; the adapter
    still loads the SDK through its own import.
    """
    builder = _RecordingBuilder(application)
    rate_limiter_options: list[dict[str, Any]] = []

    class RecordingRateLimiter(telegram_ext.AIORateLimiter):  # type: ignore[misc]
        def __init__(self, **options: Any) -> None:
            rate_limiter_options.append(options)
            super().__init__(**options)

    ext = SimpleNamespace(
        filters=telegram_ext.filters,
        MessageHandler=telegram_ext.MessageHandler,
        CallbackQueryHandler=telegram_ext.CallbackQueryHandler,
        AIORateLimiter=RecordingRateLimiter,
        Application=SimpleNamespace(builder=lambda: builder),
    )
    real_import = telegram_api.import_module
    monkeypatch.setattr(
        telegram_api,
        "import_module",
        lambda name: ext if name == "telegram.ext" else real_import(name),
    )
    return builder, rate_limiter_options


async def _start(
    adapter: TelegramChannelAdapter, application: _FakeApplication
) -> asyncio.Task[None]:
    polling = asyncio.create_task(adapter.start())
    await asyncio.wait_for(application.polling.wait(), timeout=QUEUE_DRAIN_TIMEOUT_SECONDS)
    return polling


async def _deliver(application: _FakeApplication, update: telegram.Update) -> None:
    """Hand one update to the first handler that accepts it, as PTB's dispatcher does."""
    for handler in application.handlers:
        if handler.check_update(update):
            await handler.callback(update, None)
            return
    raise AssertionError(f"no handler accepts update {update.update_id}")


def _message(chat_id: int = 12345, **content: Any) -> telegram.Message:
    return telegram.Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=telegram.Chat(id=chat_id, type="private" if chat_id > 0 else "group"),
        from_user=telegram.User(id=50, first_name="Alice", is_bot=False),
        **content,
    )


def _text_update(update_id: int, text: str, *, chat_id: int = 12345) -> telegram.Update:
    return telegram.Update(update_id=update_id, message=_message(chat_id, text=text))


def _running_tasks(excluded: set[asyncio.Task[Any]]) -> list[str]:
    """Name the adapter and engine tasks still running, ignoring *excluded*."""
    return [
        task.get_name()
        for task in asyncio.all_tasks() - excluded
        if not task.done()
        and task.get_name().startswith(("telegram:tg-assistant:", "channel:tg-assistant:"))
    ]


def test_constructor_refuses_a_missing_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(ChannelConfigError, match="TELEGRAM_BOT_TOKEN_TG_ASSISTANT"):
        make_adapter(
            tmp_path,
            monkeypatch,
            credential_resolver=lambda _key: "  ",
            set_process_token=False,
        )


@pytest.mark.asyncio
async def test_start_polls_as_the_resolved_bot_and_stop_drains_before_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    earlier_tasks = asyncio.all_tasks()
    storage = channel_state(tmp_path)
    storage.save_update_offset("tg-assistant", _BOT, 7)
    trigger = AsyncMock(side_effect=lambda *_args, **_kwargs: make_completed_run(output_text="ok"))
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger,
        command_dispatcher=make_workflow_dispatcher(),
        credential_resolver=lambda _key: " runtime-token ",
        set_process_token=False,
        update_offset_store=storage,
        running=False,
    )
    # The network is not ready at the first attempt to connect, as after a resume.
    application = _FakeApplication(bot, telegram.error.NetworkError("down"), _IDENTITY)
    builder, rate_limiter_options = _install_ptb(monkeypatch, application)
    connect_waits: list[float] = []

    async def record_wait(delay: float) -> None:
        connect_waits.append(delay)

    monkeypatch.setattr(telegram_module, "_sleep", record_wait)
    verifying: list[Any] = []
    real_client = httpx.AsyncClient

    def recording_client(**options: Any) -> httpx.AsyncClient:
        verifying.append(options.get("verify"))
        return real_client(**options)

    monkeypatch.setattr(httpx, "AsyncClient", recording_client)

    polling = await _start(adapter, application)
    # Telegram redelivers unconfirmed updates after a restart: the stored
    # watermark of this bot skips update 7, and update 8 runs.
    await _deliver(application, _text_update(7, "already answered"))
    await _deliver(application, _text_update(8, "hello"))
    await drain_chat_queue(adapter, 12345)
    assert [awaited.args[1] for awaited in trigger.await_args_list] == ["hello"]
    bot.send_message.assert_awaited_once_with(chat_id=12345, text="ok")

    # A failing update handler is logged once with its Channel; the update never is.
    caplog.set_level(logging.ERROR, logger="vbot.channels.telegram")
    [log_handler_error] = application.error_handlers
    await log_handler_error(
        _text_update(99, "private words"), SimpleNamespace(error=RuntimeError("handler broke"))
    )
    [record] = [
        record
        for record in caplog.records
        if record.name == "vbot.channels.telegram" and record.levelno == logging.ERROR
    ]
    assert "tg-assistant" in record.getMessage()
    assert "private words" not in caplog.text

    async def drain_pending_updates() -> None:
        await _deliver(application, _text_update(9, "last question"))
        await _deliver(application, _text_update(10, "/ping"))

    # A second start while polling builds nothing and ends with the first.
    second_start = asyncio.create_task(adapter.start())
    await asyncio.sleep(0)
    application.drain_on_stop = drain_pending_updates
    await adapter.stop()
    await asyncio.wait_for(
        asyncio.gather(polling, second_start), timeout=QUEUE_DRAIN_TIMEOUT_SECONDS
    )

    assert builder.received_token == "runtime-token"
    assert isinstance(builder.received_rate_limiter, telegram_ext.AIORateLimiter)
    assert rate_limiter_options == [{"max_retries": 3}]
    # The bot's and the getUpdates requests verify with the shared TLS context; a
    # default client would parse the CA bundle again for each.
    assert isinstance(builder.received_request, telegram.request.HTTPXRequest)
    assert isinstance(builder.received_get_updates_request, telegram.request.HTTPXRequest)
    assert len(verifying) == 2
    assert all(verify is shared_ssl_context() for verify in verifying)
    assert connect_waits == [1.0]
    assert application.events == [
        "initialize",
        "initialize",
        "start",
        "updater.start_polling",
        "updater.stop",
        "stop",
        "shutdown",
        "bot.shutdown",
    ]
    # Telegram keeps the update types a token last asked for; button taps must be among them.
    assert set(application.polling_options["allowed_updates"]) == {"message", "callback_query"}
    # Drained updates still reach the live bot; stop then settles the work they
    # started and their watermark saves before the application shuts down.
    assert "pong" in sent_texts(bot)
    assert _running_tasks(earlier_tasks) == []
    assert storage.load_update_offset("tg-assistant", _BOT) == 10
    with pytest.raises(ChannelError, match="not running"):
        await adapter.send("late", "12345")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "failed_at", "answer_sent_at", "levels"),
    [
        # A network blip: PTB's next poll is answered well within a minute.
        ("error-callback", [0.0, 1.0], 2.5, [logging.DEBUG, logging.DEBUG]),
        # An outage warns once it has lasted a minute, logs its further failures at
        # DEBUG, and its end once.
        (
            "error-callback",
            [0.0, 30.0, 60.0, 90.0],
            95.0,
            [logging.DEBUG, logging.DEBUG, logging.WARNING, logging.DEBUG, logging.INFO],
        ),
        # PTB retries a timed-out poll without the callback; the request sees it.
        (
            "timeout",
            [0.0, 30.0, 60.0, 90.0],
            95.0,
            [logging.DEBUG, logging.DEBUG, logging.WARNING, logging.DEBUG, logging.INFO],
        ),
    ],
    ids=["blip", "outage", "outage-seen-as-timeouts"],
)
async def test_polling_warns_only_about_an_outage_and_logs_its_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
    failed_at: list[float],
    answer_sent_at: float,
    levels: list[int],
) -> None:
    adapter, _sessions, _trigger, bot = make_adapter(tmp_path, monkeypatch, running=False)
    application = _FakeApplication(bot, _IDENTITY)
    builder, _ = _install_ptb(monkeypatch, application)
    now = 0.0
    monkeypatch.setattr(
        telegram_module,
        "_PollingHealth",
        functools.partial(telegram_api._PollingHealth, clock=lambda: now),
    )
    telegram_reachable = True

    async def answer(*_args: Any, **_kwargs: Any) -> tuple[int, bytes]:
        nonlocal now
        if not telegram_reachable:
            raise telegram.error.TimedOut() from httpx.ReadTimeout("")
        # Telegram holds a long poll open until updates arrive or it times out.
        now += 10.0
        return 200, b'{"ok": true, "result": []}'

    # Only the HTTP exchange is replaced; the adapter's getUpdates request is PTB's.
    monkeypatch.setattr(telegram.request.HTTPXRequest, "do_request", answer)
    caplog.set_level(logging.DEBUG, logger="vbot.channels.telegram")

    polling = await _start(adapter, application)
    request = builder.received_get_updates_request
    report_failure = application.error_callback
    assert isinstance(request, telegram.request.HTTPXRequest)
    assert report_failure is not None

    async def poll() -> None:
        with contextlib.suppress(telegram.error.TimedOut):
            await request.do_request("https://api.telegram.org/bot/getUpdates", "POST")

    def connection_reset() -> telegram.error.NetworkError:
        # PTB's error for a reset connection: the OS error sits at the end of its chain.
        error = telegram.error.NetworkError("httpx.ReadError: ")
        error.__cause__ = httpx.ReadError("")
        error.__cause__.__context__ = ConnectionResetError(64, "network name deleted")
        return error

    await poll()
    for moment in failed_at:
        now = moment
        if failure == "timeout":
            telegram_reachable = False
            await poll()
        else:
            report_failure(connection_reset())
    telegram_reachable = True
    now = answer_sent_at
    await poll()
    await poll()
    await adapter.stop()
    report_failure(connection_reset())
    await asyncio.wait_for(polling, timeout=QUEUE_DRAIN_TIMEOUT_SECONDS)

    # Healthy polls are silent and nothing carries a traceback. The warning names
    # the failure down to its cause; the recovery counts the outage up to the poll
    # that was answered, not the time Telegram held it open.
    records = [record for record in caplog.records if record.name == "vbot.channels.telegram"]
    assert [record.levelno for record in records] == levels
    assert all(record.exc_info is None for record in records)
    for record in records:
        if record.levelno == logging.WARNING:
            cause = "ReadTimeout" if failure == "timeout" else "ConnectionResetError"
            assert f"cause={cause}" in record.getMessage()
        if record.levelno == logging.INFO:
            assert f"failures={len(failed_at)}" in record.getMessage()
            assert "down_for=95.0s" in record.getMessage()


@pytest.mark.asyncio
async def test_a_token_rejected_while_polling_fails_the_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # PTB ends its polling for good on a rejected token without calling the error
    # callback; an adapter that kept waiting would look connected but receive nothing.
    adapter, _sessions, _trigger, bot = make_adapter(tmp_path, monkeypatch, running=False)
    application = _FakeApplication(bot, _IDENTITY)
    builder, _ = _install_ptb(monkeypatch, application)

    async def reject(*_args: Any, **_kwargs: Any) -> tuple[int, bytes]:
        return 401, b'{"ok": false, "error_code": 401, "description": "Unauthorized"}'

    monkeypatch.setattr(telegram.request.HTTPXRequest, "do_request", reject)
    polling = await _start(adapter, application)
    request = builder.received_get_updates_request
    with contextlib.suppress(telegram.error.InvalidToken):
        await request.post("https://api.telegram.org/bot/getUpdates")

    with pytest.raises(ChannelConfigError, match="rejected the bot token"):
        await asyncio.wait_for(polling, timeout=QUEUE_DRAIN_TIMEOUT_SECONDS)
    # The updater re-raises the rejection that ended its polling; stop expects it.
    assert application.updater is not None
    application.updater.stop = AsyncMock(side_effect=telegram.error.InvalidToken("Unauthorized"))
    caplog.set_level(logging.WARNING, logger="vbot.channels.telegram")
    await adapter.stop()

    assert application.events[-2:] == ["shutdown", "bot.shutdown"]
    assert [record for record in caplog.records if record.name == "vbot.channels.telegram"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity", "with_updater", "connect_waits", "stop_events"),
    [
        # A network that stays down fails the start after about 30 s of retries.
        # Before polling starts, PTB's stop calls raise RuntimeError; stop ignores them.
        (
            telegram.error.NetworkError("down"),
            True,
            [1.0, 2.0, 4.0, 8.0, 15.0],
            ["updater.stop", "stop"],
        ),
        (_IDENTITY, False, [], ["stop"]),
    ],
    ids=["telegram-unreachable", "no-updater"],
)
async def test_stop_after_a_failed_start_still_shuts_the_application_down(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    identity: object,
    with_updater: bool,
    connect_waits: list[float],
    stop_events: list[str],
) -> None:
    adapter, _sessions, _trigger, bot = make_adapter(tmp_path, monkeypatch, running=False)
    application = _FakeApplication(bot, identity, with_updater=with_updater)
    _install_ptb(monkeypatch, application)
    waits: list[float] = []

    async def record_wait(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(telegram_module, "_sleep", record_wait)

    with pytest.raises(ChannelError):
        await adapter.start()
    events_before_stop = len(application.events)
    await adapter.stop()

    assert waits == connect_waits
    # The bot's HTTP clients close even when initializing never completed.
    assert application.events[events_before_stop:] == [*stop_events, "shutdown", "bot.shutdown"]
    with pytest.raises(ChannelError, match="not running"):
        await adapter.send("late", "12345")


@pytest.mark.asyncio
async def test_a_rejected_token_never_reaches_the_start_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # PTB's own error for a token rejected at start quotes the token, and the
    # supervisor logs a start failure with its traceback.
    token = "7001:AAE-secret-token-value"
    rejected = telegram.error.InvalidToken(f"The token `{token}` was rejected by the server.")
    adapter, _sessions, _trigger, bot = make_adapter(tmp_path, monkeypatch, running=False)
    _install_ptb(monkeypatch, _FakeApplication(bot, rejected))

    with pytest.raises(ChannelError) as raised:
        await adapter.start()
    await adapter.stop()

    logged = "".join(traceback.format_exception(raised.value))
    assert "InvalidToken" in logged
    assert "secret-token-value" not in logged


@pytest.mark.asyncio
async def test_each_update_kind_reaches_only_its_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, _sessions, _trigger, bot = make_adapter(tmp_path, monkeypatch, running=False)
    application = _FakeApplication(bot, _IDENTITY)
    _install_ptb(monkeypatch, application)
    polling = await _start(adapter, application)

    text = _message(text="hi")
    photo = _message(photo=[telegram.PhotoSize(file_id="f", file_unique_id="u", width=1, height=1)])
    sticker = _message(
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
    poll = telegram.Poll.de_json(
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
    tap = telegram.CallbackQuery(
        id="cb1",
        from_user=telegram.User(id=50, first_name="A", is_bot=False),
        chat_instance="ci",
        data="chk:milk",
        message=text,
    )
    routes: list[tuple[str, dict[str, Any], str | None]] = [
        ("text", {"message": text}, "_handle_inbound_message"),
        # Edits must not start new Runs, and channel posts are not chat messages.
        ("edited text", {"edited_message": text}, None),
        ("channel post", {"channel_post": text}, None),
        ("photo", {"message": photo}, "_handle_inbound_media"),
        ("edited photo", {"edited_message": photo}, None),
        (
            "voice",
            {
                "message": _message(
                    voice=telegram.Voice(file_id="v", file_unique_id="vu", duration=2)
                )
            },
            "_handle_inbound_media",
        ),
        (
            "animation",
            {
                "message": _message(
                    animation=telegram.Animation(
                        file_id="a", file_unique_id="au", width=64, height=64, duration=1
                    )
                )
            },
            "_handle_inbound_media",
        ),
        (
            "location",
            {"message": _message(location=telegram.Location(longitude=13.4, latitude=52.5))},
            "_handle_inbound_structured_message",
        ),
        (
            "contact",
            {
                "message": _message(
                    contact=telegram.Contact(phone_number="+491234", first_name="Max")
                )
            },
            "_handle_inbound_structured_message",
        ),
        ("poll", {"message": _message(poll=poll)}, "_handle_inbound_structured_message"),
        ("sticker", {"message": sticker}, "_handle_unsupported_message_type"),
        (
            "dice",
            {"message": _message(dice=telegram.Dice(value=3, emoji="🎲"))},
            "_handle_unsupported_message_type",
        ),
        ("edited sticker", {"edited_message": sticker}, None),
        (
            "migration",
            {"message": _message(-500, migrate_to_chat_id=-100500)},
            "_handle_chat_migration",
        ),
        # Service noise such as a member joining gets no reply-producing handler.
        (
            "member joined",
            {
                "message": _message(
                    new_chat_members=[telegram.User(id=51, first_name="B", is_bot=False)]
                )
            },
            None,
        ),
        ("button tap", {"callback_query": tap}, "_handle_callback_query"),
    ]
    try:
        for update_id, (kind, content, expected) in enumerate(routes, start=1):
            update = telegram.Update(update_id=update_id, **content)
            accepting = [
                handler.callback.__name__
                for handler in application.handlers
                if handler.check_update(update)
            ]
            assert accepting == ([expected] if expected else []), kind
    finally:
        await adapter.stop()
        await polling


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored_bot", "identity", "expected_loads", "expected_saves"),
    [
        # Another bot's update ids form their own sequence below the old watermark.
        (_OTHER_BOT, _IDENTITY, [call("tg-assistant", _BOT)], [call("tg-assistant", _BOT, 5)]),
        # Without the bot's id no stored watermark can be matched to it.
        (_BOT, SimpleNamespace(id=None, username=None, full_name=None), [], []),
    ],
    ids=["another-bot", "unknown-identity"],
)
async def test_the_polling_watermark_belongs_to_the_polling_bot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stored_bot: int,
    identity: SimpleNamespace,
    expected_loads: list[Any],
    expected_saves: list[Any],
) -> None:
    storage = channel_state(tmp_path)
    storage.save_update_offset("tg-assistant", stored_bot, 900_000)
    store = Mock(wraps=storage)
    trigger = AsyncMock(return_value=make_completed_run(output_text="ok"))
    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        trigger_run=trigger,
        update_offset_store=store,
        running=False,
    )
    application = _FakeApplication(bot, identity)
    _install_ptb(monkeypatch, application)
    polling = await _start(adapter, application)

    await _deliver(application, _text_update(5, "hello"))
    await drain_chat_queue(adapter, 12345)
    await adapter.stop()
    await polling

    trigger.assert_awaited_once()
    assert store.load_update_offset.call_args_list == expected_loads
    assert store.save_update_offset.call_args_list == expected_saves


@pytest.mark.asyncio
async def test_update_ids_restart_after_the_idle_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Telegram may restart update ids after a week without updates; the adapter
    # forgets its in-memory watermark once the 48-hour offset lifetime has passed.
    clock = [0.0]
    monkeypatch.setattr(telegram_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    trigger = AsyncMock(side_effect=lambda *_args, **_kwargs: make_completed_run(output_text="ok"))
    adapter, _sessions, _trigger, _bot = make_adapter(
        tmp_path, monkeypatch, allowed_chat_ids=[12345], trigger_run=trigger
    )

    for hours, update_id, text in [
        (0, 100, "first"),
        (47, 99, "stale redelivery"),
        (48, 5, "after the window"),
        (48, 5, "duplicate delivery"),
        (48, 6, "next"),
    ]:
        clock[0] = hours * 60 * 60
        await adapter._handle_inbound_message(make_update(text=text, update_id=update_id), None)
    await drain_chat_queue(adapter, 12345)

    assert [awaited.args[1] for awaited in trigger.await_args_list] == [
        "first",
        "after the window",
        "next",
    ]
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_stop", [False, True])
async def test_stop_waits_for_an_in_flight_watermark_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_stop: bool
) -> None:
    # A replacement adapter loads the watermark at start, so stop must not return
    # (or shut down) before the last save landed, even when stop is cancelled.
    storage = channel_state(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def save(channel_id: str, bot_id: int, update_id: int) -> None:
        entered.set()
        assert release.wait(timeout=QUEUE_DRAIN_TIMEOUT_SECONDS)
        storage.save_update_offset(channel_id, bot_id, update_id)

    adapter, _sessions, _trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        update_offset_store=SimpleNamespace(
            load_update_offset=storage.load_update_offset, save_update_offset=save
        ),
        running=False,
    )
    application = _FakeApplication(bot, _IDENTITY)
    _install_ptb(monkeypatch, application)
    polling = await _start(adapter, application)
    # A denied chat's update is still claimed, so its watermark save starts.
    await _deliver(application, _text_update(7, "hello"))
    assert await asyncio.to_thread(entered.wait, QUEUE_DRAIN_TIMEOUT_SECONDS)

    stop = asyncio.create_task(adapter.stop())
    try:
        await asyncio.sleep(0)
        if cancel_stop:
            stop.cancel()
            await asyncio.sleep(0)
            stop.cancel()
        # A short observation window: the save is blocked, so any finite wait
        # shows stop still pending; release proves it then completes.
        done, _ = await asyncio.wait({stop}, timeout=0.05)
        assert not done
        assert "shutdown" not in application.events
    finally:
        release.set()
        await asyncio.gather(stop, return_exceptions=True)
    await polling

    assert storage.load_update_offset("tg-assistant", _BOT) == 7
    assert application.events.count("shutdown") == 1
    assert application.events[-2:] == ["shutdown", "bot.shutdown"]
