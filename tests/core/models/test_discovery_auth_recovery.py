"""Tests for discovery auth recovery."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from core.providers.errors import ProviderAuthError, ProviderError
from core.providers.providers import OAuthConfig
from core.providers.token_getter import OAuthTokenGetter
from core.providers.token_store import OAuthToken, TokenStore

from .discovery_test_support import (
    _SIMPLE_MODELS_URL,
    FIXTURES_DIR,
    OPENAI_SUBSCRIPTION_MODELS_URL,
    AuthConfig,
    ConnectionConfig,
    ModelDiscoveryError,
    ModelRegistry,
    Path,
    ProviderConfig,
    _simple_compatible_config,
    httpx,
    json,
    jwt_with_openai_account,
    mock_openai_codex_package,
    pytest,
    refresh_models,
    respx,
)
from .discovery_test_support import _clear_registry_cache as _clear_registry_cache
from .discovery_test_support import github_copilot_config as github_copilot_config
from .discovery_test_support import (
    openai_subscription_connection_config as openai_subscription_connection_config,
)
from .discovery_test_support import opencode_go_config as opencode_go_config
from .discovery_test_support import openrouter_config as openrouter_config


class TestRefreshModels:
    @respx.mock
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "adapter", ["xai", "nous", "minimax", "github_copilot", "openai_compatible"]
    )
    async def test_catalog_recovery_is_available_to_every_oauth_adapter(
        self,
        tmp_path: Path,
        adapter: str,
    ) -> None:
        config = _simple_compatible_config()
        connection = replace(config.connections[0], type="oauth")
        config = replace(config, adapter=adapter, connections=[connection])

        class Getter:
            token = "old-test-token"
            refresh_count = 0

            async def __call__(self) -> str:
                return self.token

            async def refresh_after_rejection(
                self, rejected: str, *, status_code: int, response_body: str
            ) -> str | None:
                if status_code != 401:
                    return None
                assert rejected == "old-test-token"
                self.refresh_count += 1
                self.token = "new-test-token"
                return self.token

        getter = Getter()
        raw_model = (
            json.loads(
                (FIXTURES_DIR / "github_copilot_models_raw.json").read_text(encoding="utf-8")
            )["data"][0]
            if adapter == "github_copilot"
            else {"id": "test-model"}
        )
        route = respx.get(_SIMPLE_MODELS_URL).mock(
            side_effect=[
                httpx.Response(401),
                httpx.Response(200, json={"data": [raw_model]}),
            ]
        )
        result = await refresh_models(
            config, getter, tmp_path / "resources", credential_connection=connection
        )
        assert result["provider_id"] == config.id
        assert getter.refresh_count == 1
        assert route.call_count == 2
        assert route.calls.last.request.headers["Authorization"] == "Bearer new-test-token"

    @respx.mock
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("initial_status", "retry_status", "expected_catalog_calls", "expected_refresh_calls"),
        [(401, 200, 2, 1), (401, 401, 2, 1), (401, 403, 2, 1), (403, 200, 1, 0)],
    )
    async def test_openai_catalog_recovers_rejected_token_once(
        self,
        tmp_path: Path,
        openai_subscription_connection_config: ProviderConfig,
        initial_status: int,
        retry_status: int,
        expected_catalog_calls: int,
        expected_refresh_calls: int,
    ) -> None:
        config = openai_subscription_connection_config
        connection = config.get_connection("subscription")
        old_token = jwt_with_openai_account("old-test-account")
        new_token = jwt_with_openai_account("new-test-account")
        store = TokenStore(tmp_path / "data")
        store.save(
            "openai",
            "subscription",
            OAuthToken(
                access_token=old_token,
                refresh_token="test-refresh-secret",
                expires_at=datetime.now(UTC) + timedelta(days=1),
            ),
        )
        oauth = OAuthConfig(
            flow="device",
            device_flow="openai_codex",
            client_id="test-client",
            device_auth_url="https://auth.openai.com/device",
            token_url="https://auth.openai.com/oauth/token",
            scopes=[],
        )
        refresh_route = None
        if expected_refresh_calls:
            refresh_route = respx.post(oauth.token_url).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "access_token": new_token,
                        "refresh_token": "rotated-test-refresh-secret",
                        "expires_in": 3600,
                    },
                )
            )
        mock_openai_codex_package()
        catalog_route = respx.get(OPENAI_SUBSCRIPTION_MODELS_URL).mock(
            side_effect=[
                httpx.Response(initial_status, json={"error": {"code": "token_expired"}}),
                httpx.Response(
                    retry_status,
                    json={"models": [{"slug": "test-model", "display_name": "Test Model"}]},
                ),
            ]
        )
        resources_dir = tmp_path / "resources"
        async with OAuthTokenGetter(store, "openai", "subscription", oauth) as getter:
            if initial_status == 401 and retry_status == 200:
                result = await refresh_models(
                    config, getter, resources_dir, credential_connection=connection
                )
                assert result["model_count"] == 1
                assert ModelRegistry.load(resources_dir).get("openai", "test-model")
            else:
                with pytest.raises(ModelDiscoveryError) as caught:
                    await refresh_models(
                        config, getter, resources_dir, credential_connection=connection
                    )
                assert isinstance(caught.value.__cause__, ProviderAuthError)
                assert not (resources_dir / "models" / "openai.json").exists()

        assert catalog_route.call_count == expected_catalog_calls
        assert (refresh_route.call_count if refresh_route else 0) == expected_refresh_calls
        first_headers = catalog_route.calls[0].request.headers
        assert first_headers["Authorization"] == f"Bearer {old_token}"
        assert first_headers["chatgpt-account-id"] == "old-test-account"
        if expected_refresh_calls:
            retry_headers = catalog_route.calls[1].request.headers
            assert retry_headers["Authorization"] == f"Bearer {new_token}"
            assert retry_headers["chatgpt-account-id"] == "new-test-account"
            saved = store.load("openai", "subscription")
            assert saved is not None
            assert saved.access_token == new_token
            assert saved.refresh_token == "rotated-test-refresh-secret"

    @respx.mock
    @pytest.mark.asyncio
    @pytest.mark.parametrize("retryable_refresh_error", [False, True])
    async def test_openai_catalog_stops_when_token_refresh_fails(
        self,
        tmp_path: Path,
        openai_subscription_connection_config: ProviderConfig,
        retryable_refresh_error: bool,
    ) -> None:
        config = openai_subscription_connection_config
        mock_openai_codex_package()
        catalog_route = respx.get(OPENAI_SUBSCRIPTION_MODELS_URL).mock(
            return_value=httpx.Response(401)
        )
        failure = (
            ProviderError("test-owned-refresh-outage", retryable=True)
            if retryable_refresh_error
            else ProviderAuthError("test-owned-refresh-rejection")
        )

        class FailingGetter:
            refresh_calls = 0

            async def __call__(self) -> str:
                return jwt_with_openai_account()

            async def refresh_after_rejection(
                self, rejected_token: str, *, status_code: int, response_body: str
            ) -> str | None:
                if status_code != 401:
                    return None
                assert rejected_token == jwt_with_openai_account()
                self.refresh_calls += 1
                raise failure

        getter = FailingGetter()
        with pytest.raises(ModelDiscoveryError) as caught:
            await refresh_models(
                config,
                getter,
                tmp_path / "resources",
                credential_connection=config.get_connection("subscription"),
            )
        assert caught.value.__cause__ is failure
        assert catalog_route.call_count == 1
        assert getter.refresh_calls == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_catalog_does_not_recover_token_getter_failure(
        self, tmp_path: Path, openai_subscription_connection_config: ProviderConfig
    ) -> None:
        config = openai_subscription_connection_config
        mock_openai_codex_package()
        failure = ProviderAuthError("test-owned-getter-rejection")
        failure.status_code = 401

        class Getter:
            async def __call__(self) -> str:
                raise failure

            async def refresh_after_rejection(
                self, _rejected_token: str, *, status_code: int, response_body: str
            ) -> str | None:
                if status_code != 401:
                    return None
                raise AssertionError("Only a rejected catalog request permits recovery")

        with pytest.raises(ModelDiscoveryError) as caught:
            await refresh_models(
                config,
                Getter(),
                tmp_path / "resources",
                credential_connection=config.get_connection("subscription"),
            )
        assert caught.value.__cause__ is failure

    @respx.mock
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("adapter", "connection_type", "mode", "static_credential"),
        [
            ("openai", "api_key", "codex_responses", False),
            ("openai", "oauth", "codex_responses", True),
        ],
    )
    async def test_catalog_does_not_refresh_unauthorized_other_connections(
        self,
        tmp_path: Path,
        adapter: str,
        connection_type: str,
        mode: str | None,
        static_credential: bool,
    ) -> None:
        config = _simple_compatible_config()
        connection = replace(config.connections[0], type=connection_type, mode=mode)
        config = replace(config, adapter=adapter, connections=[connection])
        if adapter == "openai":
            mock_openai_codex_package()
        route = respx.get(_SIMPLE_MODELS_URL).mock(return_value=httpx.Response(401))

        class Getter:
            async def __call__(self) -> str:
                return jwt_with_openai_account()

            async def refresh_after_rejection(
                self, _rejected_token: str, *, status_code: int, response_body: str
            ) -> str | None:
                if status_code != 401:
                    return None
                raise AssertionError("This Connection must not refresh on a catalog 401")

        with pytest.raises(ModelDiscoveryError):
            await refresh_models(
                config,
                jwt_with_openai_account() if static_credential else Getter(),
                tmp_path / "resources",
                credential_connection=connection,
            )
        assert route.call_count == 1

    @respx.mock
    @pytest.mark.asyncio
    async def test_catalog_rebuilds_auth_headers_on_transient_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def no_sleep(_delay: float) -> None:
            pass

        tokens = iter(("first-test-token", "second-test-token"))

        async def getter() -> str:
            return next(tokens)

        monkeypatch.setattr("core.utils.retry.asyncio.sleep", no_sleep)
        route = respx.get(_SIMPLE_MODELS_URL).mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, json={"data": [{"id": "test-model"}]}),
            ]
        )
        await refresh_models(_simple_compatible_config(), getter, tmp_path / "resources")
        assert [call.request.headers["Authorization"] for call in route.calls] == [
            "Bearer first-test-token",
            "Bearer second-test-token",
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_refresh_models_xai_subscription_uses_plain_oauth_discovery(
        self,
        tmp_path: Path,
    ) -> None:
        """xAI discovery must not run OpenAI Codex account routing.

        The xAI subscription OAuth token carries no ``chatgpt_account_id``
        claim, so the inherited OpenAI Subscription ``discovery_headers``
        would abort the refresh. xAI's ``/language-models`` catalog needs
        only the plain Bearer header and no ``client_version`` query.
        """

        resources_dir = tmp_path / "resources"
        config = ProviderConfig(
            id="xai",
            name="xAI",
            adapter="xai",
            base_url="https://api.x.ai/v1",
            connections=[
                ConnectionConfig(
                    id="api-key",
                    type="api_key",
                    label="API Key",
                    auth=AuthConfig(
                        header="Authorization",
                        prefix="Bearer ",
                        credential_key="XAI_API_KEY",
                    ),
                    models_endpoint="/language-models",
                ),
                ConnectionConfig(
                    id="subscription",
                    type="oauth",
                    label="SuperGrok Login (Subscription)",
                    auth=AuthConfig(header="Authorization", prefix="Bearer "),
                    models_endpoint="/language-models",
                ),
            ],
            defaults={"max_tokens": 8192},
            models_dev_id="xai",
        )
        access_token = "xai-subscription-token-without-chatgpt-claim"
        models_url = "https://api.x.ai/v1/language-models"
        route = respx.get(models_url).mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "grok-4.5",
                            "name": "Grok 4.5",
                            "input_modalities": ["text", "image"],
                            "output_modalities": ["text"],
                            "context_window": 500000,
                        }
                    ]
                },
            )
        )

        result = await refresh_models(
            config,
            access_token,
            resources_dir,
            credential_connection=config.connections[1],
        )

        request = route.calls.last.request
        assert request.headers["Authorization"] == f"Bearer {access_token}"
        assert "chatgpt-account-id" not in request.headers
        assert "client_version" not in request.url.params
        catalog_data = json.loads(
            (resources_dir / "models" / "xai.json").read_text(encoding="utf-8")
        )
        assert catalog_data["models"]["grok-4.5"]["connections"] == ["subscription"]
        assert result["provider_id"] == "xai"
        assert result["model_count"] == 1
