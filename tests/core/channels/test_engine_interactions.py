"""Channel engine: button taps, origin-bound Run buttons and their compensation."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import core.channels.engine as engine_module
from core.channels.adapter import RunButtonBinding, bound_run_callback_data
from core.channels.state import ChannelStateStore
from core.database import DatabaseUnavailableError
from core.extensions import InteractionButton, InteractionEvent
from core.runs import ChatRunManager
from core.sessions import SessionAddress
from core.utils.timestamps import utc_now_timestamp

from .engine_test_support import (
    CHANNEL_REPLY_SURFACE,
    SESSION_ID,
    channel_state,
    drain,
    make_completed_run,
    make_conversation,
    make_engine,
    make_new_only_dispatcher,
)

_ANCHOR = SessionAddress(project_id=None, agent_id="assistant", session_id=SESSION_ID)


def _address(session_id: str) -> SessionAddress:
    return SessionAddress(project_id=None, agent_id="assistant", session_id=session_id)


def _tap_event(data: str, *rows: tuple[InteractionButton, ...]) -> InteractionEvent:
    return InteractionEvent(
        platform="telegram",
        channel_id="tg-assistant",
        chat_id="12345",
        user_id="50",
        message_id="777",
        data=data,
        buttons=rows or ((InteractionButton(label="Done", data=data),),),
        user_display_name="Alice",
    )


def _bound_tap(
    storage: ChannelStateStore,
    binding_id: str,
    origin_session_id: str,
    *leading_rows: tuple[InteractionButton, ...],
) -> InteractionEvent:
    """Save one origin-bound Run button and return the tap on it."""
    storage.save_run_button_binding(
        "tg-assistant",
        RunButtonBinding(
            id=binding_id,
            platform_target="12345",
            thread_id=None,
            origin_session_id=origin_session_id,
            original_button_data=("run:done",),
            created_at=utc_now_timestamp(),
        ),
    )
    data = bound_run_callback_data(binding_id, 0)
    if not leading_rows:
        return _tap_event(data)
    return _tap_event(data, *leading_rows, (InteractionButton(label="Fertig", data=data),))


def _claim_status(storage: ChannelStateStore, binding_id: str) -> str:
    return storage.claim_run_button_binding(
        "tg-assistant", binding_id, platform_target="12345", thread_id=None
    ).status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "user_id", "outcome", "tapper"),
    [
        ("direct", 50, "enqueued", None),
        # Group taps name the tapper so the Agent knows who acted on the shared Session.
        ("group", 50, "enqueued", "Tapped by: [Alice|50|admin]"),
        ("group", 99, "denied", None),
    ],
    ids=["direct", "group-admin", "group-member"],
)
async def test_a_tap_enqueues_an_internal_run_describing_the_keyboard(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    kind: str,
    user_id: int,
    outcome: str,
    tapper: str | None,
) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="synced"))
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path, admin_user_ids=["50"], trigger_run=trigger_mock
    )
    caplog.set_level(logging.DEBUG, logger="vbot.channels.engine")
    event = _tap_event(
        "run:done",
        (InteractionButton(label="✅ Milk", data="chk:milk"),),
        (InteractionButton(label="⬜ Bread", data="chk:bread"),),
        (InteractionButton(label="Fertig ✅", data="run:done"),),
    )

    result = await engine.trigger_interaction_reply(
        make_conversation(kind=kind, user_id=user_id, user_display_name="Alice", message_id="777"),
        event,
    )
    await drain(engine, 12345)
    await engine.stop()

    assert result == outcome
    if outcome == "denied":
        trigger_mock.assert_not_awaited()
        assert any("denied for member" in record.getMessage() for record in caplog.records)
        return
    trigger_mock.assert_awaited_once()
    await_args = trigger_mock.await_args
    assert await_args is not None
    # An internal note-driven Run: no visible user message, the content is the note.
    assert await_args.kwargs.get("internal") is True
    if kind == "direct":
        assert await_args.kwargs.get("reply_surface") == CHANNEL_REPLY_SURFACE
    note = await_args.args[1]
    assert 'Tapped button: "Fertig ✅" (run:done)' in note
    for line in ('- "✅ Milk" (chk:milk)', '- "⬜ Bread" (chk:bread)', '- "Fertig ✅" (run:done)'):
        assert line in note
    if tapper is None:
        assert "Tapped by:" not in note
    else:
        assert tapper in note


@pytest.mark.asyncio
async def test_bound_tap_repoints_conversation_and_orders_followup_in_origin_session(
    tmp_path: Path,
) -> None:
    storage = channel_state(tmp_path)
    trigger_mock = AsyncMock(
        side_effect=[
            make_completed_run(output_text="synced", session_id="origin-session"),
            make_completed_run(output_text="deleted", session_id="origin-session"),
        ]
    )
    engine, sessions, _trigger, transport = make_engine(
        tmp_path, trigger_run=trigger_mock, run_button_binding_registry=storage
    )
    sessions.create("assistant", session_id="origin-session")
    event = _bound_tap(
        storage,
        "binding-1",
        "origin-session",
        (InteractionButton(label="✅ Milk", data="chk:milk"),),
    )
    conversation = make_conversation(kind="direct", user_id=50)

    outcome = await engine.trigger_interaction_reply(conversation, event)
    await engine.handle_inbound_text(conversation, "den rest kannst du löschen")
    await drain(engine, 12345)

    assert outcome == "enqueued"
    first_call, second_call = trigger_mock.await_args_list
    assert first_call.args[2] == "origin-session"
    # The note shows the original button data, never the internal binding data.
    assert "run:done" in first_call.args[1]
    assert event.data not in first_call.args[1]
    assert second_call.args[:3] == ("assistant", "den rest kannst du löschen", "origin-session")
    assert storage.active_session_id("tg-assistant", SESSION_ID) == "origin-session"
    assert transport.sent_texts == ["synced", "deleted"]

    assert await engine.trigger_interaction_reply(conversation, event) == "already_handled"
    assert len(trigger_mock.await_args_list) == 2
    await engine.stop()


@pytest.mark.asyncio
async def test_bound_tap_does_not_recreate_missing_origin_session(tmp_path: Path) -> None:
    storage = channel_state(tmp_path)
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="unexpected"))
    engine, sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, run_button_binding_registry=storage
    )
    event = _bound_tap(storage, "binding-missing", "deleted-session")

    outcome = await engine.trigger_interaction_reply(make_conversation(), event)

    assert outcome == "unavailable"
    assert not sessions.exists(_address("deleted-session"))
    trigger_mock.assert_not_awaited()
    await engine.stop()


@pytest.mark.asyncio
async def test_new_detaches_the_conversation_after_a_bound_tap(tmp_path: Path) -> None:
    storage = channel_state(tmp_path)
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="synced"))
    engine, sessions, _trigger, _transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        command_dispatcher=make_new_only_dispatcher(),
        run_button_binding_registry=storage,
    )
    sessions.create("assistant", session_id="origin-session")
    event = _bound_tap(storage, "binding-new", "origin-session")
    conversation = make_conversation()

    assert await engine.trigger_interaction_reply(conversation, event) == "enqueued"
    await drain(engine, 12345)
    await engine.handle_inbound_text(conversation, "/new")
    await drain(engine, 12345)

    detached_session_id = storage.active_session_id("tg-assistant", SESSION_ID)
    assert detached_session_id is not None
    assert detached_session_id not in {SESSION_ID, "origin-session"}
    # /new creates no Session; the conversation's next message starts it.
    assert not sessions.exists(_address(detached_session_id))
    await engine.handle_inbound_text(conversation, "next")
    await drain(engine, 12345)
    assert sessions.exists(_address(detached_session_id))
    assert trigger_mock.await_args_list[-1].args[1:3] == ("next", detached_session_id)
    await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_origin", [False, True])
async def test_waiting_bound_tap_keeps_origin_after_anchor_changes(
    tmp_path: Path, delete_origin: bool
) -> None:
    storage = channel_state(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()

    async def trigger(agent_id: str, content: Any, session_id: str, **kwargs: Any) -> Any:
        if content == "hold":
            started.set()
            await release.wait()
        return make_completed_run(output_text="done", session_id=session_id)

    trigger_mock = AsyncMock(side_effect=trigger)
    engine, sessions, _, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, run_button_binding_registry=storage
    )
    sessions.create("assistant", session_id="origin")
    sessions.create("assistant", session_id="other")
    event = _bound_tap(storage, "waiting-binding", "origin")
    conversation = make_conversation()
    try:
        await engine.handle_inbound_text(conversation, "hold")
        await asyncio.wait_for(started.wait(), 10)
        assert await engine.trigger_interaction_reply(conversation, event) == "enqueued"
        storage.point_conversation("tg-assistant", SESSION_ID, "direct", "other")
        if delete_origin:
            await sessions.archive(_address("origin"))
        release.set()
        await drain(engine, 12345)
        assert [call.args[2] for call in trigger_mock.await_args_list] == (
            [SESSION_ID] if delete_origin else [SESSION_ID, "origin"]
        )
        assert storage.active_session_id("tg-assistant", SESSION_ID) == "other"
        if delete_origin:
            assert not sessions.exists(_address("origin"))
    finally:
        release.set()
        await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prior_pointer", "concurrent"),
    [(None, None), ("prior-session", None), (None, "metadata"), ("prior-session", "navigation")],
    ids=["no-pointer", "prior-pointer", "concurrent-metadata", "concurrent-navigation"],
)
async def test_busy_bound_tap_restores_binding_and_previous_conversation_pointer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prior_pointer: str | None,
    concurrent: str | None,
) -> None:
    storage = channel_state(tmp_path)
    waiting_work = ChatRunManager(waiting_work_limit=1)
    held_admission = waiting_work.reserve_waiting_work(scope="already-busy", scope_limit=1)
    engine, sessions, trigger_mock, transport = make_engine(
        tmp_path, waiting_work_manager=waiting_work, run_button_binding_registry=storage
    )
    for session_id in (SESSION_ID, "prior-session", "origin-session"):
        sessions.create("assistant", session_id=session_id)
    sessions.set_metadata(_ANCHOR, {"existing": "preserved"})
    if prior_pointer is not None:
        storage.point_conversation("tg-assistant", SESSION_ID, "direct", prior_pointer)
    event = _bound_tap(storage, "binding-busy", "origin-session")

    expected_metadata = {"existing": "preserved"}
    expected_pointer = prior_pointer
    if concurrent is not None:
        original_enqueue = engine._enqueue_chat_work
        if concurrent == "metadata":
            expected_metadata["new_metadata"] = "preserved"
        else:
            expected_pointer = "newer-session"

        def enqueue(*args: Any) -> Any:
            # Another writer changes the conversation while the tap is admitted.
            if concurrent == "metadata":
                sessions.mutate_metadata(
                    _ANCHOR, lambda metadata: metadata.update(new_metadata="preserved")
                )
            else:
                storage.point_conversation("tg-assistant", SESSION_ID, "direct", "newer-session")
            return original_enqueue(*args)

        monkeypatch.setattr(engine, "_enqueue_chat_work", enqueue)

    outcome = await engine.trigger_interaction_reply(make_conversation(), event)

    assert outcome == "busy"
    assert sessions.get_metadata(_ANCHOR) == expected_metadata
    assert storage.active_session_id("tg-assistant", SESSION_ID) == expected_pointer
    assert _claim_status(storage, "binding-busy") == "claimed"
    trigger_mock.assert_not_awaited()
    assert transport.sent_texts == [engine_module._BUSY_REPLY]
    waiting_work.release_waiting_work(held_admission)
    await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["claim", "pointer", "lookup_failure"])
async def test_aborted_bound_tap_restores_unadmitted_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    storage = channel_state(tmp_path)
    engine, sessions, trigger, _transport = make_engine(
        tmp_path, run_button_binding_registry=storage
    )
    sessions.create("assistant", session_id=SESSION_ID)
    sessions.create("assistant", session_id="origin")
    previous = {"other": "keep"}
    sessions.set_metadata(_ANCHOR, previous)
    storage.point_conversation("tg-assistant", SESSION_ID, "direct", "prior")
    event = _bound_tap(storage, "cancelled-binding", "origin")
    entered, release = threading.Event(), threading.Event()
    owner, name = {
        "claim": (storage, "claim_run_button_binding"),
        "pointer": (engine._routing, "_point_conversation_at_session"),
        "lookup_failure": (sessions, "exists"),
    }[stage]
    original = getattr(owner, name)

    def delayed(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        if stage == "lookup_failure":
            raise OSError("Session lookup failed")
        return result

    monkeypatch.setattr(owner, name, delayed)
    pending = asyncio.create_task(engine.trigger_interaction_reply(make_conversation(), event))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        if stage != "lookup_failure":
            pending.cancel()
        release.set()
        with pytest.raises(OSError if stage == "lookup_failure" else asyncio.CancelledError):
            await pending
        assert sessions.get_metadata(_ANCHOR) == previous
        assert storage.active_session_id("tg-assistant", SESSION_ID) == "prior"
        monkeypatch.setattr(owner, name, original)
        assert _claim_status(storage, "cancelled-binding") == "claimed"
        trigger.assert_not_awaited()
    finally:
        release.set()
        await engine.stop()


@pytest.mark.asyncio
async def test_bound_tap_on_a_closed_session_database_restores_the_binding(
    tmp_path: Path,
) -> None:
    storage = channel_state(tmp_path)
    engine, sessions, trigger, _transport = make_engine(
        tmp_path, run_button_binding_registry=storage
    )
    sessions.create("assistant", session_id="origin")
    event = _bound_tap(storage, "closed-binding", "origin")
    sessions.close()
    try:
        with pytest.raises(DatabaseUnavailableError):
            await engine.trigger_interaction_reply(make_conversation(), event)

        # The claim was compensated on the Channel state's own pool.
        assert _claim_status(storage, "closed-binding") == "claimed"
        trigger.assert_not_awaited()
    finally:
        await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_aborted_tap_cannot_undo_later_admission_to_same_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_waiter: bool
) -> None:
    storage = channel_state(tmp_path)
    trigger = AsyncMock(return_value=make_completed_run(output_text="done", session_id="origin"))
    engine, sessions, _, _ = make_engine(
        tmp_path, run_button_binding_registry=storage, trigger_run=trigger
    )
    sessions.create("assistant", session_id=SESSION_ID)
    sessions.create("assistant", session_id="origin")
    storage.point_conversation("tg-assistant", SESSION_ID, "direct", "prior")
    events = [_bound_tap(storage, name, "origin") for name in ("first", "second")]
    entered, release = threading.Event(), threading.Event()
    original = engine._routing._point_conversation_at_session
    calls = 0

    def delayed(*args: Any) -> Any:
        nonlocal calls
        calls += 1
        first = calls == 1
        result = original(*args)
        if first:
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(engine._routing, "_point_conversation_at_session", delayed)
    first = asyncio.create_task(engine.trigger_interaction_reply(make_conversation(), events[0]))
    pending = [first]
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        first.cancel()
        if cancel_waiter:
            waiter = asyncio.create_task(
                engine.trigger_interaction_reply(make_conversation(), events[1])
            )
            pending.append(waiter)
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
        second = asyncio.create_task(
            engine.trigger_interaction_reply(make_conversation(), events[1])
        )
        pending.append(second)
        completed, _ = await asyncio.wait({second}, timeout=0.05)
        assert not completed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert await second == "enqueued"
        assert storage.active_session_id("tg-assistant", SESSION_ID) == "origin"
        await drain(engine, 12345)
        assert trigger.await_count == 1
    finally:
        release.set()
        await asyncio.gather(*pending, return_exceptions=True)
        await engine.stop()
