"""Connection-bound Model configuration tests."""

from core.projects import AgentRunOverrides, ModelConfigurationError

from .resolver_test_support import (
    AgentResolutionError,
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


def test_model_unconfigured_when_no_usable_connection(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # Arrange: model is in catalog and provider exists, but no usable connection.
    checker = _checker(
        catalog={("openai", "gpt-5.2")},
        providers={"openai": _FakeProviderConfig([_FakeConnection("api-key")])},
        usable=set(),
    )
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, checker, global_default="")

    # Act / Assert: declared model is not usable → chain falls through → error.
    with pytest.raises(AgentResolutionError):
        resolver.resolve_agent(project.project_id, "builder")


def test_connection_bound_model_unconfigured_without_allowed_credential() -> None:
    # A subscription-only model whose only credential is on the forbidden api-key
    # connection cannot run — the runtime would refuse the connection pick, so the
    # gate must refuse too (chain falls through instead of a hard run failure).
    checker = _two_connection_checker(usable={"openai:api-key"}, allowlist=("subscription",))
    assert checker.is_configured("openai/gpt-5.2") is False


def test_connection_bound_model_configured_on_allowed_credential() -> None:
    checker = _two_connection_checker(usable={"openai:subscription"}, allowlist=("subscription",))
    assert checker.is_configured("openai/gpt-5.2") is True


def test_pinned_connection_without_credential_is_unconfigured() -> None:
    # The pin is verbatim: a credential on another connection does not help.
    checker = _two_connection_checker(usable={"openai:api-key"}, allowlist=())
    assert checker.is_configured("openai/gpt-5.2::subscription") is False


def test_pinned_connection_with_credential_is_configured() -> None:
    checker = _two_connection_checker(usable={"openai:subscription"}, allowlist=())
    assert checker.is_configured("openai/gpt-5.2::subscription") is True


def test_pinned_account_suffix_checks_full_compositional_id() -> None:
    checker = _two_connection_checker(usable={"openai:subscription:work"}, allowlist=())
    assert checker.is_configured("openai/gpt-5.2::subscription:work") is True
    assert checker.is_configured("openai/gpt-5.2::subscription:home") is False


def test_pinned_unknown_connection_is_unconfigured() -> None:
    checker = _two_connection_checker(usable={"openai:ghost"}, allowlist=())
    assert checker.is_configured("openai/gpt-5.2::ghost") is False


def test_pinned_connection_forbidden_by_model_allowlist_is_unconfigured() -> None:
    checker = _two_connection_checker(
        usable={"openai:api-key", "openai:subscription"}, allowlist=("subscription",)
    )
    assert checker.is_configured("openai/gpt-5.2::api-key") is False


def test_require_configured_explains_forbidden_pinned_connection() -> None:
    checker = _two_connection_checker(
        usable={"openai:api-key", "openai:subscription"}, allowlist=("subscription",)
    )

    with pytest.raises(
        ModelConfigurationError,
        match="api-key.*subscription",
    ):
        checker.require_configured("openai/gpt-5.2::api-key")


def test_resolver_require_model_configured_uses_domain_validation_seam(
    agents: AgentStore, projects: ProjectStore
) -> None:
    resolver = _resolver(
        agents,
        projects,
        _two_connection_checker(usable={"openai:subscription"}, allowlist=("subscription",)),
    )

    resolver.require_model_configured("openai/gpt-5.2::subscription")
    with pytest.raises(ModelConfigurationError):
        resolver.require_model_configured("openai/ghost-model")


def test_identity_run_overrides_are_immutable_and_not_persisted(
    agents: AgentStore, projects: ProjectStore
) -> None:
    agents.create(
        "identity",
        model="openai/gpt-5.2",
        thinking_effort="low",
    )
    resolver = _resolver(agents, projects, _openai_configured())

    resolved = resolver.resolve_agent(
        None,
        "identity",
        run_overrides=AgentRunOverrides(
            model="openai/gpt-mini",
            thinking_effort="high",
        ),
    )

    assert resolved.model == "openai/gpt-mini"
    assert resolved.thinking_effort == "high"
    assert agents.get("identity").model == "openai/gpt-5.2"
    assert agents.get("identity").thinking_effort == "low"


def test_run_override_rejects_unusable_model(agents: AgentStore, projects: ProjectStore) -> None:
    agents.create("identity", model="openai/gpt-5.2")
    resolver = _resolver(agents, projects, _openai_configured())

    with pytest.raises(ModelConfigurationError):
        resolver.resolve_agent(
            None,
            "identity",
            run_overrides=AgentRunOverrides(model="openai/ghost-model"),
        )


def test_run_override_rejects_unknown_thinking_effort() -> None:
    with pytest.raises(ValueError):
        AgentRunOverrides(thinking_effort="extreme")


def test_empty_suffix_after_separator_is_unconfigured() -> None:
    checker = _two_connection_checker(usable={"openai:api-key"}, allowlist=())
    assert checker.is_configured("openai/gpt-5.2::") is False


def test_connection_bound_declared_model_falls_through_chain(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    # The declared model is allowlist-bound to a connection without credentials;
    # the chain must degrade to the configured project default instead of
    # resolving a model that would fail connection resolution at run time.
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

    assert resolved.model == "openai/gpt-mini"


def test_scan_reports_connection_bound_model_as_bad_model_finding(
    agents: AgentStore, projects: ProjectStore, repo: Path
) -> None:
    checker = _two_connection_checker(usable={"openai:api-key"}, allowlist=("subscription",))
    _write_agent(repo, "builder.md", model="openai/gpt-5.2")
    project = _project(projects, repo)
    resolver = _resolver(agents, projects, checker)

    result = resolver.scan_project_report(project)

    findings = result.report.findings_of(FindingType.BAD_MODEL)
    assert len(findings) == 1
    assert findings[0].agent_id == "builder"
