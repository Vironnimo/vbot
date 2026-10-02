"""OpenCode Go Adapter: profile-driven wire routing, request identity and request budgets."""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx

from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES
from core.providers.anthropic_compatible import ANTHROPIC_VERSION
from core.providers.github_copilot_responses import estimate_responses_input_tokens
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import (
    OPENCODE_SESSION_HEADER,
    OPENCODE_SESSION_ID_KWARG,
    OpenCodeGoAdapter,
)
from core.providers.providers import AuthConfig
from core.providers.wire_observations import WireObservations
from core.providers.wire_profiles import WireProfiles, bundled_wire_profile_files

from .opencode_go_test_support import (
    API_KEY,
    CHAT_MODEL,
    CHAT_URL,
    CLOSED_TOOL,
    HELLO,
    MESSAGES_MODEL,
    MESSAGES_URL,
    RESPONSES_COMPLETED,
    RESPONSES_URL,
    catalog_lookup,
    go_adapter,
    go_config,
    go_model,
    go_request,
    success_response,
)


def _mock_all_wires(*, streaming: bool) -> dict[str, respx.Route]:
    return {
        wire: respx.post(url).mock(return_value=success_response(wire, streaming=streaming))
        for wire, url in (
            ("chat", CHAT_URL),
            ("messages", MESSAGES_URL),
            ("responses", RESPONSES_URL),
        )
    }


def test_request_context_is_an_opaque_prompt_cache_affinity() -> None:
    adapter = go_adapter()

    def context(session_id: str, affinity: str | None = None) -> dict[str, Any]:
        return adapter.request_context_kwargs(
            project_id="vbot",
            agent_id="builder",
            session_id=session_id,
            prompt_cache_affinity_id=affinity,
        )

    assert context("source", "shared-lineage") == {OPENCODE_SESSION_ID_KWARG: "vbot-shared-lineage"}
    assert context("fork", "shared-lineage") == context("source", "shared-lineage")
    fallback = context("source")
    assert fallback == context("source")
    assert fallback[OPENCODE_SESSION_ID_KWARG].startswith("vbot-")
    assert "source" not in fallback[OPENCODE_SESSION_ID_KWARG]


@pytest.mark.parametrize("streaming", [False, True], ids=["send", "stream"])
@pytest.mark.parametrize(
    ("wire", "model_id"),
    [
        pytest.param("messages", MESSAGES_MODEL, id="messages"),
        pytest.param("chat", CHAT_MODEL, id="chat"),
        pytest.param("responses", "gpt-5.6-luna", id="responses"),
    ],
)
@pytest.mark.asyncio
async def test_each_wire_follows_the_profile_and_carries_identification_and_affinity(
    wire: str, model_id: str, streaming: bool
) -> None:
    adapter = go_adapter()
    context = adapter.request_context_kwargs(
        project_id="vbot",
        agent_id="builder",
        session_id="session",
        prompt_cache_affinity_id="cache-affinity",
    )

    with respx.mock:
        routes = _mock_all_wires(streaming=streaming)
        await go_request(adapter, model_id, streaming=streaming, **context)

    assert [name for name, route in routes.items() if route.called] == [wire]
    request = routes[wire].calls.last.request
    body = json.loads(request.content)
    assert request.headers[OPENCODE_SESSION_HEADER] == "vbot-cache-affinity"
    assert request.headers["user-agent"] == "vBot"
    assert OPENCODE_SESSION_ID_KWARG not in body
    if wire == "messages":
        assert request.headers["x-api-key"] == API_KEY
        assert request.headers["anthropic-version"] == ANTHROPIC_VERSION
        assert "authorization" not in request.headers
        assert body["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    else:
        assert request.headers["authorization"] == f"Bearer {API_KEY}"
        assert "x-api-key" not in request.headers


@pytest.mark.asyncio
async def test_unlisted_model_takes_the_chat_wire() -> None:
    """An id the wire profile does not route never guesses a native wire."""
    model_id = f"unlisted-{uuid.uuid4().hex}"

    with respx.mock:
        routes = _mock_all_wires(streaming=False)
        await go_adapter().send(HELLO, model_id=model_id)

    assert routes["chat"].call_count == 1
    assert not routes["messages"].called
    assert not routes["responses"].called


@pytest.mark.asyncio
async def test_messages_wire_reads_the_connection_binding_of_the_adapter() -> None:
    """A fact learned on the Adapter's Connection also shapes its Messages wire."""
    observations = WireObservations(None, save_delay=None)
    profiles = WireProfiles(
        files=bundled_wire_profile_files(),
        protocol_support=lambda _provider_id: OpenCodeGoAdapter.WIRE_PROTOCOLS,
        model_resolver=lambda _provider_id, model_id: catalog_lookup(model_id),
        report=lambda issue: None,
        observations=observations,
    )
    adapter = go_adapter()
    adapter.bind_wire_profiles(profiles.bind("opencode-go", "api-key"))
    observations.record_rejected_parameter("opencode-go", "api-key", MESSAGES_MODEL, "top_p")

    with respx.mock:
        route = respx.post(MESSAGES_URL).mock(
            return_value=success_response("messages", streaming=False)
        )
        await adapter.send(HELLO, model_id=MESSAGES_MODEL, top_p=0.5)

    assert "top_p" not in json.loads(route.calls.last.request.content)


@pytest.mark.asyncio
async def test_runtime_base_url_and_connection_reach_every_wire() -> None:
    """The runtime factory's base URL and auth override the config on all three wires."""
    runtime_url = "https://runtime-opencode-go.example/v1"
    adapter = OpenCodeGoAdapter(
        go_config(),
        API_KEY,
        runtime_url,
        AuthConfig(header="X-Runtime-Key", prefix="Key ", credential_key="RUNTIME_OPENCODE_GO_KEY"),
        model_lookup=catalog_lookup,
    )

    with respx.mock:
        routes = {
            wire: respx.post(f"{runtime_url}/{path}").mock(
                return_value=success_response(wire, streaming=False)
            )
            for wire, path in (
                ("chat", "chat/completions"),
                ("messages", "messages"),
                ("responses", "responses"),
            )
        }
        for model_id in (CHAT_MODEL, MESSAGES_MODEL, "gpt-5.6-luna"):
            await adapter.send(HELLO, model_id=model_id)
    await adapter.aclose()

    assert routes["chat"].calls.last.request.headers["x-runtime-key"] == f"Key {API_KEY}"
    assert routes["responses"].calls.last.request.headers["x-runtime-key"] == f"Key {API_KEY}"
    assert routes["messages"].calls.last.request.headers["x-api-key"] == API_KEY


@pytest.mark.parametrize(
    ("wire", "model_id", "url"),
    [
        pytest.param("messages", MESSAGES_MODEL, MESSAGES_URL, id="messages"),
        pytest.param("chat", CHAT_MODEL, CHAT_URL, id="chat"),
        pytest.param("responses", "gpt-5.6-luna", RESPONSES_URL, id="responses"),
    ],
)
@pytest.mark.asyncio
async def test_no_wire_enables_strict_tool_schemas(wire: str, model_id: str, url: str) -> None:
    """Gateway backends reject strict mode; even a large closed Tool set stays non-strict."""
    tools = [{**CLOSED_TOOL, "name": f"inspect_probe_{index}"} for index in range(21)]

    with respx.mock:
        route = respx.post(url).mock(return_value=success_response(wire, streaming=False))
        await go_adapter().send(HELLO, model_id=model_id, tools=tools)

    rendered = json.loads(route.calls.last.request.content)["tools"]
    assert len(rendered) == 21
    if wire == "messages":
        assert rendered[0] == {
            "name": "inspect_probe_0",
            "description": CLOSED_TOOL["description"],
            "input_schema": CLOSED_TOOL["parameters"],
        }
        assert all("strict" not in tool for tool in rendered)
    elif wire == "chat":
        assert all("strict" not in tool["function"] for tool in rendered)
    else:
        # The Responses wire carries the non-strict invariant explicitly.
        assert all(tool["type"] == "function" and tool["strict"] is False for tool in rendered)


def test_wire_media_support_follows_the_routed_wire() -> None:
    adapter = go_adapter()
    chat_adapter = OpenAICompatibleAdapter(go_config(), API_KEY)

    assert adapter.wire_media_support("minimax-m2.7") == IMAGE_WIRE_MEDIA_TYPES
    assert adapter.wire_media_support(CHAT_MODEL) == chat_adapter.wire_media_support(CHAT_MODEL)


_CHAT_SHAPE = {"choices": [{"message": {"role": "assistant", "content": "chat wire"}}]}
_MESSAGES_SHAPE = {
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "messages wire"}],
    "stop_reason": "end_turn",
}
_RESPONSES_SHAPE = {
    "output": [
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "responses wire"}],
        }
    ]
}
_ALL_SHAPES = {**_CHAT_SHAPE, **_MESSAGES_SHAPE, **_RESPONSES_SHAPE}


@pytest.mark.parametrize(
    ("response", "model_id", "content"),
    [
        pytest.param(_ALL_SHAPES, "minimax-m2.7", "messages wire", id="messages-model"),
        pytest.param(_ALL_SHAPES, "grok-4.6", "responses wire", id="responses-model"),
        pytest.param(_ALL_SHAPES, CHAT_MODEL, "chat wire", id="chat-model"),
        pytest.param(_ALL_SHAPES, None, "chat wire", id="choices-shape"),
        pytest.param(RESPONSES_COMPLETED, None, "Done", id="responses-shape"),
        pytest.param(_MESSAGES_SHAPE, None, "messages wire", id="messages-shape"),
    ],
)
def test_normalize_response_follows_the_model_wire_before_the_response_shape(
    response: dict[str, Any], model_id: str | None, content: str
) -> None:
    normalized = go_adapter().normalize_response(response, model_id=model_id)

    assert normalized["role"] == "assistant"
    assert normalized["content"] == content


@pytest.mark.parametrize(
    ("model_id", "context_window", "kwargs", "prompt", "bounds"),
    [
        pytest.param(CHAT_MODEL, 1_000_000, {}, "Write an app.", (384_000, 384_000), id="catalog"),
        pytest.param(
            "deepseek/deepseek-v4-flash",
            1_000_000,
            {},
            "Write an app.",
            (384_000, 384_000),
            id="vendor-prefixed",
        ),
        pytest.param(
            CHAT_MODEL, 1_000_000, {"max_tokens": 2048}, "Short.", (2048, 2048), id="explicit"
        ),
        pytest.param(CHAT_MODEL, 384_000, {}, "x" * 8_000, (1, 383_999), id="clamped-to-context"),
    ],
)
@pytest.mark.asyncio
async def test_output_limit_uses_the_catalog_ceiling_within_the_remaining_context(
    model_id: str,
    context_window: int,
    kwargs: dict[str, Any],
    prompt: str,
    bounds: tuple[int, int],
) -> None:
    catalog = go_model(CHAT_MODEL, context_window=context_window, max_output_tokens=384_000)
    adapter = go_adapter({CHAT_MODEL: catalog}.get, defaults={"max_tokens": 4096})

    with respx.mock:
        route = respx.post(CHAT_URL).mock(return_value=success_response("chat", streaming=False))
        await adapter.send([{"role": "user", "content": prompt}], model_id=model_id, **kwargs)

    max_tokens = json.loads(route.calls.last.request.content)["max_tokens"]
    assert bounds[0] <= max_tokens <= bounds[1]


@pytest.mark.asyncio
async def test_responses_route_budgets_output_against_the_rendered_responses_request() -> None:
    """The Responses wire estimates its own rendered items and clamps the catalog ceiling."""
    model_id = "muse-spark-1.3-contributor"
    catalog = go_model(model_id, context_window=20_000, max_output_tokens=16_000)
    adapter = go_adapter({model_id: catalog}.get)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "Keep answers concise."},
        {"role": "user", "content": "Inspect the repository. " * 2_000},
        {
            "role": "assistant",
            "content": None,
            "reasoning_meta": {
                "response_output": [{"type": "reasoning", "encrypted_content": "opaque"}]
            },
        },
        {"role": "user", "content": "Continue."},
    ]

    estimated = adapter.estimate_request_input_tokens(
        messages, model_id=model_id, tools=[CLOSED_TOOL]
    )
    with respx.mock:
        route = respx.post(RESPONSES_URL).mock(
            return_value=success_response("responses", streaming=False)
        )
        await adapter.send(messages, model_id=model_id, tools=[CLOSED_TOOL])

    assert estimated == estimate_responses_input_tokens(messages, tools=[CLOSED_TOOL])
    max_output = json.loads(route.calls.last.request.content)["max_output_tokens"]
    assert 0 < max_output <= 20_000 - estimated < 16_000


@pytest.mark.asyncio
async def test_aclose_closes_the_shared_client_once() -> None:
    adapter = go_adapter()
    shared_client = AsyncMock()
    adapter._client = shared_client  # noqa: SLF001 - no public observable for client ownership
    adapter._messages._client = shared_client  # noqa: SLF001

    await adapter.aclose()

    shared_client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_responses_wire_sends_a_stateless_request_and_normalizes_the_reply() -> None:
    adapter = go_adapter()

    with respx.mock:
        route = respx.post(RESPONSES_URL).mock(
            return_value=success_response("responses", streaming=False)
        )
        response = await adapter.send(
            [{"role": "system", "content": "Be brief."}, *HELLO],
            model_id="gpt-5.6-luna",
            thinking_effort="high",
            session_id="vbot-session",
        )

    body = json.loads(route.calls.last.request.content)
    # Stateless shape: complete history as input items, never stored.
    assert body["store"] is False
    assert body["instructions"] == "Be brief."
    assert [item["role"] for item in body["input"]] == ["user"]
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}
    assert body["include"] == ["reasoning.encrypted_content"]
    # No sticky-conversation contract: body-level session routing never reaches the wire.
    assert "session_id" not in body
    normalized = adapter.normalize_response(response, model_id="gpt-5.6-luna")
    assert normalized["content"] == "Done"
    assert normalized["phase"] == "final_answer"
    assert normalized["reasoning_meta"]["response_output"] == RESPONSES_COMPLETED["output"]


@pytest.mark.asyncio
async def test_responses_wire_streams_content_replay_state_usage_and_finish() -> None:
    with respx.mock:
        route = respx.post(RESPONSES_URL).mock(
            return_value=success_response("responses", streaming=True)
        )
        deltas = await go_request(
            go_adapter(), "muse-spark-1.3-contributor", streaming=True, thinking_effort="low"
        )

    body = json.loads(route.calls.last.request.content)
    assert body["stream"] is True
    assert body["reasoning"] == {"effort": "low", "summary": "auto"}
    assert [delta["type"] for delta in deltas] == [
        "content_delta",
        "reasoning_meta",
        "usage",
        "finish",
    ]
