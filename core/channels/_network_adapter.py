"""Shared engine wiring and bounded I/O for the HTTP/socket Channel adapters.

Platform adapters own authentication, event parsing and wire requests. This private
base keeps their common attachment, routing and resource lifecycle in Channels.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from core.attachments import AttachmentStore
from core.channels._message_chunks import split_message
from core.channels.adapter import (
    ChannelAccessRegistry,
    ChannelAdapter,
    ConversationFacts,
    ConversationPointerStore,
    DeniedChatFacts,
    DeniedChatLog,
    FileData,
    QuotedMessageFacts,
    ReceivedMessageStore,
    ReplyPlanFacts,
    RouteFacts,
    content_blocks_for_attachment,
)
from core.channels.config import ChannelConfig, ChannelConfigError, ChannelError
from core.channels.engine import ChannelConversationEngine
from core.chat.content_blocks import ContentBlock, TextBlock
from core.utils.retry import retry_async
from core.utils.tls import shared_ssl_context
from core.utils.workers import BoundedWorkerPool

# Readable per-message size for Slack, Mattermost and WhatsApp (below each wire limit).
_MESSAGE_CHUNK_LIMIT = 3500
_IO_POOL = BoundedWorkerPool(name="channel-network-io", max_workers=4)


async def channel_io(function: Callable[..., Any], *args: Any) -> Any:
    return await _IO_POOL.run(function, *args)


class NetworkChannelAdapter(ChannelAdapter):
    """Private base for transports receiving JSON messages over persistent connections."""

    platform_display_name = "Channel"

    def __init__(
        self,
        config: ChannelConfig,
        trigger_service: Any,
        chat_sessions: Any,
        credential_resolver: Callable[[str], str],
        attachment_store: AttachmentStore | None = None,
        *,
        command_dispatcher: Any,
        conversation_pointers: ConversationPointerStore,
        received_messages: ReceivedMessageStore,
        access_registry: ChannelAccessRegistry | None = None,
        state_dir: Path,
    ) -> None:
        self._config = config
        self._attachment_store = attachment_store
        self._credential_resolver = credential_resolver
        self._state_dir = state_dir
        self._received = received_messages
        self._engine = ChannelConversationEngine(
            config,
            trigger_service,
            chat_sessions,
            self,
            command_dispatcher=command_dispatcher,
            conversation_pointers=conversation_pointers,
            access_registry=access_registry,
        )
        self._http_client: httpx.AsyncClient | None = None
        self._socket: Any = None
        self._connected = False
        self._bot_id = ""
        self._denied = DeniedChatLog()
        self._conversations: OrderedDict[str, ConversationFacts] = OrderedDict()
        self._ingress_lock = asyncio.Lock()

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=30,
                trust_env=False,
                follow_redirects=False,
                verify=shared_ssl_context(),
            )
        return self._http_client

    async def start(self) -> None:
        try:
            await self._listen()
            raise ChannelError(f"{self.platform_display_name} connection closed", retryable=True)
        except ChannelError as error:
            raise ChannelError(
                str(error), retryable=error.retryable, retry_after=error.retry_after
            ) from None
        except Exception:
            # Socket URLs can contain credentials; never let SDK exception text or
            # chained request objects enter the service's failure/log projection.
            raise ChannelError(
                f"{self.platform_display_name} connection failed", retryable=True
            ) from None
        finally:
            self._connected = False

    async def _listen(self) -> None:
        raise NotImplementedError

    async def send_text(
        self,
        platform_target: str,
        text: str,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        raise NotImplementedError

    def credential(self, key: str) -> str:
        token = self._credential_resolver(key)
        if not isinstance(token, str) or not token.strip():
            raise ChannelConfigError(f"Missing {self.platform_display_name} credential in {key}")
        return token.strip()

    async def stop(self) -> None:
        self._connected = False
        await self._engine.stop()
        if self._socket is not None:
            await self._socket.close()
            self._socket = None
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    def connection_status(self) -> dict[str, Any]:
        return {"connected": self._connected}

    def denied_chats(self) -> list[DeniedChatFacts]:
        return self._denied.entries()

    async def relay_run(self, run: Any, reply_plan: ReplyPlanFacts) -> None:
        await self._engine.relay_run(run, reply_plan)

    def remember(self, facts: ConversationFacts) -> None:
        self._conversations[facts.chat_id] = facts
        self._conversations.move_to_end(facts.chat_id)
        while len(self._conversations) > 1024:
            self._conversations.popitem(last=False)

    async def ensure_outbound_session(
        self, platform_target: str, *, thread_id: str | None = None
    ) -> RouteFacts:
        facts = self._conversations.get(platform_target)
        if facts is None:
            raise ChannelError("Send to the target before recording outbound context")
        return await self._engine.ensure_channel_session(replace(facts, thread_id=thread_id))

    async def receive(self, facts: ConversationFacts, raw: dict[str, Any]) -> None:
        if facts.chat_id not in self._config.allowed_chat_ids:
            self._denied.record(
                chat_id=facts.chat_id, kind=facts.kind, display_name=raw.get("display_name")
            )
            return
        async with self._ingress_lock:
            # Platforms redeliver recent events after a reconnect; a receipt is
            # recorded only after the conversation accepted the message.
            receipt = f"{facts.chat_id}:{facts.message_id}"
            if facts.message_id is None or await self._received.has_received(
                self._config.id, receipt
            ):
                return
            self.remember(facts)
            if raw.get("files"):
                # One failing file must not discard the other attachments or caption.
                messages = tuple({**raw, "files": [file], "text": ""} for file in raw["files"])
                await self._engine.handle_inbound_media(
                    facts, messages, companion_text=raw.get("text") or None
                )
            elif raw.get("text"):
                await self._engine.handle_inbound_text(facts, raw["text"], raw_message=raw)
            else:
                return
            await self._received.record_received(self._config.id, receipt)

    def caption_text(self, raw_message: Any) -> str | None:
        return raw_message.get("text") or None

    async def build_quoted_message(self, raw_message: Any) -> QuotedMessageFacts | None:
        if raw_message.get("quoted"):
            return QuotedMessageFacts(user_id=None, user_display_name=None, content=None)
        return None

    def activity_indicator(
        self, platform_target: str, thread_id: str | None = None
    ) -> contextlib.AbstractAsyncContextManager[None]:
        return contextlib.nullcontext()

    async def build_media_blocks(self, raw_message: Any) -> list[ContentBlock]:
        blocks: list[ContentBlock] = []
        if raw_message.get("text"):
            blocks.append(TextBlock(type="text", text=raw_message["text"]))
        for file in raw_message.get("files", []):
            if self._attachment_store is None:
                raise ChannelError("Attachment storage is unavailable")
            self._attachment_store.ensure_within_limit(file.get("size"))
            data = await self.download(file)
            record = await self._attachment_store.store_async(
                file.get("name") or "attachment", data
            )
            blocks.extend(content_blocks_for_attachment(record))
        return blocks

    async def download(self, file: dict[str, Any]) -> bytes:
        raise NotImplementedError

    async def read_download(self, url: str, headers: dict[str, str]) -> bytes:
        if self._attachment_store is None:
            raise ChannelError("Attachment storage is unavailable")
        try:
            async with self._http.stream("GET", url, headers=headers) as response:
                self.check_response(response)
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    self._attachment_store.ensure_within_limit(len(chunks) + len(chunk))
                    chunks.extend(chunk)
                return bytes(chunks)
        except httpx.RequestError:
            raise ChannelError("Attachment download failed", retryable=True) from None

    @staticmethod
    def check_response(response: httpx.Response, *, retry_server_error: bool = True) -> None:
        if response.is_success:
            return
        status = response.status_code
        retry_after = response.headers.get("retry-after", "")
        raise ChannelError(
            f"Channel request failed (HTTP {status})",
            retryable=status == 429 or (status >= 500 and retry_server_error),
            retry_after=float(retry_after) if retry_after.isdigit() else None,
        )

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            response = await self._http.request(method, url, **kwargs)
            self.check_response(response, retry_server_error=method == "GET")
            return response.json()
        except httpx.RequestError:
            # A failed write response does not establish whether delivery happened.
            raise ChannelError(
                "Channel request could not be confirmed", retryable=method == "GET"
            ) from None
        except ValueError:
            raise ChannelError("Channel returned an invalid response") from None

    def check_send(self, message: str | None, files: list[FileData] | None, buttons: Any) -> None:
        if buttons is not None:
            raise ChannelError(f"Buttons are not supported on {self.platform_display_name}")
        if not message and not files:
            raise ChannelConfigError("Provide a message or files")
        if not self._connected:
            raise ChannelError(f"{self.platform_display_name} is not connected", retryable=True)

    @staticmethod
    def message_chunks(message: str | None) -> list[str]:
        """Split outbound Markdown text at readable boundaries, keeping code fences intact."""
        return split_message(message or "", _MESSAGE_CHUNK_LIMIT)

    async def _send_operation(
        self, operation: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any
    ) -> Any:
        """Retry one wire operation without replaying acknowledged chunks or files."""
        try:
            return await retry_async(operation, *args, **kwargs)
        except ChannelError as error:
            # A caller cannot safely restart a multipart delivery after this
            # operation exhausts its budget: earlier parts may already be visible.
            error.retryable = False
            raise
