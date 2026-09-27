"""OpenRouter Reasoning: mandatory-Reasoning Models, reasoning_details replay, the
zero-Reasoning warning, and newline-run noise in visible Reasoning."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core.models.models import ModelRegistry
from tests.core.providers.openrouter_test_support import (
    CHAT_SUCCESS,
    CHAT_URL,
    HELLO,
    RESPONSES_MODEL,
    RESPONSES_URL,
    catalog_lookup,
    catalog_model,
    chat_sse,
    openrouter_adapter,
    responses_sse,
    sent_body,
)

SPACE_BUNNY = "stealth/space-bunny-alpha"


@pytest.mark.parametrize(
    ("effort", "expected"),
    [
        pytest.param(None, None, id="omitted-keeps-provider-default"),
        pytest.param("none", "low", id="off-becomes-cheapest-supported-effort"),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_mandatory_reasoning_model_never_renders_off(
    effort: str | None, expected: str | None
) -> None:
    registry = ModelRegistry.load(Path(__file__).resolve().parents[3] / "resources")

    def lookup(model_id: str) -> Any:
        return registry.get("openrouter", model_id)

    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=CHAT_SUCCESS))
    adapter = openrouter_adapter(lookup)

    await adapter.send(HELLO, model_id=SPACE_BUNNY, thinking_effort=effort)

    body = sent_body(route)
    intent = adapter.describe_reasoning_render(
        model_lookup=lookup, model_id=SPACE_BUNNY, effort=effort
    )
    if expected is None:
        assert "reasoning" not in body
        assert intent.kind == "default"
    else:
        assert body["reasoning"] == {"effort": expected}
        assert body["include_reasoning"] is True
        assert intent.kind == "effort"
        assert intent.effort_level == expected


@respx.mock
@pytest.mark.asyncio
async def test_in_run_replay_echoes_reasoning_details_unchanged() -> None:
    """Gemini upstreams reject an in-run Tool continuation without its reasoning_details."""

    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=CHAT_SUCCESS))
    reasoning_details = [
        {"type": "reasoning.encrypted", "data": "enc-signature-blob"},
        {"type": "reasoning.text", "text": "step one", "signature": "sig-1"},
    ]

    await openrouter_adapter().send(
        [
            {"role": "user", "content": "Use the tool"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_1", "name": "lookup", "arguments": {"q": "x"}}],
                "reasoning_meta": {"reasoning_details": reasoning_details},
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ],
        model_id="google/gemini-2.5-pro",
    )

    assert sent_body(route)["messages"][1]["reasoning_details"] == reasoning_details


@pytest.mark.parametrize(
    ("reasoning_supported", "returned_reasoning", "expected_warnings"),
    [
        pytest.param(False, {}, 0, id="catalog-non-reasoning-model-expects-zero"),
        pytest.param(True, {}, 1, id="rendered-effort-swallowed"),
        pytest.param(
            True,
            {
                "reasoning": "Thinking it through.",
                "reasoning_details": [{"type": "reasoning.text", "text": "Thinking it through."}],
            },
            0,
            id="zero-counter-with-returned-reasoning",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_zero_reasoning_token_warning_follows_rendered_and_returned_reasoning(
    caplog: pytest.LogCaptureFixture,
    reasoning_supported: bool,
    returned_reasoning: dict[str, Any],
    expected_warnings: int,
) -> None:
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok", **returned_reasoning},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "completion_tokens_details": {"reasoning_tokens": 0},
                },
            },
        )
    )
    adapter = openrouter_adapter(
        catalog_lookup(catalog_model("openai/gpt-4o", reasoning=reasoning_supported))
    )

    with caplog.at_level(logging.WARNING, logger="vbot.providers.openai_compatible"):
        await adapter.send(HELLO, model_id="openai/gpt-4o", thinking_effort="high")

    assert ("reasoning" in sent_body(route)) is reasoning_supported
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == expected_warnings
    if expected_warnings:
        assert "rendered_reasoning=high" in warnings[0].getMessage()


@pytest.mark.parametrize(
    ("wire", "fragments", "expected"),
    [
        pytest.param(
            "chat",
            ["first\n\n\n\n\n\n\n\n\nsecond"],
            ["first\n\nsecond"],
            id="interior-run-collapses-to-paragraph-break",
        ),
        pytest.param(
            "chat", ["para one\n\npara two"], ["para one\n\npara two"], id="paragraph-break-kept"
        ),
        pytest.param(
            "chat", ["word\n", "\n\n\nnext"], ["word\n", "\nnext"], id="run-across-deltas-bounded"
        ),
        pytest.param(
            "chat",
            ["word\n\n", "\n\n\n", "\nnext"],
            ["word\n\n", "next"],
            id="noise-only-delta-dropped-without-resetting-the-run",
        ),
        pytest.param(
            "responses", ["word\n", "\n\n\nnext"], ["word\n", "\nnext"], id="responses-stream"
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_streamed_reasoning_collapses_newline_runs_and_leaves_content(
    wire: str, fragments: list[str], expected: list[str]
) -> None:
    if wire == "chat":
        model_id = "stealth/ox-alpha"
        respx.post(CHAT_URL).mock(
            return_value=chat_sse(
                *({"choices": [{"delta": {"reasoning": fragment}}]} for fragment in fragments),
                {"choices": [{"delta": {"content": "answer"}, "finish_reason": "stop"}]},
            )
        )
    else:
        model_id = RESPONSES_MODEL
        completed = {
            "id": "resp_reasoning",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "answer"}],
                }
            ],
        }
        respx.post(RESPONSES_URL).mock(
            return_value=responses_sse(
                *(("response.reasoning.delta", {"delta": fragment}) for fragment in fragments),
                ("response.output_text.delta", {"delta": "answer"}),
                ("response.completed", {"response": completed}),
            )
        )

    deltas = [delta async for delta in openrouter_adapter().stream(HELLO, model_id=model_id)]

    assert [d["text"] for d in deltas if d["type"] == "reasoning_delta"] == expected
    assert [d["text"] for d in deltas if d["type"] == "content_delta"] == ["answer"]


def test_completed_response_collapses_reasoning_newline_runs() -> None:
    normalized = openrouter_adapter().normalize_response(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "ok",
                        "reasoning": "why\n\n\n\n\n\n\n\n\nbecause",
                    },
                    "finish_reason": "stop",
                }
            ]
        },
        model_id="stealth/ox-alpha",
    )

    assert normalized["reasoning"] == "why\n\nbecause"
