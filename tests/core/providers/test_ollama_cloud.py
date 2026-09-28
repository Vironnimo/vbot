"""Direct Ollama Cloud ``/v1/chat/completions`` wire: request shape, reasoning effort,
the whole-request byte limit, Usage, replay carriers, and the bundled profiles."""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import httpx
import pytest
import respx
from PIL import Image

from core.attachments.images import ImageConverter
from core.chat.block_resolver import ContentBlockResolver
from core.chat.messages import ChatMessage
from core.providers.errors import ProviderRequestTooLargeError
from core.providers.ollama import OllamaCloudAdapter
from core.providers.providers import ProviderRegistry
from core.providers.reasoning import (
    REASONING_INTENT_DEFAULT,
    REASONING_INTENT_EFFORT,
    REASONING_INTENT_OFF,
    ReasoningIntent,
)
from tests.core.chat.assistant_turn_test_support import (
    assistant_turn_from_response,
    request_history,
)
from tests.core.providers.ollama_test_support import (
    CLOUD_CHAT_URL,
    CLOUD_CONFIG,
    CLOUD_TEXT_RESPONSE,
    RESOURCES,
    SAMPLE_MESSAGES,
    bundled_cloud_adapter,
    cloud_adapter,
    cloud_sse,
    model_lookup,
    sent_body,
)

V41 = "deepseek-v4.1-flash"

WEATHER_TOOL = {
    "name": "get_weather",
    "description": "Test weather tool",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
}


@respx.mock
@pytest.mark.asyncio
async def test_request_uses_the_openai_compatible_shape_on_the_exact_cloud_route() -> None:
    route = respx.post(CLOUD_CHAT_URL).mock(
        return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
    )
    connection = CLOUD_CONFIG.get_connection("api-key")
    # An explicit compatible base must not double the /v1 segment.
    adapter = cloud_adapter(CLOUD_CONFIG, "https://ollama.com/v1/", connection.auth)

    await adapter.send(
        [
            *SAMPLE_MESSAGES,
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_weather", "name": "get_weather", "arguments": {"city": "Berlin"}}
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_weather",
                "name": "get_weather",
                "content": '{"temperature": 24}',
            },
        ],
        model_id="minimax-m3",
        thinking_effort="high",
        top_p=0.95,
    )

    payload = sent_body(route)
    assert route.calls.last.request.headers["Authorization"] == "Bearer ollama-secret"
    assert payload["messages"][-2]["tool_calls"][0]["function"]["arguments"] == '{"city":"Berlin"}'
    assert payload["messages"][-1]["tool_call_id"] == "call_weather"
    # Generic compatible parameters ride at the top level, not under native ``options``.
    assert payload["top_p"] == 0.95
    await adapter.aclose()


_EFFORT = REASONING_INTENT_EFFORT
_OFF = ReasoningIntent(REASONING_INTENT_OFF)


@pytest.mark.parametrize(
    ("model_id", "effort", "wire_effort", "described"),
    [
        pytest.param(
            "minimax-m3",
            "medium",
            "medium",
            ReasoningIntent(_EFFORT, effort_level="medium"),
            id="supported-level",
        ),
        pytest.param(
            "deepseek-v4-flash",
            "xhigh",
            "max",
            ReasoningIntent(_EFFORT, effort_level="max"),
            id="xhigh-normalizes-to-max",
        ),
        pytest.param(
            "thinking-model",
            "high",
            "high",
            ReasoningIntent(_EFFORT, effort_level="high"),
            id="on-off-model-sends-level",
        ),
        pytest.param(
            "thinking-model", "none", "none", _OFF, id="explicit-off-switch-for-on-off-model"
        ),
        pytest.param("plain-model", "high", None, _OFF, id="non-thinking-model"),
        # The description of an unconfirmed Model is not pinned: it still reports
        # the effort although the wire omits it until the catalog confirms support.
        pytest.param(
            "new-unenriched-model", "high", None, None, id="unknown-support-until-catalog"
        ),
        pytest.param(
            "minimax-m3",
            "future-tier",
            None,
            ReasoningIntent(REASONING_INTENT_DEFAULT),
            id="unknown-effort-omitted-not-rejected",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_reasoning_effort_renders_only_confirmed_cloud_vocabulary(
    model_id: str, effort: str, wire_effort: str | None, described: ReasoningIntent | None
) -> None:
    """The sent effort and the ``/status`` description of the same selection agree."""

    route = respx.post(CLOUD_CHAT_URL).mock(
        return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
    )
    adapter = cloud_adapter()

    await adapter.send(SAMPLE_MESSAGES, model_id=model_id, thinking_effort=effort)

    payload = sent_body(route)
    assert payload.get("reasoning_effort") == wire_effort
    assert ("reasoning_effort" in payload) is (wire_effort is not None)
    if described is not None:
        assert (
            OllamaCloudAdapter.describe_reasoning_render(
                model_lookup=model_lookup,
                model_id=model_id,
                effort=effort,
                provider_config=CLOUD_CONFIG,
            )
            == described
        )
    await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("delta", [-1, 0, 1])
async def test_request_body_limit_counts_exact_wire_bytes_before_io(
    streaming: bool, delta: int
) -> None:
    adapter = cloud_adapter()
    limit = adapter.request_body_limit("glm-5.3-flash")
    assert limit == 16 * 1024 * 1024
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": 'Grüße 😀 "\\\n'},
                {"type": "media", "media_type": "image/png", "base64": ""},
            ],
        }
    ]
    kwargs = {
        "max_tokens": 1,
        "tools": [
            {
                "name": "test",
                "description": "Tool ä",
                "parameters": {"type": "object", "properties": {}},
            }
        ],
    }
    payload = adapter._build_payload(messages, "glm-5.3-flash", **kwargs)
    if streaming:
        adapter._prepare_stream_payload(payload)
    overhead = len(httpx.Request("POST", CLOUD_CHAT_URL, json=payload).content)
    messages[0]["content"][1]["base64"] = "A" * (limit + delta - overhead)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(CLOUD_CHAT_URL).mock(
            return_value=(
                cloud_sse({"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]})
                if streaming
                else httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
            )
        )

        async def invoke() -> None:
            if streaming:
                _ = [
                    chunk
                    async for chunk in adapter.stream(messages, model_id="glm-5.3-flash", **kwargs)
                ]
            else:
                await adapter.send(messages, model_id="glm-5.3-flash", **kwargs)

        try:
            if delta > 0:
                with pytest.raises(ProviderRequestTooLargeError) as failure:
                    await invoke()
                assert failure.value.size_bytes == limit + delta
                assert failure.value.max_bytes == limit
                assert failure.value.retryable is False
                assert route.call_count == 0
            else:
                await invoke()
                assert route.call_count == 1
                assert len(route.calls.last.request.content) == limit + delta
        finally:
            await adapter.aclose()


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        pytest.param(
            {"prompt_tokens": 0, "completion_tokens": 9, "total_tokens": 9},
            {"output_tokens": 9},
            id="impossible-zero-input-dropped",
        ),
        pytest.param(
            {"prompt_tokens": 2975, "completion_tokens": 25, "total_tokens": 3000},
            {"input_tokens": 2975, "output_tokens": 25},
            id="positive-input-preserved",
        ),
    ],
)
def test_completed_response_keeps_reasoning_and_measured_usage(
    usage: dict[str, int], expected: dict[str, int]
) -> None:
    normalized = cloud_adapter().normalize_response(
        {**CLOUD_TEXT_RESPONSE, "usage": usage}, model_id="minimax-m3"
    )

    assert normalized["reasoning"] == "The user requested exactly OK."
    assert normalized["usage"] == expected


@respx.mock
@pytest.mark.asyncio
async def test_stream_surfaces_reasoning_and_omits_zero_input_usage() -> None:
    route = respx.post(CLOUD_CHAT_URL).mock(
        return_value=cloud_sse(
            {"choices": [{"delta": {"reasoning": "Check."}}]},
            {"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]},
            {
                "choices": [],
                "usage": {"prompt_tokens": 0, "completion_tokens": 22, "total_tokens": 0},
            },
        )
    )
    adapter = cloud_adapter()

    deltas = [
        delta
        async for delta in adapter.stream(
            SAMPLE_MESSAGES, model_id="minimax-m3", thinking_effort="high"
        )
    ]

    assert {"type": "reasoning_delta", "text": "Check."} in deltas
    assert {"type": "content_delta", "text": "OK"} in deltas
    assert {"type": "usage", "output_tokens": 22} in deltas
    payload = sent_body(route)
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    await adapter.aclose()


_OLD_REASONING = "EXACT old Reasoning: äöü\nline two\n"


@pytest.mark.parametrize(
    ("model_id", "prior_response", "carrier"),
    [
        pytest.param("minimax-m3", None, "reasoning", id="verified-profile"),
        pytest.param("unprofiled-model", None, "reasoning_content", id="unprofiled-default"),
        pytest.param("unprofiled-model", ("send", "reasoning"), "reasoning", id="sent-scan"),
        pytest.param(
            "unprofiled-model",
            ("send", "reasoning_content"),
            "reasoning_content",
            id="sent-scan-rc",
        ),
        pytest.param("unprofiled-model", ("stream", "reasoning"), "reasoning", id="streamed-scan"),
        pytest.param(
            "unprofiled-model",
            ("stream", "reasoning_content"),
            "reasoning_content",
            id="streamed-scan-rc",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_readable_reasoning_replays_under_exactly_one_carrier(
    model_id: str, prior_response: tuple[str, str] | None, carrier: str
) -> None:
    route = respx.post(CLOUD_CHAT_URL)
    adapter = cloud_adapter()
    if prior_response is not None:
        # This Run's real responses decide an unprofiled Model's carrier.
        mode, field = prior_response
        if mode == "send":
            message = {"role": "assistant", "content": "OK", field: "Check."}
            route.mock(
                return_value=httpx.Response(
                    200,
                    json={
                        **CLOUD_TEXT_RESPONSE,
                        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                    },
                )
            )
            raw = await adapter.send(SAMPLE_MESSAGES, model_id=model_id, thinking_effort="high")
            # Chat normalizes every send() response before the next request.
            adapter.normalize_response(raw, model_id=model_id)
        else:
            route.mock(
                return_value=cloud_sse(
                    {"choices": [{"delta": {field: "Check."}}]},
                    {"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]},
                )
            )
            async for _ in adapter.stream(SAMPLE_MESSAGES, model_id=model_id):
                pass
    route.mock(return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE))

    await adapter.send(
        [
            *SAMPLE_MESSAGES,
            {"role": "assistant", "model": model_id, "content": "OK", "reasoning": _OLD_REASONING},
        ],
        model_id=model_id,
        thinking_effort="high",
    )

    replayed = sent_body(route)["messages"][-1]
    assert replayed[carrier] == _OLD_REASONING
    other = "reasoning" if carrier == "reasoning_content" else "reasoning_content"
    assert other not in replayed
    # Native field only: no reasoning is injected into visible content.
    assert replayed["content"] == "OK"
    await adapter.aclose()


@respx.mock
@pytest.mark.asyncio
async def test_v41_image_profile_converts_gif_before_cloud_request() -> None:
    adapter = bundled_cloud_adapter()
    route = respx.post(CLOUD_CHAT_URL).mock(
        return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
    )
    source = io.BytesIO()
    Image.new("RGB", (12, 8), "red").save(source, format="GIF")
    original = {
        "path": "fixture.gif",
        "filename": "fixture.gif",
        "media_type": "image/gif",
        "base64": base64.b64encode(source.getvalue()).decode("ascii"),
    }
    try:
        supported = adapter.wire_media_support(V41)
        assert supported == frozenset({"image/png", "image/jpeg", "image/webp"})
        assert "image/gif" in adapter.wire_media_support("kimi-k3")
        parts = await ContentBlockResolver.resolve_tool_image(
            original, frozenset({"text", "image"}), supported, ImageConverter()
        )
        assert parts[0]["media_type"] == "image/png"
        assert original["media_type"] == "image/gif"
        with Image.open(io.BytesIO(base64.b64decode(parts[0]["base64"]))) as converted:
            assert converted.size == (12, 8)
            assert converted.convert("RGB").getpixel((0, 0)) == (255, 0, 0)
        await adapter.send([{"role": "user", "content": parts}], model_id=V41)
        assert sent_body(route)["messages"][0]["content"][0]["image_url"]["url"].startswith(
            "data:image/png;base64,"
        )
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    ("current_run", "effort", "wire_effort"),
    [
        pytest.param(False, "low", "low", id="later-run"),
        pytest.param(True, "xhigh", "max", id="current-run-xhigh"),
        pytest.param(True, "none", "none", id="current-run-off"),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_v41_replays_persisted_reasoning_on_fresh_adapter(
    current_run: bool, effort: str, wire_effort: str
) -> None:
    """Protect the locally serialized contract verified live on 2026-09-11."""

    adapter = bundled_cloud_adapter()
    scope = f"ollama-cloud/{V41}::api-key"
    route = respx.post(CLOUD_CHAT_URL).mock(
        return_value=httpx.Response(200, json=CLOUD_TEXT_RESPONSE)
    )
    # Simulate the normalized response persisted by a previous Adapter/Run.
    assistant = assistant_turn_from_response(
        f"ollama-cloud/{V41}",
        {
            "content": None,
            "reasoning": "test-owned reasoning sentinel",
            "reasoning_meta": {"reasoning_details": [{"text": "opaque sentinel"}]},
            "tool_calls": [
                {"id": "call_weather", "name": "get_weather", "arguments": {"city": "Berlin"}}
            ],
        },
        reasoning_scope=scope,
    )
    restored = ChatMessage.from_dict(json.loads(json.dumps(assistant.to_dict())))
    policy = adapter.reasoning_replay_policy(V41)
    assert policy == "full_history"
    assert adapter.reasoning_replay_fidelity(V41) == "readable_only"
    question = ChatMessage.user("Check Berlin weather.")
    result = ChatMessage.tool(tool_call_id="call_weather", name="get_weather", content="20 C")
    if current_run:
        messages = request_history(
            [question], replay_policy=policy, agent_model=scope, current_turn=restored
        )
        messages.append(result.to_dict())
    else:
        messages = request_history(
            [
                question,
                restored,
                result,
                ChatMessage.assistant(model=f"ollama-cloud/{V41}", content="20 C in Berlin."),
                ChatMessage.user("Check again."),
            ],
            replay_policy=policy,
            agent_model=scope,
        )
    try:
        await adapter.send(messages, model_id=V41, thinking_effort=effort, tools=[WEATHER_TOOL])
        payload = sent_body(route)
        replayed = payload["messages"][1]
        assert payload["model"] == V41
        assert payload["reasoning_effort"] == wire_effort
        assert payload["max_tokens"] == 393_216
        assert payload["tools"][0]["function"]["name"] == "get_weather"
        assert replayed["reasoning"] == "test-owned reasoning sentinel"
        assert "reasoning_content" not in replayed
        assert "reasoning_details" not in replayed
        assert "reasoning_scope" not in replayed
        assert "opaque sentinel" not in json.dumps(payload)
        assert replayed["tool_calls"][0]["id"] == payload["messages"][2]["tool_call_id"]
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "request_kwargs",
    [
        pytest.param({}, id="bundled-default-without-catalog-ceiling"),
        pytest.param({"max_tokens": 512}, id="explicit-caller-limit-wins"),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_output_limit_always_reaches_the_cloud_payload(
    request_kwargs: dict[str, Any],
) -> None:
    """Without max_tokens the Cloud compat layer truncates at an internal ~128 tokens."""

    config = ProviderRegistry.load(RESOURCES).get("ollama-cloud")
    ceiling = request_kwargs.get("max_tokens", (config.defaults or {}).get("max_tokens"))
    assert isinstance(ceiling, int)
    route = respx.post(CLOUD_CHAT_URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "Hi"}}]}
        )
    )
    # plain-model has no catalog output ceiling.
    adapter = cloud_adapter(config)

    await adapter.send(SAMPLE_MESSAGES, model_id="plain-model", **request_kwargs)

    max_tokens = sent_body(route).get("max_tokens")
    assert isinstance(max_tokens, int)
    assert 0 < max_tokens <= ceiling
    await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        pytest.param(
            "*** Begin Patch\n*** Update File: file.txt\n@@\n summary();\n*** End Patch",
            id="observed-empty-update",
        ),
        pytest.param(
            '*** Update File: file.txt\n@@\n-old = "quoted";\n+new = "\\n\\t";\n context\n',
            id="real-change-with-escapes",
        ),
    ],
)
async def test_patch_body_survives_cloud_response_and_chat_ingestion(patch: str) -> None:
    # The observed empty Update arrived as a complete function call. Neither that
    # attempt nor a real change may be repaired or trimmed in transit.
    adapter = cloud_adapter()
    raw = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "patch-call",
                            "type": "function",
                            "function": {
                                "name": "apply_patch",
                                "arguments": json.dumps({"patch": patch}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }

    response = adapter.normalize_response(raw, model_id=V41)
    message = assistant_turn_from_response(f"ollama-cloud/{V41}", response)
    restored = ChatMessage.from_dict(json.loads(json.dumps(message.to_dict())))

    assert restored.tool_calls is not None and len(restored.tool_calls) == 1
    assert restored.tool_calls[0].arguments == {"patch": patch}
    await adapter.aclose()
