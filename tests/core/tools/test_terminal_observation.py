"""Terminal Tool: list, status paging, and what a persisted screen acknowledges."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from core.tools.terminal_manager import TerminalManager, TerminalOwner
from core.tools.tools import JsonObject
from tests.core.tools.terminal_helpers import call, make_context
from tests.core.tools.terminal_helpers import manager as manager
from tests.core.tools.terminal_manager_helpers import AdapterFactory, eventually
from tests.core.tools.terminal_manager_helpers import shell_environment as shell_environment

OWNER = TerminalOwner("project-a", "agent-a", "session-a")


async def _start_with_lines(
    terminal_manager: TerminalManager, factory: AdapterFactory, tmp_path: Path, prefix: str = ""
) -> tuple[str, Any]:
    started = await call(
        terminal_manager, make_context(tmp_path), {"action": "start", "command": "fake-tui"}
    )
    terminal_id = cast(dict[str, Any], started["data"])["terminal_id"]
    session = terminal_manager.get_session(terminal_id, OWNER)
    factory.adapters[0].emit(prefix + "".join(f"line-{index}\r\n" for index in range(50)))
    await eventually(lambda: session.renderer.revision > 0)
    return terminal_id, session


@pytest.mark.asyncio
async def test_list_shows_every_terminal_with_its_title_and_attachment(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, factory = manager
    terminal_id, _session = await _start_with_lines(
        terminal_manager, factory, tmp_path, prefix="\x1b]0;Codex migration\x07"
    )
    context = make_context(tmp_path)

    listed = await call(terminal_manager, context, {"action": "list"})
    terminals = cast(dict[str, Any], listed["data"])["terminals"]
    assert [(item["terminal_id"], item["attachment"]) for item in terminals] == [
        (terminal_id, "current")
    ]
    assert terminals[0]["title"] == "Codex migration"
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
    terminal_id, session = await _start_with_lines(terminal_manager, factory, tmp_path)
    context = make_context(tmp_path)

    async def status(**fields: Any) -> dict[str, Any]:
        result = await call(
            terminal_manager, context, {"action": "status", "terminal_id": terminal_id, **fields}
        )
        return cast(dict[str, Any], result["data"])

    first = await status(start_line=0, lines=3)
    assert "screen" not in first
    assert first["history"] == "line-0\nline-1\nline-2"
    first_scrollback = first["scrollback"]
    assert "text" not in first_scrollback
    assert "next_cursor" not in first_scrollback
    assert (
        first_scrollback["total_lines"],
        first_scrollback["start_line"],
        first_scrollback["end_line"],
        first_scrollback["next_start_line"],
    ) == (50, 0, 3, 3)
    assert first_scrollback["next_request"] == {
        "action": "status",
        "terminal_id": terminal_id,
        "start_line": 3,
        "lines": 3,
    }
    followed = await call(terminal_manager, context, first_scrollback["next_request"])
    assert followed["data"]["history"] == "line-3\nline-4\nline-5"
    assert followed["data"]["scrollback"]["next_start_line"] == 6

    tail = await status(start_line=48, lines=100)
    assert "screen" not in tail
    assert (
        tail["scrollback"]["line_count"],
        tail["scrollback"]["end_line"],
        tail["scrollback"]["next_start_line"],
        tail["scrollback"]["next_request"],
    ) == (2, 50, None, None)

    current = await status(lines=3)
    assert current["screen"] == session.renderer.screen_text()
    assert current["history"]
    assert current["scrollback"]["line_count"] == 3
    assert current["scrollback"]["next_request"] == {
        "action": "status",
        "terminal_id": terminal_id,
        "start_line": current["scrollback"]["next_start_line"],
        "lines": 3,
    }

    all_lines: list[str] = []
    request: JsonObject | None = {
        "action": "status",
        "terminal_id": terminal_id,
        "start_line": 0,
        "lines": 7,
    }
    while request is not None:
        page = await call(terminal_manager, context, request)
        assert "screen" not in page["data"]
        all_lines.extend(page["data"]["history"].splitlines())
        request = page["data"]["scrollback"]["next_request"]
    assert all_lines == [f"line-{index}" for index in range(50)]


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
    observed = terminal_manager.get_session(terminal_id, OWNER).observed_screen
    assert observed is not None
    assert observed[1:] == (160, 48)


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
    session = terminal_manager.get_session(terminal_id, OWNER)
    await terminal_manager.resize_for_operator(terminal_id, columns=100, rows=30)
    await terminal_manager.send_operator_input(terminal_id, "next")
    factory.adapters[0].emit("new prompt")
    await eventually(lambda: session.attention_revision > 0)
    acknowledged = session.acknowledged_attention_revision

    page = await call(
        terminal_manager, context, {"action": "status", "terminal_id": terminal_id, "start_line": 0}
    )
    assert "screen" not in page["data"]
    assert callbacks == []
    assert session.acknowledged_attention_revision == acknowledged

    current = await call(
        terminal_manager, context, {"action": "status", "terminal_id": terminal_id}
    )
    assert current["data"]["screen"] == "new prompt"
    assert "size_change" in current["data"]
    assert session.acknowledged_attention_revision == acknowledged
    callbacks.pop()()
    assert session.acknowledged_attention_revision == session.attention_revision
    assert "size_change" not in await terminal_manager.snapshot(terminal_id, OWNER)
