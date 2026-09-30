"""Connection-bound Model configuration and Run override tests."""

from core.projects import AgentOverrides, ModelConfigurationError
from core.sessions import SessionAddress

from .resolver_test_support import (
    AgentStore,
    FindingType,
    Path,
    ProjectStore,
    _checker,
    _FakeConnection,
    _FakeProviderConfig,
    _openai_configured,
    _project,
    _resolver,
    _two_connection_checker,
    _write_agent,
    pytest,
)
from .resolver_test_support import agents as agents
from .resolver_test_support import data_dir as data_dir
from .resolver_test_support import projects as projects
from .resolver_test_support import repo as repo
from .resolver_test_support import template_dir as template_dir


@pytest.mark.parametrize(
    ("usable", "allowlist", "model", "configured"),
    [
        pytest.param(set(), (), "openai/gpt-5.2", False, id="no-usable-connection"),
        # A subscription-only Model with its only credential on the forbidden api-key
        # Connection cannot run, so the gate refuses it too.
        pytest.param(
            {"openai:api-key"},
            ("subscription",),
            "openai/gpt-5.2",
            False,
            id="forbidden-credential",
        ),
        pytest.param(
            {"openai:subscription"},
            ("subscription",),
            "openai/gpt-5.2",
            True,
            id="allowed-credential",
        ),
        # A pin is verbatim: a credential on another Connection does not help.
        pytest.param(
            {"openai:api-key"},
            (),
            "openai/gpt-5.2::subscription",
            False,
            id="pinned-without-credential",
        ),
        pytest.param(
            {"openai:subscription"},
            (),
            "openai/gpt-5.2::subscription",
            True,
            id="pinned-with-credential",
        ),
        pytest.param(
            {"openai:subscription:work"},
            (),
            "openai/gpt-5.2::subscription:work",
            True,
            id="pinned-account",
        ),
        pytest.param(
            {"openai:subscription:work"},
            (),
            "openai/gpt-5.2::subscription:home",
            False,
            id="pinned-other-account",
        ),
        pytest.param({"openai:ghost"}, (), "openai/gpt-5.2::ghost", False, id="pinned-unknown"),
        pytest.param(
            {"openai:api-key", "openai:subscription"},
            ("subscription",),
            "openai/gpt-5.2::api-key",
            False,
            id="pinned-forbidden-by-allowlist",
        ),
        pytest.param({"openai:api-key"}, (), "openai/gpt-5.2::", False, id="empty-pin"),
    ],
)
def test_model_is_configured_only_on_an_allowed_usable_connection(
    usable: set[str], allowlist: tuple[str, ...], model: str, configured: bool
) -> None:
    checker = _two_connection_checker(usable=usable, allowlist=allowlist)

    assert checker.is_configured(model) is configured
    # The raising gate accepts exactly the Models the boolean gate accepts.
    if configured:
        checker.require_configured(model)
    else:
        with pytest.raises(ModelConfigurationError):
            checker.require_configured(model)


def test_require_configured_explains_forbidden_pinned_connection() -> None:
    checker = _two_connection_checker(
        usable={"openai:api-key", "openai:subscription"}, allowlist=("subscription",)
    )

    with pytest.raises(ModelConfigurationError, match="api-key.*subscription"):
        checker.require_configured("openai/gpt-5.2::api-key")


def test_resolver_model_gate_uses_the_domain_checker(
    agents: AgentStore, projects: ProjectStore
) -> None:
    # The /model command and the scan's BAD_MODEL check share this seam, so accepted
    # Models and clean-scan Models cannot drift.
    resolver = _resolver(agents, projects, _openai_configured())

    assert resolver.is_model_configured("openai/gpt-5.2") is True
    assert resolver.is_model_configured("openai/ghost-model") is False
    assert resolver.is_model_configured("") is False
    resolver.require_model_configured("openai/gpt-5.2")
    with pytest.raises(ModelConfigurationError):
        resolver.require_model_configured("openai/ghost-model")


def test_session_overrides_persist_in_the_session_and_never_in_the_agent(
    agents: AgentStore, projects: ProjectStore
) -> None:
    agents.create("identity", model="openai/gpt-5.2", thinking_effort="low", temperature=0.2)
    resolver = _resolver(agents, projects, _openai_configured())
    sessions = agents._session_manager()
    session = sessions.create("identity")
    other = sessions.create("identity")
    address = SessionAddress(None, "identity", session.id)
    # A field a newer vBot stored survives every update.
    sessions.mutate_metadata(
        address, lambda metadata: metadata.update(agent_overrides={"future": True})
    )

    resolver.update_session_overrides(
        address, {"model": "openai/gpt-mini", "thinking_effort": "high", "temperature": 1}
    )
    # A partial update keeps the other fields; None clears one.
    updated = resolver.update_session_overrides(address, {"temperature": None})

    resolved = resolver.resolve_agent(None, "identity", session_id=session.id)
    assert updated == AgentOverrides(model="openai/gpt-mini", thinking_effort="high")
    assert (resolved.model, resolved.thinking_effort, resolved.temperature) == (
        "openai/gpt-mini",
        "high",
        0.2,
    )
    assert sessions.metadata_value(address, "agent_overrides") == {
        "future": True,
        "model": "openai/gpt-mini",
        "thinking_effort": "high",
    }
    effective = resolver.effective_config(None, "identity", session_id=session.id)
    assert effective["model"] == {"value": "openai/gpt-mini", "source": "session"}
    assert effective["temperature"] == {"value": 0.2, "source": "agent"}
    # Other Sessions, the stored Agent and a Session that does not exist yet
    # keep the Agent's own values.
    for resolved_elsewhere in (
        resolver.resolve_agent(None, "identity", session_id=other.id),
        resolver.resolve_agent(None, "identity", session_id="ses_missing"),
        agents.get("identity"),
    ):
        assert resolved_elsewhere.model == "openai/gpt-5.2"
        assert resolved_elsewhere.thinking_effort == "low"


def test_session_overrides_reject_invalid_values_before_writing(
    agents: AgentStore, projects: ProjectStore
) -> None:
    agents.create("identity", model="openai/gpt-5.2")
    resolver = _resolver(agents, projects, _openai_configured())
    sessions = agents._session_manager()
    session = sessions.create("identity")
    address = SessionAddress(None, "identity", session.id)

    with pytest.raises(ModelConfigurationError):
        resolver.update_session_overrides(
            address, {"model": "openai/ghost-model", "thinking_effort": "high"}
        )
    for invalid in ({"thinking_effort": "extreme"}, {"temperature": 3}, {"unknown": "x"}):
        with pytest.raises(ValueError):
            resolver.update_session_overrides(address, invalid)
    assert sessions.metadata_value(address, "agent_overrides") is None

    # A stored Model that can no longer run fails the Session's resolution.
    sessions.mutate_metadata(
        address,
        lambda metadata: metadata.update(agent_overrides={"model": "openai/ghost-model"}),
    )
    with pytest.raises(ModelConfigurationError):
        resolver.resolve_agent(None, "identity", session_id=session.id)


def test_connection_bound_declared_model_falls_through_and_is_a_scan_finding(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # The declared Model is allowlist-bound to a Connection without credentials:
    # the chain degrades to the configured Project default instead of resolving a
    # Model that would fail Connection resolution at Run time.
    checker = _checker(
        catalog={("openai", "gpt-5.2"), ("openai", "gpt-mini")},
        providers={
            "openai": _FakeProviderConfig(
                [_FakeConnection("api-key"), _FakeConnection("subscription")]
            )
        },
        usable={"openai:api-key"},
        model_connections={("openai", "gpt-5.2"): ("subscription",)},
    )
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo, default_model="openai/gpt-mini")
    resolver = _resolver(agents, projects, checker)

    resolved = resolver.resolve_agent(project.project_id, "builder")
    findings = resolver.scan_project_report(project).report.findings_of(FindingType.BAD_MODEL)

    assert resolved.model == "openai/gpt-mini"
    assert [finding.agent_id for finding in findings] == ["builder"]
