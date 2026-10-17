"""Terminal Tool: definition, start, workdirs, and attachment."""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast, override

import pytest
import pytest_asyncio

from core.projects import ProjectStore
from core.providers.tool_schema import render_tool_definitions
from core.sessions import SessionAddress
from core.storage.temp_files import TemporaryFileManager
from core.subagents import SubAgentState, SubAgentStopOutcome
from core.tools import terminal as terminal_module
from core.tools import terminal_manager as manager_module
from core.tools.model_names import BASH_TOOL_NAME, model_tool_name
from core.tools.scratch_areas import ScratchAreas
from core.tools.shell_environment import terminal_environment
from core.tools.terminal import (
    TERMINAL_ACTIONS,
    TERMINAL_TOOL_DESCRIPTION,
    TERMINAL_TOOL_NAME,
    TERMINAL_TOOL_PARAMETERS,
    TERMINAL_UNADVERTISED_PARAMETERS,
    project_terminal_tool_definitions,
    register_terminal_tool,
)
from core.tools.terminal_backend import UnpassableArgumentError
from core.tools.terminal_manager import TerminalManager, TerminalOwner, TerminalRenderHost
from core.tools.tools import JsonObject, ToolContext, ToolRegistry, tool_failure
from core.utils.paths import model_path
from core.utils.tokens import _estimation_encoding, estimate_json_tokens
from tests.core.tools.terminal_helpers import call, details, make_context
from tests.core.tools.terminal_helpers import manager as manager
from tests.core.tools.terminal_manager_helpers import (
    TEST_START_WAIT_SECONDS,
    AdapterFactory,
    FakeClock,
    FakeTerminalAdapter,
    eventually,
)

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
        "text",
        "key",
        "data",
        "columns",
        "rows",
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
        # Advertised until terminals were shown by conversation and called by call_name;
        # still sent by habit.
        "group",
        "name",
    }
    assert properties["action"]["enum"] == list(TERMINAL_ACTIONS)
    assert (properties["lines"]["default"], properties["timeout"]["default"]) == (30, 60)
    # The cap is stated in the description and applied with a note, not refused.
    assert "maximum" not in properties["timeout"]
    assert "600" in properties["timeout"]["description"]
    assert "default" not in properties["command"]
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
    for hidden in ("project:", "timeout_ms", "start_line", "screen_revision"):
        assert hidden not in advertised

    registry = ToolRegistry()
    register_terminal_tool(
        registry, TerminalManager(adapter_factory=AdapterFactory()), ProjectStore(tmp_path)
    )
    tool = registry.get(TERMINAL_TOOL_NAME)
    assert tool.open_input_schema is True
    assert tool.display.summary({"action": "start", "command": "codex"}) == "start · codex"
    assert tool.display.summary({"action": "start"}) == "start · default shell"
    assert tool.display.parts_builder is not None
    resize_parts = tool.display.parts_builder(
        {"action": "resize", "terminal_id": "terminal-a", "columns": 80, "rows": 24}
    )
    assert [part.value for part in resize_parts] == ["resize", "terminal-a", "80×24"]
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
    # This is the warmed tokenizer budget, not the temporary startup fallback.
    _estimation_encoding()
    assert estimate_json_tokens(definitions[0])[0] <= 760
    for profile in ("explicit_non_strict", "omit_strict"):
        rendered = render_tool_definitions(definitions, profile=profile)[0]
        assert rendered["parameters"] == definitions[0]["parameters"]
        assert rendered.get("strict") is (False if profile == "explicit_non_strict" else None)


_DELIVERIES = (
    "While it runs, its screen arrives as a new message each time it goes idle showing something "
    "new, and so does its exit. Continue with your next step instead of polling with status."
)


@pytest.mark.parametrize("shell_offered", [True, False])
def test_definition_fits_the_offered_tools(shell_offered: bool) -> None:
    definitions: list[JsonObject] = [
        {
            "name": TERMINAL_TOOL_NAME,
            "description": TERMINAL_TOOL_DESCRIPTION,
            "parameters": TERMINAL_TOOL_PARAMETERS,
        },
        *([{"name": BASH_TOOL_NAME, "description": "shell"}] if shell_offered else []),
    ]

    projected = project_terminal_tool_definitions(definitions)

    terminal = projected[0]
    # The shell Tool is named by its Model name, and only while it is offered.
    assert (SHELL in terminal["description"]) is shell_offered
    assert terminal["parameters"] == TERMINAL_TOOL_PARAMETERS
    assert projected[1:] == definitions[1:]
    # The registered definition stays as it was.
    assert definitions[0]["description"] == TERMINAL_TOOL_DESCRIPTION
    without_terminal = definitions[1:]
    assert project_terminal_tool_definitions(without_terminal) == without_terminal


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [None, "fake-tui"], ids=["default-shell", "command"])
@pytest.mark.parametrize("scratch_available", [True, False])
async def test_start_returns_the_first_screen_once_startup_output_settles(
    manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str | None,
    scratch_available: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(terminal_module, "default_terminal_argv", lambda env: ["host-shell"])
    terminal_manager, factory = manager
    factory.initial_output = "Welcome\r\nready> "

    def identity(address: SessionAddress) -> str:
        if not scratch_available:
            raise PermissionError("private Scratch lookup details")
        return "generation_" + address.session_id

    scratch_areas = ScratchAreas(
        tmp_path,
        identity=identity,
        ancestors=lambda _address: (),
        busy_sessions=lambda: (),
    )
    ordinary_environment = terminal_environment()
    arguments: JsonObject = {"action": "start"}
    if command is not None:
        arguments["command"] = command

    result = await call(
        terminal_manager, make_context(tmp_path), arguments, scratch_areas=scratch_areas
    )

    data = cast(dict[str, Any], result["data"])
    assert data == {
        "terminal_id": data["terminal_id"],
        "call_name": "Anna",
        "state": "running",
        "screen": data["screen"],
        "cursor": "row 2, column 8",
        "log": data["log"],
        "next": _DELIVERIES,
    }
    assert data["screen"].splitlines() == ["Welcome", "ready>"]
    # start names the log, which keeps what scrolls off the screen.
    assert re.search(r" \S+\.log, ", data["log"]), data["log"]
    assert factory.calls[0][0] == [command or "host-shell"]
    assert factory.calls[0][3:] == (24, 80)
    assert not any(name.startswith("VBOT_TERMINAL_") for name in factory.calls[0][2])
    environment = factory.calls[0][2]
    own = None
    if scratch_available:
        own = Path(environment["VBOT_SCRATCH_DIR"])
        for name in ("TEMP", "TMP", "TMPDIR"):
            assert Path(environment[name]) == own / "tmp"
            assert Path(environment[name]).is_dir()
        (own / "tmp").rmdir()
    else:
        assert "VBOT_SCRATCH_DIR" not in environment
        for name in ("TEMP", "TMP", "TMPDIR"):
            assert environment.get(name) == ordinary_environment.get(name)
    again = await call(
        terminal_manager, make_context(tmp_path), arguments, scratch_areas=scratch_areas
    )
    assert again["ok"] is True
    if own is not None:
        assert (own / "tmp").is_dir()
    await terminal_manager.spawn_for_operator(command=None, arguments=[], cwd=tmp_path)
    operator_environment = factory.calls[-1][2]
    assert "VBOT_SCRATCH_DIR" not in operator_environment
    if own is not None:
        assert all(
            operator_environment.get(name) != str(own / "tmp") for name in ("TEMP", "TMP", "TMPDIR")
        )
    if not scratch_available:
        assert sum(record.levelname == "WARNING" for record in caplog.records) == 1
        assert "private Scratch lookup details" not in caplog.text


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
async def test_start_types_nothing_and_returns_the_first_visible_screen(
    clocked: tuple[TerminalManager, AdapterFactory, FakeClock], tmp_path: Path, program: str
) -> None:
    """start returns once the program's first visible output paused, or after ten
    seconds; text given with it is not typed, and the result says so."""
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
    if program == "silent":
        await clock.advance(9.9)
        assert not starting.done()
        await clock.advance(0.1)
    elif program == "answers":
        await eventually(lambda: terminal_manager.list_terminals()[0].screen_revision > 0)
        await clock.advance(2)
    else:

        def revision() -> int:
            return terminal_manager.list_terminals()[0].screen_revision

        for _ in range(10):
            shown = revision()
            factory.adapters[0].emit("working\r\n")
            await eventually(lambda shown=shown: revision() > shown)
            await clock.advance(1)

    data = cast(dict[str, Any], (await starting)["data"])
    assert data["state"] == "running"
    assert factory.adapters[0].writes == []
    assert data["terminal_id"] in data["note"]
    if program == "answers":
        assert data["screen"] == "Ready>"
        assert data["cursor"] == "row 1, column 8"
    elif program == "silent":
        assert data["screen"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["codex", "claude-code", "opencode"])
async def test_coding_agent_reference_launches_exact_arguments(
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
        assert "note" not in cast(dict[str, Any], result["data"])
        assert factory.adapters[-1].alive


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("launch_error", "code", "message", "failure_phase"),
    [
        (
            FileNotFoundError(2, "fixture executable is missing", "fixture-missing"),
            "terminal_command_not_found",
            "Program fixture-missing was not found, so no terminal was started. Pass the "
            "program's full path as command, or omit command to start the user's default shell "
            "and run it there.",
            "process",
        ),
        (
            RuntimeError("fixture transport could not initialize"),
            "terminal_launch_failed",
            "No terminal was started: the program could not start (fixture transport could not "
            "initialize). Check command, args and workdir, then start again.",
            "process",
        ),
        (
            PermissionError("fixture log cannot be opened"),
            "terminal_launch_failed",
            "No terminal was started: the program could not start (fixture log cannot be "
            "opened). Check command, args and workdir, then start again.",
            "log",
        ),
        # A batch program's argument that cmd.exe cannot pass starts nothing; the
        # error names it by its text, as command can have been split into words.
        (
            UnpassableArgumentError(
                "argument 1 contains a line break", index=1, value="two\nlines\0"
            ),
            "invalid_arguments",
            "No terminal was started: fixture-command is a batch program (.cmd or .bat), which "
            'runs through cmd.exe, and cmd.exe cannot pass the argument "two\\nlines\\u0000" to '
            "it because it contains a line break and a NUL character. Pass that text without "
            "line breaks and NUL characters, or leave it out and send it with the input action "
            "once the program is ready.",
            "process",
        ),
    ],
    ids=["missing-executable", "transport-failure", "log-open-failure", "unpassable-argument"],
)
async def test_launch_failure_is_reported_and_releases_capacity(
    tmp_path: Path,
    launch_error: Exception,
    code: str,
    message: str | None,
    failure_phase: str,
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
            if self.failing and failure_phase == "process":
                raise launch_error
            return super().__call__(argv, cwd, env, rows, columns, command_line=command_line)

    factory = RecoveringFactory()
    temporary_files = TemporaryFileManager(tmp_path)
    logs = temporary_files.root / "terminals"
    open_file = Path.open

    def open_log(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if (
            factory.failing
            and failure_phase == "log"
            and path.parent == logs
            and mode.startswith("a")
        ):
            raise launch_error
        return open_file(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_log)
    terminal_manager = TerminalManager(
        adapter_factory=factory,
        render_host=TerminalRenderHost.in_process(),
        temporary_files=temporary_files,
        sweep_interval_seconds=3600,
        activity_quiet_seconds=0.03,
        start_wait_seconds=TEST_START_WAIT_SECONDS,
    )
    terminal_manager.start()
    try:
        context = make_context(tmp_path)
        arguments: JsonObject = {"action": "start", "command": "fixture-command"}
        result = await call(terminal_manager, context, arguments)
        assert (result["ok"], result["error"]["code"]) == (False, code)
        if message is not None:
            assert result == tool_failure(code, message, retryable=False)
        assert terminal_manager.list_terminals() == []
        assert factory.adapters == []
        for path in logs.iterdir():
            os.utime(path, (0, 0))
        temporary_files.sweep()
        assert list(logs.iterdir()) == []

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
            {"action": "start", "command": "fake-tui"},
            {"action": "start", "command": "python", "args": ["-i"]},
        )
    ]
    first, second = (data["terminal_id"] for data in started)

    refused = await call(terminal_manager, context, {"action": "start", "command": "fake-tui"})

    assert refused == tool_failure(
        "terminal_capacity",
        "This Session already runs 2 terminals, the most it can run at once, so no terminal "
        f"was started. Your running terminals: {first} (Anna, fake-tui); {second} "
        "(Ben, python -i). "
        'Stop one you no longer need with terminal action "kill" and its terminal_id from this '
        "list, then start again.",
        retryable=True,
    )
    assert len(factory.calls) == 2


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


class _SubAgents:
    """The caller's Sub-Agent Sessions at every depth, as the coordinator reports them.

    The Sub-Agents in *answers_user* are ones whose Session the user took over.
    """

    def __init__(
        self, sessions: Mapping[SessionAddress, str], answers_user: Sequence[str] = ()
    ) -> None:
        self.sessions = dict(sessions)
        self.answers_user = set(answers_user)
        self.callers: list[SessionAddress] = []

    async def subagents(self, caller: SessionAddress) -> list[SubAgentState]:
        self.callers.append(caller)
        return [
            SubAgentState(
                id=subagent_id,
                title="Helper",
                agent_id=session.agent_id,
                session=session,
                parent_id=None,
                status="working",
                last_tool=None,
                activity_file=None,
                reply_target="user" if subagent_id in self.answers_user else "parent",
            )
            for session, subagent_id in self.sessions.items()
        ]

    async def running_work(self, subagent: SubAgentState) -> Sequence[str]:
        raise AssertionError("terminal asks only which Sessions are Sub-Agents'")

    async def stop_subagent(
        self, context: ToolContext, subagent: SubAgentState
    ) -> SubAgentStopOutcome:
        raise AssertionError("terminal never stops a Sub-Agent")


@pytest.mark.asyncio
async def test_list_and_status_name_the_sub_agent_whose_session_holds_a_terminal(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A terminal attached to a Sub-Agent's Session (any depth) names that Sub-Agent in
    list and status; one attached to any other Session, or to a Session the user took
    over, stays ``other``."""
    monkeypatch.setattr(manager_module, "default_terminal_argv", lambda env: ["host-shell"])
    terminal_manager, _factory = manager
    grandchild = TerminalOwner("project-a", "helper", "session-grandchild")
    stranger = TerminalOwner("project-a", "agent-a", "session-b")
    taken = TerminalOwner("project-a", "helper", "session-taken")
    terminal_ids: list[str] = []
    for holder in (grandchild, stranger, taken, None):
        started = await terminal_manager.spawn_for_operator(
            command=None, arguments=[], cwd=tmp_path
        )
        if holder is not None:
            terminal_manager.attach(started["terminal_id"], holder, origin_run_id="run-b")
        terminal_ids.append(started["terminal_id"])
    subagents = _SubAgents(
        {
            SessionAddress("project-a", "helper", "session-grandchild"): "sub_grandchild",
            SessionAddress("project-a", "helper", "session-taken"): "sub_taken",
        },
        answers_user=["sub_taken"],
    )
    arguments: JsonObject = {"action": "list"}

    listed = await call(terminal_manager, make_context(tmp_path), arguments, subagents=subagents)

    rows = {row["terminal_id"]: row for row in cast(dict[str, Any], listed["data"])["terminals"]}
    assert [(rows[id_]["attached"], rows[id_].get("subagent")) for id_ in terminal_ids] == [
        ("subagent", "sub_grandchild"),
        ("other", None),
        ("subagent", "sub_taken"),
        ("no", None),
    ]
    assert subagents.callers == [SessionAddress("project-a", "agent-a", "session-a")]
    shown = details(terminal_manager, tmp_path, arguments, listed)[-1]["items"]
    assert shown[0]["meta"].endswith("attached to Sub-Agent sub_grandchild")

    statuses = [
        cast(
            dict[str, Any],
            await call(
                terminal_manager,
                make_context(tmp_path),
                {"action": "status", "terminal_id": terminal_id},
                subagents=subagents,
            ),
        )["data"]
        for terminal_id in terminal_ids
    ]
    # One layout: status shows who holds the terminal as list does.
    assert [(item["attached"], item.get("subagent")) for item in statuses] == [
        ("subagent", "sub_grandchild"),
        ("other", None),
        ("subagent", "sub_taken"),
        ("no", None),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_id", "offered"),
    [("cmd_build", ("background_tasks",)), ("sub_child", ())],
)
async def test_ids_of_background_tasks_are_refused_before_anything_runs(
    manager: tuple[TerminalManager, AdapterFactory],
    tmp_path: Path,
    task_id: str,
    offered: tuple[str, ...],
) -> None:
    """A background command or Sub-Agent is never a terminal: every action refuses its id,
    and names the background_tasks call (stop for kill) only when the Agent can make it."""
    terminal_manager, factory = manager
    context = make_context(tmp_path, offered_tools=offered)
    calls: list[tuple[JsonObject, str]] = [
        (
            {"action": action, "terminal_id": task_id, "text": "y"}
            if action in {"start", "input"}
            else {"action": action, "terminal_id": task_id},
            "stop" if action == "kill" else "status",
        )
        for action in ("start", "status", "wait", "input", "kill", "attach", "detach")
    ]
    # Other harnesses' fields and stop action for a background shell reach the same refusal.
    calls += [
        ({"action": "kill", "process_id": task_id}, "stop"),
        ({"action": "KillShell", "shell_id": task_id}, "stop"),
        ({"action": "status", "bash_id": task_id}, "status"),
    ]

    for arguments, verb in calls:
        result = await call(terminal_manager, context, arguments)
        assert result["ok"] is False
        assert result["error"]["code"] == "not_a_terminal", arguments
        message = str(result["error"]["message"])
        assert task_id in message
        mentioned = [
            json.loads(found) for found in re.findall(r"background_tasks (\{.*?\})", message)
        ]
        assert mentioned == ([{"action": verb, "id": task_id}] if offered else [])
        assert ("background_tasks" in message) is bool(offered)
    # A terminal's own id under such a field names no terminal.
    other = await call(terminal_manager, context, {"action": "kill", "process_id": "term_x"})
    assert other["error"]["code"] == "invalid_arguments"
    assert terminal_manager.list_terminals() == [] and factory.calls == []


@pytest.mark.asyncio
async def test_attach_grants_full_contract_and_detach_only_removes_binding(
    manager: tuple[TerminalManager, AdapterFactory], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(manager_module, "default_terminal_argv", lambda env: ["host-shell"])
    terminal_manager, factory = manager
    manual = await terminal_manager.spawn_for_operator(command=None, arguments=[], cwd=tmp_path)
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
        "call_name": "Anna",
        "program": "host-shell",
        "state": "running",
        "attached": "here",
        "next": _DELIVERIES,
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
        f"into it, resize it or kill it. Nothing was done. To read its screen, call terminal "
        f"{call_text('status')}."
    )

    status = await run({"action": "status"})
    assert "attached" not in status["data"]
    assert (await run({"action": "input", "text": "hello"}))["ok"] is True
    assert factory.adapters[0].writes == ["hello"]
    # The input's result showed the reply but was not kept, so its screen is still unseen:
    # a wait returns it once the program is idle.
    assert (await run({"action": "wait", "timeout": 5}))["data"]["wait_ended"] == "idle"

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
        f"sent. To read its last screen, call terminal {call_text('status')}. To run the "
        'program again, call terminal with action "start".'
    )
