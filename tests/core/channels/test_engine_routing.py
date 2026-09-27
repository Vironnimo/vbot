"""Channel engine: Session derivation, routing metadata and routing on database pools."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from core.channels.adapter import RouteFacts
from core.database import DatabaseUnavailableError
from core.sessions import SessionAddress

from .engine_test_support import (
    SESSION_ID,
    channel_state,
    drain,
    make_completed_run,
    make_conversation,
    make_engine,
    make_new_only_dispatcher,
)

_ADDRESS = SessionAddress(project_id=None, agent_id="assistant", session_id=SESSION_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("dm_scope", "kind", "chat_id", "expected"),
    [
        ("per_conversation", "direct", 12345, "ch-tg-assistant-12345"),
        ("main", "direct", 12345, "ch-tg-assistant-main"),
        ("per_peer", "direct", 12345, "ch-tg-assistant-u987"),
        ("per_account_channel_peer", "direct", 12345, "ch-tg-assistant-12345-u987"),
        ("main", "group", -10001, "ch-tg-assistant--10001"),
    ],
)
async def test_the_dm_scope_derives_the_routed_session(
    tmp_path: Path, dm_scope: str, kind: str, chat_id: int, expected: str
) -> None:
    engine, _sessions, _trigger, _transport = make_engine(tmp_path, dm_scope=dm_scope)
    try:
        route = await engine.ensure_channel_session(
            make_conversation(chat_id=chat_id, user_id=987, kind=kind)
        )
    finally:
        await engine.stop()

    assert route == RouteFacts(agent_id="assistant", session_id=expected)


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["inbound", "outbound"])
async def test_async_routing_runs_each_database_on_its_own_pool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    direction: str,
) -> None:
    engine, _sessions, _trigger, _transport = make_engine(tmp_path)
    routing = engine._routing
    started = threading.Event()
    release = threading.Event()
    threads: dict[str, str] = {}
    route_facts = routing._route_facts
    update_session_metadata = routing._update_session_metadata

    def blocking_route_facts(conversation: Any) -> RouteFacts:
        threads["pointer"] = threading.current_thread().name
        started.set()
        assert release.wait(timeout=2)
        return route_facts(conversation)

    def recorded_update(*args: Any, **kwargs: Any) -> None:
        threads["session"] = threading.current_thread().name
        update_session_metadata(*args, **kwargs)

    monkeypatch.setattr(routing, "_route_facts", blocking_route_facts)
    monkeypatch.setattr(routing, "_update_session_metadata", recorded_update)

    async def route_conversation() -> RouteFacts:
        if direction == "inbound":
            route, _reply_plan = await routing._prepare_inbound_route_async(make_conversation())
            return route
        # A proactive channel_send target.
        return await engine.ensure_channel_session(make_conversation())

    route_task = asyncio.create_task(route_conversation())
    assert await asyncio.to_thread(started.wait, 2)
    await asyncio.sleep(0)

    assert route_task.done() is False
    release.set()
    route = await route_task
    assert route.session_id == SESSION_ID
    # Pointer work on the Channel state's pool, Session work on the Session pool.
    assert threads["pointer"].startswith("vbot-db-channels")
    assert threads["session"].startswith("vbot-db-sessions")
    await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("closed", ["channels", "sessions"])
async def test_outbound_routing_on_a_closed_database_fails_cleanly(
    tmp_path: Path, closed: str
) -> None:
    engine, chat_sessions, _trigger, _transport = make_engine(tmp_path)
    try:
        if closed == "channels":
            channel_state(tmp_path).close()
        else:
            chat_sessions.close()

        with pytest.raises(DatabaseUnavailableError):
            await engine.ensure_channel_session(make_conversation())

        if closed == "channels":
            # The pointer read fails before any Session is created.
            assert chat_sessions.exists(_ADDRESS) is False
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_routing_writes_channel_metadata_without_notes_or_routine_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    trigger_mock = AsyncMock(
        side_effect=[
            make_completed_run(output_text="first"),
            make_completed_run(output_text="second"),
        ]
    )
    engine, chat_sessions, _trigger, _transport = make_engine(tmp_path, trigger_run=trigger_mock)

    with caplog.at_level(logging.INFO, logger="vbot.channels.engine"):
        await engine.handle_inbound_text(make_conversation(), "hello")
        await drain(engine, 12345)
        # The outbound entry and a second message reuse the same Session.
        route = await engine.ensure_channel_session(make_conversation())
        await engine.handle_inbound_text(make_conversation(), "hello")
        await drain(engine, 12345)
    await engine.stop()

    assert route == RouteFacts(agent_id="assistant", session_id=SESSION_ID)
    assert [
        message for message in chat_sessions.get(_ADDRESS).load() if message.role == "note"
    ] == []
    metadata = chat_sessions.get_metadata(_ADDRESS)
    assert metadata["source_channel_id"] == "tg-assistant"
    assert metadata["platform"] == "telegram"
    assert metadata["platform_conv_id"] == "12345"
    assert metadata["last_reply_target"] == {
        "channel_id": "tg-assistant",
        "platform_target": "12345",
    }
    assert not [record for record in caplog.records if record.name == "vbot.channels.engine"]


@pytest.mark.asyncio
async def test_ensure_channel_session_follows_pointer_after_new(tmp_path: Path) -> None:
    command_dispatcher = make_new_only_dispatcher()
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/new")
    await drain(engine, 12345)

    new_session_id = channel_state(tmp_path).active_session_id("tg-assistant", SESSION_ID)
    assert new_session_id is not None
    # Proactive channel_send resolves to the active (pointer) session, not the anchor.
    route = await engine.ensure_channel_session(make_conversation())
    assert route == RouteFacts(agent_id="assistant", session_id=new_session_id)
    await engine.stop()
