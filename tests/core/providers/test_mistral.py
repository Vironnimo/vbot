"""Mistral: catalog, binary reasoning wire, content-chunk replay, and streaming."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.errors import CatalogEntrySkipped
from core.providers.mistral import MistralAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

API_KEY = "test-mistral-key"
CHAT_URL = "https://api.mistral.ai/v1/chat/completions"
CHAT_SUCCESS = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
}
HELLO = [{"role": "user", "content": "Hello"}]
ABSENT = object()

CONFIG = ProviderConfig(
    id="mistral",
    name="Mistral AI",
    adapter="mistral",
    base_url="https://api.mistral.ai/v1",
    connections=[
        ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(
                header="Authorization", prefix="Bearer ", credential_key="MISTRAL_API_KEY"
            ),
        )
    ],
    defaults={"max_tokens": 8192},
)


def _catalog_model(
    model_id: str,
    *,
    reasoning: bool = True,
    levels: tuple[str, ...] = (),
    metadata: dict[str, Any] | None = None,
) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(
                supported=reasoning, control="levels" if levels else None, levels=levels
            ),
        ),
        context_window=128000,
        max_output_tokens=8192,
        metadata=metadata or {},
    )


CATALOG = {
    "mistral-medium-latest": _catalog_model("mistral-medium-latest", reasoning=False),
    "mistral-medium-3-5": _catalog_model("mistral-medium-3-5", levels=("low", "medium", "high")),
    # The magistral-medium reasoning mode is a published per-Model wire fact.
    "magistral-medium-latest": _catalog_model(
        "magistral-medium-latest", metadata={"mistral": {"prompt_mode": "reasoning"}}
    ),
    "magistral-small-latest": _catalog_model("magistral-small-latest"),
}


def _adapter(*, catalog: bool = False) -> MistralAdapter:
    return MistralAdapter(CONFIG, API_KEY, model_lookup=CATALOG.get if catalog else None)


def _sse(*chunks: dict[str, Any]) -> httpx.Response:
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def _raw_model(
    *,
    model_id: str = "mistral-large-latest",
    completion_chat: bool = True,
    function_calling: bool = True,
    reasoning: bool = False,
    vision: bool = True,
    archived: bool = False,
    max_context_length: int | None = 128000,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "id": model_id,
        "name": "Mistral Large",
        "capabilities": {
            "completion_chat": completion_chat,
            "function_calling": function_calling,
            "reasoning": reasoning,
            "vision": vision,
        },
        "archived": archived,
    }
    if max_context_length is not None:
        raw["max_context_length"] = max_context_length
    return raw


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_chat_catalog_entry_maps_capabilities() -> None:
    model = MistralAdapter.normalize_catalog_entry(_raw_model(), {"max_tokens": 8192})

    assert model == Model(
        model_id="mistral-large-latest",
        name="Mistral Large",
        capabilities=Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=False),
            input_modalities=("text", "image"),
            output_modalities=("text",),
            supported_parameters=("response_format", "tools"),
            task_types=("chat", "text_output", "image_input", "image_understanding"),
        ),
        context_window=128000,
        # Unknown output limits stay unknown instead of adopting the Provider default.
        max_output_tokens=None,
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(
            # The capability flag decides, not a magistral name prefix.
            _raw_model(model_id="mistral-small-2603", reasoning=True),
            {"reasoning": True, "parameters": ("reasoning", "response_format", "tools")},
            id="reasoning-flag",
        ),
        pytest.param(
            _raw_model(function_calling=False, vision=False),
            {"tools": False, "json_mode": True, "input": ("text",)},
            id="text-only-chat-keeps-json-mode",
        ),
        pytest.param(
            _raw_model(max_context_length=None),
            {"context_window": None},
            id="missing-context-window-stays-unknown",
        ),
    ],
)
def test_catalog_entry_reads_capability_flags(
    raw: dict[str, Any], expected: dict[str, Any]
) -> None:
    model = MistralAdapter.normalize_catalog_entry(raw, {"max_tokens": 8192})

    actual = {
        "reasoning": model.capabilities.reasoning.supported,
        "parameters": model.capabilities.supported_parameters,
        "tools": model.capabilities.tools,
        "json_mode": model.capabilities.json_mode,
        "input": model.capabilities.input_modalities,
        "context_window": model.context_window,
    }
    assert {key: actual[key] for key in expected} == expected


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(_raw_model(completion_chat=False), id="non-chat"),
        pytest.param(_raw_model(archived=True), id="archived"),
    ],
)
def test_catalog_skips_non_chat_and_archived_models(raw: dict[str, Any]) -> None:
    with pytest.raises(CatalogEntrySkipped):
        MistralAdapter.normalize_catalog_entry(raw, {"max_tokens": 8192})


# ---------------------------------------------------------------------------
# Reasoning wire
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("catalog", "model_id", "effort", "reasoning_effort", "prompt_mode"),
    [
        # Without catalog data the binary {none, high} floor applies.
        pytest.param(False, "mistral-large-latest", "minimal", "high", ABSENT, id="minimal"),
        pytest.param(False, "mistral-large-latest", "medium", "high", ABSENT, id="medium"),
        pytest.param(False, "mistral-large-latest", "max", "high", ABSENT, id="max"),
        pytest.param(False, "mistral-large-latest", "none", "none", ABSENT, id="none"),
        pytest.param(False, "mistral-large-latest", None, ABSENT, ABSENT, id="no-effort"),
        pytest.param(False, "magistral-small-latest", "high", "high", ABSENT, id="no-catalog"),
        # A feed ladder snaps first; any active snapped effort still engages thinking.
        pytest.param(True, "mistral-medium-3-5", "medium", "high", ABSENT, id="feed-ladder"),
        pytest.param(
            True, "magistral-medium-latest", "high", ABSENT, "reasoning", id="prompt-mode"
        ),
        pytest.param(
            True, "magistral-medium-latest", "none", ABSENT, ABSENT, id="prompt-mode-none"
        ),
        pytest.param(
            True, "magistral-small-latest", "high", "high", ABSENT, id="reasoning-without-fact"
        ),
        pytest.param(
            True, "mistral-medium-latest", "high", ABSENT, ABSENT, id="reasoning-unsupported"
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_reasoning_is_a_binary_toggle_on_the_model_specific_field(
    catalog: bool,
    model_id: str,
    effort: str | None,
    reasoning_effort: Any,
    prompt_mode: Any,
) -> None:
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=CHAT_SUCCESS))
    kwargs = {} if effort is None else {"thinking_effort": effort}

    await _adapter(catalog=catalog).send(HELLO, model_id=model_id, **kwargs)

    body = json.loads(route.calls.last.request.content)
    for key, value in (("reasoning_effort", reasoning_effort), ("prompt_mode", prompt_mode)):
        if value is ABSENT:
            assert key not in body, key
        else:
            assert body[key] == value, key


# ---------------------------------------------------------------------------
# Completed responses and replay
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("content", "extra", "expected_content", "expected_reasoning", "keeps_chunks"),
    [
        pytest.param(
            "plain string",
            {"thinking": "Reasoning trace"},
            "plain string",
            "Reasoning trace",
            False,
            id="string-content-with-thinking-field",
        ),
        pytest.param(
            [{"type": "thinking", "thinking": "ThinkA"}, {"type": "text", "text": "AnswerA"}],
            {},
            "AnswerA",
            "ThinkA",
            True,
            id="typed-legacy-string-thinking",
        ),
        pytest.param(
            [
                {
                    "type": "thinking",
                    "thinking": [
                        {"type": "text", "text": "Step one. "},
                        {"type": "text", "text": "Step two."},
                    ],
                    "closed": True,
                },
                {"type": "text", "text": "39"},
                {"type": "text", "text": "1"},
            ],
            {},
            "391",
            "Step one. Step two.",
            True,
            id="typed-nested-thinking-and-multiple-texts",
        ),
    ],
)
def test_completed_response_separates_text_and_reasoning(
    content: Any,
    extra: dict[str, Any],
    expected_content: str,
    expected_reasoning: str,
    keeps_chunks: bool,
) -> None:
    normalized = _adapter().normalize_response(
        {"choices": [{"message": {"role": "assistant", "content": content, **extra}}]}
    )

    assert normalized["content"] == expected_content
    assert normalized["reasoning"] == expected_reasoning
    if keeps_chunks:
        assert normalized["reasoning_meta"]["content_chunks"] == content


CAPTURED_CHUNKS = [
    {"type": "thinking", "thinking": [{"type": "text", "text": "Step one."}], "closed": True},
    {"type": "text", "text": "Answer"},
]


@pytest.mark.parametrize(
    ("assistant", "wire_content"),
    [
        pytest.param(
            {"role": "assistant", "content": "391", "reasoning": "17*23 = 391"},
            "391",
            id="readable-reasoning-is-never-rebuilt-into-chunks",
        ),
        pytest.param(
            {
                "role": "assistant",
                "content": "A reconstructed answer",
                "reasoning": "A flattened trace",
                "reasoning_meta": {"content_chunks": CAPTURED_CHUNKS},
            },
            CAPTURED_CHUNKS,
            id="captured-chunks-replay-verbatim",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_history_replays_only_captured_content_chunks(
    assistant: dict[str, Any], wire_content: Any
) -> None:
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=CHAT_SUCCESS))

    await _adapter().send(
        [{"role": "user", "content": "Q"}, assistant, {"role": "user", "content": "Next"}],
        model_id="mistral-large-latest",
    )

    wire = json.loads(route.calls.last.request.content)["messages"][1]
    assert wire["content"] == wire_content
    assert "17*23" not in json.dumps(wire)


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        pytest.param(
            [{"choices": [{"delta": {"content": "Hi"}}]}],
            [{"type": "content_delta", "text": "Hi"}],
            id="string-content",
        ),
        pytest.param(
            [{"choices": [{"delta": {"thinking": "Reasoning delta"}}]}],
            [{"type": "reasoning_delta", "text": "Reasoning delta"}],
            id="string-thinking-field",
        ),
        pytest.param(
            [
                {
                    "choices": [{"delta": {"content": [{"type": "text", "text": "Text1"}]}}],
                    "usage": {"prompt_tokens": 21, "completion_tokens": 8},
                }
            ],
            [
                {"type": "content_delta", "text": "Text1"},
                {"type": "usage", "input_tokens": 21, "output_tokens": 8},
            ],
            id="typed-chunks-without-finish-emit-no-meta",
        ),
        pytest.param(
            [
                {
                    "choices": [
                        {
                            "delta": {
                                "content": [
                                    {"type": "thinking", "thinking": "Think1"},
                                    {"type": "text", "text": "Text1"},
                                ]
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 34, "completion_tokens": 13},
                }
            ],
            [
                {"type": "reasoning_delta", "text": "Think1"},
                {"type": "content_delta", "text": "Text1"},
                {
                    "type": "reasoning_meta",
                    "reasoning_meta": {
                        "content_chunks": [
                            {"type": "thinking", "thinking": "Think1"},
                            {"type": "text", "text": "Text1"},
                        ]
                    },
                },
                {"type": "finish", "reason": "stop"},
                {"type": "usage", "input_tokens": 34, "output_tokens": 13},
            ],
            id="typed-chunks-then-meta-finish-and-usage-in-order",
        ),
        pytest.param(
            [
                {
                    "choices": [
                        {"delta": {"content": [{"type": "thinking", "thinking": "Need a tool."}]}}
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_123",
                                        "function": {"name": "status", "arguments": "{}"},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
            ],
            [
                {"type": "reasoning_delta", "text": "Need a tool."},
                {
                    "type": "tool_call_delta",
                    "slot": 0,
                    "name_delta": "status",
                    "arguments_delta": "{}",
                    "id": "call_123",
                },
                {
                    "type": "reasoning_meta",
                    "reasoning_meta": {
                        "content_chunks": [{"type": "thinking", "thinking": "Need a tool."}]
                    },
                },
                {"type": "finish", "reason": "tool_calls"},
            ],
            id="typed-thinking-keeps-following-tool-call-deltas",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_stream_normalizes_string_and_typed_deltas(
    chunks: list[dict[str, Any]], expected: list[dict[str, Any]]
) -> None:
    route = respx.post(CHAT_URL).mock(return_value=_sse(*chunks))

    deltas = [delta async for delta in _adapter().stream(HELLO, model_id="mistral-large-latest")]

    assert deltas == expected
    request = json.loads(route.calls.last.request.content)
    assert request["stream"] is True
    assert request["stream_options"] == {"include_usage": True}
