"""Tests for the shared provider task HTTP client plumbing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.providers.errors import NetworkError, ProviderAuthError, ProviderError
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.task_client import ProviderTaskClient, classify_task_response

_PROVIDER_BASE_URL = "https://provider.example/api/v1"
_CONNECTION_BASE_URL = "https://connection.example/api/v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(connection_base_url: str | None = None) -> ProviderConfig:
    connection = ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(
            header="Authorization",
            prefix="Bearer ",
            credential_key="EXAMPLE_API_KEY",
        ),
        base_url=connection_base_url,
    )
    return ProviderConfig(
        id="example",
        name="Example",
        adapter="openai_compatible",
        base_url=_PROVIDER_BASE_URL,
        connections=[connection],
        extra_headers={"X-Title": "vBot"},
    )


def _make_client(provider: ProviderConfig | None = None) -> ProviderTaskClient:
    resolved_provider = provider or _make_provider()
    return ProviderTaskClient(
        provider=resolved_provider,
        connection=resolved_provider.get_connection("api-key"),
        credential="sk-test",
        model_id="example/some-model",
    )


class _StubRuntime:
    """Minimal ``TaskClientRuntime`` stand-in for target resolution."""

    def __init__(self, provider: ProviderConfig) -> None:
        self.providers = SimpleNamespace(get=lambda provider_id: provider)
        self.provider_credentials = SimpleNamespace(
            get_credentials=lambda provider_id, connection_id=None: "sk-test"
        )


def _target_ref() -> SimpleNamespace:
    return SimpleNamespace(
        provider_id="example",
        model_id="example/some-model",
        connection_id="example:api-key",
        local_connection_id="api-key",
    )


# ---------------------------------------------------------------------------
# from_runtime — target resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_from_runtime_builds_client_bound_to_resolved_connection() -> None:
    """``from_runtime`` resolves provider, connection, and credential, and the
    resulting client posts with the connection's auth header plus the
    provider's extra headers."""

    route = respx.post(f"{_PROVIDER_BASE_URL}/things").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = ProviderTaskClient.from_runtime(_StubRuntime(_make_provider()), _target_ref())

    result = await client.post_and_parse(
        "/things",
        timeout=5.0,
        parse=lambda response: response.json(),
        json={"model": "example/some-model"},
    )

    assert result == {"ok": True}
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer sk-test"
    assert request.headers["x-title"] == "vBot"


@pytest.mark.asyncio
@respx.mock
async def test_connection_base_url_overrides_provider_base_url() -> None:
    """A connection-level ``base_url`` wins over the provider-level one."""

    route = respx.post(f"{_CONNECTION_BASE_URL}/things").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    provider = _make_provider(connection_base_url=_CONNECTION_BASE_URL)
    client = ProviderTaskClient.from_runtime(_StubRuntime(provider), _target_ref())

    await client.post_and_parse("/things", timeout=5.0, parse=lambda response: None)

    assert route.called


# ---------------------------------------------------------------------------
# post_and_parse — classification and retry semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_post_and_parse_raises_auth_error_with_body_detail() -> None:
    """A 401 surfaces as a non-retryable auth error carrying the body detail."""

    respx.post(f"{_PROVIDER_BASE_URL}/things").mock(
        return_value=httpx.Response(401, text="bad key")
    )
    client = _make_client()

    with pytest.raises(ProviderAuthError, match="401 bad key"):
        await client.post_and_parse("/things", timeout=5.0, parse=lambda response: None)


@pytest.mark.asyncio
@respx.mock
async def test_post_and_parse_does_not_retry_non_retryable_status() -> None:
    """A 400 is classified non-retryable and the request is not repeated."""

    route = respx.post(f"{_PROVIDER_BASE_URL}/things").mock(
        return_value=httpx.Response(400, text="bad request")
    )
    client = _make_client()

    with (
        patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(ProviderError) as exc_info,
    ):
        await client.post_and_parse("/things", timeout=5.0, parse=lambda response: None)

    assert exc_info.value.retryable is False
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_post_and_parse_retries_retryable_status_until_success() -> None:
    """A 503 is retried; the next successful response is parsed normally."""

    route = respx.post(f"{_PROVIDER_BASE_URL}/things")
    route.side_effect = [
        httpx.Response(503, text="overloaded"),
        httpx.Response(200, json={"ok": True}),
    ]
    client = _make_client()

    with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
        result = await client.post_and_parse(
            "/things", timeout=5.0, parse=lambda response: response.json()
        )

    assert result == {"ok": True}
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_post_and_parse_retries_retryable_parse_errors() -> None:
    """The parse callback runs inside the retry loop: a retryable
    ``ProviderError`` raised during parsing triggers a fresh request, the
    same way a transient HTTP failure does."""

    route = respx.post(f"{_PROVIDER_BASE_URL}/things").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    client = _make_client()
    attempts: list[int] = []

    def _parse(response: httpx.Response) -> dict[str, object]:
        attempts.append(1)
        if len(attempts) == 1:
            raise ProviderError("incomplete batch", retryable=True)
        return dict(response.json())

    with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
        result = await client.post_and_parse("/things", timeout=5.0, parse=_parse)

    assert result == {"ok": True}
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_post_and_parse_wraps_connect_errors_as_retryable_network_error() -> None:
    """A connect failure is wrapped as ``NetworkError`` and retried."""

    route = respx.post(f"{_PROVIDER_BASE_URL}/things")
    route.side_effect = [
        httpx.ConnectError("connection refused"),
        httpx.Response(200, json={"ok": True}),
    ]
    client = _make_client()

    with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
        result = await client.post_and_parse(
            "/things", timeout=5.0, parse=lambda response: response.json()
        )

    assert result == {"ok": True}
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_post_and_parse_raises_network_error_when_all_attempts_fail() -> None:
    """Exhausted connect retries surface the wrapped ``NetworkError``."""

    respx.post(f"{_PROVIDER_BASE_URL}/things").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    client = _make_client()

    with (
        patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(NetworkError),
    ):
        await client.post_and_parse("/things", timeout=5.0, parse=lambda response: None)


# ---------------------------------------------------------------------------
# classify_task_response
# ---------------------------------------------------------------------------


def test_classify_task_response_passes_success_silently() -> None:
    classify_task_response(httpx.Response(200, json={"ok": True}))


def test_classify_task_response_includes_status_and_body_detail() -> None:
    with pytest.raises(ProviderError, match="500 boom") as exc_info:
        classify_task_response(httpx.Response(500, text="boom"))

    assert exc_info.value.retryable is False


def test_classify_task_response_uses_bare_status_without_body() -> None:
    with pytest.raises(ProviderError, match="503") as exc_info:
        classify_task_response(httpx.Response(503))

    assert exc_info.value.retryable is True
