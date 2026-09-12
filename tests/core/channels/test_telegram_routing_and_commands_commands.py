"""Telegram routing and commands: commands behavior."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import core.channels._conversation_routing as routing_module
import core.channels.engine as engine_module
from core.chat.commands import (
    CommandFeedback,
    CommandOutcome,
    CommandUnavailability,
)
from core.sessions import SessionAddress
from tests.core.channels.engine_test_support import (
    make_new_only_dispatcher,
)
from tests.core.channels.telegram_test_support import (
    drain_chat_queue,
    make_adapter,
    make_command_dispatcher,
    make_update,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


@pytest.mark.asyncio
async def test_plain_text_command_is_dispatched_before_trigger_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_dispatcher = make_command_dispatcher(
        result=CommandOutcome(
            command="stop",
            feedback=CommandFeedback(kind="notice", text="Run cancelled."),
        )
    )
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        command_dispatcher=command_dispatcher,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/stop"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    command_dispatcher.execute.assert_awaited_once()
    context = command_dispatcher.execute.await_args.args[1]
    assert (context.agent_id, context.session_id) == (
        "assistant",
        "ch-tg-assistant-12345",
    )
    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(chat_id=12345, text="Run cancelled.")
    await adapter.stop()


@pytest.mark.asyncio
async def test_compact_command_action_replies_without_trigger_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_dispatcher = make_command_dispatcher(
        result=CommandOutcome(
            command="compact",
            feedback=CommandFeedback(kind="notice", text="Context compacted."),
        )
    )
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        command_dispatcher=command_dispatcher,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/compact"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    command_dispatcher.execute.assert_awaited_once()
    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(chat_id=12345, text="Context compacted.")
    await adapter.stop()


@pytest.mark.asyncio
async def test_new_command_starts_fresh_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_dispatcher = make_new_only_dispatcher()
    adapter, chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        command_dispatcher=command_dispatcher,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text="/new"),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    anchor_metadata = chat_sessions.get_metadata(
        SessionAddress(project_id=None, agent_id="assistant", session_id="ch-tg-assistant-12345")
    )
    new_session_id = anchor_metadata[routing_module.ACTIVE_SESSION_METADATA_KEY]
    assert new_session_id.startswith("ses_")
    assert chat_sessions.exists(
        SessionAddress(project_id=None, agent_id="assistant", session_id=new_session_id)
    )
    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(
        chat_id=12345,
        text=engine_module._NEW_SESSION_STARTED_REPLY,
    )
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["/agent", "/agent planner"])
async def test_agent_command_reports_permanent_channel_limitation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    command_dispatcher = make_command_dispatcher(
        result=CommandOutcome(command="agent"),
        unavailable=CommandUnavailability(command="/agent", surface="channel"),
    )
    adapter, _chat_sessions, trigger_mock, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[12345],
        command_dispatcher=command_dispatcher,
    )

    await adapter._handle_inbound_message(
        make_update(chat_id=12345, user_id=50, text=message),
        SimpleNamespace(),
    )
    await drain_chat_queue(adapter, 12345)

    trigger_mock.assert_not_awaited()
    bot.send_message.assert_awaited_once_with(
        chat_id=12345,
        text="The /agent command is not available through Telegram.",
    )
    command_dispatcher.execute.assert_not_awaited()
    await adapter.stop()
