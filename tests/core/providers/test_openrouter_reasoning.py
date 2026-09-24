"""Openrouter: reasoning behavior."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ModelRegistry, ReasoningCapabilities
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


@pytest.mark.parametrize(
    ("effort", "expected"),
    [(None, None), ("none", "low"), ("high", "high")],
)
def test_space_bunny_mandatory_reasoning_uses_supported_effort(
    openrouter_config: ProviderConfig, effort: str | None, expected: str | None
) -> None:
    resources = Path(__file__).resolve().parents[3] / "resources"
    registry = ModelRegistry.load(resources)

    def lookup(model_id):
        return registry.get("openrouter", model_id)

    adapter = OpenRouterAdapter(openrouter_config, API_KEY, model_lookup=lookup)

    payload = adapter._build_payload(
        [{"role": "user", "content": "Hello"}],
        "stealth/space-bunny-alpha",
        thinking_effort=effort,
    )
    intent = adapter.describe_reasoning_render(
        model_lookup=lookup, model_id="stealth/space-bunny-alpha", effort=effort
    )

    if expected is None:
        assert "reasoning" not in payload
        assert intent.kind == "default"
    else:
        assert payload["reasoning"] == {"effort": expected}
        assert payload["include_reasoning"] is True
        assert intent.kind == "effort"
        assert intent.effort_level == expected


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


def _zero_reasoning_token_response(**message_fields: Any) -> dict[str, Any]:
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": "ok", **message_fields},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    }


def _openrouter_catalog_adapter(
    openrouter_config: ProviderConfig, *, reasoning_supported: bool
) -> OpenRouterAdapter:
    model = Model(
        model_id="openai/gpt-4o",
        name="GPT-4o",
        capabilities=Capabilities(
            vision=True,
            tools=True,
            json_mode=True,
            reasoning=ReasoningCapabilities(supported=reasoning_supported),
        ),
        context_window=128_000,
        max_output_tokens=16_384,
    )
    return OpenRouterAdapter(openrouter_config, API_KEY, model_lookup={"openai/gpt-4o": model}.get)


@respx.mock
@pytest.mark.asyncio
@pytest.mark.parametrize(("reasoning_supported", "expected_warnings"), [(False, 0), (True, 1)])
async def test_swallowed_effort_warning_follows_rendered_reasoning(
    openrouter_config: ProviderConfig,
    caplog: pytest.LogCaptureFixture,
    reasoning_supported: bool,
    expected_warnings: int,
) -> None:
    """A catalog non-reasoning Model gets no reasoning field, so 0 tokens is expected."""
    adapter = _openrouter_catalog_adapter(
        openrouter_config, reasoning_supported=reasoning_supported
    )
    route = respx.post(OPENROUTER_URL).mock(
        return_value=httpx.Response(200, json=_zero_reasoning_token_response())
    )

    with caplog.at_level(logging.WARNING, logger="vbot.providers.openai_compatible"):
        await adapter.send(
            [{"role": "user", "content": "Hello"}],
            model_id="openai/gpt-4o",
            thinking_effort="high",
        )

    request_body = json.loads(route.calls.last.request.content)
    assert ("reasoning" in request_body) is reasoning_supported
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == expected_warnings
    if expected_warnings:
        assert "rendered_reasoning=high" in warnings[0].getMessage()


@respx.mock
@pytest.mark.asyncio
async def test_zero_reasoning_counter_with_returned_reasoning_does_not_warn(
    openrouter_config: ProviderConfig, caplog: pytest.LogCaptureFixture
) -> None:
    """OpenRouter can report 0 reasoning tokens while returning Reasoning."""
    adapter = _openrouter_catalog_adapter(openrouter_config, reasoning_supported=True)
    respx.post(OPENROUTER_URL).mock(
        return_value=httpx.Response(
            200,
            json=_zero_reasoning_token_response(
                reasoning="Thinking it through.",
                reasoning_details=[{"type": "reasoning.text", "text": "Thinking it through."}],
            ),
        )
    )

    with caplog.at_level(logging.WARNING, logger="vbot.providers.openai_compatible"):
        await adapter.send(
            [{"role": "user", "content": "Hello"}],
            model_id="openai/gpt-4o",
            thinking_effort="high",
        )

    assert [record for record in caplog.records if record.levelno == logging.WARNING] == []


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
