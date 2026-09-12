"""Telegram routing and commands: routing behavior."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from core.channels import ChannelConfigError
from core.channels.telegram import (
    TelegramChannelAdapter,
)
from core.chat import MessageSender
from core.sessions import ChatSessionManager, SessionAddress
from tests.core.channels.engine_test_support import (
    assert_member_trigger,
)
from tests.core.channels.telegram_test_support import (
    CHANNEL_GROUP_REPLY_SURFACE,
    drain_chat_queue,
    make_adapter,
    make_command_dispatcher,
    make_completed_run,
    make_config,
    make_update,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


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

    async with adapter._transport._typing_indicator("12345"):
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
