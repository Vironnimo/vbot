"""OAuthRequestRecovery: one rejected-token refresh per logical request, on every Adapter wire."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.adapter import ProviderAdapter
from core.providers.errors import ProviderAuthError, ProviderError
from core.providers.github_copilot import GitHubCopilotAdapter
from core.providers.minimax import MiniMaxAdapter
from core.providers.nous import NousAdapter
from core.providers.openai import OpenAIAdapter
from core.providers.opencode_zen import OpenCodeZenAdapter
from core.providers.providers import (
    AuthConfig,
    ConnectionConfig,
    OAuthConfig,
    ProviderConfig,
    ProviderRegistry,
)
from core.providers.runtime import ADAPTER_TYPES
from core.providers.token_getter import OAuthRequestRecovery, OAuthTokenGetter, TokenGetter
from core.providers.token_store import OAuthToken, TokenStore
from core.providers.xai import XAIAdapter
from core.utils.tls import shared_ssl_context

from .oauth_test_support import (
    expired_token,
    github_oauth_config,
    jwt_with_account,
    openai_oauth_config,
    xai_oauth_config,
)

BEARER = AuthConfig(header="Authorization", prefix="Bearer ")
OLD_TOKEN = jwt_with_account("old-test-account")
NEW_TOKEN = jwt_with_account("new-test-account")
MESSAGES = [{"role": "user", "content": "Test request"}]


@pytest.fixture(scope="module")
def bundled() -> ProviderRegistry:
    return ProviderRegistry.load(Path(__file__).resolve().parents[3] / "resources")


def _oauth_connection(config: ProviderConfig) -> ConnectionConfig:
    return next(connection for connection in config.connections if connection.type == "oauth")


# ---------------------------------------------------------------------------
# Getter-owned rejection vocabulary and Account-wide coalescing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "body"),
    [
        pytest.param(
            openai_oauth_config(),
            '{"code":"unauthenticated:bad-credentials"}',
            id="other-flow-ignores-xai-vocabulary",
        ),
        pytest.param(xai_oauth_config(), '{"code":"permission_denied"}', id="xai-permission"),
        pytest.param(xai_oauth_config(), "unauthenticated:bad-credentials", id="xai-not-json"),
    ],
)
async def test_unrecognized_403_is_not_a_token_rejection(
    tmp_path: Path, config: OAuthConfig, body: str
) -> None:
    """Only xAI's exact bad-credentials 403 renews; others do not even load the token."""

    getter = OAuthTokenGetter(TokenStore(tmp_path), "provider", "oauth", config)
    # No token exists, so reading one would raise instead of returning None.
    assert (
        await getter.refresh_after_rejection("token", status_code=403, response_body=body) is None
    )


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "stored_extra", "rejection_status"),
    [
        pytest.param(
            github_oauth_config(token_exchange=True),
            {"github_oauth_token": "github-token"},
            401,
            id="copilot-exchange-401",
        ),
        pytest.param(openai_oauth_config(), {}, 401, id="standard-refresh-401"),
        pytest.param(xai_oauth_config(), {}, 403, id="rotating-refresh-xai-403"),
    ],
)
async def test_concurrent_rejected_requests_share_one_account_refresh(
    tmp_path: Path, config: OAuthConfig, stored_extra: dict[str, str], rejection_status: int
) -> None:
    """Four requests rejected together, each with its own getter, cause one token request."""

    store = TokenStore(tmp_path)
    store.save(
        "provider",
        "oauth",
        OAuthToken(OLD_TOKEN, "old-refresh", datetime.now(UTC) + timedelta(days=1), stored_extra),
        account_id="work",
    )
    renewal = respx.route(url=config.token_exchange_url or config.token_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": NEW_TOKEN,
                "token": NEW_TOKEN,
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            },
        )
    )
    all_rejected = asyncio.Event()
    rejected = 0

    async def serve(request: httpx.Request) -> httpx.Response:
        nonlocal rejected
        if request.headers["Authorization"] == f"Bearer {OLD_TOKEN}":
            rejected += 1
            if rejected == 4:
                all_rejected.set()
            await asyncio.wait_for(all_rejected.wait(), timeout=5)
            return httpx.Response(
                rejection_status, json={"code": "unauthenticated:bad-credentials"}
            )
        assert request.headers["Authorization"] == f"Bearer {NEW_TOKEN}"
        return httpx.Response(200, json={"ok": True})

    inference = respx.post("https://inference.example/request").mock(side_effect=serve)

    async def invoke(client: httpx.AsyncClient) -> Any:
        getter = OAuthTokenGetter(store, "provider", "oauth", config, account_id="work")
        recovery = OAuthRequestRecovery(getter, BEARER)

        async def request() -> Any:
            headers = {"Authorization": f"Bearer {await getter()}"}
            response = await client.post("https://inference.example/request", headers=headers)
            recovery.record_response(response.status_code, headers, response.text)
            if response.status_code >= 400:
                raise ProviderAuthError("rejected")
            return response.json()

        return await recovery.run(request)

    async with httpx.AsyncClient(verify=shared_ssl_context()) as client:
        results = await asyncio.gather(*(invoke(client) for _ in range(4)))

    assert results == [{"ok": True}] * 4
    assert inference.call_count == 8
    assert renewal.call_count == 1
    saved = store.load("provider", "oauth", account_id="work")
    assert saved is not None
    assert saved.access_token == NEW_TOKEN
    # A Copilot exchange keeps the refresh token; refresh grants rotate it.
    assert saved.refresh_token == ("old-refresh" if config.token_exchange_url else "new-refresh")
    assert store.load("provider", "oauth") is None


# ---------------------------------------------------------------------------
# Bundled OAuth Connections and their Adapter wires
# ---------------------------------------------------------------------------

BUNDLED_OAUTH_BINDINGS: dict[str, tuple[type[ProviderAdapter], str | None]] = {
    "github-copilot": (GitHubCopilotAdapter, None),
    "minimax": (MiniMaxAdapter, "anthropic_messages"),
    "nous": (NousAdapter, None),
    "openai": (OpenAIAdapter, "codex_responses"),
    "opencode-zen": (OpenCodeZenAdapter, None),
    "xai": (XAIAdapter, None),
}


def test_bundled_oauth_providers_bind_the_expected_adapter_and_mode(
    bundled: ProviderRegistry,
) -> None:
    """Pins the Provider-to-wire mapping that the wire table below relies on."""

    oauth_providers = {
        provider_id
        for provider_id in bundled.list_ids()
        if any(item.type == "oauth" for item in bundled.get(provider_id).connections)
    }
    assert oauth_providers == set(BUNDLED_OAUTH_BINDINGS)
    for provider_id, (adapter_type, mode) in BUNDLED_OAUTH_BINDINGS.items():
        config = bundled.get(provider_id)
        connection = _oauth_connection(config)
        assert ADAPTER_TYPES[config.adapter] is adapter_type, provider_id
        assert connection.mode == mode, provider_id
        assert connection.auth == BEARER, provider_id


class RefreshingGetter:
    """Token getter fake that renews on 401 only, optionally failing the renewal."""

    def __init__(self, *, failure: Exception | None = None) -> None:
        self.token = OLD_TOKEN
        self.rejected: list[str] = []
        self.failure = failure

    async def __call__(self) -> str:
        return self.token

    async def refresh_after_rejection(
        self, rejected: str, *, status_code: int, response_body: str
    ) -> str | None:
        if status_code != 401:
            return None
        self.rejected.append(rejected)
        if self.failure is not None:
            raise self.failure
        self.token = NEW_TOKEN
        return self.token


@dataclass(frozen=True)
class Wire:
    provider_id: str
    model_id: str
    protocol: str
    path: str
    credential_header: str = "Authorization"
    credential_prefix: str = "Bearer "

    def endpoint(self, streaming: bool) -> str:
        if self.protocol == "gemini_generate_content":
            action = "streamGenerateContent?alt=sse" if streaming else "generateContent"
            return f"{self.path}:{action}"
        return self.path


# One entry per Adapter request path that records rejections for OAuthRequestRecovery:
# each Provider Adapter builds, sends and parses its wires through its own code, and
# some wires present the token in a wire-specific header.
WIRES = {
    "chat-completions": Wire(
        "nous", "anthropic/claude-sonnet-4.6", "chat_completions", "/chat/completions"
    ),
    "copilot-chat-completions": Wire(
        "github-copilot", "gemini-3.1-pro-preview", "chat_completions", "/chat/completions"
    ),
    "zen-chat-completions": Wire(
        "opencode-zen", "deepseek-v4-flash", "chat_completions", "/chat/completions"
    ),
    "messages": Wire("minimax", "MiniMax-M2.7", "messages", "/messages"),
    "copilot-messages": Wire("github-copilot", "claude-haiku-4.5", "messages", "/v1/messages"),
    "zen-messages-x-api-key": Wire(
        "opencode-zen", "claude-sonnet-5", "messages", "/messages", "x-api-key", ""
    ),
    "responses": Wire("xai", "grok-4.5", "responses", "/responses"),
    "zen-responses": Wire("opencode-zen", "gpt-5.6-sol", "responses", "/responses"),
    "codex-responses": Wire("openai", "gpt-5.6-sol", "responses", "/codex/responses"),
    "copilot-responses": Wire("github-copilot", "gpt-5.4", "responses", "/responses"),
    "zen-gemini": Wire(
        "opencode-zen",
        "gemini-3.5-flash",
        "gemini_generate_content",
        "/models/gemini-3.5-flash",
        "x-goog-api-key",
        "",
    ),
}


def _model(wire: Wire) -> Model:
    metadata: dict[str, Any] = {}
    if wire.provider_id == "opencode-zen":
        metadata = {"opencode_zen": {"protocol": wire.protocol}}
    elif wire.provider_id == "github-copilot":
        metadata = {
            "github_copilot": {
                "family": wire.model_id,
                "supported_endpoints": [wire.path],
                "streaming": True,
            }
        }
    return Model(
        model_id=wire.model_id,
        name=wire.model_id,
        family=wire.model_id,
        capabilities=Capabilities(
            vision=False,
            tools=False,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
        ),
        context_window=32768,
        max_output_tokens=4096,
        metadata=metadata,
    )


def _adapter(
    bundled: ProviderRegistry, wire: Wire, token_getter: TokenGetter | str
) -> ProviderAdapter:
    """Build the Adapter the way the Runtime does for the bundled OAuth Connection."""

    config = bundled.get(wire.provider_id)
    connection = _oauth_connection(config)
    model = _model(wire)
    adapter = cast(Any, ADAPTER_TYPES[config.adapter])(
        config,
        token_getter,
        connection.base_url,
        connection.auth,
        model_lookup=lambda _model_id: model,
        connection_mode=connection.mode,
    )
    return cast(ProviderAdapter, adapter)


def _url(bundled: ProviderRegistry, wire: Wire, streaming: bool) -> str:
    config = bundled.get(wire.provider_id)
    base_url = _oauth_connection(config).base_url or config.base_url
    return base_url.rstrip("/") + wire.endpoint(streaming)


def _success_response(protocol: str, streaming: bool) -> httpx.Response:
    if protocol == "chat_completions":
        payload: dict[str, Any] = {
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "delta": {"content": "ok"},
                    "finish_reason": "stop",
                }
            ]
        }
        events = [payload]
    elif protocol == "responses":
        payload = {
            "id": "resp-test",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
        }
        events = [
            {"type": "response.output_text.delta", "delta": "ok"},
            {"type": "response.completed", "response": payload},
        ]
    elif protocol == "messages":
        payload = {
            "id": "msg-test",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        events = [
            {"type": "message_start", "message": {**payload, "content": [], "stop_reason": None}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "ok"},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]
    else:
        payload = {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP"}
            ]
        }
        events = [payload]
    if not streaming:
        return httpx.Response(200, json=payload)
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
    if protocol == "chat_completions":
        body += "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})


async def _invoke(adapter: ProviderAdapter, model_id: str, streaming: bool) -> Any:
    if streaming:
        return [delta async for delta in adapter.stream(MESSAGES, model_id=model_id)]
    return await adapter.send(MESSAGES, model_id=model_id)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True], ids=["send", "stream"])
@pytest.mark.parametrize("wire_name", list(WIRES))
async def test_rejected_token_is_refreshed_once_on_every_wire(
    bundled: ProviderRegistry, wire_name: str, streaming: bool
) -> None:
    wire = WIRES[wire_name]
    getter = RefreshingGetter()
    adapter = _adapter(bundled, wire, getter)
    # Codex send also consumes a stream internally.
    streamed_reply = streaming or wire_name == "codex-responses"
    route = respx.post(_url(bundled, wire, streaming)).mock(
        side_effect=[
            httpx.Response(401, json={"error": {"type": "AuthError"}}),
            _success_response(wire.protocol, streamed_reply),
        ]
    )

    try:
        assert await _invoke(adapter, wire.model_id, streaming)
    finally:
        await adapter.aclose()

    assert getter.rejected == [OLD_TOKEN]
    sent = [call.request.headers[wire.credential_header] for call in route.calls]
    assert sent == [wire.credential_prefix + OLD_TOKEN, wire.credential_prefix + NEW_TOKEN]
    if wire_name == "codex-responses":
        # Token-derived headers are rebuilt for the retried request.
        assert [call.request.headers["chatgpt-account-id"] for call in route.calls] == [
            "old-test-account",
            "new-test-account",
        ]


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "first_status", "second_status", "refreshed", "requests"),
    [
        pytest.param("rejected_again", 401, 401, True, 2, id="refreshed-token-rejected-again"),
        pytest.param("refresh_failure", 401, None, True, 1, id="refresh-fails"),
        pytest.param("forbidden", 403, None, False, 1, id="unrecognized-rejection"),
        pytest.param("static_key", 401, None, False, 1, id="static-credential"),
    ],
)
async def test_recovery_refreshes_at_most_once_and_only_for_recognized_rejections(
    bundled: ProviderRegistry,
    outcome: str,
    first_status: int,
    second_status: int | None,
    refreshed: bool,
    requests: int,
) -> None:
    wire = WIRES["chat-completions"]
    failure = ProviderError("test refresh unavailable", retryable=True)
    getter = RefreshingGetter(failure=failure if outcome == "refresh_failure" else None)
    adapter = _adapter(bundled, wire, getter.token if outcome == "static_key" else getter)
    replies = [httpx.Response(first_status, json={"error": {"type": "AuthError"}})]
    if second_status is not None:
        replies.append(httpx.Response(second_status, json={"error": {"type": "AuthError"}}))
    route = respx.post(_url(bundled, wire, False)).mock(side_effect=replies)

    try:
        with pytest.raises(ProviderError) as caught:
            await _invoke(adapter, wire.model_id, streaming=False)
    finally:
        await adapter.aclose()

    if outcome == "refresh_failure":
        assert caught.value is failure
    else:
        assert isinstance(caught.value, ProviderAuthError)
    assert getter.rejected == ([OLD_TOKEN] if refreshed else [])
    assert route.call_count == requests


@respx.mock
@pytest.mark.asyncio
async def test_stream_is_not_restarted_after_visible_output(bundled: ProviderRegistry) -> None:
    wire = WIRES["chat-completions"]
    getter = RefreshingGetter()
    adapter = _adapter(bundled, wire, getter)
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": "visible output"}}]},
        {"error": {"type": "authentication_error", "code": "invalid_api_key", "message": "x"}},
    ]
    route = respx.post(_url(bundled, wire, True)).mock(
        return_value=httpx.Response(
            200,
            text="".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks),
            headers={"Content-Type": "text/event-stream"},
        )
    )
    stream: AsyncIterator[dict[str, Any]] = adapter.stream(MESSAGES, model_id=wire.model_id)
    try:
        assert (await anext(stream))["text"] == "visible output"
        with pytest.raises(ProviderAuthError):
            await anext(stream)
    finally:
        await adapter.aclose()

    assert route.call_count == 1
    assert getter.rejected == []


@respx.mock
@pytest.mark.asyncio
async def test_adapter_asks_its_oauth_getter_for_each_request(
    bundled: ProviderRegistry, tmp_path: Path
) -> None:
    """An expired stored token is refreshed before the request that needs it."""

    wire = WIRES["chat-completions"]
    connection = _oauth_connection(bundled.get(wire.provider_id))
    assert connection.oauth is not None
    store = TokenStore(tmp_path)
    store.save(wire.provider_id, connection.id, expired_token())
    respx.post(connection.oauth.token_url).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "refreshed",
                "refresh_token": "rotated",
                "scope": "inference:invoke",
            },
        )
    )
    route = respx.post(_url(bundled, wire, False)).mock(
        return_value=_success_response(wire.protocol, streaming=False)
    )
    getter = OAuthTokenGetter(store, wire.provider_id, connection.id, connection.oauth)
    adapter = _adapter(bundled, wire, getter)

    try:
        await _invoke(adapter, wire.model_id, streaming=False)
    finally:
        await adapter.aclose()

    assert route.calls.last.request.headers["Authorization"] == "Bearer refreshed"


@respx.mock
@pytest.mark.asyncio
async def test_codex_request_requires_the_chatgpt_account_claim(
    bundled: ProviderRegistry,
) -> None:
    wire = WIRES["codex-responses"]
    adapter = _adapter(bundled, wire, "not-a-jwt")
    route = respx.post(_url(bundled, wire, False)).mock(return_value=httpx.Response(200))

    try:
        with pytest.raises(ProviderAuthError):
            await _invoke(adapter, wire.model_id, streaming=False)
    finally:
        await adapter.aclose()

    assert route.call_count == 0
