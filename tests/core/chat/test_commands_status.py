"""The /status command: the dispatched reply and the report pieces it is built from."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from core.chat import ChatMessage, CommandDispatcher
from core.chat.status_report import (
    STATUS_PLACEHOLDER,
    ReasoningIntent,
    StatusModelDetails,
    StatusWireProfile,
    WireProfileDescriber,
    build_status_text,
    resolve_actual_thinking_effort,
    resolve_reported_thinking_effort,
    resolve_status_model_details,
    resolve_status_project_label,
    resolve_status_temperature,
    status_session_facts,
)
from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
from core.projects import AgentResolver, ProjectStore
from core.providers.providers import GLOBAL_CONTEXT_WINDOW_FLOOR, ProviderConfig
from core.providers.wire_observations import ObservedFacts
from core.runs import ChatRunManager, Run
from core.sessions import ChatSessionManager, SessionAddress
from tests.core.chat.commands_test_support import (
    _execute,
    _execute_sync,
    _make_agent,
    _StubProject,
    _StubProjects,
    _StubResolver,
)

_SESSION_STARTED = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
_APP_STARTED = datetime(2026, 5, 18, 9, 0, tzinfo=UTC)


def _make_model(
    *,
    model_id: str = "gpt-5.2",
    name: str = "GPT-5.2",
    recommended_temperature: float | None = None,
    context_window: int | None = 200_000,
    reasoning: ReasoningCapabilities | None = None,
) -> Model:
    return Model(
        model_id=model_id,
        name=name,
        capabilities=Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=reasoning or ReasoningCapabilities(supported=True),
        ),
        context_window=context_window,
        max_output_tokens=8_192,
        recommended_temperature=recommended_temperature,
    )


def _provider(**options: Any) -> ProviderConfig:
    return ProviderConfig(
        id="provider",
        name="Provider",
        adapter="openai_compatible",
        base_url="https://example.test/v1",
        **options,
    )


class _StubSession:
    def __init__(self, messages: list[ChatMessage]) -> None:
        self._messages = messages

    def load(self) -> list[ChatMessage]:
        return list(self._messages)

    def status_snapshot(self) -> Any:
        return status_session_facts(self._messages)


class _StubSessions:
    def __init__(self, messages: list[ChatMessage] | None = None) -> None:
        self._session = _StubSession(messages or [])

    def get(self, _address: SessionAddress) -> _StubSession:
        return self._session


class _RecordingModels:
    """Model registry holding one ``openai`` Model and recording each lookup."""

    def __init__(self, model: Model) -> None:
        self._model = model
        self.calls: list[tuple[str, str]] = []

    def get(self, provider_id: str, model_id: str) -> Model:
        self.calls.append((provider_id, model_id))
        if provider_id != "openai" or model_id != "gpt-5.2":
            raise KeyError(model_id)
        return self._model


class _AnyModels:
    """Model registry answering every lookup with one Model."""

    def __init__(self, model: Model) -> None:
        self._model = model

    def get(self, _provider_id: str, _model_id: str) -> Model:
        return self._model


class _Providers:
    def __init__(self, provider: ProviderConfig) -> None:
        self._provider = provider

    def get(self, _provider_id: str) -> ProviderConfig:
        return self._provider


def _cached_turn(usage: dict[str, Any], content: str = "Answer.") -> ChatMessage:
    return ChatMessage.assistant(
        model="openai/gpt-5.2", content=content, usage=usage, timestamp=_SESSION_STARTED
    )


def _status_dispatcher(
    manager: ChatRunManager | None = None,
    *,
    resolver: _StubResolver | None = None,
    messages: list[ChatMessage] | None = None,
    model: Model | None = None,
    wire_profile_describer: WireProfileDescriber | None = None,
) -> tuple[CommandDispatcher, _StubResolver, _RecordingModels]:
    resolver = resolver or _StubResolver(_make_agent())
    models = _RecordingModels(model or _make_model())
    dispatcher = CommandDispatcher(
        manager or ChatRunManager(),
        agent_resolver=cast(AgentResolver, resolver),
        sessions=cast(ChatSessionManager, _StubSessions(messages)),
        models=cast(ModelRegistry, models),
        projects=cast(ProjectStore, _StubProjects(_StubProject("vbot", "vBot"))),
        started_at=_APP_STARTED,
        wire_profile_describer=wire_profile_describer,
    )
    return dispatcher, resolver, models


def test_status_without_services_is_a_degraded_detail_reply() -> None:
    result = _execute_sync(CommandDispatcher(ChatRunManager()), "/status")

    assert result.feedback is not None
    assert result.feedback.kind == "detail"
    reply = result.feedback.text
    assert f"Agent: {STATUS_PLACEHOLDER}" in reply
    assert "Activity: idle" in reply
    assert f"Run created at: {STATUS_PLACEHOLDER}" in reply
    assert f"Run updated at: {STATUS_PLACEHOLDER}" in reply
    assert f"Last request cache: {STATUS_PLACEHOLDER}" in reply
    assert f"Session cache: {STATUS_PLACEHOLDER}" in reply
    assert "Current time:" in reply


def test_status_reports_the_identity_sessions_agent_model_and_cache() -> None:
    messages = [
        ChatMessage.user("Status check", timestamp=_SESSION_STARTED),
        _cached_turn(
            {
                "input_tokens": 1234,
                "output_tokens": 42,
                "cache_read_tokens": 800,
                "cache_write_tokens": 100,
            }
        ),
    ]
    # A pinned connection suffix is stripped before the Model registry lookup.
    dispatcher, resolver, models = _status_dispatcher(
        resolver=_StubResolver(_make_agent(model="openai/gpt-5.2::primary")),
        messages=messages,
        model=_make_model(name="GPT-5.2 Registry"),
    )

    result = _execute_sync(dispatcher, "/status")

    assert result.feedback is not None
    reply = result.feedback.text
    assert resolver.calls == [(None, "coder", "session-one")]
    assert models.calls == [("openai", "gpt-5.2")]
    assert "Agent: Coder (openai/gpt-5.2::primary)" in reply
    assert "Model display name: GPT-5.2 Registry" in reply
    assert "Temperature: 0.3 (agent)" in reply
    assert f"Project: {STATUS_PLACEHOLDER}" in reply
    assert "Activity: idle" in reply
    assert f"Run created at: {STATUS_PLACEHOLDER}" in reply
    assert "Context usage: 1234 / 200000" in reply
    assert "Last request cache: read 800 / 1234 (64.8% hit), write 100" in reply
    assert "Session cache: read 800 / 1234 (64.8% hit), write 100, turns 1" in reply
    assert "Current time:" in reply


def test_status_in_a_project_session_resolves_its_config_agent() -> None:
    dispatcher, resolver, _ = _status_dispatcher(
        messages=[ChatMessage.user("Status check", timestamp=_SESSION_STARTED)]
    )

    result = _execute_sync(dispatcher, "/status", agent_id="builder", project_id="vbot")

    assert result.feedback is not None
    assert resolver.calls == [("vbot", "builder", "session-one")]
    assert "Agent: Coder (openai/gpt-5.2)" in result.feedback.text
    assert "Project: vBot (vbot)" in result.feedback.text


def test_status_names_the_model_recommended_temperature_without_sending_it() -> None:
    dispatcher, _, _ = _status_dispatcher(
        resolver=_StubResolver(_make_agent(temperature=None)),
        model=_make_model(recommended_temperature=1.0),
    )

    result = _execute_sync(dispatcher, "/status")

    assert result.feedback is not None
    assert "Temperature: provider default (Model recommends 1)" in result.feedback.text


def _wire_profile_fails(_agent: Any) -> StatusWireProfile | None:
    raise RuntimeError("profile files unavailable")


@pytest.mark.parametrize(
    ("described", "expected"),
    [
        pytest.param(
            StatusWireProfile("openai:api-key", "verified", "2026-09-30", ObservedFacts()),
            ["Wire profile: verified on 2026-09-30 (Connection openai:api-key)"],
            id="verified",
        ),
        pytest.param(
            StatusWireProfile(
                "openai:api-key:work",
                "configured",
                None,
                ObservedFacts(
                    reasoning_field="reasoning_content",
                    rejected_parameters=("temperature", "top_p"),
                    exclusive_parameters=("temperature+top_k",),
                    rejected_efforts=("xhigh",),
                    reasoning_returned=True,
                ),
            ),
            [
                "Wire profile: configured, unverified (Connection openai:api-key:work)",
                "Learned wire facts: reasoning arrives in reasoning_content; "
                "rejected parameters: temperature, top_p; only one of: temperature or top_k; "
                "rejected reasoning efforts: xhigh",
            ],
            id="configured-with-learned-facts",
        ),
        pytest.param(
            StatusWireProfile(
                "openai:api-key", "inferred", None, ObservedFacts(reasoning_returned=True)
            ),
            [
                "Wire profile: inferred from defaults, unverified (Connection openai:api-key)",
                "Learned wire facts: reasoning is returned",
            ],
            id="inferred-with-returned-reasoning",
        ),
        pytest.param(None, [f"Wire profile: {STATUS_PLACEHOLDER}"], id="no-usable-connection"),
        pytest.param(
            _wire_profile_fails, [f"Wire profile: {STATUS_PLACEHOLDER}"], id="describer-fails"
        ),
    ],
)
def test_status_reports_the_wire_profile_of_the_models_connection(
    described: Any, expected: list[str]
) -> None:
    described_agents: list[str] = []

    def describe(agent: Any) -> StatusWireProfile | None:
        described_agents.append(agent.model)
        if callable(described):
            return cast("StatusWireProfile | None", described(agent))
        return cast("StatusWireProfile | None", described)

    dispatcher, _, _ = _status_dispatcher(
        resolver=_StubResolver(_make_agent(model="openai/gpt-5.2::api-key:work")),
        wire_profile_describer=describe,
    )

    result = _execute_sync(dispatcher, "/status")

    assert result.feedback is not None
    assert described_agents == ["openai/gpt-5.2::api-key:work"]
    assert [
        line
        for line in result.feedback.text.splitlines()
        if line.startswith(("Wire profile:", "Learned wire facts:"))
    ] == expected


@pytest.mark.asyncio
async def test_status_reports_active_run_timestamps() -> None:
    manager = ChatRunManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(_run: Run) -> str:
        started.set()
        await release.wait()
        return "done"

    run = await manager.start(
        SessionAddress(project_id=None, agent_id="coder", session_id="session-one"), execute
    )
    await started.wait()
    dispatcher, _, _ = _status_dispatcher(manager)

    result = await _execute(dispatcher, "/status")
    expected_updated_at = run.updated_at
    release.set()
    await run.wait()

    assert result.feedback is not None
    assert "Activity: running" in result.feedback.text
    assert f"Run created at: {run.created_at}" in result.feedback.text
    assert f"Run updated at: {expected_updated_at}" in result.feedback.text


@pytest.mark.parametrize(
    ("store", "project_id", "expected"),
    [
        (_StubProject("vbot", "vBot"), "vbot", "vBot (vbot)"),
        (_StubProject("vbot", "vBot"), None, None),
        # A missing store or an unloadable Project still names the stable id.
        (None, "vbot", "vbot"),
        (_StubProject("other", "Other"), "vbot", "vbot"),
    ],
    ids=["name-and-id", "identity-session", "no-store", "unknown-project"],
)
def test_status_project_label(
    store: _StubProject | None, project_id: str | None, expected: str | None
) -> None:
    projects = cast(ProjectStore, _StubProjects(store)) if store is not None else None

    assert resolve_status_project_label(projects, project_id) == expected


def test_status_text_without_data_shows_placeholders() -> None:
    text = build_status_text(None, [], None, None)

    for label in (
        "Agent",
        "Project",
        "Model display name",
        "Fallback models",
        "Selected thinking effort",
        "Actual model thinking effort",
        "Temperature",
        "Context usage",
        "Last request cache",
        "Session cache",
        "Activity",
        "Run created at",
        "Run updated at",
        "Session started",
        "Turn count",
        "App uptime",
    ):
        assert f"{label}: {STATUS_PLACEHOLDER}" in text
    assert "Current time:" in text


def test_status_text_marks_an_estimated_context_and_omits_its_cache() -> None:
    messages = [
        ChatMessage.user("Status check", timestamp=_SESSION_STARTED),
        _cached_turn(
            {
                "input_tokens": 987,
                "output_tokens": 12,
                "input_tokens_estimated": True,
                "output_tokens_estimated": True,
                "estimated": True,
            }
        ),
    ]

    text = build_status_text(
        _make_agent(), messages, context_window=200_000, started_at=_APP_STARTED
    )

    assert "Agent: Coder (openai/gpt-5.2)" in text
    assert "Model display name: gpt-5.2" in text
    assert "Fallback models: openai/gpt-5.1" in text
    assert "Selected thinking effort: none" in text
    assert f"Actual model thinking effort: {STATUS_PLACEHOLDER}" in text
    assert "Temperature: 0.3" in text
    assert f"Activity: {STATUS_PLACEHOLDER}" in text
    assert "Context usage: ~987 / 200000" in text
    assert f"Last request cache: {STATUS_PLACEHOLDER}" in text
    assert f"Session cache: {STATUS_PLACEHOLDER}" in text
    assert "Session started:" in text
    assert "Turn count: 1" in text
    assert "App uptime:" in text


def _two_cached_turns() -> list[ChatMessage]:
    """One fully measured turn, then one whose Provider omitted only the output."""
    return [
        ChatMessage.user("Status check", timestamp=_SESSION_STARTED),
        _cached_turn(
            {
                "input_tokens": 1000,
                "output_tokens": 12,
                "cache_read_tokens": 800,
                "cache_write_tokens": 100,
            },
            "First answer.",
        ),
        _cached_turn(
            {
                "input_tokens": 500,
                "output_tokens": 8,
                "output_tokens_estimated": True,
                "estimated": True,
                "cache_read_tokens": 200,
            },
            "Second answer.",
        ),
    ]


@pytest.mark.parametrize(
    ("latest_usage", "context", "latest_cache", "session_cache"),
    [
        (
            None,
            "500 / 200000",
            "read 200 / 500 (40.0% hit), write 0",
            "read 1000 / 1500 (66.7% hit), write 100, turns 2",
        ),
        (
            {
                "input_tokens": 500,
                "input_tokens_estimated": True,
                "output_tokens": 8,
                "estimated": True,
                "cache_read_tokens": 200,
            },
            "~500 / 200000",
            STATUS_PLACEHOLDER,
            "read 800 / 1000 (80.0% hit), write 100, turns 1",
        ),
    ],
    ids=["output-estimated", "input-estimated"],
)
def test_status_text_cache_figures_count_only_measured_input(
    latest_usage: dict[str, Any] | None, context: str, latest_cache: str, session_cache: str
) -> None:
    messages = _two_cached_turns()
    if latest_usage is not None:
        messages[-1] = replace(messages[-1], usage=latest_usage)

    text = build_status_text(
        _make_agent(), messages, context_window=200_000, started_at=_APP_STARTED
    )

    assert f"Context usage: {context}" in text
    assert f"Last request cache: {latest_cache}" in text
    assert f"Session cache: {session_cache}" in text


@pytest.mark.usefixtures("current_format_data_directory")
def test_status_session_facts_match_the_persisted_status_snapshot(tmp_path: Path) -> None:
    messages = _two_cached_turns()
    sessions = ChatSessionManager(tmp_path)
    session = sessions.create("agent", session_id="session-one")
    session.append_many(messages)

    persisted = session.status_snapshot()
    in_memory = status_session_facts(messages)
    sessions.close()

    assert persisted.cache_input_tokens == in_memory.cache_input_tokens == 1500
    assert persisted.session_usage == in_memory.session_usage
    assert persisted.latest_assistant_usage == in_memory.latest_assistant_usage


def test_status_text_renders_unset_defaults_and_the_effort_split() -> None:
    unset = build_status_text(
        _make_agent(temperature=None, thinking_effort=None),
        messages=[],
        context_window=None,
        started_at=None,
    )
    snapped = build_status_text(
        _make_agent(thinking_effort="max"),
        messages=[],
        context_window=200_000,
        started_at=None,
        actual_thinking_effort="high",
    )

    assert "Selected thinking effort: default" in unset
    assert "Temperature: provider default" in unset
    assert "Selected thinking effort: max" in snapped
    assert "Actual model thinking effort: high" in snapped


_LADDER = ("low", "medium", "high")


@pytest.mark.parametrize(
    ("selection", "levels", "control", "budget_max", "expected"),
    [
        ("max", _LADDER, None, None, "high"),
        ("medium", ("low", "high"), None, None, "low"),
        ("high", (), None, None, None),
        ("", _LADDER, None, None, None),
        (None, _LADDER, None, None, None),
        ("high", (), "on_off", None, "on"),
        ("none", (), "on_off", None, "off"),
        ("", (), "on_off", None, None),
        ("medium", (), "budget", None, "on (8,192 tokens)"),
        ("high", (), "budget", 32000, "on (24,000 tokens)"),
        ("none", (), "budget", None, "off"),
    ],
    ids=[
        "snaps-down-to-ladder",
        "snaps-to-nearest-lower",
        "no-ladder",
        "empty-selection",
        "no-selection",
        "toggle-on",
        "toggle-off",
        "toggle-unselected",
        "budget-fallback-ladder",
        "budget-scaled-to-max",
        "budget-off",
    ],
)
def test_actual_thinking_effort_follows_the_models_reasoning_control(
    selection: str | None,
    levels: tuple[str, ...],
    control: str | None,
    budget_max: int | None,
    expected: str | None,
) -> None:
    arguments: list[Any] = [selection, levels]
    if control is not None:
        arguments.append(control)
    if budget_max is not None:
        arguments.append(budget_max)

    assert resolve_actual_thinking_effort(*arguments) == expected


def _broken_describer(*_args: Any) -> ReasoningIntent:
    raise KeyError("provider missing")


@pytest.mark.parametrize(
    ("effort", "describe", "expected"),
    [
        # The adapter's render description wins over the declared on/off control: the
        # Cloud wire carries the effort level, so the report is the level, not "on".
        ("xhigh", lambda *_args: ReasoningIntent("effort", effort_level="max"), "max"),
        ("none", lambda *_args: ReasoningIntent("off"), "off"),
        ("xhigh", None, "on"),
        ("xhigh", lambda *_args: None, "on"),
        ("xhigh", _broken_describer, "on"),
    ],
    ids=["described-level", "described-off", "no-describer", "undescribed", "describer-error"],
)
def test_reported_thinking_effort_prefers_the_adapter_description(
    effort: str, describe: Callable[..., ReasoningIntent | None] | None, expected: str
) -> None:
    calls: list[tuple[Any, ...]] = []

    def recording(*args: Any) -> ReasoningIntent | None:
        calls.append(args)
        assert describe is not None
        return describe(*args)

    agent = _make_agent(model="ollama-cloud/glm-5.3-flash", thinking_effort=effort)
    reported = resolve_reported_thinking_effort(
        agent=agent,
        models=cast(ModelRegistry, object()),
        model_details=StatusModelDetails(
            context_window=1_048_576,
            display_name="glm-5.3-flash",
            reasoning_levels=(),
            reasoning_control="on_off",
        ),
        describe_render=recording if describe is not None else None,
    )

    assert reported == expected
    # The describer resolves the Agent's Provider, Connection and Model itself.
    assert calls == ([] if describe is None else [(agent,)])


def test_reported_thinking_effort_without_an_agent_is_unknown() -> None:
    details = StatusModelDetails(context_window=None, display_name=None)

    assert resolve_reported_thinking_effort(agent=None, models=None, model_details=details) is None


@pytest.mark.parametrize(
    ("model", "provider", "expected"),
    [
        (
            _make_model(
                reasoning=ReasoningCapabilities(supported=True, control="levels", levels=_LADDER)
            ),
            None,
            {
                "context_window": 200_000,
                "display_name": "GPT-5.2",
                "reasoning_levels": _LADDER,
                "reasoning_control": "levels",
            },
        ),
        # A null window resolves through the Provider default, then the global floor,
        # so /status shows the budget Compaction actually uses.
        (
            _make_model(context_window=None),
            _provider(context_window=64_000),
            {"context_window": 64_000},
        ),
        (_make_model(context_window=None), None, {"context_window": GLOBAL_CONTEXT_WINDOW_FLOOR}),
        (
            _make_model(recommended_temperature=1.0),
            _provider(defaults={"temperature": 0.7}),
            {"recommended_temperature": 1.0, "provider_default_temperature": 0.7},
        ),
    ],
    ids=["reasoning-ladder", "provider-window", "global-floor-window", "temperature-tiers"],
)
def test_status_model_details_resolve_through_the_model_and_provider(
    model: Model, provider: ProviderConfig | None, expected: dict[str, Any]
) -> None:
    details = resolve_status_model_details(
        _make_agent(model="provider/gpt-5.2"),
        cast(ModelRegistry, _AnyModels(model)),
        cast(Any, _Providers(provider)) if provider is not None else None,
    )

    assert {name: getattr(details, name) for name in expected} == expected


@pytest.mark.parametrize(
    ("agent_temperature", "recommended", "provider_default", "expected"),
    [
        (0.2, 1.0, 0.7, "0.2 (agent)"),
        (None, 1.0, 0.7, "0.7 (provider config)"),
        (None, 1.0, None, "provider default (Model recommends 1)"),
        (None, None, None, "provider default"),
    ],
)
def test_status_temperature_names_what_the_request_sends(
    agent_temperature: float | None,
    recommended: float | None,
    provider_default: float | None,
    expected: str,
) -> None:
    details = StatusModelDetails(
        context_window=None,
        display_name=None,
        recommended_temperature=recommended,
        provider_default_temperature=provider_default,
    )

    assert resolve_status_temperature(agent_temperature, details) == expected
