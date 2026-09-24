"""Self-chat-only WhatsApp adapter over a private Node child-process pipe."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

from core.channels._network_adapter import NetworkChannelAdapter, channel_io
from core.channels._whatsapp_setup import (
    bridge_directory,
    bridge_ready,
    child_options,
    node_executable,
)
from core.channels.adapter import ConversationFacts, FileData
from core.channels.config import ChannelError


class WhatsAppChannelAdapter(NetworkChannelAdapter):
    platform = "whatsapp"
    platform_display_name = "WhatsApp"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._process: asyncio.subprocess.Process | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._write_lock = asyncio.Lock()
        self._sequence = 0
        self._qr: str | None = None
        self._qr_deadline = 0.0
        self._pairing_state = "starting"

    async def _listen(self) -> None:
        if not await channel_io(bridge_ready, self._state_dir):
            raise ChannelError("Install WhatsApp support from Channel settings before connecting")
        limit = self._attachment_store.max_size_bytes if self._attachment_store else 20_971_520
        self._process = await asyncio.create_subprocess_exec(
            node_executable(),
            str(bridge_directory(self._state_dir) / "bridge.js"),
            str(self._state_dir / "whatsapp"),
            str(limit),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=limit * 2 + 8192,
            **child_options(),
        )
        assert self._process.stdout is not None
        # The reader must keep resolving download/send replies while the ingress
        # worker waits for media or immediate Command delivery.
        inbound: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)

        async def consume() -> None:
            while True:
                await self.handle_event(await inbound.get())

        worker = asyncio.create_task(consume())

        async def read_events() -> None:
            assert self._process is not None and self._process.stdout is not None
            while line := await self._process.stdout.readline():
                event = json.loads(line)
                if "ok" in event:
                    future = self._pending.get(event.get("id"))
                    if future is not None and not future.done():
                        future.set_result(event)
                elif event.get("event") == "message":
                    try:
                        inbound.put_nowait(event)
                    except asyncio.QueueFull:
                        raise ChannelError("WhatsApp inbound buffer is full") from None
                elif event.get("event") == "qr":
                    self._qr = event["image"]
                    self._qr_deadline = time.monotonic() + 45
                    self._pairing_state = "pairing"
                elif event.get("event") == "connected":
                    self._connected = True
                    self._pairing_state = "connected"
                    self._qr = None
                elif event.get("event") in {"closed", "failed"}:
                    self._qr = None
                    self._connected = False
                    self._pairing_state = event.get("reason", "disconnected")
                    self._fail_pending_calls()
                    if self._pairing_state == "logged_out":
                        # Remain idle until an explicit new pairing request; repeated
                        # restarts cannot repair revoked device credentials.
                        await asyncio.Future()
            raise ChannelError("WhatsApp bridge disconnected", retryable=True)

        reader = asyncio.create_task(read_events())
        try:
            done, _ = await asyncio.wait((reader, worker), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            reader.cancel()
            worker.cancel()
            await asyncio.gather(reader, worker, return_exceptions=True)
            self._qr = None
            self._fail_pending_calls()

    def _fail_pending_calls(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ChannelError("WhatsApp connection closed"))

    async def stop(self) -> None:
        self._qr = None
        self._fail_pending_calls()
        process = self._process
        if process is not None:
            if process.stdin:
                process.stdin.close()
            try:
                async with asyncio.timeout(5):
                    await process.wait()
            except TimeoutError:
                process.kill()
                await process.wait()
            self._process = None
        await super().stop()

    def pairing_status(self) -> dict[str, Any]:
        return {
            "state": self._pairing_state,
            "qr_image": self._qr if time.monotonic() < self._qr_deadline else None,
        }

    async def call_bridge(self, payload: dict[str, Any]) -> dict[str, Any]:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            raise ChannelError("WhatsApp is not connected")
        self._sequence += 1
        request_id = str(self._sequence)
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with asyncio.timeout(90):
                async with self._write_lock:
                    process.stdin.write((json.dumps({**payload, "id": request_id}) + "\n").encode())
                    await process.stdin.drain()
                response = await future
            if not response.get("ok"):
                raise ChannelError(
                    "WhatsApp operation failed; check connection and attachment size"
                )
            return response
        except (TimeoutError, OSError):
            # Delivery might already have happened; do not automatically resend.
            raise ChannelError("WhatsApp operation could not be confirmed") from None
        finally:
            self._pending.pop(request_id, None)
            # Disconnect can fail the response while stdin.drain is still
            # pending. Consume that failure even if the pipe or caller cancels
            # before this coroutine reaches the response await.
            if future.done() and not future.cancelled():
                future.exception()
            else:
                future.cancel()

    def self_facts(self, message_id: str | None = None) -> ConversationFacts:
        return ConversationFacts(
            platform=self.platform,
            channel_id=self._config.id,
            chat_id="self",
            user_id="self",
            message_id=message_id,
        )

    async def handle_event(self, event: dict[str, Any]) -> None:
        if event.get("event") == "message" and isinstance(event.get("id"), str):
            await self.receive(self.self_facts(event["id"]), event)

    async def download(self, file: dict[str, Any]) -> bytes:
        response = await self.call_bridge({"action": "download", "message_id": file["id"]})
        return base64.b64decode(response["data"], validate=True)

    async def send_text(
        self,
        platform_target: str,
        text: str,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        await self.send(text, platform_target, thread_id=thread_id)

    async def send(
        self,
        message: str | None,
        platform_target: str,
        *,
        files: list[FileData] | None = None,
        thread_id: str | None = None,
        buttons: Any = None,
    ) -> None:
        self.check_send(message, files, buttons)
        if platform_target != "self" or thread_id is not None:
            raise ChannelError("WhatsApp supports only platform_target 'self', without threads")
        self.remember(self.self_facts())
        for chunk in self.message_chunks(message):
            await self.call_bridge({"action": "send", "target": "self", "text": chunk})
        for file in files or []:
            if self._attachment_store:
                self._attachment_store.ensure_within_limit(len(file.data))
            await self.call_bridge(
                {
                    "action": "send",
                    "target": "self",
                    "file": {
                        "name": file.filename,
                        "mimetype": file.media_type,
                        "data": base64.b64encode(file.data).decode(),
                    },
                }
            )
