"""Wire contracts, routing and resource bounds for the new Channels."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from core.attachments import AttachmentStore, AttachmentTooLargeError
from core.channels.adapter import FileData
from core.channels.config import ChannelConfig, ChannelConfigError, ChannelError
from core.channels.mattermost import MattermostChannelAdapter
from core.channels.slack import SlackChannelAdapter
from core.channels.whatsapp import WhatsAppChannelAdapter
from core.runs import ASSISTANT_OUTPUT_EVENT, Run, WaitingWorkAdmission
from core.sessions import ChatSessionManager
from tests.core.channels.discord_helpers import make_command_dispatcher
from tests.core.channels.engine_test_support import MemoryChannelAccessRegistry, channel_state

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


def make_adapter(tmp_path: Path, platform: str, *, allow: list[str] | None = None) -> Any:
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

    async def trigger(*args: Any, **kwargs: Any) -> Run:
        run = Run(
            run_id="run-test", agent_id="assistant", session_id=kwargs.get("session_id", "test")
        )
        run.emit(ASSISTANT_OUTPUT_EVENT, {"message": {"content": "reply"}})
        run.mark_completed("reply")
        return run

    service = SimpleNamespace(
        trigger_run=AsyncMock(side_effect=trigger),
        reserve_waiting_work=Mock(
            return_value=WaitingWorkAdmission(id="admission-test", scope="test")
        ),
        release_waiting_work=Mock(return_value=True),
    )
    classes: dict[str, Any] = {
        "slack": SlackChannelAdapter,
        "mattermost": MattermostChannelAdapter,
        "whatsapp": WhatsAppChannelAdapter,
    }
    adapter = classes[platform](
        config,
        service,
        ChatSessionManager(tmp_path),
        lambda key: f"secret-{key}",
        AttachmentStore(tmp_path),
        command_dispatcher=make_command_dispatcher(),
        conversation_pointers=channel_state(tmp_path, config.id),
        received_messages=channel_state(tmp_path, config.id),
        access_registry=MemoryChannelAccessRegistry([]),
        state_dir=tmp_path / "channels" / config.id,
    )
    adapter._bot_id = "BOT"
    adapter._connected = True
    adapter.trigger = service.trigger_run
    return adapter


def event(
    platform: str, *, text: str = "hello", chat: str = "C1", user: str = "U1", direct: bool = True
) -> dict[str, Any]:
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


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["slack", "mattermost"])
async def test_inbound_uses_real_engine_and_persists_dedup(tmp_path: Path, platform: str) -> None:
    adapter = make_adapter(tmp_path, platform)
    adapter.send_text = AsyncMock()
    incoming = event(platform)
    try:
        await adapter.handle_event(incoming)
        await adapter._engine._chat_queues["C1"].join()
        adapter.trigger.assert_awaited_once()
        adapter.send_text.assert_awaited_once()
        await adapter.handle_event(incoming)
        adapter.trigger.assert_awaited_once()
    finally:
        await adapter.stop()
    # A restarted adapter still recognizes the redelivered event.
    restarted = make_adapter(tmp_path, platform)
    try:
        await restarted.handle_event(incoming)
        restarted.trigger.assert_not_awaited()
    finally:
        await restarted.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["slack", "mattermost"])
async def test_unauthorized_and_bot_events_never_reach_engine(
    tmp_path: Path, platform: str
) -> None:
    adapter = make_adapter(tmp_path, platform, allow=[])
    adapter._engine.handle_inbound_text = AsyncMock()
    try:
        await adapter.handle_event(event(platform, user="BOT"))
        assert adapter.denied_chats() == []
        await adapter.handle_event(event(platform))
        assert adapter.denied_chats()[0].chat_id == "C1"
        adapter._engine.handle_inbound_text.assert_not_awaited()
    finally:
        await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["slack", "mattermost"])
async def test_unaddressed_group_does_not_trigger(tmp_path: Path, platform: str) -> None:
    adapter = make_adapter(tmp_path, platform)
    try:
        await adapter.handle_event(event(platform, direct=False))
        adapter.trigger.assert_not_awaited()
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_slack_upload_flow_threads_and_auth(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path, "slack")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "files.slack.com":
            assert "authorization" not in request.headers
            assert request.content == b"payload"
            return httpx.Response(200)
        assert request.headers["authorization"] == "Bearer secret-BOT"
        method = request.url.path.split("/")[-1]
        data: dict[str, Any] = {"ok": True}
        if method == "conversations.info":
            data["channel"] = {"is_im": True, "user": "U1"}
        if method == "files.getUploadURLExternal":
            data.update(upload_url="https://files.slack.com/upload/test", file_id="F1")
        return httpx.Response(200, json=data)

    adapter._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        await adapter.send(
            "hello", "C1", thread_id="111.2", files=[FileData("test.txt", "text/plain", b"payload")]
        )
        assert [r.url.path for r in requests] == [
            "/api/conversations.info",
            "/api/chat.postMessage",
            "/api/files.getUploadURLExternal",
            "/upload/test",
            "/api/files.completeUploadExternal",
        ]
        assert json.loads(requests[1].content)["thread_ts"] == "111.2"
        assert json.loads(requests[-1].content)["thread_ts"] == "111.2"
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_mattermost_upload_and_thread(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path, "mattermost")
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer secret-BOT"
        if request.url.path.endswith("/channels/C1"):
            return httpx.Response(200, json={"type": "D", "name": "BOT__U1"})
        if request.url.path.endswith("/files"):
            return httpx.Response(200, json={"file_infos": [{"id": "F1"}]})
        return httpx.Response(201, json={"id": "P1"})

    adapter._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        await adapter.send(
            "hello", "C1", files=[FileData("note.txt", "text/plain", b"payload")], thread_id="ROOT"
        )
        assert json.loads(requests[-1].content) == {
            "channel_id": "C1",
            "message": "hello",
            "root_id": "ROOT",
            "file_ids": ["F1"],
        }
        assert b"payload" in requests[1].content
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_slack_download_cannot_leak_token_and_is_bounded(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path, "slack")
    adapter._attachment_store = AttachmentStore(tmp_path, max_size_bytes=3)
    calls = Mock()

    def handle(request: httpx.Request) -> httpx.Response:
        calls(request)
        return httpx.Response(200, content=b"1234")

    adapter._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ChannelError):
            await adapter.download({"url": "https://evil.test/private"})
        calls.assert_not_called()
        with pytest.raises(AttachmentTooLargeError):
            await adapter.download({"url": "https://files.slack.com/private"})
    finally:
        await adapter.stop()


@pytest.mark.asyncio
async def test_whatsapp_self_target_and_media(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path, "whatsapp")
    adapter.call_bridge = AsyncMock(return_value={"ok": True})
    try:
        with pytest.raises(ChannelError):
            await adapter.send("hello", "123@s.whatsapp.net")
        adapter.call_bridge.assert_not_awaited()
        await adapter.send("hello", "self", files=[FileData("note.txt", "text/plain", b"test")])
        assert adapter.call_bridge.await_args_list[0].args[0]["target"] == "self"
        assert adapter.call_bridge.await_args_list[1].args[0]["file"]["data"] == "dGVzdA=="
    finally:
        await adapter.stop()


def test_whatsapp_bridge_gate_real_javascript() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the WhatsApp bridge tests")
    script = Path(__file__).with_name("whatsapp_gate.test.mjs")
    completed = subprocess.run(
        [node, "--test", str(script)], capture_output=True, text=True, timeout=30
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    bridge = Path(__file__).parents[3] / "core/channels/whatsapp_bridge/bridge.js"
    parsed = subprocess.run(
        [node, "--check", str(bridge)], capture_output=True, text=True, timeout=30
    )
    assert parsed.returncode == 0, parsed.stderr


@pytest.mark.asyncio
async def test_failed_attachment_preserves_caption_and_other_file(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path, "slack")
    adapter.send_text = AsyncMock()
    adapter.download = AsyncMock(return_value=b"small text file")
    incoming = event("slack", text="keep this caption")
    incoming["files"] = [
        {"id": "BIG", "name": "big.txt", "size": 99999999999},
        {"id": "SMALL", "name": "small.txt", "size": 15},
    ]
    try:
        await adapter.handle_event(incoming)
        await adapter._engine._chat_queues["C1"].join()
        adapter.trigger.assert_awaited_once()
        content = adapter.trigger.await_args.args[1]
        assert content[0].text == "keep this caption"
        assert len(content) == 2
        adapter.download.assert_awaited_once()
    finally:
        await adapter.stop()


@pytest.mark.parametrize("updates", [{"allowed_chat_ids": ["someone"]}, {"token_env_var": "TOKEN"}])
def test_whatsapp_config_rejects_unsupported_account_scope(updates: dict[str, Any]) -> None:
    with pytest.raises(ChannelConfigError):
        ChannelConfig.from_dict(
            {"id": "wa", "platform": "whatsapp", "agent_id": "assistant", **updates}
        )


@pytest.mark.asyncio
async def test_rate_limit_keeps_retry_hint(tmp_path: Path) -> None:
    adapter = make_adapter(tmp_path, "slack")
    adapter._http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(429, headers={"retry-after": "7"})
        )
    )
    try:
        with pytest.raises(ChannelError) as error:
            await adapter.api("chat.postMessage", {})
        assert error.value.retryable and error.value.retry_after == 7
        assert "secret" not in str(error.value)
    finally:
        await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["slack", "mattermost"])
@pytest.mark.parametrize("exhausted", [False, True])
async def test_chunk_retry_never_replays_delivered_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str, exhausted: bool
) -> None:
    adapter = make_adapter(tmp_path, platform)
    monkeypatch.setattr("core.utils.retry._sleep", AsyncMock())
    delivered: list[str] = []
    attempts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("conversations.info"):
            return httpx.Response(200, json={"ok": True, "channel": {"is_im": True}})
        if request.url.path.endswith("/channels/C1"):
            return httpx.Response(200, json={"type": "D"})
        payload = json.loads(request.content)
        chunk = payload["text" if platform == "slack" else "message"]
        attempts.append(chunk)
        if chunk == "tail" and (exhausted or attempts.count("tail") == 1):
            return httpx.Response(429, headers={"retry-after": "0"})
        delivered.append(chunk)
        return httpx.Response(200, json={"ok": True})

    adapter._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        if exhausted:
            with pytest.raises(ChannelError) as error:
                await adapter.send_text("C1", "x" * 3500 + "tail")
            assert not error.value.retryable
            assert delivered == ["x" * 3500]
            assert attempts.count("tail") == 4
        else:
            await adapter.send_text("C1", "x" * 3500 + "tail")
            assert delivered == ["x" * 3500, "tail"]
            assert attempts.count("tail") == 2
    finally:
        await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["slack", "mattermost"])
async def test_long_markdown_is_split_without_breaking_code_fences(
    tmp_path: Path, platform: str
) -> None:
    adapter = make_adapter(tmp_path, platform)
    delivered: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("conversations.info"):
            return httpx.Response(200, json={"ok": True, "channel": {"is_im": True}})
        if request.url.path.endswith("/channels/C1"):
            return httpx.Response(200, json={"type": "D"})
        delivered.append(json.loads(request.content)["text" if platform == "slack" else "message"])
        return httpx.Response(200, json={"ok": True})

    adapter._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    code = "\n".join(f"print({index})" for index in range(600))
    try:
        await adapter.send_text("C1", f"Intro\n\n```python\n{code}\n```\n\nOutro")
    finally:
        await adapter.stop()

    assert len(delivered) > 1
    assert all(len(chunk) <= 3500 for chunk in delivered)
    assert all(chunk.startswith("```python\n") for chunk in delivered[1:])
    assert all(chunk.endswith("\n```") for chunk in delivered[:-1])


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["upload", "complete"])
@pytest.mark.parametrize("status", [429, 500])
async def test_slack_file_failure_preserves_prior_delivery_and_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, status: int
) -> None:
    adapter = make_adapter(tmp_path, "slack")
    monkeypatch.setattr("core.utils.retry._sleep", AsyncMock())
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append(method)
        if method == "conversations.info":
            return httpx.Response(200, json={"ok": True, "channel": {"is_im": True}})
        if method == "files.getUploadURLExternal":
            return httpx.Response(
                200,
                json={"ok": True, "upload_url": "https://files.slack.com/upload", "file_id": "F1"},
            )
        failing_method = "upload" if stage == "upload" else "files.completeUploadExternal"
        if method == failing_method and calls.count(method) == 1:
            return httpx.Response(status)
        return httpx.Response(200, json={"ok": True})

    adapter._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        delivery = adapter.send("caption", "C1", files=[FileData("a.txt", "text/plain", b"a")])
        if status == 500:
            with pytest.raises(ChannelError) as error:
                await delivery
            assert not error.value.retryable
        else:
            await delivery
        assert calls.count("chat.postMessage") == 1
        assert calls.count("files.getUploadURLExternal") == 1
        assert calls.count("upload") == (2 if stage == "upload" and status == 429 else 1)
        assert calls.count("files.completeUploadExternal") == (
            0
            if stage == "upload" and status == 500
            else 2
            if stage == "complete" and status == 429
            else 1
        )
    finally:
        await adapter.stop()
