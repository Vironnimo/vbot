"""Telegram routing and commands: application behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import core.channels.telegram as telegram_module
from tests.core.channels.telegram_test_support import (
    make_adapter,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


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
