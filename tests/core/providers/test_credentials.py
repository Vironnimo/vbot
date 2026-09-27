"""ProviderCredentialResolver: Accounts, credential lookup, enablement and usability."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from core.providers.credentials import ProviderCredentialResolver
from core.providers.providers import (
    AuthConfig,
    ConnectionConfig,
    OAuthConfig,
    ProviderConfig,
    ProviderRegistry,
)
from core.providers.token_store import OAuthToken, TokenStore
from core.utils.errors import ConfigError

_BEARER = {"header": "Authorization", "prefix": "Bearer "}


def _registry() -> ProviderRegistry:
    """OpenAI with env-backed, token-store and env-backed OAuth-stub Connections; a keyless
    local Ollama beside a keyed cloud Connection."""

    openai = ProviderConfig(
        id="openai",
        name="OpenAI",
        adapter="openai_compatible",
        base_url="https://api.openai.com/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(**_BEARER, credential_key="OPENAI_API_KEY"),
            ),
            ConnectionConfig(
                id="subscription",
                type="oauth",
                label="Subscription",
                auth=AuthConfig(**_BEARER),
                oauth=OAuthConfig(
                    flow="device",
                    client_id="client-id",
                    device_auth_url="https://auth.openai.com/device",
                    token_url="https://auth.openai.com/token",
                    scopes=["openid"],
                ),
            ),
            ConnectionConfig(
                id="oauth-stub",
                type="oauth",
                label="OAuth Stub",
                auth=AuthConfig(**_BEARER, credential_key="OPENAI_STUB_KEY"),
            ),
        ],
    )
    ollama = ProviderConfig(
        id="ollama",
        name="Ollama",
        adapter="ollama",
        base_url="http://localhost:11434",
        connections=[
            ConnectionConfig(
                id="local",
                type="none",
                label="Local",
                auth=AuthConfig(header="", prefix="", credential_key=""),
            ),
            ConnectionConfig(
                id="cloud",
                type="api_key",
                label="Ollama Cloud",
                auth=AuthConfig(**_BEARER, credential_key="OLLAMA_API_KEY"),
            ),
        ],
    )
    return ProviderRegistry({"openai": openai, "ollama": ollama})


def _resolver(
    *,
    env: Mapping[str, str] | None = None,
    data_dir: Mapping[str, str] | None = None,
    overrides: dict[str, bool] | None = None,
    token_store: TokenStore | None = None,
) -> ProviderCredentialResolver:
    return ProviderCredentialResolver(
        _registry(),
        process_env=dict(env or {}),
        fallback_credentials=data_dir,
        token_store=token_store,
        enabled_overrides_loader=(lambda: overrides) if overrides is not None else None,
    )


# ---------------------------------------------------------------------------
# Accounts and credential lookup
# ---------------------------------------------------------------------------


def test_env_accounts_list_default_first_and_process_env_shadows_data_dir() -> None:
    resolver = _resolver(
        env={
            "OPENAI_API_KEY__ZETA": "zeta-secret",
            "OPENAI_API_KEY": "default-secret",
            "OPENAI_API_KEY__WORK": "",
        },
        data_dir={"OPENAI_API_KEY__WORK": "data-dir-secret", "OPENAI_API_KEY__ALPHA": "alpha"},
    )

    accounts = resolver.list_accounts("openai", "api-key")

    assert [(a.id, a.usable, a.source, a.credential_key) for a in accounts] == [
        ("default", True, "process_env", "OPENAI_API_KEY"),
        ("alpha", True, "data_dir", "OPENAI_API_KEY__ALPHA"),
        # An empty process value still shadows the data-dir credential.
        ("work", False, "process_env", "OPENAI_API_KEY__WORK"),
        ("zeta", True, "process_env", "OPENAI_API_KEY__ZETA"),
    ]


def test_token_store_and_keyless_connections_list_their_own_accounts(tmp_path: Path) -> None:
    token_store = TokenStore(tmp_path)
    token_store.save("openai", "subscription", OAuthToken(access_token="default-token"))
    token_store.save(
        "openai", "subscription", OAuthToken(access_token="work-token"), account_id="work"
    )
    resolver = _resolver(token_store=token_store)

    assert [
        (a.id, a.usable, a.source, a.credential_key)
        for a in resolver.list_accounts("openai", "subscription")
    ] == [("default", True, "oauth", ""), ("work", True, "oauth", "")]
    assert [
        (a.id, a.usable, a.source, a.credential_key)
        for a in resolver.list_accounts("ollama", "local")
    ] == [("default", True, "none", "")]
    with pytest.raises(ConfigError):
        resolver.list_accounts("openai", "missing")


@pytest.mark.parametrize(
    ("env", "connection_id", "expected"),
    [
        pytest.param(
            {"OPENAI_API_KEY": "default-secret", "OPENAI_API_KEY__WORK": "work-secret"},
            "openai:api-key:work",
            "work-secret",
            id="explicit-account",
        ),
        pytest.param(
            {"OPENAI_API_KEY__ALPHA": "alpha-secret", "OPENAI_API_KEY": "default-secret"},
            "openai:api-key",
            "default-secret",
            id="default-account-first",
        ),
        pytest.param(
            {"OPENAI_API_KEY__ZETA": "zeta-secret", "OPENAI_API_KEY__ALPHA": "alpha-secret"},
            "openai:api-key",
            "alpha-secret",
            id="first-sorted-account",
        ),
        pytest.param(
            {"OPENAI_STUB_KEY__WORK": "stub-work-secret"},
            "openai:oauth-stub",
            "stub-work-secret",
            id="oauth-stub-with-credential-key",
        ),
        pytest.param({}, "ollama:local", "", id="keyless"),
        pytest.param(
            {"OLLAMA_API_KEY": "sk-cloud"}, "ollama:cloud", "sk-cloud", id="keyed-sibling"
        ),
    ],
)
def test_get_credentials_resolves_the_exact_or_first_usable_account(
    env: dict[str, str], connection_id: str, expected: str
) -> None:
    provider_id = connection_id.split(":")[0]

    assert _resolver(env=env).get_credentials(provider_id, connection_id) == expected


def test_oauth_credentials_come_from_the_account_token_file(tmp_path: Path) -> None:
    token_store = TokenStore(tmp_path)
    token_store.save(
        "openai", "subscription", OAuthToken(access_token="work-token"), account_id="work"
    )
    resolver = _resolver(token_store=token_store)

    assert resolver.get_credentials("openai", "openai:subscription:work") == "work-token"
    assert resolver.get_credentials("openai", "openai:subscription") == "work-token"
    assert resolver.resolve_account_id("openai", "subscription") == "work"
    assert resolver.has_credentials("openai", "openai:subscription") is True
    assert resolver.has_credentials("openai", "openai:subscription:default") is False


@pytest.mark.parametrize(
    ("env", "connection_id", "named"),
    [
        pytest.param(
            {"OPENAI_API_KEY": "default-secret"}, "openai:api-key:work", "work", id="env-account"
        ),
        pytest.param({}, "openai:api-key", "OPENAI_API_KEY", id="env-connection"),
        pytest.param({}, "openai:subscription:work", "work", id="oauth-account"),
        pytest.param({}, "openai:subscription", "subscription", id="oauth-connection"),
    ],
)
def test_missing_credentials_name_the_slot_but_no_secret(
    tmp_path: Path, env: dict[str, str], connection_id: str, named: str
) -> None:
    resolver = _resolver(env=env, token_store=TokenStore(tmp_path))

    with pytest.raises(ConfigError, match=named) as error_info:
        resolver.get_credentials("openai", connection_id)

    assert "default-secret" not in str(error_info.value)


def test_has_credentials_checks_the_named_account_or_any_account() -> None:
    resolver = _resolver(env={"OPENAI_API_KEY__WORK": "work-secret"})

    assert resolver.has_credentials("openai", "openai:api-key:work") is True
    assert resolver.has_credentials("openai", "openai:api-key:other") is False
    assert resolver.has_credentials("openai", "openai:api-key:default") is False
    assert resolver.has_credentials("openai", "openai:api-key") is True
    assert resolver.has_credentials("openai") is True


@pytest.mark.parametrize(
    ("env", "connection", "account_id", "expected"),
    [
        pytest.param({"OPENAI_API_KEY__WORK": "w"}, "api-key", "work", "work", id="explicit"),
        pytest.param({}, "local", None, "default", id="keyless-implicit-default"),
        pytest.param({"OPENAI_API_KEY": "d"}, "api-key", "work", ConfigError, id="no-credential"),
        pytest.param({}, "api-key", "WORK", ConfigError, id="invalid-id"),
        pytest.param({}, "subscription", None, ConfigError, id="no-usable-account"),
    ],
)
def test_resolve_account_id(
    tmp_path: Path,
    env: dict[str, str],
    connection: str,
    account_id: str | None,
    expected: str | type[Exception],
) -> None:
    resolver = _resolver(env=env, token_store=TokenStore(tmp_path))
    provider_id = "ollama" if connection == "local" else "openai"

    if isinstance(expected, str):
        assert resolver.resolve_account_id(provider_id, connection, account_id) == expected
    else:
        with pytest.raises(expected):
            resolver.resolve_account_id(provider_id, connection, account_id)


def test_reloaded_data_dir_credentials_reach_the_injected_resolver() -> None:
    resolver = _resolver()
    assert resolver.is_usable("openai", "openai:api-key") is False

    resolver.reload_fallback_credentials({"OPENAI_API_KEY": "data-dir-secret"})

    assert resolver.is_usable("openai", "openai:api-key") is True
    assert resolver.get_credentials("openai", "openai:api-key") == "data-dir-secret"

    resolver.reload_fallback_credentials({})

    assert resolver.is_usable("openai", "openai:api-key") is False


def test_bundled_opencode_connections_share_key_slots_but_not_enablement() -> None:
    registry = ProviderRegistry.load(Path(__file__).resolve().parents[3] / "resources")
    resolver = ProviderCredentialResolver(
        registry,
        process_env={"OPENCODE_API_KEY__WORK": "process-test"},
        fallback_credentials={
            "OPENCODE_API_KEY": "default-test",
            "OPENCODE_API_KEY__WORK": "data-test",
        },
        enabled_overrides_loader=lambda: {"opencode-zen:api-key": False},
    )
    for provider in ("opencode-go", "opencode-zen"):
        assert resolver.get_credentials(provider, f"{provider}:api-key:default") == "default-test"
        assert resolver.get_credentials(provider, f"{provider}:api-key:work") == "process-test"
    assert resolver.is_usable("opencode-go", "opencode-go:api-key")
    assert not resolver.is_usable("opencode-zen", "opencode-zen:api-key")

    resolver.reload_fallback_credentials({"OPENCODE_API_KEY": "replacement-test"})
    for provider in ("opencode-go", "opencode-zen"):
        assert (
            resolver.get_credentials(provider, f"{provider}:api-key:default") == "replacement-test"
        )

    resolver.reload_fallback_credentials({})
    for provider in ("opencode-go", "opencode-zen"):
        assert not resolver.has_credentials(provider, f"{provider}:api-key:default")
        assert resolver.has_credentials(provider, f"{provider}:api-key:work")


# ---------------------------------------------------------------------------
# Enablement, enrollment and usability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "env", "connection_id", "enabled", "added", "credentialed", "usable"),
    [
        pytest.param(None, {}, "ollama:local", False, False, True, False, id="keyless-default"),
        pytest.param(
            {"ollama:local": True}, {}, "ollama:local", True, True, True, True, id="keyless-added"
        ),
        pytest.param(
            {"ollama:local": False},
            {},
            "ollama:local",
            False,
            True,
            True,
            False,
            id="keyless-added-then-disabled",
        ),
        pytest.param(None, {}, "openai:api-key", True, False, False, False, id="keyed-no-key"),
        pytest.param(
            None,
            {"OPENAI_API_KEY": "sk-test"},
            "openai:api-key",
            True,
            True,
            True,
            True,
            id="keyed-with-key",
        ),
        pytest.param(
            {"openai:api-key": False},
            {"OPENAI_API_KEY__WORK": "sk-test"},
            "openai:api-key:work",
            False,
            True,
            True,
            False,
            id="disabled-connection-ignores-account-and-key",
        ),
    ],
)
def test_connection_state_separates_enabled_added_credentialed_and_usable(
    overrides: dict[str, bool] | None,
    env: dict[str, str],
    connection_id: str,
    enabled: bool,
    added: bool,
    credentialed: bool,
    usable: bool,
) -> None:
    resolver = _resolver(env=env, overrides=overrides)
    provider_id = connection_id.split(":")[0]

    assert resolver.is_connection_enabled(provider_id, connection_id) is enabled
    assert resolver.is_connection_added(provider_id, connection_id) is added
    assert resolver.has_credentials(provider_id, connection_id) is credentialed
    assert resolver.is_usable(provider_id, connection_id) is usable


@pytest.mark.parametrize(
    ("overrides", "enabled", "added", "usable"),
    [
        # local: credentialed but disabled; cloud: enabled but keyless. Never usable together.
        pytest.param(None, True, False, False, id="defaults"),
        pytest.param({"ollama:cloud": False}, False, False, False, id="all-disabled"),
        pytest.param({"ollama:local": True}, True, True, True, id="one-complete-connection"),
    ],
)
def test_provider_level_state_is_decided_per_connection(
    overrides: dict[str, bool] | None, enabled: bool, added: bool, usable: bool
) -> None:
    resolver = _resolver(overrides=overrides)

    assert resolver.is_connection_enabled("ollama") is enabled
    assert resolver.is_connection_added("ollama") is added
    assert resolver.is_usable("ollama") is usable
    assert resolver.has_credentials("ollama") is True
