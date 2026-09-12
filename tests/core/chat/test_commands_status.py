"""Tests for commands status."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from core.chat import (
    ChatMessage,
    CommandDispatcher,
)
from core.chat.status_report import (
    STATUS_PLACEHOLDER,
    ReasoningIntent,
    StatusModelDetails,
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
from core.providers.providers import ProviderConfig
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


def _make_model(
    *,
    model_id: str = "gpt-5.2",
    name: str = "GPT-5.2",
    recommended_temperature: float | None = None,
) -> Model:
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
        recommended_temperature=recommended_temperature,
    )


class _StubSession:
    def __init__(self, messages: list[ChatMessage]) -> None:
        self._messages = messages

    def load(self) -> list[ChatMessage]:
        return list(self._messages)

    def status_snapshot(self) -> Any:
        return status_session_facts(self._messages)


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

    def get(self, _address: SessionAddress) -> _StubSession:
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


def test_dispatch_status_with_no_deps_returns_degraded_reply() -> None:
    dispatcher = CommandDispatcher(ChatRunManager())

    result = _execute_sync(dispatcher, "/status")

    assert result.feedback is not None
    reply = result.feedback.text
    assert reply != ""
    assert f"Agent: {STATUS_PLACEHOLDER}" in reply
    assert "Activity: idle" in reply
    assert f"Run created at: {STATUS_PLACEHOLDER}" in reply
    assert f"Run updated at: {STATUS_PLACEHOLDER}" in reply
    assert f"Last request cache: {STATUS_PLACEHOLDER}" in reply
    assert f"Session cache: {STATUS_PLACEHOLDER}" in reply
    assert "Current time:" in reply


def test_dispatch_status_with_full_deps_returns_reply_with_expected_fields() -> None:
    session_started = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
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
    dispatcher = CommandDispatcher(
        ChatRunManager(),
        agent_resolver=cast(AgentResolver, _StubResolver(_make_agent())),
        sessions=cast(ChatSessionManager, _StubSessions(messages)),
        models=cast(ModelRegistry, _StubModels(_make_model())),
        started_at=datetime(2026, 5, 18, 9, 0, tzinfo=UTC),
    )

    result = _execute_sync(dispatcher, "/status")

    assert result.feedback is not None
    reply = result.feedback.text
    assert "Agent: Coder (openai/gpt-5.2)" in reply
    assert "Model display name: GPT-5.2" in reply
    assert "Temperature: 0.3 (agent)" in reply
    assert "Activity: idle" in reply
    assert f"Run created at: {STATUS_PLACEHOLDER}" in reply
    assert f"Run updated at: {STATUS_PLACEHOLDER}" in reply
    assert "Context usage: 1234 / 200000" in reply
    assert "Last request cache: read 800 / 1234 (64.8% hit), write 100" in reply
    assert "Session cache: read 800 / 1234 (64.8% hit), write 100, turns 1" in reply
    assert "Current time:" in reply


@pytest.mark.asyncio
async def test_dispatch_status_reports_active_run_timestamps() -> None:
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
    dispatcher = CommandDispatcher(
        manager,
        agent_resolver=cast(AgentResolver, _StubResolver(_make_agent())),
        sessions=cast(ChatSessionManager, _StubSessions([])),
        models=cast(ModelRegistry, _StubModels(_make_model())),
    )

    result = await _execute(dispatcher, "/status")
    expected_updated_at = run.updated_at
    release.set()
    await run.wait()

    assert result.feedback is not None
    assert "Activity: running" in result.feedback.text
    assert f"Run created at: {run.created_at}" in result.feedback.text
    assert f"Run updated at: {expected_updated_at}" in result.feedback.text


def test_dispatch_status_reports_resolved_model_recommended_temperature() -> None:
    dispatcher = CommandDispatcher(
        ChatRunManager(),
        agent_resolver=cast(AgentResolver, _StubResolver(_make_agent(temperature=None))),
        sessions=cast(ChatSessionManager, _StubSessions([])),
        models=cast(ModelRegistry, _StubModels(_make_model(recommended_temperature=1.0))),
    )

    result = _execute_sync(dispatcher, "/status")

    assert result.feedback is not None
    assert "Temperature: 1 (model recommendation)" in result.feedback.text


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

    result = _execute_sync(dispatcher, "/status")

    assert result.feedback is not None
    assert "Model display name: GPT-5.2 Registry" in result.feedback.text
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

    result = _execute_sync(dispatcher, "/status", agent_id="builder", project_id="vbot")

    assert result.feedback is not None
    # The project session resolves through the run-path seam instead of degrading
    # to an empty reply, and the resolver sees the session's project id.
    assert resolver.calls == [("vbot", "builder")]
    assert "Agent: Coder (openai/gpt-5.2)" in result.feedback.text
    assert "Project: vBot (vbot)" in result.feedback.text


def test_dispatch_status_identity_session_shows_project_placeholder() -> None:
    resolver = _StubResolver(_make_agent())
    dispatcher = CommandDispatcher(
        ChatRunManager(),
        agent_resolver=cast(AgentResolver, resolver),
        sessions=cast(ChatSessionManager, _StubSessions([])),
        models=cast(ModelRegistry, _StubModels(_make_model())),
        projects=cast(ProjectStore, _StubProjects(_StubProject("vbot", "vBot"))),
    )

    result = _execute_sync(dispatcher, "/status")

    assert result.feedback is not None
    assert resolver.calls == [(None, "coder")]
    assert f"Project: {STATUS_PLACEHOLDER}" in result.feedback.text


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
    assert f"Fallback models: {STATUS_PLACEHOLDER}" in text
    assert f"Selected thinking effort: {STATUS_PLACEHOLDER}" in text
    assert f"Actual model thinking effort: {STATUS_PLACEHOLDER}" in text
    assert f"Temperature: {STATUS_PLACEHOLDER}" in text
    assert f"Context usage: {STATUS_PLACEHOLDER}" in text
    assert f"Last request cache: {STATUS_PLACEHOLDER}" in text
    assert f"Session cache: {STATUS_PLACEHOLDER}" in text
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
    assert "Fallback models: openai/gpt-5.1" in text
    assert "Selected thinking effort: none" in text
    assert f"Actual model thinking effort: {STATUS_PLACEHOLDER}" in text
    assert "Temperature: 0.3" in text
    assert f"Activity: {STATUS_PLACEHOLDER}" in text
    assert f"Run created at: {STATUS_PLACEHOLDER}" in text
    assert f"Run updated at: {STATUS_PLACEHOLDER}" in text
    assert "Context usage: ~987 / 200000" in text
    assert f"Last request cache: {STATUS_PLACEHOLDER}" in text
    assert f"Session cache: {STATUS_PLACEHOLDER}" in text
    assert "Session started:" in text
    assert "Turn count: 1" in text
    assert "App uptime:" in text
    assert "Current time:" in text


def test_build_status_text_reports_latest_and_session_cache() -> None:
    session_started = datetime(2026, 5, 18, 10, 0, tzinfo=UTC)
    messages = [
        ChatMessage.user("Status check", timestamp=session_started),
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="First answer.",
            usage={
                "input_tokens": 1000,
                "output_tokens": 12,
                "cache_read_tokens": 800,
                "cache_write_tokens": 100,
            },
            timestamp=session_started,
        ),
        ChatMessage.assistant(
            model="openai/gpt-5.2",
            content="Second answer.",
            usage={"input_tokens": 500, "output_tokens": 8, "cache_read_tokens": 200},
            timestamp=session_started,
        ),
    ]

    text = build_status_text(
        _make_agent(),
        messages,
        context_window=200_000,
        started_at=datetime(2026, 5, 18, 9, 0, tzinfo=UTC),
    )

    assert "Context usage: 500 / 200000" in text
    assert "Last request cache: read 200 / 500 (40.0% hit), write 0" in text
    assert "Session cache: read 1000 / 1500 (66.7% hit), write 100, turns 2" in text


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


def _on_off_details() -> StatusModelDetails:
    return StatusModelDetails(
        context_window=1_048_576,
        display_name="glm-5.3-flash",
        reasoning_levels=(),
        reasoning_control="on_off",
    )


def test_resolve_reported_thinking_effort_prefers_adapter_description() -> None:
    """The adapter's render description wins over the declared-control fallback.

    This is the ollama-cloud glm-5.3-flash case the user hit: the catalog
    declares a binary on_off control, but the Cloud wire carries the effort
    level — so /status must report ``max``, not ``on``.
    """

    def describe_render(provider_id: str, model_id: str, effort: str | None):
        assert provider_id == "ollama-cloud"
        assert model_id == "glm-5.3-flash"
        return ReasoningIntent("effort", effort_level="max")

    text_value = resolve_reported_thinking_effort(
        agent=_make_agent(model="ollama-cloud/glm-5.3-flash", thinking_effort="xhigh"),
        models=cast(ModelRegistry, object()),
        model_details=_on_off_details(),
        describe_render=describe_render,
    )

    assert text_value == "max"


def test_resolve_reported_thinking_effort_falls_back_without_describer() -> None:
    text_value = resolve_reported_thinking_effort(
        agent=_make_agent(model="ollama-cloud/glm-5.3-flash", thinking_effort="xhigh"),
        models=cast(ModelRegistry, object()),
        model_details=_on_off_details(),
        describe_render=None,
    )

    assert text_value == "on"


def test_resolve_reported_thinking_effort_falls_back_when_unresolvable() -> None:
    text_value = resolve_reported_thinking_effort(
        agent=_make_agent(model="ollama-cloud/glm-5.3-flash", thinking_effort="xhigh"),
        models=cast(ModelRegistry, object()),
        model_details=_on_off_details(),
        describe_render=lambda *_args: None,
    )

    assert text_value == "on"


def test_resolve_reported_thinking_effort_falls_back_on_describer_error() -> None:
    def broken_describer(*_args: Any) -> ReasoningIntent:
        raise KeyError("provider missing")

    text_value = resolve_reported_thinking_effort(
        agent=_make_agent(model="ollama-cloud/glm-5.3-flash", thinking_effort="xhigh"),
        models=cast(ModelRegistry, object()),
        model_details=_on_off_details(),
        describe_render=broken_describer,
    )

    assert text_value == "on"


def test_resolve_reported_thinking_effort_none_without_agent() -> None:
    assert (
        resolve_reported_thinking_effort(
            agent=None,
            models=None,
            model_details=_on_off_details(),
        )
        is None
    )


def test_resolve_reported_thinking_effort_reports_off_from_description() -> None:
    text_value = resolve_reported_thinking_effort(
        agent=_make_agent(model="ollama-cloud/glm-5.3-flash", thinking_effort="none"),
        models=cast(ModelRegistry, object()),
        model_details=_on_off_details(),
        describe_render=lambda *_args: ReasoningIntent("off"),
    )

    assert text_value == "off"


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


def test_resolve_status_model_details_surfaces_temperature_tiers() -> None:
    """The model recommendation and the provider default feed the status line."""
    model = _make_model(recommended_temperature=1.0)

    class _Models:
        def get(self, _provider_id: str, _model_id: str) -> Model:
            return model

    class _Providers:
        def get(self, _provider_id: str) -> Any:
            return ProviderConfig(
                id="warm",
                name="Warm",
                adapter="openai_compatible",
                base_url="https://example.test/v1",
                defaults={"temperature": 0.7},
            )

    details = resolve_status_model_details(
        _make_agent(model="warm/gpt-5.2"),
        cast(ModelRegistry, _Models()),
        cast(Any, _Providers()),
    )

    assert details.recommended_temperature == 1.0
    assert details.provider_default_temperature == 0.7


def test_resolve_status_temperature_reports_tier_sources() -> None:
    """Each tier of the resolution chain renders its value with its source."""
    details = StatusModelDetails(
        context_window=None,
        display_name=None,
        recommended_temperature=1.0,
        provider_default_temperature=0.7,
    )

    assert resolve_status_temperature(0.2, details) == "0.2 (agent)"
    assert resolve_status_temperature(None, details) == "1 (model recommendation)"

    provider_only = StatusModelDetails(
        context_window=None,
        display_name=None,
        provider_default_temperature=0.7,
    )
    assert resolve_status_temperature(None, provider_only) == "0.7 (provider default)"

    empty = StatusModelDetails(context_window=None, display_name=None)
    assert resolve_status_temperature(None, empty) == "default"


def test_build_status_text_renders_resolved_temperature_status() -> None:
    text = build_status_text(
        _make_agent(temperature=None),
        messages=[],
        context_window=None,
        started_at=None,
        temperature_status="1 (model recommendation)",
    )

    assert "Temperature: 1 (model recommendation)" in text
