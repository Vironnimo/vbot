"""Terminal manager: the operator's stream, controls, groups, and launch history."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, override

import pytest

import core.tools._terminal_state as terminal_state
from core.tools.terminal_manager import (
    TerminalClosedError,
    TerminalManager,
    TerminalManagerError,
    TerminalNotFoundError,
    TerminalProgramNotRunningError,
    TerminalRenderHost,
    TerminalStaleScreenError,
)
from tests.core.tools.terminal_manager_helpers import (
    TEST_ACTIVITY_QUIET_SECONDS,
    AdapterFactory,
    FakeClock,
    FakeTerminalAdapter,
    PendingTriggerService,
    eventually,
    owner,
    screen_shows,
    spawn,
    terminal_info,
)
from tests.core.tools.terminal_manager_helpers import clocked_manager as clocked_manager
from tests.core.tools.terminal_manager_helpers import default_shell as default_shell
from tests.core.tools.terminal_manager_helpers import terminal_manager as terminal_manager

Clocked = tuple[TerminalManager, AdapterFactory, PendingTriggerService, FakeClock]


async def _next_event(stream: Any) -> dict[str, Any]:
    return await asyncio.wait_for(anext(stream), timeout=1)


def _manager(adapter_factory: Any = None, **options: Any) -> TerminalManager:
    return TerminalManager(
        adapter_factory=adapter_factory or AdapterFactory(),
        render_host=TerminalRenderHost.in_process(),
        sweep_interval_seconds=3600,
        **options,
    )


@pytest.mark.asyncio
async def test_operator_stream_starts_with_ansi_snapshot_and_continues_in_sequence(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    started = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    adapter.emit(
        "\x1b]0;Codex auth refactor\x07"
        + "".join(f"history-{index}\r\n" for index in range(40))
        + "\x1b[31mREADY>\x1b[0m "
    )
    await eventually(lambda: screen_shows(manager, started.terminal_id, "READY>"))

    stream = manager.watch_for_operator(started.terminal_id)
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

    adapter.emit("\x1b]0;Codex tests\x07next")
    event = await _next_event(stream)
    while event["type"] != "terminal_output":
        assert event["sequence"] > ready["sequence"]
        event = await _next_event(stream)
    assert event["data"].endswith("next")
    assert event["sequence"] > ready["sequence"]
    state_event = await _next_event(stream)
    assert state_event["type"] == "terminal_state"
    assert state_event["terminal"]["title"] == "Codex tests"
    await stream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entered", "left", "hidden"),
    [
        ("PS> ", "\x1b[?1049h\x1b[2J\x1b[Hnvim\x1b[?1049lPS> ", "nvim"),
        ("\x1b[?2004hTUI", "\x1b[?2004lPS> ", None),
    ],
    ids=["alternate-screen-exit", "bracketed-paste-off"],
)
async def test_operator_stream_refreshes_the_authoritative_screen_when_a_program_leaves_a_mode(
    clocked_manager: Clocked,
    tmp_path: Path,
    entered: str,
    left: str,
    hidden: str | None,
) -> None:
    manager, factory, _trigger, clock = clocked_manager
    started = await spawn(manager, tmp_path)
    adapter = factory.adapters[0]
    adapter.emit(entered)
    await eventually(lambda: terminal_info(manager, started.terminal_id).screen_revision > 0)
    stream = manager.watch_for_operator(started.terminal_id)
    ready = await anext(stream)

    adapter.emit(left)
    events = [await _next_event(stream)]
    while events[-1]["type"] != "terminal_output":
        events.append(await _next_event(stream))
    # Leaving the alternate screen refreshes at once, leaving bracketed paste
    # at the quiet boundary.
    await eventually(lambda: clock.sleeping)
    await clock.advance(TEST_ACTIVITY_QUIET_SECONDS)
    while events[-1]["type"] != "terminal_snapshot":
        events.append(await _next_event(stream))
    snapshot = events[-1]

    output = next(event for event in events if event["type"] == "terminal_output")
    assert snapshot["sequence"] == output["sequence"] + 1
    assert snapshot["sequence"] > ready["sequence"]
    assert "PS>" in snapshot["ansi"]
    if hidden is not None:
        assert hidden not in snapshot["ansi"]
    assert (await manager.read_for_operator(started.terminal_id))["bracketed_paste"] is False
    await stream.aclose()


@pytest.mark.asyncio
async def test_operator_stream_publishes_final_snapshot_before_terminal_state(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    started = await spawn(manager, tmp_path)
    stream = manager.watch_for_operator(started.terminal_id)
    ready = await anext(stream)

    factory.adapters[0].emit("final screen")
    await eventually(lambda: screen_shows(manager, started.terminal_id, "final screen"))
    factory.adapters[0].finish(0)
    events: list[dict[str, Any]] = []
    while not any(
        event["type"] == "terminal_state" and event["terminal"]["state"] == "exited"
        for event in events
    ):
        events.append(await _next_event(stream))

    snapshot_index = next(
        index for index, event in enumerate(events) if event["type"] == "terminal_snapshot"
    )
    exited_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "terminal_state" and event["terminal"]["state"] == "exited"
    )
    assert snapshot_index < exited_index
    assert events[snapshot_index]["sequence"] > ready["sequence"]
    assert "final screen" in events[snapshot_index]["ansi"]
    await stream.aclose()


@pytest.mark.asyncio
async def test_terminal_snapshots_obey_byte_budget_and_reconnect(
    terminal_manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(terminal_state, "TERMINAL_STREAM_BYTE_LIMIT", 50_000)
    manager, factory = terminal_manager
    started = await spawn(manager, tmp_path)
    terminal_id = started.terminal_id
    adapter = factory.adapters[0]
    adapter.emit("".join("x" * 70 + "\r\n" for _ in range(250)))
    # Each return from the alternate screen publishes a snapshot of history and screen.
    for _ in range(20):
        adapter.emit("\x1b[?1049h\x1b[?1049l")
    await eventually(lambda: terminal_info(manager, terminal_id).screen_revision == 21)

    # The retained replay window has no public reader.
    events = manager._sessions[terminal_id]._stream.events
    snapshots = [event for event in events if event["type"] == "terminal_snapshot"]
    assert 0 < len(snapshots) < 20
    assert sum(terminal_state._stream_event_size(event) for event in events) <= 50_000
    stream = manager.watch_for_operator(terminal_id)
    reconnected = await anext(stream)
    assert reconnected["sequence"] == events[-1]["sequence"]
    assert reconnected["ansi"] == snapshots[-1]["ansi"]
    await stream.aclose()


@pytest.mark.asyncio
async def test_operator_controls_same_live_session_and_changed_callbacks(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, factory = terminal_manager
    changed: list[str] = []
    unsubscribe = manager.add_changed_callback(changed.append)
    started = await spawn(manager, tmp_path)
    terminal_id = started.terminal_id

    listed = manager.list_for_operator()
    assert [item["terminal_id"] for item in listed] == [terminal_id]
    assert changed == [terminal_id]

    result = await manager.send_operator_input(terminal_id, "hello\r")
    assert result["state"] == "working"
    assert factory.adapters[0].writes == ["hello\r"]

    resized = await manager.resize_for_operator(terminal_id, columns=90, rows=28)
    assert (resized["columns"], resized["rows"]) == (90, 28)
    assert factory.adapters[0].resizes == [(28, 90)]

    killed = await manager.kill_for_operator(terminal_id)
    assert killed["state"] == "exited"
    assert manager.list_for_operator()[0]["state"] == "exited"
    forgotten = manager.forget_for_operator(terminal_id)
    assert forgotten["state"] == "exited"
    assert manager.list_for_operator() == []
    assert changed.count(terminal_id) >= 5
    unsubscribe()


@pytest.mark.asyncio
async def test_operator_read_preserves_binding_and_rejects_stale_guarded_input(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, _factory = terminal_manager
    started = await manager.spawn(
        owner(), ["codex"], cwd=tmp_path, env=None, origin_run_id="run-live"
    )
    terminal_id = started.terminal_id
    read = await manager.read_for_operator(terminal_id)
    assert read["terminal"]["terminal_id"] == terminal_id
    assert terminal_info(manager, terminal_id).attachment == owner()
    # The read is no Agent observation: a later resize is no change from a screen
    # the Agent saw.
    await manager.resize_for_operator(terminal_id, columns=100, rows=30)
    assert "size_change" not in await manager.snapshot(terminal_id, owner())

    revision = read["terminal"]["screen_revision"]
    await manager.send_operator_input(terminal_id, "first", expected_screen_revision=revision)
    with pytest.raises(TerminalStaleScreenError):
        await manager.send_operator_input(terminal_id, "second", expected_screen_revision=revision)
    assert terminal_info(manager, terminal_id).attachment == owner()


@pytest.mark.asyncio
async def test_operator_input_expecting_a_program_writes_only_while_it_runs(
    tmp_path: Path,
) -> None:
    running: set[str] = {"codex"}
    probes: list[tuple[int, str]] = []

    def probe(pid: int, program: str) -> bool:
        probes.append((pid, program))
        return program in running

    factory = AdapterFactory()
    manager = _manager(factory, program_probe=probe)
    manager.start()
    try:
        started = await manager.spawn(
            owner(), ["pwsh"], cwd=tmp_path, env=None, origin_run_id="run-live"
        )
        terminal_id = started.terminal_id
        await manager.send_operator_input(
            terminal_id,
            "task",
            expected_program="codex",
            expected_screen_revision=started.screen_revision,
        )
        running.clear()
        with pytest.raises(TerminalProgramNotRunningError, match="codex is not running"):
            await manager.send_operator_input(terminal_id, "\r", expected_program="codex")
        # Input without an expected program never asks the process tree.
        await manager.send_operator_input(terminal_id, "dir\r")
        with pytest.raises(ValueError, match="expected_program"):
            await manager.send_operator_input(terminal_id, "x", expected_program=" ")
        assert factory.adapters[0].writes == ["task", "dir\r"]
        assert probes == [(factory.adapters[0].pid, "codex")] * 2
    finally:
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("default_shell")
async def test_operator_reads_whether_each_terminal_still_runs_its_launch_program(
    tmp_path: Path,
) -> None:
    running: set[str] = {"codex"}

    def probe(pid: int, program: str) -> bool:
        return program in running

    manager = _manager(program_probe=probe)
    manager.start()
    try:
        codex = await manager.spawn_for_operator(command="codex", arguments=[], cwd=tmp_path)
        claude = await manager.spawn_for_operator(
            command="C:\\tools\\claude.cmd", arguments=[], cwd=tmp_path
        )
        # A plain shell has no launch program to check.
        await manager.spawn_for_operator(command=None, arguments=[], cwd=tmp_path)

        assert await manager.running_programs_for_operator() == {
            codex["terminal_id"]: True,
            claude["terminal_id"]: False,
        }
        await manager.kill_for_operator(codex["terminal_id"])
        assert await manager.running_programs_for_operator() == {claude["terminal_id"]: False}
    finally:
        await manager.aclose()


class ExitStatusFailsOnceAdapter(FakeTerminalAdapter):
    """A process whose exit status cannot be read the first time it ends."""

    def __init__(self) -> None:
        super().__init__()
        self.status_failures = 1

    @override
    def exit_code(self) -> int | None:
        if self.status_failures:
            self.status_failures -= 1
            raise OSError("exit status unavailable")
        return super().exit_code()


@pytest.mark.asyncio
async def test_operator_kill_recovers_a_partially_recorded_finish(tmp_path: Path) -> None:
    adapter = ExitStatusFailsOnceAdapter()
    manager = _manager(lambda *_args, **_kwargs: adapter)
    try:
        started = await spawn(manager, tmp_path)
        terminal_id = started.terminal_id
        adapter.finish(5)
        # The end was noticed, but recording it failed: the terminal neither runs nor finished.
        await eventually(lambda: terminal_info(manager, terminal_id).finished_at is not None)
        assert terminal_info(manager, terminal_id).state == "ready"
        with pytest.raises(TerminalClosedError):
            await manager.send_operator_input(terminal_id, "x")

        result = await manager.kill_for_operator(terminal_id)

        assert (result["state"], result["exit_code"]) == ("exited", 5)
        assert manager.list_for_operator()[0]["state"] == "exited"
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_finished_operator_history_expires_with_a_catalog_change(tmp_path: Path) -> None:
    manager = _manager(finished_session_ttl=timedelta(microseconds=1))
    try:
        changed: list[str] = []
        manager.add_changed_callback(changed.append)
        started = await spawn(manager, tmp_path)
        await manager.kill_for_operator(started.terminal_id)
        changed.clear()

        async def expired() -> bool:
            await manager.sweep_finished()
            return manager.list_for_operator() == []

        await eventually(expired)
        assert changed == [started.terminal_id]
    finally:
        await manager.aclose()


@pytest.mark.asyncio
@pytest.mark.usefixtures("default_shell")
async def test_manual_launch_history_is_persistent_mru_and_deduplicated(tmp_path: Path) -> None:
    history_path = tmp_path / "terminals" / "launch-history.json"
    manager = _manager(launch_history_path=history_path, data_dir=tmp_path)
    manager.start()
    try:
        for command, arguments, workdir in [
            ("python", ["-m", "http.server", "8080"], "~/sites/docs"),
            ("codex", ["--profile", "work space"], "C:\\Development\\vBot"),
            ("python", ["-m", "http.server", "8080"], "~/sites/docs"),
        ]:
            await manager.spawn_for_operator(
                command=command, arguments=arguments, cwd=tmp_path, launch_workdir=workdir
            )

        history = manager.list_operator_launch_history()
        assert [entry["command"] for entry in history] == ["python", "codex"]
        assert history[0]["args"] == ["-m", "http.server", "8080"]
        assert history[0]["workdir"] == "~/sites/docs"
        assert len(history[0]["id"]) == 64
        assert history_path.is_file()
    finally:
        await manager.aclose()

    reloaded = TerminalManager(
        launch_history_path=history_path, data_dir=tmp_path, adapter_factory=AdapterFactory()
    )
    assert reloaded.list_operator_launch_history() == history


async def _spawn_manual(
    manager: TerminalManager, tmp_path: Path, *, group_id: str | None = None
) -> str:
    result = await manager.spawn_for_operator(
        command=None, arguments=[], cwd=tmp_path, group_id=group_id
    )
    return str(result["terminal_id"])


@pytest.mark.asyncio
async def test_user_groups_are_unique_persistent_and_renamable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="vbot")
    groups_path = tmp_path / "terminals" / "groups.json"
    manager = TerminalManager(
        groups_path=groups_path, data_dir=tmp_path, sweep_interval_seconds=3600
    )
    try:
        created = manager.create_group_for_operator("Work")
        assert (created["kind"], created["terminal_count"]) == ("user", 0)
        with pytest.raises(TerminalManagerError, match="already exists"):
            manager.create_group_for_operator("work")

        renamed = manager.rename_group_for_operator(created["group_id"], "Dev")
        assert renamed["name"] == "Dev"
        assert groups_path.is_file()
    finally:
        await manager.aclose()
    # Group names are user text; the log names groups by their ids only.
    messages = [record.getMessage() for record in caplog.records]
    assert any(created["group_id"] in message for message in messages)
    assert not any("Work" in message or "Dev" in message for message in messages)

    reloaded = TerminalManager(
        groups_path=groups_path, data_dir=tmp_path, sweep_interval_seconds=3600
    )
    try:
        groups = reloaded.list_groups_for_operator()
        assert [(group["name"], group["kind"]) for group in groups] == [("Dev", "user")]
    finally:
        await reloaded.aclose()


@pytest.mark.asyncio
async def test_agent_group_is_created_and_reused_by_name(tmp_path: Path) -> None:
    manager = TerminalManager(sweep_interval_seconds=3600)
    try:
        first = manager.resolve_or_create_agent_group("codex")
        second = manager.resolve_or_create_agent_group("codex")
        assert first.group_id == second.group_id
        assert first.kind == "agent"

        # An operator user group with the same name wins the reuse lookup.
        user = manager.create_group_for_operator("My Codex")
        reused = manager.resolve_or_create_agent_group("my codex")
        assert reused.group_id == user["group_id"]
    finally:
        await manager.aclose()


@pytest.mark.asyncio
async def test_spawned_terminals_join_explicit_and_automatic_groups(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, _factory = terminal_manager
    group = manager.create_group_for_operator("Work")
    agent_started = await spawn(manager, tmp_path)
    manual_id = await _spawn_manual(manager, tmp_path)
    grouped_id = await _spawn_manual(manager, tmp_path, group_id=group["group_id"])

    by_id = {summary["terminal_id"]: summary for summary in manager.list_for_operator()}
    assert by_id[agent_started.terminal_id]["group_id"] == "auto:agent:agent-a"
    assert by_id[manual_id]["group_id"] == "auto:manual"
    assert by_id[grouped_id]["group_id"] == group["group_id"]

    by_name = {item["name"]: item for item in manager.list_groups_for_operator()}
    assert by_name["Work"]["terminal_count"] == 1
    assert by_name["Manual"]["terminal_count"] == 1
    assert by_name["Agent agent-a"]["terminal_count"] == 1
    assert "finished" not in by_name


@pytest.mark.asyncio
async def test_killed_terminal_moves_to_finished_group_only_when_present(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, _factory = terminal_manager
    started = await spawn(manager, tmp_path)
    await manager.kill_for_operator(started.terminal_id)

    by_id = {summary["terminal_id"]: summary for summary in manager.list_for_operator()}
    assert by_id[started.terminal_id]["group_id"] == "finished"
    finished = [
        group for group in manager.list_groups_for_operator() if group["kind"] == "finished"
    ]
    assert [group["terminal_count"] for group in finished] == [1]

    manager.forget_for_operator(started.terminal_id)
    assert all(group["kind"] != "finished" for group in manager.list_groups_for_operator())


@pytest.mark.asyncio
async def test_group_order_is_persisted_and_new_terminals_append(tmp_path: Path) -> None:
    groups_path = tmp_path / "terminals" / "groups.json"
    manager = _manager(groups_path=groups_path, data_dir=tmp_path)
    manager.start()
    try:
        group_id = manager.create_group_for_operator("Work")["group_id"]
        first = await _spawn_manual(manager, tmp_path, group_id=group_id)
        second = await _spawn_manual(manager, tmp_path, group_id=group_id)

        manager.set_group_order_for_operator(group_id, [second, first])
        assert [item["terminal_id"] for item in manager.list_for_operator()] == [second, first]

        third = await _spawn_manual(manager, tmp_path, group_id=group_id)
        listed = [item["terminal_id"] for item in manager.list_for_operator()]
        assert listed == [second, first, third]

        with pytest.raises(TerminalManagerError, match="do not belong"):
            manager.set_group_order_for_operator(group_id, ["other-terminal"])
    finally:
        await manager.aclose()

    reloaded = TerminalManager(
        groups_path=groups_path, data_dir=tmp_path, sweep_interval_seconds=3600
    )
    assert reloaded.list_groups_for_operator()[0]["order"] == [second, first]


@pytest.mark.asyncio
async def test_deleting_a_group_kills_every_terminal_in_it(
    terminal_manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    manager, _factory = terminal_manager
    group_id = manager.create_group_for_operator("Work")["group_id"]
    first = await _spawn_manual(manager, tmp_path, group_id=group_id)
    second = await _spawn_manual(manager, tmp_path, group_id=group_id)
    outsider = await _spawn_manual(manager, tmp_path)

    result = await manager.delete_group_for_operator(group_id)
    assert result["terminals_killed"] == 2
    assert group_id not in {item["group_id"] for item in manager.list_groups_for_operator()}

    by_id = {summary["terminal_id"]: summary for summary in manager.list_for_operator()}
    assert [(by_id[item]["state"], by_id[item]["group_id"]) for item in (first, second)] == [
        ("exited", "finished"),
        ("exited", "finished"),
    ]
    assert by_id[outsider]["state"] != "exited"
    with pytest.raises(TerminalNotFoundError):
        await manager.delete_group_for_operator(group_id)
