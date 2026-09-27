"""Channel engine: group addressing, observed messages, participants and permissions."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.chat import MessageSender
from core.runs import RunKind
from core.sessions import SessionAddress

from .engine_test_support import (
    CHANNEL_GROUP_REPLY_SURFACE,
    CHANNEL_REPLY_SURFACE,
    SESSION_ID,
    MemoryChannelAccessRegistry,
    assert_member_trigger,
    channel_state,
    command_outcome,
    drain,
    make_command_dispatcher,
    make_completed_run,
    make_conversation,
    make_engine,
)

MEMBER_TOOL_DENIAL = (
    "Tool access denied: the current sender is a group member. "
    "Group members may use only web_search and web_fetch."
)
_ADDRESS = SessionAddress(project_id=None, agent_id="assistant", session_id=SESSION_ID)


def _observed_notes(chat_sessions: Any) -> list[str]:
    return [
        message.content
        for message in chat_sessions.get(_ADDRESS).load()
        if message.role == "note"
        and isinstance(message.content, str)
        and message.content.startswith("[channel-message] ")
    ]


@pytest.mark.asyncio
async def test_member_runs_carry_the_sender_and_keep_member_tool_access(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    access = MemoryChannelAccessRegistry()
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        response_mode="all",
        access_registry=access,
    )

    await engine.handle_inbound_text(
        make_conversation(kind="group", user_display_name="Alice"), "hello"
    )
    await drain(engine, 12345)

    resolver = assert_member_trigger(
        trigger_mock,
        "assistant",
        "hello",
        SESSION_ID,
        sender=MessageSender(id="50", display_name="Alice", role="member"),
    )
    assert resolver("web_search") is None
    assert resolver("web_fetch") is None
    assert resolver("bash") == MEMBER_TOOL_DENIAL
    assert "Do not retry" not in MEMBER_TOOL_DENIAL
    # A grant after ingress does not upgrade the admitted Run.
    access.admin_user_ids.add("50")
    assert resolver("bash") == MEMBER_TOOL_DENIAL
    await engine.stop()


@pytest.mark.asyncio
async def test_revoke_before_next_tool_call_limits_active_admin_run(
    tmp_path: Path,
) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    access = MemoryChannelAccessRegistry(["50"])
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        response_mode="all",
        access_registry=access,
    )

    await engine.handle_inbound_text(
        make_conversation(kind="group", user_display_name="Alice"),
        "hello",
    )
    await drain(engine, 12345)

    awaited = trigger_mock.await_args
    assert awaited is not None
    assert awaited.args == ("assistant", "hello", SESSION_ID)
    kwargs = dict(awaited.kwargs)
    assert kwargs.pop("sender") == MessageSender(id="50", display_name="Alice", role="admin")
    assert kwargs.pop("reply_surface") == CHANNEL_GROUP_REPLY_SURFACE
    assert "tool_restriction" not in kwargs
    assert kwargs.pop("run_kind") is RunKind.CHANNEL
    resolver = kwargs.pop("tool_denial_resolver")
    assert kwargs == {}
    assert resolver("bash") is None

    access.admin_user_ids.remove("50")

    assert resolver("bash") == MEMBER_TOOL_DENIAL
    assert resolver("web_search") is None
    await engine.stop()


@pytest.mark.asyncio
async def test_group_sender_role_follows_the_user_id_and_names_fall_back_to_it(
    tmp_path: Path,
) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        response_mode="all",
        access_registry=MemoryChannelAccessRegistry(["50"]),
    )

    for user_id, display_name in ((50, "Same Name"), (51, "Same Name"), (52, None)):
        await engine.handle_inbound_text(
            make_conversation(kind="group", user_id=user_id, user_display_name=display_name),
            "message",
        )
        await drain(engine, 12345)

    assert [call.kwargs["sender"] for call in trigger_mock.await_args_list] == [
        MessageSender(id="50", display_name="Same Name", role="admin"),
        MessageSender(id="51", display_name="Same Name", role="member"),
        MessageSender(id="52", display_name="52", role="member"),
    ]
    await engine.stop()


@pytest.mark.asyncio
async def test_direct_message_triggers_without_sender_even_in_mention_mode(
    tmp_path: Path,
) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, observe_unaddressed=True
    )

    await engine.handle_inbound_text(
        make_conversation(kind="direct", user_display_name="Alice"), "hello"
    )
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once_with(
        "assistant",
        "hello",
        SESSION_ID,
        sender=None,
        reply_surface=CHANNEL_REPLY_SURFACE,
        run_kind=RunKind.CHANNEL,
    )
    assert _observed_notes(chat_sessions) == []
    await engine.stop()


@pytest.mark.asyncio
async def test_group_participants_live_in_channel_state_not_session_metadata(
    tmp_path: Path,
) -> None:
    state = channel_state(tmp_path)
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path, observe_unaddressed=True, access_registry=state
    )

    for user_id, display_name in ((50, "Alice"), (51, "Bob"), (50, "Alice Renamed")):
        await engine.handle_inbound_text(
            make_conversation(kind="group", user_id=user_id, user_display_name=display_name),
            "hello everyone",
        )
    await drain(engine, 12345)
    await engine.stop()

    access = await state.access_state("tg-assistant")
    [group] = access["groups"]
    assert group["access_scope_id"] == "12345"
    assert [
        (participant["user_id"], participant["display_name"], participant["role"])
        for participant in group["participants"]
    ] == [("50", "Alice Renamed", "member"), ("51", "Bob", "member")]
    metadata = chat_sessions.get_metadata(_ADDRESS)
    assert not {"participants", "conversation_kind", "active_session_id"} & metadata.keys()


@pytest.mark.asyncio
async def test_an_observed_message_on_a_known_conversation_writes_only_its_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, chat_sessions, _trigger, _transport = make_engine(tmp_path, observe_unaddressed=True)
    database = chat_sessions._store.database
    original = database.write
    writes: list[object] = []

    def counted(fn, **kwargs):  # type: ignore[no-untyped-def]
        writes.append(fn)
        return original(fn, **kwargs)

    monkeypatch.setattr(database, "write", counted)

    # The first message creates the Session with its channel context in one write.
    await engine.handle_inbound_text(
        make_conversation(kind="group", user_display_name="Alice"), "hello everyone"
    )
    await drain(engine, 12345)
    assert len(writes) == 2
    for user_id, display_name in ((50, "Alice"), (51, "Bob")):
        writes.clear()
        await engine.handle_inbound_text(
            make_conversation(kind="group", user_id=user_id, user_display_name=display_name),
            "hello again",
        )
        await drain(engine, 12345)
        # Participants are Channel state, so even a new sender writes only the note.
        assert len(writes) == 1
    await engine.stop()


@pytest.mark.asyncio
async def test_group_unaddressed_text_is_dropped_in_mention_mode(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher()
    engine, chat_sessions, trigger_mock, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(kind="group"), "hello everyone")
    await drain(engine, 12345)

    trigger_mock.assert_not_awaited()
    command_dispatcher.execute.assert_not_awaited()
    assert transport.sent == []
    # Dropped messages must not create a Session either.
    assert not chat_sessions.exists(_ADDRESS)
    await engine.stop()


@pytest.mark.asyncio
async def test_group_unaddressed_text_is_observed_as_a_sanitized_note(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher()
    engine, chat_sessions, trigger_mock, transport = make_engine(
        tmp_path,
        command_dispatcher=command_dispatcher,
        observe_unaddressed=True,
    )

    await engine.handle_inbound_text(
        make_conversation(kind="group", user_id="|50]\r", user_display_name="[Alice]\n|"),
        "hello\nworld",
    )
    await drain(engine, 12345)

    assert _observed_notes(chat_sessions) == ["[channel-message] [Alice|50|member]: hello\nworld"]
    trigger_mock.assert_not_awaited()
    command_dispatcher.execute.assert_not_awaited()
    assert transport.sent == []
    metadata = chat_sessions.get_metadata(_ADDRESS)
    assert metadata["last_reply_target"] == {
        "channel_id": "tg-assistant",
        "platform_target": "12345",
    }
    await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mentioned_bot", "is_reply_to_bot", "text"),
    [
        (True, False, "hello bot"),
        (False, True, "hello bot"),
        (False, False, "Hey VBOT, status?"),
    ],
    ids=["mention", "reply-to-bot", "wake-word-any-case"],
)
async def test_addressed_group_text_triggers_a_run_in_mention_mode(
    tmp_path: Path, mentioned_bot: bool, is_reply_to_bot: bool, text: str
) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        mention_patterns=[r"\bvbot\b"],
        observe_unaddressed=True,
        admin_user_ids=["99"],
    )

    await engine.handle_inbound_text(
        make_conversation(
            kind="group", mentioned_bot=mentioned_bot, is_reply_to_bot=is_reply_to_bot
        ),
        text,
    )
    await drain(engine, 12345)

    # A member's addressed message starts a normal Run, not an observation.
    trigger_mock.assert_awaited_once()
    assert transport.sent_texts == ["ok"]
    assert _observed_notes(chat_sessions) == []
    await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "admin_user_ids", "response_mode", "dispatched"),
    [
        ("group", ["50"], "mention", True),
        ("group", ["99"], "mention", False),
        ("group", ["99"], "all", False),
        ("direct", ["99"], "mention", True),
    ],
    ids=["group-admin", "group-member", "group-member-all-mode", "direct-message"],
)
async def test_group_commands_require_an_admin_sender(
    tmp_path: Path,
    kind: str,
    admin_user_ids: list[str],
    response_mode: str,
    dispatched: bool,
) -> None:
    command_dispatcher = make_command_dispatcher(result=command_outcome("stop", "Run cancelled."))
    engine, chat_sessions, trigger_mock, transport = make_engine(
        tmp_path,
        command_dispatcher=command_dispatcher,
        admin_user_ids=admin_user_ids,
        response_mode=response_mode,
        observe_unaddressed=True,
    )

    await engine.handle_inbound_text(make_conversation(kind=kind, user_id=50), "/stop")
    await drain(engine, 12345)

    trigger_mock.assert_not_awaited()
    if dispatched:
        command_dispatcher.execute.assert_awaited_once()
        assert transport.sent_texts == ["Run cancelled."]
    else:
        # A denied Command is neither dispatched nor observed, and creates no Session.
        command_dispatcher.execute.assert_not_awaited()
        assert transport.sent == []
        assert not chat_sessions.exists(_ADDRESS)
    await engine.stop()
