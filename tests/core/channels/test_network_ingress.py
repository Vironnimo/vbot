"""Slack and Mattermost: connection handshakes and inbound message admission."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from core.attachments import AttachmentStore, AttachmentTooLargeError
from core.channels.config import ChannelError
from core.chat.content_blocks import TextBlock
from core.runs import WaitingWorkLimitError

from .engine_test_support import channel_state
from .network_test_support import event, make_adapter

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


class Socket:
    """WebSocket double that yields the given events, then closes."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = iter(events)
        self.send = AsyncMock()
        self.close = AsyncMock()

    async def __aenter__(self) -> Socket:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    def __aiter__(self) -> Socket:
        return self

    async def __anext__(self) -> str:
        try:
            return json.dumps(next(self.events))
        except StopIteration:
            raise StopAsyncIteration from None

    async def recv(self) -> str:
        return await self.__anext__()


@pytest.mark.asyncio
async def test_slack_acknowledges_before_dispatch_and_clears_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slack(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/auth.test"):
            return httpx.Response(200, json={"ok": True, "user_id": "BOT"})
        assert request.headers["authorization"] == "Bearer secret-APP"
        return httpx.Response(200, json={"ok": True, "url": "wss://wss.slack.com/link"})

    h = make_adapter(tmp_path, "slack", http=slack, connected=False)
    adapter = h.adapter
    socket = Socket(
        [{"type": "events_api", "envelope_id": "e1", "payload": {"event": {"text": "hi"}}}]
    )
    monkeypatch.setattr("core.channels.slack.connect", lambda *a, **kw: socket)

    async def handle(event: dict[str, Any]) -> None:
        assert adapter.connection_status() == {"connected": True}
        assert socket.send.await_args is not None
        assert json.loads(socket.send.await_args.args[0]) == {"envelope_id": "e1"}
        assert event["text"] == "hi"

    adapter.handle_event = AsyncMock(side_effect=handle)
    try:
        with pytest.raises(ChannelError, match="connection closed"):
            await adapter.start()
        adapter.handle_event.assert_awaited_once()
        assert adapter.connection_status() == {"connected": False}
        assert [request.url.path for request in h.requests] == [
            "/api/auth.test",
            "/api/apps.connections.open",
        ]
    finally:
        await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["OK", "FAIL"])
async def test_mattermost_requires_websocket_authentication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    def mattermost(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v4/users/me"
        return httpx.Response(200, json={"id": "BOT", "username": "vbot"})

    adapter = make_adapter(tmp_path, "mattermost", http=mattermost, connected=False).adapter
    socket = Socket([{"seq_reply": 1, "status": status}, {"event": "posted", "data": {}}])
    monkeypatch.setattr("core.channels.mattermost.connect", lambda *a, **kw: socket)
    adapter.handle_event = AsyncMock()
    try:
        with pytest.raises(ChannelError):
            await adapter.start()
        assert adapter.handle_event.await_count == (1 if status == "OK" else 0)
        assert socket.send.await_args is not None
        assert json.loads(socket.send.await_args.args[0])["data"] == {"token": "secret-BOT"}
        assert not adapter.connection_status()["connected"]
    finally:
        await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["slack", "mattermost"])
@pytest.mark.parametrize("media", [False, True], ids=["text", "media"])
@pytest.mark.parametrize("observe", [False, True], ids=["reply", "observe"])
async def test_inbound_uses_real_engine_and_persists_dedup(
    tmp_path: Path, platform: str, media: bool, observe: bool
) -> None:
    h = make_adapter(tmp_path, platform)
    h.adapter._config.observe_unaddressed = observe
    incoming = event(platform, direct=not observe)
    if media:
        h.adapter.build_media_blocks = AsyncMock(return_value=[TextBlock(type="text", text="file")])
        if platform == "slack":
            incoming["files"] = [{"id": "F1"}]
        else:
            post = json.loads(incoming["post"])
            post["file_ids"] = ["F1"]
            incoming["post"] = json.dumps(post)
    try:
        h.reserve_waiting_work.side_effect = WaitingWorkLimitError("full")
        await h.adapter.handle_event(incoming)
        h.trigger.assert_not_awaited()
        if media:
            h.adapter.build_media_blocks.assert_not_awaited()
        h.reserve_waiting_work.side_effect = None
        await h.adapter.handle_event(incoming)
        await h.drain()
        if observe:
            h.trigger.assert_not_awaited()
            if media:
                h.adapter.build_media_blocks.assert_not_awaited()
        else:
            h.trigger.assert_awaited_once()
            assert h.posted_texts()[-1] == "reply"
        reservations = h.reserve_waiting_work.call_count
        await h.adapter.handle_event(incoming)
        assert h.reserve_waiting_work.call_count == reservations
    finally:
        await h.adapter.stop()
    # A restarted adapter still recognizes the redelivered event.
    restarted = make_adapter(tmp_path, platform)
    try:
        await restarted.adapter.handle_event(incoming)
        restarted.reserve_waiting_work.assert_not_called()
    finally:
        await restarted.adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["slack", "mattermost"])
async def test_own_unaddressed_and_denied_messages_are_not_admitted(
    tmp_path: Path, platform: str
) -> None:
    h = make_adapter(tmp_path, platform)
    try:
        await h.adapter.handle_event(event(platform, user="BOT"))
        await h.adapter.handle_event(event(platform, direct=False))
        assert h.adapter.denied_chats() == []
        await h.adapter.handle_event(event(platform, chat="C2"))
        assert [entry.chat_id for entry in h.adapter.denied_chats()] == ["C2"]
        h.reserve_waiting_work.assert_not_called()
        h.trigger.assert_not_awaited()
    finally:
        await h.adapter.stop()


@pytest.mark.asyncio
async def test_failed_attachment_preserves_caption_and_other_file(tmp_path: Path) -> None:
    def files(request: httpx.Request) -> httpx.Response | None:
        if request.url.host == "files.slack.com":
            return httpx.Response(200, content=b"small text file")
        return None

    h = make_adapter(tmp_path, "slack", http=files)
    incoming = event("slack", text="keep this caption")
    incoming["files"] = [
        {"id": "BIG", "name": "big.txt", "size": 99999999999},
        {
            "id": "SMALL",
            "name": "small.txt",
            "size": 15,
            "url_private_download": "https://files.slack.com/small",
        },
    ]
    try:
        await h.adapter.handle_event(incoming)
        await h.drain()
        h.trigger.assert_awaited_once()
        assert h.trigger.await_args is not None
        content = h.trigger.await_args.args[1]
        assert content[0] == TextBlock(type="text", text="keep this caption")
        assert len(content) == 2
        # The oversized file is refused on its reported size, before any download.
        assert [r.url.path for r in h.requests if r.url.host == "files.slack.com"] == ["/small"]
    finally:
        await h.adapter.stop()


@pytest.mark.asyncio
async def test_slack_download_cannot_leak_token_and_is_bounded(tmp_path: Path) -> None:
    h = make_adapter(
        tmp_path,
        "slack",
        http=lambda _request: httpx.Response(200, content=b"1234"),
        attachment_store=AttachmentStore(tmp_path, max_size_bytes=3),
    )
    try:
        with pytest.raises(ChannelError):
            await h.adapter.download({"url": "https://evil.test/private"})
        assert h.requests == []
        with pytest.raises(AttachmentTooLargeError):
            await h.adapter.download({"url": "https://files.slack.com/private"})
    finally:
        await h.adapter.stop()


@pytest.mark.asyncio
async def test_cancelled_ingress_drains_receipt_write_before_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = make_adapter(tmp_path, "slack")
    database = channel_state(tmp_path, "test-slack").database
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_write = database.write

    def blocked_write(*args: Any, **kwargs: Any) -> Any:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return original_write(*args, **kwargs)

    monkeypatch.setattr(database, "write", blocked_write)
    receiving = asyncio.create_task(h.adapter.handle_event(event("slack")))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        receiving.cancel()
        done, _ = await asyncio.wait((receiving,), timeout=0.02)
        assert not done
        receiving.cancel()
        done, _ = await asyncio.wait((receiving,), timeout=0.02)
        assert not done
    finally:
        release.set()
        await asyncio.gather(receiving, return_exceptions=True)
        await h.adapter.stop()
    assert receiving.cancelled()
    restarted = make_adapter(tmp_path, "slack")
    try:
        await restarted.adapter.handle_event(event("slack"))
        restarted.reserve_waiting_work.assert_not_called()
    finally:
        await restarted.adapter.stop()
