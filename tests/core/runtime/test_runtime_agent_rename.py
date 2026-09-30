"""Identity Agent rename through the Runtime: every reference owner, rollback, recovery."""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from collections.abc import Callable
from typing import Any

import pytest

import core.agents._workspace as workspace_ops
from core.agents import AgentReferencedError
from core.runtime.runtime import Runtime
from core.sessions import SessionAddress
from core.utils.config import Config


class _Killed(BaseException):
    """The process dying at one step: no ``except Exception`` compensation runs."""


def _started(config: Config) -> Runtime:
    runtime = Runtime(config, safe_startup_mode="test")
    runtime.start()
    return runtime


async def _seed(runtime: Runtime, *, channel_enabled: bool = False) -> str:
    """Give ``coder`` a Session and one reference of every kind; return its current Session."""
    coder = runtime.agents.create("coder", "Coder")
    runtime.chat_sessions.create("coder", session_id="kept")
    channel_dir = runtime.storage.data_dir / "channels" / "tg-coder"
    channel_dir.mkdir(parents=True)
    (channel_dir / "channel.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "id": "tg-coder",
                "platform": "telegram",
                "agent_id": "coder",
                "dm_scope": "per_conversation",
                "allowed_chat_ids": [12345],
                "token_env_var": "TELEGRAM_BOT_TOKEN_TG_CODER",
                "enabled": channel_enabled,
            }
        ),
        encoding="utf-8",
    )
    await runtime.cron_service.create_job(
        agent_id="coder", prompt="Check in", schedule_type="interval", interval_seconds=3600
    )
    runtime.bootstrap_service.create_job(agent_id="coder", prompt="Verify", mode="once")
    event = runtime.calendar_service.create_event(title="Review", start="2026-10-01T09:00:00")
    await runtime.calendar_service.actions.add(
        event.id, when="start - 1h", prompt="Prepare", target="coder"
    )
    return coder.current_session_id


def _assert_agent_is(runtime: Runtime, agent_id: str, current_session_id: str) -> None:
    """The Agent and every reference to it name ``agent_id``, and the rename is finished."""
    assert sorted(agent.id for agent in runtime.agents.list()) == sorted(["main", agent_id])
    assert runtime.agents.get(agent_id).current_session_id == current_session_id
    assert runtime.chat_sessions.exists(SessionAddress(None, agent_id, "kept"))
    assert [channel.agent_id for channel in runtime.channel_service.list_channels()] == [agent_id]
    assert [job.agent_id for job in runtime.cron_service.list_jobs()] == [agent_id]
    assert [job.agent_id for job in runtime.bootstrap_service.list_jobs()] == [agent_id]
    assert [action["target"] for action in runtime.calendar_service.actions.list_actions()] == [
        agent_id
    ]
    assert not (runtime.storage.data_dir / "agents" / "rename-pending.json").exists()


def _record_channel_changes(
    runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> tuple[list[asyncio.AbstractEventLoop], list[str]]:
    """Record the loop of each Channel update and every adapter start."""
    loops: list[asyncio.AbstractEventLoop] = []
    started: list[str] = []
    service = runtime.channel_service
    update_channel = service.update_channel

    async def recorded(channel_id: str, **fields: Any) -> None:
        loops.append(asyncio.get_running_loop())
        await update_channel(channel_id, **fields)

    monkeypatch.setattr(service, "update_channel", recorded)
    monkeypatch.setattr(
        service, "start_channel", lambda channel_id, **_: started.append(channel_id)
    )
    return loops, started


def _fail_when_retargeted_to(
    service: Any, name: str, agent_id: str, error: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make ``service.name`` raise ``error`` whenever it moves a reference to ``agent_id``."""
    original = getattr(service, name)
    failing: Any
    if inspect.iscoroutinefunction(original):

        async def failing(reference: Any, target: str) -> Any:
            if target == agent_id:
                raise error
            return await original(reference, target)

    else:

        def failing(reference: Any, target: str) -> Any:
            if target == agent_id:
                raise error
            return original(reference, target)

    monkeypatch.setattr(service, name, failing)


def _record_threads(
    owner: Any, names: tuple[str, ...], threads: set[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Record the thread of every call of the ``names`` of ``owner``."""
    for name in names:
        original = getattr(owner, name)
        recorded: Any
        if inspect.iscoroutinefunction(original):

            async def recorded(*args: Any, _original: Any = original) -> Any:
                threads.add(threading.get_ident())
                return await _original(*args)

        else:

            def recorded(*args: Any, _original: Any = original) -> Any:
                threads.add(threading.get_ident())
                return _original(*args)

        monkeypatch.setattr(owner, name, recorded)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fails", "channels_running"),
    [
        pytest.param(False, True, id="completes"),
        pytest.param(True, True, id="reverts"),
        pytest.param(False, False, id="channels-stopped"),
    ],
)
async def test_a_live_rename_moves_every_reference_or_none(
    config: Config, monkeypatch: pytest.MonkeyPatch, fails: bool, channels_running: bool
) -> None:
    runtime = _started(config)
    try:
        # A stopped Channel service (the test startup mode never starts it) must
        # not start the adapter of an enabled Channel it retargets.
        current_session_id = await _seed(runtime, channel_enabled=not channels_running)
        if channels_running:
            runtime.channel_service.start()
        channel_loops, started_adapters = _record_channel_changes(runtime, monkeypatch)
        # Every read and change of the jobs and actions the running services keep
        # on the Event Loop happens there, never on the rename's worker.
        automation_threads: set[int] = set()
        for owner, names in (
            (runtime.cron_service, ("list_jobs", "retarget_agent_async", "_notify_changed")),
            (runtime.bootstrap_service, ("list_jobs", "retarget_agent")),
            (runtime.calendar_service.actions, ("list_actions", "retarget_identity_async")),
            (runtime.calendar_service, ("_notify_changed",)),
        ):
            _record_threads(owner, names, automation_threads, monkeypatch)
        if fails:
            # The last reference fails after every other one moved.
            _fail_when_retargeted_to(
                runtime.calendar_service.actions,
                "retarget_identity_async",
                "researcher",
                OSError("calendar storage is read-only"),
                monkeypatch,
            )
            with pytest.raises(OSError, match="read-only"):
                await runtime.rename_agent("coder", "researcher")
        else:
            outcome = await runtime.rename_agent("coder", "researcher")
            assert outcome.agent.id == "researcher"
            assert outcome.channel_ids == ("tg-coder",)
            assert len(outcome.cron_job_ids) == len(outcome.bootstrap_job_ids) == 1
            assert outcome.calendar_action_count == 1

        _assert_agent_is(runtime, "coder" if fails else "researcher", current_session_id)
        # The rename worker hands every change of a running Channel, forward and
        # back, to this Event Loop, which owns the Channel adapters.
        changes = (2 if fails else 1) if channels_running else 0
        assert channel_loops == [asyncio.get_running_loop()] * changes
        assert started_adapters == []
        assert automation_threads == {threading.get_ident()}
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_a_rename_to_an_id_that_references_still_name_changes_nothing(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _started(config)
    try:
        current_session_id = await _seed(runtime)
        # The rename and delete checks read every Channel config, off the Event Loop.
        channel_reads: set[int] = set()
        _record_threads(runtime.channel_service._storage, ("load_all",), channel_reads, monkeypatch)
        # A deleted ``researcher`` left a Cron job behind: the renamed Agent would
        # adopt it, and a revert would take it along. Its delegation grant went
        # with the deletion.
        runtime.agents.create("researcher", "Researcher")
        leftover = await runtime.cron_service.create_job(
            agent_id="researcher", prompt="Report", schedule_type="interval", interval_seconds=60
        )
        runtime.agents.update("coder", tools={"subagent": {"allowed_agents": ["researcher"]}})
        runtime.agents.delete("researcher")
        assert runtime.agents.get("coder").tools["subagent"]["allowed_agents"] == []

        with pytest.raises(AgentReferencedError) as refused:
            await runtime.rename_agent("coder", "researcher")

        assert refused.value.references == (f"cron:{leftover.id}",)
        assert await runtime.agent_references("researcher") == (f"cron:{leftover.id}",)
        assert len(channel_reads) > 0
        assert threading.get_ident() not in channel_reads
        assert sorted(agent.id for agent in runtime.agents.list()) == ["coder", "main"]
        assert runtime.agents.get("coder").current_session_id == current_session_id
        assert sorted(job.agent_id for job in runtime.cron_service.list_jobs()) == [
            "coder",
            "researcher",
        ]
        assert [channel.agent_id for channel in runtime.channel_service.list_channels()] == [
            "coder"
        ]
        assert not (runtime.storage.data_dir / "agents" / "rename-pending.json").exists()
    finally:
        await runtime.aclose()


def _kill_moving_agent_files(runtime: Runtime, patch: pytest.MonkeyPatch) -> None:
    def die(*_args: Any) -> bool:
        raise _Killed

    patch.setattr(workspace_ops, "_move_renamed_tree", die)


def _kill_moving_bootstrap_jobs(runtime: Runtime, patch: pytest.MonkeyPatch) -> None:
    _fail_when_retargeted_to(
        runtime.bootstrap_service, "retarget_agent", "researcher", _Killed(), patch
    )


def _kill_reverting_bootstrap_jobs(runtime: Runtime, patch: pytest.MonkeyPatch) -> None:
    _fail_when_retargeted_to(
        runtime.calendar_service.actions,
        "retarget_identity_async",
        "researcher",
        OSError("calendar storage is read-only"),
        patch,
    )
    _fail_when_retargeted_to(runtime.bootstrap_service, "retarget_agent", "coder", _Killed(), patch)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kill", "expected"),
    [
        # The next start finishes the Agent's own files before anything reads the roster:
        # the current Session, already moved, is not replaced by a fresh one.
        pytest.param(_kill_moving_agent_files, "researcher", id="agent-files"),
        pytest.param(_kill_moving_bootstrap_jobs, "researcher", id="references"),
        pytest.param(_kill_reverting_bootstrap_jobs, "coder", id="reverting"),
    ],
)
async def test_a_rename_killed_at_any_step_ends_on_the_next_start(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    kill: Callable[[Runtime, pytest.MonkeyPatch], None],
    expected: str,
) -> None:
    runtime = _started(config)
    try:
        current_session_id = await _seed(runtime)
        with monkeypatch.context() as patch:
            kill(runtime, patch)
            with pytest.raises(_Killed):
                await runtime.rename_agent("coder", "researcher")
    finally:
        await runtime.aclose()
    assert (config.data_dir / "agents" / "rename-pending.json").is_file()

    restarted = _started(config)
    try:
        _assert_agent_is(restarted, expected, current_session_id)
    finally:
        await restarted.aclose()
