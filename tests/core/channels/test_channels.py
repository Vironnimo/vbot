"""Channels: delivery behavior."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.channels import (
    ChannelConfigError,
    ChannelError,
    ChannelStorage,
)
from core.channels.adapter import (
    RouteFacts,
    parse_bound_run_callback_data,
)
from core.channels.channels import ChannelService
from core.channels.state import ChannelStateStore
from core.chat import ReplySurface
from core.database import DatabaseUnavailableError
from core.extensions import InteractionButton
from core.runs import Run
from core.sessions import ChatSessionManager, SessionAddress
from tests.core.channels.channels_helpers import (
    BlockingAdapter,
    make_config,
    make_service,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


def _saved_run_button_ids(service: ChannelService, channel_id: str) -> list[str]:
    with service.database.read() as connection:
        rows = connection.execute(
            "SELECT binding_id FROM channel_run_buttons WHERE channel_id = ?",
            (channel_id,),
        ).fetchall()
    return [str(row[0]) for row in rows]


@pytest.mark.asyncio
async def test_completion_run_relays_to_persisted_channel_target(tmp_path: Path) -> None:
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
    service = make_service(tmp_path, chat_sessions=sessions)
    service._storage.save(make_config(allowed_chat_ids=["12345"]))
    adapter = BlockingAdapter()

    async def stay_running() -> None:
        await asyncio.Event().wait()

    running = asyncio.create_task(stay_running())
    service._adapters["tg-assistant"] = adapter
    service._adapter_tasks["tg-assistant"] = running
    run = Run(run_id="completion-run", agent_id="assistant", session_id=session_id)
    surface = ReplySurface.channel(
        platform="telegram",
        platform_display_name="Telegram",
        channel_id="tg-assistant",
    )

    try:
        await service.relay_completion_run(run, surface)
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)

    assert len(adapter.relayed_runs) == 1
    relayed_run, reply_plan = adapter.relayed_runs[0]
    assert relayed_run is run
    assert reply_plan.channel_id == "tg-assistant"
    assert reply_plan.platform_target == "12345"
    assert reply_plan.thread_id == "77"


@pytest.mark.asyncio
async def test_channel_service_send_routes_to_running_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

    service = make_service(tmp_path)
    adapter = BlockingAdapter()
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)

    service.start_channel(config.id)
    await asyncio.wait_for(adapter.started.wait(), timeout=1)

    # Act
    await service.send(config.id, "Hello", "12345")

    # Assert
    assert adapter.sent_messages == [("Hello", "12345")]

    # Cleanup
    service.stop()
    await asyncio.wait_for(adapter.stopped.wait(), timeout=1)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_channel_service_send_passes_buttons_to_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

    service = make_service(tmp_path)
    adapter = BlockingAdapter()
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)

    service.start_channel(config.id)
    await asyncio.wait_for(adapter.started.wait(), timeout=1)

    rows = [[InteractionButton(label="Milk ⬜", data="chk:milk")]]
    await service.send(config.id, "Shopping", "12345", buttons=rows)

    assert adapter.sent_messages == [("Shopping", "12345")]
    assert adapter.sent_buttons == [rows]

    service.stop()
    await asyncio.wait_for(adapter.stopped.wait(), timeout=1)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_channel_service_persists_and_rewrites_origin_bound_run_buttons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)
    sessions = ChatSessionManager(tmp_path)
    sessions.create("assistant", session_id="origin-session")

    service = make_service(tmp_path, chat_sessions=sessions)
    adapter = BlockingAdapter()
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)
    service.start_channel(config.id)
    await asyncio.wait_for(adapter.started.wait(), timeout=1)

    await service.send(
        config.id,
        "Shopping",
        "12345",
        buttons=[
            [
                InteractionButton(label="Milk", data="chk:milk"),
                InteractionButton(label="Fertig", data="run:done"),
            ]
        ],
        run_origin=RouteFacts(agent_id="assistant", session_id="origin-session"),
    )

    sent_buttons = adapter.sent_buttons[0]
    assert sent_buttons is not None
    assert sent_buttons[0][0].data == "chk:milk"
    parsed = parse_bound_run_callback_data(sent_buttons[0][1].data)
    assert parsed is not None
    binding_id, button_index = parsed
    assert button_index == 0

    service.stop()
    await asyncio.wait_for(adapter.stopped.wait(), timeout=1)
    await asyncio.sleep(0)
    service.close()

    # A reopened state store proves the binding is durable, not adapter memory.
    reloaded_storage = ChannelStateStore.open(tmp_path)
    mismatch = reloaded_storage.claim_run_button_binding(
        config.id,
        binding_id,
        platform_target="99999",
        thread_id=None,
    )
    assert mismatch.status == "target_mismatch"
    claim = reloaded_storage.claim_run_button_binding(
        config.id,
        binding_id,
        platform_target="12345",
        thread_id=None,
    )
    assert claim.status == "claimed"
    assert claim.binding is not None
    assert claim.binding.origin_session_id == "origin-session"
    assert claim.binding.original_button_data == ("run:done",)
    second_claim = reloaded_storage.claim_run_button_binding(
        config.id,
        binding_id,
        platform_target="12345",
        thread_id=None,
    )
    assert second_claim.status == "consumed"
    reloaded_storage.close()


@pytest.mark.asyncio
async def test_channel_service_discards_run_binding_when_send_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)
    sessions = ChatSessionManager(tmp_path)
    sessions.create("assistant", session_id="origin-session")
    service = make_service(tmp_path, chat_sessions=sessions)
    adapter = BlockingAdapter()
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)
    service.start_channel(config.id)
    await asyncio.wait_for(adapter.started.wait(), timeout=1)
    monkeypatch.setattr(adapter, "send", AsyncMock(side_effect=ChannelError("wire failed")))

    with pytest.raises(ChannelError, match="wire failed"):
        await service.send(
            config.id,
            "Shopping",
            "12345",
            buttons=[[InteractionButton(label="Fertig", data="run:done")]],
            run_origin=RouteFacts(agent_id="assistant", session_id="origin-session"),
        )

    assert _saved_run_button_ids(service, config.id) == []
    service.stop()
    await asyncio.wait_for(adapter.stopped.wait(), timeout=1)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cancel_during_run_button_preparation_discards_unsent_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(enabled=True)
    ChannelStorage(tmp_path).save(config)
    sessions = ChatSessionManager(tmp_path)
    sessions.create("assistant", session_id="origin-session")
    service = make_service(tmp_path, chat_sessions=sessions)
    adapter = BlockingAdapter()
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)
    service.start_channel(config.id)
    await adapter.started.wait()
    saved = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_save = service._state.save_run_button_binding

    def blocked_save(channel_id, binding):
        original_save(channel_id, binding)
        loop.call_soon_threadsafe(saved.set)
        assert release.wait(timeout=5)

    monkeypatch.setattr(service._state, "save_run_button_binding", blocked_save)
    sending = asyncio.create_task(
        service.send(
            config.id,
            "Shopping",
            "12345",
            buttons=[[InteractionButton(label="Done", data="run:done")]],
            run_origin=RouteFacts(agent_id="assistant", session_id="origin-session"),
        )
    )
    try:
        await asyncio.wait_for(saved.wait(), timeout=2)
        sending.cancel()
        await asyncio.sleep(0)
        sending.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await sending
        assert _saved_run_button_ids(service, config.id) == []
        assert adapter.sent_messages == []
    finally:
        release.set()
        await asyncio.gather(sending, return_exceptions=True)
        await service.aclose()


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_unsent_binding_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = make_config(enabled=True)
    ChannelStorage(tmp_path).save(config)
    sessions = ChatSessionManager(tmp_path)
    sessions.create("assistant", session_id="origin-session")
    service = make_service(tmp_path, chat_sessions=sessions)
    adapter = BlockingAdapter()
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)
    monkeypatch.setattr(adapter, "send", AsyncMock(side_effect=ChannelError("wire failed")))
    service.start_channel(config.id)
    await adapter.started.wait()
    cleaning = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_discard = service._state.discard_run_button_binding

    def blocked_discard(channel_id, binding_id):
        loop.call_soon_threadsafe(cleaning.set)
        assert release.wait(timeout=5)
        original_discard(channel_id, binding_id)

    monkeypatch.setattr(service._state, "discard_run_button_binding", blocked_discard)
    sending = asyncio.create_task(
        service.send(
            config.id,
            "Shopping",
            "12345",
            buttons=[[InteractionButton(label="Done", data="run:done")]],
            run_origin=RouteFacts(agent_id="assistant", session_id="origin-session"),
        )
    )
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
        assert _saved_run_button_ids(service, config.id) == []
    finally:
        release.set()
        await asyncio.gather(sending, return_exceptions=True)
        await service.aclose()


async def _started_run_button_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sessions: ChatSessionManager
) -> tuple[ChannelService, BlockingAdapter]:
    config = make_config(enabled=True)
    ChannelStorage(tmp_path).save(config)
    service = make_service(tmp_path, chat_sessions=sessions)
    adapter = BlockingAdapter()
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)
    service.start_channel(config.id)
    await asyncio.wait_for(adapter.started.wait(), timeout=1)
    return service, adapter


async def _send_run_button(service: ChannelService, origin: RouteFacts) -> None:
    await service.send(
        "tg-assistant",
        "Shopping",
        "12345",
        buttons=[[InteractionButton(label="Done", data="run:done")]],
        run_origin=origin,
    )


@pytest.mark.asyncio
async def test_run_button_preparation_runs_each_database_on_its_own_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("assistant", session_id="origin-session")
    service, adapter = await _started_run_button_service(tmp_path, monkeypatch, sessions)
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
        await _send_run_button(
            service, RouteFacts(agent_id="assistant", session_id="origin-session")
        )

        assert adapter.sent_messages == [("Shopping", "12345")]
        assert len(_saved_run_button_ids(service, "tg-assistant")) == 1
        # The origin check on the Session pool, the binding on the Channel state's pool.
        assert threads["origin"].startswith("vbot-db-sessions")
        assert threads["binding"].startswith("vbot-db-channels")
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin", "error"),
    [
        (RouteFacts(agent_id="assistant", session_id="missing-session"), ChannelConfigError),
        (RouteFacts(agent_id="other-agent", session_id="foreign-session"), ChannelConfigError),
        (RouteFacts(agent_id="assistant", session_id="origin-session"), DatabaseUnavailableError),
    ],
    ids=["missing-origin", "foreign-agent", "closed-session-database"],
)
async def test_refused_run_button_origin_sends_and_binds_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin: RouteFacts,
    error: type[Exception],
) -> None:
    sessions = ChatSessionManager(tmp_path)
    sessions.create("assistant", session_id="origin-session")
    sessions.create("other-agent", session_id="foreign-session")
    service, adapter = await _started_run_button_service(tmp_path, monkeypatch, sessions)
    if error is DatabaseUnavailableError:
        sessions.close()
    try:
        with pytest.raises(error):
            await _send_run_button(service, origin)

        assert adapter.sent_messages == []
        assert _saved_run_button_ids(service, "tg-assistant") == []
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_channel_service_send_rejects_oversized_callback_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

    service = make_service(tmp_path)
    adapter = BlockingAdapter()
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)

    service.start_channel(config.id)
    await asyncio.wait_for(adapter.started.wait(), timeout=1)

    # 65 bytes of callback data: one over Telegram's 64-byte cap.
    oversized = [[InteractionButton(label="x", data="d" * 65)]]
    with pytest.raises(ChannelConfigError):
        await service.send(config.id, "hi", "12345", buttons=oversized)

    # The rejected send never reached the adapter.
    assert adapter.sent_messages == []

    service.stop()
    await asyncio.wait_for(adapter.stopped.wait(), timeout=1)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_channel_service_send_rejects_empty_button_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = ChannelStorage(tmp_path)
    config = make_config(enabled=True)
    storage.save(config)

    service = make_service(tmp_path)
    adapter = BlockingAdapter()
    monkeypatch.setattr(service, "_create_adapter", lambda _config: adapter)

    service.start_channel(config.id)
    await asyncio.wait_for(adapter.started.wait(), timeout=1)

    with pytest.raises(ChannelConfigError):
        await service.send(
            config.id, "hi", "12345", buttons=[[InteractionButton(label="x", data="")]]
        )

    service.stop()
    await asyncio.wait_for(adapter.stopped.wait(), timeout=1)
    await asyncio.sleep(0)
