"""Terminal: launch behavior."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from core.projects import ProjectStore
from core.tools import terminal as terminal_module
from core.tools import terminal_manager as manager_module
from core.tools.terminal import (
    TERMINAL_ACTIONS,
    TERMINAL_DEFAULT_WAIT_MS,
    TERMINAL_PROJECT_WORKDIR_PREFIX,
    TERMINAL_TOOL_DESCRIPTION,
    TERMINAL_TOOL_NAME,
    TERMINAL_TOOL_PARAMETERS,
    register_terminal_tool,
)
from core.tools.terminal_manager import TerminalManager, TerminalOwner
from core.tools.tools import JsonObject, ToolRegistry, tool_failure
from core.utils.paths import model_path
from tests.core.tools.terminal_helpers import (
    call,
    make_context,
)
from tests.core.tools.terminal_helpers import (
    manager as manager,
)
from tests.core.tools.terminal_manager_helpers import (
    AdapterFactory,
    FakeTerminalAdapter,
    eventually,
)


def test_schema_matches_flat_action_tool_conventions(tmp_path: Path) -> None:
    assert TERMINAL_TOOL_PARAMETERS["type"] == "object"
    assert TERMINAL_TOOL_PARAMETERS["required"] == ["action"]
    assert "oneOf" not in TERMINAL_TOOL_PARAMETERS
    assert "additionalProperties" not in TERMINAL_TOOL_PARAMETERS
    properties = cast(dict[str, Any], TERMINAL_TOOL_PARAMETERS["properties"])
    assert properties["action"]["enum"] == list(TERMINAL_ACTIONS)
    assert "default" not in properties["columns"]
    assert "default" not in properties["rows"]
    assert properties["lines"]["default"] == 30
    assert properties["timeout_ms"]["default"] == TERMINAL_DEFAULT_WAIT_MS
    assert "default" not in properties["command"]
    assert properties["name"]["maxLength"] == 80
    assert "enter" not in properties
    assert "enter" in properties["key"]["enum"]
    assert all(
        isinstance(property_schema.get("description"), str) and property_schema["description"]
        for property_schema in properties.values()
    )
    assert properties["data"]["maxLength"] == 65_536
    assert properties["text"]["maxLength"] == 65_536
    assert "f12" in properties["key"]["enum"]
    assert "ctrl_z" in properties["key"]["enum"]
    assert TERMINAL_TOOL_DESCRIPTION

    registry = ToolRegistry()
    register_terminal_tool(
        registry,
        TerminalManager(adapter_factory=AdapterFactory()),
        ProjectStore(tmp_path),
    )
    tool = registry.get(TERMINAL_TOOL_NAME)
    assert tool.open_input_schema is True
    assert tool.display.summary({"action": "start", "command": "codex"}) == "start · codex"
    assert tool.display.summary({"action": "start"}) == "start · default shell"
    list_display = registry.display_for_call(
        TERMINAL_TOOL_NAME,
        {"action": "list"},
        result={
            "ok": True,
            "data": {"terminals": [{"terminal_id": "terminal-a"}]},
            "error": None,
            "artifacts": [],
        },
    )
    assert list_display["facts"] == [
        {"kind": "count", "value": 1, "unit": "results", "at_least": False}
    ]
    with pytest.raises(ValueError):
        tool.contract.validate_arguments(
            {
                "action": "status",
                "terminal_id": "terminal-a",
                "lines": 150,
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [None, "fake-tui"])
async def test_start_uses_compact_default_until_explicit_resize(
    manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str | None,
) -> None:
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda env: ["host-shell"])
    terminal_manager, factory = manager
    context = make_context(tmp_path)
    arguments: JsonObject = {"action": "start"}
    if command is not None:
        arguments["command"] = command
    result = await call(terminal_manager, context, arguments)

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    assert data["state"] == "ready"
    assert data["command"] == (command or "host-shell")
    assert data["columns"] == 80
    assert data["rows"] == 24
    assert data["delivery"] == "automatic_terminal_activity"
    assert factory.calls[0][0] == [command or "host-shell"]
    assert factory.calls[0][3:] == (24, 80)
    assert not any(name.startswith("VBOT_TERMINAL_") for name in factory.calls[0][2])

    await terminal_manager.resize_for_operator(data["terminal_id"], columns=153, rows=43)
    status = await call(
        terminal_manager, context, {"action": "status", "terminal_id": data["terminal_id"]}
    )
    assert status["ok"] is True
    resized = cast(dict[str, Any], status["data"])
    assert (resized["columns"], resized["rows"]) == (153, 43)
    assert factory.adapters[0].resizes == [(43, 153)]


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["codex", "claude-code", "opencode"])
async def test_coding_agent_reference_launches_exact_arguments_and_submits_task(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path, reference: str
) -> None:
    package = Path(__file__).resolve().parents[3] / "resources" / "skills" / "coding-agents"
    document = (package / "references" / f"{reference}.md").read_text(encoding="utf-8")
    examples = re.findall(r"```json\s*(.*?)```", document, re.DOTALL)
    assert examples
    terminal_manager, factory = manager
    registry = ToolRegistry()
    register_terminal_tool(registry, terminal_manager, ProjectStore(tmp_path))
    context = make_context(tmp_path)
    for example in examples:
        arguments = json.loads(example)
        arguments["workdir"] = str(tmp_path)
        result = await registry.dispatch(context, arguments, [TERMINAL_TOOL_NAME])
        assert result["ok"] is True
        assert factory.calls[-1][0] == [arguments["command"], *arguments["args"]]
        assert factory.calls[-1][1] == tmp_path
        adapter = factory.adapters[-1]
        assert adapter.writes == []
        adapter.emit("Ready> ")
        await eventually(
            lambda adapter=adapter, task=arguments["text"]: adapter.writes == [task, "\r"]
        )
        assert adapter.alive


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "launch_error",
    [
        FileNotFoundError(2, "fixture executable is missing", "fixture-missing"),
        PermissionError(13, "fixture launch denied", "fixture-denied"),
        RuntimeError("fixture transport could not initialize"),
    ],
    ids=["missing-executable", "permission-denied", "transport-failure"],
)
async def test_dispatch_returns_launch_failure_and_releases_capacity(
    tmp_path: Path, launch_error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_module, "TERMINAL_MAX_LIVE_PER_SESSION", 1)
    monkeypatch.setattr(manager_module, "TERMINAL_MAX_LIVE_GLOBAL", 1)

    class RecoveringFactory(AdapterFactory):
        failing = True

        def __call__(
            self,
            argv: Sequence[str],
            cwd: Path,
            env: Mapping[str, str],
            rows: int,
            columns: int,
        ) -> FakeTerminalAdapter:
            if self.failing:
                raise launch_error
            return super().__call__(argv, cwd, env, rows, columns)

    factory = RecoveringFactory()
    terminal_manager = TerminalManager(
        adapter_factory=factory,
        sweep_interval_seconds=3600,
    )
    terminal_manager.start()
    try:
        registry = ToolRegistry()
        register_terminal_tool(registry, terminal_manager, ProjectStore(tmp_path))
        context = make_context(tmp_path)
        arguments: JsonObject = {"action": "start", "command": "fixture-command"}
        result = await registry.dispatch(context, arguments, [TERMINAL_TOOL_NAME])
        assert result == tool_failure(
            "terminal_launch_failed",
            f"Terminal process could not be started: {launch_error}",
            retryable=False,
        )
        assert terminal_manager.list_sessions() == []
        assert factory.adapters == []

        factory.failing = False
        recovered = await registry.dispatch(context, arguments, [TERMINAL_TOOL_NAME])
        assert recovered["ok"] is True
        assert len(factory.adapters) == 1
        assert factory.adapters[0].alive
    finally:
        await terminal_manager.aclose()


@pytest.mark.asyncio
async def test_dispatch_ignores_empty_optional_text_when_key_submits_input(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, factory = manager
    registry = ToolRegistry()
    register_terminal_tool(registry, terminal_manager, ProjectStore(tmp_path))
    context = make_context(tmp_path)

    started = await registry.dispatch(
        context,
        {"action": "start", "command": "fake-tui"},
        [TERMINAL_TOOL_NAME],
    )
    terminal_id = cast(dict[str, Any], started["data"])["terminal_id"]

    result = await registry.dispatch(
        context,
        {
            "action": "input",
            "terminal_id": terminal_id,
            "text": "",
            "key": "enter",
        },
        [TERMINAL_TOOL_NAME],
    )

    assert result["ok"] is True
    await eventually(lambda: factory.adapters[0].writes == ["\r"])


@pytest.mark.asyncio
async def test_dispatch_ignores_empty_optional_text_as_successful_noop(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, factory = manager
    registry = ToolRegistry()
    register_terminal_tool(registry, terminal_manager, ProjectStore(tmp_path))
    context = make_context(tmp_path)

    started = await registry.dispatch(
        context,
        {"action": "start", "command": "fake-tui"},
        [TERMINAL_TOOL_NAME],
    )
    terminal_id = cast(dict[str, Any], started["data"])["terminal_id"]

    result = await registry.dispatch(
        context,
        {"action": "input", "terminal_id": terminal_id, "text": ""},
        [TERMINAL_TOOL_NAME],
    )

    data = cast(dict[str, Any], result["data"])
    assert result["ok"] is True
    assert data["characters_sent"] == 0
    assert data["screen_revision"] == 0
    assert factory.adapters[0].writes == []


@pytest.mark.asyncio
async def test_start_accepts_name_and_trimmed_blank_name_fails(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, factory = manager
    context = make_context(tmp_path)
    started = await call(
        terminal_manager,
        context,
        {"action": "start", "command": "fake-tui", "name": "  joe  "},
    )
    started_data = cast(dict[str, Any], started["data"])
    assert started["ok"] is True
    assert started_data["name"] == "joe"

    listed = await call(terminal_manager, context, {"action": "list"})
    terminals = cast(dict[str, Any], listed["data"])["terminals"]
    assert terminals[0]["name"] == "joe"

    status = await call(
        terminal_manager,
        context,
        {"action": "status", "terminal_id": started_data["terminal_id"]},
    )
    assert cast(dict[str, Any], status["data"])["name"] == "joe"

    waited = await call(
        terminal_manager,
        context,
        {
            "action": "wait",
            "terminal_id": started_data["terminal_id"],
            "timeout_ms": 0,
        },
    )
    assert "name" not in cast(dict[str, Any], waited["data"])

    sent = await call(
        terminal_manager,
        context,
        {"action": "input", "terminal_id": started_data["terminal_id"], "text": "x"},
    )
    assert "name" not in cast(dict[str, Any], sent["data"])

    resized = await call(
        terminal_manager,
        context,
        {
            "action": "resize",
            "terminal_id": started_data["terminal_id"],
            "columns": 100,
            "rows": 30,
        },
    )
    assert "name" not in cast(dict[str, Any], resized["data"])

    killed = await call(
        terminal_manager,
        context,
        {"action": "kill", "terminal_id": started_data["terminal_id"]},
    )
    assert "name" not in cast(dict[str, Any], killed["data"])

    blank = await call(
        terminal_manager,
        context,
        {"action": "start", "command": "fake-tui", "name": "   "},
    )
    assert cast(dict[str, Any], blank["error"])["code"] == "invalid_arguments"

    invalid = await call(
        terminal_manager,
        context,
        {"action": "start", "command": "fake-tui", "name": "x" * 81},
    )
    assert cast(dict[str, Any], invalid["error"])["code"] == "invalid_arguments"
    assert len(factory.calls) == 1


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
            "workdir": f"{TERMINAL_PROJECT_WORKDIR_PREFIX}vbot",
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
            "workdir": f"{TERMINAL_PROJECT_WORKDIR_PREFIX}vbot",
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
async def test_attach_grants_full_contract_and_detach_only_removes_binding(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda env: ["host-shell"])
    terminal_manager, factory = manager
    manual = await terminal_manager.spawn_for_operator(
        command=None,
        arguments=[],
        cwd=tmp_path,
        name="afk codex",
    )
    terminal_id = manual["terminal_id"]
    context = make_context(tmp_path)
    other_context = make_context(tmp_path, session_id="session-b")

    listed = await call(terminal_manager, context, {"action": "list"})
    listed_item = cast(dict[str, Any], listed["data"])["terminals"][0]
    assert listed_item["terminal_id"] == terminal_id
    assert listed_item["attachment"] == "none"

    attached = await call(
        terminal_manager,
        context,
        {"action": "attach", "terminal_id": terminal_id},
    )
    attached_data = cast(dict[str, Any], attached["data"])
    assert attached["ok"] is True
    assert attached_data["attached"] is True
    assert attached_data["changed"] is True
    assert attached_data["attachment"] == "current"

    idempotent = await call(
        terminal_manager,
        context,
        {"action": "attach", "terminal_id": terminal_id},
    )
    assert cast(dict[str, Any], idempotent["data"])["changed"] is False

    conflict = await call(
        terminal_manager,
        other_context,
        {"action": "attach", "terminal_id": terminal_id},
    )
    assert cast(dict[str, Any], conflict["error"])["code"] == "terminal_already_attached"

    status = await call(
        terminal_manager,
        context,
        {"action": "status", "terminal_id": terminal_id},
    )
    assert status["ok"] is True

    sent = await call(
        terminal_manager,
        context,
        {"action": "input", "terminal_id": terminal_id, "text": "hello"},
    )
    assert sent["ok"] is True
    assert factory.adapters[0].writes == ["hello"]

    resized = await call(
        terminal_manager,
        context,
        {"action": "resize", "terminal_id": terminal_id, "columns": 100, "rows": 24},
    )
    assert resized["ok"] is True
    assert factory.adapters[0].resizes == [(24, 100)]

    waited = await call(
        terminal_manager,
        context,
        {"action": "wait", "terminal_id": terminal_id, "timeout_ms": 0},
    )
    assert waited["ok"] is True

    wrong_detach = await call(
        terminal_manager,
        other_context,
        {"action": "detach", "terminal_id": terminal_id},
    )
    assert cast(dict[str, Any], wrong_detach["error"])["code"] == "terminal_not_attached"

    detached = await call(
        terminal_manager,
        context,
        {"action": "detach", "terminal_id": terminal_id},
    )
    detached_data = cast(dict[str, Any], detached["data"])
    assert detached_data["attached"] is False
    assert detached_data["process_continues"] is True
    assert factory.adapters[0].alive is True

    denied = await call(
        terminal_manager,
        context,
        {"action": "status", "terminal_id": terminal_id},
    )
    denied_error = cast(dict[str, Any], denied["error"])
    assert denied_error["code"] == "terminal_not_owned"
    assert "attach" in denied_error["message"]

    reattached = await call(
        terminal_manager,
        other_context,
        {"action": "attach", "terminal_id": terminal_id},
    )
    assert reattached["ok"] is True
    killed = await call(
        terminal_manager,
        other_context,
        {"action": "kill", "terminal_id": terminal_id},
    )
    assert killed["ok"] is True
    assert factory.adapters[0].alive is False

    closed = await call(
        terminal_manager,
        context,
        {"action": "attach", "terminal_id": terminal_id},
    )
    assert cast(dict[str, Any], closed["error"])["code"] == "terminal_closed"


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
