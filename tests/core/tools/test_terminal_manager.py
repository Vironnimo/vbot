"""Tests for Session-scoped interactive terminal lifecycle and attention."""

from __future__ import annotations

import asyncio
import json
import queue
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

import core.tools.terminal_manager as terminal_module
from core.tools.terminal_hook_sink import TERMINAL_HOOK_EVENT_VERSION
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
    manager = TerminalManager(adapter_factory=factory, sweep_interval_seconds=3600)
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
async def test_codex_hooks_project_question_approval_and_turn_completion(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path, command="codex")
    assert session.event_nonce is not None
    launch_argv, _cwd, launch_env, _rows, _columns = factory.calls[0]
    assert "--no-alt-screen" in launch_argv
    assert "hooks.Stop=" in " ".join(launch_argv)
    assert launch_env["VBOT_TERMINAL_EVENT_NONCE"] == session.event_nonce

    question = {
        "hook_event_name": "PreToolUse",
        "session_id": "codex-session",
        "turn_id": "turn-1",
        "tool_use_id": "question-1",
        "tool_name": "request_user_input",
        "tool_input": {"questions": [{"question": "red or blue?"}]},
    }
    await manager._consume_hook_record(session, _record(session, question))
    await manager._consume_hook_record(session, _record(session, question))
    assert session.state == "needs_input"
    assert session.attention_revision == 1
    assert session.attention is not None
    assert session.attention.kind == "question"
    assert session.attention.details["questions"] == [{"question": "red or blue?"}]

    approval = {
        "hook_event_name": "PermissionRequest",
        "session_id": "codex-session",
        "turn_id": "turn-1",
        "tool_use_id": "approval-1",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hello"},
    }
    await manager._consume_hook_record(session, _record(session, approval))
    assert session.attention is not None
    assert session.attention.kind == "approval"
    assert session.attention.details["tool_input"] == {"command": "echo hello"}

    complete = {
        "hook_event_name": "Stop",
        "session_id": "codex-session",
        "turn_id": "turn-1",
        "last_assistant_message": "Finished cleanly.",
    }
    await manager._consume_hook_record(session, _record(session, complete))
    assert session.state == "turn_complete"
    assert session.adapter.is_alive()
    assert session.attention is not None
    assert session.attention.details["final_message"] == "Finished cleanly."


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
    manager = TerminalManager(trigger, adapter_factory=factory, sweep_interval_seconds=3600)
    manager.start()
    try:
        session = await spawn(manager, tmp_path, command="codex")
        complete = {
            "hook_event_name": "Stop",
            "session_id": "codex-session",
            "turn_id": "turn-1",
            "last_assistant_message": "Done.",
        }
        await manager._consume_hook_record(session, _record(session, complete))
        await eventually(lambda: len(trigger.submissions) == 1)

        args, kwargs = trigger.submissions[0]
        assert args == ("agent-a", "session-a")
        assert kwargs["origin_run_id"] == "run-a"
        assert kwargs["project_id"] == "project-a"
        assert "Codex turn complete" in kwargs["body"]
        assert "Terminal Session remains open" in kwargs["body"]

        manager.acknowledge_attention(session.terminal_id, owner(), 1)
        await asyncio.sleep(0)
        assert len(trigger.cancellations) == 1
        assert session.acknowledged_attention_revision == 1
        assert session.notification_task is not None
        assert session.notification_task.cancelled()
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_session_move_reroutes_pending_attention_to_new_owner(tmp_path: Path) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(trigger, adapter_factory=factory, sweep_interval_seconds=3600)
    manager.start()
    try:
        session = await spawn(manager, tmp_path, command="codex")
        await manager._consume_hook_record(
            session,
            _record(
                session,
                {
                    "hook_event_name": "Stop",
                    "session_id": "codex-session",
                    "turn_id": "turn-1",
                    "last_assistant_message": "Done.",
                },
            ),
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


def _record(session: Any, event: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "version": TERMINAL_HOOK_EVENT_VERSION,
            "nonce": session.event_nonce,
            "event": event,
        }
    ).encode("utf-8")
