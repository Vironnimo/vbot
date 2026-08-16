"""Tests for model-recommended temperature fallback in the chat layer.

When an agent has no explicit temperature (``None``), the chat layer should
fall back to the model's ``recommended_temperature`` from the Model DB. An
explicit agent temperature always wins.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.chat.model_resolution import resolve_request_temperature
from tests.core.chat.chat_loop_support import (
    StubAdapter,
    StubAgent,
    StubModels,
    StubRuntime,
    build_chat_loop,
)

JsonObject = dict[str, Any]


class TestResolveChatTemperature:
    def test_agent_temperature_wins_over_model_recommendation(self):
        models = StubModels(
            {("ollama-cloud", "glm-5.2"): 200000},
            recommended_temperatures={("ollama-cloud", "glm-5.2"): 1.0},
        )
        result = resolve_request_temperature(0.1, models, "ollama-cloud", "glm-5.2")
        assert result == 0.1

    def test_none_agent_temperature_uses_model_recommendation(self):
        models = StubModels(
            {("ollama-cloud", "glm-5.2"): 200000},
            recommended_temperatures={("ollama-cloud", "glm-5.2"): 1.0},
        )
        result = resolve_request_temperature(None, models, "ollama-cloud", "glm-5.2")
        assert result == 1.0

    def test_none_agent_and_no_recommendation_yields_none(self):
        models = StubModels({("openai", "gpt-5.2"): 128000})
        result = resolve_request_temperature(None, models, "openai", "gpt-5.2")
        assert result is None

    def test_zero_agent_temperature_is_respected(self):
        """``0.0`` is a real value, not None — it must win over a recommendation."""
        models = StubModels(
            {("ollama-cloud", "glm-5.2"): 200000},
            recommended_temperatures={("ollama-cloud", "glm-5.2"): 1.0},
        )
        result = resolve_request_temperature(0.0, models, "ollama-cloud", "glm-5.2")
        assert result == 0.0

    def test_empty_provider_id_yields_none(self):
        models = StubModels({})
        result = resolve_request_temperature(None, models, "", "glm-5.2")
        assert result is None

    def test_unknown_model_yields_none(self):
        models = StubModels({})
        result = resolve_request_temperature(None, models, "ollama-cloud", "unknown")
        assert result is None


@pytest.mark.asyncio
async def test_model_recommended_temperature_reaches_adapter(tmp_path: Path) -> None:
    """When the agent temperature is None, the model's recommended temperature
    is what the adapter sees on the wire."""

    agent = StubAgent(
        id="coder",
        model="ollama-cloud/glm-5.2",
        allowed_tools=["*"],
        temperature=None,  # type: ignore[arg-type]
    )
    adapter = StubAdapter([{"content": "Hi", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        models=StubModels(
            {("ollama-cloud", "glm-5.2"): 200000},
            recommended_temperatures={("ollama-cloud", "glm-5.2"): 1.0},
        ),
    )

    await build_chat_loop(runtime).send("coder", "Hello", session_id="session-one")

    assert adapter.requests[0]["kwargs"]["temperature"] == 1.0


@pytest.mark.asyncio
async def test_agent_temperature_still_wins_on_wire(tmp_path: Path) -> None:
    """When the agent has an explicit temperature, it wins over the model
    recommendation — the model fact is a fallback only."""

    agent = StubAgent(
        id="coder",
        model="ollama-cloud/glm-5.2",
        allowed_tools=["*"],
        temperature=0.1,
    )
    adapter = StubAdapter([{"content": "Hi", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        models=StubModels(
            {("ollama-cloud", "glm-5.2"): 200000},
            recommended_temperatures={("ollama-cloud", "glm-5.2"): 1.0},
        ),
    )

    await build_chat_loop(runtime).send("coder", "Hello", session_id="session-one")

    assert adapter.requests[0]["kwargs"]["temperature"] == 0.1


@pytest.mark.asyncio
async def test_none_temperature_with_no_recommendation_sends_none(tmp_path: Path) -> None:
    """When neither agent nor model sets a temperature, None reaches the adapter
    so the provider-config default or API default applies."""

    agent = StubAgent(
        id="coder",
        model="openai/gpt-5.2",
        allowed_tools=["*"],
        temperature=None,  # type: ignore[arg-type]
    )
    adapter = StubAdapter([{"content": "Hi", "reasoning": None, "tool_calls": None}])
    runtime: Any = StubRuntime(
        data_dir=tmp_path,
        agent=agent,
        adapter=adapter,
        models=StubModels({("openai", "gpt-5.2"): 128000}),
    )

    await build_chat_loop(runtime).send("coder", "Hello", session_id="session-one")

    assert adapter.requests[0]["kwargs"]["temperature"] is None
