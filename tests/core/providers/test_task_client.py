"""Tests for the shared provider task HTTP client plumbing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx

from core.providers.accounts import ConnectionRef
from core.providers.errors import (
    NetworkError,
    ProviderAuthError,
    ProviderError,
    ProviderOutcomeUnknownError,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.task_client import (
    NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
    ProviderTaskClient,
    TaskRequestRetryPolicy,
    classify_task_response,
)

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


def _make_keyless_client() -> ProviderTaskClient:
    connection = ConnectionConfig(
        id="default",
        type="none",
        label="Default",
        auth=AuthConfig(header="", prefix=""),
    )
    provider = ProviderConfig(
        id="local",
        name="Local",
        adapter="openai_compatible",
        base_url="http://127.0.0.1:8080/v1",
        connections=[connection],
        extra_headers={"X-Title": "vBot"},
    )
    return ProviderTaskClient(
        provider=provider,
        connection=connection,
        credential="",
        model_id="local/some-model",
    )


class _StubRuntime:
    """Minimal ``TaskClientRuntime`` stand-in for target resolution."""

    def __init__(self, provider: ProviderConfig, token: str = "sk-test") -> None:
        self.providers = SimpleNamespace(get=lambda provider_id: provider)
        self._token = token

    def get_connection_token_getter(self, connection: ConnectionRef):
        async def _get_token() -> str:
            return self._token

        return _get_token


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
async def test_keyless_connection_omits_auth_header_and_keeps_extra_headers() -> None:
    client = _make_keyless_client()

    headers = await client._headers()

    assert headers == {"X-Title": "vBot"}


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
async def test_post_and_parse_rebuilds_headers_inside_retry_loop() -> None:
    """Each retry asks the token getter again so OAuth refreshes can take effect."""

    route = respx.post(f"{_PROVIDER_BASE_URL}/things")
    route.side_effect = [
        httpx.Response(503, text="overloaded"),
        httpx.Response(200, json={"ok": True}),
    ]
    tokens = ["first-token", "second-token"]

    async def _get_token() -> str:
        return tokens.pop(0)

    provider = _make_provider()
    client = ProviderTaskClient(
        provider=provider,
        connection=provider.get_connection("api-key"),
        token_getter=_get_token,
        model_id="example/some-model",
    )

    with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
        result = await client.post_and_parse(
            "/things", timeout=5.0, parse=lambda response: response.json()
        )

    assert result == {"ok": True}
    assert [call.request.headers["authorization"] for call in route.calls] == [
        "Bearer first-token",
        "Bearer second-token",
    ]


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


@pytest.mark.asyncio
@respx.mock
async def test_non_idempotent_request_retries_failure_before_send() -> None:
    route = respx.post(f"{_PROVIDER_BASE_URL}/things")
    route.side_effect = [
        httpx.ConnectTimeout("connection timed out"),
        httpx.Response(200, json={"ok": True}),
    ]
    client = _make_client()

    with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
        result = await client.post_and_parse(
            "/things",
            timeout=5.0,
            parse=lambda response: response.json(),
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
        )

    assert result == {"ok": True}
    assert route.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_type",
    [httpx.ReadTimeout, httpx.WriteError, httpx.RemoteProtocolError],
)
@respx.mock
async def test_non_idempotent_request_does_not_retry_ambiguous_transport_failure(
    error_type: type[httpx.TransportError],
) -> None:
    route = respx.post(f"{_PROVIDER_BASE_URL}/things").mock(
        side_effect=error_type("ambiguous transport failure")
    )
    client = _make_client()

    with pytest.raises(ProviderOutcomeUnknownError) as exc_info:
        await client.post_and_parse(
            "/things",
            timeout=5.0,
            parse=lambda response: response.json(),
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
        )

    assert exc_info.value.retryable is False
    assert exc_info.value.operation_key
    assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [502, 504])
@respx.mock
async def test_non_idempotent_request_does_not_retry_ambiguous_gateway_status(
    status_code: int,
) -> None:
    route = respx.post(f"{_PROVIDER_BASE_URL}/things").mock(
        return_value=httpx.Response(status_code, text="gateway failure")
    )
    client = _make_client()

    with pytest.raises(ProviderOutcomeUnknownError) as exc_info:
        await client.post_and_parse(
            "/things",
            timeout=5.0,
            parse=lambda response: response.json(),
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
        )

    assert f"HTTP {status_code}" in str(exc_info.value)
    assert "idempotency-key" not in route.calls[0].request.headers
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_non_idempotent_request_retries_rate_limit() -> None:
    route = respx.post(f"{_PROVIDER_BASE_URL}/things")
    route.side_effect = [
        httpx.Response(429, text="rate limited"),
        httpx.Response(200, json={"ok": True}),
    ]
    client = _make_client()

    with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
        result = await client.post_and_parse(
            "/things",
            timeout=5.0,
            parse=lambda response: response.json(),
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
        )

    assert result == {"ok": True}
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_non_idempotent_request_only_retries_verified_status() -> None:
    route = respx.post(f"{_PROVIDER_BASE_URL}/things")
    route.side_effect = [
        httpx.Response(503, text="not processed"),
        httpx.Response(200, json={"ok": True}),
    ]
    client = _make_client()
    policy = TaskRequestRetryPolicy(
        replay_safe=False,
        verified_safe_retry_status_codes=frozenset({503}),
    )

    with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
        result = await client.post_and_parse(
            "/things",
            timeout=5.0,
            parse=lambda response: response.json(),
            retry_policy=policy,
        )

    assert result == {"ok": True}
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_non_idempotent_request_does_not_retry_unverified_503() -> None:
    route = respx.post(f"{_PROVIDER_BASE_URL}/things").mock(
        return_value=httpx.Response(503, text="ambiguous")
    )
    client = _make_client()

    with pytest.raises(ProviderOutcomeUnknownError):
        await client.post_and_parse(
            "/things",
            timeout=5.0,
            parse=lambda response: response.json(),
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
        )

    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_non_idempotent_request_does_not_retry_broken_success_response() -> None:
    route = respx.post(f"{_PROVIDER_BASE_URL}/things").mock(
        return_value=httpx.Response(200, json={"incomplete": True})
    )
    client = _make_client()

    def _parse(_response: httpx.Response) -> None:
        raise ProviderError("result is incomplete", retryable=True)

    with pytest.raises(ProviderOutcomeUnknownError):
        await client.post_and_parse(
            "/things",
            timeout=5.0,
            parse=_parse,
            retry_policy=NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY,
        )

    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_idempotency_header_reuses_one_operation_key_across_retries() -> None:
    route = respx.post(f"{_PROVIDER_BASE_URL}/things")
    route.side_effect = [
        httpx.Response(503, text="overloaded"),
        httpx.Response(200, json={"ok": True}),
    ]
    client = _make_client()
    policy = TaskRequestRetryPolicy(
        replay_safe=False,
        idempotency_header_name="Idempotency-Key",
    )

    with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
        result = await client.post_and_parse(
            "/things",
            timeout=5.0,
            parse=lambda response: response.json(),
            retry_policy=policy,
        )

    operation_keys = [call.request.headers["idempotency-key"] for call in route.calls]
    assert result == {"ok": True}
    assert len(set(operation_keys)) == 1
    assert operation_keys[0]


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


def test_classify_task_response_get_500_is_retryable_when_idempotent() -> None:
    with pytest.raises(ProviderError, match="500 boom") as exc_info:
        classify_task_response(httpx.Response(500, text="boom"), idempotent=True)

    assert exc_info.value.retryable is True


@pytest.mark.asyncio
@respx.mock
async def test_get_and_parse_retries_transient_500_until_success() -> None:
    """A status poll / result download GET is replay-safe: a 500 retries."""

    route = respx.get(f"{_PROVIDER_BASE_URL}/jobs/abc")
    route.side_effect = [
        httpx.Response(500, text="transient"),
        httpx.Response(200, json={"status": "done"}),
    ]
    client = _make_client()

    with patch("core.utils.retry.asyncio.sleep", new_callable=AsyncMock):
        result = await client.get_and_parse(
            "/jobs/abc",
            timeout=5.0,
            parse=lambda response: response.json(),
        )

    assert result == {"status": "done"}
    assert route.call_count == 2
