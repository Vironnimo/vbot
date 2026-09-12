"""Channels: delivery behavior."""

from __future__ import annotations

import asyncio
import json
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
from core.chat import ReplySurface
from core.extensions import InteractionButton
from core.runs import Run
from core.sessions import ChatSessionManager, SessionAddress
from tests.core.channels.channels_helpers import (
    BlockingAdapter,
    make_config,
    make_service,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


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

    # A fresh storage instance proves the binding is durable, not adapter memory.
    reloaded_storage = ChannelStorage(tmp_path)
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

    service.stop()
    await asyncio.wait_for(adapter.stopped.wait(), timeout=1)
    await asyncio.sleep(0)


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

    binding_path = tmp_path / "channels" / config.id / "run-button-bindings.json"
    payload = json.loads(binding_path.read_text(encoding="utf-8"))
    assert payload["bindings"] == {}
    service.stop()
    await asyncio.wait_for(adapter.stopped.wait(), timeout=1)
    await asyncio.sleep(0)


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
