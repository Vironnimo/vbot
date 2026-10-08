"""Channels: outbound delivery, completion relays and origin-bound Run buttons."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.channels import ChannelConfigError, ChannelError, ChannelNotFoundError
from core.channels.adapter import RouteFacts, parse_bound_run_callback_data
from core.channels.channels import ChannelService
from core.channels.state import ChannelStateStore
from core.chat import ReplySurface
from core.database import DatabaseUnavailableError
from core.extensions import InteractionButton
from core.runs import ASSISTANT_OUTPUT_EVENT, Run
from core.sessions import ChatSessionManager, SessionAddress
from tests.core.channels.channels_test_support import (
    BlockingAdapter,
    start_with_adapter,
)
from tests.core.channels.engine_test_support import connect, settle_replies

pytestmark = pytest.mark.usefixtures("current_format_data_directory")

_ORIGIN = RouteFacts(agent_id="assistant", session_id="origin-session")


def _saved_run_button_ids(service: ChannelService, channel_id: str) -> list[str]:
    with service.database.read() as connection:
        rows = connection.execute(
            "SELECT binding_id FROM channel_run_buttons WHERE channel_id = ?",
            (channel_id,),
        ).fetchall()
    return [str(row[0]) for row in rows]


def _origin_sessions(tmp_path: Path) -> ChatSessionManager:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("assistant", session_id="origin-session")
    return sessions


async def _send_run_button(service: ChannelService, origin: RouteFacts = _ORIGIN) -> None:
    await service.send(
        "tg-assistant",
        "Shopping",
        "12345",
        buttons=[[InteractionButton(label="Done", data="run:done")]],
        run_origin=origin,
    )


@pytest.mark.asyncio
async def test_send_reaches_the_running_adapter_after_validating_buttons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = BlockingAdapter()
    service = await start_with_adapter(tmp_path, monkeypatch, adapter)
    rows = [[InteractionButton(label="Milk ⬜", data="chk:milk")]]
    try:
        await service.send("tg-assistant", "Hello", "12345")
        await service.send("tg-assistant", "Shopping", "12345", buttons=rows)
        # Empty data, and 65 bytes of data (one over Telegram's 64-byte cap),
        # fail on every platform before the adapter is reached.
        for data in ("", "d" * 65):
            with pytest.raises(ChannelConfigError):
                await service.send(
                    "tg-assistant", "hi", "12345", buttons=[[InteractionButton("x", data)]]
                )

        assert adapter.sent_messages == [("Hello", "12345"), ("Shopping", "12345")]
        assert adapter.sent_buttons == [None, rows]
    finally:
        await service.aclose()
        service.close()

    with pytest.raises(ChannelNotFoundError):
        await service.send("tg-assistant", "hello", "12345")


@pytest.mark.asyncio
async def test_completion_run_relays_to_persisted_channel_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = ChatSessionManager(tmp_path)
    session_id = "ch-tg-assistant-12345"
    sessions.create("assistant", session_id=session_id)
    sessions.set_metadata(
        SessionAddress(project_id=None, agent_id="assistant", session_id=session_id),
        {
            "last_reply_target": {
                "channel_id": "tg-assistant",
                "platform_target": "12345",
                "thread_id": "77",
            }
        },
    )
    adapter = BlockingAdapter()
    service = await start_with_adapter(tmp_path, monkeypatch, adapter, chat_sessions=sessions)
    engine = service._active_engine("tg-assistant")
    connect(engine, adapter)
    run = Run(run_id="completion-run", agent_id="assistant", session_id=session_id)
    run.emit(ASSISTANT_OUTPUT_EVENT, {"message": {"content": "Done."}})
    run.mark_completed("Done.")
    surface = ReplySurface.channel(
        platform="telegram",
        platform_display_name="Telegram",
        channel_id="tg-assistant",
    )
    try:
        await service.relay_completion_run(run, surface)
        await settle_replies(engine)
    finally:
        await service.aclose()
        service.close()

    assert adapter.replies == [("12345", "77", "Done.")]


@pytest.mark.asyncio
async def test_channel_service_persists_and_rewrites_origin_bound_run_buttons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = BlockingAdapter()
    service = await start_with_adapter(
        tmp_path, monkeypatch, adapter, chat_sessions=_origin_sessions(tmp_path)
    )
    try:
        await service.send(
            "tg-assistant",
            "Shopping",
            "12345",
            buttons=[
                [
                    InteractionButton(label="Milk", data="chk:milk"),
                    InteractionButton(label="Fertig", data="run:done"),
                ]
            ],
            run_origin=_ORIGIN,
        )
    finally:
        await service.aclose()
        service.close()

    sent_buttons = adapter.sent_buttons[0]
    assert sent_buttons is not None
    assert sent_buttons[0][0].data == "chk:milk"
    parsed = parse_bound_run_callback_data(sent_buttons[0][1].data)
    assert parsed is not None
    binding_id, button_index = parsed
    assert button_index == 0

    # A reopened state store proves the binding is durable, not adapter memory.
    reloaded = ChannelStateStore.open(tmp_path)
    try:
        mismatch = reloaded.claim_run_button_binding(
            "tg-assistant", binding_id, platform_target="99999", thread_id=None
        )
        claimed = reloaded.claim_run_button_binding(
            "tg-assistant", binding_id, platform_target="12345", thread_id=None
        )
        replayed = reloaded.claim_run_button_binding(
            "tg-assistant", binding_id, platform_target="12345", thread_id=None
        )
    finally:
        reloaded.close()

    assert (mismatch.status, claimed.status, replayed.status) == (
        "target_mismatch",
        "claimed",
        "consumed",
    )
    assert claimed.binding is not None
    assert claimed.binding.origin_session_id == "origin-session"
    assert claimed.binding.original_button_data == ("run:done",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin", "error"),
    [
        (RouteFacts(agent_id="assistant", session_id="missing-session"), ChannelConfigError),
        (RouteFacts(agent_id="other-agent", session_id="foreign-session"), ChannelConfigError),
        (_ORIGIN, DatabaseUnavailableError),
        (_ORIGIN, ChannelError),
    ],
    ids=["missing-origin", "foreign-agent", "closed-session-database", "adapter-send-failure"],
)
async def test_a_refused_or_failed_run_button_send_keeps_no_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: RouteFacts,
    error: type[Exception],
) -> None:
    sessions = _origin_sessions(tmp_path)
    sessions.create("other-agent", session_id="foreign-session")
    adapter = BlockingAdapter()
    service = await start_with_adapter(tmp_path, monkeypatch, adapter, chat_sessions=sessions)
    if error is DatabaseUnavailableError:
        sessions.close()
    if error is ChannelError:
        monkeypatch.setattr(adapter, "send", AsyncMock(side_effect=ChannelError("wire failed")))
    try:
        with pytest.raises(error):
            await _send_run_button(service, origin)

        assert adapter.sent_messages == []
        assert _saved_run_button_ids(service, "tg-assistant") == []
    finally:
        await service.aclose()
        service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ChannelError("wire unconfirmed", possibly_delivered=True),
        # An unexpected failure while sending leaves delivery unknown as well.
        RuntimeError("adapter bug"),
    ],
    ids=["possibly-delivered", "unexpected-failure"],
)
async def test_a_possibly_delivered_run_button_send_keeps_its_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    adapter = BlockingAdapter()
    service = await start_with_adapter(
        tmp_path, monkeypatch, adapter, chat_sessions=_origin_sessions(tmp_path)
    )
    monkeypatch.setattr(adapter, "send", AsyncMock(side_effect=error))
    try:
        with pytest.raises(type(error)):
            await _send_run_button(service)

        # A tap on a button the chat may show must still reach its origin Session.
        binding_ids = _saved_run_button_ids(service, "tg-assistant")
        assert len(binding_ids) == 1
        claim = service._state.claim_run_button_binding(
            "tg-assistant", binding_ids[0], platform_target="12345", thread_id=None
        )
        assert claim.status == "claimed"
    finally:
        await service.aclose()
        service.close()


@pytest.mark.asyncio
async def test_run_button_preparation_runs_each_database_on_its_own_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = _origin_sessions(tmp_path)
    adapter = BlockingAdapter()
    service = await start_with_adapter(tmp_path, monkeypatch, adapter, chat_sessions=sessions)
    threads: dict[str, str] = {}
    session_exists = sessions.exists
    save_binding = service._state.save_run_button_binding

    def recorded_exists(address: SessionAddress) -> bool:
        threads["origin"] = threading.current_thread().name
        return session_exists(address)

    def recorded_save(channel_id: str, binding: object) -> None:
        threads["binding"] = threading.current_thread().name
        save_binding(channel_id, binding)  # type: ignore[arg-type]

    monkeypatch.setattr(sessions, "exists", recorded_exists)
    monkeypatch.setattr(service._state, "save_run_button_binding", recorded_save)
    try:
        await _send_run_button(service)

        assert adapter.sent_messages == [("Shopping", "12345")]
        assert len(_saved_run_button_ids(service, "tg-assistant")) == 1
        # The origin check on the Session pool, the binding on the Channel state's pool.
        assert threads["origin"].startswith("vbot-db-sessions")
        assert threads["binding"].startswith("vbot-db-channels")
    finally:
        await service.aclose()
        service.close()


@pytest.mark.asyncio
async def test_cancel_during_run_button_preparation_discards_unsent_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = BlockingAdapter()
    service = await start_with_adapter(
        tmp_path, monkeypatch, adapter, chat_sessions=_origin_sessions(tmp_path)
    )
    saved = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_save = service._state.save_run_button_binding

    def blocked_save(channel_id, binding):
        original_save(channel_id, binding)
        loop.call_soon_threadsafe(saved.set)
        assert release.wait(timeout=5)

    monkeypatch.setattr(service._state, "save_run_button_binding", blocked_save)
    sending = asyncio.create_task(_send_run_button(service))
    try:
        await asyncio.wait_for(saved.wait(), timeout=2)
        sending.cancel()
        await asyncio.sleep(0)
        sending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await sending
        assert _saved_run_button_ids(service, "tg-assistant") == []
        assert adapter.sent_messages == []
    finally:
        release.set()
        await asyncio.gather(sending, return_exceptions=True)
        await service.aclose()
        service.close()


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_unsent_binding_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = BlockingAdapter()
    service = await start_with_adapter(
        tmp_path, monkeypatch, adapter, chat_sessions=_origin_sessions(tmp_path)
    )
    monkeypatch.setattr(adapter, "send", AsyncMock(side_effect=ChannelError("wire failed")))
    cleaning = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_discard = service._state.discard_run_button_binding

    def blocked_discard(channel_id, binding_id):
        loop.call_soon_threadsafe(cleaning.set)
        assert release.wait(timeout=5)
        original_discard(channel_id, binding_id)

    monkeypatch.setattr(service._state, "discard_run_button_binding", blocked_discard)
    sending = asyncio.create_task(_send_run_button(service))
    try:
        await asyncio.wait_for(cleaning.wait(), timeout=2)
        sending.cancel()
        await asyncio.sleep(0)
        sending.cancel()
        await asyncio.sleep(0)
        assert not sending.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await sending
        assert _saved_run_button_ids(service, "tg-assistant") == []
    finally:
        release.set()
        await asyncio.gather(sending, return_exceptions=True)
        await service.aclose()
        service.close()
