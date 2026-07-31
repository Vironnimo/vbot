"""Channel command and new-session lifecycle tests."""

from __future__ import annotations

from core.chat import (
    CommandDispatcher,
    CommandFeedback,
    ExtensionCommandContext,
)
from core.runs import COMPACTION_COMPLETED_EVENT, RunKind

from .engine_test_support import (
    CHANNEL_REPLY_SURFACE,
    SESSION_ID,
    AsyncMock,
    ChatRunManager,
    CommandNavigation,
    CommandOutcome,
    CommandRun,
    CommandUnavailability,
    ConversationFacts,
    Path,
    PreparedCommand,
    ReplyPlanFacts,
    RouteFacts,
    Run,
    asyncio,
    command_outcome,
    drain,
    engine_module,
    logging,
    make_command_dispatcher,
    make_completed_run,
    make_conversation,
    make_engine,
    make_new_only_dispatcher,
    pytest,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_mode", ["immediate", "serialized"])
async def test_extension_command_uses_generic_channel_projection(
    tmp_path: Path,
    execution_mode: str,
) -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    def handler(context: ExtensionCommandContext, argument: str | None) -> CommandOutcome:
        assert context.reply_surface == CHANNEL_REPLY_SURFACE
        return CommandOutcome(
            command="workflow",
            feedback=CommandFeedback(
                kind="notice",
                text=f"Workflow {argument or 'default'} complete.",
            ),
        )

    dispatcher.register_extension_command(
        "workflow_ext",
        name="workflow",
        description="Run the workflow.",
        handler=handler,
        execution_mode=execution_mode,
    )
    engine, _sessions, trigger_mock, transport = make_engine(
        tmp_path,
        command_dispatcher=dispatcher,
    )

    await engine.handle_inbound_text(make_conversation(), "/workflow review")
    await drain(engine, 12345)

    trigger_mock.assert_not_awaited()
    assert transport.sent == [("12345", "Workflow review complete.")]
    await engine.stop()


@pytest.mark.asyncio
async def test_extension_command_declared_unavailable_on_channels(
    tmp_path: Path,
) -> None:
    dispatcher = CommandDispatcher(ChatRunManager())
    dispatcher.register_extension_command(
        "workflow_ext",
        name="workflow",
        description="Run the workflow.",
        handler=lambda _context, _argument: CommandOutcome(command="workflow"),
        unavailable_surfaces=frozenset({"channel"}),
    )
    engine, _sessions, trigger_mock, transport = make_engine(
        tmp_path,
        command_dispatcher=dispatcher,
    )

    await engine.handle_inbound_text(make_conversation(), "/workflow")

    trigger_mock.assert_not_awaited()
    assert transport.sent == [("12345", "The /workflow command is not available through Telegram.")]
    await engine.stop()


@pytest.mark.asyncio
async def test_extension_command_relays_follow_up_run(tmp_path: Path) -> None:
    follow_up = make_completed_run(output_text="Workflow result.")
    dispatcher = CommandDispatcher(ChatRunManager())
    dispatcher.register_extension_command(
        "workflow_ext",
        name="workflow",
        description="Run the workflow.",
        handler=lambda _context, _argument: CommandOutcome(
            command="workflow",
            feedback=CommandFeedback(kind="notice", text="Workflow started."),
            runs=(CommandRun(role="follow_up", run=follow_up),),
        ),
    )
    engine, _sessions, _trigger_mock, transport = make_engine(
        tmp_path,
        command_dispatcher=dispatcher,
    )

    await engine.handle_inbound_text(make_conversation(), "/workflow")
    await drain(engine, 12345)

    assert transport.sent_texts == ["Workflow started.", "Workflow result."]
    await engine.stop()


@pytest.mark.asyncio
async def test_primary_compaction_run_relays_completed_feedback(tmp_path: Path) -> None:
    compaction_run = Run(
        run_id="run-compact",
        agent_id="assistant",
        session_id=SESSION_ID,
    )
    compaction_run.emit(COMPACTION_COMPLETED_EVENT, {})
    compaction_run.mark_completed("ok")
    dispatcher = make_command_dispatcher(
        result=CommandOutcome(
            command="compact",
            runs=(CommandRun(role="primary", run=compaction_run),),
        ),
    )
    engine, _sessions, _trigger_mock, transport = make_engine(
        tmp_path,
        command_dispatcher=dispatcher,
    )

    await engine.handle_inbound_text(make_conversation(), "/compact")
    await drain(engine, 12345)

    assert transport.sent_texts == ["Context compacted."]
    await engine.stop()


@pytest.mark.asyncio
async def test_extension_command_handler_failure_isolated_through_channel(
    tmp_path: Path,
) -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    def fail(_context: ExtensionCommandContext, _argument: str | None) -> CommandOutcome:
        raise RuntimeError("implementation detail")

    dispatcher.register_extension_command(
        "workflow_ext",
        name="workflow",
        description="Run the workflow.",
        handler=fail,
    )
    engine, _sessions, _trigger_mock, transport = make_engine(
        tmp_path,
        command_dispatcher=dispatcher,
    )

    await engine.handle_inbound_text(make_conversation(), "/workflow")
    await drain(engine, 12345)

    assert transport.sent_texts == ["The /workflow command failed. Check the server logs."]
    await engine.stop()


@pytest.mark.asyncio
async def test_queued_extension_command_removed_before_execution_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def handler(_context: ExtensionCommandContext, _argument: str | None) -> CommandOutcome:
        nonlocal called
        called = True
        return CommandOutcome(command="workflow")

    dispatcher = CommandDispatcher(ChatRunManager())
    dispatcher.register_extension_command(
        "workflow_ext",
        name="workflow",
        description="Run the workflow.",
        handler=handler,
    )
    engine, _sessions, _trigger_mock, transport = make_engine(
        tmp_path,
        command_dispatcher=dispatcher,
    )
    execution_reached = asyncio.Event()
    release_execution = asyncio.Event()
    execute_prepared = engine._execute_prepared_command

    async def pause_before_execution(
        prepared: PreparedCommand,
        conversation: ConversationFacts,
        route: RouteFacts,
        reply_plan: ReplyPlanFacts,
        conversation_key: str,
    ) -> None:
        execution_reached.set()
        await release_execution.wait()
        await execute_prepared(
            prepared,
            conversation,
            route,
            reply_plan,
            conversation_key,
        )

    monkeypatch.setattr(engine, "_execute_prepared_command", pause_before_execution)

    await engine.handle_inbound_text(make_conversation(), "/workflow")
    await asyncio.wait_for(execution_reached.wait(), timeout=1)
    dispatcher.unregister_extension_commands("workflow_ext")
    release_execution.set()
    await drain(engine, 12345)

    assert called is False
    assert transport.sent_texts == [
        "The /workflow command is no longer available. Please send it again."
    ]
    await engine.stop()


@pytest.mark.asyncio
async def test_handled_command_replies_before_trigger(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(result=command_outcome("stop", "Run cancelled."))
    engine, _sessions, trigger_mock, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/stop")
    await drain(engine, 12345)

    command_dispatcher.execute.assert_awaited_once()
    context = command_dispatcher.execute.await_args.args[1]
    assert (context.agent_id, context.session_id) == ("assistant", SESSION_ID)
    trigger_mock.assert_not_awaited()
    assert transport.sent == [("12345", "Run cancelled.")]
    await engine.stop()


@pytest.mark.asyncio
async def test_compact_command_action_replies_in_worker(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(
        result=command_outcome("compact", "Context compacted.")
    )
    engine, _sessions, trigger_mock, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/compact")
    await drain(engine, 12345)

    command_dispatcher.execute.assert_awaited_once()
    trigger_mock.assert_not_awaited()
    assert transport.sent == [("12345", "Context compacted.")]
    await engine.stop()


@pytest.mark.asyncio
async def test_compact_command_action_forwards_instruction(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(
        result=command_outcome("compact", "Context compacted."),
        argument="keep the API design",
    )
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/compact keep the API design")
    await drain(engine, 12345)

    prepared = command_dispatcher.execute.await_args.args[0]
    assert prepared.argument == "keep the API design"
    await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "command", "argument"),
    [
        ("/learn the deploy steps", "learn", "the deploy steps"),
        ("/reflect", "reflect", None),
        ("/model openai/gpt-5", "model", "openai/gpt-5"),
        ("/rename Release planning", "rename", "Release planning"),
        ("/handoff agent:reviewer", "handoff", "agent:reviewer"),
    ],
)
async def test_unified_stateful_commands_execute_through_chat_core(
    tmp_path: Path,
    message: str,
    command: str,
    argument: str | None,
) -> None:
    dispatcher = make_command_dispatcher(
        result=command_outcome(command, f"{command} complete"),
        argument=argument,
    )
    engine, _sessions, trigger_mock, transport = make_engine(
        tmp_path, command_dispatcher=dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), message)
    await drain(engine, 12345)

    dispatcher.execute.assert_awaited_once()
    prepared, context = dispatcher.execute.await_args.args
    assert (prepared.name, prepared.argument) == (command, argument)
    assert context.reply_surface == CHANNEL_REPLY_SURFACE
    assert transport.sent == [("12345", f"{command} complete")]
    trigger_mock.assert_not_awaited()
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
    engine, chat_sessions, _trigger, transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        command_dispatcher=dispatcher,
    )

    await engine.handle_inbound_text(make_conversation(), "/handoff agent:reviewer")
    await drain(engine, 12345)
    await engine.handle_inbound_text(make_conversation(), "later message")
    await drain(engine, 12345)

    assert transport.sent_texts == ["review reply", "source reply"]
    assert engine._config.agent_id == "assistant"
    assert (
        chat_sessions.get_metadata("assistant", SESSION_ID).get(
            engine_module.ACTIVE_SESSION_METADATA_KEY
        )
        is None
    )
    assert trigger_mock.await_args is not None
    assert trigger_mock.await_args.args[:3] == ("assistant", "later message", SESSION_ID)
    await engine.stop()


@pytest.mark.asyncio
async def test_new_session_command_starts_fresh_session_and_redirects_followups(
    tmp_path: Path,
) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, transport = make_engine(
        tmp_path, trigger_run=trigger_mock, command_dispatcher=make_new_only_dispatcher()
    )

    await engine.handle_inbound_text(make_conversation(), "/new")
    await drain(engine, 12345)

    new_session_id = chat_sessions.get_metadata("assistant", SESSION_ID)[
        engine_module.ACTIVE_SESSION_METADATA_KEY
    ]
    # A distinct session, anchored to the conversation for grouping, was created.
    assert new_session_id != SESSION_ID
    assert new_session_id.startswith(f"{SESSION_ID}-")
    assert chat_sessions.exists("assistant", new_session_id)
    # The previous (anchor) session is left intact and still loadable, but /new
    # does not invent a model-facing note for either session.
    assert chat_sessions.get("assistant", SESSION_ID).load() == []
    # /new confirms without triggering a run.
    assert transport.sent_texts == [engine_module._NEW_SESSION_STARTED_REPLY]
    trigger_mock.assert_not_awaited()

    # A later message follows the pointer into the new session.
    await engine.handle_inbound_text(make_conversation(), "after new")
    await drain(engine, 12345)

    trigger_mock.assert_awaited_once()
    assert trigger_mock.await_args is not None
    assert trigger_mock.await_args.args[2] == new_session_id
    await engine.stop()


@pytest.mark.asyncio
async def test_message_enqueued_behind_pending_new_routes_to_new_session(tmp_path: Path) -> None:
    """A message arriving before a queued /new is processed follows the moved pointer.

    Routing is resolved at processing time, not enqueue time: both items sit in the
    conversation queue together, /new moves the pointer first, and the message that
    was already enqueued behind it must land in the new session, not the old one.
    """
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, command_dispatcher=make_new_only_dispatcher()
    )

    await engine.handle_inbound_text(make_conversation(), "/new")
    await engine.handle_inbound_text(make_conversation(), "right after new")
    await drain(engine, 12345)

    new_session_id = chat_sessions.get_metadata("assistant", SESSION_ID)[
        engine_module.ACTIVE_SESSION_METADATA_KEY
    ]
    trigger_mock.assert_awaited_once()
    assert trigger_mock.await_args is not None
    assert trigger_mock.await_args.args[2] == new_session_id
    await engine.stop()


@pytest.mark.asyncio
async def test_new_session_tags_fresh_session_with_metadata_but_no_reminder(tmp_path: Path) -> None:
    command_dispatcher = make_new_only_dispatcher()
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), "/new")
    await drain(engine, 12345)

    new_session_id = chat_sessions.get_metadata("assistant", SESSION_ID)[
        engine_module.ACTIVE_SESSION_METADATA_KEY
    ]
    notes = [
        message
        for message in chat_sessions.get("assistant", new_session_id).load()
        if message.role == "note"
    ]
    assert notes == []
    metadata = chat_sessions.get_metadata("assistant", new_session_id)
    assert metadata["source_channel_id"] == "tg-assistant"
    assert metadata["platform"] == "telegram"
    assert metadata["platform_conv_id"] == "12345"
    assert metadata["last_reply_target"] == {
        "channel_id": "tg-assistant",
        "platform_target": "12345",
    }
    # The fresh session is not itself a pointer anchor, and tracks no participant.
    assert engine_module.ACTIVE_SESSION_METADATA_KEY not in metadata
    assert "participants" not in metadata
    await engine.stop()


@pytest.mark.asyncio
async def test_new_session_command_refused_while_run_active(tmp_path: Path) -> None:
    command_dispatcher = make_command_dispatcher(
        result=command_outcome(
            "new", "A new session can be started after the current run finishes."
        )
    )
    engine, chat_sessions, trigger_mock, transport = make_engine(
        tmp_path,
        command_dispatcher=command_dispatcher,
    )

    await engine.handle_inbound_text(make_conversation(), "/new")
    await drain(engine, 12345)

    assert transport.sent_texts == ["A new session can be started after the current run finishes."]
    trigger_mock.assert_not_awaited()
    # No new session and no pointer: the anchor is unchanged.
    metadata = chat_sessions.get_metadata("assistant", SESSION_ID)
    assert engine_module.ACTIVE_SESSION_METADATA_KEY not in metadata
    await engine.stop()


@pytest.mark.asyncio
async def test_new_session_in_one_chat_leaves_other_chat_untouched(tmp_path: Path) -> None:
    trigger_mock = AsyncMock(return_value=make_completed_run(output_text="ok"))
    engine, chat_sessions, _trigger, _transport = make_engine(
        tmp_path, trigger_run=trigger_mock, command_dispatcher=make_new_only_dispatcher()
    )

    await engine.handle_inbound_text(make_conversation(chat_id=12345), "/new")
    await drain(engine, 12345)

    await engine.handle_inbound_text(make_conversation(chat_id=67890), "hello B")
    await drain(engine, 67890)

    # Chat B is unaffected: it routes to its own derived anchor and has no pointer.
    trigger_mock.assert_awaited_once_with(
        "assistant",
        "hello B",
        "ch-tg-assistant-67890",
        sender=None,
        reply_surface=CHANNEL_REPLY_SURFACE,
        run_kind=RunKind.CHANNEL,
    )
    metadata_b = chat_sessions.get_metadata("assistant", "ch-tg-assistant-67890")
    assert engine_module.ACTIVE_SESSION_METADATA_KEY not in metadata_b
    await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["/agent", "/agent planner"])
async def test_agent_command_reports_permanent_channel_limitation(
    tmp_path: Path, message: str
) -> None:
    command_dispatcher = make_command_dispatcher(
        result=command_outcome("agent", "unused"),
        unavailable=CommandUnavailability(command="/agent", surface="channel"),
    )
    engine, _sessions, trigger_mock, transport = make_engine(
        tmp_path, command_dispatcher=command_dispatcher
    )

    await engine.handle_inbound_text(make_conversation(), message)
    await drain(engine, 12345)

    trigger_mock.assert_not_awaited()
    assert transport.sent == [("12345", "The /agent command is not available through Telegram.")]
    command_dispatcher.execute.assert_not_awaited()
    await engine.stop()


@pytest.mark.asyncio
async def test_compact_action_failure_is_logged_and_replies_generically(
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
async def test_stop_command_eagerly_dispatched_while_worker_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    command_dispatcher = make_command_dispatcher(result=command_outcome("stop", "Run cancelled."))
    trigger_mock = AsyncMock(
        return_value=Run(run_id="run-active", agent_id="assistant", session_id=SESSION_ID)
    )
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path, trigger_run=trigger_mock, command_dispatcher=command_dispatcher
    )

    relay_started = asyncio.Event()
    release_relay = asyncio.Event()

    async def block_relay(_run: Run, _platform_target: str) -> None:
        relay_started.set()
        await release_relay.wait()

    monkeypatch.setattr(engine, "_relay_run_events", AsyncMock(side_effect=block_relay))

    await engine.handle_inbound_text(make_conversation(), "hello")
    await asyncio.wait_for(relay_started.wait(), timeout=1)

    await engine.handle_inbound_text(make_conversation(), "/stop")
    await asyncio.sleep(0)

    # Plain text never touches the dispatcher; the command dispatched eagerly while
    # the worker was still blocked relaying the first message's run.
    command_dispatcher.execute.assert_awaited_once()
    assert trigger_mock.await_count == 1
    assert transport.sent == [("12345", "Run cancelled.")]

    release_relay.set()
    await drain(engine, 12345)
    await engine.stop()


@pytest.mark.asyncio
async def test_stop_command_bypasses_full_waiting_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waiting_work_manager = ChatRunManager(waiting_work_limit=1)
    command_dispatcher = make_command_dispatcher(result=command_outcome("stop", "Run cancelled."))
    trigger_mock = AsyncMock(
        return_value=Run(run_id="run-active", agent_id="assistant", session_id=SESSION_ID)
    )
    engine, _sessions, _trigger, transport = make_engine(
        tmp_path,
        trigger_run=trigger_mock,
        command_dispatcher=command_dispatcher,
        waiting_work_manager=waiting_work_manager,
    )
    relay_started = asyncio.Event()
    release_relay = asyncio.Event()

    async def block_relay(_run: Run, _reply_plan: ReplyPlanFacts) -> None:
        relay_started.set()
        await release_relay.wait()

    monkeypatch.setattr(engine, "_relay_run_events", block_relay)

    await engine.handle_inbound_text(make_conversation(), "running")
    await asyncio.wait_for(relay_started.wait(), timeout=1)
    await engine.handle_inbound_text(make_conversation(), "queued")
    await engine.handle_inbound_text(make_conversation(), "/stop")

    command_dispatcher.execute.assert_awaited_once()
    assert transport.sent_texts == ["Run cancelled."]
    release_relay.set()
    await drain(engine, 12345)
    await engine.stop()
