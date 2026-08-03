"""Tests for Session-scoped interactive terminal lifecycle and attention."""

from __future__ import annotations

import asyncio
import queue
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

import core.tools.terminal_manager as terminal_module
from core.tools.terminal_manager import (
    TerminalCursorError,
    TerminalManager,
    TerminalNotFoundError,
    TerminalOwner,
    TerminalStaleScreenError,
)


class FakeTerminalAdapter:
    def __init__(self, initial_output: str | None = None) -> None:
        self._output: queue.Queue[str | None] = queue.Queue()
        if initial_output is not None:
            self._output.put(initial_output)
        self.writes: list[str] = []
        self.resizes: list[tuple[int, int]] = []
        self.alive = True
        self.code: int | None = None

    @property
    def pid(self) -> int:
        return 987_654

    def read(self, _size: int) -> str:
        try:
            value = self._output.get(timeout=0.05)
        except queue.Empty:
            return ""
        if value is None:
            raise EOFError
        return value

    def write(self, text: str) -> None:
        self.writes.append(text)

    def resize(self, rows: int, columns: int) -> None:
        self.resizes.append((rows, columns))

    def is_alive(self) -> bool:
        return self.alive

    def exit_code(self) -> int | None:
        return self.code

    def terminate(self) -> None:
        self.finish(-1)

    def emit(self, text: str) -> None:
        self._output.put(text)

    def finish(self, code: int = 0) -> None:
        if not self.alive:
            return
        self.code = code
        self.alive = False
        self._output.put(None)


class AdapterFactory:
    def __init__(self, initial_output: str | None = None) -> None:
        self.initial_output = initial_output
        self.adapters: list[FakeTerminalAdapter] = []
        self.calls: list[tuple[list[str], Path, dict[str, str], int, int]] = []

    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        rows: int,
        columns: int,
    ) -> FakeTerminalAdapter:
        adapter = FakeTerminalAdapter(self.initial_output)
        self.adapters.append(adapter)
        self.calls.append((list(argv), cwd, dict(env), rows, columns))
        return adapter


class PendingTriggerService:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.submissions: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.cancellations: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def submit_completion(self, *args: Any, **kwargs: Any) -> Any:
        self.submissions.append((args, kwargs))

        async def pending() -> None:
            await self.release.wait()

        return pending()

    def cancel_completion(self, *args: Any, **kwargs: Any) -> None:
        self.cancellations.append((args, kwargs))


@pytest_asyncio.fixture
async def terminal_manager() -> AsyncIterator[tuple[TerminalManager, AdapterFactory]]:
    factory = AdapterFactory()
    manager = TerminalManager(
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        yield manager, factory
    finally:
        await manager.aclose()


def owner(session_id: str = "session-a") -> TerminalOwner:
    return TerminalOwner("project-a", "agent-a", session_id)


async def spawn(
    manager: TerminalManager,
    tmp_path: Path,
    *,
    command: str = "fake-tui",
    initial_text: str | None = None,
) -> Any:
    return await manager.spawn(
        owner(),
        [command],
        cwd=tmp_path,
        env=None,
        columns=120,
        rows=32,
        origin_run_id="run-a",
        initial_text=initial_text,
    )


async def eventually(predicate: Any, *, attempts: int = 100) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition was not reached")


@pytest.mark.asyncio
async def test_initial_task_waits_for_tui_and_sends_enter_separately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "TERMINAL_INITIAL_INPUT_QUIET_SECONDS", 0.01)
    factory = AdapterFactory("READY> ")
    manager = TerminalManager(adapter_factory=factory, sweep_interval_seconds=3600)
    manager.start()
    try:
        session = await spawn(manager, tmp_path, initial_text="do the work")
        assert session.state == "starting"
        await eventually(lambda: factory.adapters[0].writes == ["do the work", "\r"])
        assert session.state == "working"
        assert manager.get_session(session.terminal_id, owner()) is session
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_sessions_are_owner_isolated_and_transfer_with_agent_move(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, _factory = terminal_manager
    session = await spawn(manager, tmp_path)
    other = owner("session-b")

    with pytest.raises(TerminalNotFoundError):
        manager.get_session(session.terminal_id, other)

    assert manager.transfer_scope(owner(), other) == 1
    assert manager.get_session(session.terminal_id, other) is session
    assert manager.list_sessions(owner()) == []


@pytest.mark.asyncio
async def test_screen_revision_guards_input_and_resize_updates_both_sides(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    adapter.emit("PROMPT> ")
    await eventually(lambda: session.renderer.revision > 0)

    with pytest.raises(TerminalStaleScreenError):
        await manager.send_input(
            session.terminal_id,
            owner(),
            text="answer",
            key=None,
            enter=True,
            expected_screen_revision=0,
            origin_run_id="run-b",
        )

    revision = session.renderer.revision
    await manager.send_input(
        session.terminal_id,
        owner(),
        text="answer",
        key=None,
        enter=True,
        expected_screen_revision=revision,
        origin_run_id="run-b",
    )
    assert adapter.writes == ["answer", "\r"]

    result = await manager.resize(session.terminal_id, owner(), columns=100, rows=24)
    assert adapter.resizes == [(24, 100)]
    assert result["columns"] == 100
    assert result["rows"] == 24


@pytest.mark.asyncio
async def test_operator_stream_starts_with_ansi_snapshot_and_continues_in_sequence(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    adapter.emit("\x1b[31mREADY>\x1b[0m ")
    await eventually(lambda: session.renderer.revision > 0)

    stream = manager.watch_for_operator(session.terminal_id)
    ready = await anext(stream)
    assert ready["type"] == "terminal_ready"
    assert ready["terminal"]["owner"] == {
        "project_id": "project-a",
        "agent_id": "agent-a",
        "session_id": "session-a",
    }
    assert "READY>" in ready["ansi"]
    assert "\x1b[2J" in ready["ansi"]

    next_event = asyncio.create_task(anext(stream))
    adapter.emit("next")
    event = await asyncio.wait_for(next_event, timeout=1)
    while event["type"] != "terminal_output":
        assert event["sequence"] > ready["sequence"]
        event = await asyncio.wait_for(anext(stream), timeout=1)
    assert event["data"] == "next"
    assert event["sequence"] > ready["sequence"]
    await stream.aclose()


@pytest.mark.asyncio
async def test_operator_controls_same_live_session_and_changed_callbacks(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    changed: list[str] = []
    unsubscribe = manager.add_changed_callback(changed.append)
    session = await spawn(manager, tmp_path)

    listed = manager.list_active_for_operator()
    assert [item["terminal_id"] for item in listed] == [session.terminal_id]
    assert changed == [session.terminal_id]

    result = await manager.send_operator_input(session.terminal_id, "hello\r")
    assert result["state"] == "working"
    assert factory.adapters[0].writes == ["hello\r"]

    resized = await manager.resize_for_operator(session.terminal_id, columns=90, rows=28)
    assert resized["columns"] == 90
    assert resized["rows"] == 28
    assert factory.adapters[0].resizes == [(28, 90)]

    killed = await manager.kill_for_operator(session.terminal_id)
    assert killed["state"] == "exited"
    assert manager.list_active_for_operator() == []
    assert changed.count(session.terminal_id) >= 4
    unsubscribe()


@pytest.mark.asyncio
async def test_operator_kill_recovers_a_partially_recorded_finish(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, _factory = terminal_manager
    session = await spawn(manager, tmp_path)
    session.finished_at = session.started_at

    result = await manager.kill_for_operator(session.terminal_id)

    assert result["state"] == "exited"
    assert manager.list_active_for_operator() == []


@pytest.mark.asyncio
async def test_status_is_bounded_and_scrollback_cursor_is_signed(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    factory.adapters[0].emit("".join(f"line-{index}\r\n" for index in range(50)))
    await eventually(lambda: session.renderer.revision > 0)

    snapshot = await manager.snapshot(session.terminal_id, owner(), lines=3)
    assert snapshot["scrollback"]["line_count"] == 3
    before = snapshot["scrollback"]["next_before"]
    assert isinstance(before, int)
    cursor = manager.encode_cursor(session.terminal_id, before)
    assert manager.decode_cursor(cursor, session.terminal_id) == before
    with pytest.raises(TerminalCursorError):
        manager.decode_cursor(cursor + "x", session.terminal_id)


@pytest.mark.asyncio
async def test_launch_passes_every_program_exact_argv_without_private_environment(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await manager.spawn(
        owner(),
        ["codex", "--profile", "work"],
        cwd=tmp_path,
        env={"CALLER_VALUE": "unchanged"},
        columns=120,
        rows=32,
        origin_run_id="run-a",
    )
    launch_argv, _cwd, launch_env, _rows, _columns = factory.calls[0]
    assert launch_argv == ["codex", "--profile", "work"]
    assert launch_env["CALLER_VALUE"] == "unchanged"
    assert not any(name.startswith("VBOT_TERMINAL_") for name in launch_env)
    assert not hasattr(session, "codex_integration")


@pytest.mark.asyncio
async def test_exact_agent_data_and_named_keys_share_the_generic_pty(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)

    raw = "\x1b[200~paste\r\n\x1b[201~"
    sent = await manager.send_input(
        session.terminal_id,
        owner(),
        data=raw,
        text=None,
        key=None,
        enter=False,
        expected_screen_revision=None,
        origin_run_id="run-b",
    )
    await manager.send_input(
        session.terminal_id,
        owner(),
        data=None,
        text=None,
        key="f12",
        enter=False,
        expected_screen_revision=None,
        origin_run_id="run-b",
    )

    assert factory.adapters[0].writes == [raw, "\x1b[24~"]
    assert sent["characters_sent"] == len(raw)


@pytest.mark.asyncio
async def test_operator_activity_settles_without_automatic_agent_wakeup(tmp_path: Path) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        session = await spawn(manager, tmp_path)
        await manager.send_operator_input(session.terminal_id, "look\r")
        factory.adapters[0].emit("screen changed")
        await eventually(lambda: session.attention_revision == 1)

        assert session.state == "ready"
        assert session.attention is not None
        assert session.attention.kind == "output_settled"
        assert trigger.submissions == []
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_wait_exit_and_explicit_kill_have_distinct_attention(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    natural = await spawn(manager, tmp_path)
    factory.adapters[0].finish(7)
    await eventually(lambda: natural.state == "exited")
    snapshot, timed_out = await manager.wait_for_attention(
        natural.terminal_id, owner(), after_revision=0, timeout_ms=10
    )
    assert not timed_out
    assert snapshot["attention"]["kind"] == "exited"
    assert snapshot["exit_code"] == 7

    killed = await spawn(manager, tmp_path)
    result = await manager.kill(killed.terminal_id, owner())
    assert result["state"] == "exited"
    assert result["attention"] is None


@pytest.mark.asyncio
async def test_attention_auto_delivers_and_manual_ack_cancels_exactly_once(
    tmp_path: Path,
) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        session = await spawn(manager, tmp_path)
        await manager.send_input(
            session.terminal_id,
            owner(),
            data=None,
            text="do work",
            key=None,
            enter=True,
            expected_screen_revision=None,
            origin_run_id="run-b",
        )
        factory.adapters[0].emit("working...\r\nREADY> ")
        await eventually(lambda: len(trigger.submissions) == 1)

        args, kwargs = trigger.submissions[0]
        assert args == ("agent-a", "session-a")
        assert kwargs["origin_run_id"] == "run-b"
        assert kwargs["project_id"] == "project-a"
        assert "Terminal output settled" in kwargs["body"]
        assert "does not imply" in kwargs["body"]
        assert "Reuse this Terminal Session" in kwargs["body"]
        assert session.attention is not None
        assert session.attention.kind == "output_settled"

        manager.acknowledge_attention(session.terminal_id, owner(), 1)
        await asyncio.sleep(0)
        assert len(trigger.cancellations) == 1
        assert session.acknowledged_attention_revision == 1
        assert session.notification_task is not None
        assert session.notification_task.cancelled()
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_new_output_postpones_a_pending_agent_wakeup_to_the_next_quiet_boundary(
    tmp_path: Path,
) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        session = await spawn(manager, tmp_path)
        await manager.send_input(
            session.terminal_id,
            owner(),
            data="begin",
            text=None,
            key=None,
            enter=False,
            expected_screen_revision=None,
            origin_run_id="run-b",
        )
        await eventually(lambda: len(trigger.submissions) == 1)

        factory.adapters[0].emit("late output")
        await eventually(lambda: len(trigger.cancellations) == 1)
        await eventually(lambda: len(trigger.submissions) == 2)

        assert session.attention_revision == 2
        assert session.attention is not None
        assert session.attention.kind == "output_settled"
        assert trigger.submissions[0][1]["notice_id"] != trigger.submissions[1][1]["notice_id"]
        assert trigger.submissions[1][1]["origin_run_id"] == "run-b"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_session_move_reroutes_pending_attention_to_new_owner(tmp_path: Path) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    manager.start()
    try:
        session = await spawn(manager, tmp_path)
        await manager.send_input(
            session.terminal_id,
            owner(),
            data=None,
            text="do work",
            key=None,
            enter=True,
            expected_screen_revision=None,
            origin_run_id="run-b",
        )
        await eventually(lambda: len(trigger.submissions) == 1)
        target = TerminalOwner("project-b", "agent-b", "session-a")

        assert manager.transfer_scope(owner(), target) == 1
        await eventually(lambda: len(trigger.submissions) == 2)

        assert trigger.submissions[0][0] == ("agent-a", "session-a")
        assert trigger.submissions[1][0] == ("agent-b", "session-a")
        assert trigger.submissions[1][1]["project_id"] == "project-b"
        assert len(trigger.cancellations) == 1
        assert manager.get_session(session.terminal_id, target) is session
    finally:
        await manager.aclose()
