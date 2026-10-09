"""Channel engine: replies owed across adapter restarts and from an ended engine."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
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
from .telegram_test_support import make_adapter, make_callback_update

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
@pytest.mark.parametrize("restoration", ["restored", "missing", "failed", "older_record"])
async def test_work_an_ended_engine_never_answered_gets_notices_and_usable_tap_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restoration: str
) -> None:
    storage = channel_state(tmp_path)
    runs = _Runs()
    adapter, sessions, _trigger, bot = make_adapter(
        tmp_path,
        monkeypatch,
        allowed_chat_ids=[-100],
        response_mode="all",
        admin_user_ids=["50"],
        trigger_run=runs.trigger,
        run_button_binding_registry=storage,
    )
    engine = adapter._engine
    conversation = make_conversation(chat_id=-100, kind="group", thread_id="7")
    await engine.handle_inbound_text(conversation, "first")
    await asyncio.wait_for(runs.started.wait(), timeout=5)
    # All wait behind the running first message when the engine ends. Each tap
    # must keep its own origin, reply reference and topic in the same chat.
    await engine.handle_inbound_text(conversation, "second")
    taps = []
    for binding_id, thread_id, message_id in [("binding", "7", "777"), ("other", "8", "888")]:
        sessions.create("assistant", session_id=binding_id)
        storage.save_run_button_binding(
            "tg-assistant",
            RunButtonBinding(
                id=binding_id,
                platform_target="-100",
                thread_id=thread_id,
                origin_session_id=binding_id,
                original_button_data=("run:done",),
                created_at=utc_now_timestamp(),
            ),
        )
        data = bound_run_callback_data(binding_id, 0)
        tap = InteractionEvent(
            platform="telegram",
            channel_id="tg-assistant",
            chat_id="-100",
            user_id="50",
            message_id=message_id,
            thread_id=thread_id,
            data=data,
            buttons=((InteractionButton(label="Done", data=data),),),
        )
        tap_conversation = replace(conversation, message_id=message_id, thread_id=thread_id)
        taps.append((tap_conversation, tap))
        update = make_callback_update(
            chat_id=-100,
            message_id=int(message_id),
            data=data,
            inline_keyboard=[[SimpleNamespace(text="Done", callback_data=data)]],
        )
        update.effective_message.message_thread_id = int(thread_id)
        update.effective_message.is_topic_message = True
        await adapter._handle_callback_query(update, None)
        # A duplicate tap closes the original keyboard too, but may not lose the
        # accepted tap's durable retry context.
        await adapter._handle_callback_query(update, None)
        assert bot.edit_message_reply_markup.await_args.kwargs["reply_markup"] is None
        assert await engine.trigger_interaction_reply(tap_conversation, tap) == "already_handled"
    await adapter.stop()

    if restoration == "missing":
        storage.discard_run_button_binding("tg-assistant", "binding")
    elif restoration == "failed":
        restore = storage.restore_run_button_binding

        def fail_restore(channel_id: str, binding_id: str) -> bool:
            if binding_id == "binding":
                raise RuntimeError("binding restore unavailable")
            return restore(channel_id, binding_id)

        monkeypatch.setattr(storage, "restore_run_button_binding", fail_restore)
    elif restoration == "older_record":
        for pending in storage.take_pending_replies("tg-assistant", "inspector")[0]:
            if pending.binding_id == "binding":
                storage.owe_reply("tg-assistant", replace(pending, retry_keyboard=None))

    resumed_runs = _Runs()
    _next, _sessions, transport = await _next_engine(
        tmp_path,
        run_button_binding_registry=storage,
        admin_user_ids=["50"],
        trigger_run=resumed_runs.trigger,
    )

    retry_available = restoration == "restored"
    tap_notice = (
        engine_module._UNANSWERED_TAP_ONLY_REPLY
        if retry_available
        else engine_module._UNANSWERED_TAP_UNAVAILABLE_REPLY
    )
    assert sorted(transport.sent) == sorted(
        [
            ("-100", content_module._INTERRUPTED_REPLY),
            ("-100", engine_module._UNANSWERED_MESSAGE_REPLY),
            ("-100", tap_notice),
            ("-100", engine_module._UNANSWERED_TAP_ONLY_REPLY),
        ]
    )
    retry_targets = {
        thread_id: (reply_to, buttons)
        for thread_id, reply_to, buttons in zip(
            transport.sent_thread_ids,
            transport.sent_reply_targets,
            transport.sent_buttons,
            strict=True,
        )
        if buttons is not None
    }
    available_taps = taps if retry_available else taps[1:]
    assert retry_targets == {
        tap.thread_id: (tap.message_id, [list(tap.buttons[0])]) for _, tap in available_taps
    }
    for tap_conversation, tap in available_taps:
        # The reissued wire keyboard admits exactly one new tap to the origin.
        assert await _next.trigger_interaction_reply(tap_conversation, tap) == "enqueued"
        assert await _next.trigger_interaction_reply(tap_conversation, tap) == "already_handled"
        await asyncio.wait_for(resumed_runs.started.wait(), timeout=5)
        expected_origin = "binding" if tap.thread_id == "7" else "other"
        assert resumed_runs.runs[-1].session_id == expected_origin
        _answer(resumed_runs.runs[-1], "done")
        await drain(_next, -100)
        resumed_runs.started.clear()
    await _next.stop()
