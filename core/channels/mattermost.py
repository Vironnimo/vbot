"""Mattermost bot transport using its official REST and WebSocket APIs."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from websockets.asyncio.client import connect

from core.channels._network_adapter import NetworkChannelAdapter
from core.channels.adapter import ConversationFacts, FileData
from core.channels.config import ChannelError
from core.utils.tls import shared_ssl_context


class MattermostChannelAdapter(NetworkChannelAdapter):
    platform = "mattermost"
    platform_display_name = "Mattermost"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._token = self.credential(self._config.token_env_var)
        self._base = self._config.server_url.rstrip("/") + "/api/v4"
        self._headers = {"Authorization": f"Bearer {self._token}"}
        self._username = ""

    async def api(self, method: str, path: str, **kwargs: Any) -> Any:
        return await self.request(method, self._base + path, headers=self._headers, **kwargs)

    async def _listen(self) -> None:
        identity = await self.api("GET", "/users/me")
        self._bot_id = identity["id"]
        self._username = identity["username"]
        parts = urlsplit(self._base + "/websocket")
        url = urlunsplit(
            ("wss" if parts.scheme == "https" else "ws", parts.netloc, parts.path, "", "")
        )
        async with connect(
            url,
            max_size=2_097_152,
            ping_interval=20,
            proxy=None,
            ssl=shared_ssl_context() if url.startswith("wss://") else None,
        ) as socket:
            self._socket = socket
            await socket.send(
                json.dumps(
                    {"seq": 1, "action": "authentication_challenge", "data": {"token": self._token}}
                )
            )
            try:
                async with asyncio.timeout(15):
                    while True:
                        event = json.loads(await socket.recv())
                        if event.get("seq_reply") == 1:
                            if event.get("status") != "OK":
                                raise ChannelError("Mattermost WebSocket authentication failed")
                            break
                self._connected = True
                async for encoded in socket:
                    event = json.loads(encoded)
                    if event.get("event") == "posted":
                        await self.handle_event(event.get("data", {}))
            finally:
                self._connected = False

    async def target_facts(self, target: str) -> ConversationFacts:
        if not target.isalnum():
            raise ChannelError("Mattermost target must be a channel id")
        channel = await self.api("GET", f"/channels/{quote(target, safe='')}")
        peer = target
        if channel.get("type") == "D":
            peer = next(
                (p for p in channel.get("name", "").split("__") if p != self._bot_id), target
            )
        return ConversationFacts(
            platform=self.platform,
            channel_id=self._config.id,
            chat_id=target,
            user_id=peer,
            kind="direct" if channel.get("type") == "D" else "group",
            access_scope_id=target,
        )

    async def handle_event(self, data: dict[str, Any]) -> None:
        post = data.get("post", {})
        if isinstance(post, str):
            post = json.loads(post)
        if (
            post.get("type")
            or not post.get("user_id")
            or post["user_id"] == self._bot_id
            or post.get("props", {}).get("from_bot")
        ):
            return
        chat = post.get("channel_id")
        if not isinstance(chat, str) or not isinstance(post.get("id"), str):
            return
        text = post.get("message") or ""
        mentions = data.get("mentions", [])
        if isinstance(mentions, str):
            mentions = json.loads(mentions)
        facts = ConversationFacts(
            platform=self.platform,
            channel_id=self._config.id,
            chat_id=chat,
            user_id=post["user_id"],
            user_display_name=data.get("sender_name") or post["user_id"],
            kind="direct" if data.get("channel_type") == "D" else "group",
            access_scope_id=chat,
            message_id=post["id"],
            thread_id=post.get("root_id") or None,
            mentioned_bot=self._bot_id in mentions,
        )
        await self.receive(
            facts,
            {
                "text": text,
                "display_name": data.get("channel_display_name"),
                "quoted": bool(post.get("root_id")),
                "files": [{"id": file_id} for file_id in post.get("file_ids", [])],
            },
        )

    async def build_media_blocks(self, raw_message: Any) -> Any:
        files = []
        for file in raw_message.get("files", []):
            info = await self.api("GET", f"/files/{quote(file['id'], safe='')}/info")
            files.append({"id": file["id"], "name": info.get("name"), "size": info.get("size")})
        return await super().build_media_blocks({**raw_message, "files": files})

    async def download(self, file: dict[str, Any]) -> bytes:
        return await self.read_download(
            f"{self._base}/files/{quote(file['id'], safe='')}", self._headers
        )

    async def send_text(
        self,
        platform_target: str,
        text: str,
        *,
        reply_to_message_id: str | None = None,
        thread_id: str | None = None,
    ) -> None:
        await self.send(text, platform_target, thread_id=thread_id or reply_to_message_id)

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
        self.remember(await self.target_facts(platform_target))
        file_ids: list[str] = []
        for file in files or []:
            upload = await self._send_operation(
                self.api,
                "POST",
                "/files",
                data={"channel_id": platform_target},
                files={"files": (file.filename, file.data, file.media_type)},
            )
            file_ids.extend(info["id"] for info in upload["file_infos"])
        chunks = self.message_chunks(message) or [""]
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"channel_id": platform_target, "message": chunk}
            if thread_id:
                payload["root_id"] = thread_id
            if index == 0 and file_ids:
                payload["file_ids"] = file_ids
            await self._send_operation(self.api, "POST", "/posts", json=payload)
