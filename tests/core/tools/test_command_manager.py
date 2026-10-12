"""CommandManager: one-shot commands on pipes, their end, stop, delivery and lifecycle.

Most cases drive a fake process and tree on a fake clock. The real-process cases
check what only the operating system can show: no input and no terminal, the
Job that contains every process from creation on, and kills from outside vBot.
They run on a clock ten times faster than real time, so the end-detection
pauses cost a tenth of their nominal length.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import psutil  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from core.runs import RunExecutionOwner
from core.sessions import SessionAddress
from core.storage.temp_files import TemporaryFileManager
from core.tools.command_manager import (
    COMMAND_HEAD_LINES,
    COMMAND_MEMORY_LINE_MAX_CHARS,
    COMMAND_RETENTION_SECONDS,
    COMMAND_TEMPORARY_CATEGORY,
    CommandCapacityError,
    CommandManager,
    CommandNotFoundError,
    CommandNotStartedError,
    CommandStopError,
)
from core.tools.process_tree import (
    ProcessTree,
    ProgramExit,
    RunningProcess,
    track_process_tree,
)
from tests.core.tools.command_manager_helpers import (
    OWNER,
    FakeSink,
    FakeTree,
    Harness,
    command_harness,
    format_report,
    turns,
)

CHILD = SessionAddress("project-a", "sub-agent", "session-child")
OTHER = SessionAddress("project-a", "agent-b", "session-b")
GROUP = RunExecutionOwner("ext", "group-1", "participant", "generation", "epoch-1")
SERVER = RunningProcess(5001, "server.exe")
CONSOLE_HOST = RunningProcess(5002, "conhost.exe")
WORKER = RunningProcess(5003, "worker.exe")

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")


@pytest_asyncio.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    async with command_harness(tmp_path) as harness:
        yield harness


# --- Output ----------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chunks", "lines"),
    [
        (
            [b"\x1b[31mred\x1b[0m plain\n", b"\x1b]0;title\x07after title\n"],
            ["red plain", "after title"],
        ),
        (
            [b"progress 10%\rprogress 50%\r", b"progress 100%\n", b"done\r\n"],
            ["progress 100%", "done"],
        ),
        ([b"abd\x08c\n"], ["abc"]),
        (["ä".encode()[:1], "ä".encode()[1:] + b" \xff ok\n"], ["ä � ok"]),
        # PowerShell writes a bare ESC for `e in a double-quoted string.
        ([b"literal \x1bexpected\n"], ["literal \\x1bexpected"]),
        ([b"no line break"], ["no line break"]),
    ],
    ids=["escapes", "redrawn-line", "backspace", "utf-8", "data-escape", "last-line"],
)
async def test_output_is_clean_text(
    harness: Harness, chunks: list[bytes], lines: list[str]
) -> None:
    command_id, process = await harness.launch()
    assert re.fullmatch(r"cmd_[0-9a-hjkmnp-tv-z]{12}", command_id)
    for chunk in chunks:
        process.write(chunk)
    process.exit(0)

    assert await harness.manager.wait(command_id, seconds=None) == "ended"
    report = await harness.finish(command_id)

    assert report.transcript.head == tuple(lines)
    assert (report.status, report.exit_code, report.stop_reason) == ("completed", 0, None)
    assert report.pid == process.pid and report.owner == OWNER
    assert process.released and process.tree.closed


@pytest.mark.asyncio
async def test_log_file_has_every_line_whole_and_shows_the_current_line_live(
    harness: Harness,
) -> None:
    command_id, process = await harness.launch()
    log_path = harness.manager.report(command_id).log_path
    assert log_path is not None and log_path.parent.name == COMMAND_TEMPORARY_CATEGORY

    process.write("first\npartial")
    await harness.advance(1.0)
    assert log_path.read_bytes() == b"first\npartial"
    process.write("\rreplaced\n" + "y" * (COMMAND_MEMORY_LINE_MAX_CHARS + 4000) + "\nlast")
    process.exit(0)
    report = await harness.finish(command_id)

    assert log_path.read_text(encoding="utf-8") == (
        "first\nreplaced\n" + "y" * (COMMAND_MEMORY_LINE_MAX_CHARS + 4000) + "\nlast\n"
    )
    # The transcript keeps the beginning of a very long line.
    assert report.transcript.head == (
        "first",
        "replaced",
        "y" * COMMAND_MEMORY_LINE_MAX_CHARS,
        "last",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("total", "tail_from", "omitted"),
    [(140, 41, 0), (141, 62, 21), (300, 221, 180)],
    ids=["whole", "first-omission", "long"],
)
async def test_transcript_keeps_the_head_and_the_tail(
    harness: Harness, total: int, tail_from: int, omitted: int
) -> None:
    command_id, process = await harness.launch()
    process.write("".join(f"line {number}\n" for number in range(1, total + 1)))
    process.exit(0)

    transcript = (await harness.finish(command_id)).transcript

    assert transcript.head == tuple(f"line {n}" for n in range(1, COMMAND_HEAD_LINES + 1))
    assert transcript.tail == tuple(f"line {n}" for n in range(tail_from, total + 1))
    assert (transcript.omitted_lines, transcript.total_lines) == (omitted, total)


@pytest.mark.asyncio
async def test_foreground_output_is_published_until_the_command_moves_to_the_background(
    harness: Harness,
) -> None:
    published: list[str] = []

    async def on_output(text: str) -> None:
        published.append(text)

    command_id, process = await harness.launch(on_output=on_output)
    process.write("a\n")
    await turns()
    process.write("b\n")
    await turns()
    assert published == ["a"]
    await harness.advance(0.5)
    assert published == ["a", "a\nb"]

    harness.manager.to_background(command_id, deliver=False)
    process.write("c\n")
    await harness.advance(1.0)
    assert published == ["a", "a\nb"]
    assert harness.manager.latest_output(command_id, 2) == "b\nc"


# --- End detection -----------------------------------------------------------


@pytest.mark.asyncio
async def test_quiet_leftovers_end_the_command_and_stay_tied_to_it(harness: Harness) -> None:
    command_id, process = await harness.launch()
    process.write("server started\n")
    process.exit(0, leftovers=[SERVER, WORKER, CONSOLE_HOST])

    # The output is drained for a second, then the leftovers must stay quiet for two.
    await harness.advance(2.5)
    assert not harness.manager.report(command_id).ended
    await harness.advance(1.0)
    report = harness.manager.report(command_id)

    assert (report.ended, report.finished, report.status) == (True, False, "completed")
    # The console host of a windowless console is no work of the command.
    assert report.still_running == (SERVER, WORKER)
    # Listed as the shell lists a command that ended with processes still running.
    harness.manager.to_background(command_id, deliver=False)
    harness.changes.clear()
    # What still runs stays current, and listeners hear when a process ended.
    process.tree.running.remove(WORKER)
    await harness.advance(1.0)
    assert harness.manager.report(command_id).still_running == (SERVER,)
    assert harness.changes == [command_id]
    process.end_leftovers()
    await harness.advance(1.0)
    report = harness.manager.report(command_id)
    assert (report.finished, report.still_running) == (True, ())


@pytest.mark.asyncio
@pytest.mark.parametrize("activity", ["output", "cpu", "new-process"])
async def test_working_leftovers_hold_the_end(harness: Harness, activity: str) -> None:
    command_id, process = await harness.launch()
    process.exit(0, leftovers=[SERVER])

    for _second in range(6):
        if activity == "output":
            process.write("working\n")
        elif activity == "cpu":
            process.tree.cpu_seconds += 0.5
        else:
            process.tree.started += 1
        await harness.advance(1.0)
        assert not harness.manager.report(command_id).ended

    await harness.advance(3.25)
    assert harness.manager.report(command_id).ended


@pytest.mark.asyncio
async def test_processes_that_cannot_be_inspected_at_the_exit_keep_the_command_running(
    harness: Harness,
) -> None:
    command_id, process = await harness.launch()
    process.tree.facts_error = OSError("access denied")
    process.exit(0, leftovers=[SERVER])

    # They may still run: the command neither ends nor drops them.
    await harness.advance(5.0)
    assert harness.manager.report(command_id).status == "running"
    process.tree.facts_error = None
    await harness.advance(3.25)
    report = harness.manager.report(command_id)
    assert (report.ended, report.still_running) == (True, (SERVER,))


# --- Stops -------------------------------------------------------------------


async def _close_session(manager: CommandManager) -> object:
    return await manager.close_scope(OWNER)


async def _close_agent(manager: CommandManager) -> object:
    return await manager.close_agent_scope("agent-a", "project-a")


async def _close_project(manager: CommandManager) -> object:
    return await manager.close_project_scope("project-a")


async def _close_group(manager: CommandManager) -> object:
    await manager.close_execution_group("ext", "group-1", "epoch-1")
    return 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop", "reason", "background", "delivered"),
    [
        (lambda m, c: m.stop(c, "agent"), "agent", True, False),
        (lambda m, c: m.stop(c, "parent"), "parent", True, True),
        (lambda m, c: m.stop(c, "user"), "user", True, True),
        (lambda m, c: m.cancel_run("run-a"), "run_cancelled", False, False),
        (lambda m, c: _close_session(m), "scope", True, False),
        (lambda m, c: _close_agent(m), "scope", True, False),
        (lambda m, c: _close_project(m), "scope", True, False),
        (lambda m, c: _close_group(m), "scope", True, False),
    ],
    ids=[
        "agent",
        "parent",
        "user",
        "run-cancelled",
        "session-scope",
        "agent-scope",
        "project-scope",
        "execution-group",
    ],
)
async def test_each_stop_kills_the_whole_tree_and_records_its_reason(
    harness: Harness,
    stop: Callable[[CommandManager, str], Awaitable[object]],
    reason: str,
    background: bool,
    delivered: bool,
) -> None:
    command_id, process = await harness.launch(execution_owner=GROUP)
    if background:
        harness.manager.to_background(command_id, deliver=True)
    assert harness.manager.has_execution_work(GROUP)

    await stop(harness.manager, command_id)
    report = await harness.finish(command_id)

    assert (report.status, report.stop_reason) == ("stopped", reason)
    assert process.tree.terminations == 1 and process.tree.closed
    assert not harness.manager.has_execution_work(GROUP)
    assert harness.sink.bodies == ([format_report(report)] if delivered else [])


@pytest.mark.asyncio
async def test_the_first_stop_wins_and_fixes_the_outcome(harness: Harness) -> None:
    command_id, process = await harness.launch()
    process.tree.exits.append(ProgramExit("git.exe", 128))

    def kill_with_a_failing_child() -> None:
        # A child that fails because of the kill is no failure of the command.
        process.tree.exits.append(ProgramExit("node.exe", 1))
        FakeTree.terminate(process.tree)

    process.tree.terminate = kill_with_a_failing_child  # type: ignore[method-assign]

    first, second = await asyncio.gather(
        harness.manager.stop(command_id, "user"), harness.manager.stop(command_id, "shutdown")
    )

    assert first.stop_reason == second.stop_reason == "user"
    assert first.failed_programs == ("git.exe exited with code 128",)
    assert process.tree.terminations == 1
    # A stop after the end changes nothing.
    ended_id, ended = await harness.launch()
    ended.exit(0)
    await harness.finish(ended_id)
    report = await harness.manager.stop(ended_id, "user")
    assert (report.status, report.stop_reason, ended.tree.terminations) == ("completed", None, 0)


@pytest.mark.asyncio
async def test_a_stop_after_the_end_kills_the_leftovers_and_keeps_the_outcome(
    harness: Harness,
) -> None:
    command_id, process = await harness.launch()
    harness.manager.to_background(command_id, deliver=True)
    process.exit(3, leftovers=[SERVER])
    await harness.advance(3.25)
    assert harness.sink.bodies == [format_report(harness.manager.report(command_id))]

    report = await harness.manager.stop(command_id, "agent")

    assert (report.status, report.exit_code, report.stop_reason) == ("failed", 3, None)
    assert (report.finished, report.still_running, process.tree.terminations) == (True, (), 1)
    assert len(harness.sink.submissions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("exits_on_interrupt", [True, False])
async def test_posix_stop_interrupts_first_and_kills_after_the_grace(
    harness: Harness, exits_on_interrupt: bool
) -> None:
    command_id, process = await harness.launch()
    process.interruptible = True
    process.exits_on_interrupt = exits_on_interrupt

    stopping = asyncio.ensure_future(harness.manager.stop(command_id, "user"))
    await harness.advance(2.75)
    assert process.interrupts == 1 and process.tree.terminations == 0
    await harness.advance(0.5)
    report = await asyncio.wait_for(stopping, 5)

    assert (report.status, report.stop_reason) == ("stopped", "user")
    assert process.tree.terminations == (0 if exits_on_interrupt else 1)


@pytest.mark.asyncio
async def test_an_unconfirmed_kill_keeps_the_command_and_a_later_stop_retries(
    harness: Harness,
) -> None:
    command_id, process = await harness.launch()
    process.tree.terminate_error = OSError("access denied")

    with pytest.raises(CommandStopError) as failure:
        await harness.manager.stop(command_id, "user")

    assert failure.value.survivors == (RunningProcess(process.pid, "shell"),)
    assert harness.manager.report(command_id).status == "running"
    # A lifecycle close goes on: the failure is logged and the command stays live.
    assert await harness.manager.close_scope(OWNER) == 1
    assert harness.manager.report(command_id).status == "running"
    process.tree.terminate_error = None
    report = await harness.manager.stop(command_id, "user")
    assert (report.status, process.tree.terminations) == ("stopped", 3)


# --- Background and delivery -------------------------------------------------


@pytest.mark.asyncio
async def test_a_background_result_is_delivered_once_when_the_command_ends(
    harness: Harness,
) -> None:
    command_id, process = await harness.launch(owner=CHILD)
    assert harness.manager.status(command_id) is None
    assert harness.manager.commands([OWNER, CHILD]) == []

    moved = harness.manager.to_background(command_id, deliver=True)

    # Reported at once: the server publishes `running` before the shell call's
    # result that hands the command off.
    assert harness.changes == [command_id]
    assert (moved.ended, moved.background, moved.delivers_result) == (False, True, True)
    assert harness.manager.status(command_id) == "running"
    assert [row.owner for row in harness.manager.commands([OWNER, CHILD])] == [CHILD]
    assert harness.manager.commands([OWNER]) == []
    process.exit(0, leftovers=[SERVER])
    await harness.advance(3.25)
    process.end_leftovers()
    report = await harness.finish(command_id)

    assert harness.sink.submissions == [
        {
            "agent_id": "sub-agent",
            "session_id": "session-child",
            "notice_id": f"command:{command_id}",
            "origin_run_id": "run-a",
            "body": format_report(report),
            "project_id": "project-a",
            "execution_owner": None,
            "wake": True,
        }
    ]
    # Moved to the background, ended, finished.
    assert harness.changes == [command_id] * 3
    # A foreground command never delivers and raises no change events.
    foreground_id, foreground = await harness.launch()
    foreground.exit(0)
    await harness.finish(foreground_id)
    assert len(harness.sink.submissions) == 1 and foreground_id not in harness.changes


@pytest.mark.asyncio
async def test_a_command_that_ended_before_moving_to_the_background_delivers_nothing(
    harness: Harness,
) -> None:
    command_id, process = await harness.launch()
    process.exit(0)
    await harness.manager.wait(command_id, seconds=None)

    report = harness.manager.to_background(command_id, deliver=True)

    assert (report.ended, report.delivers_result) == (True, False)
    assert harness.sink.submissions == []


@pytest.mark.asyncio
async def test_scope_close_withdraws_results_not_yet_handed_over(harness: Harness) -> None:
    running_id, _running = await harness.launch()
    ended_id, ended = await harness.launch()
    for command_id in (running_id, ended_id):
        harness.manager.to_background(command_id, deliver=True)
    ended.exit(0)
    await harness.finish(ended_id)
    assert len(harness.sink.submissions) == 1

    assert await harness.manager.close_scope(OWNER) == 1

    assert harness.manager.report(running_id).stop_reason == "scope"
    assert len(harness.sink.submissions) == 1
    assert harness.sink.cancellations == [f"command:{ended_id}"]


@pytest.mark.asyncio
async def test_acknowledging_a_shown_end_withdraws_a_result_not_yet_handed_over(
    harness: Harness,
) -> None:
    pending_id, pending = await harness.launch()
    handed_id, handed = await harness.launch()
    claimed_id, claimed = await harness.launch()
    for command_id in (pending_id, handed_id, claimed_id):
        harness.manager.to_background(command_id, deliver=True)
    # Before the end there is nothing to acknowledge.
    harness.manager.acknowledge(pending_id)
    for process in (pending, handed, claimed):
        process.exit(0)
    for command_id in (pending_id, handed_id, claimed_id):
        await harness.finish(command_id)
    deliveries = {
        submission["notice_id"]: future
        for submission, future in zip(harness.sink.submissions, harness.sink.futures, strict=True)
    }
    deliveries[f"command:{handed_id}"].set_result(None)
    # A delivery is building the message that carries this result.
    harness.sink.claimed.add(f"command:{claimed_id}")
    await turns()

    for command_id in (pending_id, handed_id, claimed_id, "cmd_unknown"):
        harness.manager.acknowledge(command_id)

    assert len(harness.sink.submissions) == 3
    assert harness.sink.cancellations == [f"command:{pending_id}", f"command:{claimed_id}"]
    await turns()
    assert deliveries[f"command:{pending_id}"].cancelled()
    # The claimed result is delivered once, as submitted.
    assert not deliveries[f"command:{claimed_id}"].done()


@pytest.mark.asyncio
async def test_transferred_commands_deliver_to_their_new_session(harness: Harness) -> None:
    command_id, process = await harness.launch()
    harness.manager.to_background(command_id, deliver=True)

    assert harness.manager.transfer_scope(OWNER, OTHER) == 1
    identity = SessionAddress(None, "agent-old", "session-i")
    identity_id, identity_process = await harness.launch(owner=identity)
    assert harness.manager.transfer_agent_scope("agent-old", "agent-new") == 1
    process.exit(0)
    identity_process.exit(0)
    await harness.finish(command_id)
    await harness.finish(identity_id)

    assert [(s["agent_id"], s["session_id"]) for s in harness.sink.submissions] == [
        ("agent-b", "session-b")
    ]
    assert harness.manager.report(identity_id).owner == SessionAddress(
        None, "agent-new", "session-i"
    )


# --- Runs, capacity, shutdown, retention -----------------------------------


@pytest.mark.asyncio
async def test_run_cancel_stops_only_its_foreground_commands(harness: Harness) -> None:
    foreground_id, _foreground = await harness.launch()
    background_id, _background = await harness.launch()
    harness.manager.to_background(background_id, deliver=True)
    other_id, _other = await harness.launch(run_id="run-b")

    await harness.manager.cancel_run("run-a")

    assert harness.manager.report(foreground_id).stop_reason == "run_cancelled"
    assert harness.manager.report(background_id).status == "running"
    assert harness.manager.report(other_id).status == "running"
    with pytest.raises(CommandNotStartedError):
        await harness.launch()
    harness.manager.release_run("run-a")
    await harness.manager.stop(background_id, "user")
    await harness.launch()


@pytest.mark.asyncio
async def test_a_cancelled_launch_leaves_no_command_behind(harness: Harness) -> None:
    harness.launcher.gate = asyncio.Event()
    launching = asyncio.ensure_future(harness.launch())
    await turns()
    launching.cancel()
    await turns()
    harness.launcher.gate.set()

    with pytest.raises(asyncio.CancelledError):
        await launching

    [process] = harness.launcher.processes
    assert process.tree.terminations == 1 and process.released


@pytest.mark.asyncio
async def test_capacity_counts_running_commands_and_launches_in_progress(
    harness: Harness,
) -> None:
    first_id, first = await harness.launch()
    await harness.launch()
    harness.launcher.gate = asyncio.Event()
    pending = asyncio.ensure_future(harness.launch())
    await turns()

    with pytest.raises(CommandCapacityError) as refused:
        await harness.launch()

    assert refused.value.limit == 3
    harness.launcher.gate.set()
    await pending
    first.exit(0)
    await harness.finish(first_id)
    await harness.launch()


@pytest.mark.asyncio
async def test_shutdown_stops_every_command_and_delivers_background_results(
    harness: Harness,
) -> None:
    foreground_id, foreground = await harness.launch()
    background_id, background = await harness.launch()
    harness.manager.to_background(background_id, deliver=True)

    await harness.manager.shutdown()

    reports = [harness.manager.report(i) for i in (foreground_id, background_id)]
    assert [report.stop_reason for report in reports] == ["shutdown", "shutdown"]
    assert foreground.tree.terminations == background.tree.terminations == 1
    assert harness.sink.bodies == [format_report(reports[1])]
    with pytest.raises(CommandNotStartedError):
        await harness.launch()


@pytest.mark.asyncio
async def test_closing_the_manager_kills_every_command_and_ends_the_waits_for_it(
    harness: Harness,
) -> None:
    command_id, process = await harness.launch()
    waiting = asyncio.ensure_future(harness.manager.wait(command_id, seconds=None))
    finishing = asyncio.ensure_future(harness.manager.wait_finished(command_id))
    await turns()

    harness.manager.close()

    assert await asyncio.wait_for(waiting, 1) == "ended"
    await asyncio.wait_for(finishing, 1)
    assert process.tree.terminations == 1


@pytest.mark.asyncio
async def test_aclose_kills_the_remaining_commands_off_the_event_loop_together(
    tmp_path: Path,
) -> None:
    async with command_harness(tmp_path, run_blocking=asyncio.to_thread) as harness:
        processes = [(await harness.launch())[1] for _ in range(2)]
        # Each kill waits until the other one runs: one at a time, or on the
        # Event Loop, they would wait for each other until the barrier breaks.
        both = threading.Barrier(2, timeout=5)
        for process in processes:

            def kill_together(tree: FakeTree = process.tree) -> None:
                both.wait()
                FakeTree.terminate(tree)

            process.tree.terminate = kill_together  # type: ignore[method-assign]

        await harness.manager.aclose()

        assert [process.tree.terminations for process in processes] == [1, 1]
        assert all(process.released for process in processes)


@pytest.mark.asyncio
async def test_commands_are_forgotten_after_their_retention(harness: Harness) -> None:
    command_id, process = await harness.launch()
    harness.manager.to_background(command_id, deliver=False)
    held_id, held = await harness.launch()
    harness.manager.to_background(held_id, deliver=False)
    process.exit(0)
    held.exit(0, leftovers=[SERVER])
    await harness.finish(command_id)
    harness.changes.clear()

    await harness.advance(COMMAND_RETENTION_SECONDS - 60, step=30)
    assert harness.manager.status(command_id) == "completed"
    # The sweep forgets it without a query and tells the listeners.
    await harness.advance(90, step=30)
    assert harness.changes.count(command_id) == 1
    assert harness.manager.status(command_id) is None
    with pytest.raises(CommandNotFoundError):
        harness.manager.report(command_id)
    # A command whose leftovers still run stays.
    assert [row.command_id for row in harness.manager.commands([OWNER])] == [held_id]
    # Its retention counts from when its last process ended, not from its end.
    held.end_leftovers()
    await harness.finish(held_id)
    await harness.advance(COMMAND_RETENTION_SECONDS - 60, step=30)
    assert harness.manager.status(held_id) == "completed"
    await harness.advance(90, step=30)
    assert harness.manager.status(held_id) is None


# --- Real processes ----------------------------------------------------------


@pytest_asyncio.fixture
async def real_manager(tmp_path: Path) -> AsyncIterator[tuple[CommandManager, FakeSink]]:
    sink = FakeSink()
    manager = CommandManager(
        sink,
        temporary_files=TemporaryFileManager(tmp_path / "data"),
        process_tracker=_tracked_suspended,
        monotonic=lambda: time.monotonic() * 10,
        sleep=lambda seconds: asyncio.sleep(seconds / 10),
    )
    try:
        yield manager, sink
    finally:
        await manager.aclose()


def _tracked_suspended(pid: int) -> ProcessTree:
    if sys.platform == "win32":
        # The root runs nothing before it is in the Job: nothing can escape it.
        assert psutil.Process(pid).status() == psutil.STATUS_STOPPED
    return track_process_tree(pid)


# A virtual environment's python.exe on Windows is a launcher that starts the base
# interpreter as its child; the base interpreter keeps the process tree exact.
_PYTHON = getattr(sys, "_base_executable", None) or sys.executable


async def _launch_real(manager: CommandManager, tmp_path: Path, *argv: str) -> tuple[str, int]:
    handle = await manager.launch(
        OWNER,
        argv,
        command=" ".join(argv),
        description=None,
        cwd=tmp_path,
        env=_environment(),
        origin_run_id="run-a",
        formatter=format_report,
    )
    return handle.command_id, handle.pid


def _environment() -> dict[str, str]:
    return {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}


async def _printed(manager: CommandManager, command_id: str, count: int) -> list[str]:
    """The first *count* words the command printed, once it printed them."""
    for _attempt in range(1000):
        words = manager.latest_output(command_id, 10).split()
        if len(words) >= count:
            return words[:count]
        await asyncio.sleep(0.01)
    raise AssertionError(f"the command printed fewer than {count} words")


def _alive(pid: int) -> bool:
    try:
        status: str = psutil.Process(pid).status()
    except psutil.Error:
        return False
    return bool(status != psutil.STATUS_ZOMBIE)


# The root starts a child at once, which starts a grandchild at once; each prints
# the id of the process it started.
_SLEEP = "import time; time.sleep(60)"
_GRANDCHILD = (
    f"import subprocess, sys, time; g = subprocess.Popen([sys.executable, '-c', {_SLEEP!r}]); "
    "print(g.pid, flush=True); time.sleep(60)"
)
_CHAIN = (
    f"import subprocess, sys; c = subprocess.Popen([sys.executable, '-c', {_GRANDCHILD!r}]); "
    "print(c.pid, flush=True); c.wait()"
)
# The root starts a child that starts a grandchild and exits: the grandchild is
# an orphan outside the root's descendants, still in the command's Job.
_ORPHAN_PARENT = (
    f"import subprocess, sys; g = subprocess.Popen([sys.executable, '-c', {_SLEEP!r}]); "
    "print(g.pid, flush=True)"
)
_ORPHANING = (
    f"import subprocess, sys, time; subprocess.run([sys.executable, '-c', {_ORPHAN_PARENT!r}]); "
    "print('orphaned', flush=True); time.sleep(60)"
)


@pytest.mark.asyncio
async def test_a_command_gets_no_input_and_no_terminal(
    real_manager: tuple[CommandManager, FakeSink], tmp_path: Path
) -> None:
    manager, _sink = real_manager
    program = (
        "import sys; data = sys.stdin.read(); "
        "print(repr(data), sys.stdin.isatty(), sys.stdout.isatty(), sys.stderr.isatty()); "
        "sys.stderr.write('on stderr\\n')"
    )
    command_id, _pid = await _launch_real(manager, tmp_path, _PYTHON, "-c", program)

    assert await manager.wait(command_id, seconds=30) == "ended"
    report = manager.report(command_id)

    assert report.transcript.head == ("'' False False False", "on stderr")
    assert (report.status, report.exit_code) == ("completed", 0)


@windows_only
@pytest.mark.asyncio
async def test_a_stop_kills_every_process_the_job_contains(
    real_manager: tuple[CommandManager, FakeSink], tmp_path: Path
) -> None:
    manager, _sink = real_manager
    command_id, root = await _launch_real(manager, tmp_path, _PYTHON, "-c", _CHAIN)
    pids = [root, *map(int, await _printed(manager, command_id, 2))]

    report = await manager.stop(command_id, "user")

    assert (report.status, report.finished) == ("stopped", True)
    assert not [pid for pid in pids if _alive(pid)]


@windows_only
@pytest.mark.asyncio
async def test_a_tree_kill_from_outside_ends_the_command_like_any_end(
    real_manager: tuple[CommandManager, FakeSink], tmp_path: Path
) -> None:
    manager, sink = real_manager
    command_id, root = await _launch_real(manager, tmp_path, _PYTHON, "-c", _CHAIN)
    manager.to_background(command_id, deliver=True)
    pids = [root, *map(int, await _printed(manager, command_id, 2))]

    killed = await asyncio.to_thread(
        subprocess.run, ["taskkill", "/PID", str(root), "/T", "/F"], capture_output=True
    )

    assert killed.returncode == 0
    assert await manager.wait(command_id, seconds=30) == "ended"
    report = manager.report(command_id)
    assert (report.status, report.exit_code, report.stop_reason) == ("failed", 1, None)
    assert not [pid for pid in pids if _alive(pid)]
    assert sink.bodies == [format_report(report)]


@windows_only
@pytest.mark.asyncio
async def test_an_orphan_the_tree_kill_misses_is_reported_and_stopped(
    real_manager: tuple[CommandManager, FakeSink], tmp_path: Path
) -> None:
    manager, _sink = real_manager
    command_id, root = await _launch_real(manager, tmp_path, _PYTHON, "-c", _ORPHANING)
    printed, _orphaned = await _printed(manager, command_id, 2)
    orphan = int(printed)

    await asyncio.to_thread(
        subprocess.run, ["taskkill", "/PID", str(root), "/T", "/F"], capture_output=True
    )

    assert await manager.wait(command_id, seconds=30) == "ended"
    report = manager.report(command_id)
    assert (report.exit_code, report.finished) == (1, False)
    assert [process.pid for process in report.still_running] == [orphan]
    report = await manager.stop(command_id, "agent")
    assert report.finished and not _alive(orphan)
