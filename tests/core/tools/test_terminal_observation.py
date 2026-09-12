"""Terminal: observation behavior."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from core.projects import ProjectStore
from core.tools.terminal import (
    register_terminal_tool,
)
from core.tools.terminal_manager import TerminalManager, TerminalOwner
from core.tools.tools import JsonObject, ToolRegistry, tool_failure
from tests.core.tools.terminal_helpers import (
    call,
    make_context,
)
from tests.core.tools.terminal_helpers import (
    manager as manager,
)
from tests.core.tools.terminal_manager_helpers import AdapterFactory, eventually


@pytest.mark.asyncio
async def test_list_discovers_terminal_attachment_state_and_status_paginates(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, factory = manager
    context = make_context(tmp_path)
    started = await call(
        terminal_manager,
        context,
        {"action": "start", "command": "fake-tui"},
    )
    terminal_id = cast(dict[str, Any], started["data"])["terminal_id"]
    session = terminal_manager.get_session(
        terminal_id, TerminalOwner("project-a", "agent-a", "session-a")
    )
    factory.adapters[0].emit(
        "\x1b]0;Codex migration\x07" + "".join(f"line-{index}\r\n" for index in range(50))
    )
    await eventually(lambda: session.renderer.revision > 0)

    listed = await call(terminal_manager, context, {"action": "list"})
    terminals = cast(dict[str, Any], listed["data"])["terminals"]
    assert [item["terminal_id"] for item in terminals] == [terminal_id]
    assert terminals[0]["title"] == "Codex migration"
    assert terminals[0]["attachment"] == "current"
    discovered = await call(
        terminal_manager, make_context(tmp_path, session_id="other"), {"action": "list"}
    )
    other_terminals = cast(dict[str, Any], discovered["data"])["terminals"]
    assert [item["terminal_id"] for item in other_terminals] == [terminal_id]
    assert other_terminals[0]["attachment"] == "other"

    status = await call(
        terminal_manager,
        context,
        {"action": "status", "terminal_id": terminal_id, "lines": 3},
    )
    status_data = cast(dict[str, Any], status["data"])
    assert status_data["title"] == "Codex migration"
    scrollback = status_data["scrollback"]
    assert scrollback["line_count"] == 3
    assert scrollback["next_start_line"] is not None
    assert scrollback["next_request"] == {
        "action": "status",
        "terminal_id": terminal_id,
        "start_line": scrollback["next_start_line"],
        "lines": 3,
    }
    continued = await call(
        terminal_manager,
        context,
        cast(dict[str, Any], scrollback["next_request"]),
    )
    assert continued["ok"] is True
    continued_scrollback = cast(dict[str, Any], continued["data"])["scrollback"]
    assert continued_scrollback["line_count"] == 3
    assert continued_scrollback["next_request"]["lines"] == 3

    larger_continuation = await call(
        terminal_manager,
        context,
        {
            "action": "status",
            "terminal_id": terminal_id,
            "start_line": scrollback["next_start_line"],
            "lines": 100,
        },
    )
    assert larger_continuation["ok"] is True
    assert cast(dict[str, Any], larger_continuation["data"])["scrollback"]["line_count"] > 3


@pytest.mark.asyncio
async def test_status_pages_forward_with_absolute_start_line(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, factory = manager
    context = make_context(tmp_path)
    started = await call(
        terminal_manager,
        context,
        {"action": "start", "command": "fake-tui"},
    )
    terminal_id = cast(dict[str, Any], started["data"])["terminal_id"]
    session = terminal_manager.get_session(
        terminal_id, TerminalOwner("project-a", "agent-a", "session-a")
    )
    factory.adapters[0].emit("".join(f"line-{index}\r\n" for index in range(50)))
    await eventually(lambda: session.renderer.revision > 0)

    first = await call(
        terminal_manager,
        context,
        {"action": "status", "terminal_id": terminal_id, "start_line": 0, "lines": 3},
    )
    first_scrollback = cast(dict[str, Any], first["data"])["scrollback"]
    assert "screen" not in first["data"]
    assert first_scrollback["text"] == "line-0\nline-1\nline-2"
    assert first_scrollback["total_lines"] == 50
    assert first_scrollback["start_line"] == 0
    assert first_scrollback["end_line"] == 3
    assert first_scrollback["next_start_line"] == 3
    assert "next_cursor" not in first_scrollback
    assert first_scrollback["next_request"] == {
        "action": "status",
        "terminal_id": terminal_id,
        "start_line": 3,
        "lines": 3,
    }

    followed = await call(
        terminal_manager,
        context,
        cast(dict[str, Any], first_scrollback["next_request"]),
    )
    followed_scrollback = cast(dict[str, Any], followed["data"])["scrollback"]
    assert followed_scrollback["text"] == "line-3\nline-4\nline-5"
    assert followed_scrollback["start_line"] == 3
    assert followed_scrollback["next_start_line"] == 6

    tail = await call(
        terminal_manager,
        context,
        {"action": "status", "terminal_id": terminal_id, "start_line": 48, "lines": 100},
    )
    tail_scrollback = cast(dict[str, Any], tail["data"])["scrollback"]
    assert "screen" not in tail["data"]
    assert tail_scrollback["line_count"] == 2
    assert tail_scrollback["end_line"] == 50
    assert tail_scrollback["next_start_line"] is None
    assert tail_scrollback["next_request"] is None

    current = await call(
        terminal_manager, context, {"action": "status", "terminal_id": terminal_id}
    )
    assert current["data"]["screen"] == session.renderer.screen_text()
    assert current["data"]["scrollback"]["text"]
    all_lines = []
    request = {"action": "status", "terminal_id": terminal_id, "start_line": 0, "lines": 7}
    while request is not None:
        page = await call(terminal_manager, context, request)
        assert "screen" not in page["data"]
        all_lines.extend(page["data"]["scrollback"]["text"].splitlines())
        request = page["data"]["scrollback"]["next_request"]
    assert all_lines == [f"line-{index}" for index in range(50)]


@pytest.mark.asyncio
async def test_status_rejects_cursor_parameter_as_unknown(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, factory = manager
    context = make_context(tmp_path)
    started = await call(
        terminal_manager,
        context,
        {"action": "start", "command": "fake-tui"},
    )
    terminal_id = cast(dict[str, Any], started["data"])["terminal_id"]

    rejected = await call(
        terminal_manager,
        context,
        {
            "action": "status",
            "terminal_id": terminal_id,
            "cursor": "some-signed-cursor",
        },
    )
    assert rejected["ok"] is False
    assert cast(dict[str, Any], rejected["error"])["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_input_supports_convenient_and_exact_data_and_rejects_stale_screen(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, factory = manager
    context = make_context(tmp_path)
    started = await call(
        terminal_manager,
        context,
        {"action": "start", "command": "fake-tui"},
    )
    terminal_id = cast(dict[str, Any], started["data"])["terminal_id"]
    session = terminal_manager.get_session(
        terminal_id, TerminalOwner("project-a", "agent-a", "session-a")
    )
    factory.adapters[0].emit("QUESTION> ")
    await eventually(lambda: session.renderer.revision > 0)

    stale = await call(
        terminal_manager,
        context,
        {
            "action": "input",
            "terminal_id": terminal_id,
            "text": "answer",
            "expected_screen_revision": 0,
        },
    )
    assert stale == tool_failure(
        "stale_screen",
        "Terminal screen changed; inspect status before sending this input",
        retryable=True,
    )

    result = await call(
        terminal_manager,
        context,
        {
            "action": "input",
            "terminal_id": terminal_id,
            "text": "answer",
            "expected_screen_revision": session.renderer.revision,
        },
    )
    assert result["ok"] is True
    assert factory.adapters[0].writes == ["answer"]
    assert cast(dict[str, Any], result["data"])["key"] is None
    assert cast(dict[str, Any], result["data"])["delivery"] == ("automatic_terminal_activity")

    confirmed = await call(
        terminal_manager,
        context,
        {
            "action": "input",
            "terminal_id": terminal_id,
            "key": "enter",
        },
    )
    assert confirmed["ok"] is True
    assert factory.adapters[0].writes[-1] == "\r"
    assert cast(dict[str, Any], confirmed["data"])["key"] == "enter"

    submitted = await call(
        terminal_manager,
        context,
        {
            "action": "input",
            "terminal_id": terminal_id,
            "text": "submit",
            "key": "enter",
        },
    )
    assert submitted["ok"] is True
    assert factory.adapters[0].writes[-2:] == ["submit", "\r"]

    raw = "\x1b[200~more\r\n\x1b[201~"
    exact = await call(
        terminal_manager,
        context,
        {"action": "input", "terminal_id": terminal_id, "data": raw},
    )
    assert exact["ok"] is True
    assert factory.adapters[0].writes[-1] == raw

    factory.adapters[0].emit("\x1b[?2004h")
    await eventually(lambda: session.renderer.bracketed_paste_enabled)
    multiline = "first\n  second"
    pasted = await call(
        terminal_manager,
        context,
        {"action": "input", "terminal_id": terminal_id, "text": multiline},
    )
    assert pasted["ok"] is True
    assert factory.adapters[0].writes[-1] == f"\x1b[200~{multiline}\x1b[201~"
    assert cast(dict[str, Any], pasted["data"])["bracketed_paste"] is True


@pytest.mark.asyncio
async def test_manual_attention_result_acknowledges_only_after_persistence(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, _factory = manager
    callbacks: list[Callable[[], None]] = []
    context = make_context(
        tmp_path, result_persisted_hook=lambda callback: callbacks.append(callback)
    )
    started = await call(
        terminal_manager,
        context,
        {"action": "start", "command": "fake-tui"},
    )
    terminal_id = cast(dict[str, Any], started["data"])["terminal_id"]
    session = terminal_manager.get_session(
        terminal_id, TerminalOwner("project-a", "agent-a", "session-a")
    )
    callbacks.pop()()
    terminal_manager._io._set_attention(
        session,
        kind="output_settled",
        summary="Output is quiet.",
        details={"screen_revision": 0},
        deliver=False,
    )

    status = await call(
        terminal_manager,
        context,
        {"action": "status", "terminal_id": terminal_id},
    )
    assert status["ok"] is True
    assert session.acknowledged_attention_revision == 0
    assert len(callbacks) == 1
    callbacks.pop()()
    assert session.acknowledged_attention_revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["status", "wait", "kill"])
async def test_resize_notice_is_consumed_only_with_a_persisted_screen(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path, action: str
) -> None:
    terminal_manager, _factory = manager
    callbacks: list[Callable[[], None]] = []
    context = make_context(tmp_path, result_persisted_hook=callbacks.append)
    started = await call(terminal_manager, context, {"action": "start"})
    data = cast(dict[str, Any], started["data"])
    terminal_id = data["terminal_id"]
    assert "size_change" not in data
    callbacks.pop()()

    for columns, rows in [(70, 20), (160, 48), (100, 30)]:
        await terminal_manager.resize_for_operator(terminal_id, columns=columns, rows=rows)
    arguments: JsonObject = {"action": action, "terminal_id": terminal_id}
    if action == "wait":
        arguments["timeout_ms"] = 0
    result = await call(terminal_manager, context, arguments)
    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    assert (data["columns"], data["rows"]) == (100, 30)
    change = data["size_change"]
    assert (change["previous_columns"], change["previous_rows"]) == (80, 24)
    assert isinstance(change["notice"], str) and change["notice"]
    owner = TerminalOwner("project-a", "agent-a", "session-a")
    assert "size_change" in await terminal_manager.snapshot(terminal_id, owner)
    callbacks.pop()()
    assert "size_change" not in await terminal_manager.snapshot(terminal_id, owner)


@pytest.mark.asyncio
async def test_resize_after_screen_capture_remains_unseen_after_persistence(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, _factory = manager
    callbacks: list[Callable[[], None]] = []
    context = make_context(tmp_path, result_persisted_hook=callbacks.append)
    started = await call(terminal_manager, context, {"action": "start"})
    terminal_id = cast(dict[str, Any], started["data"])["terminal_id"]
    callbacks.pop()()
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
    callbacks: list[Callable[[], None]] = []
    context = make_context(tmp_path, result_persisted_hook=callbacks.append)
    started = await call(terminal_manager, context, {"action": "start"})
    terminal_id = cast(dict[str, Any], started["data"])["terminal_id"]
    callbacks.pop()()
    for columns, rows in [(140, 40), (160, 48)]:
        await terminal_manager.resize_for_operator(terminal_id, columns=columns, rows=rows)
        await call(terminal_manager, context, {"action": "status", "terminal_id": terminal_id})
    callbacks.pop()()
    callbacks.pop()()
    owner = TerminalOwner("project-a", "agent-a", "session-a")
    assert "size_change" not in await terminal_manager.snapshot(terminal_id, owner)
    observed = terminal_manager.get_session(terminal_id, owner).observed_screen
    assert observed is not None
    assert observed[1:] == (160, 48)


@pytest.mark.asyncio
async def test_returning_to_previous_size_still_reports_intermediate_resizes(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, _factory = manager
    callbacks: list[Callable[[], None]] = []
    context = make_context(tmp_path, result_persisted_hook=callbacks.append)
    started = await call(terminal_manager, context, {"action": "start"})
    terminal_id = cast(dict[str, Any], started["data"])["terminal_id"]
    callbacks.pop()()
    owner = TerminalOwner("project-a", "agent-a", "session-a")
    await terminal_manager.resize_for_operator(terminal_id, columns=80, rows=24)
    assert "size_change" not in await terminal_manager.snapshot(terminal_id, owner)
    await terminal_manager.resize_for_operator(terminal_id, columns=160, rows=48)
    await terminal_manager.resize_for_operator(terminal_id, columns=80, rows=24)
    snapshot = await terminal_manager.snapshot(terminal_id, owner)
    assert "size_change" in snapshot
    assert (snapshot["columns"], snapshot["rows"]) == (80, 24)


@pytest.mark.asyncio
async def test_new_attachment_does_not_inherit_another_sessions_screen_observation(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, _factory = manager
    callbacks: list[Callable[[], None]] = []
    context = make_context(tmp_path, result_persisted_hook=callbacks.append)
    started = await call(terminal_manager, context, {"action": "start"})
    terminal_id = cast(dict[str, Any], started["data"])["terminal_id"]
    callbacks.pop()()
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


@pytest.mark.parametrize(
    "arguments",
    (
        {"action": "start", "command": "   "},
        {"action": "list", "terminal_id": "not-accepted"},
        {"action": "status"},
        {"action": "input", "terminal_id": "missing", "key": "space"},
        {
            "action": "input",
            "terminal_id": "missing",
            "data": "raw",
            "enter": True,
        },
        {"action": "resize", "terminal_id": "missing", "columns": 120},
        {"action": "unknown"},
    ),
)
@pytest.mark.asyncio
async def test_invalid_or_inapplicable_arguments_return_stable_failure(
    manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    arguments: JsonObject,
) -> None:
    result = await call(manager[0], make_context(tmp_path), arguments)
    assert result["ok"] is False
    assert cast(dict[str, Any], result["error"])["code"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_history_page_does_not_acknowledge_an_unseen_resize_or_attention(manager, tmp_path):
    terminal_manager, factory = manager
    callbacks = []
    context = make_context(tmp_path, result_persisted_hook=callbacks.append)
    started = await call(terminal_manager, context, {"action": "start", "command": "fake-tui"})
    terminal_id = started["data"]["terminal_id"]
    callbacks.pop()()
    owner = TerminalOwner("project-a", "agent-a", "session-a")
    session = terminal_manager.get_session(terminal_id, owner)
    await terminal_manager.resize_for_operator(terminal_id, columns=100, rows=30)
    await terminal_manager.send_operator_input(terminal_id, "next")
    factory.adapters[0].emit("new prompt")
    await eventually(lambda: session.attention_revision > 0)
    acknowledged = session.acknowledged_attention_revision
    page = await call(
        terminal_manager,
        context,
        {
            "action": "status",
            "terminal_id": terminal_id,
            "start_line": 0,
        },
    )
    assert page["ok"]
    assert "screen" not in page["data"]
    assert callbacks == []
    assert session.acknowledged_attention_revision == acknowledged
    current = await call(
        terminal_manager, context, {"action": "status", "terminal_id": terminal_id}
    )
    assert current["data"]["screen"] == "new prompt"
    assert "size_change" in current["data"]
    callbacks.pop()()
    assert session.acknowledged_attention_revision == session.attention_revision
    assert "size_change" not in await terminal_manager.snapshot(terminal_id, owner)


def test_terminal_definition_budget_and_wire_contract(tmp_path):
    from core.providers.tool_schema import render_tool_definitions
    from core.utils.tokens import estimate_json_tokens

    registry = ToolRegistry()
    register_terminal_tool(
        registry, TerminalManager(adapter_factory=AdapterFactory()), ProjectStore(tmp_path)
    )
    definitions = registry.provider_definitions(allowed_tools=["terminal"])
    assert estimate_json_tokens(definitions[0])[0] <= 900
    for profile in ("explicit_non_strict", "omit_strict"):
        rendered = render_tool_definitions(definitions, profile=profile)[0]
        assert rendered["parameters"] == definitions[0]["parameters"]
        assert rendered.get("strict") is (False if profile == "explicit_non_strict" else None)
