"""Channel engine: replies owed across adapter restarts and from an ended engine."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

import core.channels._conversation_content as content_module
import core.channels.engine as engine_module
from core.channels.adapter import RunButtonBinding, bound_run_callback_data
from core.channels.engine import ChannelConversationEngine
from core.chat import ChatMessage
from core.extensions import InteractionButton, InteractionEvent
from core.runs import ASSISTANT_OUTPUT_DELTA_EVENT, ASSISTANT_OUTPUT_EVENT, Run
from core.runs.run import DEFAULT_RUN_EVENT_RETENTION_LIMIT
from core.sessions import ChatSessionManager, SessionAddress
from core.sessions._types import SessionRunCompletion
from core.utils.timestamps import utc_now_timestamp

from .engine_test_support import (
    SESSION_ID,
    FakeTransport,
    channel_state,
    connect,
    drain,
    make_conversation,
    make_engine,
    settle_replies,
)

pytestmark = pytest.mark.usefixtures("current_format_data_directory")

_ADDRESS = SessionAddress(project_id=None, agent_id="assistant", session_id=SESSION_ID)


class _Runs:
    """Trigger double: every Run stays running until the test ends it."""

    def __init__(self, *, event_retention_limit: int = DEFAULT_RUN_EVENT_RETENTION_LIMIT) -> None:
        self.runs: list[Run] = []
        self.trigger = AsyncMock(side_effect=self._start)
        self.started = asyncio.Event()
        self._event_retention_limit = event_retention_limit

    async def _start(self, agent_id: str, _content: Any, session_id: str, **_kwargs: Any) -> Run:
        run = Run(
            run_id=f"run-{len(self.runs)}",
            agent_id=agent_id,
            session_id=session_id,
            event_retention_limit=self._event_retention_limit,
        )
        self.runs.append(run)
        self.started.set()
        return run


def _answer(run: Run, text: str) -> None:
    run.emit(ASSISTANT_OUTPUT_EVENT, {"message": {"content": text}})
    run.mark_completed(text)


async def _next_engine(
    tmp_path: Path, **kwargs: Any
) -> tuple[ChannelConversationEngine, ChatSessionManager, FakeTransport]:
    """Start the Channel's next engine, as the service does after the last one ended."""
    engine, sessions, _trigger, transport = make_engine(tmp_path, **kwargs)
    engine.start()
    await settle_replies(engine)
    return engine, sessions, transport


@pytest.mark.asyncio
async def test_an_answer_finished_while_the_adapter_restarts_reaches_the_next_connection(
    tmp_path: Path,
) -> None:
    runs = _Runs()
    engine, _sessions, _trigger, first = make_engine(tmp_path, trigger_run=runs.trigger)
    await engine.handle_inbound_text(make_conversation(), "first")
    await engine.handle_inbound_text(make_conversation(), "second")
    await asyncio.wait_for(runs.started.wait(), timeout=5)

    # The adapter stops; its Run answers while no adapter is connected.
    engine.set_connected(False)
    _answer(runs.runs[0], "first answer")
    for _ in range(50):  # the relay reaches its send and waits there
        await asyncio.sleep(0)
    assert first.sent == []
    second = FakeTransport()
    connect(engine, second)
    while len(runs.runs) < 2:
        await asyncio.sleep(0)
    _answer(runs.runs[1], "second answer")
    await drain(engine, 12345)
    await engine.stop()

    # The answer waited for the restarted adapter, and the queued message was
    # still processed.
    assert first.sent == []
    assert second.sent == [("12345", "first answer"), ("12345", "second answer")]
    assert channel_state(tmp_path).take_pending_replies("tg-assistant", "next") == ([], 0)


def _record_history(
    sessions: ChatSessionManager, run_id: str, output: str | None, ending: str | None
) -> None:
    """Write what the Run left in its Session; ``ending`` is ``status`` or ``status:reason``."""
    session = sessions.get(_ADDRESS)
    if output is not None:
        session.for_run(run_id).append(
            ChatMessage.assistant(model="model", content=output, interrupted=ending != "completed")
        )
    if ending is not None:
        status, _, reason = ending.partition(":")
        now = utc_now_timestamp()
        session._store.finish_run(
            _ADDRESS,
            SessionRunCompletion(
                run_id=run_id,
                status=status,
                timing={"started_at": now, "completed_at": now, "duration_ms": 0},
                iteration_count=1,
                completion_reason=reason or None,
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output", "ending", "reply"),
    [
        # vBot shut down mid-answer: the chat gets the partial answer the Run kept.
        ("partial answer", "cancelled:shutdown", "partial answer"),
        # The process ended before the Run's end was recorded.
        ("partial answer", None, "partial answer"),
        (None, None, content_module._INTERRUPTED_REPLY),
        ("full answer", "completed", "full answer"),
        (None, "cancelled:user", content_module._CANCELLED_REPLY),
    ],
    ids=["shutdown-partial", "crash-partial", "crash-nothing", "completed", "cancelled"],
)
async def test_a_run_answer_an_ended_engine_owed_is_sent_once_from_session_history(
    tmp_path: Path, output: str | None, ending: str | None, reply: str
) -> None:
    runs = _Runs()
    engine, sessions, _trigger, first = make_engine(tmp_path, trigger_run=runs.trigger)
    await engine.handle_inbound_text(make_conversation(), "hello")
    await asyncio.wait_for(runs.started.wait(), timeout=5)
    await engine.stop()
    run_id = runs.runs[0].id
    sessions.get(_ADDRESS).start_run(run_id)
    _record_history(sessions, run_id, output, ending)

    _next, _sessions, transport = await _next_engine(tmp_path)
    _later, _sessions, later = await _next_engine(tmp_path)

    assert first.sent == []
    assert transport.sent == [("12345", reply)]
    assert later.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("ends_before_subscription", [False, True], ids=["running", "ended"])
async def test_a_run_still_running_when_its_engine_ended_is_relayed_by_the_next(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ends_before_subscription: bool
) -> None:
    runs = _Runs(event_retention_limit=4)
    engine, sessions, _trigger, first = make_engine(tmp_path, trigger_run=runs.trigger)
    await engine.handle_inbound_text(make_conversation(), "hello")
    await asyncio.wait_for(runs.started.wait(), timeout=5)
    await engine.stop()
    run = runs.runs[0]
    sessions.get(_ADDRESS).start_run(run.id)
    _record_history(sessions, run.id, "preserved partial", None)
    run.emit(
        ASSISTANT_OUTPUT_EVENT,
        {"message": {"content": "preserved partial", "interrupted": True}},
    )
    # The next engine's replay begins after the preserved answer fragment.
    for _ in range(5):
        run.emit(ASSISTANT_OUTPUT_DELTA_EVENT, {"content_delta": "x"})

    def finish() -> None:
        _record_history(sessions, run.id, " continuation", "completed")
        _answer(run, " continuation")

    transport = FakeTransport()
    if ends_before_subscription:

        @contextlib.asynccontextmanager
        async def activity(_target: str, _thread: str | None = None) -> AsyncIterator[None]:
            # The Run was still running at lookup, but finished while the
            # transport established its activity indicator, before subscribe.
            finish()
            yield

        monkeypatch.setattr(transport, "activity_indicator", activity)

    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, running_run=run, transport=transport
    )
    engine.start()
    if not ends_before_subscription:
        async with asyncio.timeout(5):
            while run.subscriber_count == 0:
                await asyncio.sleep(0)
        finish()
    await settle_replies(engine)

    assert first.sent == []
    assert transport.sent == [("12345", "preserved partial continuation")]
    assert channel_state(tmp_path).take_pending_replies("tg-assistant", "later") == ([], 0)
    await engine.stop()


@pytest.mark.asyncio
async def test_work_an_ended_engine_never_answered_gets_one_notice_and_taps_work_again(
    tmp_path: Path,
) -> None:
    storage = channel_state(tmp_path)
    runs = _Runs()
    engine, sessions, _trigger, _first = make_engine(
        tmp_path, trigger_run=runs.trigger, run_button_binding_registry=storage
    )
    sessions.create("assistant", session_id="origin")
    storage.save_run_button_binding(
        "tg-assistant",
        RunButtonBinding(
            id="binding",
            platform_target="12345",
            thread_id=None,
            origin_session_id="origin",
            original_button_data=("run:done",),
            created_at=utc_now_timestamp(),
        ),
    )
    data = bound_run_callback_data("binding", 0)
    await engine.handle_inbound_text(make_conversation(), "first")
    await asyncio.wait_for(runs.started.wait(), timeout=5)
    # Both wait behind the running first message when the engine ends.
    await engine.handle_inbound_text(make_conversation(), "second")
    tap = InteractionEvent(
        platform="telegram",
        channel_id="tg-assistant",
        chat_id="12345",
        user_id="50",
        message_id="777",
        data=data,
        buttons=((InteractionButton(label="Done", data=data),),),
    )
    assert await engine.trigger_interaction_reply(make_conversation(), tap) == "enqueued"
    await engine.stop()

    _next, _sessions, transport = await _next_engine(tmp_path, run_button_binding_registry=storage)

    notice = f"{engine_module._UNANSWERED_MESSAGE_REPLY} {engine_module._UNANSWERED_TAP_REPLY}"
    assert sorted(transport.sent) == sorted(
        [("12345", content_module._INTERRUPTED_REPLY), ("12345", notice)]
    )
    claim = storage.claim_run_button_binding(
        "tg-assistant", "binding", platform_target="12345", thread_id=None
    )
    assert claim.status == "claimed"
