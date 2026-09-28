"""xAI: Model-scoped Responses policy and OAuth token-rejection recovery."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core.models.discovery import ModelDiscoveryError, refresh_models
from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.errors import ProviderAuthError
from core.providers.providers import (
    AuthConfig,
    ConnectionConfig,
    ProviderConfig,
    ProviderRegistry,
)
from core.providers.task_client import ProviderTaskClient
from core.providers.token_getter import OAuthTokenGetter, TokenGetter
from core.providers.token_store import OAuthToken, TokenStore
from core.providers.xai import XAIAdapter

RESOURCES = Path(__file__).resolve().parents[3] / "resources"
XAI_RESPONSES_URL = "https://api.x.ai/v1/responses"
HELLO = [{"role": "user", "content": "Hello"}]
SUCCESS_RESPONSE = {
    "id": "response-id",
    "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
}
ENCRYPTED_REASONING = ["reasoning.encrypted_content"]
ABSENT = object()


def _model(
    model_id: str,
    *,
    reasoning: bool,
    levels: tuple[str, ...] = (),
    tools: bool = True,
    supported_parameters: tuple[str, ...] | None = None,
) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=True,
            tools=tools,
            json_mode=True,
            reasoning=ReasoningCapabilities(
                supported=reasoning,
                control="levels" if levels else None,
                levels=levels,
            ),
            input_modalities=("text", "image"),
            output_modalities=("text",),
            supported_parameters=(
                supported_parameters
                if supported_parameters is not None
                else (
                    "max_output_tokens",
                    "parallel_tool_calls",
                    "prompt_cache_key",
                    "reasoning_effort",
                    "response_format",
                    "service_tier",
                    "temperature",
                    "tools",
                    "top_p",
                )
            ),
        ),
        context_window=500000,
        max_output_tokens=30000,
    )


MODELS = {
    "grok-4.5": _model("grok-4.5", reasoning=True, levels=("low", "medium", "high")),
    "grok-4.3": _model("grok-4.3", reasoning=True, levels=("none", "low", "medium", "high")),
    "grok-4.20-0309-reasoning": _model("grok-4.20-0309-reasoning", reasoning=True),
    "grok-4.20-0309-non-reasoning": _model("grok-4.20-0309-non-reasoning", reasoning=False),
    "grok-4.20-multi-agent-0309": _model(
        "grok-4.20-multi-agent-0309",
        reasoning=True,
        levels=("low", "medium", "high", "xhigh"),
        tools=False,
        supported_parameters=(
            "prompt_cache_key",
            "reasoning_effort",
            "response_format",
            "service_tier",
            "temperature",
            "top_p",
        ),
    ),
}


@pytest.fixture()
def xai_adapter() -> XAIAdapter:
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
            )
        ],
        defaults={"max_tokens": 8192},
    )
    return XAIAdapter(config, "xai-secret", model_lookup=MODELS.get)


async def _sent_body(
    adapter: XAIAdapter, messages: list[dict[str, Any]], **kwargs: Any
) -> dict[str, Any]:
    with respx.mock:
        route = respx.post(XAI_RESPONSES_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        await adapter.send(messages, **kwargs)
    body: dict[str, Any] = json.loads(route.calls.last.request.content)
    assert route.calls.last.request.headers["authorization"] == "Bearer xai-secret"
    assert body["store"] is False
    return body


@pytest.mark.asyncio
async def test_request_carries_cache_affinity_tier_and_model_output_limit(
    xai_adapter: XAIAdapter,
) -> None:
    body = await _sent_body(
        xai_adapter,
        HELLO,
        model_id="grok-4.5",
        prompt_cache_key="cache-affinity",
        service_tier="priority",
    )

    assert body["prompt_cache_key"] == "cache-affinity"
    assert body["service_tier"] == "priority"
    assert body["max_output_tokens"] == 30000


@pytest.mark.parametrize(
    ("model_id", "request_kwargs", "expected"),
    [
        pytest.param(
            "grok-4.5",
            {"thinking_effort": "none"},
            {"reasoning": {"effort": "low", "summary": "auto"}, "include": ENCRYPTED_REASONING},
            id="grok-4.5-cannot-disable-and-maps-none-to-low",
        ),
        pytest.param(
            "grok-4.3",
            {"thinking_effort": "none"},
            {"reasoning": ABSENT, "include": ENCRYPTED_REASONING},
            id="grok-4.3-can-disable-reasoning",
        ),
        pytest.param(
            "grok-4.20-0309-reasoning",
            {"thinking_effort": "high"},
            {"reasoning": ABSENT, "include": ENCRYPTED_REASONING},
            id="fixed-reasoning-omits-effort-but-keeps-encrypted-replay",
        ),
        pytest.param(
            "grok-4.20-0309-non-reasoning",
            {"thinking_effort": "high", "include_reasoning": True, "service_tier": "untrusted"},
            {"reasoning": ABSENT, "include": ABSENT, "service_tier": ABSENT},
            id="non-reasoning-strips-reasoning-and-unknown-tier",
        ),
        pytest.param(
            "grok-4.20-multi-agent-0309",
            {
                "thinking_effort": "xhigh",
                "tools": [{"type": "function", "function": {"name": "lookup"}}],
            },
            {
                "reasoning": {"effort": "xhigh", "summary": "auto"},
                "tools": ABSENT,
                "max_output_tokens": ABSENT,
            },
            id="multi-agent-keeps-xhigh-and-drops-client-tools-and-limits",
        ),
    ],
)
@pytest.mark.asyncio
async def test_reasoning_and_request_fields_follow_the_model_profile(
    xai_adapter: XAIAdapter,
    model_id: str,
    request_kwargs: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    body = await _sent_body(xai_adapter, HELLO, model_id=model_id, **request_kwargs)

    for key, value in expected.items():
        if value is ABSENT:
            assert key not in body, key
        else:
            assert body[key] == value, key


@pytest.mark.asyncio
async def test_output_limit_budgets_against_wire_items_not_persisted_meta(
    xai_adapter: XAIAdapter,
) -> None:
    """A long reasoning Session must not fail the local context clamp.

    The persisted ``reasoning_meta`` carries redundant copies of the reasoning
    items (``response_output``, ``reasoning_items``, ``encrypted_content``) that
    never reach the wire. The output-limit clamp must budget against the actual
    Responses input items, so a Session that still fits the context window keeps
    working instead of raising "leaves no output capacity".
    """

    reasoning_item = {"type": "reasoning", "id": "rs_1", "encrypted_content": "opaque"}
    messages: list[dict[str, Any]] = []
    for index in range(40):
        messages.append(
            {
                "role": "assistant",
                "content": f"Step {index}.",
                "reasoning_meta": {
                    "response_output": [dict(reasoning_item)],
                    "reasoning_items": [dict(reasoning_item)],
                    "encrypted_content": ["opaque"],
                },
                "tool_calls": [{"id": f"call_{index}", "name": "search", "arguments": {}}],
            }
        )
        messages.append(
            {"role": "tool", "tool_call_id": f"call_{index}", "name": "search", "content": "result"}
        )
    messages.append({"role": "user", "content": "Continue"})

    body = await _sent_body(xai_adapter, messages, model_id="grok-4.5")

    assert body["max_output_tokens"] == 30000


def test_request_context_uses_shared_prompt_cache_affinity(xai_adapter: XAIAdapter) -> None:
    assert xai_adapter.request_context_kwargs(
        agent_id="agent",
        session_id="session",
        prompt_cache_affinity_id="shared-prefix",
    ) == {"prompt_cache_key": "shared-prefix"}


def test_wire_accepts_only_jpeg_and_png_images(xai_adapter: XAIAdapter) -> None:
    assert xai_adapter.wire_media_support("grok-4.5") == {"image/jpeg", "image/png"}


@pytest.mark.asyncio
async def test_discovery_keeps_the_connection_header_and_fetches_no_extra_metadata() -> None:
    config = ProviderConfig(
        id="xai", name="xAI", adapter="xai", base_url="https://api.x.ai/v1", connections=[]
    )

    async def fetch_json(url: str) -> object:
        raise AssertionError("xAI discovery must not fetch external metadata")

    assert XAIAdapter.discovery_headers(
        config, "xai-access-token", {"Authorization": "Bearer xai-access-token"}
    ) == {"Authorization": "Bearer xai-access-token"}
    assert XAIAdapter.discovery_params() == {}
    assert await XAIAdapter.resolve_discovery_params(fetch_json) == {}


# ---------------------------------------------------------------------------
# OAuth token-rejection recovery through every HTTP consumer
# ---------------------------------------------------------------------------

TOKEN_REJECTION = {
    "code": "unauthenticated:bad-credentials",
    "error": "The OAuth2 access token could not be validated.",
}
NON_RENEWING_BODIES = {
    "permission": {"code": "permission_denied", "error": "test permission failure"},
    # The vocabulary counts only as the top-level code, never inside a message.
    "unstructured": {"error": "unauthenticated:bad-credentials"},
}
CONSUMERS = ("send", "stream", "catalog", "task_get", "task_post")


def _consumer_route(
    consumer: str, provider: ProviderConfig, connection: ConnectionConfig
) -> tuple[str, str, httpx.Response]:
    """Return the method, endpoint and successful reply of one HTTP consumer."""

    if consumer == "catalog":
        endpoint = connection.models_endpoint or provider.models_endpoint
        return "GET", str(endpoint), httpx.Response(200, json={"models": [{"id": "grok-4.5"}]})
    if consumer.startswith("task_"):
        method = "GET" if consumer == "task_get" else "POST"
        return method, "/test-task", httpx.Response(200, json={"ok": True})
    if consumer == "stream":
        completed = {
            "type": "response.completed",
            "response": {**SUCCESS_RESPONSE, "status": "completed"},
        }
        return (
            "POST",
            "/responses",
            httpx.Response(
                200,
                text=f"data: {json.dumps(completed)}\n\n",
                headers={"Content-Type": "text/event-stream"},
            ),
        )
    return "POST", "/responses", httpx.Response(200, json=SUCCESS_RESPONSE)


async def _invoke(
    consumer: str,
    provider: ProviderConfig,
    connection: ConnectionConfig,
    credential: TokenGetter | str,
    resources_dir: Path,
) -> Any:
    if consumer == "catalog":
        return await refresh_models(
            provider, credential, resources_dir, credential_connection=connection
        )
    if consumer.startswith("task_"):
        client = ProviderTaskClient(
            provider=provider,
            connection=connection,
            model_id="test-model",
            credential=credential if isinstance(credential, str) else None,
            token_getter=None if isinstance(credential, str) else credential,
        )
        if consumer == "task_get":
            return await client.get_and_parse(
                "/test-task", timeout=1, parse=lambda response: response.json()
            )
        return await client.post_and_parse(
            "/test-task", timeout=1, parse=lambda response: response.json(), json={"input": "x"}
        )
    adapter = XAIAdapter(
        provider,
        credential,
        base_url=connection.base_url or provider.base_url,
        auth_config=connection.auth,
        model_lookup=lambda model_id: _model(model_id, reasoning=False),
    )
    try:
        if consumer == "stream":
            return [delta async for delta in adapter.stream(HELLO, model_id="grok-4.5")]
        return await adapter.send(HELLO, model_id="grok-4.5")
    finally:
        await adapter.aclose()


# Each consumer records its own HTTP responses for the shared recovery, so every
# consumer proves that the exact rejection body reaches the xAI getter and the
# request replays with the rotated token. The shared getter and recovery decide
# every refusal, so each refusal runs once, spread across the consumers.
@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("consumer", "outcome"),
    [
        *(pytest.param(consumer, "success", id=f"success-{consumer}") for consumer in CONSUMERS),
        pytest.param("stream", "rejected_again", id="rejected_again-stream"),
        pytest.param("catalog", "permission", id="permission-catalog"),
        pytest.param("task_get", "unstructured", id="unstructured-task_get"),
        pytest.param("task_post", "static_key", id="static_key-task_post"),
    ],
)
async def test_xai_403_recovers_only_exact_oauth_token_rejection(
    tmp_path: Path,
    consumer: str,
    outcome: str,
) -> None:
    provider = ProviderRegistry.load(RESOURCES).get("xai")
    connection = provider.get_connection("subscription")
    assert connection.oauth is not None
    store = TokenStore(tmp_path / "data")
    original = OAuthToken(
        access_token="old-test-token",
        refresh_token="old-test-refresh",
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    store.save("xai", "subscription", original)
    renews = outcome in {"success", "rejected_again"}
    refresh = respx.post(connection.oauth.token_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "new-test-token",
                "refresh_token": "new-test-refresh",
                "expires_in": 3600,
            },
        )
    )
    method, endpoint, success = _consumer_route(consumer, provider, connection)
    rejection = httpx.Response(403, json=NON_RENEWING_BODIES.get(outcome, TOKEN_REJECTION))
    route = respx.route(
        method=method, url=(connection.base_url or provider.base_url).rstrip("/") + endpoint
    ).mock(side_effect=[rejection, rejection if outcome == "rejected_again" else success])

    getter = OAuthTokenGetter(store, "xai", "subscription", connection.oauth)
    credential: TokenGetter | str = "test-key" if outcome == "static_key" else getter
    if outcome == "success":
        assert await _invoke(consumer, provider, connection, credential, tmp_path)
    else:
        with pytest.raises((ProviderAuthError, ModelDiscoveryError)):
            await _invoke(consumer, provider, connection, credential, tmp_path)

    assert route.call_count == (2 if renews else 1)
    assert refresh.call_count == (1 if renews else 0)
    saved = store.load("xai", "subscription")
    if renews:
        assert saved is not None and saved.refresh_token == "new-test-refresh"
        assert route.calls[1].request.headers["Authorization"] == "Bearer new-test-token"
    else:
        assert saved == original
