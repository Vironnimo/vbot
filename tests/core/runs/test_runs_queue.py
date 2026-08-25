"""Run queue admission, mutation, and FIFO draining tests."""

from __future__ import annotations

from core.chat.content_blocks import FileBlock
from core.sessions import SessionAddress
from tests.core.chat.chat_loop_support import build_chat_loop

from .runs_test_support import (
    RUN_STARTED_EVENT,
    ActiveRunError,
    Any,
    ChatRunManager,
    ChatSessionManager,
    Path,
    QueuedRunItem,
    Run,
    RunStatus,
    SimpleNamespace,
    WaitingWorkLimitError,
    asyncio,
    logging,
    pytest,
)

pytestmark = pytest.mark.asyncio


async def test_rejects_second_active_run_for_same_session() -> None:
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        await release.wait()
        return run.id

    first_run = await manager.start(
        agent_id="coder", session_id="session-one", executor=execute, project_id=None
    )

    with pytest.raises(ActiveRunError):
        await manager.start(
            agent_id="coder", session_id="session-one", executor=execute, project_id=None
        )

    release.set()
    assert await first_run.wait() == first_run.id


async def test_waiting_work_limit_rejects_the_next_queued_run() -> None:
    manager = ChatRunManager(waiting_work_limit=2)
    release = asyncio.Event()

    async def execute(_run: Run) -> str:
        await release.wait()
        return "done"

    active_run = await manager.start(
        agent_id="coder", session_id="session-one", executor=execute, project_id=None
    )
    first = await manager.enqueue(
        agent_id="coder", session_id="session-one", executor=execute, project_id=None
    )
    second = await manager.enqueue(
        agent_id="coder", session_id="session-one", executor=execute, project_id=None
    )

    assert manager.waiting_work_count() == 2
    with pytest.raises(WaitingWorkLimitError):
        await manager.enqueue(
            agent_id="coder", session_id="session-one", executor=execute, project_id=None
        )

    release.set()
    assert await active_run.wait() == "done"
    assert await (await first.future).wait() == "done"
    assert await (await second.future).wait() == "done"


async def test_waiting_work_admission_transfers_to_a_queued_run() -> None:
    manager = ChatRunManager(waiting_work_limit=1)
    release = asyncio.Event()

    async def execute(_run: Run) -> str:
        await release.wait()
        return "done"

    active_run = await manager.start(
        agent_id="coder", session_id="session-one", executor=execute, project_id=None
    )
    admission = manager.reserve_waiting_work(scope="channel:chat", scope_limit=8)

    queued = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=execute,
        project_id=None,
        waiting_work_admission=admission,
    )

    assert manager.waiting_work_count() == 1
    assert manager.release_waiting_work(admission) is False
    with pytest.raises(WaitingWorkLimitError):
        manager.reserve_waiting_work(scope="other:chat", scope_limit=8)

    release.set()
    assert await active_run.wait() == "done"
    started_queued_run = await queued.future
    assert manager.waiting_work_count() == 0
    assert await started_queued_run.wait() == "done"


async def test_waiting_work_admission_enforces_its_scope_limit() -> None:
    manager = ChatRunManager(waiting_work_limit=4)

    first = manager.reserve_waiting_work(scope="channel:chat", scope_limit=2)
    second = manager.reserve_waiting_work(scope="channel:chat", scope_limit=2)

    with pytest.raises(WaitingWorkLimitError):
        manager.reserve_waiting_work(scope="channel:chat", scope_limit=2)

    assert manager.release_waiting_work(first) is True
    assert manager.release_waiting_work(second) is True


async def test_enqueue_when_session_is_idle_starts_run_immediately() -> None:
    manager = ChatRunManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(_run: Run) -> str:
        started.set()
        await release.wait()
        return "done"

    item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=execute,
        display_content="Queued hello",
        project_id=None,
    )
    run = await item.future

    assert run.status == RunStatus.RUNNING
    assert manager.active_run(agent_id="coder", session_id="session-one", project_id=None) is run
    assert manager.list_queued("coder", "session-one", project_id=None) == []
    assert item.to_dict()["content"] == "Queued hello"

    await started.wait()
    release.set()

    assert await run.wait() == "done"


async def test_enqueue_when_session_is_busy_queues_and_drains_after_completion() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()
    queued_started = asyncio.Event()
    queued_release = asyncio.Event()

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    async def queued_execute(_run: Run) -> str:
        queued_started.set()
        await queued_release.wait()
        return "queued"

    active_run = await manager.start(
        agent_id="coder", session_id="session-one", executor=active_execute, project_id=None
    )
    item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=queued_execute,
        display_content="Queued next",
        project_id=None,
        work_id="sub-work-one",
    )

    assert item.future.done() is False
    assert item.work_id == "sub-work-one"
    assert [
        queued_item.item_id
        for queued_item in manager.list_queued("coder", "session-one", project_id=None)
    ] == [item.item_id]

    active_release.set()
    assert await active_run.wait() == "active"

    queued_run = await asyncio.wait_for(item.future, timeout=1)

    assert queued_run.status == RunStatus.RUNNING
    assert queued_run.work_id == "sub-work-one"
    assert manager.list_queued("coder", "session-one", project_id=None) == []

    await queued_started.wait()
    queued_release.set()

    assert await queued_run.wait() == "queued"


async def test_queued_run_keeps_agent_activity_projection_policy_when_drained() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    async def queued_execute(_run: Run) -> str:
        return "queued"

    active_run = await manager.start(
        agent_id="coder", session_id="session-one", executor=active_execute, project_id=None
    )
    item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=queued_execute,
        project_id=None,
        contributes_to_agent_activity=False,
    )

    assert item.contributes_to_agent_activity is False
    active_release.set()
    assert await active_run.wait() == "active"

    queued_run = await asyncio.wait_for(item.future, timeout=1)
    assert await queued_run.wait() == "queued"
    assert queued_run.contributes_to_agent_activity is False
    assert all(event.contributes_to_agent_activity is False for event in queued_run.events)


async def test_all_queued_returns_fresh_cross_session_snapshot_in_fifo_order() -> None:
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(_run: Run) -> str:
        await release.wait()
        return "done"

    identity_run = await manager.start(
        agent_id="coder", session_id="session-one", executor=execute, project_id=None
    )
    project_run = await manager.start(
        agent_id="writer", session_id="session-two", executor=execute, project_id="project-a"
    )
    identity_item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=execute,
        display_content="identity",
        project_id=None,
    )
    project_item = await manager.enqueue(
        agent_id="writer",
        session_id="session-two",
        executor=execute,
        display_content="internal",
        internal=True,
        project_id="project-a",
    )

    snapshot = manager.all_queued()
    assert snapshot == [
        (
            SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
            identity_item,
        ),
        (
            SessionAddress(project_id="project-a", agent_id="writer", session_id="session-two"),
            project_item,
        ),
    ]
    snapshot.clear()
    assert manager.all_queued() == [
        (
            SessionAddress(project_id=None, agent_id="coder", session_id="session-one"),
            identity_item,
        ),
        (
            SessionAddress(project_id="project-a", agent_id="writer", session_id="session-two"),
            project_item,
        ),
    ]

    release.set()
    assert await identity_run.wait() == "done"
    assert await project_run.wait() == "done"
    assert await (await identity_item.future).wait() == "done"
    assert await (await project_item.future).wait() == "done"


async def test_enqueue_when_session_is_busy_logs_queue_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()

    async def execute(_run: Run) -> str:
        await active_release.wait()
        return "done"

    active_run = await manager.start(
        agent_id="coder", session_id="session-one", executor=execute, project_id=None
    )
    with caplog.at_level(logging.INFO, logger="vbot.runs"):
        item = await manager.enqueue(
            agent_id="coder",
            session_id="session-one",
            executor=execute,
            display_content="Queued next",
            project_id=None,
        )

    queue_line = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("Run queued for busy session")
    )
    assert "agent=coder" in queue_line
    assert "session=session-one" in queue_line
    assert "queue_depth=1" in queue_line

    active_release.set()
    assert await active_run.wait() == "done"
    queued_run = await asyncio.wait_for(item.future, timeout=1)
    assert await queued_run.wait() == "done"


async def test_multiple_enqueued_items_drain_in_fifo_order() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()
    started: list[str] = []
    started_events = {
        "first": asyncio.Event(),
        "second": asyncio.Event(),
        "third": asyncio.Event(),
    }
    queued_releases = {
        "first": asyncio.Event(),
        "second": asyncio.Event(),
        "third": asyncio.Event(),
    }

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    def make_executor(label: str) -> Any:
        async def execute(_run: Run) -> str:
            started.append(label)
            started_events[label].set()
            await queued_releases[label].wait()
            return label

        return execute

    active_run = await manager.start(
        agent_id="coder", session_id="session-one", executor=active_execute, project_id=None
    )
    first_item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=make_executor("first"),
        display_content="first",
        project_id=None,
    )
    second_item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=make_executor("second"),
        display_content="second",
        project_id=None,
    )
    third_item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=make_executor("third"),
        display_content="third",
        project_id=None,
    )

    assert [
        item.display_content
        for item in manager.list_queued("coder", "session-one", project_id=None)
    ] == [
        "first",
        "second",
        "third",
    ]

    active_release.set()
    assert await active_run.wait() == "active"

    first_run = await asyncio.wait_for(first_item.future, timeout=1)
    await started_events["first"].wait()
    assert started == ["first"]
    queued_releases["first"].set()
    assert await first_run.wait() == "first"

    second_run = await asyncio.wait_for(second_item.future, timeout=1)
    await started_events["second"].wait()
    assert started == ["first", "second"]
    queued_releases["second"].set()
    assert await second_run.wait() == "second"

    third_run = await asyncio.wait_for(third_item.future, timeout=1)
    await started_events["third"].wait()
    assert started == ["first", "second", "third"]
    queued_releases["third"].set()
    assert await third_run.wait() == "third"


async def test_remove_queued_item_cancels_future_and_removes_from_queue() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    async def queued_execute(_run: Run) -> str:
        return "queued"

    active_run = await manager.start(
        agent_id="coder", session_id="session-one", executor=active_execute, project_id=None
    )
    item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=queued_execute,
        display_content="remove me",
        project_id=None,
    )

    assert manager.remove_queued("coder", "session-one", item.item_id, project_id=None) is True
    assert manager.list_queued("coder", "session-one", project_id=None) == []
    assert item.future.cancelled() is True
    assert manager.remove_queued("coder", "session-one", item.item_id, project_id=None) is False

    active_release.set()
    assert await active_run.wait() == "active"
    assert manager.active_run(agent_id="coder", session_id="session-one", project_id=None) is None


async def test_cancelling_queue_waiter_removes_item_and_prevents_execution() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()
    queued_executed = asyncio.Event()

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    async def queued_execute(_run: Run) -> str:
        queued_executed.set()
        return "queued"

    active_run = await manager.start(
        agent_id="coder", session_id="session-one", executor=active_execute, project_id=None
    )
    item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=queued_execute,
        display_content="abandoned",
        project_id=None,
    )

    async def wait_for_start() -> Run:
        return await item.future

    waiter = asyncio.create_task(wait_for_start())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await asyncio.sleep(0)

    assert item.future.cancelled() is True
    assert manager.list_queued("coder", "session-one", project_id=None) == []

    active_release.set()
    assert await active_run.wait() == "active"
    await asyncio.sleep(0)

    assert queued_executed.is_set() is False
    assert manager.active_run(agent_id="coder", session_id="session-one", project_id=None) is None


async def test_queue_drain_skips_future_cancelled_in_same_tick() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()
    queued_executed = asyncio.Event()
    queued_item: QueuedRunItem | None = None

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        assert queued_item is not None
        queued_item.future.cancel()
        return "active"

    async def queued_execute(_run: Run) -> str:
        queued_executed.set()
        return "queued"

    active_run = await manager.start(
        agent_id="coder", session_id="session-one", executor=active_execute, project_id=None
    )
    queued_item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=queued_execute,
        display_content="cancel during drain",
        project_id=None,
    )

    active_release.set()
    assert await active_run.wait() == "active"
    await asyncio.sleep(0)

    assert queued_item.future.cancelled() is True
    assert queued_executed.is_set() is False
    assert manager.list_queued("coder", "session-one", project_id=None) == []


async def test_update_queued_item_replaces_executor_and_display_content() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()
    updated_started = asyncio.Event()
    queued_release = asyncio.Event()
    executed: list[str] = []

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    async def original_execute(_run: Run) -> str:
        executed.append("original")
        return "original"

    async def updated_execute(_run: Run) -> str:
        executed.append("updated")
        updated_started.set()
        await queued_release.wait()
        return "updated"

    active_run = await manager.start(
        agent_id="coder", session_id="session-one", executor=active_execute, project_id=None
    )
    item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=original_execute,
        display_content="original",
        editable=True,
        project_id=None,
    )

    assert (
        manager.update_queued(
            "coder",
            "session-one",
            item.item_id,
            updated_execute,
            "updated",
            project_id=None,
            editable=False,
        )
        is True
    )
    assert (
        manager.list_queued("coder", "session-one", project_id=None)[0].display_content == "updated"
    )
    assert manager.list_queued("coder", "session-one", project_id=None)[0].editable is False
    assert (
        manager.update_queued(
            "coder", "session-one", "missing", updated_execute, "updated", project_id=None
        )
        is False
    )

    active_release.set()
    assert await active_run.wait() == "active"

    queued_run = await asyncio.wait_for(item.future, timeout=1)
    await updated_started.wait()
    assert executed == ["updated"]
    queued_release.set()
    assert await queued_run.wait() == "updated"


async def test_enqueue_race_condition_session_becomes_idle_between_error_and_enqueue() -> None:
    manager = ChatRunManager()
    active_release = asyncio.Event()
    queued_release = asyncio.Event()

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    async def queued_execute(_run: Run) -> str:
        await queued_release.wait()
        return "queued"

    active_run = await manager.start(
        agent_id="coder", session_id="session-one", executor=active_execute, project_id=None
    )

    with pytest.raises(ActiveRunError):
        await manager.start(
            agent_id="coder", session_id="session-one", executor=queued_execute, project_id=None
        )

    active_release.set()
    assert await active_run.wait() == "active"

    item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=queued_execute,
        display_content="race",
        project_id=None,
    )
    queued_run = await item.future

    assert queued_run.status == RunStatus.RUNNING
    assert manager.list_queued("coder", "session-one", project_id=None) == []

    queued_release.set()
    assert await queued_run.wait() == "queued"


async def test_chat_loop_queue_run_uses_display_preview_for_busy_session(tmp_path: Path) -> None:
    session_id = "session-one"
    active_release = asyncio.Event()
    agents = SimpleNamespace(
        get=lambda agent_id: SimpleNamespace(id=agent_id, model="openai/gpt-5.2")
    )
    runtime = SimpleNamespace(
        agents=agents,
        agent_resolver=SimpleNamespace(
            resolve_agent=lambda _project_id, agent_id: agents.get(agent_id)
        ),
        providers=SimpleNamespace(
            get=lambda provider_id: SimpleNamespace(connections=[SimpleNamespace(id="api-key")])
        ),
        provider_credentials=SimpleNamespace(
            has_credentials=lambda _provider_id, connection_id=None: (
                connection_id == "openai:api-key"
            ),
            is_usable=lambda _provider_id, connection_id=None: connection_id == "openai:api-key",
        ),
        models=SimpleNamespace(get=lambda _provider_id, _model_id: SimpleNamespace(connections=())),
        chat_sessions=ChatSessionManager(tmp_path),
        chat_runs=ChatRunManager(),
    )
    runtime.chat_run_manager = runtime.chat_runs
    runtime.chat_sessions.create("coder", session_id=session_id)

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    active_run = await runtime.chat_runs.start(
        agent_id="coder", session_id=session_id, executor=active_execute, project_id=None
    )

    item = await build_chat_loop(runtime).queue_run(
        "coder",
        "x" * 600,
        session_id=session_id,
    )

    assert item.display_content == "x" * 500
    assert item.editable is False
    assert runtime.chat_runs.list_queued("coder", session_id, project_id=None)[0] is item

    short_text_item = await build_chat_loop(runtime).queue_run(
        "coder",
        "short text",
        session_id=session_id,
    )

    assert short_text_item.display_content == "short text"
    assert short_text_item.editable is True

    attachment_item = await build_chat_loop(runtime).queue_run(
        "coder",
        [
            FileBlock(
                type="file",
                attachment_id="attachment-one",
                filename="report.pdf",
                media_type="application/pdf",
            )
        ],
        session_id=session_id,
    )

    assert attachment_item.display_content == "[attachment]"
    assert attachment_item.editable is False

    assert (
        runtime.chat_runs.remove_queued("coder", session_id, item.item_id, project_id=None) is True
    )
    assert (
        runtime.chat_runs.remove_queued(
            "coder", session_id, attachment_item.item_id, project_id=None
        )
        is True
    )
    assert (
        runtime.chat_runs.remove_queued(
            "coder", session_id, short_text_item.item_id, project_id=None
        )
        is True
    )
    active_release.set()
    assert await active_run.wait() == "active"


async def test_drained_queued_run_started_payload_contains_queue_item_id() -> None:
    """A queued item that gets drained carries its item id on the run_started payload."""
    manager = ChatRunManager()
    active_release = asyncio.Event()
    queued_release = asyncio.Event()

    async def active_execute(_run: Run) -> str:
        await active_release.wait()
        return "active"

    async def queued_execute(_run: Run) -> str:
        await queued_release.wait()
        return "queued"

    active_run = await manager.start(
        agent_id="coder", session_id="session-one", executor=active_execute, project_id=None
    )
    item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=queued_execute,
        display_content="Queued next",
        project_id=None,
    )

    active_release.set()
    assert await active_run.wait() == "active"

    queued_run = await asyncio.wait_for(item.future, timeout=1)
    await asyncio.sleep(0)

    started_events = [event for event in queued_run.events if event.type == RUN_STARTED_EVENT]
    assert len(started_events) == 1
    assert started_events[0].payload == {
        "status": RunStatus.RUNNING.value,
        "queue_item_id": item.item_id,
    }

    queued_release.set()
    assert await queued_run.wait() == "queued"


async def test_enqueue_idle_session_start_immediately_carries_queue_item_id() -> None:
    """enqueue on an idle session still tags run_started with the queued item id."""
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(_run: Run) -> str:
        await release.wait()
        return "done"

    item = await manager.enqueue(
        agent_id="coder",
        session_id="session-one",
        executor=execute,
        display_content="Hello",
        project_id=None,
    )
    run = await item.future
    await asyncio.sleep(0)

    started_events = [event for event in run.events if event.type == RUN_STARTED_EVENT]
    assert len(started_events) == 1
    assert started_events[0].payload == {
        "status": RunStatus.RUNNING.value,
        "queue_item_id": item.item_id,
    }

    release.set()
    assert await run.wait() == "done"
