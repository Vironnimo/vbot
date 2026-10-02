"""Slack and Mattermost: outbound wire contracts, chunking and retry safety."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from core.channels.adapter import FileData
from core.channels.config import ChannelError

from .network_test_support import HttpHandler, make_adapter

pytestmark = pytest.mark.usefixtures("current_format_data_directory")


def _is_message_post(request: httpx.Request) -> bool:
    return request.url.path.endswith(("/chat.postMessage", "/posts"))


def _message_text(request: httpx.Request) -> str:
    payload = json.loads(request.content)
    return str(payload.get("text", payload.get("message")))


@pytest.mark.asyncio
async def test_slack_upload_flow_threads_and_auth(tmp_path: Path) -> None:
    def slack(request: httpx.Request) -> None:
        if request.url.host == "files.slack.com":
            # The pre-signed upload URL must never receive the bot token.
            assert "authorization" not in request.headers
            assert request.content == b"payload"
        else:
            assert request.headers["authorization"] == "Bearer secret-BOT"

    h = make_adapter(tmp_path, "slack", http=slack)
    try:
        await h.adapter.send(
            "hello", "C1", thread_id="111.2", files=[FileData("test.txt", "text/plain", b"payload")]
        )
        assert [r.url.path for r in h.requests] == [
            "/api/conversations.info",
            "/api/chat.postMessage",
            "/api/files.getUploadURLExternal",
            "/upload/test",
            "/api/files.completeUploadExternal",
        ]
        assert json.loads(h.requests[1].content)["thread_ts"] == "111.2"
        assert json.loads(h.requests[-1].content)["thread_ts"] == "111.2"
    finally:
        await h.adapter.stop()


@pytest.mark.asyncio
async def test_mattermost_upload_and_thread(tmp_path: Path) -> None:
    def mattermost(request: httpx.Request) -> None:
        assert request.headers["authorization"] == "Bearer secret-BOT"

    h = make_adapter(tmp_path, "mattermost", http=mattermost)
    try:
        await h.adapter.send(
            "hello", "C1", files=[FileData("note.txt", "text/plain", b"payload")], thread_id="ROOT"
        )
        assert json.loads(h.requests[-1].content) == {
            "channel_id": "C1",
            "message": "hello",
            "root_id": "ROOT",
            "file_ids": ["F1"],
        }
        assert b"payload" in h.requests[1].content
    finally:
        await h.adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "exhausted"),
    [("slack", False), ("mattermost", True)],
    ids=["slack-recovers", "mattermost-exhausts"],
)
async def test_chunk_retry_never_replays_delivered_text(
    tmp_path: Path, platform: str, exhausted: bool
) -> None:
    def rate_limit_tail(request: httpx.Request) -> httpx.Response | None:
        failing = [r for r in h.requests if _is_message_post(r) and _message_text(r) == "tail"]
        if _is_message_post(request) and failing and (exhausted or len(failing) == 1):
            return httpx.Response(429, headers={"retry-after": "0"})
        return None

    h = make_adapter(tmp_path, platform, http=rate_limit_tail)

    def attempts() -> list[str]:
        return [_message_text(r) for r in h.requests if _is_message_post(r)]

    try:
        if exhausted:
            with pytest.raises(ChannelError) as error:
                await h.adapter.send_text("C1", "x" * 3500 + "tail")
            # Once one chunk exhausts its retries, resending the message would duplicate.
            assert not error.value.retryable
            assert attempts() == ["x" * 3500] + ["tail"] * 4
        else:
            await h.adapter.send_text("C1", "x" * 3500 + "tail")
            assert attempts() == ["x" * 3500, "tail", "tail"]
    finally:
        await h.adapter.stop()


@pytest.mark.asyncio
async def test_long_markdown_is_split_without_breaking_code_fences(tmp_path: Path) -> None:
    h = make_adapter(tmp_path, "slack")
    code = "\n".join(f"print({index})" for index in range(600))
    try:
        await h.adapter.send_text("C1", f"Intro\n\n```python\n{code}\n```\n\nOutro")
    finally:
        await h.adapter.stop()

    delivered = h.posted_texts()
    assert len(delivered) > 1
    assert all(len(chunk) <= 3500 for chunk in delivered)
    assert all(chunk.startswith("```python\n") for chunk in delivered[1:])
    assert all(chunk.endswith("\n```") for chunk in delivered[:-1])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failing_step", "status", "uploads", "completions"),
    [
        ("upload", 429, 2, 1),
        ("upload", 500, 1, 0),
        ("files.completeUploadExternal", 500, 1, 1),
    ],
    ids=["upload-rate-limited", "upload-server-error", "complete-server-error"],
)
async def test_slack_file_failure_preserves_prior_delivery_and_upload(
    tmp_path: Path, failing_step: str, status: int, uploads: int, completions: int
) -> None:
    calls: list[str] = []

    def fail_once(request: httpx.Request) -> httpx.Response | None:
        step = (
            "upload"
            if request.url.host == "files.slack.com"
            else request.url.path.rsplit("/", 1)[-1]
        )
        calls.append(step)
        if step == failing_step and calls.count(step) == 1:
            return httpx.Response(status)
        return None

    h = make_adapter(tmp_path, "slack", http=fail_once)
    try:
        delivery = h.adapter.send("caption", "C1", files=[FileData("a.txt", "text/plain", b"a")])
        if status == 500:
            # A server error after a write may hide a delivered part: never retried.
            with pytest.raises(ChannelError) as error:
                await delivery
            assert not error.value.retryable
        else:
            await delivery
        assert calls.count("chat.postMessage") == 1
        assert calls.count("files.getUploadURLExternal") == 1
        assert calls.count("upload") == uploads
        assert calls.count("files.completeUploadExternal") == completions
    finally:
        await h.adapter.stop()


def _rate_limited(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(429, headers={"retry-after": "7"})


def _rate_limited_for_a_fraction(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(429, headers={"retry-after": "1.5"})


def _connection_lost(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadError("secret-url?token=credential", request=request)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server", "retryable", "retry_after"),
    [
        (_rate_limited, True, 7),
        (_rate_limited_for_a_fraction, True, 1.5),
        (_connection_lost, False, None),
    ],
    ids=["rate-limited", "fractional-retry-after", "ambiguous-write"],
)
async def test_request_failure_keeps_retry_hint_without_request_details(
    tmp_path: Path,
    server: HttpHandler,
    retryable: bool,
    retry_after: float | None,
) -> None:
    h = make_adapter(tmp_path, "slack", http=server)
    try:
        with pytest.raises(ChannelError) as error:
            await h.adapter.api("chat.postMessage", {})
        assert (error.value.retryable, error.value.retry_after) == (retryable, retry_after)
        # Request URLs and tokens never reach the error text or its chained cause.
        assert "secret" not in str(error.value) and "credential" not in str(error.value)
        assert error.value.__cause__ is None
        assert error.value.__context__ is None or error.value.__suppress_context__
    finally:
        await h.adapter.stop()
