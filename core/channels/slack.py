"""Slack Socket Mode transport. No public webhook or Slack-specific SDK required."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import httpx
from websockets.asyncio.client import connect

from core.channels._network_adapter import NetworkChannelAdapter
from core.channels.adapter import ConversationFacts, FileData
from core.channels.config import ChannelError
from core.utils.tls import shared_ssl_context


class SlackChannelAdapter(NetworkChannelAdapter):
    platform = "slack"
    platform_display_name = "Slack"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._token = self.credential(self._config.token_env_var)
        self._app_token = self.credential(self._config.app_token_env_var)

    async def api(
        self, method: str, payload: dict[str, Any], *, app_token: bool = False
    ) -> dict[str, Any]:
        result = await self.request(
            "POST",
            f"https://slack.com/api/{method}",
            json=payload,
            headers={"Authorization": f"Bearer {self._app_token if app_token else self._token}"},
        )
        if not isinstance(result, dict) or not result.get("ok"):
            code = (
                result.get("error", "invalid_response")
                if isinstance(result, dict)
                else "invalid_response"
            )
            # Error codes are protocol values; never echo arbitrary upstream content.
            safe_code = (
                code
                if isinstance(code, str) and code.replace("_", "").isalnum() and len(code) < 80
                else "invalid_response"
            )
            raise ChannelError(
                f"Slack request failed: {safe_code}",
                retryable=safe_code == "ratelimited",
            )
        return result

    async def _listen(self) -> None:
        identity = await self.api("auth.test", {})
        self._bot_id = identity["user_id"]
        connection = await self.api("apps.connections.open", {}, app_token=True)
        url = connection["url"]
        parts = urlsplit(url)
        if parts.scheme != "wss" or not (parts.hostname or "").endswith(".slack.com"):
            raise ChannelError("Slack returned an invalid Socket Mode address")
        async with connect(
            url,
            max_size=2_097_152,
            ping_interval=20,
            proxy=None,
            ssl=shared_ssl_context(),
        ) as socket:
            self._socket = socket
            self._connected = True
            try:
                async for encoded in socket:
                    event = json.loads(encoded)
                    if event.get("envelope_id"):
                        await socket.send(json.dumps({"envelope_id": event["envelope_id"]}))
                    if event.get("type") == "disconnect":
                        break
                    if event.get("type") == "events_api":
                        await self.handle_event(event.get("payload", {}).get("event", {}))
            finally:
                self._connected = False

    async def target_facts(self, target: str) -> ConversationFacts:
        if not target.isalnum():
            raise ChannelError("Slack target must be a channel or DM id")
        info = (await self.api("conversations.info", {"channel": target}))["channel"]
        return ConversationFacts(
            platform=self.platform,
            channel_id=self._config.id,
            chat_id=target,
            user_id=info.get("user") or target,
            kind="direct" if info.get("is_im") else "group",
            access_scope_id=target,
        )

    async def handle_event(self, event: dict[str, Any]) -> None:
        if event.get("type") != "message" or event.get("subtype") not in (None, "file_share"):
            return
        if event.get("bot_id") or not event.get("user") or event["user"] == self._bot_id:
            return
        chat = event.get("channel")
        if not isinstance(chat, str) or not isinstance(event.get("ts"), str):
            return
        text = event.get("text") or ""
        direct = event.get("channel_type") == "im"
        facts = ConversationFacts(
            platform=self.platform,
            channel_id=self._config.id,
            chat_id=chat,
            user_id=event["user"],
            user_display_name=event["user"],
            kind="direct" if direct else "group",
            access_scope_id=chat,
            message_id=event["ts"],
            thread_id=event.get("thread_ts"),
            mentioned_bot=f"<@{self._bot_id}>" in text,
        )
        await self.receive(
            facts,
            {
                "text": text,
                "quoted": bool(event.get("thread_ts")),
                "files": [
                    {
                        "id": f.get("id"),
                        "url": f.get("url_private_download") or f.get("url_private"),
                        "name": f.get("name"),
                        "size": f.get("size"),
                    }
                    for f in event.get("files", [])
                ],
            },
        )

    async def download(self, file: dict[str, Any]) -> bytes:
        url = file.get("url")
        if not url and file.get("id"):
            info = (await self.api("files.info", {"file": file["id"]}))["file"]
            url = info.get("url_private_download") or info.get("url_private")
        parts = urlsplit(url or "")
        if (
            parts.scheme != "https"
            or parts.hostname != "files.slack.com"
            or parts.username
            or parts.password
        ):
            raise ChannelError("Slack attachment has an invalid download address")
        return await self.read_download(str(url), {"Authorization": f"Bearer {self._token}"})

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
        for chunk in self.message_chunks(message):
            payload: dict[str, Any] = {
                "channel": platform_target,
                "text": chunk,
                "unfurl_links": False,
                "unfurl_media": False,
            }
            if thread_id:
                payload["thread_ts"] = thread_id
            await self._send_operation(self.api, "chat.postMessage", payload)
        for file in files or []:
            # Slack retired files.upload; use the external upload flow.
            upload = await self._send_operation(
                self.api,
                "files.getUploadURLExternal",
                {"filename": file.filename, "length": len(file.data)},
            )
            url = urlsplit(upload["upload_url"])
            if url.scheme != "https" or url.hostname != "files.slack.com":
                raise ChannelError("Slack returned an invalid upload address")
            await self._send_operation(self._upload_bytes, upload["upload_url"], file.data)
            complete: dict[str, Any] = {
                "files": [{"id": upload["file_id"], "title": file.filename}],
                "channel_id": platform_target,
            }
            if thread_id:
                complete["thread_ts"] = thread_id
            await self._send_operation(self.api, "files.completeUploadExternal", complete)

    async def _upload_bytes(self, url: str, data: bytes) -> None:
        try:
            response = await self._http.post(url, content=data)
        except httpx.RequestError:
            raise ChannelError("Slack upload could not be confirmed") from None
        self.check_response(response, retry_server_error=False)
