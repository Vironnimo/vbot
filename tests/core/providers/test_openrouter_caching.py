"""Openrouter: caching behavior."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from core.providers._openrouter_policy import (
    _is_claude_family,
)
from core.providers.openrouter import (
    OpenRouterAdapter,
)
from tests.core.providers.openrouter_helpers import (
    OPENROUTER_URL,
    SUCCESS_RESPONSE,
)
from tests.core.providers.openrouter_helpers import (
    openrouter_adapter as openrouter_adapter,
)
from tests.core.providers.openrouter_helpers import (
    openrouter_config as openrouter_config,
)

# Prompt caching — envelope-layout cache_control for Claude-family models
EPHEMERAL = {"type": "ephemeral"}


def _marked_parts(payload: dict) -> list[dict]:
    """Every content part in the message array carrying a cache_control marker."""
    marked: list[dict] = []
    for message in payload.get("messages", []):
        content = message.get("content")
        if isinstance(content, list):
            marked += [p for p in content if isinstance(p, dict) and "cache_control" in p]
    return marked


def test_is_claude_family_matches_only_claude() -> None:
    assert _is_claude_family("anthropic/claude-sonnet-4.6")
    assert _is_claude_family("~anthropic/claude-haiku-latest")
    assert _is_claude_family("anthropic/CLAUDE-opus-4-1")
    assert not _is_claude_family("openai/gpt-5.2")
    assert not _is_claude_family("google/gemini-2.5-pro")


class TestOpenRouterPromptCaching:
    @respx.mock
    @pytest.mark.asyncio
    async def test_claude_system_message_gets_envelope_marker(
        self, openrouter_adapter: OpenRouterAdapter
    ) -> None:
        """A string system prompt becomes a content-part list with a marker."""
        route = respx.post(OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await openrouter_adapter.send(
            [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hi"},
            ],
            model_id="anthropic/claude-sonnet-4.6",
        )

        messages = json.loads(route.calls.last.request.content)["messages"]
        assert messages[0]["content"] == [
            {"type": "text", "text": "You are helpful.", "cache_control": EPHEMERAL}
        ]

    @respx.mock
    @pytest.mark.asyncio
    async def test_non_claude_model_gets_no_markers(
        self, openrouter_adapter: OpenRouterAdapter
    ) -> None:
        """OpenAI/Gemini cache implicitly — no cache_control is injected."""
        route = respx.post(OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )

        await openrouter_adapter.send(
            [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hi"},
            ],
            model_id="openai/gpt-5.2",
        )

        payload = json.loads(route.calls.last.request.content)
        assert payload["messages"][0]["content"] == "You are helpful."
        assert _marked_parts(payload) == []

    @respx.mock
    @pytest.mark.asyncio
    async def test_never_exceeds_four_breakpoints(
        self, openrouter_adapter: OpenRouterAdapter
    ) -> None:
        """System + rolling markers stay within Anthropic's four-breakpoint limit."""
        route = respx.post(OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        history = [{"role": "system", "content": "Sys"}]
        for index in range(8):
            role = "user" if index % 2 == 0 else "assistant"
            history.append({"role": role, "content": f"m{index}"})

        await openrouter_adapter.send(history, model_id="anthropic/claude-sonnet-4.6")

        assert len(_marked_parts(json.loads(route.calls.last.request.content))) == 4

    @respx.mock
    @pytest.mark.asyncio
    async def test_marks_three_most_recent_non_system_messages(
        self, openrouter_adapter: OpenRouterAdapter
    ) -> None:
        """Beyond the system marker, only the last three messages carry a marker."""
        route = respx.post(OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        history = [{"role": "system", "content": "Sys"}]
        for index in range(6):
            role = "user" if index % 2 == 0 else "assistant"
            history.append({"role": role, "content": f"m{index}"})

        await openrouter_adapter.send(history, model_id="anthropic/claude-sonnet-4.6")

        messages = json.loads(route.calls.last.request.content)["messages"]
        marked = [
            isinstance(m["content"], list) and "cache_control" in m["content"][-1] for m in messages
        ]
        # system (idx 0) + last three (idx 4,5,6); idx 1,2,3 unmarked.
        assert marked == [True, False, False, False, True, True, True]

    @respx.mock
    @pytest.mark.asyncio
    async def test_skips_pure_tool_call_assistant_turn(
        self, openrouter_adapter: OpenRouterAdapter
    ) -> None:
        """An assistant turn with no content (pure tool_calls) cannot carry a
        marker, so the breakpoint rolls to an older message instead."""
        route = respx.post(OPENROUTER_URL).mock(
            return_value=httpx.Response(200, json=SUCCESS_RESPONSE)
        )
        history: list[dict[str, Any]] = [
            {"role": "user", "content": "run the tool"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "name": "do_it", "arguments": {"x": 1}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "done"},
        ]

        await openrouter_adapter.send(history, model_id="anthropic/claude-sonnet-4.6")

        messages = json.loads(route.calls.last.request.content)["messages"]
        assistant = next(m for m in messages if m["role"] == "assistant")
        # The pure tool-call assistant turn never becomes a breakpoint carrier.
        assert not (
            isinstance(assistant.get("content"), list)
            and any(isinstance(p, dict) and "cache_control" in p for p in assistant["content"])
        )
        # Both markable turns (user + tool_result) carry the marker.
        assert len(_marked_parts(json.loads(route.calls.last.request.content))) == 2
