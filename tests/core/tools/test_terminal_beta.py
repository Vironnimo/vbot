"""Tests for the Agent-facing terminal_beta Tool contract."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio

from core.projects import ProjectStore
from core.tools.terminal_beta import (
    TERMINAL_BETA_ACTIONS,
    TERMINAL_BETA_DEFAULT_COMMAND,
    TERMINAL_BETA_DEFAULT_WAIT_MS,
    TERMINAL_BETA_PROJECT_WORKDIR_PREFIX,
    TERMINAL_BETA_TOOL_DESCRIPTION,
    TERMINAL_BETA_TOOL_NAME,
    TERMINAL_BETA_TOOL_PARAMETERS,
    make_terminal_beta_handler,
    register_terminal_beta_tool,
)
from core.tools.terminal_manager import TerminalManager, TerminalOwner
from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_failure
from core.utils.paths import model_path
from tests.core.tools.test_terminal_manager import AdapterFactory, eventually


@pytest_asyncio.fixture
async def manager() -> AsyncIterator[tuple[TerminalManager, AdapterFactory]]:
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


def make_context(
    tmp_path: Path,
    *,
    session_id: str = "session-a",
    result_persisted_hook: Callable[[Callable[[], None]], None] | None = None,
) -> ToolContext:
    return ToolContext(
        agent_id="agent-a",
        session_id=session_id,
        run_id="run-a",
        tool_call_id="call-a",
        tool_name=TERMINAL_BETA_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
        cwd=tmp_path,
        project_id="project-a",
        result_persisted_hook=result_persisted_hook,
    )


async def call(
    manager: TerminalManager,
    context: ToolContext,
    arguments: JsonObject,
    projects: ProjectStore | None = None,
) -> JsonObject:
    project_store = projects if projects is not None else ProjectStore(context.data_root)
    return cast(
        JsonObject,
        await make_terminal_beta_handler(manager, project_store)(context, arguments),
    )


def test_schema_matches_flat_action_tool_conventions(tmp_path: Path) -> None:
    assert TERMINAL_BETA_TOOL_PARAMETERS["type"] == "object"
    assert TERMINAL_BETA_TOOL_PARAMETERS["required"] == ["action"]
    assert "oneOf" not in TERMINAL_BETA_TOOL_PARAMETERS
    assert "additionalProperties" not in TERMINAL_BETA_TOOL_PARAMETERS
    properties = cast(dict[str, Any], TERMINAL_BETA_TOOL_PARAMETERS["properties"])
    assert properties["action"]["enum"] == list(TERMINAL_BETA_ACTIONS)
    assert properties["columns"]["default"] == 120
    assert properties["rows"]["default"] == 32
    assert properties["lines"]["default"] == 30
    assert properties["timeout_ms"]["default"] == TERMINAL_BETA_DEFAULT_WAIT_MS
    assert properties["command"]["default"] == TERMINAL_BETA_DEFAULT_COMMAND
    assert "no program-specific flags" in properties["command"]["description"]
    assert "sent unchanged" in properties["data"]["description"]
    assert properties["data"]["maxLength"] == 65_536
    assert properties["text"]["maxLength"] == 65_536
    assert "f12" in properties["key"]["enum"]
    assert "ctrl_z" in properties["key"]["enum"]
    assert "next_request unchanged" in properties["cursor"]["description"]
    assert "May be used with or without cursor" in properties["lines"]["description"]
    assert properties["workdir"]["description"] == (
        "Working directory for start. Relative paths use the current working directory; omit "
        "for that directory. Use 'project:<project-id>' to start in a registered Project's "
        "directory."
    )
    assert "survive individual Runs" in TERMINAL_BETA_TOOL_DESCRIPTION
    assert "without program-specific flags, hooks, or configuration" in (
        TERMINAL_BETA_TOOL_DESCRIPTION
    )
    assert "Quiet output is only an activity boundary" in TERMINAL_BETA_TOOL_DESCRIPTION

    registry = ToolRegistry()
    register_terminal_beta_tool(
        registry,
        TerminalManager(adapter_factory=AdapterFactory()),
        ProjectStore(tmp_path),
    )
    tool = registry.get(TERMINAL_BETA_TOOL_NAME)
    assert tool.open_input_schema is True
    assert tool.display.summary({"action": "start"}) == "start · codex"
    with pytest.raises(ValueError, match="greater than the maximum of 100"):
        tool.contract.validate_arguments(
            {
                "action": "status",
                "terminal_id": "terminal-a",
                "cursor": "signed-cursor",
                "lines": 150,
            }
        )


@pytest.mark.asyncio
async def test_start_defaults_to_unmodified_codex_and_returns_non_polling_handoff(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, factory = manager
    result = await call(terminal_manager, make_context(tmp_path), {"action": "start"})

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    assert data["state"] == "ready"
    assert data["command"] == TERMINAL_BETA_DEFAULT_COMMAND
    assert data["columns"] == 120
    assert data["rows"] == 32
    assert data["delivery"] == "automatic_terminal_activity"
    assert "continues independently of this vBot Run" in data["handoff_note"]
    assert "Quiet output does not prove" in data["handoff_note"]
    assert "do not poll" in data["handoff_note"]
    assert factory.calls[0][0] == [TERMINAL_BETA_DEFAULT_COMMAND]
    assert not any(name.startswith("VBOT_TERMINAL_") for name in factory.calls[0][2])


@pytest.mark.asyncio
async def test_start_resolves_live_project_cwd_by_stable_id_without_changing_owner(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, factory = manager
    first_repo = tmp_path / "first-repo"
    second_repo = tmp_path / "second-repo"
    first_repo.mkdir()
    second_repo.mkdir()
    projects = ProjectStore(tmp_path / "data")
    projects.create("vbot", "Renamable Project", first_repo)
    context = make_context(tmp_path)

    first = await call(
        terminal_manager,
        context,
        {
            "action": "start",
            "command": "fake-tui",
            "workdir": f"{TERMINAL_BETA_PROJECT_WORKDIR_PREFIX}vbot",
        },
        projects,
    )

    assert first["ok"] is True
    first_data = cast(dict[str, Any], first["data"])
    assert first_data["workdir"] == model_path(first_repo.resolve())
    assert factory.calls[0][1] == first_repo.resolve()
    terminal_manager.get_session(
        first_data["terminal_id"],
        TerminalOwner("project-a", "agent-a", "session-a"),
    )

    projects.update("vbot", display_name="Different Name", cwd=second_repo)
    second = await call(
        terminal_manager,
        context,
        {
            "action": "start",
            "command": "fake-tui",
            "workdir": f"{TERMINAL_BETA_PROJECT_WORKDIR_PREFIX}vbot",
        },
        projects,
    )

    assert second["ok"] is True
    assert factory.calls[1][1] == second_repo.resolve()


@pytest.mark.asyncio
async def test_start_keeps_relative_workdir_resolution_unchanged(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, factory = manager
    child = tmp_path / "child"
    child.mkdir()

    result = await call(
        terminal_manager,
        make_context(tmp_path),
        {"action": "start", "command": "fake-tui", "workdir": "child"},
    )

    assert result["ok"] is True
    assert factory.calls[0][1] == child.resolve()


@pytest.mark.asyncio
async def test_start_rejects_unresolvable_project_workdirs_before_spawn(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, factory = manager
    projects = ProjectStore(tmp_path / "data")
    projects.create("offline", "Offline", tmp_path / "missing-repo")
    context = make_context(tmp_path)

    missing = await call(
        terminal_manager,
        context,
        {"action": "start", "workdir": "project:missing"},
        projects,
    )
    unavailable = await call(
        terminal_manager,
        context,
        {"action": "start", "workdir": "project:offline"},
        projects,
    )
    empty = await call(
        terminal_manager,
        context,
        {"action": "start", "workdir": "project:"},
        projects,
    )

    assert cast(dict[str, Any], missing["error"])["code"] == "project_not_found"
    assert cast(dict[str, Any], unavailable["error"])["code"] == "project_unavailable"
    assert cast(dict[str, Any], empty["error"])["code"] == "invalid_arguments"
    assert factory.calls == []


@pytest.mark.asyncio
async def test_list_is_session_scoped_and_status_paginates(
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
    hidden = await call(
        terminal_manager, make_context(tmp_path, session_id="other"), {"action": "list"}
    )
    assert cast(dict[str, Any], hidden["data"])["terminals"] == []

    status = await call(
        terminal_manager,
        context,
        {"action": "status", "terminal_id": terminal_id, "lines": 3},
    )
    status_data = cast(dict[str, Any], status["data"])
    assert status_data["title"] == "Codex migration"
    scrollback = status_data["scrollback"]
    assert scrollback["line_count"] == 3
    assert scrollback["next_cursor"] is not None
    assert scrollback["next_request"] == {
        "action": "status",
        "terminal_id": terminal_id,
        "cursor": scrollback["next_cursor"],
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
            "cursor": scrollback["next_cursor"],
            "lines": 100,
        },
    )
    assert larger_continuation["ok"] is True
    assert cast(dict[str, Any], larger_continuation["data"])["scrollback"]["line_count"] > 3


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
    assert factory.adapters[0].writes == ["answer", "\r"]
    assert cast(dict[str, Any], result["data"])["delivery"] == ("automatic_terminal_activity")

    raw = "\x1b[200~more\r\n\x1b[201~"
    exact = await call(
        terminal_manager,
        context,
        {"action": "input", "terminal_id": terminal_id, "data": raw},
    )
    assert exact["ok"] is True
    assert factory.adapters[0].writes[-1] == raw


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
    terminal_manager._set_attention(
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
            "enter": False,
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
