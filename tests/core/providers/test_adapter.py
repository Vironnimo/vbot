"""ProviderAdapter contract: abstract interface, defaults, scoped request input
budgets and public stream closure across nested wire routes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from typing import Any, cast, override

import httpx
import pytest
import respx

from core.debug import DebugContext
from core.providers.adapter import (
    IMAGE_WIRE_MEDIA_TYPES,
    ProviderAdapter,
    request_input_budget,
    resolve_request_input_budget,
)
from core.providers.adapter_types import ADAPTER_TYPES
from core.providers.anthropic_compatible import AnthropicCompatibleAdapter
from core.providers.errors import ProviderError
from core.providers.github_copilot import GitHubCopilotAdapter
from core.providers.http import ProviderHTTPTransports
from core.providers.lmstudio import LMStudioAdapter
from core.providers.openai import OpenAIAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import OpenCodeGoAdapter
from core.providers.openrouter import OpenRouterAdapter
from core.providers.providers import RequestInput
from core.providers.reasoning import REASONING_REPLAY_FULL_HISTORY
from core.providers.xai import XAIAdapter

from .adapter_test_support import TOKEN, bearer_config, catalog_model


class _StubAdapter(ProviderAdapter):
    """Minimal concrete Adapter implementing only the abstract interface."""

    @override
    async def aclose(self) -> None:
        """Nothing to release."""

    @override
    async def send(self, messages: list[dict], *, model_id: str, **kwargs) -> dict:
        return {}

    @override
    async def stream(self, messages: list[dict], *, model_id: str, **kwargs) -> AsyncIterator[dict]:
        yield {"type": "finish", "reason": "stop"}


# ---------------------------------------------------------------------------
# Abstract interface and defaults
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["aclose", "send", "stream"])
def test_concrete_adapter_must_implement_aclose_send_and_stream(missing: str) -> None:
    methods = {
        name: getattr(_StubAdapter, name)
        for name in ("aclose", "send", "stream")
        if name != missing
    }
    incomplete = type("IncompleteAdapter", (ProviderAdapter,), methods)

    with pytest.raises(TypeError):
        incomplete()
    assert isinstance(_StubAdapter(), ProviderAdapter)


def test_optional_capabilities_degrade_safely_by_default() -> None:
    adapter = _StubAdapter()

    # Media support, byte limits, replay and listed Tools come from the wire profile.
    profile = adapter.wire_profile("any-model")
    assert adapter.wire_media_support("any-model") == profile.media.types
    assert adapter.image_size_limit("any-model") is None
    assert adapter.request_body_limit("any-model") is None
    assert adapter.input_token_limit("any-model") is None
    assert adapter.reasoning_replay_fidelity("any-model") == profile.replay.fidelity
    assert adapter.reasoning_replay_policy("any-model") == REASONING_REPLAY_FULL_HISTORY
    assert adapter.list_announced_tools("any-model") is False
    assert adapter.on_demand_tools("any-model") is True
    # Debug context is a no-op without a recorder.
    adapter.set_debug_context(
        DebugContext(
            run_id="run-1",
            agent_id="agent-1",
            session_id="session-1",
            provider_id="stub",
            connection_id="stub:api-key",
            model_id="any-model",
            streaming=False,
            iteration_number=1,
        )
    )
    # Response normalization is optional for construction but required at use.
    with pytest.raises(NotImplementedError):
        adapter.normalize_response({})
    # The shared image set every image-capable wire may declare.
    assert frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"}) == (
        IMAGE_WIRE_MEDIA_TYPES
    )


# ---------------------------------------------------------------------------
# Runtime HTTP transport ownership
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("adapter_kind", [*ADAPTER_TYPES, "anthropic_compatible"])
@pytest.mark.asyncio
async def test_adapters_borrow_runtime_http_and_keep_client_state_per_adapter(
    adapter_kind: str,
) -> None:
    transports = ProviderHTTPTransports()
    scope = transports.scope("provider", "api-key", "account")
    config = bearer_config("provider", adapter=adapter_kind)
    adapter_type = (
        AnthropicCompatibleAdapter
        if adapter_kind == "anthropic_compatible"
        else ADAPTER_TYPES[adapter_kind]
    )
    first = cast(Any, adapter_type)(config, TOKEN, http_transport=scope)
    second = cast(Any, adapter_type)(config, TOKEN, http_transport=scope)
    first_client = first._client  # noqa: SLF001 - HTTP client state has no public Adapter view
    second_client = second._client  # noqa: SLF001
    try:
        with respx.mock:
            route = respx.get(url__regex=r"https://provider\.example\.test/.*").mock(
                side_effect=[
                    httpx.Response(200, headers={"set-cookie": "adapter-state=first; Path=/"}),
                    httpx.Response(200),
                    httpx.Response(200),
                ]
            )
            await first_client.get("transport-probe")
            await second_client.get("transport-probe")
            assert first_client.cookies["adapter-state"] == "first"
            assert "cookie" not in route.calls[1].request.headers

            await first.aclose()
            assert first_client.is_closed
            assert not second_client.is_closed
            assert (await second_client.get("transport-probe")).status_code == 200

            await transports.aclose()
            with pytest.raises(RuntimeError):
                await second_client.get("transport-probe")
    finally:
        await first.aclose()
        await second.aclose()
        await transports.aclose()


# ---------------------------------------------------------------------------
# Scoped request input budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_input_budget_is_task_local_nested_and_model_specific() -> None:
    local = RequestInput.estimate(280_000)

    async def one_budget(tokens: int) -> None:
        budget = RequestInput.measured(tokens, estimated_change=1_000)
        with request_input_budget("model", budget):
            await asyncio.sleep(0)
            assert resolve_request_input_budget("model", 280_000) == budget
            assert resolve_request_input_budget("other-model", 280_000) == local
            with request_input_budget("model", RequestInput.measured(123)):
                assert resolve_request_input_budget("model", 280_000) == RequestInput.measured(123)
            assert resolve_request_input_budget("model", 280_000) == budget
        assert resolve_request_input_budget("model", 280_000) == local

    await asyncio.gather(one_budget(150_000), one_budget(20_000))


def test_request_input_budget_evaluates_a_fallback_only_without_a_matching_scope() -> None:
    calls: list[bool] = []

    def estimate() -> int:
        calls.append(True)
        return 280_000

    with request_input_budget("model", RequestInput.measured(0)):
        assert resolve_request_input_budget("model", estimate) == RequestInput.measured(0)
        assert calls == []
        assert resolve_request_input_budget("other-model", estimate) == RequestInput.estimate(
            280_000
        )
    assert resolve_request_input_budget("model", estimate) == RequestInput.estimate(280_000)
    assert len(calls) == 2


_BUDGET_MODEL_ID = "gpt-5.5"  # the OpenAI, xAI and Go wire files route it to Responses
_BUDGET_MESSAGES = [{"role": "user", "content": "Keep this request unchanged."}]
_BUDGET_TOOLS = [
    {
        "name": "lookup",
        "description": "Look up test data.",
        "parameters": {"type": "object", "properties": {}},
    }
]
_BUDGET_WIRES: dict[str, tuple[str, Callable[..., ProviderAdapter], str, str]] = {
    "chat": ("compatible", OpenAICompatibleAdapter, "/chat/completions", _BUDGET_MODEL_ID),
    "messages": ("compatible", AnthropicCompatibleAdapter, "/messages", _BUDGET_MODEL_ID),
    "openai-responses": ("openai", OpenAIAdapter, "/responses", _BUDGET_MODEL_ID),
    "xai-responses": ("xai", XAIAdapter, "/responses", _BUDGET_MODEL_ID),
    "copilot-chat": (
        "github-copilot",
        GitHubCopilotAdapter,
        "/chat/completions",
        _BUDGET_MODEL_ID,
    ),
    "copilot-responses": ("github-copilot", GitHubCopilotAdapter, "/responses", _BUDGET_MODEL_ID),
    "openrouter-responses": ("openrouter", OpenRouterAdapter, "/responses", "openai/gpt-5.6-sol"),
    "go-responses": ("opencode-go", OpenCodeGoAdapter, "/responses", _BUDGET_MODEL_ID),
}


def _budget_adapter(wire: str) -> tuple[Any, str, str]:
    provider_id, adapter_type, path, model_id = _BUDGET_WIRES[wire]
    model = catalog_model(
        model_id,
        context_window=256_000,
        max_output_tokens=256_000,
        metadata={
            "github_copilot": {
                "vendor": "OpenAI",
                "family": model_id,
                "supported_endpoints": [path],
                "max_prompt_tokens": 200_000,
                "tool_calls": True,
            },
        },
    )
    config = bearer_config(provider_id, base_url="https://provider.example/v1")
    adapter = adapter_type(config, TOKEN, model_lookup=lambda _model_id: model)
    return adapter, f"https://provider.example/v1{path}", model_id


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", list(_BUDGET_WIRES))
async def test_matching_budget_replaces_wire_estimation_without_changing_the_request(
    wire: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, url, model_id = _budget_adapter(wire)
    calls: list[tuple[Any, ...]] = []

    def estimate(messages, *, model_id, tools=None):
        calls.append((messages, model_id, tools))
        return 150_000

    def unexpected_estimate(*_args, **_kwargs):
        raise AssertionError("A matching Context budget must not re-estimate the request")

    async def sent_body() -> dict[str, Any]:
        await adapter.send(_BUDGET_MESSAGES, model_id=model_id, tools=_BUDGET_TOOLS)
        body: dict[str, Any] = json.loads(route.calls.last.request.content)
        return body

    try:
        with respx.mock:
            route = respx.post(url).mock(return_value=httpx.Response(200, json={"id": "reply"}))
            monkeypatch.setattr(adapter, "estimate_request_input_tokens", estimate)
            standalone = await sent_body()
            assert calls
            assert all(call == (_BUDGET_MESSAGES, model_id, _BUDGET_TOOLS) for call in calls)
            limit = "max_output_tokens" if "max_output_tokens" in standalone else "max_tokens"
            assert standalone[limit] == 68_500

            calls.clear()
            with request_input_budget("another-model", RequestInput.estimate(250_000)):
                assert await sent_body() == standalone
            assert calls

            monkeypatch.setattr(adapter, "estimate_request_input_tokens", unexpected_estimate)
            with request_input_budget(model_id, RequestInput.estimate(150_000)):
                assert await sent_body() == standalone
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_matching_budget_still_enforces_the_reported_input_limit_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, url, model_id = _budget_adapter("copilot-responses")

    def unexpected_estimate(*_args, **_kwargs):
        raise AssertionError("Input-limit validation must use the matching Context budget")

    monkeypatch.setattr(adapter, "estimate_request_input_tokens", unexpected_estimate)
    assert adapter.input_token_limit(model_id) == 200_000
    try:
        with respx.mock:
            route = respx.post(url).mock(return_value=httpx.Response(200, json={"id": "reply"}))
            with (
                request_input_budget(model_id, RequestInput.measured(200_001)),
                pytest.raises(ProviderError) as error,
            ):
                await adapter.send(_BUDGET_MESSAGES, model_id=model_id)
    finally:
        await adapter.aclose()

    assert error.value.retryable is False
    assert route.call_count == 0


# ---------------------------------------------------------------------------
# Public stream closure
# ---------------------------------------------------------------------------


class _PartialResponseStream(httpx.AsyncByteStream):
    """An SSE body that stays open after its first events until it is closed."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.closed = False

    @override
    async def __aiter__(self) -> AsyncIterator[bytes]:
        for event in self.events:
            yield f"data: {json.dumps(event)}\n\n".encode()

    @override
    async def aclose(self) -> None:
        self.closed = True


_PARTIAL_EVENTS: dict[str, list[dict[str, Any]]] = {
    "chat": [{"choices": [{"index": 0, "delta": {"content": "partial"}}]}],
    "responses": [{"type": "response.output_text.delta", "delta": "partial"}],
    "messages": [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "partial"},
        },
    ],
}

_COPILOT_ENDPOINTS = {
    "chat": "/chat/completions",
    "responses": "/responses",
    "messages": "/v1/messages",
}


def _copilot_lookup(model_id: str):
    vendor, wire = {
        "gemini-3.1-pro-preview": ("Google", "chat"),
        "gpt-5-mini": ("OpenAI", "responses"),
        "claude-sonnet-4.6": ("Anthropic", "messages"),
    }[model_id]
    return catalog_model(
        model_id,
        metadata={
            "github_copilot": {
                "vendor": vendor,
                "family": model_id,
                "supported_endpoints": [_COPILOT_ENDPOINTS[wire]],
                "streaming": True,
            }
        },
    )


_CLOSURE_PROVIDER_IDS: dict[Callable[..., ProviderAdapter], str] = {
    GitHubCopilotAdapter: "github-copilot",
    OpenRouterAdapter: "openrouter",
    LMStudioAdapter: "lmstudio",
}


# One case per Provider-owned route that wraps the shared stream decoders in its own
# generator: each wrapper must forward closure to the nested transport iterator.
@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_type", "model_id", "wire"),
    [
        pytest.param(GitHubCopilotAdapter, "gemini-3.1-pro-preview", "chat", id="copilot-chat"),
        pytest.param(GitHubCopilotAdapter, "gpt-5-mini", "responses", id="copilot-responses"),
        pytest.param(GitHubCopilotAdapter, "claude-sonnet-4.6", "messages", id="copilot-messages"),
        pytest.param(OpenRouterAdapter, "test-model", "chat", id="openrouter-chat"),
        pytest.param(
            OpenRouterAdapter, "openai/gpt-5.6-luna", "responses", id="openrouter-responses"
        ),
        pytest.param(LMStudioAdapter, "test-model", "chat", id="lmstudio-chat"),
    ],
)
async def test_closing_the_public_stream_closes_the_partial_http_response(
    adapter_type: Callable[..., ProviderAdapter], model_id: str, wire: str
) -> None:
    lookup = _copilot_lookup if adapter_type is GitHubCopilotAdapter else lambda _model_id: None
    provider_id = _CLOSURE_PROVIDER_IDS[adapter_type]
    config = bearer_config(provider_id, base_url="https://provider.test/v1")
    adapter = adapter_type(config, TOKEN, model_lookup=lookup)
    # LM Studio loads the Model through its native API before streaming.
    respx.get("https://provider.test/api/v1/models").mock(
        return_value=httpx.Response(
            200, json={"models": [{"key": model_id, "loaded_instances": [{}]}]}
        )
    )
    body = _PartialResponseStream(_PARTIAL_EVENTS[wire])
    route = respx.post(url__regex=r"https://provider\.test/.*").mock(
        return_value=httpx.Response(200, stream=body, headers={"content-type": "text/event-stream"})
    )
    stream = cast(
        AsyncGenerator[dict[str, Any]],
        adapter.stream([{"role": "user", "content": "test"}], model_id=model_id),
    )
    try:
        assert await anext(stream) == {"type": "content_delta", "text": "partial"}
        await stream.aclose()
        assert body.closed
        assert route.call_count == 1
    finally:
        await stream.aclose()
        await adapter.aclose()
