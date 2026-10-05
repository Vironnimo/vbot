"""The built-in status Tool through production dispatch.

The report text itself is built by ``core.chat.status_report`` and covered with the /status
command in ``tests/core/chat/test_commands_status.py``; these tests guard the Tool's target
selection, its refusals, and that it feeds the report from the services it was registered with.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from core.agents.agents import Agent
from core.chat import (
    ChatSessionError,
    CommandDispatcher,
    CommandExecutionContext,
    ReplySurface,
)
from core.chat.messages import ChatMessage
from core.chat.status_report import STATUS_PLACEHOLDER, ReasoningIntent, status_session_facts
from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.projects import (
    AgentResolutionError,
    AgentResolver,
    ProjectStore,
    ResolutionAgentNotFoundError,
    ResolutionProjectNotFoundError,
    RuntimeAgent,
)
from core.runs import ChatRunManager, Run
from core.sessions import ChatSessionManager, SessionAddress
from core.settings.settings import SettingsValidationError
from core.tools import ToolAccess, ToolContext, ToolRegistry
from core.tools.status import STATUS_TOOL_NAME, register_status_tool
from tests.core.tools.tools_test_support import dispatch_as_executor


def _make_agent(**overrides: Any) -> Agent:
    agent = Agent(
        id="coder",
        name="Coder",
        model="openai/gpt-5.2",
        fallback_models=["openai/gpt-5.1"],
        workspace="workspace",
        temperature=0.3,
        thinking_effort="none",
        tool_access=ToolAccess(mode="all"),
        allowed_skills=["*"],
        tools={},
        created_at="2026-05-18T10:00:00+00:00",
        updated_at="2026-05-18T10:00:00+00:00",
    )
    return replace(agent, **overrides)


def _make_model(**overrides: Any) -> Model:
    model = Model(
        model_id="gpt-5.2",
        name="GPT-5.2",
        capabilities=Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=True),
        ),
        context_window=200_000,
        max_output_tokens=8_192,
    )
    return replace(model, **overrides)


def _context(tmp_path: Path, *, project_id: str | None = None) -> ToolContext:
    return ToolContext(
        agent_id="coder",
        session_id="session-one",
        run_id="run-one",
        tool_call_id="call-one",
        tool_name=STATUS_TOOL_NAME,
        tool_call_index=0,
        workspace=tmp_path,
        vbot_root=tmp_path,
        data_root=tmp_path,
        project_id=project_id,
    )


class _StubResolver:
    """Returns one Agent for every target and records the targets it was asked for."""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent
        self.calls: list[tuple[str | None, str, str | None]] = []

    def resolve_agent(
        self, project_id: str | None, agent_id: str, *, session_id: str | None = None
    ) -> Agent:
        self.calls.append((project_id, agent_id, session_id))
        return self._agent

    async def resolve_agent_async(
        self, project_id: str | None, agent_id: str, *, session_id: str | None = None
    ) -> Agent:
        return self.resolve_agent(project_id, agent_id, session_id=session_id)


class _RaisingResolver:
    def __init__(self, error: AgentResolutionError) -> None:
        self._error = error

    def resolve_agent(
        self, _project_id: str | None, _agent_id: str, *, session_id: str | None = None
    ) -> Agent:
        raise self._error


class _StubSession:
    def __init__(self, messages: list[ChatMessage]) -> None:
        self._messages = messages

    def load(self) -> list[ChatMessage]:
        return list(self._messages)

    def status_snapshot(self) -> Any:
        return status_session_facts(self._messages)


class _StubSessions:
    def __init__(self, messages: list[ChatMessage]) -> None:
        self._session = _StubSession(messages)
        self.calls: list[tuple[str, str, str | None]] = []

    def get(self, address: SessionAddress) -> _StubSession:
        self.calls.append((address.agent_id, address.session_id, address.project_id))
        return self._session


class _NotFoundSessions:
    def get(self, address: SessionAddress) -> _StubSession:
        raise ChatSessionError(f"session does not exist: {address.session_id}")


class _StubModels:
    def __init__(self, model: Model) -> None:
        self._model = model

    def get(self, _provider_id: str, _model_id: str) -> Model:
        return self._model


class _StubProject:
    def __init__(self, project_id: str, display_name: str) -> None:
        self.project_id = project_id
        self.display_name = display_name


class _StubProjects:
    def __init__(self, project: _StubProject) -> None:
        self._project = project

    def get(self, project_id: str) -> _StubProject:
        if project_id != self._project.project_id:
            raise KeyError(project_id)
        return self._project


def _registry(
    *,
    resolver: Any = None,
    sessions: Any = None,
    models: Any = None,
    chat_runs: ChatRunManager | None = None,
    started_at: datetime | None = None,
    **options: Any,
) -> ToolRegistry:
    registry = ToolRegistry()
    register_status_tool(
        registry,
        cast(AgentResolver, resolver or _StubResolver(_make_agent())),
        cast(ChatSessionManager, sessions if sessions is not None else _StubSessions([])),
        cast(ModelRegistry, models or _StubModels(_make_model())),
        chat_runs or ChatRunManager(),
        started_at,
        **options,
    )
    return registry


def _dispatch(
    registry: ToolRegistry,
    tmp_path: Path,
    arguments: dict[str, object] | None = None,
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    context = _context(tmp_path, project_id=project_id)
    return asyncio.run(dispatch_as_executor(registry, context, arguments or {}))


def test_schema_offers_only_the_optional_target() -> None:
    tool = _registry().get(STATUS_TOOL_NAME)

    assert set(tool.parameters["properties"]) == {"agent_id", "session_id"}
    assert tool.parameters["required"] == []
    assert tool.open_input_schema is True


def test_status_tool_reports_the_current_session_like_the_status_command(tmp_path: Path) -> None:
    session_started = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
    started_at = datetime(2026, 5, 18, 9, 0, tzinfo=UTC)
    messages = [
        ChatMessage.user("Status check", timestamp=session_started),
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="All systems go.",
            usage={
                "input_tokens": 1234,
                "output_tokens": 42,
                "cache_read_tokens": 800,
                "cache_write_tokens": 100,
            },
            timestamp=session_started,
        ),
    ]
    resolver = cast(AgentResolver, _StubResolver(_make_agent()))
    sessions = cast(ChatSessionManager, _StubSessions(messages))
    models = cast(ModelRegistry, _StubModels(_make_model(name="GPT-5.2 Registry")))
    dispatcher = CommandDispatcher(
        ChatRunManager(),
        agent_resolver=resolver,
        sessions=sessions,
        models=models,
        started_at=started_at,
    )
    prepared = dispatcher.prepare("/status")
    assert prepared is not None
    command_result = asyncio.run(
        dispatcher.execute(
            prepared,
            CommandExecutionContext(
                agent_id="coder",
                session_id="session-one",
                project_id=None,
                reply_surface=ReplySurface.webui(),
            ),
        )
    )
    assert command_result.feedback is not None
    registry = _registry(resolver=resolver, sessions=sessions, models=models, started_at=started_at)

    result = _dispatch(registry, tmp_path)

    assert result["ok"] is True
    data = result["data"]
    assert (set(data), data["agent_id"], data["session_id"]) == (
        {"text", "agent_id", "session_id"},
        "coder",
        "session-one",
    )

    def _without_live_time_lines(status_text: str) -> list[str]:
        return [
            line
            for line in status_text.splitlines()
            if not line.startswith(("Session started:", "App uptime:", "Current time:"))
        ]

    # Only the user's /status reports the wire profile; Agents cannot act on it.
    wire_lines = ("Wire profile:", "Learned wire facts:")
    assert _without_live_time_lines(data["text"]) == [
        line
        for line in _without_live_time_lines(command_result.feedback.text)
        if not line.startswith(wire_lines)
    ]
    assert f"Wire profile: {STATUS_PLACEHOLDER}" in command_result.feedback.text.splitlines()
    assert not any(line.startswith(wire_lines) for line in data["text"].splitlines())
    assert "Agent: Coder (openai/gpt-5.2)" in data["text"]
    assert "Activity: idle" in data["text"]
    assert "Session cache: read 800 / 1234 (64.8% hit), write 100, turns 1" in data["text"]


def test_status_tool_reports_through_the_services_it_was_registered_with(tmp_path: Path) -> None:
    # A project run: the resolver and the Session lookup receive the Project and the Session
    # (whose Agent overrides the report must reflect), and the Model
    # registry, the Project store and the reasoning describer each feed their report line.
    resolver = _StubResolver(_make_agent(thinking_effort="xhigh", temperature=None))
    sessions = _StubSessions([])
    described: list[tuple[str, str | None]] = []

    def describe_render(agent: RuntimeAgent) -> ReasoningIntent:
        described.append((agent.model, agent.thinking_effort))
        return ReasoningIntent("effort", effort_level="max")

    registry = _registry(
        resolver=resolver,
        sessions=sessions,
        models=_StubModels(_make_model(name="GPT-5.2 Registry", recommended_temperature=1.0)),
        projects=cast(ProjectStore, _StubProjects(_StubProject("vbot", "vBot"))),
        reasoning_render_describer=describe_render,
    )

    result = _dispatch(registry, tmp_path, project_id="vbot")

    text = result["data"]["text"]
    assert resolver.calls == [("vbot", "coder", "session-one")]
    assert sessions.calls == [("coder", "session-one", "vbot")]
    assert described == [("openai/gpt-5.2", "xhigh")]
    for line in (
        "Project: vBot (vbot)",
        "Model display name: GPT-5.2 Registry",
        "Selected thinking effort: xhigh",
        "Actual model thinking effort: max",
        "Temperature: provider default (Model recommends 1)",
        "Top P: provider default",
    ):
        assert line in text


def _unknown_configured_zone() -> str:
    raise SettingsValidationError("settings.timezone is not a known IANA timezone")


@pytest.mark.parametrize(
    ("timezone_name_loader", "zone_names"),
    [
        pytest.param(lambda: "Europe/Berlin", {"CET", "CEST"}, id="configured-zone"),
        pytest.param(_unknown_configured_zone, {"UTC"}, id="unknown-zone-reports-in-utc"),
    ],
)
def test_status_times_name_the_configured_zone_or_utc(
    tmp_path: Path, timezone_name_loader: Any, zone_names: set[str]
) -> None:
    # status only reads: an unusable configured zone must not cost the report.
    registry = _registry(timezone_name_loader=timezone_name_loader)

    result = _dispatch(registry, tmp_path)

    [current] = [
        line for line in result["data"]["text"].splitlines() if line.startswith("Current time:")
    ]
    assert current.rsplit(" ", 1)[1] in zone_names


@pytest.mark.parametrize(
    ("resolver_error", "expected_code"),
    [
        (ResolutionAgentNotFoundError("Agent not found: coder"), "agent_not_found"),
        (ResolutionProjectNotFoundError("Project not found: vbot"), "project_not_found"),
        (AgentResolutionError("agent 'coder' has no usable model"), "agent_unavailable"),
    ],
)
def test_status_tool_reports_why_the_target_agent_cannot_be_resolved(
    tmp_path: Path, resolver_error: AgentResolutionError, expected_code: str
) -> None:
    # Only a missing Agent or Project is "not found"; an Agent that exists but
    # cannot run reports the resolver's reason instead.
    registry = _registry(resolver=_RaisingResolver(resolver_error))

    result = _dispatch(registry, tmp_path, project_id="vbot")

    assert result["ok"] is False
    error = result["error"]
    assert error["code"] == expected_code
    if expected_code == "agent_unavailable":
        assert error["retryable"] is False
        assert "agent 'coder' has no usable model" in error["message"]
        assert "coder@vbot" in error["message"]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"session_id": "missing"},
            "No Session missing exists for Agent coder. Omit session_id to check your current "
            "Session. If that Session belongs to another Agent, such as a Sub-Agent, also pass "
            "that Agent's agent_id.",
        ),
        (
            {"agent_id": "worker", "session_id": "missing"},
            "No Session missing exists for Agent worker. Omit session_id to check your current "
            "Session.",
        ),
        # Nothing was named, so there is no other call to suggest.
        ({"session_id": "current"}, "No Session session-one exists for Agent coder."),
    ],
)
def test_status_tool_names_the_next_call_when_session_is_missing(
    tmp_path: Path, arguments: dict[str, object], message: str
) -> None:
    registry = _registry(sessions=_NotFoundSessions())

    result = _dispatch(registry, tmp_path, arguments)

    assert result["error"] == {"code": "session_not_found", "message": message}


def test_status_tool_names_the_next_call_when_agent_is_missing(tmp_path: Path) -> None:
    registry = _registry(resolver=_RaisingResolver(ResolutionAgentNotFoundError("Agent not found")))

    result = _dispatch(registry, tmp_path, {"agent_id": "gone", "session_id": "s"})

    assert result["error"] == {
        "code": "agent_not_found",
        "message": (
            "Agent not found: gone. Omit agent_id and session_id to check your current Session."
        ),
    }


def test_status_tool_rejects_agent_id_without_session_id(tmp_path: Path) -> None:
    sessions = _StubSessions([])
    registry = _registry(sessions=sessions)

    result = _dispatch(registry, tmp_path, {"agent_id": "other"})
    own = _dispatch(registry, tmp_path, {"agent_id": "coder"})

    assert result["ok"] is False
    assert result["error"] == {
        "code": "invalid_arguments",
        "message": (
            "status needs session_id to inspect a Session of Agent other; nothing was checked. "
            'Call {"agent_id": "other", "session_id": "<session id>"}, for example with the '
            "session_id from a subagent result, or omit agent_id to check your current Session."
        ),
    }
    # Naming yourself without a Session checks the current Session.
    assert own["ok"] is True
    assert sessions.calls == [("coder", "session-one", None)]


@pytest.mark.parametrize(
    ("arguments", "target"),
    [
        # Placeholders and words for "my current Session" mean omission.
        ({"session_id": "", "agent_id": " "}, ("coder", "session-one")),
        ({"session_id": "current", "agent_id": "null"}, ("coder", "session-one")),
        ({"session_id": "this", "id": "."}, ("coder", "session-one")),
        ({"session": "ses_abcdefghijkl"}, ("coder", "ses_abcdefghijkl")),
        ({"agent": "worker", "sessionId": "ses_abcdefghijkl"}, ("worker", "ses_abcdefghijkl")),
        # A Session id sent as "id" is read as session_id.
        ({"id": "ses_abcdefghijkl"}, ("coder", "ses_abcdefghijkl")),
        (
            {"id": "ses_abcdefghijkl", "session_id": "ses_abcdefghijkl"},
            ("coder", "ses_abcdefghijkl"),
        ),
        ({"id": "ses_abcdefghijkl", "agent_id": "worker"}, ("worker", "ses_abcdefghijkl")),
    ],
)
def test_status_tool_reads_clear_targets_written_other_ways(
    tmp_path: Path, arguments: dict[str, object], target: tuple[str, str]
) -> None:
    sessions = _StubSessions([])
    registry = _registry(sessions=sessions)

    result = _dispatch(registry, tmp_path, arguments)

    assert result["ok"] is True, result
    assert (result["data"]["agent_id"], result["data"]["session_id"]) == target
    assert sessions.calls == [(*target, None)]


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            {"id": "sub_abcdefghijkl"},
            "status was not run: sub_abcdefghijkl is a Sub-Agent id. To see that Sub-Agent, "
            'call subagent with {"action": "list", "id": "sub_abcdefghijkl"}. status reports '
            "a chat Session and takes session_id, with agent_id for another Agent's Session.",
        ),
        (
            {"work_id": "sub_abcdefghijkl", "agent_id": "worker"},
            "status was not run: sub_abcdefghijkl is a Sub-Agent id. To see that Sub-Agent, "
            'call subagent with {"action": "list", "id": "sub_abcdefghijkl"}. status reports '
            "a chat Session and takes session_id, with agent_id for another Agent's Session.",
        ),
        (
            {"id": "worker"},
            'status was not run: it has no "id" parameter, so "worker" is ambiguous. Pass a '
            'Session as "session_id", with "agent_id" for another Agent\'s Session, or omit '
            "both to check your current Session.",
        ),
        (
            {"id": "ses_abcdefghijkl", "session_id": "ses_zzzzzzzzzzzz"},
            "Conflicting values for session_id; provide one intended value.",
        ),
        (
            {"session_id": "ses_abcdefghijkl", "session": "ses_zzzzzzzzzzzz"},
            'Conflicting values for session_id: session_id is "ses_abcdefghijkl" and session '
            'is "ses_zzzzzzzzzzzz". Send only the intended one.',
        ),
    ],
)
def test_status_tool_refuses_unclear_ids_before_lookup(
    tmp_path: Path, arguments: dict[str, object], message: str
) -> None:
    sessions = _StubSessions([])
    registry = _registry(sessions=sessions)

    result = _dispatch(registry, tmp_path, arguments)

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert message in result["error"]["message"]
    assert sessions.calls == []


def test_status_tool_rejects_unknown_arguments(tmp_path: Path) -> None:
    result = _dispatch(_registry(), tmp_path, {"unexpected": True})

    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_arguments"
    assert "unexpected" in result["error"]["message"]


@pytest.mark.parametrize(
    "arguments",
    (
        {"request": {"operation": "current"}},
        {"current": {}},
        {"action": "current"},
    ),
)
def test_status_tool_accepts_recognizable_operation_shapes(
    tmp_path: Path,
    arguments: dict[str, object],
) -> None:
    result = _dispatch(_registry(), tmp_path, arguments)

    assert result["ok"] is True
    assert result["data"]["session_id"] == "session-one"


@pytest.mark.asyncio
async def test_status_tool_reports_running_target_session(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    chat_runs = ChatRunManager()

    async def execute(_run: Run) -> str:
        started.set()
        await release.wait()
        return "done"

    run = await chat_runs.start(
        SessionAddress(project_id=None, agent_id="reviewer", session_id="session-two"),
        execute,
    )
    await started.wait()
    registry = _registry(chat_runs=chat_runs)

    result = await dispatch_as_executor(
        registry, _context(tmp_path), {"agent_id": "reviewer", "session_id": "session-two"}
    )
    expected_updated_at = run.updated_at
    release.set()
    await run.wait()

    assert result["ok"] is True
    data = cast(dict[str, Any], result["data"])
    text = cast(str, data["text"])
    assert "Activity: running" in text
    assert f"Run created at: {run.created_at}" in text
    assert f"Run updated at: {expected_updated_at}" in text
    assert data["agent_id"] == "reviewer"
    assert data["session_id"] == "session-two"
