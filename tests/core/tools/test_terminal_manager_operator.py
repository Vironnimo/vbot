"""Terminal manager: operator behavior."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import core.tools._terminal_state as terminal_state
import core.tools.terminal_manager as terminal_module
from core.tools.terminal_manager import (
    TerminalClosedError,
    TerminalManager,
    TerminalNotOwnedError,
)
from tests.core.tools.terminal_manager_helpers import (
    AdapterFactory,
    PendingTriggerService,
    eventually,
    owner,
    spawn,
)
from tests.core.tools.terminal_manager_helpers import (
    terminal_manager as terminal_manager,
)


@pytest.mark.asyncio
async def test_operator_stream_starts_with_ansi_snapshot_and_continues_in_sequence(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    adapter.emit(
        "\x1b]0;Codex auth refactor\x07"
        + "".join(f"history-{index}\r\n" for index in range(40))
        + "\x1b[31mREADY>\x1b[0m "
    )
    await eventually(lambda: session.renderer.page(before=None, limit=100)["line_count"] > 0)

    stream = manager.watch_for_operator(session.terminal_id)
    ready = await anext(stream)
    assert ready["type"] == "terminal_ready"
    assert ready["terminal"]["owner"] == {
        "project_id": "project-a",
        "agent_id": "agent-a",
        "session_id": "session-a",
    }
    assert ready["terminal"]["title"] == "Codex auth refactor"
    assert "history-0" in ready["ansi"]
    assert "READY>" in ready["ansi"]
    assert "\x1b[2J" in ready["ansi"]

    next_event = asyncio.create_task(anext(stream))
    adapter.emit("\x1b]0;Codex tests\x07next")
    event = await asyncio.wait_for(next_event, timeout=1)
    while event["type"] != "terminal_output":
        assert event["sequence"] > ready["sequence"]
        event = await asyncio.wait_for(anext(stream), timeout=1)
    assert event["data"].endswith("next")
    assert event["sequence"] > ready["sequence"]
    state_event = await asyncio.wait_for(anext(stream), timeout=1)
    assert state_event["type"] == "terminal_state"
    assert state_event["terminal"]["title"] == "Codex tests"
    await stream.aclose()


@pytest.mark.asyncio
async def test_operator_stream_refreshes_authoritative_screen_after_alternate_screen_exit(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    adapter.emit("PS> ")
    await eventually(lambda: session.renderer.screen_text() == "PS>")
    stream = manager.watch_for_operator(session.terminal_id)
    ready = await anext(stream)

    adapter.emit("\x1b[?1049h\x1b[2J\x1b[Hnvim\x1b[?1049lPS> ")
    output = await asyncio.wait_for(anext(stream), timeout=1)
    while output["type"] != "terminal_output":
        output = await asyncio.wait_for(anext(stream), timeout=1)
    snapshot = await asyncio.wait_for(anext(stream), timeout=1)

    assert output["type"] == "terminal_output"
    assert snapshot["type"] == "terminal_snapshot"
    assert snapshot["sequence"] == output["sequence"] + 1
    assert snapshot["sequence"] > ready["sequence"]
    assert "PS>" in snapshot["ansi"]
    assert "nvim" not in snapshot["ansi"]
    await stream.aclose()


@pytest.mark.asyncio
async def test_operator_stream_refreshes_after_tui_disables_bracketed_paste(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    adapter.emit("\x1b[?2004hTUI")
    await eventually(lambda: session.renderer.bracketed_paste_enabled)
    stream = manager.watch_for_operator(session.terminal_id)
    ready = await anext(stream)

    adapter.emit("\x1b[?2004lPS> ")
    snapshot: dict[str, Any] | None = None
    while snapshot is None:
        event = await asyncio.wait_for(anext(stream), timeout=1)
        if event["type"] == "terminal_snapshot":
            snapshot = event

    assert snapshot["sequence"] > ready["sequence"]
    assert "PS>" in snapshot["ansi"]
    assert session.renderer.bracketed_paste_enabled is False
    await stream.aclose()


@pytest.mark.asyncio
async def test_operator_stream_publishes_final_snapshot_before_terminal_state(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    stream = manager.watch_for_operator(session.terminal_id)
    ready = await anext(stream)

    factory.adapters[0].emit("final screen")
    await eventually(lambda: "final screen" in session.renderer.screen_text())
    factory.adapters[0].finish(0)
    events: list[dict[str, Any]] = []
    while not any(
        event["type"] == "terminal_state" and event["terminal"]["state"] == "exited"
        for event in events
    ):
        events.append(await asyncio.wait_for(anext(stream), timeout=1))

    terminal_snapshot_index = next(
        index for index, event in enumerate(events) if event["type"] == "terminal_snapshot"
    )
    terminal_state_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "terminal_state" and event["terminal"]["state"] == "exited"
    )
    assert terminal_snapshot_index < terminal_state_index
    assert events[terminal_snapshot_index]["sequence"] > ready["sequence"]
    assert "final screen" in events[terminal_snapshot_index]["ansi"]
    await stream.aclose()


@pytest.mark.asyncio
async def test_operator_controls_same_live_session_and_changed_callbacks(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    changed: list[str] = []
    unsubscribe = manager.add_changed_callback(changed.append)
    session = await spawn(manager, tmp_path)

    listed = manager.list_for_operator()
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
    assert manager.list_for_operator()[0]["state"] == "exited"
    forgotten = manager.forget_for_operator(session.terminal_id)
    assert forgotten["state"] == "exited"
    assert manager.list_for_operator() == []
    assert changed.count(session.terminal_id) >= 5
    unsubscribe()


@pytest.mark.asyncio
async def test_closed_pty_write_marks_terminal_exited_and_delivers_attention(
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
        manager.attach(session.terminal_id, owner(), origin_run_id="attach-run")
        factory.adapters[0].write_error = EOFError("Pty is closed")

        with pytest.raises(TerminalClosedError):
            await manager.send_operator_input(session.terminal_id, "late input")

        await eventually(lambda: len(trigger.submissions) == 1)
        assert session.state == "exited"
        assert session.attention is not None
        assert session.attention.kind == "exited"
        assert trigger.submissions[0][1]["origin_run_id"] == "attach-run"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_unattached_operator_terminal_has_no_agent_scope_or_attention_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda env: ["host-shell"])
    manager.start()
    try:
        result = await manager.spawn_for_operator(
            command=None,
            arguments=["--login"],
            cwd=tmp_path,
        )
        terminal_id = result["terminal_id"]
        session = manager._sessions[terminal_id]

        assert factory.calls[0][0] == ["host-shell", "--login"]
        assert result["owner"] is None
        assert result["attachment"] is None
        assert session.owner is None
        assert session.lifecycle_owner is None
        assert session.attachment is None
        assert manager.list_sessions() == [session]

        await manager.close_project_scope("project-a")
        assert factory.adapters[0].alive is True

        await manager.send_operator_input(terminal_id, "echo ready\r")
        factory.adapters[0].emit("ready\r\n")
        await eventually(lambda: session.state == "ready")
        assert trigger.submissions == []

        factory.adapters[0].finish(0)
        await eventually(lambda: session.state == "exited")
        assert trigger.submissions == []
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_operator_terminal_attach_delivers_activity_and_detach_preserves_lifetime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trigger = PendingTriggerService()
    factory = AdapterFactory()
    manager = TerminalManager(
        trigger,
        adapter_factory=factory,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda env: ["host-shell"])
    manager.start()
    try:
        result = await manager.spawn_for_operator(
            command=None, arguments=[], cwd=tmp_path, columns=137, rows=41
        )
        terminal_id = result["terminal_id"]
        session = manager._sessions[terminal_id]

        attached, changed = manager.attach(terminal_id, owner(), origin_run_id="attach-run")
        assert attached is session
        assert changed is True
        assert session.owner is None
        assert session.lifecycle_owner is None
        assert session.attachment == owner()
        assert manager.get_session(terminal_id, owner()) is session
        assert (session.renderer.columns, session.renderer.rows) == (137, 41)
        assert factory.adapters[0].resizes == []

        same, changed = manager.attach(terminal_id, owner(), origin_run_id="attach-run-2")
        assert same is session
        assert changed is False
        assert (session.renderer.columns, session.renderer.rows) == (137, 41)
        assert factory.adapters[0].resizes == []

        await manager.send_operator_input(terminal_id, "echo ready\r")
        factory.adapters[0].emit("ready\r\n")
        await eventually(lambda: len(trigger.submissions) == 1)
        assert trigger.submissions[0][0] == ("agent-a", "session-a")
        assert trigger.submissions[0][1]["origin_run_id"] == "attach-run-2"

        assert manager.detach(terminal_id, owner()) is session
        assert session.attachment is None
        assert factory.adapters[0].alive is True
        with pytest.raises(TerminalNotOwnedError):
            manager.get_session(terminal_id, owner())

        await manager.close_scope(owner())
        assert factory.adapters[0].alive is True
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_attach_arms_an_already_working_terminal_for_its_next_quiet_boundary(
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
        result = await manager.spawn_for_operator(command=None, arguments=[], cwd=tmp_path)
        session = manager._sessions[result["terminal_id"]]
        session.state = "working"

        manager.attach(session.terminal_id, owner(), origin_run_id="attach-run")

        await eventually(lambda: len(trigger.submissions) == 1)
        assert session.attention is not None
        assert session.attention.kind == "output_settled"
        assert trigger.submissions[0][1]["origin_run_id"] == "attach-run"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_attach_rejects_another_session_and_detached_agent_origin_still_owns_lifecycle(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    session = await spawn(manager, tmp_path)
    other = owner("session-b")

    with pytest.raises(terminal_module.TerminalAlreadyAttachedError):
        manager.attach(session.terminal_id, other, origin_run_id="run-b")

    manager.detach(session.terminal_id, owner())
    attached, changed = manager.attach(session.terminal_id, other, origin_run_id="run-b")
    assert attached is session
    assert changed is True
    assert session.owner == owner()
    assert session.lifecycle_owner == owner()
    assert session.attachment == other

    await manager.close_scope(other)
    assert factory.adapters[0].alive is True
    assert session.attachment is None

    await manager.close_scope(owner())
    assert factory.adapters[0].alive is False


@pytest.mark.asyncio
async def test_session_move_transfers_operator_terminal_attachment_not_lifecycle(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    result = await manager.spawn_for_operator(command=None, arguments=[], cwd=tmp_path)
    session = manager._sessions[result["terminal_id"]]
    target = owner("session-b")
    manager.attach(session.terminal_id, owner(), origin_run_id="run-a")

    assert manager.transfer_scope(owner(), target) == 1
    assert session.owner is None
    assert session.lifecycle_owner is None
    assert session.attachment == target
    assert manager.get_session(session.terminal_id, target) is session

    await manager.close_scope(target)
    assert session.attachment is None
    assert factory.adapters[0].alive is True


@pytest.mark.asyncio
async def test_manual_launch_history_is_persistent_mru_and_deduplicated(tmp_path: Path) -> None:
    history_path = tmp_path / "terminals" / "launch-history.json"
    factory = AdapterFactory()
    manager = TerminalManager(
        adapter_factory=factory,
        launch_history_path=history_path,
        data_dir=tmp_path,
        sweep_interval_seconds=3600,
    )
    manager.start()
    try:
        await manager.spawn_for_operator(
            command="python",
            arguments=["-m", "http.server", "8080"],
            cwd=tmp_path,
            launch_workdir="~/sites/docs",
        )
        await manager.spawn_for_operator(
            command="codex",
            arguments=["--profile", "work space"],
            cwd=tmp_path,
            launch_workdir="C:\\Development\\vBot",
        )
        await manager.spawn_for_operator(
            command="python",
            arguments=["-m", "http.server", "8080"],
            cwd=tmp_path,
            launch_workdir="~/sites/docs",
        )

        history = manager.list_operator_launch_history()
        assert len(history) == 2
        assert history[0]["command"] == "python"
        assert history[0]["args"] == ["-m", "http.server", "8080"]
        assert history[0]["workdir"] == "~/sites/docs"
        assert len(history[0]["id"]) == 64
        assert history[1]["command"] == "codex"
        assert history_path.is_file()
    finally:
        await manager.aclose()

    reloaded = TerminalManager(
        launch_history_path=history_path,
        data_dir=tmp_path,
        adapter_factory=AdapterFactory(),
    )
    assert reloaded.list_operator_launch_history() == history


@pytest.mark.asyncio
async def test_operator_kill_recovers_a_partially_recorded_finish(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, _factory = terminal_manager
    session = await spawn(manager, tmp_path)
    session.finished_at = session.started_at

    result = await manager.kill_for_operator(session.terminal_id)

    assert result["state"] == "exited"
    assert manager.list_for_operator()[0]["state"] == "exited"


@pytest.mark.asyncio
async def test_finished_operator_history_expires_with_a_catalog_change(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, _factory = terminal_manager
    changed: list[str] = []
    manager.add_changed_callback(changed.append)
    session = await spawn(manager, tmp_path)
    await manager.kill_for_operator(session.terminal_id)
    session.finished_at = (
        terminal_state._utc_now() - terminal_module.TERMINAL_FINISHED_TTL - timedelta(seconds=1)
    )
    changed.clear()

    await manager.sweep_finished()

    assert manager.list_for_operator() == []
    assert changed == [session.terminal_id]


@pytest.mark.asyncio
async def test_terminal_snapshots_obey_byte_budget_and_reconnect(
    terminal_manager, tmp_path, monkeypatch
):
    monkeypatch.setattr(terminal_state, "TERMINAL_STREAM_BYTE_LIMIT", 50_000)
    manager, _ = terminal_manager
    session = await manager.spawn(owner(), ["fake"], cwd=tmp_path, env=None, origin_run_id="run")
    session.renderer.feed("".join("x" * 70 + "\r\n" for _ in range(250)))
    for _ in range(20):
        manager._events._publish_snapshot(session)
    events = session.stream.events
    assert len(events) < 20
    assert sum(terminal_state._stream_event_size(event) for event in events) <= 50_000
    stream = manager.watch_for_operator(session.terminal_id)
    snapshot = await anext(stream)
    assert snapshot["sequence"] == session.stream_sequence
    assert snapshot["ansi"] == session.renderer.ansi_snapshot()
    await stream.aclose()
