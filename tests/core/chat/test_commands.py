"""Built-in slash commands: recognition, arguments, availability and their replies."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.chat import CommandDispatcher, ReplySurface
from core.chat.commands import (
    AgentArgument,
    HandoffArgument,
    parse_agent_argument,
    parse_handoff_argument,
)
from core.chat.status_report import STATUS_PLACEHOLDER
from core.projects import AgentResolver, ModelConfigurationError, ProjectStore
from core.runs import ChatRunManager, Run, RunCancelledError
from core.sessions import SessionAddress
from tests.core.chat.commands_test_support import (
    _execute,
    _execute_sync,
    _make_agent,
    _prepared,
    _StubProject,
    _StubProjects,
    _StubResolver,
)


class _StubStoredAgent:
    def __init__(self, agent_id: str) -> None:
        self.id = agent_id


class _StubAgentStore:
    """Agent store stub exposing only the directory card's ``list`` seam."""

    def __init__(self, agent_ids: list[str]) -> None:
        self._agents = [_StubStoredAgent(agent_id) for agent_id in agent_ids]

    def list(self) -> list[_StubStoredAgent]:
        return list(self._agents)


class _StubScannedAgent:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id


class _StubScanReport:
    def __init__(self, team: list[_StubScannedAgent]) -> None:
        self.team = team


class _StubTeamResolver:
    """Resolver stub returning a fixed team for the directory card."""

    def __init__(self, team: list[str]) -> None:
        self._team = [_StubScannedAgent(agent_id) for agent_id in team]

    def scan_project_report(self, project: Any) -> _StubScanReport:
        return _StubScanReport(self._team)


def test_built_in_commands_declare_their_argument_and_result_kind() -> None:
    assert {
        name: (spec.argument, spec.catalog_result)
        for name, spec in CommandDispatcher.BUILT_IN_COMMANDS.items()
    } == {
        "agent": ("optional", "state_change"),
        "compact": ("optional", "notice"),
        "handoff": ("optional", "state_change"),
        "help": ("none", "detail"),
        "learn": ("optional", "state_change"),
        "model": ("optional", "state_change"),
        "new": ("none", "state_change"),
        "reflect": ("optional", "state_change"),
        "rename": ("optional", "notice"),
        "status": ("none", "detail"),
        "stop": ("optional", "notice"),
    }


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("/stop", True),
        (" /STOP ", True),
        ("/handoff a b", True),
        ("/rename Release planning", True),
        ("/bogus", False),
        ("/stop all", True),
        ("/status now", False),
        ("hello", False),
    ],
)
def test_prepare_recognizes_the_command_catalog(message: str, expected: bool) -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    assert (dispatcher.prepare(message) is not None) is expected


@pytest.mark.parametrize(
    ("message", "name", "argument"),
    [
        ("/handoff", "handoff", None),
        ("  /handoff MyAgent  ", "handoff", "MyAgent"),
        ("/handoff agent:main do not forget", "handoff", "agent:main do not forget"),
        ("/learn the deploy steps we just did", "learn", "the deploy steps we just did"),
        ("/reflect", "reflect", None),
        ("/agent builder@vbot ship the fix", "agent", "builder@vbot ship the fix"),
        ("/model reset", "model", "reset"),
        ("/compact keep the API design", "compact", "keep the API design"),
        ("/rename", "rename", None),
        ("/new", "new", None),
    ],
)
def test_serialized_commands_carry_their_raw_argument(
    message: str, name: str, argument: str | None
) -> None:
    # The raw remainder travels verbatim (or None when absent); the executing accessor
    # interprets it, so ids keep their case and "reset" or a title are not normalized here.
    prepared = _prepared(CommandDispatcher(ChatRunManager()), message)

    assert (prepared.name, prepared.argument, prepared.execution_mode) == (
        name,
        argument,
        "serialized",
    )


@pytest.mark.asyncio
async def test_stop_cancels_the_active_run_only_when_executed() -> None:
    manager = ChatRunManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        started.set()
        await release.wait()
        run.raise_if_cancelled()
        return "done"

    run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"), execute
    )
    await started.wait()
    dispatcher = CommandDispatcher(manager)

    assert dispatcher.prepare("/stop") is not None
    assert run.cancel_requested is False
    result = await _execute(dispatcher, " /STOP ")

    assert result.feedback is not None and result.feedback.kind == "notice"
    assert (run.cancel_requested, run.cancel_reason) == (True, "user")
    assert run.cancel_initiator == "webui_command"
    release.set()
    with pytest.raises(RunCancelledError):
        await run.wait()


@pytest.mark.asyncio
async def test_stop_all_hands_the_session_to_stop_all_and_other_arguments_stop_nothing() -> None:
    manager = ChatRunManager()
    release = asyncio.Event()

    async def execute(_run: Run) -> str:
        await release.wait()
        return "done"

    run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"), execute
    )
    stopped: list[SessionAddress] = []
    counts = iter([0, 4])

    async def stop_all(address: SessionAddress) -> int:
        stopped.append(address)
        return next(counts)

    dispatcher = CommandDispatcher(manager, stop_all=stop_all)
    nothing = await _execute(dispatcher, "/stop ALL", project_id="vbot")
    some = await _execute(dispatcher, "/stop all", project_id="vbot")
    unknown = await _execute(dispatcher, "/stop now")
    unwired = await _execute(CommandDispatcher(manager), "/stop all")

    tree = SessionAddress(project_id="vbot", agent_id="coder", session_id="session-one")
    assert stopped == [tree, tree]
    replies = [outcome.feedback for outcome in (nothing, some, unknown, unwired)]
    assert [reply.kind if reply else None for reply in replies] == ["notice"] * 4
    texts = [reply.text for reply in replies if reply is not None]
    # Nothing stopped, something stopped and the usage hint are three different answers;
    # without a stop_all owner "/stop all" gets the usage hint too.
    assert len(set(texts[:3])) == 3
    assert texts[3] == texts[2]
    # Only the Session tree owner stops anything: the active Run is untouched here.
    assert run.cancel_requested is False
    release.set()
    assert await run.wait() == "done"


def test_stop_without_an_active_run_still_replies_with_a_notice() -> None:
    result = _execute_sync(CommandDispatcher(ChatRunManager()), "/stop")

    assert result.feedback is not None
    assert (result.feedback.kind, bool(result.feedback.text)) == ("notice", True)


def test_help_lists_the_current_commands_as_detail() -> None:
    result = _execute_sync(CommandDispatcher(ChatRunManager()), "/help")

    assert result.feedback is not None and result.feedback.kind == "detail"
    reply = result.feedback.text
    assert "/compact - Compact the current session's context immediately." in reply
    assert "/continue" not in reply
    assert "/retry" not in reply
    assert "$skill-name" in reply


def test_agent_without_argument_lists_personal_and_team_directory() -> None:
    dispatcher = CommandDispatcher(
        ChatRunManager(),
        agent_resolver=cast(AgentResolver, _StubTeamResolver(["builder", "planner"])),
        projects=cast(ProjectStore, _StubProjects(_StubProject("vbot", "vBot"))),
        agents=cast(Any, _StubAgentStore(["assistant", "coder"])),
    )

    result = _execute_sync(dispatcher, "/agent", agent_id="assistant")

    assert result.feedback is not None
    assert result.feedback.kind == "detail"
    assert not result.facts
    reply = result.feedback.text
    assert "assistant" in reply
    assert "coder" in reply
    # Team agents are shown project-qualified, teaching the address the move expects.
    assert "builder@vbot" in reply
    assert "planner@vbot" in reply


@pytest.mark.parametrize("message", ["/agent", "/agent planner"])
def test_agent_is_unavailable_on_every_channel_form(message: str) -> None:
    dispatcher = CommandDispatcher(ChatRunManager())
    prepared = _prepared(dispatcher, message)

    unavailable = dispatcher.unavailability(
        prepared,
        ReplySurface.channel(
            platform="telegram",
            platform_display_name="Telegram",
            channel_id="tg-assistant",
        ),
    )

    assert unavailable is not None
    assert unavailable.command == "/agent"
    assert dispatcher.unavailability(prepared, ReplySurface.webui()) is None


@pytest.mark.parametrize(
    ("project_id", "model_value", "model_source", "fragments"),
    [
        (None, "openai/gpt-5.2", "agent", ("openai/gpt-5.2", "agent configuration")),
        (None, None, None, ("not configured", STATUS_PLACEHOLDER)),
        ("vbot", "openai/gpt-mini", "override", ("openai/gpt-mini", "override (set via /model)")),
        ("vbot", "openai/gpt-5.2", "agent", ("agent file in repo",)),
        ("vbot", "openai/gpt-5.2", "project_default", ("project default",)),
        ("vbot", "openai/gpt-5.2", "global_default", ("global default",)),
        (None, "openai/gpt-mini", "session", ("openai/gpt-mini", "this session")),
        ("vbot", "openai/gpt-mini", "session", ("openai/gpt-mini", "this session")),
    ],
    ids=[
        "identity-agent",
        "identity-unconfigured",
        "project-override",
        "project-agent-file",
        "project-default",
        "global-default",
        "identity-session",
        "project-session",
    ],
)
def test_model_without_argument_names_the_winning_configuration_tier(
    project_id: str | None,
    model_value: str | None,
    model_source: str | None,
    fragments: tuple[str, ...],
) -> None:
    resolver = _StubResolver(_make_agent(), model_value=model_value, model_source=model_source)
    dispatcher = CommandDispatcher(
        ChatRunManager(),
        agent_resolver=cast(AgentResolver, resolver),
        projects=cast(ProjectStore, _StubProjects(_StubProject("vbot", "vBot")))
        if project_id is not None
        else None,
    )

    result = _execute_sync(dispatcher, "/model", project_id=project_id)

    assert result.feedback is not None
    assert result.feedback.kind == "detail"
    assert not result.facts
    assert all(fragment in result.feedback.text for fragment in fragments)
    # The reply describes the Model this Session runs, including its own override.
    assert resolver.calls == [(project_id, "coder", "session-one")]


def test_model_without_services_degrades_to_placeholder() -> None:
    result = _execute_sync(CommandDispatcher(ChatRunManager()), "/model")

    assert result.feedback is not None
    assert result.feedback.kind == "detail"
    assert STATUS_PLACEHOLDER in result.feedback.text
    assert "not configured" in result.feedback.text


def test_parse_agent_argument_splits_first_token_as_address() -> None:
    assert parse_agent_argument("planner") == AgentArgument(address="planner", task=None)
    assert parse_agent_argument("builder@vbot do X") == AgentArgument(
        address="builder@vbot", task="do X"
    )
    assert parse_agent_argument("  planner   ship it  ") == AgentArgument(
        address="planner", task="ship it"
    )


@pytest.mark.parametrize(
    ("argument", "target", "instruction"),
    [
        (None, None, None),
        ("   ", None, None),
        ("agent:main", "main", None),
        ("don't forget the plates!", None, "don't forget the plates!"),
        ("agent:main don't forget the plates!", "main", "don't forget the plates!"),
        ("Agent:MyReviewer review carefully", "MyReviewer", "review carefully"),
        # "agent:" without an id is no target slot, and other colons are free text.
        ("agent: do the thing", None, "agent: do the thing"),
        ("remember: call bob", None, "remember: call bob"),
    ],
)
def test_parse_handoff_argument_separates_target_and_instruction(
    argument: str | None, target: str | None, instruction: str | None
) -> None:
    assert parse_handoff_argument(argument) == HandoffArgument(
        target_agent_id=target, instruction=instruction
    )


@pytest.mark.asyncio
async def test_execute_compact_exposes_the_compaction_run_as_primary() -> None:
    run = Run(run_id="run-compact", agent_id="coder", session_id="session-one")

    class Trigger:
        async def start_compaction_run(
            self,
            agent_id: str,
            session_id: str,
            instruction: str | None,
            *,
            project_id: str | None,
        ) -> Run:
            assert (agent_id, session_id, instruction, project_id) == (
                "coder",
                "session-one",
                "keep the API design",
                "project-one",
            )
            return run

    result = await _execute(
        CommandDispatcher(ChatRunManager(), trigger_service=Trigger()),
        "/compact keep the API design",
        project_id="project-one",
    )

    assert result.feedback is None
    assert len(result.runs) == 1
    assert result.runs[0].role == "primary"
    assert result.runs[0].run is run


class _RecordingAgents:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def update(self, agent_id: str, **changes: Any) -> Any:
        self.updates.append((agent_id, changes))
        return SimpleNamespace(id=agent_id, **changes)


class _RecordingProjects:
    def __init__(self) -> None:
        self.set_calls: list[tuple[str, str, str, Any]] = []
        self.clear_calls: list[tuple[str, str, str]] = []

    def set_override(self, project_id: str, agent_id: str, field: str, value: Any) -> Any:
        self.set_calls.append((project_id, agent_id, field, value))
        return SimpleNamespace(project_id=project_id)

    def clear_override(self, project_id: str, agent_id: str, field: str) -> Any:
        self.clear_calls.append((project_id, agent_id, field))
        return SimpleNamespace(project_id=project_id)


class _ConfiguredModels:
    """Model-validation stub (only the configured set is usable) with in-memory Session
    Agent overrides."""

    def __init__(self, configured: set[str]) -> None:
        self._configured = configured
        self.session_overrides: dict[SessionAddress, dict[str, Any]] = {}

    def require_model_configured(self, model: str) -> None:
        if model not in self._configured:
            raise ModelConfigurationError(f"model is not configured: {model}")

    async def update_session_overrides_async(
        self, address: SessionAddress, changes: dict[str, Any]
    ) -> None:
        stored = self.session_overrides.setdefault(address, {})
        for name, value in changes.items():
            if value is None:
                stored.pop(name, None)
            else:
                stored[name] = value


_SESSION_OVERRIDES = {"model": "openai/gpt-mini", "thinking_effort": "high"}


def _model_dispatcher(
    agents: _RecordingAgents, projects: _RecordingProjects, resolver: _ConfiguredModels
) -> CommandDispatcher:
    return CommandDispatcher(
        ChatRunManager(),
        agent_resolver=cast(AgentResolver, resolver),
        agents=cast(Any, agents),
        projects=cast(ProjectStore, projects),
        models=cast(Any, SimpleNamespace()),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "project_id", "agent_updates", "override_sets", "override_clears", "facts"),
    [
        # An identity Session writes the Agent's own model.
        pytest.param(
            "/model openai/gpt-5",
            None,
            [("coder", {"model": "openai/gpt-5"})],
            [],
            [],
            {"agent_id": "coder", "model": "openai/gpt-5"},
            id="identity",
        ),
        # Reset writes an empty model (the global default) and skips validation.
        pytest.param(
            "/model reset",
            None,
            [("coder", {"model": ""})],
            [],
            [],
            {"agent_id": "coder", "model": ""},
            id="identity-reset",
        ),
        # A Project Session writes a per-Agent override instead.
        pytest.param(
            "/model openai/gpt-5",
            "vbot",
            [],
            [("vbot", "coder", "model", "openai/gpt-5")],
            [],
            None,
            id="project",
        ),
        # The reset token is case-insensitive.
        pytest.param(
            "/model RESET",
            "vbot",
            [],
            [],
            [("vbot", "coder", "model")],
            None,
            id="project-reset",
        ),
    ],
)
async def test_model_value_writes_the_identity_model_or_project_override(
    message: str,
    project_id: str | None,
    agent_updates: list[tuple[str, dict[str, Any]]],
    override_sets: list[tuple[str, str, str, Any]],
    override_clears: list[tuple[str, str, str]],
    facts: dict[str, Any] | None,
) -> None:
    agents, projects = _RecordingAgents(), _RecordingProjects()
    resolver = _ConfiguredModels({"openai/gpt-5"})
    session = SessionAddress(project_id=project_id, agent_id="coder", session_id="session-one")
    resolver.session_overrides[session] = dict(_SESSION_OVERRIDES)
    dispatcher = _model_dispatcher(agents, projects, resolver)

    result = await _execute(dispatcher, message, project_id=project_id)

    assert agents.updates == agent_updates
    assert projects.set_calls == override_sets
    assert projects.clear_calls == override_clears
    # The Session runs the Agent's Model next; its other overrides stay.
    assert resolver.session_overrides[session] == {"thinking_effort": "high"}
    if facts is not None:
        assert result.facts == facts
    assert result.feedback is not None
    assert result.feedback.kind == "notice"


@pytest.mark.asyncio
async def test_model_value_writes_nothing_when_the_resolver_refuses_the_model() -> None:
    # The resolver's Model checker owns usability, including forbidden pinned
    # Connections (tests/core/projects/test_resolver_connections.py).
    agents, projects = _RecordingAgents(), _RecordingProjects()
    resolver = _ConfiguredModels({"openai/gpt-5"})
    session = SessionAddress(project_id=None, agent_id="coder", session_id="session-one")
    resolver.session_overrides[session] = dict(_SESSION_OVERRIDES)
    dispatcher = _model_dispatcher(agents, projects, resolver)

    with pytest.raises(ModelConfigurationError):
        await _execute(dispatcher, "/model openai/ghost")

    assert (agents.updates, projects.set_calls, projects.clear_calls) == ([], [], [])
    assert resolver.session_overrides[session] == _SESSION_OVERRIDES


class _RecordingTitles:
    def __init__(self) -> None:
        self.renamed: list[tuple[SessionAddress, str]] = []

    def set_title(self, address: SessionAddress, title: str) -> str | None:
        self.renamed.append((address, title))
        return " ".join(title.split()) or None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "project_id", "written", "title"),
    [
        ("/rename Release planning", None, "Release planning", "Release planning"),
        # No argument clears the title.
        ("/rename", None, "", None),
        ("/rename Docs", "vbot", "Docs", "Docs"),
    ],
    ids=["identity", "clear", "project"],
)
async def test_rename_writes_the_session_title(
    message: str, project_id: str | None, written: str, title: str | None
) -> None:
    sessions = _RecordingTitles()
    dispatcher = CommandDispatcher(ChatRunManager(), sessions=cast(Any, sessions))

    result = await _execute(dispatcher, message, project_id=project_id)

    address = SessionAddress(project_id=project_id, agent_id="coder", session_id="session-one")
    assert sessions.renamed == [(address, written)]
    assert result.facts == {"session_id": "session-one", "title": title}
    assert result.feedback is not None
    assert result.feedback.kind == "notice"
