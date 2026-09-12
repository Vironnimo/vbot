"""Openrouter: reasoning behavior."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers._openrouter_constants import (
    _REASONING_TRAILING_NEWLINES_STATE_KEY,
)
from core.providers._openrouter_policy import (
    _collapse_reasoning_newline_runs,
)
from core.providers.openrouter import (
    OpenRouterAdapter,
)
from core.providers.providers import ProviderConfig
from tests.core.providers.openrouter_helpers import (
    API_KEY,
    OPENROUTER_URL,
    SUCCESS_RESPONSE,
)
from tests.core.providers.openrouter_helpers import (
    openrouter_adapter as openrouter_adapter,
)
from tests.core.providers.openrouter_helpers import (
    openrouter_config as openrouter_config,
)


def test_reasoning_replay_policy_is_model_specific(
    openrouter_config: ProviderConfig,
) -> None:
    models = {
        "google/gemini-2.5-pro": Model(
            model_id="google/gemini-2.5-pro",
            name="Gemini",
            capabilities=Capabilities(
                vision=True,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True),
            ),
            context_window=1_000_000,
            max_output_tokens=64_000,
            reasoning_replay="none",
        ),
        "openai/gpt-4o": Model(
            model_id="openai/gpt-4o",
            name="GPT-4o",
            capabilities=Capabilities(
                vision=True,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=False),
            ),
            context_window=128_000,
            max_output_tokens=16_384,
            reasoning_replay="current_run",
        ),
    }
    adapter = OpenRouterAdapter(
        openrouter_config,
        API_KEY,
        model_lookup=models.get,
    )

    assert adapter.reasoning_replay_policy("google/gemini-2.5-pro") == "none"
    assert adapter.reasoning_replay_policy("openai/gpt-4o") == "current_run"
    assert adapter.reasoning_replay_policy("unknown/new-model") == "full_history"


@respx.mock
@pytest.mark.asyncio
async def test_in_run_round_trips_reasoning_details(
    openrouter_adapter: OpenRouterAdapter,
) -> None:
    """In-run replay must echo reasoning_details unchanged (Gemini upstreams 400 without it)."""
    route = respx.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))
    reasoning_details = [
        {"type": "reasoning.encrypted", "data": "enc-signature-blob"},
        {"type": "reasoning.text", "text": "step one", "signature": "sig-1"},
    ]
    history: list[dict[str, Any]] = [
        {"role": "user", "content": "Use the tool"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "name": "lookup", "arguments": {"q": "x"}}],
            "reasoning_meta": {"reasoning_details": reasoning_details},
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "result"},
    ]

    await openrouter_adapter.send(history, model_id="google/gemini-2.5-pro")

    request_body = json.loads(route.calls.last.request.content)
    assistant_message = request_body["messages"][1]
    assert assistant_message["reasoning_details"] == reasoning_details


def test_collapse_reasoning_newline_runs_collapses_interior_runs() -> None:
    # Arrange
    text = "first\n\n\n\n\n\n\n\n\nsecond"

    # Act
    collapsed = _collapse_reasoning_newline_runs(text, None)

    # Assert
    assert collapsed == "first\n\nsecond"


def test_collapse_reasoning_newline_runs_keeps_paragraph_breaks() -> None:
    # Arrange
    text = "para one\n\npara two"

    # Act
    collapsed = _collapse_reasoning_newline_runs(text, None)

    # Assert
    assert collapsed == text


def test_collapse_reasoning_newline_runs_bounds_cross_delta_run() -> None:
    # Arrange
    state: dict[str, Any] = {}

    # Act
    first = _collapse_reasoning_newline_runs("word\n", state)
    second = _collapse_reasoning_newline_runs("\n\n\nnext", state)

    # Assert
    assert first == "word\n"
    assert second == "\nnext"
    assert "".join([first, second]).endswith("word\n\nnext")


def test_collapse_reasoning_newline_runs_empty_fragment_keeps_trailing_state() -> None:
    # Arrange
    state: dict[str, Any] = {}
    _collapse_reasoning_newline_runs("word\n\n", state)
    assert state[_REASONING_TRAILING_NEWLINES_STATE_KEY] == 2

    # Act
    noise = _collapse_reasoning_newline_runs("\n\n\n", state)

    # Assert
    assert noise == ""
    # The dropped fragment must not reset the emitted stream's trailing run.
    assert state[_REASONING_TRAILING_NEWLINES_STATE_KEY] == 2
    after = _collapse_reasoning_newline_runs("\nnext", state)
    assert after == "next"


def test_normalize_stream_chunk_collapses_reasoning_noise_only(
    openrouter_adapter: OpenRouterAdapter,
) -> None:
    # Arrange
    raw_chunk = {
        "choices": [
            {
                "delta": {
                    "content": "answer",
                    "reasoning": "This\n\n\n\n\n\n\n\n\n trace",
                },
            }
        ]
    }
    state: dict[str, Any] = {}

    # Act
    deltas = openrouter_adapter._normalize_stream_chunk(raw_chunk, set(), state)

    # Assert
    reasoning = [delta["text"] for delta in deltas if delta.get("type") == "reasoning_delta"]
    content = [delta["text"] for delta in deltas if delta.get("type") == "content_delta"]
    assert reasoning == ["This\n\n trace"]
    assert content == ["answer"]


def test_normalize_response_collapses_reasoning_newline_runs(
    openrouter_adapter: OpenRouterAdapter,
) -> None:
    """Non-streaming responses get the same newline-run collapse."""

    # Arrange
    raw_response = {
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
    }

    # Act
    response = openrouter_adapter.normalize_response(raw_response, model_id="stealth/ox-alpha")

    # Assert
    assert response["reasoning"] == "why\n\nbecause"
