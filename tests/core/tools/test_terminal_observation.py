"""Terminal Tool: list, status paging, and what a persisted screen acknowledges."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from core.storage.temp_files import TemporaryFileManager
from core.tools._terminal_render_host import TerminalScreen
from core.tools.terminal_manager import (
    TerminalInfo,
    TerminalManager,
    TerminalOwner,
    TerminalRenderHost,
)
from core.tools.tools import JsonObject
from tests.core.tools.terminal_helpers import call, details, make_context
from tests.core.tools.terminal_helpers import manager as manager
from tests.core.tools.terminal_manager_helpers import (
    TEST_START_WAIT_SECONDS,
    AdapterFactory,
    eventually,
)

OWNER = TerminalOwner("project-a", "agent-a", "session-a")


async def _render(
    terminal_manager: TerminalManager, factory: AdapterFactory, terminal_id: str, output: str
) -> None:
    """Emit program output and wait until the terminal has rendered it."""
    shown = terminal_manager.terminal(terminal_id, OWNER).screen_revision
    factory.adapters[0].emit(output)
    await eventually(lambda: terminal_manager.terminal(terminal_id, OWNER).screen_revision > shown)


async def _start_with_lines(
    terminal_manager: TerminalManager, factory: AdapterFactory, tmp_path: Path, prefix: str = ""
) -> str:
    started = await call(
        terminal_manager, make_context(tmp_path), {"action": "start", "command": "fake-tui"}
    )
    terminal_id = str(cast(dict[str, Any], started["data"])["terminal_id"])
    await _render(
        terminal_manager,
        factory,
        terminal_id,
        prefix + "".join(f"line-{index}\r\n" for index in range(50)),
    )
    return terminal_id


@pytest.mark.asyncio
async def test_list_shows_every_terminal_with_its_title_and_attachment(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, factory = manager
    terminal_id = await _start_with_lines(
        terminal_manager, factory, tmp_path, prefix="\x1b]0;Codex migration\x07"
    )
    context = make_context(tmp_path)

    listed = await call(terminal_manager, context, {"action": "list"})
    terminals = cast(dict[str, Any], listed["data"])["terminals"]
    assert terminals == [
        {"terminal_id": terminal_id, "program": "fake-tui", "state": "running", "attached": "here"}
    ]
    [shown] = details(terminal_manager, tmp_path, {"action": "list"}, listed)
    assert shown["type"] == "results"
    assert shown["items"] == [{"title": "fake-tui", "meta": "running · attached here"}]
    discovered = await call(
        terminal_manager, make_context(tmp_path, session_id="other"), {"action": "list"}
    )
    other_terminals = cast(dict[str, Any], discovered["data"])["terminals"]
    assert [(item["terminal_id"], item["attached"]) for item in other_terminals] == [
        (terminal_id, "other")
    ]
    # The program's own window title shows with the launch facts.
    status = await call(terminal_manager, context, {"action": "status", "terminal_id": terminal_id})
    assert cast(dict[str, Any], status["data"])["title"] == "Codex migration"


@pytest.mark.asyncio
async def test_status_pages_the_whole_buffer_by_absolute_start_line(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, factory = manager
    terminal_id = await _start_with_lines(terminal_manager, factory, tmp_path)
    context = make_context(tmp_path)

    def request(start_line: int, lines: int) -> JsonObject:
        return {
            "action": "status",
            "terminal_id": terminal_id,
            "start_line": start_line,
            "lines": lines,
        }

    async def status(**fields: Any) -> dict[str, Any]:
        result = await call(
            terminal_manager, context, {"action": "status", "terminal_id": terminal_id, **fields}
        )
        return cast(dict[str, Any], result["data"])

    # 50 lines on a 24-row screen: lines 0-26 scrolled into history, 27-49 are on screen.
    current = await status(lines=3)
    assert current["screen"].splitlines() == [f"line-{index}" for index in range(27, 50)]
    assert current["history"] == "line-24\nline-25\nline-26"
    assert current["scrollback"] == {
        "first_line": 0,
        "start_line": 24,
        "end_line": 27,
        "total_lines": 50,
        "screen_start_line": 27,
        "older_request": request(21, 3),
    }

    first = await status(start_line=0, lines=3)
    assert "screen" not in first
    assert first["history"] == "line-0\nline-1\nline-2"
    assert first["scrollback"] == {
        "first_line": 0,
        "start_line": 0,
        "end_line": 3,
        "total_lines": 50,
        "screen_start_line": 27,
        "newer_request": request(3, 3),
    }
    # A page runs on into the screen, so newer requests read the whole buffer.
    straddling = await status(start_line=25, lines=4)
    assert straddling["history"] == "line-25\nline-26\nline-27\nline-28"
    assert (
        straddling["scrollback"]["older_request"],
        straddling["scrollback"]["newer_request"],
    ) == (request(21, 4), request(29, 4))
    all_lines: list[str] = []
    next_page: JsonObject | None = request(0, 7)
    while next_page is not None:
        page = cast(dict[str, Any], (await call(terminal_manager, context, next_page))["data"])
        assert "screen" not in page
        all_lines.extend(page["history"].splitlines())
        next_page = page["scrollback"].get("newer_request")
    assert all_lines == [f"line-{index}" for index in range(50)]

    # Line numbers stay fixed while output arrives; the screen moves on.
    more = "".join(f"more-{index}\r\n" for index in range(5))
    await _render(terminal_manager, factory, terminal_id, more)
    assert (await status(start_line=0, lines=3))["history"] == first["history"]
    moved = await status(start_line=25, lines=4)
    assert moved["history"] == straddling["history"]
    assert (moved["scrollback"]["screen_start_line"], moved["scrollback"]["total_lines"]) == (
        32,
        55,
    )
    assert (await status(lines=3))["history"] == "line-29\nline-30\nline-31"

    # A full-screen view keeps no terminal history, and status says so.
    await _render(terminal_manager, factory, terminal_id, "\x1b[?1049h\x1b[Hfull-screen view")
    full_screen = await status()
    assert full_screen["screen"] == "full-screen view"
    assert "alternate screen" in full_screen["note"]
    await _render(terminal_manager, factory, terminal_id, "\x1b[?1049l")
    primary = await status()
    assert primary["screen"].splitlines()[-1] == "more-4"
    assert "alternate_screen" not in primary
    assert "alternate screen" not in primary.get("note", "")


@pytest.mark.asyncio
async def test_results_say_what_they_leave_out_and_where_to_read_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A result says how many lines the Agent has not seen it leaves out and
    names the page of the newest of them the terminal keeps (user decision D1),
    and a page that cannot start where asked says so; both name the log, which
    has every line, and say where it ends once writing it failed."""
    disk_full = False
    open_file = Path.open

    class FillingLog:
        def __init__(self, handle: Any) -> None:
            self._handle = handle

        def write(self, data: bytes) -> int:
            if disk_full:
                raise OSError(28, "No space left on device")
            return int(self._handle.write(data))

        def __getattr__(self, name: str) -> Any:
            return getattr(self._handle, name)

    def open_log(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        handle = open_file(path, mode, *args, **kwargs)
        return FillingLog(handle) if path.suffix == ".log" and mode == "ab" else handle

    monkeypatch.setattr(Path, "open", open_log)
    factory = AdapterFactory()
    terminal_manager = TerminalManager(
        adapter_factory=factory,
        render_host=TerminalRenderHost.in_process(),
        temporary_files=TemporaryFileManager(tmp_path / "temp"),
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
        idle_seconds=0.06,
        start_wait_seconds=TEST_START_WAIT_SECONDS,
        scrollback_lines=10,
    )
    terminal_manager.start()
    try:
        persisted: list[Callable[[], None]] = []
        context = make_context(tmp_path, result_persisted_hook=persisted.append)
        started = await call(terminal_manager, context, {"action": "start", "command": "fake"})
        persisted.pop()()
        data = cast(dict[str, Any], started["data"])
        terminal_id = data["terminal_id"]
        named = re.search(r" (\S+\.log), ", data["log"])
        assert named is not None, data["log"]
        log = named[1]

        def request(start_line: int, lines: int = 100) -> str:
            page = {"action": "status", "terminal_id": terminal_id, "start_line": start_line}
            return json.dumps({**page, "lines": lines})

        async def status(**fields: Any) -> dict[str, Any]:
            arguments = {"action": "status", "terminal_id": terminal_id, **fields}
            return cast(dict[str, Any], (await call(terminal_manager, context, arguments))["data"])

        # 60 lines on 24 rows: lines 0-36 scrolled off the screen, 27-36 are kept.
        lines = "".join(f"line-{index}\r\n" for index in range(60))
        await _render(terminal_manager, factory, terminal_id, lines)
        first = await status(lines=3)
        assert first["history"] == "line-34\nline-35\nline-36"
        assert re.search(r"\b34\b", first["note"]), first["note"]
        assert log in first["note"] and request(27, 7) in first["note"]
        persisted.pop()()

        # The Agent saw down to line 59; the note counts only the lines after it.
        lines = "".join(f"line-{index}\r\n" for index in range(60, 90))
        await _render(terminal_manager, factory, terminal_id, lines)
        second = await status(lines=3)
        assert second["history"] == "line-64\nline-65\nline-66"
        assert re.search(r"\b4\b", second["note"]), second["note"]
        assert log in second["note"] and request(60, 4) in second["note"]

        # Pages start at the oldest kept line and end at the newest line.
        older = await status(start_line=0, lines=3)
        assert older["history"] == "line-57\nline-58\nline-59"
        assert "57" in older["note"] and log in older["note"]
        newer = await status(start_line=500, lines=3)
        assert newer["scrollback"]["start_line"] == 90
        assert "500" in newer["note"] and "89" in newer["note"]
        # A page right after the last line is empty and says so too (code review R9).
        after = await status(start_line=90, lines=3)
        assert "history" not in after
        assert after["note"] == "Line 90 does not exist yet: the terminal has lines up to 89."

        # Once writing the log failed at line 90, results say which lines it lacks:
        # 0-89 are in the log, 97-106 are kept, 90-96 are lost.
        complete = (await status())["log"]
        disk_full = True
        lines = "".join(f"line-{index}\r\n" for index in range(90, 130))
        await _render(terminal_manager, factory, terminal_id, lines)
        assert b"line-90" not in Path(log).read_bytes()
        failed = await status(start_line=0, lines=3)
        assert failed["log"] != complete and log in failed["log"] and "90" in failed["log"]
        assert log in failed["note"] and "89" in failed["note"]
        current = (await status(lines=3))["note"]
        assert re.search(r"\b44\b", current) and re.search(r"\b30\b", current), current
        assert log in current and request(97) in current
    finally:
        await terminal_manager.aclose()


@pytest.mark.asyncio
async def test_a_screen_that_cannot_be_drawn_says_so_and_catches_up_later(
    manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While drawing output fails, input still reaches the program and results
    say the screen is out of date; once drawing works again, a screen drawn anew
    from the log shows the output the old one missed, in the size and the modes
    the terminal has now."""
    terminal_manager, factory = manager
    context = make_context(tmp_path)
    started = await call(terminal_manager, context, {"action": "start", "command": "fake"})
    terminal_id = cast(dict[str, Any], started["data"])["terminal_id"]
    await _render(terminal_manager, factory, terminal_id, "before\r\n")
    feed = TerminalScreen.feed

    async def broken_feed(_screen: TerminalScreen, _text: str) -> Any:
        raise RuntimeError("renderer broke")

    monkeypatch.setattr(TerminalScreen, "feed", broken_feed)
    # The output the screen misses turns bracketed paste on.
    factory.adapters[0].emit("missed\r\n\x1b[?2004h")
    status: JsonObject = {"action": "status", "terminal_id": terminal_id}

    async def behind() -> bool:
        data = cast(dict[str, Any], (await call(terminal_manager, context, status))["data"])
        return "renderer broke" in data.get("note", "")

    await eventually(behind)
    # The reply ends at a quiet boundary, which a screen behind its output
    # cannot show; the short timeout ends it instead.
    typing: JsonObject = {"action": "input", "terminal_id": terminal_id, "text": "y"}
    typed = await call(terminal_manager, context, {**typing, "timeout": 0.05})
    assert typed["ok"] is True, typed
    assert factory.adapters[0].writes[-1] == "y"
    assert "renderer broke" in cast(dict[str, Any], typed["data"])["note"]
    await terminal_manager.resize_for_operator(terminal_id, columns=100, rows=30)

    monkeypatch.setattr(TerminalScreen, "feed", feed)
    await _render(terminal_manager, factory, terminal_id, "after\r\n")

    async def caught_up() -> bool:
        data = cast(dict[str, Any], (await call(terminal_manager, context, status))["data"])
        return data["screen"].splitlines() == ["before", "missed", "after"] and (
            "renderer broke" not in data.get("note", "")
        )

    await eventually(caught_up, attempts=600)
    assert (await terminal_manager.read_for_operator(terminal_id))["bracketed_paste"] is True
    info = terminal_manager.terminal(terminal_id, OWNER)
    assert (info.columns, info.rows) == (100, 30)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"action": "start", "command": "   "}, "command"),
        ({"action": "input", "terminal_id": "missing", "data": "raw", "key": "enter"}, "data"),
        ({"action": "resize", "terminal_id": "missing", "columns": 120}, "no call resizes it"),
        ({"action": "status", "terminal_id": "missing", "lines": 150}, "lines"),
        ({"action": "unknown"}, "action"),
    ],
    ids=[
        "blank-command",
        "data-with-key",
        "resize",
        "too-many-lines",
        "action",
    ],
)
@pytest.mark.asyncio
async def test_invalid_or_inapplicable_arguments_return_stable_failure(
    manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    arguments: JsonObject,
    message: str,
) -> None:
    result = await call(manager[0], make_context(tmp_path), arguments)

    error = cast(dict[str, Any], result["error"])
    assert error["code"] == "invalid_arguments"
    assert message in error["message"]
    assert manager[1].calls == []


@pytest.mark.asyncio
async def test_a_failure_while_acting_reports_unknown_effects(
    manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_manager, factory = manager
    context = make_context(tmp_path)
    started = await call(terminal_manager, context, {"action": "start", "command": "fake-tui"})
    terminal_id = started["data"]["terminal_id"]

    def fail(text: str) -> None:
        raise RuntimeError("console write failed")

    monkeypatch.setattr(factory.adapters[0], "write", fail)

    result = await call(
        terminal_manager,
        context,
        {"action": "input", "terminal_id": terminal_id, "text": "y", "key": "enter"},
    )

    assert result["error"] == {
        "code": "tool_execution_error",
        "message": "terminal failed while running: console write failed. It is unknown how much "
        "of the call took effect. Check the current state before you call terminal again.",
    }


async def _start_persisted(
    terminal_manager: TerminalManager, tmp_path: Path
) -> tuple[str, list[Callable[[], None]]]:
    """Start a terminal whose start screen has been persisted, and collect later callbacks."""
    callbacks: list[Callable[[], None]] = []
    context = make_context(tmp_path, result_persisted_hook=callbacks.append)
    started = await call(terminal_manager, context, {"action": "start"})
    data = cast(dict[str, Any], started["data"])
    assert "size_change" not in data
    callbacks.pop()()
    return data["terminal_id"], callbacks


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["status", "wait"])
async def test_resize_notice_is_consumed_only_with_a_persisted_screen(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path, action: str
) -> None:
    terminal_manager, _factory = manager
    terminal_id, callbacks = await _start_persisted(terminal_manager, tmp_path)
    context = make_context(tmp_path, result_persisted_hook=callbacks.append)

    for columns, rows in [(70, 20), (160, 48), (100, 30)]:
        await terminal_manager.resize_for_operator(terminal_id, columns=columns, rows=rows)
    arguments: JsonObject = {"action": action, "terminal_id": terminal_id}
    if action == "wait":
        arguments["timeout"] = 0.01
    result = await call(terminal_manager, context, arguments)
    data = cast(dict[str, Any], result["data"])
    change = data["size_change"]
    assert (change["previous_columns"], change["previous_rows"]) == (80, 24)
    assert change["notice"] == (
        "The terminal was resized since your previous screen result. Previous size: 80 columns "
        "x 24 rows. Current size: 100 columns x 30 rows. Use positions from the current screen."
    )
    assert "size_change" in await terminal_manager.snapshot(terminal_id, OWNER)
    callbacks.pop()()
    assert "size_change" not in await terminal_manager.snapshot(terminal_id, OWNER)


@pytest.mark.asyncio
async def test_resize_after_screen_capture_remains_unseen_after_persistence(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, _factory = manager
    terminal_id, callbacks = await _start_persisted(terminal_manager, tmp_path)
    context = make_context(tmp_path, result_persisted_hook=callbacks.append)
    await terminal_manager.resize_for_operator(terminal_id, columns=140, rows=40)
    await call(terminal_manager, context, {"action": "status", "terminal_id": terminal_id})
    await terminal_manager.resize_for_operator(terminal_id, columns=160, rows=48)
    callbacks.pop()()

    result = await call(terminal_manager, context, {"action": "status", "terminal_id": terminal_id})
    data = cast(dict[str, Any], result["data"])
    change = data["size_change"]
    assert (change["previous_columns"], change["previous_rows"]) == (140, 40)
    assert "Current size: 160 columns x 48 rows." in change["notice"]


@pytest.mark.asyncio
async def test_input_and_wait_show_only_the_history_no_result_showed(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    """Results that follow the program show the lines above the screen only
    when no result showed them, the newest 30 at most, and offer no pages; the
    note names the page of the newest 100 lines they leave out (user decision D1)."""
    terminal_manager, factory = manager
    terminal_id, callbacks = await _start_persisted(terminal_manager, tmp_path)
    context = make_context(tmp_path, result_persisted_hook=callbacks.append)
    wait: JsonObject = {"action": "wait", "terminal_id": terminal_id, "timeout": 0.01}

    async def follow(arguments: JsonObject) -> dict[str, Any]:
        data = cast(dict[str, Any], (await call(terminal_manager, context, arguments))["data"])
        callbacks.pop()()
        return data

    # 30 lines on 24 rows: lines 0-6 scrolled off a screen no result showed.
    await _render(terminal_manager, factory, terminal_id, "".join(f"a-{i}\r\n" for i in range(30)))
    first = await follow(wait)
    assert first["history"] == "\n".join(f"a-{i}" for i in range(7))
    assert "scrollback" not in first and "note" not in first
    # The lines that scroll off next were on the screen the result showed.
    await _render(terminal_manager, factory, terminal_id, "".join(f"b-{i}\r\n" for i in range(10)))
    second = await follow({"action": "input", "terminal_id": terminal_id, "text": "x"})
    assert "history" not in second and "scrollback" not in second, second
    # 250 lines from line 40 on: lines 40-266 scroll off unseen, the result
    # shows the newest 30 of them and offers the page of the 100 above those.
    await _render(terminal_manager, factory, terminal_id, "".join(f"c-{i}\r\n" for i in range(250)))
    third = await follow(wait)
    assert third["history"].split("\n") == [f"c-{i}" for i in range(197, 227)]
    assert "scrollback" not in third
    page = json.dumps(
        {"action": "status", "terminal_id": terminal_id, "start_line": 137, "lines": 100}
    )
    assert re.fullmatch(
        r"197 earlier lines you have not seen are not shown; to read them, read the log "
        rf"\S+\.log; the newest 100 of them can also be read with terminal {re.escape(page)}\.",
        third["note"],
    ), third["note"]


@pytest.mark.asyncio
async def test_a_screen_persisted_after_a_clear_leaves_the_lines_drawn_since_unseen(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    """A clear erases what the Agent saw: the lines drawn there next are new
    to it, also when the result that showed the old screen becomes durable
    after the clear (code review R8: the late acknowledgement undid the clear)."""
    terminal_manager, factory = manager
    terminal_id, callbacks = await _start_persisted(terminal_manager, tmp_path)
    context = make_context(tmp_path, result_persisted_hook=callbacks.append)
    status = {"action": "status", "terminal_id": terminal_id, "lines": 3}
    await _render(
        terminal_manager, factory, terminal_id, "".join(f"old-{i}\r\n" for i in range(30))
    )
    await call(terminal_manager, context, status)
    # The screen showed lines 7-30; a clear erases it from line 7 on.
    await _render(
        terminal_manager,
        factory,
        terminal_id,
        "\x1b[2J\x1b[H" + "".join(f"new-{i}\r\n" for i in range(40)),
    )
    callbacks.pop()()

    data = cast(dict[str, Any], (await call(terminal_manager, context, status))["data"])
    # new-0 is line 7; the page shows lines 21-23 (new-14 to new-16).
    assert data["history"] == "new-14\nnew-15\nnew-16"
    assert data["note"].startswith("14 earlier lines you have not seen are not shown"), data


@pytest.mark.asyncio
async def test_out_of_order_screen_persistence_cannot_restore_an_old_size(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, _factory = manager
    terminal_id, callbacks = await _start_persisted(terminal_manager, tmp_path)
    context = make_context(tmp_path, result_persisted_hook=callbacks.append)
    for columns, rows in [(140, 40), (160, 48)]:
        await terminal_manager.resize_for_operator(terminal_id, columns=columns, rows=rows)
        await call(terminal_manager, context, {"action": "status", "terminal_id": terminal_id})
    callbacks.pop()()
    callbacks.pop()()

    assert "size_change" not in await terminal_manager.snapshot(terminal_id, OWNER)
    # The next resize is reported against the newest persisted size.
    await terminal_manager.resize_for_operator(terminal_id, columns=100, rows=30)
    change = (await terminal_manager.snapshot(terminal_id, OWNER))["size_change"]
    assert (change["previous_columns"], change["previous_rows"]) == (160, 48)


@pytest.mark.asyncio
async def test_returning_to_previous_size_still_reports_intermediate_resizes(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, _factory = manager
    terminal_id, _callbacks = await _start_persisted(terminal_manager, tmp_path)
    await terminal_manager.resize_for_operator(terminal_id, columns=80, rows=24)
    assert "size_change" not in await terminal_manager.snapshot(terminal_id, OWNER)
    await terminal_manager.resize_for_operator(terminal_id, columns=160, rows=48)
    await terminal_manager.resize_for_operator(terminal_id, columns=80, rows=24)

    snapshot = await terminal_manager.snapshot(terminal_id, OWNER)
    assert "size_change" in snapshot
    assert (snapshot["columns"], snapshot["rows"]) == (80, 24)


@pytest.mark.asyncio
async def test_new_attachment_does_not_inherit_another_sessions_screen_observation(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, _factory = manager
    terminal_id, callbacks = await _start_persisted(terminal_manager, tmp_path)
    context = make_context(tmp_path, result_persisted_hook=callbacks.append)
    await terminal_manager.resize_for_operator(terminal_id, columns=160, rows=48)
    await call(terminal_manager, context, {"action": "status", "terminal_id": terminal_id})
    await call(terminal_manager, context, {"action": "detach", "terminal_id": terminal_id})
    new_context = make_context(tmp_path, session_id="session-b")
    await call(terminal_manager, new_context, {"action": "attach", "terminal_id": terminal_id})
    callbacks.pop()()

    snapshot = await terminal_manager.snapshot(
        terminal_id, TerminalOwner("project-a", "agent-a", "session-b")
    )
    assert "size_change" not in snapshot
    assert (snapshot["columns"], snapshot["rows"]) == (160, 48)


@pytest.mark.asyncio
async def test_only_a_persisted_current_screen_acknowledges_resize_and_attention(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, factory = manager
    callbacks: list[Callable[[], None]] = []
    context = make_context(tmp_path, result_persisted_hook=callbacks.append)
    started = await call(terminal_manager, context, {"action": "start", "command": "fake-tui"})
    terminal_id = cast(dict[str, Any], started["data"])["terminal_id"]
    callbacks.pop()()

    def info() -> TerminalInfo:
        return terminal_manager.terminal(terminal_id, OWNER)

    await terminal_manager.resize_for_operator(terminal_id, columns=100, rows=30)
    await terminal_manager.send_operator_input(terminal_id, "next")
    factory.adapters[0].emit("new prompt")
    await eventually(lambda: info().attention_revision > 0)
    acknowledged = info().acknowledged_attention_revision

    page = await call(
        terminal_manager, context, {"action": "status", "terminal_id": terminal_id, "start_line": 0}
    )
    assert "screen" not in page["data"]
    assert callbacks == []
    assert info().acknowledged_attention_revision == acknowledged

    current = await call(
        terminal_manager, context, {"action": "status", "terminal_id": terminal_id}
    )
    assert current["data"]["screen"] == "new prompt"
    assert "size_change" in current["data"]
    assert info().acknowledged_attention_revision == acknowledged
    callbacks.pop()()
    assert info().acknowledged_attention_revision == info().attention_revision
    assert "size_change" not in await terminal_manager.snapshot(terminal_id, OWNER)

    # input shows the screen its output paused on. That screen was seen, so the idle
    # boundary after the input records nothing, and a wait ends only at its timeout.
    shown = info().attention_revision
    replied = await call(
        terminal_manager,
        context,
        {"action": "input", "terminal_id": terminal_id, "text": "go", "key": "enter"},
    )
    assert replied["data"]["screen"] == "new prompt"
    while callbacks:
        callbacks.pop()()
    assert await terminal_manager.wait(terminal_id, OWNER, seconds=0.2) == "timeout"
    assert info().attention_revision == shown
