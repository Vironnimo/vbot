"""Tests for the adapter render descriptions behind /status thinking-effort.

``ProviderAdapter.describe_reasoning_render`` is the wire-truthful seam
``/status`` reports from: each adapter describes what a request with the
selected effort would actually carry. Each row pins a describe result against a
render contract that the adapter's request tests pin on the wire; Ollama Cloud's
description is checked against its sent body in ``test_ollama_cloud.py``.
"""

from __future__ import annotations

import pytest

from core.models.models import (
    REASONING_CONTROL_BUDGET,
    REASONING_CONTROL_LEVELS,
    REASONING_CONTROL_ON_OFF,
    Capabilities,
    Model,
    ReasoningCapabilities,
)
from core.providers.adapter import ProviderAdapter
from core.providers.minimax import MINIMAX_M3_MODEL_ID, MiniMaxAdapter, _MiniMaxMessagesAdapter
from core.providers.ollama import OllamaAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.openrouter import OpenRouterAdapter
from core.providers.reasoning import (
    REASONING_INTENT_BUDGET,
    REASONING_INTENT_EFFORT,
    REASONING_INTENT_OFF,
    REASONING_INTENT_ON,
    ReasoningIntent,
)


def _model(
    model_id: str,
    *,
    control: str | None = REASONING_CONTROL_ON_OFF,
    levels: tuple[str, ...] = (),
    budget_max: int | None = None,
) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=False,
            reasoning=ReasoningCapabilities(
                supported=True,
                control=control,
                levels=levels,
                budget_max=budget_max,
            ),
        ),
        context_window=1_048_576,
        max_output_tokens=None,
    )


_ON_OFF = _model("toggle-model")
_BUDGET_100K = _model("budget-model", control=REASONING_CONTROL_BUDGET, budget_max=100_000)


@pytest.mark.parametrize(
    ("adapter_class", "record", "effort", "expected"),
    [
        # The generic wire sends the snapped effort even for an on_off Model, and
        # without a feed ladder it snaps against its low/medium/high floor.
        pytest.param(
            OpenAICompatibleAdapter,
            _ON_OFF,
            "xhigh",
            ReasoningIntent(REASONING_INTENT_EFFORT, effort_level="high"),
            id="generic-on-off-sends-floor-level",
        ),
        # The generic wire has no budget field: a budget intent renders the level.
        pytest.param(
            OpenAICompatibleAdapter,
            _BUDGET_100K,
            "medium",
            ReasoningIntent(REASONING_INTENT_EFFORT, effort_level="medium"),
            id="generic-budget-degrades-to-level",
        ),
        pytest.param(
            OpenAICompatibleAdapter,
            _ON_OFF,
            "none",
            ReasoningIntent(REASONING_INTENT_OFF),
            id="generic-none-is-off",
        ),
        # OpenRouter toggles ``reasoning.enabled``; the effort never reaches it.
        pytest.param(
            OpenRouterAdapter,
            _ON_OFF,
            "high",
            ReasoningIntent(REASONING_INTENT_ON, effort_level="high"),
            id="openrouter-on-off-toggles",
        ),
        # xhigh ties between high and max; the lower rank wins so the render never
        # silently increases cost beyond the selection.
        pytest.param(
            OpenRouterAdapter,
            _model("ladder-model", control=REASONING_CONTROL_LEVELS, levels=("low", "high", "max")),
            "xhigh",
            ReasoningIntent(REASONING_INTENT_EFFORT, effort_level="high"),
            id="openrouter-levels-snaps-lower-on-tie",
        ),
        # The native ``think`` control is a boolean for on_off Models.
        pytest.param(
            OllamaAdapter,
            _ON_OFF,
            "high",
            ReasoningIntent(REASONING_INTENT_ON),
            id="native-ollama-on-off-toggles",
        ),
        pytest.param(
            OllamaAdapter,
            _model(
                "gpt-oss:20b", control=REASONING_CONTROL_LEVELS, levels=("low", "medium", "high")
            ),
            "high",
            ReasoningIntent(REASONING_INTENT_EFFORT, effort_level="high"),
            id="native-ollama-levels",
        ),
        # A native-budget wire reports the rendered token budget.
        pytest.param(
            ProviderAdapter,
            _model("budget-model", control=REASONING_CONTROL_BUDGET, budget_max=32_000),
            "high",
            ReasoningIntent(REASONING_INTENT_BUDGET, budget_tokens=24_000),
            id="base-budget-tokens",
        ),
        # M3's render is the binary adaptive switch; no level is sent.
        pytest.param(
            MiniMaxAdapter,
            _model(MINIMAX_M3_MODEL_ID, control=None),
            "high",
            ReasoningIntent(REASONING_INTENT_ON),
            id="minimax-m3-on",
        ),
        pytest.param(
            MiniMaxAdapter,
            _model(MINIMAX_M3_MODEL_ID, control=None),
            "none",
            ReasoningIntent(REASONING_INTENT_OFF),
            id="minimax-m3-off",
        ),
        # M2.x reasons by default and takes no reasoning control.
        pytest.param(
            MiniMaxAdapter,
            _model("MiniMax-M2.7", control=None),
            "high",
            ReasoningIntent(REASONING_INTENT_ON),
            id="minimax-m2-always-on",
        ),
        # The Anthropic-compatible M2.x wire strips every reasoning control.
        pytest.param(
            _MiniMaxMessagesAdapter,
            _model("MiniMax-M2.7"),
            "high",
            ReasoningIntent(REASONING_INTENT_ON),
            id="minimax-messages-always-on",
        ),
    ],
)
def test_describe_reasoning_render_reports_what_the_wire_carries(
    adapter_class: type[ProviderAdapter],
    record: Model,
    effort: str,
    expected: ReasoningIntent,
) -> None:
    def model_lookup(model_id: str) -> Model | None:
        return record if model_id == record.model_id else None

    intent = adapter_class.describe_reasoning_render(
        model_lookup=model_lookup,
        model_id=record.model_id,
        effort=effort,
    )

    assert intent == expected
