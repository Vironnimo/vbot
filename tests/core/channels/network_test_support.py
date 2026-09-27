"""Slack, Mattermost and WhatsApp adapter harness on the real Channel engine."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx

from core.attachments import AttachmentStore
from core.channels._network_adapter import NetworkChannelAdapter
from core.channels.config import ChannelConfig
from core.channels.mattermost import MattermostChannelAdapter
from core.channels.slack import SlackChannelAdapter
from core.channels.whatsapp import WhatsAppChannelAdapter
from core.runs import ASSISTANT_OUTPUT_EVENT, Run
from core.sessions import ChatSessionManager

from .engine_test_support import (
    MemoryChannelAccessRegistry,
    channel_state,
    drain,
    make_command_dispatcher,
    make_trigger_service,
)

HttpHandler = Callable[[httpx.Request], httpx.Response | None]

_ADAPTER_CLASSES: dict[str, type[NetworkChannelAdapter]] = {
    "slack": SlackChannelAdapter,
    "mattermost": MattermostChannelAdapter,
    "whatsapp": WhatsAppChannelAdapter,
}


@dataclass
class NetworkHarness:
    """A network Channel adapter on an in-memory platform server."""

    adapter: Any
    trigger: AsyncMock
    reserve_waiting_work: Mock
    requests: list[httpx.Request]

    async def drain(self, chat: str = "C1") -> None:
        """Wait until the chat's admitted work was processed."""
        await drain(self.adapter._engine, chat)

    def posted_texts(self) -> list[str]:
        """Return the message texts posted through Slack or Mattermost, in order."""
        texts: list[str] = []
        for request in self.requests:
            if request.url.path.endswith("/chat.postMessage"):
                texts.append(json.loads(request.content)["text"])
            elif request.url.path.endswith("/posts"):
                texts.append(json.loads(request.content)["message"])
        return texts


def platform_response(request: httpx.Request) -> httpx.Response:
    """Answer like a healthy Slack or Mattermost server where C1 is a DM with U1."""
    if request.url.host == "slack.com":
        data: dict[str, Any] = {"ok": True}
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "conversations.info":
            data["channel"] = {"is_im": True, "user": "U1"}
        if method == "files.getUploadURLExternal":
            data.update(upload_url="https://files.slack.com/upload/test", file_id="F1")
        return httpx.Response(200, json=data)
    if request.url.host == "files.slack.com":
        return httpx.Response(200)
    if request.url.path.endswith("/channels/C1"):
        return httpx.Response(200, json={"type": "D", "name": "BOT__U1"})
    if request.url.path.endswith("/files"):
        return httpx.Response(200, json={"file_infos": [{"id": "F1"}]})
    return httpx.Response(201, json={"id": "P1"})


def make_adapter(
    tmp_path: Path,
    platform: str,
    *,
    allow: list[str] | None = None,
    http: HttpHandler | None = None,
    attachment_store: AttachmentStore | None = None,
    connected: bool = True,
) -> NetworkHarness:
    """Build a platform adapter whose HTTP calls reach ``http``, then ``platform_response``.

    WhatsApp talks only to its bridge pipe and gets no HTTP client.

    ``http`` may answer a request itself or return None for the default answer.
    Every triggered Run completes with the reply "reply". With ``connected`` the
    adapter starts in the state its connection loop establishes.
    """
    config = ChannelConfig(
        id=f"test-{platform}",
        platform=platform,
        agent_id="assistant",
        token_env_var="BOT" if platform != "whatsapp" else "",
        app_token_env_var="APP" if platform == "slack" else "",
        server_url="https://mattermost.example" if platform == "mattermost" else "",
        allowed_chat_ids=allow
        if allow is not None
        else ["self" if platform == "whatsapp" else "C1"],
    )
    config.validate()

    async def complete(_agent_id: str, _content: Any, session_id: str, **_kwargs: Any) -> Run:
        run = Run(run_id="run-test", agent_id="assistant", session_id=session_id)
        run.emit(ASSISTANT_OUTPUT_EVENT, {"message": {"content": "reply"}})
        run.mark_completed("reply")
        return run

    trigger = AsyncMock(side_effect=complete)
    trigger_service = make_trigger_service(trigger)
    adapter = _ADAPTER_CLASSES[platform](
        config,
        trigger_service,
        ChatSessionManager(tmp_path),
        lambda key: f"secret-{key}",
        attachment_store or AttachmentStore(tmp_path),
        command_dispatcher=make_command_dispatcher(),
        conversation_pointers=channel_state(tmp_path, config.id),
        received_messages=channel_state(tmp_path, config.id),
        access_registry=MemoryChannelAccessRegistry([]),
        state_dir=tmp_path / "channels" / config.id,
    )
    requests: list[httpx.Request] = []

    def route(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        answer = http(request) if http is not None else None
        return answer if answer is not None else platform_response(request)

    if platform != "whatsapp":
        # The adapters own their HTTP client; no public seam injects a transport.
        adapter._http_client = httpx.AsyncClient(transport=httpx.MockTransport(route))
    if connected:
        adapter._bot_id = "BOT"
        adapter._connected = True
    return NetworkHarness(
        adapter=adapter,
        trigger=trigger,
        reserve_waiting_work=trigger_service.reserve_waiting_work,
        requests=requests,
    )


def event(
    platform: str, *, text: str = "hello", chat: str = "C1", user: str = "U1", direct: bool = True
) -> dict[str, Any]:
    """Return one inbound Slack message event or Mattermost ``posted`` payload."""
    if platform == "slack":
        return {
            "type": "message",
            "channel": chat,
            "channel_type": "im" if direct else "channel",
            "user": user,
            "ts": "123.001",
            "text": text,
        }
    return {
        "post": json.dumps({"id": "POST1", "channel_id": chat, "user_id": user, "message": text}),
        "channel_type": "D" if direct else "O",
        "mentions": "[]",
    }
