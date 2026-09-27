"""Providers: the Provider registry (parsing, validation, lookup, caching, Custom Providers)."""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from core.providers.providers import (
    AuthConfig,
    ConnectionConfig,
    OAuthConfig,
    ProviderConfig,
    ProviderRegistry,
)
from core.utils.errors import ConfigError

# A field set to ``_DROP`` is left out of the written JSON.
_DROP: Any = object()


def _without_dropped(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not _DROP}


def _connection(**changes: Any) -> dict[str, Any]:
    return _without_dropped(
        {
            "id": "api-key",
            "type": "api_key",
            "label": "API Key",
            "auth": {
                "header": "Authorization",
                "prefix": "Bearer ",
                "credential_key": "MINIMAL_API_KEY",
            },
            **changes,
        }
    )


def _oauth(**changes: Any) -> dict[str, Any]:
    return {
        "flow": "device",
        "client_id": "client-id",
        "device_auth_url": "https://auth.example.test/device/code",
        "token_url": "https://auth.example.test/oauth/token",
        "scopes": ["openid"],
        **changes,
    }


def _provider(**changes: Any) -> dict[str, Any]:
    return _without_dropped(
        {
            "id": "minimal",
            "name": "Minimal",
            "adapter": "openai_compatible",
            "base_url": "https://minimal.example.test/v1",
            "connections": [_connection()],
            **changes,
        }
    )


def _write_providers(resources: Path, *documents: Any) -> Path:
    providers_dir = resources / "providers"
    providers_dir.mkdir(parents=True, exist_ok=True)
    for index, document in enumerate(documents):
        (providers_dir / f"provider-{index}.json").write_text(
            json.dumps(document), encoding="utf-8"
        )
    return resources


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_EVERY_OPTIONAL_FIELD = {
    "id": "gateway",
    "name": "Gateway",
    "adapter": "openai",
    "base_url": "https://gateway.example.test/v1",
    "connections": [
        {
            "id": "subscription",
            "type": "oauth",
            "label": "Subscription",
            "auth": {"header": "Authorization", "prefix": "Bearer "},
            "base_url": "https://chatgpt.com/backend-api",
            "mode": "codex_responses",
            "models_endpoint": "/codex/models",
            "oauth": _oauth(
                device_flow="openai_codex",
                token_exchange_url="https://auth.example.test/oauth/exchange",
                verification_uri="https://auth.example.test/codex/device",
                redirect_uri="https://auth.example.test/deviceauth/callback",
                expires_in=600,
            ),
        },
        {
            "id": "api-key",
            "type": "api_key",
            "label": "API Key",
            "auth": {"header": "x-api-key", "prefix": "", "credential_key": "GATEWAY_API_KEY"},
            "catalog_requires_credentials": False,
        },
        # Keyless Connections need no auth block; an empty one parses leniently.
        {"id": "local", "type": "none", "label": "Local", "auto_refresh": True},
        {"id": "lan", "type": "none", "label": "LAN", "auth": {}},
    ],
    "defaults": {"max_tokens": 4096, "temperature": 0.7},
    "extra_headers": {"HTTP-Referer": "https://vbot.app", "X-Title": "vBot"},
    "models_endpoint": "/models",
    "models_dev_id": "gateway-dev",
    "context_window": 128_000,
    "catalog_exclusions": ["broken-preview", "legacy-model"],
}

_KEYLESS_AUTH = AuthConfig(header="", prefix="", credential_key="")


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        pytest.param(
            _EVERY_OPTIONAL_FIELD,
            ProviderConfig(
                id="gateway",
                name="Gateway",
                adapter="openai",
                base_url="https://gateway.example.test/v1",
                connections=[
                    ConnectionConfig(
                        id="subscription",
                        type="oauth",
                        label="Subscription",
                        auth=AuthConfig(header="Authorization", prefix="Bearer "),
                        base_url="https://chatgpt.com/backend-api",
                        oauth=OAuthConfig(
                            flow="device",
                            client_id="client-id",
                            device_auth_url="https://auth.example.test/device/code",
                            token_url="https://auth.example.test/oauth/token",
                            scopes=["openid"],
                            token_exchange_url="https://auth.example.test/oauth/exchange",
                            device_flow="openai_codex",
                            verification_uri="https://auth.example.test/codex/device",
                            redirect_uri="https://auth.example.test/deviceauth/callback",
                            expires_in=600,
                        ),
                        mode="codex_responses",
                        models_endpoint="/codex/models",
                    ),
                    ConnectionConfig(
                        id="api-key",
                        type="api_key",
                        label="API Key",
                        auth=AuthConfig(
                            header="x-api-key", prefix="", credential_key="GATEWAY_API_KEY"
                        ),
                        catalog_requires_credentials=False,
                    ),
                    ConnectionConfig(
                        id="local",
                        type="none",
                        label="Local",
                        auth=_KEYLESS_AUTH,
                        auto_refresh=True,
                    ),
                    ConnectionConfig(id="lan", type="none", label="LAN", auth=_KEYLESS_AUTH),
                ],
                defaults={"max_tokens": 4096, "temperature": 0.7},
                extra_headers={"HTTP-Referer": "https://vbot.app", "X-Title": "vBot"},
                models_endpoint="/models",
                models_dev_id="gateway-dev",
                context_window=128_000,
                catalog_exclusions=frozenset({"broken-preview", "legacy-model"}),
            ),
            id="every-optional-field",
        ),
        # Absent optional fields parse to the documented "unset" values.
        pytest.param(
            _provider(),
            ProviderConfig(
                id="minimal",
                name="Minimal",
                adapter="openai_compatible",
                base_url="https://minimal.example.test/v1",
                connections=[
                    ConnectionConfig(
                        id="api-key",
                        type="api_key",
                        label="API Key",
                        auth=AuthConfig(
                            header="Authorization",
                            prefix="Bearer ",
                            credential_key="MINIMAL_API_KEY",
                        ),
                        base_url=None,
                        oauth=None,
                        mode=None,
                        models_endpoint=None,
                        auto_refresh=False,
                        catalog_requires_credentials=True,
                    )
                ],
                defaults=None,
                extra_headers=None,
                models_endpoint=None,
                models_dev_id=None,
                context_window=None,
                catalog_exclusions=frozenset(),
                custom=False,
            ),
            id="required-fields-only",
        ),
    ],
)
def test_load_parses_bundled_provider_json(
    tmp_path: Path, document: dict[str, Any], expected: ProviderConfig
) -> None:
    registry = ProviderRegistry.load(_write_providers(tmp_path, document))

    assert registry.get(expected.id) == expected


def test_direct_construction_leaves_optional_facts_unset() -> None:
    config = ProviderConfig(
        id="opencode-go",
        name="OpenCode Go",
        adapter="opencode_go",
        base_url="https://example.test/v1",
    )
    connection = ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(header="Authorization", prefix="Bearer "),
    )

    assert (
        config.connections,
        config.models_dev_id,
        config.context_window,
        config.catalog_exclusions,
        config.custom,
    ) == ([], None, None, frozenset(), False)
    assert (
        connection.mode,
        connection.models_endpoint,
        connection.auto_refresh,
        connection.catalog_requires_credentials,
    ) == (None, None, False, True)
    # The models.dev key falls back to the vBot Provider id.
    assert config.effective_models_dev_id() == "opencode-go"
    assert dataclasses.replace(config, models_dev_id="opencode").effective_models_dev_id() == (
        "opencode"
    )


@pytest.mark.parametrize(
    ("instance", "field_name"),
    [
        pytest.param(
            ProviderConfig(id="p", name="P", adapter="openai_compatible", base_url="https://p"),
            "id",
            id="provider",
        ),
        pytest.param(
            ConnectionConfig(id="c", type="none", label="C", auth=_KEYLESS_AUTH),
            "label",
            id="connection",
        ),
        pytest.param(_KEYLESS_AUTH, "credential_key", id="auth"),
    ],
)
def test_parsed_configs_are_immutable(instance: object, field_name: str) -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(instance, field_name, "changed")


def test_auth_config_carries_only_credential_centric_fields() -> None:
    assert [field.name for field in dataclasses.fields(AuthConfig)] == [
        "header",
        "prefix",
        "credential_key",
    ]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("documents", "error"),
    [
        pytest.param([[]], ConfigError, id="root-not-an-object"),
        pytest.param([_provider(id="open:ai")], ConfigError, id="provider-id-with-colon"),
        pytest.param([_provider(), _provider()], KeyError, id="duplicate-provider-id"),
        pytest.param([_provider(connections=_DROP)], ConfigError, id="missing-connections"),
        # '--' and ':' would break token filenames and Connection id parsing.
        pytest.param(
            [_provider(connections=[_connection(id="api--key")])],
            ConfigError,
            id="connection-id-with-double-dash",
        ),
        pytest.param(
            [_provider(connections=[_connection(id="api:key")])],
            ConfigError,
            id="connection-id-with-colon",
        ),
        pytest.param(
            [_provider(connections=[_connection(), _connection()])],
            KeyError,
            id="duplicate-connection-id",
        ),
        pytest.param(
            [_provider(connections=[_connection(type="bearer")])],
            ConfigError,
            id="unknown-connection-type",
        ),
        pytest.param(
            [_provider(connections=[_connection(auth=_DROP)])],
            ConfigError,
            id="keyed-connection-without-auth",
        ),
        pytest.param(
            [
                _provider(
                    connections=[_connection(auth={"header": "Authorization", "prefix": "Bearer "})]
                )
            ],
            ConfigError,
            id="api-key-without-credential-key",
        ),
        pytest.param(
            [
                _provider(
                    connections=[_connection(type="oauth", oauth=_oauth(flow="authorization_code"))]
                )
            ],
            ConfigError,
            id="unknown-oauth-flow",
        ),
        pytest.param(
            [
                _provider(
                    connections=[_connection(type="oauth", oauth=_oauth(device_flow="unknown"))]
                )
            ],
            ConfigError,
            id="unknown-oauth-device-flow",
        ),
        pytest.param(
            [_provider(connections=[_connection(type="oauth", oauth=_oauth(expires_in=0))])],
            ConfigError,
            id="non-positive-oauth-expiry",
        ),
        pytest.param(
            [_provider(connections=[_connection(mode=42)])], ConfigError, id="non-string-mode"
        ),
        pytest.param(
            [_provider(connections=[_connection(models_endpoint=["not", "a", "string"])])],
            ConfigError,
            id="non-string-connection-models-endpoint",
        ),
        pytest.param(
            [_provider(connections=[_connection(auto_refresh="yes")])],
            ConfigError,
            id="non-boolean-auto-refresh",
        ),
        pytest.param(
            [_provider(connections=[_connection(catalog_requires_credentials="no")])],
            ConfigError,
            id="non-boolean-catalog-requires-credentials",
        ),
        pytest.param(
            [_provider(models_dev_id=["not", "a", "string"])],
            ConfigError,
            id="non-string-models-dev-id",
        ),
        pytest.param([_provider(context_window=0)], ConfigError, id="non-positive-context-window"),
        pytest.param([_provider(context_window=1.5)], ConfigError, id="non-int-context-window"),
        pytest.param([_provider(context_window=True)], ConfigError, id="boolean-context-window"),
        pytest.param(
            [_provider(catalog_exclusions="broken-preview")],
            ConfigError,
            id="catalog-exclusions-not-a-list",
        ),
        pytest.param(
            [_provider(catalog_exclusions=[""])], ConfigError, id="empty-catalog-exclusion"
        ),
        pytest.param(
            [_provider(catalog_exclusions=[123])], ConfigError, id="non-string-catalog-exclusion"
        ),
    ],
)
def test_load_rejects_invalid_provider_json(
    tmp_path: Path, documents: list[Any], error: type[Exception]
) -> None:
    with pytest.raises(error):
        ProviderRegistry.load(_write_providers(tmp_path, *documents))


def test_several_connections_may_share_a_type(tmp_path: Path) -> None:
    document = _provider(
        connections=[_connection(id="primary-key"), _connection(id="secondary-key")]
    )

    config = ProviderRegistry.load(_write_providers(tmp_path, document)).get("minimal")

    assert [(connection.id, connection.type) for connection in config.connections] == [
        ("primary-key", "api_key"),
        ("secondary-key", "api_key"),
    ]


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def test_lookup_lists_sorted_ids_and_names_what_is_available_on_a_miss(tmp_path: Path) -> None:
    registry = ProviderRegistry.load(_write_providers(tmp_path, _provider(), _EVERY_OPTIONAL_FIELD))
    config = registry.get("gateway")

    assert registry.list_ids() == ["gateway", "minimal"]
    assert config.get_connection("api-key") is config.connections[1]
    with pytest.raises(KeyError, match="gateway, minimal"):
        registry.get("no-such-provider")
    with pytest.raises(KeyError, match="subscription, api-key, local, lan"):
        config.get_connection("missing")


@pytest.mark.parametrize("create_directory", [True, False], ids=["empty", "missing"])
def test_a_resources_directory_without_provider_json_is_an_empty_registry(
    tmp_path: Path, create_directory: bool
) -> None:
    if create_directory:
        (tmp_path / "providers").mkdir()

    assert ProviderRegistry.load(tmp_path).list_ids() == []


# ---------------------------------------------------------------------------
# Caching and tolerant loading
# ---------------------------------------------------------------------------


def test_bundled_registry_is_cached_per_resources_directory(tmp_path: Path) -> None:
    resources = _write_providers(tmp_path / "resources", _provider(), _EVERY_OPTIONAL_FIELD)
    other = _write_providers(tmp_path / "other")

    first = ProviderRegistry.load(resources)
    (resources / "providers" / "provider-0.json").unlink()
    second = ProviderRegistry.load(resources)

    assert second is first
    assert second.list_ids() == ["gateway", "minimal"]
    assert ProviderRegistry.load(other) is not first


def test_tolerant_load_skips_invalid_configs_and_keeps_valid_ones(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    resources = _write_providers(tmp_path, _provider())
    corrupt_path = resources / "providers" / "corrupt.json"
    corrupt_path.write_text('{"id":', encoding="utf-8")
    shadowing = {
        "name": "Shadow",
        "adapter": "openai_compatible",
        "base_url": "https://shadow.example/v1",
        "auth": "none",
    }

    with caplog.at_level(logging.WARNING, logger="vbot.providers"):
        registry = ProviderRegistry.load(
            resources, custom_providers={"minimal": shadowing}, tolerate_invalid=True
        )

    assert registry.list_ids() == ["minimal"]
    assert registry.get("minimal").custom is False
    assert str(corrupt_path) in caplog.text
    assert "Ignoring invalid Custom Provider 'minimal'" in caplog.text


def test_tolerant_load_survives_provider_directory_scan_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()

    def fail_scan(*_args: object, **_kwargs: object) -> list[Path]:
        raise OSError("scan failed")

    monkeypatch.setattr(Path, "glob", fail_scan)

    with caplog.at_level(logging.WARNING, logger="vbot.providers"):
        registry = ProviderRegistry.load(tmp_path, custom_providers={}, tolerate_invalid=True)

    assert registry.list_ids() == []
    assert str(providers_dir) in caplog.text


def test_tolerant_load_does_not_populate_strict_registry_cache(tmp_path: Path) -> None:
    providers_dir = tmp_path / "providers"
    providers_dir.mkdir()
    providers_dir.joinpath("corrupt.json").write_text('{"id":', encoding="utf-8")

    assert ProviderRegistry.load(tmp_path, tolerate_invalid=True).list_ids() == []
    with pytest.raises(json.JSONDecodeError):
        ProviderRegistry.load(tmp_path)


# ---------------------------------------------------------------------------
# Custom Providers
# ---------------------------------------------------------------------------


def _custom(auth: str, **changes: Any) -> dict[str, Any]:
    return {
        "name": "Custom",
        "adapter": "openai_compatible",
        "base_url": "http://127.0.0.1:8080/v1",
        "auth": auth,
        "models_endpoint": None,
        "defaults": {},
        "models": {},
        **changes,
    }


@pytest.mark.parametrize(
    ("provider_id", "custom", "connection"),
    [
        pytest.param(
            "local-ai",
            _custom("none", models_endpoint="/models"),
            ConnectionConfig(id="default", type="none", label="Default", auth=_KEYLESS_AUTH),
            id="keyless",
        ),
        # The credential key is derived from the normalized Custom Provider id.
        pytest.param(
            "my-gateway",
            _custom("api_key"),
            ConnectionConfig(
                id="default",
                type="api_key",
                label="Default",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key="VBOT_CUSTOM_MY_GATEWAY_API_KEY",
                ),
            ),
            id="api-key",
        ),
    ],
)
def test_custom_provider_materializes_one_default_connection(
    tmp_path: Path, provider_id: str, custom: dict[str, Any], connection: ConnectionConfig
) -> None:
    registry = ProviderRegistry.load(tmp_path, custom_providers={provider_id: custom})

    assert registry.get(provider_id) == ProviderConfig(
        id=provider_id,
        name="Custom",
        adapter="openai_compatible",
        base_url="http://127.0.0.1:8080/v1",
        connections=[connection],
        defaults={},
        models_endpoint=custom["models_endpoint"],
        custom=True,
    )


def test_reload_replaces_custom_providers_in_place(tmp_path: Path) -> None:
    registry = ProviderRegistry.load(tmp_path, custom_providers={})
    held_reference = registry

    registry.reload(tmp_path, custom_providers={"gateway": _custom("api_key")})

    assert held_reference.list_ids() == ["gateway"]


def test_custom_provider_registries_are_isolated_per_runtime_data(tmp_path: Path) -> None:
    first = ProviderRegistry.load(tmp_path, custom_providers={"first": _custom("none")})
    second = ProviderRegistry.load(tmp_path, custom_providers={"second": _custom("none")})
    bundled_only = ProviderRegistry.load(tmp_path)

    assert first.list_ids() == ["first"]
    assert second.list_ids() == ["second"]
    assert bundled_only.list_ids() == []


def test_custom_provider_cannot_shadow_bundled_provider(tmp_path: Path) -> None:
    resources = _write_providers(tmp_path, _provider())

    with pytest.raises(ConfigError):
        ProviderRegistry.load(resources, custom_providers={"minimal": _custom("none")})
