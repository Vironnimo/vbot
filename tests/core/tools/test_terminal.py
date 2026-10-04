"""Terminal Tool: definition, launch, workdirs, and attachment."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast, override

import pytest

from core.projects import ProjectStore
from core.providers.tool_schema import render_tool_definitions
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
from core.tools.terminal_manager import TerminalManager, TerminalOwner, TerminalRenderHost
from core.tools.tools import JsonObject, ToolRegistry, tool_failure
from core.utils.paths import model_path
from core.utils.tokens import estimate_json_tokens
from tests.core.tools.terminal_helpers import call, make_context
from tests.core.tools.terminal_helpers import manager as manager
from tests.core.tools.terminal_manager_helpers import (
    AdapterFactory,
    FakeTerminalAdapter,
    eventually,
)
from tests.core.tools.terminal_manager_helpers import quick_readiness as quick_readiness


def test_definition_follows_flat_action_conventions_within_its_budget(tmp_path: Path) -> None:
    assert TERMINAL_TOOL_PARAMETERS["type"] == "object"
    assert TERMINAL_TOOL_PARAMETERS["required"] == ["action"]
    assert "oneOf" not in TERMINAL_TOOL_PARAMETERS
    assert "additionalProperties" not in TERMINAL_TOOL_PARAMETERS
    properties = cast(dict[str, Any], TERMINAL_TOOL_PARAMETERS["properties"])
    assert properties["action"]["enum"] == list(TERMINAL_ACTIONS)
    assert "default" not in properties["columns"]
    assert "default" not in properties["rows"]
    assert properties["lines"]["default"] == 30
    assert "default" not in properties["timeout_ms"]
    assert "maximum" not in properties["timeout_ms"]
    assert str(TERMINAL_DEFAULT_WAIT_MS) in properties["timeout_ms"]["description"]
    assert "after_revision" not in properties
    assert "default" not in properties["command"]
    assert properties["name"]["maxLength"] == 80
    assert "enter" not in properties
    assert "enum" not in properties["key"]
    assert "enter" in properties["key"]["description"]
    assert all(
        isinstance(property_schema.get("description"), str) and property_schema["description"]
        for property_schema in properties.values()
    )
    assert properties["data"]["maxLength"] == 65_536
    assert properties["text"]["maxLength"] == 65_536
    assert "f1-f12" in properties["key"]["description"]
    assert "ctrl_a-ctrl_z" in properties["key"]["description"]
    assert TERMINAL_TOOL_DESCRIPTION

    registry = ToolRegistry()
    register_terminal_tool(
        registry, TerminalManager(adapter_factory=AdapterFactory()), ProjectStore(tmp_path)
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

    definitions = registry.provider_definitions(allowed_tools=[TERMINAL_TOOL_NAME])
    assert estimate_json_tokens(definitions[0])[0] <= 900
    for profile in ("explicit_non_strict", "omit_strict"):
        rendered = render_tool_definitions(definitions, profile=profile)[0]
        assert rendered["parameters"] == definitions[0]["parameters"]
        assert rendered.get("strict") is (False if profile == "explicit_non_strict" else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [None, "fake-tui"], ids=["default-shell", "command"])
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
    assert (data["columns"], data["rows"]) == (80, 24)
    assert data["delivery"] == "automatic_terminal_activity"
    assert factory.calls[0][0] == [command or "host-shell"]
    assert factory.calls[0][3:] == (24, 80)
    assert not any(name.startswith("VBOT_TERMINAL_") for name in factory.calls[0][2])

    await terminal_manager.resize_for_operator(data["terminal_id"], columns=153, rows=43)
    status = await call(
        terminal_manager, context, {"action": "status", "terminal_id": data["terminal_id"]}
    )
    resized = cast(dict[str, Any], status["data"])
    assert (resized["columns"], resized["rows"]) == (153, 43)
    assert factory.adapters[0].resizes == [(43, 153)]


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["codex", "claude-code", "opencode"])
@pytest.mark.usefixtures("quick_readiness")
async def test_coding_agent_reference_launches_exact_arguments_and_submits_task(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path, reference: str
) -> None:
    package = Path(__file__).resolve().parents[3] / "resources" / "skills" / "coding-agents"
    document = (package / "references" / f"{reference}.md").read_text(encoding="utf-8")
    examples = re.findall(r"```json\s*(.*?)```", document, re.DOTALL)
    assert examples
    terminal_manager, factory = manager
    context = make_context(tmp_path)
    for example in examples:
        arguments = json.loads(example)
        arguments["workdir"] = str(tmp_path)
        result = await call(terminal_manager, context, arguments)
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
        RuntimeError("fixture transport could not initialize"),
    ],
    ids=["missing-executable", "transport-failure"],
)
async def test_launch_failure_is_reported_and_releases_capacity(
    tmp_path: Path, launch_error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_module, "TERMINAL_MAX_LIVE_PER_SESSION", 1)
    monkeypatch.setattr(manager_module, "TERMINAL_MAX_LIVE_GLOBAL", 1)

    class RecoveringFactory(AdapterFactory):
        failing = True

        @override
        def __call__(
            self,
            argv: Sequence[str],
            cwd: Path,
            env: Mapping[str, str],
            rows: int,
            columns: int,
            *,
            command_line: str | None = None,
        ) -> FakeTerminalAdapter:
            if self.failing:
                raise launch_error
            return super().__call__(argv, cwd, env, rows, columns, command_line=command_line)

    factory = RecoveringFactory()
    terminal_manager = TerminalManager(
        adapter_factory=factory,
        render_host=TerminalRenderHost.in_process(),
        sweep_interval_seconds=3600,
    )
    terminal_manager.start()
    try:
        context = make_context(tmp_path)
        arguments: JsonObject = {"action": "start", "command": "fixture-command"}
        result = await call(terminal_manager, context, arguments)
        assert result == tool_failure(
            "terminal_launch_failed",
            f"Terminal process could not be started: {launch_error}",
            retryable=False,
        )
        assert terminal_manager.list_terminals() == []
        assert factory.adapters == []

        factory.failing = False
        recovered = await call(terminal_manager, context, arguments)
        assert recovered["ok"] is True
        assert len(factory.adapters) == 1
        assert factory.adapters[0].alive
    finally:
        await terminal_manager.aclose()


@pytest.mark.asyncio
async def test_start_name_is_trimmed_and_shown_only_with_launch_facts(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, factory = manager
    context = make_context(tmp_path)
    started = await call(
        terminal_manager, context, {"action": "start", "command": "fake-tui", "name": "  joe  "}
    )
    started_data = cast(dict[str, Any], started["data"])
    terminal_id = started_data["terminal_id"]
    assert started_data["name"] == "joe"

    listed = await call(terminal_manager, context, {"action": "list"})
    assert cast(dict[str, Any], listed["data"])["terminals"][0]["name"] == "joe"
    status = await call(terminal_manager, context, {"action": "status", "terminal_id": terminal_id})
    assert cast(dict[str, Any], status["data"])["name"] == "joe"

    for follow_up in (
        {"action": "wait", "timeout_ms": 0},
        {"action": "input", "text": "x"},
        {"action": "resize", "columns": 100, "rows": 30},
        {"action": "kill"},
    ):
        result = await call(terminal_manager, context, {**follow_up, "terminal_id": terminal_id})
        assert result["ok"] is True, follow_up
        assert "name" not in cast(dict[str, Any], result["data"]), follow_up

    for invalid in ("   ", "x" * 81):
        refused = await call(
            terminal_manager, context, {"action": "start", "command": "fake-tui", "name": invalid}
        )
        assert cast(dict[str, Any], refused["error"])["code"] == "invalid_arguments"
    assert len(factory.calls) == 1


@pytest.mark.asyncio
async def test_start_resolves_project_workdirs_by_stable_id_and_relative_workdirs(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, factory = manager
    first_repo = tmp_path / "first-repo"
    second_repo = tmp_path / "second-repo"
    child = tmp_path / "child"
    for directory in (first_repo, second_repo, child):
        directory.mkdir()
    projects = ProjectStore(tmp_path / "data")
    projects.create("vbot", "Renamable Project", first_repo)
    context = make_context(tmp_path)
    in_project: JsonObject = {
        "action": "start",
        "command": "fake-tui",
        "workdir": f"{TERMINAL_PROJECT_WORKDIR_PREFIX}vbot",
    }

    first = await call(terminal_manager, context, in_project, projects)
    first_data = cast(dict[str, Any], first["data"])
    assert first_data["workdir"] == model_path(first_repo.resolve())
    assert factory.calls[0][1] == first_repo.resolve()
    # The terminal still belongs to the calling Project, not the referenced one.
    terminal_manager.terminal(
        first_data["terminal_id"], TerminalOwner("project-a", "agent-a", "session-a")
    )

    projects.update("vbot", display_name="Different Name", cwd=second_repo)
    second = await call(terminal_manager, context, in_project, projects)
    assert second["ok"] is True
    assert factory.calls[1][1] == second_repo.resolve()

    relative = await call(
        terminal_manager, context, {"action": "start", "command": "fake-tui", "workdir": "child"}
    )
    assert relative["ok"] is True
    assert factory.calls[2][1] == child.resolve()


@pytest.mark.asyncio
async def test_start_rejects_unresolvable_workdirs_before_spawn(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path
) -> None:
    terminal_manager, factory = manager
    projects = ProjectStore(tmp_path / "data")
    projects.create("offline", "Offline", tmp_path / "missing-repo")
    context = make_context(tmp_path)
    groups_before = terminal_manager.list_groups_for_operator()

    errors = []
    for workdir in ("project:missing", "project:offline", "project:", "missing-dir"):
        result = await call(
            terminal_manager,
            context,
            {"action": "start", "workdir": workdir, "group": "build"},
            projects,
        )
        errors.append(cast(dict[str, Any], result["error"]))

    assert [error["code"] for error in errors] == [
        "project_not_found",
        "project_unavailable",
        "invalid_arguments",
        "invalid_arguments",
    ]
    assert errors[3]["message"] == (
        f"Terminal workdir is not a directory: {model_path(tmp_path / 'missing-dir')}"
    )
    assert factory.calls == []
    assert terminal_manager.list_groups_for_operator() == groups_before


@pytest.mark.asyncio
async def test_attach_grants_full_contract_and_detach_only_removes_binding(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda env: ["host-shell"])
    terminal_manager, factory = manager
    manual = await terminal_manager.spawn_for_operator(
        command=None, arguments=[], cwd=tmp_path, name="afk codex"
    )
    terminal_id = manual["terminal_id"]
    context = make_context(tmp_path)
    other_context = make_context(tmp_path, session_id="session-b")

    async def run(arguments: JsonObject, caller: Any = context) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await call(terminal_manager, caller, {**arguments, "terminal_id": terminal_id}),
        )

    listed = await call(terminal_manager, context, {"action": "list"})
    listed_item = cast(dict[str, Any], listed["data"])["terminals"][0]
    assert (listed_item["terminal_id"], listed_item["attachment"]) == (terminal_id, "none")

    attached = await run({"action": "attach"})
    assert attached["ok"] is True
    assert (
        attached["data"]["attached"],
        attached["data"]["changed"],
        attached["data"]["attachment"],
    ) == (True, True, "current")
    assert (await run({"action": "attach"}))["data"]["changed"] is False
    conflict = await run({"action": "attach"}, other_context)
    assert conflict["error"]["code"] == "terminal_already_attached"

    assert (await run({"action": "status"}))["ok"] is True
    assert (await run({"action": "input", "text": "hello"}))["ok"] is True
    assert factory.adapters[0].writes == ["hello"]
    resized = await run({"action": "resize", "columns": 100, "rows": 24})
    assert (resized["data"]["columns"], resized["data"]["rows"]) == (100, 24)
    assert factory.adapters[0].resizes == [(24, 100)]
    assert (await run({"action": "wait", "timeout_ms": 0}))["ok"] is True

    wrong_detach = await run({"action": "detach"}, other_context)
    assert wrong_detach["error"]["code"] == "terminal_not_attached"
    detached = await run({"action": "detach"})
    assert detached["data"]["attached"] is False
    assert detached["data"]["process_continues"] is True
    assert factory.adapters[0].alive is True
    denied = await run({"action": "status"})
    assert denied["error"]["code"] == "terminal_not_owned"
    assert "attach" in denied["error"]["message"]

    assert (await run({"action": "attach"}, other_context))["ok"] is True
    assert (await run({"action": "kill"}, other_context))["ok"] is True
    assert factory.adapters[0].alive is False
    closed = await run({"action": "attach"})
    assert closed["error"]["code"] == "terminal_closed"
