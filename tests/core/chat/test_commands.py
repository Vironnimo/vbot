"""Tests for slash command dispatch."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from core.agents.agents import Agent
from core.chat import (
    ChatMessage,
    CommandAction,
    CommandDispatcher,
    CommandHandled,
    NotACommand,
)
from core.chat.commands import (
    STATUS_PLACEHOLDER,
    HandoffArgument,
    build_status_text,
    parse_handoff_argument,
    resolve_actual_thinking_effort,
    resolve_status_model_details,
    resolve_status_project_label,
)
from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.projects import AgentResolver, ProjectStore
from core.providers.providers import ProviderConfig
from core.runs import ChatRunManager, Run, RunCancelledError
from core.sessions import ChatSessionManager


def _make_agent(
    *,
    model: str = "openai/gpt-5.2",
    fallback_model: str = "openai/gpt-5.1",
    temperature: float | None = 0.3,
    thinking_effort: str | None = "none",
) -> Agent:
    return Agent(
        id="coder",
        name="Coder",
        model=model,
        fallback_model=fallback_model,
        workspace="workspace",
        temperature=temperature,
        thinking_effort=thinking_effort,
        allowed_tools=["*"],
        allowed_skills=["*"],
        created_at="2026-05-18T10:00:00+00:00",
        updated_at="2026-05-18T10:00:00+00:00",
    )


def _make_model(*, model_id: str = "gpt-5.2", name: str = "GPT-5.2") -> Model:
    return Model(
        model_id=model_id,
        name=name,
        capabilities=Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=True),
        ),
        context_window=200_000,
        max_output_tokens=8_192,
    )


class _StubResolver:
    """Resolver stub returning a fixed agent, recording the resolve target.

    Mirrors the run-path seam ``/status`` now uses, so a test can assert the
    dispatcher threads the session's ``project_id`` through to the resolver.
    """

    def __init__(self, agent: Agent, *, resolve_error: Exception | None = None) -> None:
        self._agent = agent
        self._resolve_error = resolve_error
        self.calls: list[tuple[str | None, str]] = []

    def resolve_agent(self, project_id: str | None, agent_id: str) -> Agent:
        self.calls.append((project_id, agent_id))
        if self._resolve_error is not None:
            raise self._resolve_error
        return self._agent


class _StubProject:
    def __init__(self, project_id: str, display_name: str) -> None:
        self.project_id = project_id
        self.display_name = display_name


class _StubProjects:
    """Project store stub resolving a single project by id."""

    def __init__(self, project: _StubProject) -> None:
        self._project = project

    def get(self, project_id: str) -> _StubProject:
        if project_id != self._project.project_id:
            raise KeyError(project_id)
        return self._project


class _StubSession:
    def __init__(self, messages: list[ChatMessage]) -> None:
        self._messages = messages

    def load(self) -> list[ChatMessage]:
        return list(self._messages)


class _StubCreatedSession:
    def __init__(self, session_id: str) -> None:
        self.id = session_id


class _StubSessions:
    def __init__(
        self,
        messages: list[ChatMessage] | None = None,
        created_session_id: str = "session-new",
    ) -> None:
        self._session = _StubSession(messages or [])
        self._created_session_id = created_session_id
        self.create_calls: list[str] = []

    def get(
        self, _agent_id: str, _session_id: str, _project_id: str | None = None
    ) -> _StubSession:
        return self._session

    def create(self, agent_id: str) -> _StubCreatedSession:
        self.create_calls.append(agent_id)
        return _StubCreatedSession(self._created_session_id)


class _StubModels:
    def __init__(self, model: Model) -> None:
        self._model = model

    def get(self, _provider_id: str, _model_id: str) -> Model:
        return self._model


class _RecordingModels:
    def __init__(self, model: Model) -> None:
        self._model = model
        self.calls: list[tuple[str, str]] = []

    def get(self, provider_id: str, model_id: str) -> Model:
        self.calls.append((provider_id, model_id))
        if provider_id != "openai" or model_id != "gpt-5.2":
            raise KeyError(model_id)
        return self._model


@pytest.mark.asyncio
async def test_dispatch_stop_with_active_run_returns_cancelled_reply() -> None:
    manager = ChatRunManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        started.set()
        await release.wait()
        run.raise_if_cancelled()
        return "done"

    run = await manager.start(agent_id="coder", session_id="session-one", executor=execute)
    await started.wait()

    dispatcher = CommandDispatcher(manager)
    result = dispatcher.dispatch("coder", "session-one", " /STOP ")

    assert isinstance(result, CommandHandled)
    assert result.reply == "Run cancelled."
    assert run.cancel_requested is True

    release.set()
    with pytest.raises(RunCancelledError):
        await run.wait()


def test_dispatch_stop_with_no_active_run_returns_not_found_reply() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "/stop")

    assert isinstance(result, CommandHandled)
    assert result.reply == "No active run to cancel."


def test_dispatch_unknown_command_returns_not_a_command() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "/bogus")

    assert isinstance(result, NotACommand)


def test_dispatch_non_command_message_returns_not_a_command() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "hello")

    assert isinstance(result, NotACommand)


def test_built_in_commands_include_current_catalog() -> None:
    assert set(CommandDispatcher.BUILT_IN_COMMANDS) == {
        "compact",
        "handoff",
        "help",
        "new",
        "retry",
        "status",
        "stop",
    }


def test_built_in_commands_declare_argument_and_output_metadata() -> None:
    specs = CommandDispatcher.BUILT_IN_COMMANDS
    argument_modes = {name: spec.argument for name, spec in specs.items()}
    output_channels = {name: spec.output for name, spec in specs.items()}

    assert argument_modes == {
        "compact": "optional",
        "handoff": "optional",
        "help": "none",
        "new": "none",
        "retry": "none",
        "status": "none",
        "stop": "none",
    }
    assert output_channels == {
        "compact": "toast",
        "handoff": "action",
        "help": "transient",
        "new": "action",
        "retry": "action",
        "status": "transient",
        "stop": "toast",
    }


def test_dispatch_status_marks_transient_output() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "/status")

    assert isinstance(result, CommandHandled)
    assert result.output == "transient"


def test_dispatch_help_marks_transient_output() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "/help")

    assert isinstance(result, CommandHandled)
    assert result.output == "transient"


def test_dispatch_stop_marks_toast_output() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "/stop")

    assert isinstance(result, CommandHandled)
    assert result.output == "toast"


def test_dispatch_handoff_without_argument_returns_action() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "/handoff")

    assert result == CommandAction(name="handoff", argument=None)


def test_dispatch_handoff_with_agent_id_returns_action() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "/handoff coder")

    assert result == CommandAction(name="handoff", argument="coder")


def test_dispatch_handoff_preserves_agent_id_case() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "/handoff MyAgent")

    assert isinstance(result, CommandAction)
    assert result.name == "handoff"
    assert result.argument == "MyAgent"


def test_dispatch_handoff_tolerates_surrounding_whitespace() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "  /handoff coder  ")

    assert result == CommandAction(name="handoff", argument="coder")


def test_dispatch_handoff_takes_full_remainder_as_argument() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "/handoff agent:main do not forget")

    assert result == CommandAction(name="handoff", argument="agent:main do not forget")


def test_parse_handoff_argument_empty_is_neither_target_nor_instruction() -> None:
    assert parse_handoff_argument(None) == HandoffArgument(target_agent_id=None, instruction=None)
    assert parse_handoff_argument("   ") == HandoffArgument(target_agent_id=None, instruction=None)


def test_parse_handoff_argument_agent_prefix_only_selects_target() -> None:
    assert parse_handoff_argument("agent:main") == HandoffArgument(
        target_agent_id="main", instruction=None
    )


def test_parse_handoff_argument_instruction_only_keeps_current_agent() -> None:
    assert parse_handoff_argument("don't forget the plates!") == HandoffArgument(
        target_agent_id=None, instruction="don't forget the plates!"
    )


def test_parse_handoff_argument_agent_prefix_with_instruction() -> None:
    assert parse_handoff_argument("agent:main don't forget the plates!") == HandoffArgument(
        target_agent_id="main", instruction="don't forget the plates!"
    )


def test_parse_handoff_argument_keyword_is_case_insensitive_id_keeps_case() -> None:
    assert parse_handoff_argument("Agent:MyReviewer review carefully") == HandoffArgument(
        target_agent_id="MyReviewer", instruction="review carefully"
    )


def test_parse_handoff_argument_bare_agent_prefix_falls_through_to_instruction() -> None:
    # ``agent:`` with no id is not a valid target slot.
    assert parse_handoff_argument("agent: do the thing") == HandoffArgument(
        target_agent_id=None, instruction="agent: do the thing"
    )


def test_parse_handoff_argument_colon_in_free_text_does_not_capture_target() -> None:
    assert parse_handoff_argument("remember: call bob") == HandoffArgument(
        target_agent_id=None, instruction="remember: call bob"
    )


def test_dispatch_compact_with_instruction_returns_action_with_argument() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "/compact keep the API design")

    assert result == CommandAction(name="compact", argument="keep the API design")


def test_dispatch_compact_without_instruction_returns_action_without_argument() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "/compact")

    assert result == CommandAction(name="compact", argument=None)


def test_dispatch_no_argument_command_with_trailing_text_is_not_a_command() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "/status now")

    assert isinstance(result, NotACommand)


@pytest.mark.parametrize(
    ("message", "action_name"),
    [
        ("/compact", "compact"),
        ("/new", "new_session"),
        ("/retry", "retry_last_turn"),
    ],
)
def test_dispatch_accessor_commands_return_actions(message: str, action_name: str) -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", message)

    assert isinstance(result, CommandAction)
    assert result.name == action_name


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("/stop", True),
        (" /STOP ", True),
        ("/handoff", True),
        ("/handoff coder", True),
        ("/handoff a b", True),
        ("/compact keep the design", True),
        ("/bogus", False),
        ("/stop now", False),
        ("hello", False),
    ],
)
def test_recognizes_matches_dispatch_recognition(message: str, expected: bool) -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    assert dispatcher.recognizes(message) is expected


@pytest.mark.asyncio
async def test_recognizes_does_not_execute_command_side_effects() -> None:
    manager = ChatRunManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(run: Run) -> str:
        started.set()
        await release.wait()
        run.raise_if_cancelled()
        return "done"

    run = await manager.start(agent_id="coder", session_id="session-one", executor=execute)
    await started.wait()

    dispatcher = CommandDispatcher(manager)
    recognized = dispatcher.recognizes("/stop")

    assert recognized is True
    assert run.cancel_requested is False

    release.set()
    assert await run.wait() == "done"


def test_dispatch_help_returns_current_command_list() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "/help")

    assert isinstance(result, CommandHandled)
    assert result.reply is not None
    assert "/compact - Compact the current session's context immediately." in result.reply
    assert "/retry - Retry the last user turn in this session." in result.reply
    assert "$skill-name" in result.reply


def test_dispatch_status_with_no_deps_returns_degraded_reply() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = dispatcher.dispatch("coder", "session-one", "/status")

    assert isinstance(result, CommandHandled)
    assert result.reply is not None
    assert result.reply != ""
    assert f"Agent: {STATUS_PLACEHOLDER}" in result.reply
    assert "Activity: idle" in result.reply
    assert f"Run created at: {STATUS_PLACEHOLDER}" in result.reply
    assert f"Run updated at: {STATUS_PLACEHOLDER}" in result.reply
    assert "Current time:" in result.reply


def test_dispatch_status_with_full_deps_returns_reply_with_expected_fields() -> None:
    session_started = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
    messages = [
        ChatMessage.user("Status check", timestamp=session_started),
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="All systems go.",
            usage={"input_tokens": 1234, "output_tokens": 42},
            timestamp=session_started,
        ),
    ]
    dispatcher = CommandDispatcher(
        ChatRunManager(),
        agent_resolver=cast(AgentResolver, _StubResolver(_make_agent())),
        sessions=cast(ChatSessionManager, _StubSessions(messages)),
        models=cast(ModelRegistry, _StubModels(_make_model())),
        started_at=datetime(2026, 5, 18, 9, 0, tzinfo=UTC),
    )

    result = dispatcher.dispatch("coder", "session-one", "/status")

    assert isinstance(result, CommandHandled)
    assert result.reply is not None
    assert "Agent: Coder (openai/gpt-5.2)" in result.reply
    assert "Model display name: GPT-5.2" in result.reply
    assert "Activity: idle" in result.reply
    assert f"Run created at: {STATUS_PLACEHOLDER}" in result.reply
    assert f"Run updated at: {STATUS_PLACEHOLDER}" in result.reply
    assert "Context usage: 1234 / 200000" in result.reply
    assert "Current time:" in result.reply


@pytest.mark.asyncio
async def test_dispatch_status_reports_active_run_timestamps() -> None:
    manager = ChatRunManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(_run: Run) -> str:
        started.set()
        await release.wait()
        return "done"

    run = await manager.start(agent_id="coder", session_id="session-one", executor=execute)
    await started.wait()
    dispatcher = CommandDispatcher(
        manager,
        agent_resolver=cast(AgentResolver, _StubResolver(_make_agent())),
        sessions=cast(ChatSessionManager, _StubSessions([])),
        models=cast(ModelRegistry, _StubModels(_make_model())),
    )

    result = dispatcher.dispatch("coder", "session-one", "/status")
    expected_updated_at = run.updated_at
    release.set()
    await run.wait()

    assert isinstance(result, CommandHandled)
    assert result.reply is not None
    assert "Activity: running" in result.reply
    assert f"Run created at: {run.created_at}" in result.reply
    assert f"Run updated at: {expected_updated_at}" in result.reply


def test_dispatch_status_strips_pinned_suffix_before_registry_lookup() -> None:
    session_started = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
    messages = [
        ChatMessage.user("Status check", timestamp=session_started),
    ]
    models = _RecordingModels(_make_model(name="GPT-5.2 Registry"))
    dispatcher = CommandDispatcher(
        ChatRunManager(),
        agent_resolver=cast(
            AgentResolver, _StubResolver(_make_agent(model="openai/gpt-5.2::primary"))
        ),
        sessions=cast(ChatSessionManager, _StubSessions(messages)),
        models=cast(ModelRegistry, models),
        started_at=datetime(2026, 5, 18, 9, 0, tzinfo=UTC),
    )

    result = dispatcher.dispatch("coder", "session-one", "/status")

    assert isinstance(result, CommandHandled)
    assert result.reply is not None
    assert "Model display name: GPT-5.2 Registry" in result.reply
    assert models.calls == [("openai", "gpt-5.2")]


def test_dispatch_status_in_project_session_resolves_config_agent() -> None:
    session_started = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
    messages = [ChatMessage.user("Status check", timestamp=session_started)]
    resolver = _StubResolver(_make_agent(model="openai/gpt-5.2"))
    dispatcher = CommandDispatcher(
        ChatRunManager(),
        agent_resolver=cast(AgentResolver, resolver),
        sessions=cast(ChatSessionManager, _StubSessions(messages)),
        models=cast(ModelRegistry, _StubModels(_make_model())),
        projects=cast(ProjectStore, _StubProjects(_StubProject("vbot", "vBot"))),
        started_at=datetime(2026, 5, 18, 9, 0, tzinfo=UTC),
    )

    result = dispatcher.dispatch("builder", "session-one", "/status", "vbot")

    assert isinstance(result, CommandHandled)
    assert result.reply is not None
    # The project session resolves through the run-path seam instead of degrading
    # to an empty reply, and the resolver sees the session's project id.
    assert resolver.calls == [("vbot", "builder")]
    assert "Agent: Coder (openai/gpt-5.2)" in result.reply
    assert "Project: vBot (vbot)" in result.reply


def test_dispatch_status_identity_session_shows_project_placeholder() -> None:
    resolver = _StubResolver(_make_agent())
    dispatcher = CommandDispatcher(
        ChatRunManager(),
        agent_resolver=cast(AgentResolver, resolver),
        sessions=cast(ChatSessionManager, _StubSessions([])),
        models=cast(ModelRegistry, _StubModels(_make_model())),
        projects=cast(ProjectStore, _StubProjects(_StubProject("vbot", "vBot"))),
    )

    result = dispatcher.dispatch("coder", "session-one", "/status")

    assert isinstance(result, CommandHandled)
    assert result.reply is not None
    assert resolver.calls == [(None, "coder")]
    assert f"Project: {STATUS_PLACEHOLDER}" in result.reply


def test_resolve_status_project_label_renders_name_and_id() -> None:
    projects = cast(ProjectStore, _StubProjects(_StubProject("vbot", "vBot")))

    assert resolve_status_project_label(projects, "vbot") == "vBot (vbot)"


def test_resolve_status_project_label_identity_session_is_none() -> None:
    projects = cast(ProjectStore, _StubProjects(_StubProject("vbot", "vBot")))

    assert resolve_status_project_label(projects, None) is None


def test_resolve_status_project_label_degrades_to_id_when_unresolvable() -> None:
    # Missing store, or a project that can't be loaded, still names the stable id.
    assert resolve_status_project_label(None, "vbot") == "vbot"
    projects = cast(ProjectStore, _StubProjects(_StubProject("other", "Other")))
    assert resolve_status_project_label(projects, "vbot") == "vbot"


def test_build_status_text_degraded_with_no_data() -> None:
    text = build_status_text(None, [], None, None)

    assert f"Agent: {STATUS_PLACEHOLDER}" in text
    assert f"Project: {STATUS_PLACEHOLDER}" in text
    assert f"Model display name: {STATUS_PLACEHOLDER}" in text
    assert f"Fallback model: {STATUS_PLACEHOLDER}" in text
    assert f"Selected thinking effort: {STATUS_PLACEHOLDER}" in text
    assert f"Actual model thinking effort: {STATUS_PLACEHOLDER}" in text
    assert f"Temperature: {STATUS_PLACEHOLDER}" in text
    assert f"Context usage: {STATUS_PLACEHOLDER}" in text
    assert f"Activity: {STATUS_PLACEHOLDER}" in text
    assert f"Run created at: {STATUS_PLACEHOLDER}" in text
    assert f"Run updated at: {STATUS_PLACEHOLDER}" in text
    assert f"Session started: {STATUS_PLACEHOLDER}" in text
    assert f"Turn count: {STATUS_PLACEHOLDER}" in text
    assert f"App uptime: {STATUS_PLACEHOLDER}" in text
    assert "Current time:" in text


def test_build_status_text_with_full_data() -> None:
    session_started = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
    messages = [
        ChatMessage.user("Status check", timestamp=session_started),
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="All systems go.",
            usage={"input_tokens": 987, "output_tokens": 12, "estimated": True},
            timestamp=session_started,
        ),
    ]

    text = build_status_text(
        _make_agent(),
        messages,
        context_window=200_000,
        started_at=datetime(2026, 5, 18, 9, 0, tzinfo=UTC),
    )

    assert "Agent: Coder (openai/gpt-5.2)" in text
    assert "Model display name: gpt-5.2" in text
    assert "Fallback model: openai/gpt-5.1" in text
    assert "Selected thinking effort: none" in text
    assert f"Actual model thinking effort: {STATUS_PLACEHOLDER}" in text
    assert "Temperature: 0.3" in text
    assert f"Activity: {STATUS_PLACEHOLDER}" in text
    assert f"Run created at: {STATUS_PLACEHOLDER}" in text
    assert f"Run updated at: {STATUS_PLACEHOLDER}" in text
    assert "Context usage: ~987 / 200000" in text
    assert "Session started:" in text
    assert "Turn count: 1" in text
    assert "App uptime:" in text
    assert "Current time:" in text


def test_build_status_text_handles_unresolved_nullable_defaults() -> None:
    text = build_status_text(
        _make_agent(temperature=None, thinking_effort=None),
        messages=[],
        context_window=None,
        started_at=None,
    )

    assert "Selected thinking effort: default" in text
    assert "Temperature: default" in text


def test_resolve_actual_thinking_effort_snaps_to_ladder() -> None:
    """The actual effort is the selection snapped against the model's ladder."""
    assert resolve_actual_thinking_effort("max", ("low", "medium", "high")) == "high"
    assert resolve_actual_thinking_effort("medium", ("low", "high")) == "low"


def test_resolve_actual_thinking_effort_none_without_ladder_or_selection() -> None:
    """No ladder or no selection means the wire effort is not resolvable here."""
    assert resolve_actual_thinking_effort("high", ()) is None
    assert resolve_actual_thinking_effort("", ("low", "high")) is None
    assert resolve_actual_thinking_effort(None, ("low", "high")) is None


def test_resolve_actual_thinking_effort_on_off_reports_state() -> None:
    """A toggle model has no effort ladder, so report on/off instead of '—'.

    This is the minimax-m3 (opencode-go, on_off control) case the user hit: any
    non-``none`` selection means reasoning is on; ``none`` means off; no selection
    stays unresolved (provider default)."""
    assert resolve_actual_thinking_effort("high", (), "on_off") == "on"
    assert resolve_actual_thinking_effort("minimal", (), "on_off") == "on"
    assert resolve_actual_thinking_effort("none", (), "on_off") == "off"
    assert resolve_actual_thinking_effort("", (), "on_off") is None


def test_resolve_actual_thinking_effort_budget_reports_rendered_budget() -> None:
    """A budget model reports the rendered token budget, not a bare 'on'."""
    # No budget_max → absolute fallback ladder (medium → 8192).
    assert resolve_actual_thinking_effort("medium", (), "budget") == "on (8,192 tokens)"
    # A seeded budget_max scales the budget proportionally (high → 0.75 * 32000).
    assert resolve_actual_thinking_effort("high", (), "budget", 32000) == "on (24,000 tokens)"
    # ``none`` still reports off.
    assert resolve_actual_thinking_effort("none", (), "budget") == "off"


def test_build_status_text_reports_selected_and_actual_effort_split() -> None:
    """When the model ladder snaps the selection, both lines show distinct values."""
    text = build_status_text(
        _make_agent(thinking_effort="max"),
        messages=[],
        context_window=200_000,
        started_at=None,
        actual_thinking_effort=resolve_actual_thinking_effort("max", ("low", "medium", "high")),
    )

    assert "Selected thinking effort: max" in text
    assert "Actual model thinking effort: high" in text


def test_resolve_status_model_details_returns_reasoning_ladder() -> None:
    """The model resolver surfaces the effective ladder for the actual-effort split."""
    model = Model(
        model_id="gpt-5.2",
        name="GPT-5.2",
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(
                supported=True,
                control="levels",
                levels=("low", "medium", "high"),
            ),
        ),
        context_window=200_000,
        max_output_tokens=8_192,
    )

    class _Models:
        def get(self, _provider_id: str, _model_id: str) -> Model:
            return model

    details = resolve_status_model_details(
        _make_agent(model="openai/gpt-5.2"),
        cast(ModelRegistry, _Models()),
    )

    assert details.context_window == 200_000
    assert details.display_name == "GPT-5.2"
    assert details.reasoning_levels == ("low", "medium", "high")
    assert details.reasoning_control == "levels"


def test_resolve_status_model_details_resolves_window_through_default_chain() -> None:
    """A null-window model reports a usable window via the provider-config default,
    so /status shows the budget compaction actually uses rather than 'unknown'."""
    model = Model(
        model_id="thin-model",
        name="Thin Model",
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
        ),
        context_window=None,
        max_output_tokens=None,
    )

    class _Models:
        def get(self, _provider_id: str, _model_id: str) -> Model:
            return model

    class _Providers:
        def get(self, _provider_id: str) -> Any:
            return ProviderConfig(
                id="thin",
                name="Thin",
                adapter="openai_compatible",
                base_url="https://example.test/v1",
                context_window=64_000,
            )

    details = resolve_status_model_details(
        _make_agent(model="thin/thin-model"),
        cast(ModelRegistry, _Models()),
        cast(Any, _Providers()),
    )

    assert details.context_window == 64_000


def test_resolve_status_model_details_falls_back_to_global_floor() -> None:
    """With neither a model window nor a provider default, /status reports the
    conservative global floor instead of failing or showing nothing."""
    from core.providers.providers import GLOBAL_CONTEXT_WINDOW_FLOOR

    model = Model(
        model_id="custom",
        name="Custom",
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
        ),
        context_window=None,
        max_output_tokens=None,
    )

    class _Models:
        def get(self, _provider_id: str, _model_id: str) -> Model:
            return model

    details = resolve_status_model_details(
        _make_agent(model="custom/custom"),
        cast(ModelRegistry, _Models()),
        None,
    )

    assert details.context_window == GLOBAL_CONTEXT_WINDOW_FLOOR
