"""Channel engine: Command scheduling, outcome projection and the /new lifecycle."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import core.channels.engine as engine_module
from core.chat import CommandDispatcher, CommandFeedback, ExtensionCommandContext
from core.chat.commands import CommandNavigation, CommandOutcome, CommandRun
from core.runs import COMPACTION_COMPLETED_EVENT, ChatRunManager, Run, RunKind
from core.sessions import SessionAddress

from .engine_test_support import (
    CHANNEL_REPLY_SURFACE,
    SESSION_ID,
    HeldRuns,
    channel_state,
    command_outcome,
    drain,
    make_command_dispatcher,
    make_completed_run,
    make_conversation,
    make_engine,
    make_new_only_dispatcher,
)


def _workflow_dispatcher(handler, **options) -> CommandDispatcher:  # type: ignore[no-untyped-def]
    dispatcher = CommandDispatcher(ChatRunManager())
    dispatcher.register_extension_command(
        "workflow_ext",
        name="workflow",
        description="Run the workflow.",
        handler=handler,
        **options,
    )
    return dispatcher


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_mode", ["immediate", "serialized"])
async def test_commands_execute_through_chat_and_reply_without_a_run(
    tmp_path: Path, execution_mode: str
) -> None:
    contexts: list[ExtensionCommandContext] = []

    def handler(context: ExtensionCommandContext, argument: str | None) -> CommandOutcome:
        contexts.append(context)
        return CommandOutcome(
            command="workflow",
            feedback=CommandFeedback(kind="notice", text=f"Workflow {argument} complete."),
        )

    engine, _sessions, trigger_mock, transport = make_engine(
        tmp_path,
        command_dispatcher=_workflow_dispatcher(handler, execution_mode=execution_mode),
    )

    await engine.handle_inbound_text(make_conversation(), "/workflow keep the API design")
    await drain(engine, 12345)

    [context] = contexts
    assert (context.agent_id, context.session_id, context.project_id) == (
        "assistant",
        SESSION_ID,
        None,
    )
    assert context.reply_surface == CHANNEL_REPLY_SURFACE
    trigger_mock.assert_not_awaited()
    assert transport.sent == [("12345", "Workflow keep the API design complete.")]
    await engine.stop()


@pytest.mark.asyncio
async def test_a_command_unavailable_on_channels_is_refused_by_name(tmp_path: Path) -> None:
    handler = AsyncMock()
    engine, _sessions, trigger_mock, transport = make_engine(
        tmp_path,
        command_dispatcher=_workflow_dispatcher(
            handler, unavailable_surfaces=frozenset({"channel"})
        ),
    )

    await engine.handle_inbound_text(make_conversation(), "/workflow planner")

    handler.assert_not_called()
    trigger_mock.assert_not_awaited()
    assert transport.sent == [("12345", "The /workflow command is not available through Telegram.")]
    await engine.stop()


@pytest.mark.asyncio
async def test_extension_page_navigation_preserves_channel_anchor(tmp_path: Path) -> None:
    dispatcher = _workflow_dispatcher(
        lambda _context, _argument: CommandOutcome(
            command="workflow",
            feedback=CommandFeedback(kind="notice", text="Workflow is ready."),
            navigation=CommandNavigation(
                kind="open_extension_page", extension="workflow_ext", page="overview"
            ),
        ),
        page_ids=frozenset({"overview"}),
        execution_mode="immediate",
    )
    trigger = AsyncMock(return_value=make_completed_run(output_text="source reply"))
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, trigger_run=trigger, command_dispatcher=dispatcher
    )
    await engine.handle_inbound_text(make_conversation(), "/workflow")
    await engine.handle_inbound_text(make_conversation(), "later message")
    await drain(engine, 12345)

    assert transport.sent_texts == ["Workflow is ready.", "source reply"]
    assert trigger.await_args is not None
    assert trigger.await_args.args[:3] == ("assistant", "later message", SESSION_ID)
    await engine.stop()


@pytest.mark.asyncio
async def test_extension_command_relays_follow_up_run(tmp_path: Path) -> None:
    follow_up = make_completed_run(output_text="Workflow result.")
    dispatcher = _workflow_dispatcher(
        lambda _context, _argument: CommandOutcome(
            command="workflow",
            feedback=CommandFeedback(kind="notice", text="Workflow started."),
            runs=(CommandRun(role="follow_up", run=follow_up),),
        )
    )
    engine, _sessions, _trigger_mock, transport = make_engine(
        tmp_path, command_dispatcher=dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/workflow")
    await drain(engine, 12345)

    assert transport.sent_texts == ["Workflow started.", "Workflow result."]
    await engine.stop()


@pytest.mark.asyncio
async def test_primary_compaction_run_relays_completed_feedback(tmp_path: Path) -> None:
    compaction_run = Run(run_id="run-compact", agent_id="assistant", session_id=SESSION_ID)
    compaction_run.emit(COMPACTION_COMPLETED_EVENT, {})
    compaction_run.mark_completed("ok")
    dispatcher = make_command_dispatcher(
        result=CommandOutcome(
            command="compact",
            runs=(CommandRun(role="primary", run=compaction_run),),
        ),
    )
    engine, _sessions, _trigger_mock, transport = make_engine(
        tmp_path, command_dispatcher=dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/compact")
    await drain(engine, 12345)

    assert transport.sent_texts == ["Context compacted."]
    await engine.stop()


@pytest.mark.asyncio
async def test_extension_command_handler_failure_isolated_through_channel(
    tmp_path: Path,
) -> None:
    def fail(_context: ExtensionCommandContext, _argument: str | None) -> CommandOutcome:
        raise RuntimeError("implementation detail")

    engine, _sessions, _trigger_mock, transport = make_engine(
        tmp_path, command_dispatcher=_workflow_dispatcher(fail)
    )

    await engine.handle_inbound_text(make_conversation(), "/workflow")
    await drain(engine, 12345)

    assert transport.sent_texts == ["The /workflow command failed. Check the server logs."]
    await engine.stop()


@pytest.mark.asyncio
async def test_command_execution_failure_is_logged_and_replies_generically(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    command_dispatcher = make_command_dispatcher(result=command_outcome("compact", "unused"))
    command_dispatcher.execute.side_effect = RuntimeError("compact failed")
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )
    caplog.set_level(logging.ERROR, logger="vbot.channels.engine")

    await engine.handle_inbound_text(make_conversation(), "/compact")
    await drain(engine, 12345)

    assert transport.sent_texts == [engine_module._FAILED_REPLY]
    records = [r for r in caplog.records if r.message.startswith("Channel command failed")]
    assert len(records) == 1
    assert "command=compact" in records[0].message
    assert records[0].exc_info is not None
    await engine.stop()


@pytest.mark.asyncio
async def test_queued_extension_command_removed_before_execution_is_stale(
    tmp_path: Path,
) -> None:
    handler = AsyncMock()
    dispatcher = _workflow_dispatcher(handler)
    runs = HeldRuns()
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, trigger_run=runs.trigger, command_dispatcher=dispatcher
    )
    try:
        # The serialized Command waits behind a running turn.
        await engine.handle_inbound_text(make_conversation(), "running")
        await runs.wait_started()
        await engine.handle_inbound_text(make_conversation(), "/workflow")
        dispatcher.unregister_extension_commands("workflow_ext")
        runs.release()
        await drain(engine, 12345)

        handler.assert_not_called()
        assert transport.sent_texts[-1] == (
            "The /workflow command is no longer available. Please send it again."
        )
    finally:
        runs.release()
        await engine.stop()


@pytest.mark.asyncio
async def test_handoff_follow_up_is_one_shot_and_keeps_channel_anchor(tmp_path: Path) -> None:
    follow_up = make_completed_run(session_id="review-session", output_text="review reply")
    dispatcher = make_command_dispatcher(
        result=CommandOutcome(
            command="handoff",
            navigation=CommandNavigation(
                kind="offer_session",
                agent_id="reviewer",
                session_id="review-session",
            ),
            runs=(CommandRun(role="follow_up", run=follow_up),),
        ),
        argument="agent:reviewer",
    )
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="source reply"))
    engine, _chat_sessions, _trigger, transport = make_engine(
        tmp_path, trigger_run=trigger_mock, command_dispatcher=dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/handoff agent:reviewer")
    await drain(engine, 12345)
    await engine.handle_inbound_text(make_conversation(), "later message")
    await drain(engine, 12345)

    assert transport.sent_texts == ["review reply", "source reply"]
    assert channel_state(tmp_path).active_session_id("tg-assistant", SESSION_ID) is None
    assert trigger_mock.await_args is not None
    assert trigger_mock.await_args.args[:3] == ("assistant", "later message", SESSION_ID)
    await engine.stop()


@pytest.mark.asyncio
async def test_new_starts_a_fresh_session_for_its_chat_only(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, transport = make_engine(
        tmp_path, trigger_run=trigger_mock, command_dispatcher=make_new_only_dispatcher()
    )
    state = channel_state(tmp_path)

    # Routing resolves when work is processed: the message queued behind /new
    # lands in the new Session.
    await engine.handle_inbound_text(make_conversation(), "/new")
    await engine.handle_inbound_text(make_conversation(), "right after new")
    await drain(engine, 12345)
    await engine.handle_inbound_text(make_conversation(chat_id=67890), "hello B")
    await drain(engine, 67890)

    new_session_id = state.active_session_id("tg-assistant", SESSION_ID)
    assert new_session_id is not None
    assert new_session_id != SESSION_ID
    assert new_session_id.startswith("ses_")
    new_address = SessionAddress(project_id=None, agent_id="assistant", session_id=new_session_id)
    # /new confirms without a Run and invents no note for either Session.
    assert transport.sent_texts[0] == engine_module._NEW_SESSION_STARTED_REPLY
    assert (
        chat_sessions.get(
            SessionAddress(project_id=None, agent_id="assistant", session_id=SESSION_ID)
        ).load()
        == []
    )
    assert [
        message for message in chat_sessions.get(new_address).load() if message.role == "note"
    ] == []
    metadata = chat_sessions.get_metadata(new_address)
    assert metadata["source_channel_id"] == "tg-assistant"
    assert metadata["platform"] == "telegram"
    assert metadata["platform_conv_id"] == "12345"
    assert metadata["last_reply_target"] == {
        "channel_id": "tg-assistant",
        "platform_target": "12345",
    }
    # Routing state lives in the Channel state database, not in Session metadata.
    assert not {"active_session_id", "conversation_kind", "participants"} & metadata.keys()
    assert state.active_session_id("tg-assistant", new_session_id) is None

    first_call, second_call = trigger_mock.await_args_list
    assert first_call.args[1:3] == ("right after new", new_session_id)
    # Another chat keeps routing to its own derived anchor without a pointer.
    assert second_call.args == ("assistant", "hello B", "ch-tg-assistant-67890")
    assert second_call.kwargs == {
        "sender": None,
        "reply_surface": CHANNEL_REPLY_SURFACE,
        "run_kind": RunKind.CHANNEL,
    }
    assert state.active_session_id("tg-assistant", "ch-tg-assistant-67890") is None
    await engine.stop()


@pytest.mark.asyncio
async def test_a_refused_new_keeps_the_anchor(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(
        result=command_outcome(
            "new", "A new session can be started after the current run finishes."
        )
    )
    engine, _chat_sessions, trigger_mock, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/new")
    await drain(engine, 12345)

    assert transport.sent_texts == ["A new session can be started after the current run finishes."]
    trigger_mock.assert_not_awaited()
    assert channel_state(tmp_path).active_session_id("tg-assistant", SESSION_ID) is None
    await engine.stop()


@pytest.mark.asyncio
async def test_stop_is_dispatched_at_once_past_a_busy_worker_and_a_full_queue(
    tmp_path: Path,
) -> None:
    waiting_work_manager = ChatRunManager(waiting_work_limit=1)
    command_dispatcher = make_command_dispatcher(result=command_outcome("stop", "Run cancelled."))
    runs = HeldRuns()
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path,
        trigger_run=runs.trigger,
        command_dispatcher=command_dispatcher,
        waiting_work_manager=waiting_work_manager,
    )
    try:
        await engine.handle_inbound_text(make_conversation(), "running")
        await runs.wait_started()
        await engine.handle_inbound_text(make_conversation(), "queued")
        await engine.handle_inbound_text(make_conversation(), "/stop")

        command_dispatcher.execute.assert_awaited_once()
        assert runs.contents == ["running"]
        assert transport.sent == [("12345", "Run cancelled.")]
    finally:
        runs.release()
        await drain(engine, 12345)
        await engine.stop()
