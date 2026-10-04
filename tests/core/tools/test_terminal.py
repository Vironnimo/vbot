"""Terminal Tool: definition, start, workdirs, and attachment."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast, override

import pytest
import pytest_asyncio

from core.projects import ProjectStore
from core.providers.tool_schema import render_tool_definitions
from core.tools import terminal as terminal_module
from core.tools import terminal_manager as manager_module
from core.tools.model_names import BASH_TOOL_NAME, model_tool_name
from core.tools.terminal import (
    TERMINAL_ACTIONS,
    TERMINAL_TOOL_DESCRIPTION,
    TERMINAL_TOOL_NAME,
    TERMINAL_TOOL_PARAMETERS,
    TERMINAL_UNADVERTISED_PARAMETERS,
    project_terminal_tool_definitions,
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
    FakeClock,
    FakeTerminalAdapter,
    eventually,
)
from tests.core.tools.terminal_manager_helpers import quick_readiness as quick_readiness

SHELL = model_tool_name(BASH_TOOL_NAME)


def test_definition_follows_flat_action_conventions_within_its_budget(tmp_path: Path) -> None:
    assert TERMINAL_TOOL_PARAMETERS["type"] == "object"
    assert TERMINAL_TOOL_PARAMETERS["required"] == ["action"]
    assert "oneOf" not in TERMINAL_TOOL_PARAMETERS
    assert "additionalProperties" not in TERMINAL_TOOL_PARAMETERS
    properties = cast(dict[str, Any], TERMINAL_TOOL_PARAMETERS["properties"])
    assert list(properties) == [
        "action",
        "terminal_id",
        "command",
        "args",
        "workdir",
        "name",
        "group",
        "text",
        "key",
        "data",
        "lines",
        "pattern",
        "timeout",
    ]
    # Accepted from other harnesses and from results' own paging requests, never shown.
    assert set(TERMINAL_UNADVERTISED_PARAMETERS) == {
        "timeout_ms",
        "start_line",
        "expected_screen_revision",
        "after_revision",
        "columns",
        "rows",
    }
    assert properties["action"]["enum"] == list(TERMINAL_ACTIONS)
    assert "resize" not in TERMINAL_ACTIONS
    assert (properties["lines"]["default"], properties["timeout"]["default"]) == (30, 60)
    # The cap is stated in the description and applied with a note, not refused.
    assert "maximum" not in properties["timeout"]
    assert "600" in properties["timeout"]["description"]
    assert "default" not in properties["command"]
    assert properties["name"]["maxLength"] == 80
    assert "enum" not in properties["key"]
    assert "f1-f12" in properties["key"]["description"]
    assert "ctrl_a-ctrl_z" in properties["key"]["description"]
    assert properties["data"]["maxLength"] == 65_536
    assert properties["text"]["maxLength"] == 65_536
    assert all(
        isinstance(property_schema.get("description"), str) and property_schema["description"]
        for property_schema in properties.values()
    )
    # No hidden form leaks into the advertised text.
    advertised = json.dumps(TERMINAL_TOOL_PARAMETERS) + TERMINAL_TOOL_DESCRIPTION
    for hidden in ("project:", "timeout_ms", "start_line", "screen_revision", "resize"):
        assert hidden not in advertised

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
    assert estimate_json_tokens(definitions[0])[0] <= 760
    for profile in ("explicit_non_strict", "omit_strict"):
        rendered = render_tool_definitions(definitions, profile=profile)[0]
        assert rendered["parameters"] == definitions[0]["parameters"]
        assert rendered.get("strict") is (False if profile == "explicit_non_strict" else None)


_DELIVERY = (
    "For terminals attached to this Session, a program's exit arrives as a new message, and so "
    "does an interactive program's screen when its output settles after activity."
)
_SUBAGENT_DELIVERY = (
    "For terminals attached to this Session, an interactive program's exit arrives as a new "
    "message while you work, and so does its screen when its output settles after activity."
)


@pytest.mark.parametrize(
    ("depth", "shell_offered", "expected"),
    [
        (
            0,
            True,
            "Start and operate programs in a live terminal: interactive programs you drive by "
            "typing, such as REPLs, TUIs and coding-agent CLIs, and commands that {shell} left "
            "running. Run commands that finish on their own with {shell}. A program keeps "
            "running after your turn ends, until it exits or is stopped. "
            f"{_DELIVERY} Screen text is rendered terminal text, not exact file content.",
        ),
        (
            0,
            False,
            "Start and operate programs in a live terminal: interactive programs you drive by "
            "typing, such as REPLs, TUIs and coding-agent CLIs. A program keeps running after "
            f"your turn ends, until it exits or is stopped. {_DELIVERY} Screen text is rendered "
            "terminal text, not exact file content.",
        ),
        (
            1,
            True,
            "Start and operate programs in a live terminal: interactive programs you drive by "
            "typing, such as REPLs, TUIs and coding-agent CLIs, and commands that {shell} left "
            "running. Run commands that finish on their own with {shell}. A program keeps "
            f"running after your turn ends, until it exits or is stopped. {_SUBAGENT_DELIVERY} "
            "A command's result does not arrive on its own. Before your final answer, use wait "
            "for every program whose result you need. Screen text is rendered terminal text, "
            "not exact file content.",
        ),
        (
            1,
            False,
            "Start and operate programs in a live terminal: interactive programs you drive by "
            "typing, such as REPLs, TUIs and coding-agent CLIs. A program keeps running after "
            f"your turn ends, until it exits or is stopped. {_SUBAGENT_DELIVERY} Before your "
            "final answer, use wait for every program whose result you need. Screen text is "
            "rendered terminal text, not exact file content.",
        ),
    ],
)
def test_definition_fits_the_session_depth_and_offered_tools(
    depth: int, shell_offered: bool, expected: str
) -> None:
    definitions: list[JsonObject] = [
        {
            "name": TERMINAL_TOOL_NAME,
            "description": TERMINAL_TOOL_DESCRIPTION,
            "parameters": TERMINAL_TOOL_PARAMETERS,
        },
        *([{"name": BASH_TOOL_NAME, "description": "shell"}] if shell_offered else []),
    ]

    projected = project_terminal_tool_definitions(definitions, nesting_depth=depth)

    terminal = projected[0]
    assert terminal["description"] == expected.format(shell=SHELL)
    # The shell's results hand out terminal ids only where the shell is offered.
    assert terminal["parameters"]["properties"]["terminal_id"]["description"] == (
        f"The terminal's id from start, list, or a {SHELL} result. Required except for start "
        "and list."
        if shell_offered
        else "The terminal's id from start or list. Required except for start and list."
    )
    assert projected[1:] == definitions[1:]
    # The registered definition stays as it was.
    assert TERMINAL_TOOL_PARAMETERS["properties"]["terminal_id"]["description"].endswith(
        f"or a {SHELL} result. Required except for start and list."
    )
    without_terminal = definitions[1:]
    assert project_terminal_tool_definitions(without_terminal, nesting_depth=depth) == (
        without_terminal
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [None, "fake-tui"], ids=["default-shell", "command"])
async def test_start_returns_the_first_screen_once_startup_output_settles(
    manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str | None,
) -> None:
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda env: ["host-shell"])
    terminal_manager, factory = manager
    factory.initial_output = "Welcome\r\nready> "
    arguments: JsonObject = {"action": "start"}
    if command is not None:
        arguments["command"] = command

    result = await call(terminal_manager, make_context(tmp_path), arguments)

    data = cast(dict[str, Any], result["data"])
    assert data == {
        "terminal_id": data["terminal_id"],
        "state": "running",
        "screen": data["screen"],
    }
    assert data["screen"].splitlines() == ["Welcome", "ready>"]
    assert factory.calls[0][0] == [command or "host-shell"]
    assert factory.calls[0][3:] == (24, 80)
    assert not any(name.startswith("VBOT_TERMINAL_") for name in factory.calls[0][2])


@pytest.mark.asyncio
async def test_start_of_a_program_that_exits_at_once_shows_its_end_and_last_screen(
    tmp_path: Path,
) -> None:
    class ExitingFactory(AdapterFactory):
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
            adapter = super().__call__(argv, cwd, env, rows, columns, command_line=command_line)
            adapter.finish(2)
            return adapter

    exiting = TerminalManager(
        adapter_factory=ExitingFactory("usage: fake-tui [options]\r\n"),
        render_host=TerminalRenderHost.in_process(),
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
    )
    exiting.start()
    try:
        result = await call(
            exiting, make_context(tmp_path), {"action": "start", "command": "fake-tui"}
        )
    finally:
        await exiting.aclose()

    data = cast(dict[str, Any], result["data"])
    assert (data["state"], data["exit_code"], data["screen"]) == (
        "exited",
        2,
        "usage: fake-tui [options]",
    )


@pytest_asyncio.fixture
async def clocked() -> AsyncIterator[tuple[TerminalManager, AdapterFactory, FakeClock]]:
    clock = FakeClock()
    factory = AdapterFactory()
    terminal_manager = TerminalManager(
        adapter_factory=factory,
        render_host=TerminalRenderHost.in_process(),
        sweep_interval_seconds=3600,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    terminal_manager.start()
    try:
        yield terminal_manager, factory, clock
    finally:
        await terminal_manager.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("program", ["silent", "answers", "keeps-printing"])
@pytest.mark.usefixtures("quick_readiness")
async def test_start_with_text_returns_once_its_output_settles_or_after_ten_seconds(
    clocked: tuple[TerminalManager, AdapterFactory, FakeClock], tmp_path: Path, program: str
) -> None:
    terminal_manager, factory, clock = clocked
    if program != "silent":
        factory.initial_output = "Ready> "
    starting = asyncio.ensure_future(
        call(
            terminal_manager,
            make_context(tmp_path),
            {"action": "start", "command": "slow-tui", "text": "fix the tests"},
        )
    )
    await eventually(lambda: clock.sleeping)

    async def emit(output: str) -> None:
        shown = terminal_manager.list_terminals()[0].screen_revision
        factory.adapters[0].emit(output)
        await eventually(lambda: terminal_manager.list_terminals()[0].screen_revision > shown)

    if program == "silent":
        # The program shows nothing yet, so its text waits for its first screen.
        await clock.advance(9.9)
        assert not starting.done()
        await clock.advance(0.1)
    else:
        await eventually(lambda: factory.adapters[0].writes == ["fix the tests", "\r"])
        if program == "answers":
            await emit("fix the tests\r\nDone.\r\nReady> ")
            await clock.advance(2)
        else:
            for _ in range(10):
                await emit("working\r\n")
                await clock.advance(1)

    data = cast(dict[str, Any], (await starting)["data"])
    assert data["state"] == "running"
    if program == "answers":
        # The result shows the output the text caused.
        assert data["screen"].splitlines() == ["Ready> fix the tests", "Done.", "Ready>"]
        assert "note" not in data
    elif program == "silent":
        assert data["note"] == (
            "text is not typed yet: it is typed and submitted once the program's screen stops "
            "changing."
        )
        assert factory.adapters[0].writes == []
    else:
        assert data["note"] == (
            "the text was submitted; its output is not on this screen yet. To see it, call "
            "terminal "
            + json.dumps({"action": "wait", "terminal_id": data["terminal_id"]})
            + "; do not send it again."
        )


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
    factory.initial_output = "Ready> "
    context = make_context(tmp_path)
    for example in examples:
        arguments = json.loads(example)
        arguments["workdir"] = str(tmp_path)
        result = await call(terminal_manager, context, arguments)
        assert result["ok"] is True
        assert factory.calls[-1][0] == [arguments["command"], *arguments["args"]]
        assert factory.calls[-1][1] == tmp_path
        # start returns once the task was typed on the program's first screen.
        adapter = factory.adapters[-1]
        assert adapter.writes == [arguments["text"], "\r"]
        assert "note" not in cast(dict[str, Any], result["data"])
        assert adapter.alive


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("launch_error", "code", "message"),
    [
        (
            FileNotFoundError(2, "fixture executable is missing", "fixture-missing"),
            "terminal_command_not_found",
            "Program fixture-missing was not found, so no terminal was started. Pass the "
            "program's full path as command, or omit command to start the user's default shell "
            "and run it there.",
        ),
        (
            RuntimeError("fixture transport could not initialize"),
            "terminal_launch_failed",
            "No terminal was started: the program could not start (fixture transport could not "
            "initialize). Check command, args and workdir, then start again.",
        ),
    ],
    ids=["missing-executable", "transport-failure"],
)
async def test_launch_failure_is_reported_and_releases_capacity(
    tmp_path: Path,
    launch_error: Exception,
    code: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
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
        activity_quiet_seconds=0.03,
    )
    terminal_manager.start()
    try:
        context = make_context(tmp_path)
        arguments: JsonObject = {"action": "start", "command": "fixture-command"}
        result = await call(terminal_manager, context, arguments)
        assert result == tool_failure(code, message, retryable=False)
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
async def test_capacity_refusal_names_the_terminals_to_stop(
    manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(manager_module, "TERMINAL_MAX_LIVE_PER_SESSION", 2)
    terminal_manager, factory = manager
    context = make_context(tmp_path)
    started = [
        cast(dict[str, Any], (await call(terminal_manager, context, arguments))["data"])
        for arguments in (
            {"action": "start", "command": "fake-tui", "name": "build watcher"},
            {"action": "start", "command": "python", "args": ["-i"]},
        )
    ]
    first, second = (data["terminal_id"] for data in started)

    refused = await call(terminal_manager, context, {"action": "start", "command": "fake-tui"})

    assert refused == tool_failure(
        "terminal_capacity",
        "This Session already runs 2 terminals, the most it can run at once, so no terminal "
        f"was started. Your running terminals: {first} (build watcher); {second} (python -i). "
        'Stop one you no longer need with terminal action "kill" and its terminal_id from this '
        "list, then start again.",
        retryable=True,
    )
    assert len(factory.calls) == 2


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
    assert "name" not in started_data

    listed = await call(terminal_manager, context, {"action": "list"})
    assert cast(dict[str, Any], listed["data"])["terminals"][0]["name"] == "joe"
    status = await call(terminal_manager, context, {"action": "status", "terminal_id": terminal_id})
    assert cast(dict[str, Any], status["data"])["name"] == "joe"

    for follow_up in (
        {"action": "wait", "timeout": 0.01},
        {"action": "input", "text": "x"},
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
    in_project: JsonObject = {"action": "start", "command": "fake-tui", "workdir": "project:vbot"}

    first = await call(terminal_manager, context, in_project, projects)
    terminal_id = cast(dict[str, Any], first["data"])["terminal_id"]
    assert factory.calls[0][1] == first_repo.resolve()
    status = await call(terminal_manager, context, {"action": "status", "terminal_id": terminal_id})
    assert cast(dict[str, Any], status["data"])["workdir"] == model_path(first_repo.resolve())
    # The terminal still belongs to the calling Project, not the referenced one.
    terminal_manager.terminal(terminal_id, TerminalOwner("project-a", "agent-a", "session-a"))

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
    assert all(error["message"].startswith("No terminal was started: ") for error in errors)
    assert errors[3]["message"] == (
        f"No terminal was started: workdir {model_path(tmp_path / 'missing-dir')} is not a "
        "directory. Pass an existing directory, or omit workdir to use the working directory."
    )
    assert factory.calls == []
    assert terminal_manager.list_groups_for_operator() == groups_before


@pytest.mark.asyncio
async def test_attach_grants_full_contract_and_detach_only_removes_binding(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_module, "default_terminal_argv", lambda env: ["host-shell"])
    terminal_manager, factory = manager
    manual = await terminal_manager.spawn_for_operator(
        command=None, arguments=[], cwd=tmp_path, name="afk codex"
    )
    terminal_id = manual["terminal_id"]
    persisted: list[Callable[[], None]] = []
    context = make_context(tmp_path, result_persisted_hook=persisted.append)
    other_context = make_context(tmp_path, session_id="session-b")

    def call_text(action: str) -> str:
        return json.dumps({"action": action, "terminal_id": terminal_id})

    async def run(arguments: JsonObject, caller: Any = context) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await call(terminal_manager, caller, {**arguments, "terminal_id": terminal_id}),
        )

    listed = await call(terminal_manager, context, {"action": "list"})
    listed_item = cast(dict[str, Any], listed["data"])["terminals"][0]
    assert (listed_item["terminal_id"], listed_item["attached"]) == (terminal_id, "no")

    # Reading an unattached terminal attaches nothing and acknowledges nothing.
    read = await run({"action": "status"})
    assert (read["data"]["attached"], read["data"]["next"]) == (
        "no",
        f"To type into it or wait for it, attach it first with terminal {call_text('attach')}.",
    )
    assert persisted == []
    refused = await run({"action": "input", "text": "hello"})
    assert refused["error"] == {
        "code": "terminal_not_owned",
        "retryable": False,
        "message": f"{terminal_id} is not attached to this Session, so input was not done. "
        f"Attach it with terminal {call_text('attach')}, then repeat the call.",
    }

    attached = await run({"action": "attach"})
    assert attached["data"] == {
        "terminal_id": terminal_id,
        "program": "host-shell",
        "name": "afk codex",
        "state": "running",
        "attached": "here",
    }
    again = await run({"action": "attach"})
    assert again["data"]["note"] == (
        "The terminal was already attached to this Session; nothing changed."
    )
    conflict = await run({"action": "attach"}, other_context)
    assert conflict["error"] == {
        "code": "terminal_already_attached",
        "retryable": False,
        "message": f"{terminal_id} is attached to another Session, and a terminal is attached to "
        "one Session at a time. Nothing was changed. To read its screen without attaching it, "
        f"call terminal {call_text('status')}.",
    }
    seen_by_other = await run({"action": "status"}, other_context)
    assert (seen_by_other["data"]["attached"], seen_by_other["data"]["next"]) == (
        "other",
        "It is attached to another Session; only that Session can type into it or wait for it.",
    )
    taken = await run({"action": "wait", "timeout": 0}, other_context)
    assert taken["error"]["message"] == (
        f"{terminal_id} is attached to another Session; only that Session can wait for it, type "
        f"into it or kill it. Nothing was done. To read its screen, call terminal "
        f"{call_text('status')}."
    )

    status = await run({"action": "status"})
    assert "attached" not in status["data"]
    assert (await run({"action": "input", "text": "hello"}))["ok"] is True
    assert factory.adapters[0].writes == ["hello"]
    # The input's result showed the settled reply but was not kept, so a wait returns it.
    assert (await run({"action": "wait", "timeout": 0.01}))["data"]["wait_ended"] == "quiet"

    wrong_detach = await run({"action": "detach"}, other_context)
    assert wrong_detach["error"] == {
        "code": "terminal_not_attached",
        "retryable": False,
        "message": f"{terminal_id} is not attached to this Session, so there is nothing to "
        "detach. Nothing was changed.",
    }
    detached = await run({"action": "detach"})
    assert detached["data"]["attached"] == "no"
    assert factory.adapters[0].alive is True

    assert (await run({"action": "attach"}, other_context))["ok"] is True
    killed = await run({"action": "kill"}, other_context)
    assert (killed["data"]["state"], factory.adapters[0].alive) == ("stopped", False)
    closed = await run({"action": "input", "text": "more"}, other_context)
    assert closed["error"]["code"] == "terminal_closed"
    assert closed["error"]["message"] == (
        f"The program in {terminal_id} has ended (stopped, exit code -1), so the input was not "
        f"sent. To read its last screen or output, call terminal {call_text('status')}. To run "
        'the program again, call terminal with action "start".'
    )
