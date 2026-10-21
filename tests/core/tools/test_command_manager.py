"""CommandManager: one-shot commands on pipes, their end, stop, delivery and lifecycle.

Most cases drive a fake process and tree on a fake clock. The real-process cases
check what only the operating system can show: no input and no terminal, the
Job that contains every process from creation on, and kills from outside vBot.
They run on a clock ten times faster than real time, so the end-detection
pauses cost a tenth of their nominal length.
"""

from __future__ import annotations

import asyncio
import errno
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from pathlib import Path

import psutil  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from core.database import write_bootstrap_marker
from core.permissions import PermissionBoundary
from core.runs import RunAdmissionBlockedError, RunExecutionOwner
from core.sessions import ChatSessionManager, SessionAddress
from core.storage.temp_files import TemporaryFileManager
from core.tools import _command_output as command_output_module
from core.tools._command_cleaning import OutputCleaner, OutputUpdate
from core.tools._interpreter_host import InterpreterHost
from core.tools.command_manager import (
    COMMAND_HEAD_CHARS,
    COMMAND_RETENTION_SECONDS,
    COMMAND_TAIL_CHARS,
    COMMAND_TEMPORARY_CATEGORY,
    SERVICE_POLL_SECONDS,
    CommandCapacityError,
    CommandManager,
    CommandManagerError,
    CommandNotFoundError,
    CommandNotStartedError,
    CommandStopError,
)
from core.tools.process_tree import (
    KillNotConfirmedError,
    ProgramExit,
    RunningProcess,
    track_process_tree,
)
from core.utils.timestamps import parse_timestamp
from tests.core.permissions.command_check_support import restricted
from tests.core.tools.command_manager_helpers import (
    OWNER,
    FakeSink,
    FakeTree,
    Harness,
    command_harness,
    format_report,
    run_inline,
    turns,
)
from tests.core.tools.terminal_manager_helpers import eventually

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
        # A CRLF split between reads ends the line like one.
        ([b"kept\r", b"\nnext\r\n"], ["kept", "next"]),
        ([b"abd\x08c\n"], ["abc"]),
        (["ä".encode()[:1], "ä".encode()[1:] + b" \xff ok\n"], ["ä � ok"]),
        # PowerShell writes a bare ESC for `e in a double-quoted string.
        ([b"literal \x1bexpected\n"], ["literal \\x1bexpected"]),
        # C1 code points are text to a UTF-8 terminal: each is dropped, none
        # starts a sequence that hides the rest of the line.
        (
            ["Grüße und mehr\n".encode().decode("latin-1").encode(), "a\x85b\x9bc\x90d\n".encode()],
            ["GrÃ¼Ãe und mehr", "abcd"],
        ),
        ([b"no line break"], ["no line break"]),
        (["Grüße\n".encode("utf-16-le")], ["Grüße"]),
        (
            [bytes((byte,)) for byte in b"\xff\xfe" + "Grüße 東京 😀\n".encode("utf-16-le")],
            ["Grüße 東京 😀"],
        ),
        (
            [bytes((byte,)) for byte in b"\xfe\xff" + "東京 😀\n".encode("utf-16-be")],
            ["東京 😀"],
        ),
        (
            [b"before\n", b"\xff\xfe" + "Grüße 東京 😀\n".encode("utf-16-le"), b"after\n"],
            ["before", "Grüße 東京 😀", "after"],
        ),
    ],
    ids=[
        "escapes",
        "redrawn-line",
        "split-line-break",
        "backspace",
        "utf-8",
        "data-escape",
        "c1-code-points",
        "last-line",
        "utf-16-le-unmarked",
        "utf-16-le-split",
        "utf-16-be-split",
        "mixed-encodings",
    ],
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
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-16-be"])
async def test_log_file_has_every_byte_the_command_wrote_as_it_arrives(
    harness: Harness, encoding: str
) -> None:
    command_id, process = await harness.launch()
    log_path = harness.manager.report(command_id).log_path
    assert log_path is not None and log_path.parent.name == COMMAND_TEMPORARY_CATEGORY
    mark = {"utf-8": b"", "utf-16-le": b"\xff\xfe", "utf-16-be": b"\xfe\xff"}[encoding]
    chunks = [
        mark + "PARTIAL-".encode(encoding),
        "\r\x1b[31mred\x1b[0m\r\n".encode(encoding),
        "second\n".encode(encoding),
        b"progress 10%\rprogress 100%\n",
        b"abd\x08c \xff \x80 ",
        "ä".encode()[:1],
        "ä".encode()[1:] + b" partial",
    ]

    process.write(chunks[0])
    await turns()
    # Written as it arrives, before the command ends.
    assert log_path.read_bytes() == chunks[0]
    assert harness.manager.report(command_id).transcript.head == ("PARTIAL-",)
    for chunk in chunks[1:3]:
        process.write(chunk)
    await turns()
    # A short second UTF-16 line is visible immediately too, not held back
    # until the command ends while looking for a possible encoding change.
    assert harness.manager.report(command_id).transcript.head == ("red", "second")
    assert log_path.read_bytes() == b"".join(chunks[:3])
    for chunk in chunks[3:]:
        process.write(chunk)
    process.exit(0)
    report = await harness.finish(command_id)

    assert log_path.read_bytes() == b"".join(chunks)
    assert report.log_path == log_path
    assert report.transcript.head == ("red", "second", "progress 100%", "abc � � ä partial")


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le", "utf-16-be"])
@pytest.mark.parametrize("advanced", [False, True], ids=["before-update", "after-update"])
async def test_output_that_cannot_be_cleaned_is_kept_uncleaned_and_logged_whole(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    encoding: str,
    advanced: bool,
) -> None:
    command_id, process = await harness.launch()
    log_path = harness.manager.report(command_id).log_path
    # The first chunk ends inside a character in the current encoding. Both
    # the encoding choice and its unfinished character must survive failure.
    mark = {"utf-8": b"", "utf-16-le": b"\xff\xfe", "utf-16-be": b"\xfe\xff"}[encoding]
    first = mark + "\x1b[1mcollected\x1b[0m 3 items\nGr".encode(encoding) + "ü".encode(encoding)[:1]
    process.write(first)
    await turns()
    feed = OutputCleaner.feed

    def broken(cleaner: OutputCleaner, data: bytes) -> OutputUpdate:
        if advanced:
            # A worker can fail after applying the chunk but before its
            # update arrived; only the last acknowledged state counts.
            feed(cleaner, data)
        raise ValueError("cleaner bug")

    monkeypatch.setattr(OutputCleaner, "feed", broken)
    later = [
        "ü".encode(encoding)[1:] + "n\n".encode(encoding),
        b"\x1b[32mpassed\x1b[0m 1\r\n",
        b"passed 2\n",
        b"passed 3",
    ]
    for chunk in later:
        process.write(chunk)
    await turns()

    # The pipe is still read: the command never waits on a full one.
    assert process.unread == 0
    process.exit(1)
    report = await harness.finish(command_id)
    assert (report.status, report.exit_code) == ("failed", 1)
    # The later output is decoded but not cleaned, from where the cleaner's
    # decoding stopped; nothing of it is lost.
    assert report.transcript.head == (
        "collected 3 items",
        "Grün",
        "\x1b[32mpassed\x1b[0m 1",
        "passed 2",
        "passed 3",
    )
    assert log_path is not None and report.log_path == log_path
    assert log_path.read_bytes() == first + b"".join(later)
    errors = [record for record in caplog.records if record.levelname == "ERROR"]
    assert len(errors) == 1


@pytest.mark.parametrize(
    "kept", [b"first\nsecond\nth", b"fir", b""], ids=["later-part", "first-part", "nothing"]
)
@pytest.mark.parametrize("unexpected", [False, True], ids=["os-error", "unexpected-error"])
@pytest.mark.asyncio
async def test_a_log_file_that_cannot_be_written_reports_what_it_kept_and_why(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    kept: bytes,
    unexpected: bool,
) -> None:
    command_id, process = await harness.launch()
    log_path = harness.manager.report(command_id).log_path
    failure = (
        RuntimeError("Unexpected write failure")
        if unexpected
        else OSError(errno.ENOSPC, "No space left on device")
    )
    room = [len(kept)]
    write = os.write

    def failing_write(fd: int, data: bytes | memoryview) -> int:
        # The real write loop must retain each partial write when its next
        # attempt fails, even if the failure is not an operating-system error.
        if not room[0]:
            raise failure
        count = write(fd, data[: room[0]])
        room[0] -= count
        return count

    monkeypatch.setattr(command_output_module.os, "write", failing_write)
    for chunk in ("first\n", "second\nthird\n", "fourth\n"):
        process.write(chunk)
        await turns()
    process.exit(0)
    report = await harness.finish(command_id)

    # The file keeps what was written before the failure; the report says how
    # much and why, and names no file that holds nothing. The transcript goes on.
    assert report.log_loss is not None and report.log_loss.error is failure
    assert (report.log_loss.kept_bytes, report.log_loss.kept_lines) == (
        len(kept),
        kept.count(b"\n"),
    )
    assert report.log_path == (log_path if kept else None)
    assert log_path is not None and log_path.read_bytes() == kept
    assert report.transcript.head == ("first", "second", "third", "fourth")
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1 and "log file" in warnings[0].getMessage()


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["lines", "one-line", "erased-long-line", "erased-line-end"])
async def test_transcript_keeps_the_first_and_the_last_characters_of_any_output(
    harness: Harness, output: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    command_id, process = await harness.launch()
    if output == "lines":
        lines = [f"line {number:05d}" for number in range(10_000)]
        text = "".join(f"{line}\n" for line in lines)
    else:
        lines = ["".join(chr(ord("a") + number % 26) for number in range(200_000))]
        text = lines[0]
    chunks = [text[start : start + 16_384] for start in range(0, len(text), 16_384)]
    if output == "erased-long-line":
        # A backspace right after the line in progress was shortened to its end
        # leaves that end shorter than the tail's limit; the lines after it, also
        # those that arrive later, still replace it.
        line = text[:80_000]
        lines = [line[:-1], "", "after 1", "after 2"]
        chunks = [line[:40_000], line[40_000:] + "\x08\n\n", "after 1\nafter 2\n"]
    elif output == "erased-line-end":
        # Backspaces that erase more than the kept end of a long line leave a
        # line as long as a whole line can be: all of it is kept.
        line = text[:70_000]
        lines = [line[:29_000]]
        chunks = [line[:40_000], line[40_000:], "\x08" * 41_000]
    for chunk in chunks:
        process.write(chunk)
        await turns()
    process.exit(0)

    report = await harness.finish(command_id)
    transcript = report.transcript

    assert transcript.total_lines == len(lines)
    if output == "lines":
        # Whole lines: the first within the head's characters, the last within the tail's.
        head = (COMMAND_HEAD_CHARS + 1) // 11
        tail = (COMMAND_TAIL_CHARS + 1) // 11
        assert transcript.head == tuple(lines[:head])
        assert transcript.tail == tuple(lines[-tail:])
        assert (transcript.head_cut, transcript.tail_cut) == (0, 0)
        assert transcript.omitted_lines == len(lines) - head - tail
        # Restart recovery reads the same transcript back from the log file,
        # also one larger than the windows it reads.
        monkeypatch.setattr(command_output_module, "_RECOVERY_HEAD_BYTES", 4096)
        monkeypatch.setattr(command_output_module, "_RECOVERY_TAIL_BYTES", 65_536)
        assert report.log_path is not None
        snapshot = await command_output_module.read_log(
            InterpreterHost.in_process(), report.log_path, run_blocking=run_inline
        )
        assert snapshot is not None and snapshot.transcript == transcript
    elif output == "one-line":
        # A line too long for both: its start and its end.
        (line,) = lines
        assert transcript.head == (line[:COMMAND_HEAD_CHARS],)
        assert transcript.tail == (line[-COMMAND_TAIL_CHARS:],)
        assert transcript.head_cut == len(line) - COMMAND_HEAD_CHARS
        assert transcript.tail_cut == len(line) - COMMAND_TAIL_CHARS
        assert transcript.omitted_lines == 0
    else:
        # A long line erased again keeps its start, and the lines after it.
        assert transcript.head == (lines[0][:COMMAND_HEAD_CHARS],)
        assert transcript.head_cut == len(lines[0]) - COMMAND_HEAD_CHARS
        kept = lines if output == "erased-line-end" else lines[1:]
        assert (transcript.tail, transcript.tail_cut) == (tuple(kept), 0)
        assert transcript.omitted_lines == 0


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
    assert harness.manager.busy_addresses() == {OWNER}
    process.write("server started\n")
    process.exit(0, leftovers=[SERVER, WORKER, CONSOLE_HOST])

    # The output is drained for a second, then the leftovers must stay quiet for two.
    await harness.advance(2.5)
    assert not harness.manager.report(command_id).ended
    await harness.advance(1.0)
    report = harness.manager.report(command_id)

    assert (report.ended, report.finished, report.status) == (True, False, "completed")
    assert harness.manager.busy_addresses() == {OWNER}
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
    assert harness.manager.busy_addresses() == set()


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
    return None


# Commands a scope close must leave running: (Session, execution owner).
_SESSION_BYSTANDERS = [(SessionAddress("project-a", "agent-a", "session-c"), None)]
_PROJECT_BYSTANDERS = [
    (SessionAddress("project-b", "agent-a", "session-a"), None),
    (SessionAddress(None, "agent-a", "session-a"), None),
]
_AGENT_BYSTANDERS = [*_PROJECT_BYSTANDERS, (OTHER, None)]
_GROUP_BYSTANDERS = [
    (OWNER, RunExecutionOwner("ext", "group-2", "participant", "generation", "epoch-1")),
    (OWNER, RunExecutionOwner("ext", "group-1", "participant", "generation", "epoch-2")),
    (OWNER, None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop", "reason", "background", "delivered", "bystanders"),
    [
        (lambda m, c: m.stop(c, "agent"), "agent", True, False, []),
        (lambda m, c: m.stop(c, "parent"), "parent", True, True, []),
        (lambda m, c: m.stop(c, "user"), "user", True, True, []),
        (lambda m, c: m.cancel_run("run-a"), "run_cancelled", False, False, []),
        (lambda m, c: _close_session(m), "scope", True, False, _SESSION_BYSTANDERS),
        (lambda m, c: _close_agent(m), "scope", True, False, _AGENT_BYSTANDERS),
        (lambda m, c: _close_project(m), "scope", True, False, _PROJECT_BYSTANDERS),
        (lambda m, c: _close_group(m), "scope", True, False, _GROUP_BYSTANDERS),
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
    tmp_path: Path,
    stop: Callable[[CommandManager, str], Awaitable[object]],
    reason: str,
    background: bool,
    delivered: bool,
    bystanders: list[tuple[SessionAddress, RunExecutionOwner | None]],
) -> None:
    async with command_harness(tmp_path, max_live=8) as harness:
        command_id, process = await harness.launch(execution_owner=GROUP)
        if background:
            harness.manager.to_background(command_id, deliver=True)
        others = [
            await harness.launch(owner=owner, execution_owner=execution_owner)
            for owner, execution_owner in bystanders
        ]
        for other_id, _other in others:
            harness.manager.to_background(other_id, deliver=True)
        assert harness.manager.has_execution_work(GROUP)

        stopped = await stop(harness.manager, command_id)
        report = await harness.finish(command_id)

        assert (report.status, report.stop_reason) == ("stopped", reason)
        assert process.tree.terminations == 1 and process.tree.closed
        assert not harness.manager.has_execution_work(GROUP)
        assert harness.sink.bodies == ([format_report(report)] if delivered else [])
        # A scope close stops only its own commands, and counts only them.
        if reason == "scope" and stopped is not None:
            assert stopped == 1
        for other_id, other in others:
            assert harness.manager.report(other_id).status == "running"
            assert other.tree.terminations == 0


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
    tmp_path: Path,
) -> None:
    async def run_blocking(function: Callable[..., object], /, *args: object) -> object:
        if getattr(function, "__func__", None) is FakeTree.terminate:
            # The server fails because of the kill, and a poll of the
            # leftovers sees that before the kill is confirmed.
            process.tree.exits.append(ProgramExit("server.exe", 1))
            await harness.advance(1.0)
        return function(*args)

    async with command_harness(tmp_path, run_blocking=run_blocking) as harness:
        command_id, process = await harness.launch()
        harness.manager.to_background(command_id, deliver=True)
        process.exit(3, leftovers=[SERVER])
        await harness.advance(3.25)
        assert harness.sink.bodies == [format_report(harness.manager.report(command_id))]
        process.tree.exits.append(ProgramExit("git.exe", 128))

        report = await harness.manager.stop(command_id, "agent")

        assert (report.status, report.exit_code, report.stop_reason) == ("failed", 3, None)
        assert (report.finished, report.still_running) == (True, ())
        assert process.tree.terminations == 1
        # A leftover that failed by itself is the command's; one the kill ended is not.
        assert report.failed_programs == ("git.exe exited with code 128",)
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
@pytest.mark.parametrize(
    ("failure", "then"),
    [
        ("refused", "stop-again"),
        ("refused", "end"),
        ("cancelled-before-kill", "end"),
        ("not-confirmed", "end"),
        ("cancelled-during-kill", "end"),
    ],
)
async def test_a_failed_stop_keeps_its_reason_only_once_its_kill_was_sent(
    tmp_path: Path, failure: str, then: str
) -> None:
    kill_gate = asyncio.Event()
    kill_gate.set()

    async def run_blocking(function: Callable[..., object], /, *args: object) -> object:
        if getattr(function, "__func__", None) is FakeTree.terminate:
            await kill_gate.wait()
        return function(*args)

    kill_sent = failure in ("not-confirmed", "cancelled-during-kill")
    async with command_harness(tmp_path, run_blocking=run_blocking) as harness:
        command_id, process = await harness.launch()
        harness.manager.to_background(command_id, deliver=True)
        if failure == "refused":
            process.tree.terminate_error = OSError("access denied")
        elif failure == "not-confirmed":
            process.tree.terminate_error = KillNotConfirmedError("the processes survived")
        elif failure == "cancelled-before-kill":
            # POSIX: the program ignores the interrupt, the stop waits its grace.
            process.interruptible = True
        else:
            kill_gate.clear()
        stopping = asyncio.ensure_future(harness.manager.stop(command_id, "agent"))

        if failure.startswith("cancelled"):
            # The Run is cancelled while its stop call waits.
            await turns()
            stopping.cancel()
            await asyncio.gather(stopping, return_exceptions=True)
            assert stopping.cancelled() and process.tree.terminations == 0
        else:
            with pytest.raises(CommandStopError) as stop_failure:
                await stopping
            assert stop_failure.value.survivors == (RunningProcess(process.pid, "shell"),)
            assert stop_failure.value.kill_sent == kill_sent

        # Without a kill, the command runs on as if not stopped; with one, the
        # stop's reason stays for the end the kill causes. Either way the
        # Agent's call showed no end, so the end is delivered.
        running = harness.manager.report(command_id)
        assert (running.status, running.stop_reason) == (
            "running",
            "agent" if kill_sent else None,
        )
        if then == "stop-again":
            # A lifecycle close goes on: the failure is logged and the command stays live.
            assert await harness.manager.close_scope(OWNER) == 1
            assert harness.manager.report(command_id).status == "running"
            process.tree.terminate_error = None
            report = await harness.manager.stop(command_id, "user")
            assert (report.status, report.stop_reason) == ("stopped", "user")
            assert process.tree.terminations == 3
            # The scope close withdrew the delivery for good.
            assert harness.sink.submissions == []
            return
        process.tree.exits.append(ProgramExit("node.exe", 1))
        process.write("FAILED: 3 tests\n")
        if failure == "cancelled-during-kill":
            # The kill began before the cancel and goes on.
            kill_gate.set()
        else:
            # The command ends: by itself, or of the kill sent before.
            process.exit(1)
        report = await harness.finish(command_id)

    if kill_sent:
        assert (report.status, report.stop_reason) == ("stopped", "agent")
        # The kill ended the child.
        assert report.failed_programs == ()
    else:
        assert (report.status, report.exit_code, report.stop_reason) == ("failed", 1, None)
        assert report.failed_programs == ("node.exe exited with code 1",)
    assert harness.sink.bodies == [format_report(report)]


# --- Background and delivery -------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        "running",
        "ended",
        "delivered",
        "withdrawn",
        "moved",
        "stop_after_restart",
        "ack_after_restart",
        "move_after_restart",
        "closed_execution",
        "replaced_session",
        "expired",
        "moved_before_handoff",
        "owned_running",
        "owned_ended",
        "owned_delivered",
    ],
)
async def test_background_checkpoint_recovers_once_after_restart(
    tmp_path: Path,
    outcome: str,
) -> None:
    """A crash recovers an armed command and never repeats its persisted Note."""
    data = tmp_path / "sessions"
    write_bootstrap_marker(data)
    sessions = ChatSessionManager(data)
    sessions.create(OWNER.agent_id, OWNER.session_id, OWNER.project_id)
    generation_id = sessions.get(OWNER).generation_id
    assert generation_id is not None
    execution_owner = (
        RunExecutionOwner("ext", "closed-group", "participant", generation_id, "old-epoch")
        if outcome.startswith("owned_")
        else None
    )
    running_outcomes = {"running", "moved", "owned_running"}
    restored_owner = OWNER
    try:
        async with command_harness(tmp_path, checkpoint_store=sessions) as harness:
            await harness.manager.wait_recovered()
            command_id, process = await harness.launch(execution_owner=execution_owner)
            if outcome == "moved_before_handoff":
                restored_owner = SessionAddress(OWNER.project_id, "renamed-agent", OWNER.session_id)
                await sessions.move(OWNER, restored_owner)
            await harness.manager.to_background_durably(command_id, deliver=True)
            if outcome == "running":
                # Output after its last saved state reaches only the raw log.
                process.write("compiled 3 files\n")
                await turns()
            if outcome not in running_outcomes:
                process.exit(0)
                await harness.finish(command_id)
                await eventually(lambda: bool(harness.sink.submissions))
            if outcome in {"delivered", "owned_delivered"}:
                # The Session commit succeeded, but its acknowledgement did not
                # get back to the checkpoint before the server disappeared.
                await sessions.get(OWNER).add_note_async(harness.sink.bodies[0])
            elif outcome == "withdrawn":
                harness.manager.acknowledge(command_id)
            await harness.manager.flush()
            checkpoint = sessions.background_command_read()[0]["checkpoint"]
        # The harness closes fake processes; restore the exact durable crash
        # boundary rather than running an actual server/process crash.
        if outcome == "running":
            # The previous server's last write to the raw log, 2 minutes 5 seconds in.
            last_write = parse_timestamp(checkpoint["started_at"]).timestamp() + 125
            os.utime(checkpoint["log_path"], (last_write, last_write))
        if outcome == "expired":
            checkpoint.update(pending=False, retained_until=0)
        if outcome == "replaced_session":
            sessions.delete(OWNER)
            sessions.create(OWNER.agent_id, OWNER.session_id, OWNER.project_id)
        sessions.background_command_put(
            OWNER,
            command_id,
            "run-a",
            checkpoint,
            generation_id=generation_id,
        )
        if outcome == "moved":
            restored_owner = SessionAddress(OWNER.project_id, "renamed-agent", OWNER.session_id)
            await sessions.move(OWNER, restored_owner)
    finally:
        sessions.close()

    sessions = ChatSessionManager(data)
    try:
        async with command_harness(tmp_path, checkpoint_store=sessions) as restored:
            if outcome in {"replaced_session", "expired"}:
                await restored.manager.wait_recovered()
                assert restored.manager.commands([OWNER]) == []
                assert sessions.background_command_read() == []
                return
            expected_delivery = (
                outcome not in {"delivered", "withdrawn"} and execution_owner is None
            )
            submission_count = int(expected_delivery)
            if expected_delivery:
                await eventually(lambda: bool(restored.sink.submissions))
                assert restored.sink.submissions[0]["address"] == restored_owner
                if outcome == "running":
                    body = restored.sink.bodies[0]
                    assert "was stopped after at least 2 minutes 5 seconds: " in body
                    assert "compiled 3 files" in body
                if outcome == "stop_after_restart":
                    assert await restored.manager.close_scope(restored_owner) == 0
                    assert restored.sink.futures[0].cancelled()
                elif outcome == "ack_after_restart":
                    restored.manager.acknowledge(command_id)
                    assert restored.sink.futures[0].cancelled()
                elif outcome == "closed_execution":
                    restored.sink.futures[0].set_exception(RunAdmissionBlockedError("closed"))
                else:
                    if outcome == "move_after_restart":
                        target = SessionAddress(OWNER.project_id, "renamed-agent", OWNER.session_id)
                        await sessions.move(restored_owner, target)
                        assert await restored.manager.transfer_scope(restored_owner, target) == 1
                        restored_owner = target
                        await eventually(lambda: len(restored.sink.submissions) == 2)
                        assert restored.sink.futures[0].cancelled()
                        assert restored.sink.submissions[-1]["address"] == target
                        submission_count = 2
                    await sessions.get(restored_owner).add_note_async(restored.sink.bodies[-1])
                    restored.sink.futures[-1].set_result(None)
            await restored.manager.wait_recovered()
            assert len(restored.sink.submissions) == submission_count
            if execution_owner is not None:
                # The old group epoch must never enter TriggerService or start
                # another Run. Its one outcome stays in the original Session.
                notes = [
                    message
                    for message in sessions.get(restored_owner).load()
                    if message.role == "note"
                ]
                assert len(notes) == 1
                assert command_id in str(notes[0].content)
            report = restored.manager.report(command_id)
            assert report.finished
            assert report.owner == restored_owner
            assert report.status == ("stopped" if outcome in running_outcomes else "completed")
            assert restored.manager.busy_addresses() == set()
            assert restored.launcher.processes == []
            assert not sessions.background_command_read()[0]["checkpoint"]["pending"]
            retained_until = sessions.background_command_read()[0]["checkpoint"]["retained_until"]
        async with command_harness(tmp_path, checkpoint_store=sessions) as again:
            await again.manager.wait_recovered()
            assert again.sink.submissions == []
            if execution_owner is not None:
                assert (
                    len(
                        [
                            message
                            for message in sessions.get(restored_owner).load()
                            if message.role == "note"
                        ]
                    )
                    == 1
                )
            assert (
                sessions.background_command_read()[0]["checkpoint"]["retained_until"]
                == retained_until
            )
    finally:
        sessions.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("rules", ["full_access", "restricted"])
async def test_a_delivery_formatted_after_restart_names_only_paths_its_session_may_read(
    tmp_path: Path, rules: str
) -> None:
    """The checkpoint keeps the boundary of the command's Session; recovery formats under it."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "helpers.py").touch()
    permissions = (
        restricted(tmp_path, deny=["Read(tools/**)"])
        if rules == "restricted"
        else PermissionBoundary.UNRESTRICTED
    )
    stored = permissions.to_ceiling_dict(source="agent")
    data = tmp_path / "sessions"
    write_bootstrap_marker(data)
    sessions = ChatSessionManager(data)
    sessions.create(OWNER.agent_id, OWNER.session_id, OWNER.project_id)
    generation_id = sessions.get(OWNER).generation_id
    assert generation_id is not None
    try:
        async with command_harness(tmp_path, checkpoint_store=sessions) as harness:
            await harness.manager.wait_recovered()
            command_id, process = await harness.launch(
                command="python -c 'import helpers'", permissions=permissions
            )
            await harness.manager.to_background_durably(command_id, deliver=True)
            process.write("ModuleNotFoundError: No module named 'helpers'\n")
            process.exit(1)
            await harness.finish(command_id)
            await harness.manager.flush()
            checkpoint = sessions.background_command_read()[0]["checkpoint"]
        assert checkpoint["permission_ceiling"] == stored
        # The previous server saved the end, but not the body of its delivery.
        checkpoint["body"] = None
        sessions.background_command_put(
            OWNER, command_id, "run-a", checkpoint, generation_id=generation_id
        )
    finally:
        sessions.close()

    sessions = ChatSessionManager(data)
    try:
        async with command_harness(tmp_path, checkpoint_store=sessions) as restored:
            await eventually(lambda: bool(restored.sink.submissions))
            body = restored.sink.bodies[0]
            assert "Hint: Python cannot import 'helpers'" in body
            # The rules refuse reading tools/, so the hint does not name the module there.
            assert ("tools/helpers.py" in body) is (rules == "full_access")
            restored.sink.futures[0].set_result(None)
            await restored.manager.wait_recovered()
            saved = sessions.background_command_read()[0]["checkpoint"]
            assert not saved["pending"]
            assert saved["permission_ceiling"] == stored
    finally:
        sessions.close()


@pytest.mark.asyncio
async def test_a_failed_checkpoint_cannot_be_hidden_by_another_commands_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = tmp_path / "sessions"
    write_bootstrap_marker(data)
    sessions = ChatSessionManager(data)
    sessions.create(OWNER.agent_id, OWNER.session_id, OWNER.project_id)
    put = sessions.background_command_put
    fail: set[str] = set()

    def save(
        address: SessionAddress,
        command_id: str,
        origin_run_id: str,
        checkpoint: dict[str, object],
        *,
        generation_id: str,
    ) -> None:
        if command_id in fail:
            raise OSError("checkpoint disk unavailable")
        put(address, command_id, origin_run_id, checkpoint, generation_id=generation_id)

    monkeypatch.setattr(sessions, "background_command_put", save)
    try:
        async with command_harness(tmp_path, checkpoint_store=sessions) as harness:
            await harness.manager.wait_recovered()
            first, _process = await harness.launch()
            second, _other = await harness.launch()
            fail.add(first)
            harness.manager.to_background(first, deliver=True)
            harness.manager.to_background(second, deliver=True)
            with pytest.raises(OSError, match="checkpoint disk unavailable"):
                await harness.manager.flush()
            assert [row["command_id"] for row in sessions.background_command_read()] == [second]
            fail.clear()
            await harness.manager.to_background_durably(first, deliver=True)
            assert {row["command_id"] for row in sessions.background_command_read()} == {
                first,
                second,
            }
    finally:
        sessions.close()


@pytest.mark.asyncio
async def test_a_background_result_is_delivered_once_when_the_command_ends(
    harness: Harness,
) -> None:
    command_id, process = await harness.launch(owner=CHILD)
    assert harness.manager.status(command_id) is None
    assert harness.manager.commands([OWNER, CHILD]) == []
    assert not harness.manager.has_unfinished_background()
    # What Session activity counts at each change: the result wakes CHILD until
    # the sink holds it, without a gap between the end and the handover.
    wakes: list[int] = []
    harness.manager.add_changed_callback(
        lambda _command_id: wakes.append(harness.manager.pending_wakes(CHILD))
    )

    moved = harness.manager.to_background(command_id, deliver=True)

    # Reported at once: the server publishes `running` before the shell call's
    # result that hands the command off.
    assert harness.changes == [command_id]
    assert (moved.ended, moved.background) == (False, True)
    assert harness.manager.status(command_id) == "running"
    assert [row.owner for row in harness.manager.commands([OWNER, CHILD])] == [CHILD]
    assert harness.manager.commands([OWNER]) == []
    assert harness.manager.has_unfinished_background(besides=OWNER)
    assert not harness.manager.has_unfinished_background(besides=CHILD)
    process.exit(0, leftovers=[SERVER])
    await harness.advance(3.25)
    # Ended, but a process it started still runs.
    assert harness.manager.has_unfinished_background()
    process.end_leftovers()
    report = await harness.finish(command_id)
    assert not harness.manager.has_unfinished_background()

    assert harness.sink.submissions == [
        {
            "address": CHILD,
            "notice_id": f"command:{command_id}",
            "origin_run_id": "run-a",
            "body": format_report(report),
            "execution_owner": None,
            "wake": True,
        }
    ]
    # Moved to the background, ended, handed to the sink, finished.
    assert harness.changes == [command_id] * 4
    assert wakes == [1, 1, 0, 0]
    # A foreground command never delivers and raises no change events.
    foreground_id, foreground = await harness.launch()
    foreground.exit(0)
    await harness.finish(foreground_id)
    assert len(harness.sink.submissions) == 1 and foreground_id not in harness.changes


class _Sockets:
    """The processes listening on a TCP port, as the service detection reads them."""

    def __init__(self) -> None:
        self.listening: set[int] = set()
        self.denied = False
        self.reads = 0

    def __call__(self) -> frozenset[int]:
        self.reads += 1
        if self.denied:
            raise psutil.AccessDenied()
        return frozenset(self.listening)


@pytest.mark.asyncio
async def test_a_command_serving_on_a_tcp_port_stops_counting_as_a_pending_wake(
    tmp_path: Path,
) -> None:
    sockets = _Sockets()
    async with command_harness(tmp_path, read_listening=sockets) as harness:
        manager = harness.manager
        server_id, server = await harness.launch(command="serve")
        watcher_id, watcher = await harness.launch(command="watch")
        for command_id in (server_id, watcher_id):
            manager.to_background(command_id, deliver=True)
        # A process the server's shell started listens on a port.
        server.tree.running.append(SERVER)
        sockets.listening.add(SERVER.pid)
        # Sockets the platform keeps private detect nothing.
        sockets.denied = True
        await harness.advance(SERVICE_POLL_SECONDS)
        assert (sockets.reads, manager.pending_wakes(OWNER)) == (1, 2)

        sockets.denied = False
        harness.changes.clear()
        await harness.advance(SERVICE_POLL_SECONDS)
        # The server is a service; the watcher without a port still counts.
        assert manager.pending_wakes(OWNER) == 1
        assert harness.changes == [server_id]
        # Goals keep counting every command whose result is armed.
        assert manager.has_pending_delivery(OWNER)
        # The classification is sticky.
        sockets.listening.clear()
        await harness.advance(SERVICE_POLL_SECONDS)
        assert manager.pending_wakes(OWNER) == 1

        watcher.exit(0)
        await harness.finish(watcher_id)
        assert manager.pending_wakes(OWNER) == 0
        # Nothing is polled while no command it could find runs.
        reads = sockets.reads
        await harness.advance(3 * SERVICE_POLL_SECONDS, step=1.0)
        assert sockets.reads == reads


@pytest.mark.asyncio
async def test_a_command_that_ended_before_moving_to_the_background_delivers_nothing(
    harness: Harness,
) -> None:
    command_id, process = await harness.launch()
    process.exit(0)
    await harness.manager.wait(command_id, seconds=None)

    report = harness.manager.to_background(command_id, deliver=True)

    assert report.ended
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
@pytest.mark.parametrize("before_submit", [False, True])
async def test_transferred_commands_deliver_to_their_new_session(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, before_submit: bool
) -> None:
    command_id, process = await harness.launch()
    flushing = asyncio.Event()
    flushed = asyncio.Event()
    if before_submit:
        flush = harness.manager._services.flush

        async def paused_flush() -> None:
            flushing.set()
            await flushed.wait()
            await flush()

        monkeypatch.setattr(
            harness.manager, "_services", replace(harness.manager._services, flush=paused_flush)
        )
    submitted_id, submitted = await harness.launch()
    for background_id in (command_id, submitted_id):
        harness.manager.to_background(background_id, deliver=True)
    # Its result went to the old Session and is not delivered yet.
    submitted.exit(0)
    await harness.finish(submitted_id)
    if before_submit:
        await flushing.wait()
        assert harness.sink.submissions == []

    assert await harness.manager.transfer_scope(OWNER, OTHER) == 2
    flushed.set()
    await eventually(lambda: len(harness.sink.submissions) == (1 if before_submit else 2))
    # A durable status result in the moved Session withdraws its exact pending
    # receipt, including when submission started only after the Session moved.
    harness.manager.acknowledge(submitted_id)
    await turns()
    assert all(future.cancelled() for future in harness.sink.futures)
    identity = SessionAddress(None, "agent-old", "session-i")
    identity_id, identity_process = await harness.launch(owner=identity)
    assert await harness.manager.transfer_agent_scope("agent-old", "agent-new") == 1
    process.exit(0)
    identity_process.exit(0)
    await harness.finish(command_id)
    await harness.finish(identity_id)

    assert [(s["notice_id"], s["address"]) for s in harness.sink.submissions] == [
        *([] if before_submit else [(f"command:{submitted_id}", OWNER)]),
        (f"command:{submitted_id}", OTHER),
        (f"command:{command_id}", OTHER),
    ]
    assert harness.sink.futures[0].cancelled()
    if not before_submit:
        assert harness.sink.submissions[0]["body"] == harness.sink.submissions[1]["body"]
    assert harness.manager.report(identity_id).owner == SessionAddress(
        None, "agent-new", "session-i"
    )


# --- Runs, capacity, shutdown, retention -----------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_rename", [False, True])
async def test_transfer_includes_a_command_still_starting(
    harness: Harness,
    agent_rename: bool,
) -> None:
    source = SessionAddress(None, "agent-old", "session-a")
    target = SessionAddress(None, "agent-new", "session-a" if agent_rename else "session-b")
    harness.launcher.gate = asyncio.Event()
    starting = asyncio.create_task(harness.launch(owner=source))
    await turns()
    moving = asyncio.create_task(
        harness.manager.transfer_agent_scope("agent-old", "agent-new")
        if agent_rename
        else harness.manager.transfer_scope(source, target)
    )
    await turns()
    assert not moving.done()
    harness.launcher.gate.set()
    assert await moving == 1
    command_id, process = await starting
    assert harness.manager.report(command_id).owner == target
    assert await harness.manager.close_scope(target) == 1
    assert process.tree.terminations == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_stop", [False, True])
async def test_process_handles_stay_open_until_a_kill_is_confirmed(
    tmp_path: Path,
    cancel_stop: bool,
) -> None:
    confirming = asyncio.Event()
    confirm = asyncio.Event()

    async def run_blocking(function: Callable[..., object], /, *args: object) -> object:
        result = function(*args)
        if getattr(function, "__func__", None) is FakeTree.terminate:
            confirming.set()
            await confirm.wait()
            tree = getattr(function, "__self__", None)
            assert isinstance(tree, FakeTree)
            assert not tree.closed
        return result

    async with command_harness(tmp_path, run_blocking=run_blocking) as harness:
        command_id, process = await harness.launch()
        stopping = asyncio.create_task(harness.manager.stop(command_id, "user"))
        try:
            await confirming.wait()
            if cancel_stop:
                stopping.cancel()
                await asyncio.gather(stopping, return_exceptions=True)
            await harness.finish(command_id)
            assert not process.tree.closed and not process.released
        finally:
            confirm.set()
        if not cancel_stop:
            assert (await stopping).status == "stopped"
        await turns()
        assert process.tree.closed and process.released


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
    pending = asyncio.ensure_future(harness.launch(owner=CHILD))
    await turns()
    assert harness.manager.busy_addresses() == {OWNER, CHILD}

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
    tmp_path: Path, caplog: pytest.LogCaptureFixture
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
        unkillable_id, unkillable = await harness.launch()
        unkillable.tree.terminate_error = OSError("access denied")

        # Runtime stop does not wait for processes whose kill was not confirmed.
        with pytest.raises(CommandManagerError, match=unkillable_id):
            async with asyncio.timeout(5):
                await harness.manager.aclose()

        assert [process.tree.terminations for process in processes] == [1, 1]
        assert all(process.released for process in [*processes, unkillable])
        assert f"pid {unkillable.pid}" in caplog.text


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
        process_tracker=track_process_tree,
        monotonic=lambda: time.monotonic() * 10,
        sleep=lambda seconds: asyncio.sleep(seconds / 10),
    )
    try:
        yield manager, sink
    finally:
        await manager.aclose()


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
        permissions=PermissionBoundary.UNRESTRICTED,
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
