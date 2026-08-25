"""Reply-delivery retries: transient send failures must not silently lose answers."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from core.channels.adapter import ReplyPlanFacts
from core.channels.channels import ChannelError

from .engine_test_support import FakeTransport, make_engine


def make_reply_plan(**overrides: Any) -> ReplyPlanFacts:
    facts: dict[str, Any] = {
        "channel_id": "tg-assistant",
        "platform_target": "12345",
        "reply_to_message_id": None,
        "thread_id": None,
    }
    facts.update(overrides)
    return ReplyPlanFacts(**facts)


def kill_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove backoff waits so retry loops run at test speed."""

    async def instant_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("core.utils.retry.asyncio.sleep", instant_sleep)


@pytest.mark.asyncio
async def test_send_reply_retries_transient_failure_and_delivers(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kill_retry_sleep(monkeypatch)
    transport = FakeTransport()
    engine, _sessions, _trigger, transport_ref = make_engine(
        tmp_path,
        transport=transport,
    )
    del transport_ref
    original_send = transport.send_text
    attempts = {"count": 0}

    async def flaky_send(*args: Any, **kwargs: Any) -> None:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ChannelError("network blip", retryable=True)
        await original_send(*args, **kwargs)

    transport.send_text = flaky_send  # type: ignore[method-assign]

    await engine._send_reply(make_reply_plan(), "final answer")

    assert transport.sent_texts == ["final answer"]
    assert attempts["count"] == 2


@pytest.mark.asyncio
async def test_send_reply_never_retries_permanent_failure(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    kill_retry_sleep(monkeypatch)
    transport = FakeTransport()
    engine, _sessions, _trigger, _transport_ref = make_engine(
        tmp_path,
        transport=transport,
    )
    attempts = {"count": 0}

    async def refusing_send(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        attempts["count"] += 1
        raise ChannelError("chat not found")

    transport.send_text = refusing_send  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger="channels.engine"):
        await engine._send_reply(make_reply_plan(), "answer")

    assert attempts["count"] == 1
    assert any("Channel reply lost" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_send_reply_logs_lost_answer_after_exhausted_retries(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    kill_retry_sleep(monkeypatch)
    transport = FakeTransport()
    engine, _sessions, _trigger, _transport_ref = make_engine(
        tmp_path,
        transport=transport,
    )
    attempts = {"count": 0}

    async def transient_but_stuck_send(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        attempts["count"] += 1
        raise ChannelError("still down", retryable=True)

    transport.send_text = transient_but_stuck_send  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR, logger="channels.engine"):
        await engine._send_reply(make_reply_plan(), "answer")

    # One initial attempt plus the shared maximum of three retries.
    assert attempts["count"] == 4
    assert transport.sent_texts == []
    lost_records = [
        record for record in caplog.records if "Channel reply lost" in record.getMessage()
    ]
    assert len(lost_records) == 1
    assert lost_records[0].levelno == logging.ERROR
