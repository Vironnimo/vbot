"""Discord: bounded history backfill travels with the addressed turn's admission."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core.attachments import AttachmentStore
from core.chat import MessageSender
from core.runs import ChatRunManager, Run

from .discord_test_support import (
    BOT_MENTION,
    GROUP_REPLY_SURFACE,
    DiscordHarness,
    FakeAttachment,
    FakeChannel,
    make_adapter,
    make_completed_run,
    make_message,
    session_address,
)
from .engine_test_support import HeldRuns, assert_member_trigger

pytestmark = pytest.mark.usefixtures("current_format_data_directory")

SESSION_ID = "ch-dc-assistant-100"


def context_messages(channel: FakeChannel, *message_ids: int) -> list[Any]:
    """Return channel history newest first, as the Discord API yields it."""
    return [
        make_message(channel, message_id=index, author_id=50, content=f"context {index}")
        for index in sorted(message_ids, reverse=True)
    ]


def context_notes(*message_ids: int) -> list[str]:
    return [f"[channel-message] [Alice|50|member]: context {index}" for index in message_ids]


def mention(channel: FakeChannel, message_id: int, **fields: Any) -> Any:
    return make_message(
        channel,
        message_id=message_id,
        author_id=50,
        content=f"question {message_id} <@999>",
        mentions=[BOT_MENTION],
        **fields,
    )


def record_notes_at_trigger(h: DiscordHarness) -> list[str]:
    """Make each triggered Run record the Session notes its Agent would see."""
    observed: list[str] = []

    async def trigger(_agent_id: str, _content: Any, session_id: str, **_kwargs: Any) -> Run:
        observed.extend(h.notes())
        return make_completed_run(session_id)

    h.trigger.side_effect = trigger
    return observed


@pytest.mark.asyncio
async def test_mention_backfills_history_since_last_bot_reply_in_order(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    channel.history_messages = [
        make_message(channel, message_id=12, author_id=51, content="second", display_name="Bob"),
        make_message(channel, message_id=11, author_id=50, content="first", display_name="Alice"),
        make_message(
            channel,
            message_id=10,
            author_id=999,
            content="previous bot reply",
            display_name="vBot",
            author_is_bot=True,
        ),
        make_message(channel, message_id=9, author_id=52, content="too old", display_name="Eve"),
    ]
    h = make_adapter(tmp_path, target=channel, allowed_chat_ids=[100])
    observed = record_notes_at_trigger(h)

    await h.receive(mention(channel, 13))
    await h.drain()

    assert observed == [
        "[channel-message] [Alice|50|member]: first",
        "[channel-message] [Bob|51|member]: second",
    ]
    assert_member_trigger(
        h.trigger,
        "assistant",
        "question 13 <@999>",
        SESSION_ID,
        sender=MessageSender(id="50", display_name="Alice"),
        reply_surface=GROUP_REPLY_SURFACE,
    )
    assert channel.history_calls[0]["limit"] == 50
    assert channel.history_calls[0]["oldest_first"] is False
    await h.adapter.stop()


@pytest.mark.asyncio
async def test_history_failure_still_processes_triggering_message(tmp_path: Path) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))

    def fail_history(**_kwargs: Any) -> Any:
        raise PermissionError("missing read message history")

    channel.history = fail_history  # type: ignore[method-assign]
    h = make_adapter(tmp_path, target=channel, allowed_chat_ids=[100])

    await h.receive(mention(channel, 13))
    await h.drain()

    h.trigger.assert_awaited_once()
    await h.adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observe_unaddressed", "notes"),
    [
        (False, None),
        (True, ["[channel-message] [Alice|50|member]: background context"]),
    ],
    ids=["dropped", "observed"],
)
async def test_unaddressed_group_message_never_backfills_history(
    tmp_path: Path, observe_unaddressed: bool, notes: list[str] | None
) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    channel.history_messages = context_messages(channel, 1)
    h = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
        observe_unaddressed=observe_unaddressed,
    )

    await h.receive(
        make_message(channel, message_id=11, author_id=50, content="background context")
    )
    await h.drain()

    # Passive observation records the message itself; without it nothing is stored.
    if notes is None:
        assert not h.sessions.exists(session_address(100))
    else:
        assert h.notes() == notes
    assert channel.history_calls == []
    h.trigger.assert_not_awaited()
    await h.adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_attachment", [False, True], ids=["text", "attachment"])
async def test_turn_refused_while_busy_keeps_its_history_for_the_retry(
    tmp_path: Path, with_attachment: bool
) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    channel.history_messages = context_messages(channel, 1, 2, 3)
    waiting = ChatRunManager(waiting_work_limit=1)
    h = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
        attachment_store=AttachmentStore(tmp_path),
        waiting_work_manager=waiting,
    )
    observed = record_notes_at_trigger(h)
    attachments = [FakeAttachment(300, "file.txt", b"context file")] if with_attachment else []
    message = mention(channel, 200, attachments=attachments)
    try:
        held = waiting.reserve_waiting_work(scope="busy", scope_limit=1)
        await h.receive(message)
        h.trigger.assert_not_awaited()
        assert waiting.waiting_work_count() == 1
        assert not h.sessions.exists(session_address(100))
        for attachment in attachments:
            attachment.read.assert_not_awaited()
        waiting.release_waiting_work(held)

        # Within a limit of one, the whole history rides on the turn's own admission.
        await h.receive(message)
        await h.drain()
        h.trigger.assert_awaited_once()
        assert observed == context_notes(1, 2, 3)
        assert waiting.waiting_work_count() == 0
    finally:
        await h.adapter.stop()
        await waiting.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["release", "stop"])
async def test_pending_turns_store_shared_history_once(tmp_path: Path, finish: str) -> None:
    channel = FakeChannel(100, guild=SimpleNamespace(id=1))
    runs = HeldRuns()
    waiting = ChatRunManager(waiting_work_limit=32)
    h = make_adapter(
        tmp_path,
        target=channel,
        allowed_chat_ids=[100],
        trigger_run=runs.trigger,
        waiting_work_manager=waiting,
    )
    try:
        await h.receive(mention(channel, 200))
        await runs.wait_started(SESSION_ID)
        # The active Run keeps both later turns pending in the chat's queue.
        channel.history_messages = context_messages(channel, 1, 2, 3)
        second = mention(channel, 210)
        await h.receive(second)
        channel.history_messages.insert(0, second)
        await h.receive(mention(channel, 211))
        # One admission per pending turn: the history rides on the second turn's.
        assert waiting.waiting_work_count() == 2

        if finish == "stop":
            await h.adapter.stop()
            assert waiting.waiting_work_count() == 0
            runs.trigger.assert_awaited_once()
        else:
            runs.release()
            await h.drain()
            assert runs.trigger.await_count == 3
            assert h.notes() == context_notes(1, 2, 3)
            assert waiting.waiting_work_count() == 0
    finally:
        runs.release()
        await h.adapter.stop()
        await waiting.aclose()
