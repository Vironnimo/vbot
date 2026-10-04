"""Terminal Tool: list, status paging, and what a persisted screen acknowledges."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from core.tools.terminal_manager import TerminalInfo, TerminalManager, TerminalOwner
from core.tools.tools import JsonObject
from tests.core.tools.terminal_helpers import call, details, make_context
from tests.core.tools.terminal_helpers import manager as manager
from tests.core.tools.terminal_manager_helpers import AdapterFactory, eventually
from tests.core.tools.terminal_manager_helpers import shell_environment as shell_environment

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
    assert [(item["terminal_id"], item["attachment"]) for item in terminals] == [
        (terminal_id, "current")
    ]
    assert terminals[0]["title"] == "Codex migration"
    [shown] = details(terminal_manager, tmp_path, {"action": "list"}, listed)
    assert shown["type"] == "results"
    assert shown["items"][0]["title"] == "Codex migration"
    assert shown["items"][0]["meta"].endswith(" · attached here")
    discovered = await call(
        terminal_manager, make_context(tmp_path, session_id="other"), {"action": "list"}
    )
    other_terminals = cast(dict[str, Any], discovered["data"])["terminals"]
    assert [(item["terminal_id"], item["attachment"]) for item in other_terminals] == [
        (terminal_id, "other")
    ]
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
    assert full_screen["alternate_screen"] is True
    assert full_screen["note"]
    await _render(terminal_manager, factory, terminal_id, "\x1b[?1049l")
    primary = await status()
    assert primary["screen"].splitlines()[-1] == "more-4"
    assert not {"alternate_screen", "note"} & set(primary)


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"action": "start", "command": "   "}, "command"),
        ({"action": "input", "terminal_id": "missing", "data": "raw", "key": "enter"}, "data"),
        ({"action": "resize", "terminal_id": "missing", "columns": 120}, "rows"),
        ({"action": "status", "terminal_id": "missing", "lines": 150}, "lines"),
        ({"action": "status", "terminal_id": "missing", "cursor": "signed"}, "cursor"),
        ({"action": "unknown"}, "action"),
    ],
    ids=[
        "blank-command",
        "data-with-key",
        "resize-without-rows",
        "too-many-lines",
        "cursor",
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

    def fail(rows: int, columns: int) -> None:
        raise OSError("resize ioctl failed")

    monkeypatch.setattr(factory.adapters[0], "resize", fail)

    result = await call(
        terminal_manager,
        context,
        {"action": "resize", "terminal_id": terminal_id, "columns": 100, "rows": 30},
    )

    assert result["error"] == {
        "code": "tool_execution_error",
        "message": "terminal failed while running: resize ioctl failed. It is unknown how much "
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
@pytest.mark.parametrize("action", ["status", "wait", "kill"])
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
        arguments["timeout_ms"] = 0
    result = await call(terminal_manager, context, arguments)
    data = cast(dict[str, Any], result["data"])
    assert (data["columns"], data["rows"]) == (100, 30)
    change = data["size_change"]
    assert (change["previous_columns"], change["previous_rows"]) == (80, 24)
    assert isinstance(change["notice"], str) and change["notice"]
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
    assert (data["columns"], data["rows"]) == (160, 48)


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
