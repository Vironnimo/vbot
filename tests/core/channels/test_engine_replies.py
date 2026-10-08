"""Channel engine: Run outcome replies, reply targets and reply-delivery retries."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import core.channels._conversation_content as content_module
import core.channels.engine as engine_module
from core.channels import ChannelError
from core.runs import ASSISTANT_OUTPUT_EVENT, USER_MESSAGE_EVENT, Run
from core.sessions import SessionAddress

from .engine_test_support import (
    SESSION_ID,
    FakeTransport,
    drain,
    make_cancelled_run,
    make_completed_run,
    make_conversation,
    make_empty_completed_run,
    make_engine,
    make_failed_run,
    make_interrupted_run,
)


def _recovered_run() -> Run:
    run = Run(run_id="run-recovered", agent_id="assistant", session_id=SESSION_ID)
    run.emit(
        ASSISTANT_OUTPUT_EVENT,
        {"message": {"content": "preserved partial", "interrupted": True}},
    )
    run.emit(ASSISTANT_OUTPUT_EVENT, {"message": {"content": " continuation"}})
    run.mark_completed("ok")
    return run


_STEERED_INPUT: dict[str, Any] = {}


def _ended_after(end: str, *messages: dict[str, Any]) -> Callable[[], Run]:
    """A Run that emitted ``messages`` (``_STEERED_INPUT`` is a user input) and then ended."""

    def make() -> Run:
        run = Run(run_id=f"run-{end}", agent_id="assistant", session_id=SESSION_ID)
        for message in messages:
            if message is _STEERED_INPUT:
                run.emit(USER_MESSAGE_EVENT, {})
            else:
                run.emit(ASSISTANT_OUTPUT_EVENT, {"message": message})
        if end == "failed":
            run.mark_failed(RuntimeError("could not record the Run's end"))
        else:
            run.mark_cancelled()
        return run

    return make


def _cancelled_after(*messages: dict[str, Any]) -> Callable[[], Run]:
    return _ended_after("cancelled", *messages)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make_run", "reply"),
    [
        (lambda: make_completed_run(output_text="final reply"), "final reply"),
        (make_empty_completed_run, content_module._EMPTY_ASSISTANT_REPLY),
        # The failure text stays internal.
        (lambda: make_failed_run(message="boom"), engine_module._FAILED_REPLY),
        # A failure after the complete answer still delivers it.
        (_ended_after("failed", {"content": "final reply"}), "final reply"),
        (make_cancelled_run, content_module._CANCELLED_REPLY),
        # Stop after a complete answer (post-answer Compaction) delivers that answer.
        (_cancelled_after({"content": "final reply"}), "final reply"),
        # Text before Tool calls or an interrupted partial is no answer.
        (
            _cancelled_after(
                {"content": "final reply"},
                {"content": "let me check", "tool_calls": [{"id": "call-1"}]},
            ),
            content_module._CANCELLED_REPLY,
        ),
        (
            _cancelled_after({"content": "partial", "interrupted": True}),
            content_module._CANCELLED_REPLY,
        ),
        # An answer given before a steered input does not answer that input.
        (
            _cancelled_after({"content": "first answer"}, _STEERED_INPUT),
            content_module._CANCELLED_REPLY,
        ),
        (lambda: make_interrupted_run(output_text="preserved partial"), "preserved partial"),
        # A recovered Run forwards partial and continuation without added text.
        (_recovered_run, "preserved partial continuation"),
    ],
    ids=[
        "completed",
        "empty",
        "failed",
        "failed-after-answer",
        "cancelled",
        "cancelled-after-answer",
        "cancelled-in-tool-turn",
        "cancelled-mid-answer",
        "cancelled-after-steered-input",
        "interrupted",
        "recovered",
    ],
)
async def test_each_run_outcome_becomes_one_reply(
    tmp_path: Path, make_run: Callable[[], Run], reply: str
) -> None:
    trigger_mock = AsyncMock(return_value=make_run())
    engine, _sessions, _trigger, transport = make_engine(tmp_path, trigger_run=trigger_mock)

    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)

    assert transport.sent == [("12345", reply)]
    assert transport.activity_targets == ["12345"]
    await engine.stop()


@pytest.mark.asyncio
async def test_trigger_exception_sends_failure_without_leaking_internals(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    trigger_mock = AsyncMock(side_effect=RuntimeError("internal stack trace"))
    engine, _sessions, _trigger, transport = make_engine(tmp_path, trigger_run=trigger_mock)
    caplog.set_level(logging.ERROR, logger="vbot.channels.engine")

    await engine.handle_inbound_text(make_conversation(), "hello")
    await drain(engine, 12345)

    assert transport.sent_texts == [engine_module._FAILED_REPLY]
    records = [r for r in caplog.records if r.message.startswith("Channel trigger run failed")]
    assert len(records) == 1
    assert records[0].exc_info is not None
    await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(("kind", "reply_target"), [("group", "777"), ("direct", None)])
async def test_only_group_replies_reference_the_triggering_message(
    tmp_path: Path, kind: str, reply_target: str | None
) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, trigger_run=trigger_mock, response_mode="all"
    )

    await engine.handle_inbound_text(make_conversation(kind=kind, message_id="777"), "hello")
    await drain(engine, 12345)

    assert transport.sent == [("12345", "ok")]
    assert transport.sent_reply_targets == [reply_target]
    await engine.stop()


@pytest.mark.asyncio
async def test_topic_message_reply_carries_thread_everywhere(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, transport = make_engine(
        tmp_path, trigger_run=trigger_mock, response_mode="all"
    )
    address = SessionAddress(project_id=None, agent_id="assistant", session_id=SESSION_ID)

    await engine.handle_inbound_text(
        make_conversation(kind="group", message_id="777", thread_id="42"),
        "hello",
    )
    await drain(engine, 12345)

    # Reply text, activity indicator, and the reply-target metadata all carry the topic.
    assert transport.sent == [("12345", "ok")]
    assert transport.sent_thread_ids == ["42"]
    assert transport.activity_thread_ids == ["42"]
    assert chat_sessions.get_metadata(address)["last_reply_target"] == {
        "channel_id": "tg-assistant",
        "platform_target": "12345",
        "thread_id": "42",
    }

    # A later non-topic message rewrites the reply target without the thread key.
    await engine.handle_inbound_text(make_conversation(kind="group", mentioned_bot=True), "hi")
    await drain(engine, 12345)
    assert "thread_id" not in chat_sessions.get_metadata(address)["last_reply_target"]
    await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("send_errors", "attempts", "logged"),
    [
        ([ChannelError("network blip", retryable=True)], 2, None),
        ([ChannelError("chat not found")], 1, "Channel reply lost"),
        # One attempt plus the shared maximum of three retries.
        ([ChannelError("still down", retryable=True)] * 4, 4, "Channel reply lost"),
        # Earlier chunks reached the chat: the reply is incomplete, not lost.
        (
            [ChannelError("chunk 2 failed", possibly_delivered=True)],
            1,
            "Channel reply incomplete",
        ),
    ],
    ids=["transient-failure-retried", "permanent-failure", "retries-exhausted", "partly-sent"],
)
async def test_reply_delivery_retries_only_transient_failures_and_logs_lost_replies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    send_errors: list[ChannelError],
    attempts: int,
    logged: str | None,
) -> None:
    transport = FakeTransport()
    deliver = transport.send_text
    errors = iter(send_errors)
    calls = 0

    async def flaky_send(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        error = next(errors, None)
        if error is not None:
            raise error
        await deliver(*args, **kwargs)

    monkeypatch.setattr(transport, "send_text", flaky_send)
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="final answer"))
    engine, _sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, transport=transport
    )

    with caplog.at_level(logging.ERROR, logger="vbot.channels.engine"):
        await engine.handle_inbound_text(make_conversation(), "hello")
        await drain(engine, 12345)
    await engine.stop()

    assert calls == attempts
    assert transport.sent_texts == (["final answer"] if logged is None else [])
    failed = [record for record in caplog.records if "Channel reply" in record.getMessage()]
    assert [(record.levelno, record.getMessage().startswith(str(logged))) for record in failed] == (
        [] if logged is None else [(logging.ERROR, True)]
    )
