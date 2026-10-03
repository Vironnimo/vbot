"""Connection-bound Model configuration and Run override tests."""

import asyncio

from core.agents import LIBRARIAN_AGENT_ID, SKILL_AGENT_ID_KEY, skill_subject_id
from core.projects import AgentOverrides, AgentResolutionError, ModelConfigurationError
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
        address,
        {"model": "openai/gpt-mini", "thinking_effort": "high", "temperature": 1, "top_p": 0.9},
    )
    # A partial update keeps the other fields; None clears one.
    updated = resolver.update_session_overrides(address, {"temperature": None})

    resolved = resolver.resolve_agent(None, "identity", session_id=session.id)
    assert updated == AgentOverrides(model="openai/gpt-mini", thinking_effort="high", top_p=0.9)
    assert (resolved.model, resolved.thinking_effort, resolved.temperature, resolved.top_p) == (
        "openai/gpt-mini",
        "high",
        0.2,
        0.9,
    )
    assert sessions.metadata_value(address, "agent_overrides") == {
        "future": True,
        "model": "openai/gpt-mini",
        "thinking_effort": "high",
        "top_p": 0.9,
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


def test_a_librarian_session_runs_on_the_skills_of_its_bound_agent(
    agents: AgentStore, projects: ProjectStore
) -> None:
    agents.create("coder", model="openai/gpt-5.2", allowed_skills=["review"])
    librarian = agents.ensure_librarian()
    assert librarian is not None
    resolver = _resolver(agents, projects, _openai_configured())
    sessions = agents._session_manager()
    bound = SessionAddress(None, LIBRARIAN_AGENT_ID, sessions.create(LIBRARIAN_AGENT_ID).id)
    unbound = sessions.create(LIBRARIAN_AGENT_ID).id
    sessions.mutate_metadata(bound, lambda metadata: metadata.update({SKILL_AGENT_ID_KEY: "coder"}))

    for view in (
        resolver.resolve_agent(None, LIBRARIAN_AGENT_ID, session_id=bound.session_id),
        asyncio.run(
            resolver.resolve_agent_async(None, LIBRARIAN_AGENT_ID, session_id=bound.session_id)
        ),
    ):
        # The subject's Skills and Skill selection; everything else stays the Librarian's.
        assert (view.id, skill_subject_id(view), view.allowed_skills) == (
            LIBRARIAN_AGENT_ID,
            "coder",
            ["review"],
        )
        assert view.tool_access == librarian.tool_access
    # Outside a bound Session the Librarian works on its own Skills, and only the
    # Librarian's Sessions bind: another Agent's Session ignores the key.
    coder_session = sessions.create("coder").address
    sessions.mutate_metadata(
        coder_session, lambda metadata: metadata.update({SKILL_AGENT_ID_KEY: LIBRARIAN_AGENT_ID})
    )
    for view, owner in (
        (resolver.resolve_agent(None, LIBRARIAN_AGENT_ID, session_id=unbound), "librarian"),
        (resolver.resolve_agent(None, LIBRARIAN_AGENT_ID), "librarian"),
        (resolver.resolve_agent(None, "coder", session_id=coder_session.session_id), "coder"),
    ):
        assert skill_subject_id(view) == owner
        assert getattr(view, "skill_agent_id", None) is None
    # A Session whose Agent is gone, or names the Librarian itself, cannot run.
    for subject in ("ghost", LIBRARIAN_AGENT_ID):
        sessions.set_metadata(bound, {SKILL_AGENT_ID_KEY: subject})
        with pytest.raises(AgentResolutionError, match=f"Agent {subject}, which no longer"):
            resolver.resolve_agent(None, LIBRARIAN_AGENT_ID, session_id=bound.session_id)


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
