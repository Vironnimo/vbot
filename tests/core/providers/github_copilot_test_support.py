"""Shared configuration, Models and wire helpers for the GitHub Copilot Adapter tests.

The Adapter routes each Model to ``/chat/completions``, ``/responses`` or
``/v1/messages``. The helpers mock all three endpoints at once, so a test
observes the selected route from the endpoint that was actually called. The
Responses helpers also drive the shared stateless Responses codec directly.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
import respx

from core.models.models import Model
from core.providers.github_copilot import GitHubCopilotAdapter
from core.providers.github_copilot_policy import (
    CHAT_COMPLETIONS_ENDPOINT,
    MESSAGES_ENDPOINT,
    RESPONSES_ENDPOINT,
    GitHubCopilotModelPolicy,
    copilot_model_policy,
)
from core.providers.github_copilot_responses import (
    ResponsesStreamState,
    iter_responses_sse_deltas_with_state,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.token_getter import TokenGetter

FIXTURE_PATH = Path("tests/core/models/fixtures/github_copilot_models_raw.json")
API_KEY = "test-api-key-12345"
COPILOT_CONFIG = ProviderConfig(
    id="github-copilot",
    name="GitHub Copilot",
    adapter="github_copilot",
    base_url="https://api.githubcopilot.com",
    connections=[
        ConnectionConfig(
            id="oauth",
            type="oauth",
            label="Sign in with GitHub",
            auth=AuthConfig(header="Authorization", prefix="Bearer ", credential_key=""),
        )
    ],
    defaults={"max_tokens": 4096},
)
# Without a config output default the Messages route falls back to its own.
NO_DEFAULTS_CONFIG = replace(COPILOT_CONFIG, defaults=None)

CHAT_URL = f"{COPILOT_CONFIG.base_url}{CHAT_COMPLETIONS_ENDPOINT}"
RESPONSES_URL = f"{COPILOT_CONFIG.base_url}{RESPONSES_ENDPOINT}"
MESSAGES_URL = f"{COPILOT_CONFIG.base_url}{MESSAGES_ENDPOINT}"
ENDPOINT_URLS = {
    CHAT_COMPLETIONS_ENDPOINT: CHAT_URL,
    RESPONSES_ENDPOINT: RESPONSES_URL,
    MESSAGES_ENDPOINT: MESSAGES_URL,
}

CHAT_REPLY: dict[str, Any] = {
    "id": "chatcmpl-abc123",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello!"},
            "finish_reason": "stop",
        }
    ],
}
EMPTY_REPLIES: dict[str, dict[str, Any]] = {
    CHAT_COMPLETIONS_ENDPOINT: CHAT_REPLY,
    RESPONSES_ENDPOINT: {"output": []},
    MESSAGES_ENDPOINT: {"content": []},
}
SAMPLE_MESSAGES = [{"role": "user", "content": "Hello"}]

SYNTHETIC_COPILOT_METADATA_BY_MODEL_ID = {
    "claude-haiku-4.5": {
        "github_copilot": {
            "vendor": "Anthropic",
            "family": "claude-haiku-4.5",
            "version": "claude-haiku-4.5",
            "supported_endpoints": [CHAT_COMPLETIONS_ENDPOINT, MESSAGES_ENDPOINT],
            "adaptive_thinking": True,
            "parallel_tool_calls": True,
            "streaming": True,
            "structured_outputs": True,
            "tool_calls": True,
        }
    },
    "gemini-3.1-pro-preview": {
        "github_copilot": {
            "vendor": "Google",
            "family": "gemini-3.1-pro-preview",
            "supported_endpoints": [CHAT_COMPLETIONS_ENDPOINT],
            "tool_calls": True,
            "streaming": True,
        }
    },
    "gpt-5.4": {
        "github_copilot": {
            "vendor": "OpenAI",
            "family": "gpt-5.4",
            "version": "gpt-5.4",
            "supported_endpoints": [CHAT_COMPLETIONS_ENDPOINT, RESPONSES_ENDPOINT, "ws:/responses"],
            "reasoning_efforts": ["low", "medium", "high"],
            "parallel_tool_calls": True,
            "streaming": True,
            "structured_outputs": True,
            "tool_calls": True,
        }
    },
    "gpt-5.4-partial": {
        "github_copilot": {
            "vendor": "OpenAI",
            "family": "gpt-5.4",
            "version": "gpt-5.4",
            "supported_endpoints": [RESPONSES_ENDPOINT],
            "streaming": True,
            "tool_calls": True,
        }
    },
}


def raw_copilot_models() -> dict[str, dict[str, Any]]:
    """The captured ``/models`` entries of the Copilot fixture, by id."""

    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["data"]
    return {entry["id"]: entry for entry in data}


def copilot_model(model_id: str) -> Model:
    """The fixture Model normalized through the Copilot catalog."""

    return GitHubCopilotAdapter.normalize_catalog_entry(raw_copilot_models()[model_id], {})


def copilot_model_with_metadata(
    model_id: str,
    metadata: Mapping[str, Any],
    *,
    family: str = "",
    context_window: int | None = None,
) -> Model:
    """A catalog Model without an output limit that carries only the given metadata."""

    base_model = GitHubCopilotAdapter.normalize_catalog_entry(
        {"id": model_id, "name": model_id, "capabilities": {"supports": {}}},
        {},
    )
    return replace(base_model, metadata=metadata, family=family, context_window=context_window)


def copilot_metadata(
    vendor: str, family: str, endpoints: Iterable[str], **facts: Any
) -> dict[str, Any]:
    """Catalog metadata in the ``github_copilot`` shape the Adapter reads."""

    return {
        "github_copilot": {
            "vendor": vendor,
            "family": family,
            "version": family,
            "supported_endpoints": list(endpoints),
            **facts,
        }
    }


def _copilot_metadata_lookup(model_id: str) -> Model | None:
    """Synthetic metadata first, then the fixture catalog; ``None`` for other ids."""

    synthetic_metadata = SYNTHETIC_COPILOT_METADATA_BY_MODEL_ID.get(model_id)
    if synthetic_metadata is not None:
        return copilot_model_with_metadata(model_id, synthetic_metadata)
    if model_id not in raw_copilot_models():
        return None
    return copilot_model(model_id)


def make_adapter(
    config: ProviderConfig = COPILOT_CONFIG,
    *,
    lookup: Callable[[str], Model | None] | None = _copilot_metadata_lookup,
    metadata: Mapping[str, Any] | None = None,
    family: str = "",
    context_window: int | None = None,
    token_getter: TokenGetter | str = API_KEY,
) -> GitHubCopilotAdapter:
    """The Adapter; ``metadata`` resolves every Model id to that catalog metadata."""

    if metadata is not None:
        catalog_metadata = metadata

        def metadata_lookup(model_id: str) -> Model | None:
            return copilot_model_with_metadata(
                model_id, catalog_metadata, family=family, context_window=context_window
            )

        lookup = metadata_lookup
    return GitHubCopilotAdapter(config, token_getter, model_lookup=lookup)


@dataclass(frozen=True)
class Exchange:
    """One ``send()`` against the mocked Copilot endpoints."""

    endpoint: str
    request: httpx.Request
    response: dict[str, Any]

    @property
    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(self.request.content)
        return payload


async def send_exchange(
    adapter: GitHubCopilotAdapter,
    messages: list[dict[str, Any]] = SAMPLE_MESSAGES,
    *,
    model_id: str,
    reply: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> Exchange:
    """Send once with every endpoint mocked; ``reply`` replaces the empty reply."""

    with respx.mock:
        routes = {
            endpoint: respx.post(url).mock(
                return_value=httpx.Response(
                    200, json=dict(reply) if reply is not None else EMPTY_REPLIES[endpoint]
                )
            )
            for endpoint, url in ENDPOINT_URLS.items()
        }
        response = await adapter.send(messages, model_id=model_id, **kwargs)
    [(endpoint, route)] = [(endpoint, route) for endpoint, route in routes.items() if route.called]
    return Exchange(endpoint, route.calls.last.request, response)


def sse_events(*events: Mapping[str, Any]) -> str:
    """Frame Responses or Messages events as named SSE events, as both wires send them."""

    return "".join(sse_event(str(event["type"]), event) for event in events)


def sse_response(
    body: str | bytes | AsyncIterator[bytes] | httpx.AsyncByteStream,
) -> httpx.Response:
    headers = {"content-type": "text/event-stream"}
    if isinstance(body, httpx.AsyncByteStream):
        return httpx.Response(200, stream=body, headers=headers)
    return httpx.Response(200, content=body, headers=headers)


async def stream_deltas(
    adapter: GitHubCopilotAdapter,
    body: str | bytes | AsyncIterator[bytes] | httpx.AsyncByteStream,
    messages: list[dict[str, Any]] = SAMPLE_MESSAGES,
    *,
    model_id: str,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """Every normalized delta of one ``stream()`` whose endpoint answers ``body``."""

    with respx.mock:
        for url in ENDPOINT_URLS.values():
            respx.post(url).mock(return_value=sse_response(body))
        return [delta async for delta in adapter.stream(messages, model_id=model_id, **kwargs)]


class BrokenStream(httpx.AsyncByteStream):
    """Delivers one complete SSE frame, then fails like a dropped connection."""

    def __init__(self, first_frame: str, failure: Exception) -> None:
        self._first_frame = first_frame
        self._failure = failure
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._first_frame.encode()
        raise self._failure

    async def aclose(self) -> None:
        self.closed = True


class RotatingTokenGetter:
    """Async token getter that yields the next token on each call."""

    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self.calls = 0

    async def __call__(self) -> str:
        token = self._tokens[min(self.calls, len(self._tokens) - 1)]
        self.calls += 1
        return token


# ---------------------------------------------------------------------------
# Shared Responses codec
# ---------------------------------------------------------------------------


def responses_policy(model_id: str = "gpt-5.4", **overrides: Any) -> GitHubCopilotModelPolicy:
    """A Copilot Responses policy with every optional feature unless overridden."""

    metadata = copilot_metadata(
        "OpenAI",
        model_id,
        [RESPONSES_ENDPOINT],
        reasoning_efforts=["low", "medium", "high", "xhigh"],
        tool_calls=True,
        parallel_tool_calls=True,
        streaming=True,
        structured_outputs=True,
    )
    metadata["github_copilot"].update(overrides)
    return copilot_model_policy(model_id, metadata)


def sse_event(event: str, data: Mapping[str, Any]) -> str:
    """One named SSE event; ``data`` may omit ``type`` to rely on the event name."""

    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def decode_responses_sse(
    lines: Iterable[str], state: ResponsesStreamState | None = None
) -> list[dict[str, Any]]:
    """Decode Responses SSE lines with a fresh (or the given) stream state."""

    return list(iter_responses_sse_deltas_with_state(lines, state or ResponsesStreamState()))
