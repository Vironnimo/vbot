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
from core.providers.anthropic import AnthropicAdapter
from core.providers.github_copilot import GitHubCopilotAdapter
from core.providers.kimi import KimiAdapter
from core.providers.minimax import MINIMAX_M3_MODEL_ID, MiniMaxAdapter
from core.providers.ollama import OllamaAdapter
from core.providers.openai import OpenAIAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.openrouter import OpenRouterAdapter
from core.providers.reasoning import (
    REASONING_INTENT_BUDGET,
    REASONING_INTENT_DEFAULT,
    REASONING_INTENT_EFFORT,
    REASONING_INTENT_OFF,
    REASONING_INTENT_ON,
    ReasoningIntent,
)
from core.providers.stepfun import StepFunAdapter
from core.providers.xai import XAIAdapter

from .adapter_test_support import bearer_config


def _model(
    model_id: str,
    *,
    control: str | None = REASONING_CONTROL_ON_OFF,
    levels: tuple[str, ...] = (),
    budget_max: int | None = None,
    metadata: dict[str, object] | None = None,
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
        metadata=metadata or {},
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
        # The generic wire spells off only as the ``none`` effort, which an
        # on_off Model's ladder lacks: nothing is sent.
        pytest.param(
            OpenAICompatibleAdapter,
            _ON_OFF,
            "none",
            ReasoningIntent(REASONING_INTENT_DEFAULT),
            id="generic-on-off-none-sends-nothing",
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


@pytest.mark.parametrize(
    ("adapter_class", "provider_id", "record", "effort", "expected"),
    [
        # M3's render is the binary adaptive switch; no level is sent.
        pytest.param(
            MiniMaxAdapter,
            "minimax",
            _model(MINIMAX_M3_MODEL_ID),
            "high",
            ReasoningIntent(REASONING_INTENT_ON),
            id="minimax-m3-on",
        ),
        pytest.param(
            MiniMaxAdapter,
            "minimax",
            _model(MINIMAX_M3_MODEL_ID),
            "none",
            ReasoningIntent(REASONING_INTENT_OFF),
            id="minimax-m3-off",
        ),
        # M2.x reasons on every request and takes no reasoning control.
        pytest.param(
            MiniMaxAdapter,
            "minimax",
            _model("MiniMax-M2.7", control=None),
            "none",
            ReasoningIntent(REASONING_INTENT_ON),
            id="minimax-m2-always-on",
        ),
        # The Platform cannot disable K3 thinking: off is sent as the low effort.
        pytest.param(
            KimiAdapter,
            "kimi",
            _model("kimi-k3", control=REASONING_CONTROL_LEVELS, levels=("low", "high", "max")),
            "none",
            ReasoningIntent(REASONING_INTENT_EFFORT, effort_level="low"),
            id="kimi-k3-platform-off-is-low",
        ),
        # Step 3.5 Flash takes no reasoning_effort at all.
        pytest.param(
            StepFunAdapter,
            "stepfun",
            _model("step-3.5-flash", control=None),
            "high",
            ReasoningIntent(REASONING_INTENT_DEFAULT),
            id="stepfun-flash-sends-nothing",
        ),
        # Adaptive-only Claude Models cannot disable thinking: off sends nothing.
        pytest.param(
            AnthropicAdapter,
            "anthropic",
            _model(
                "claude-adaptive-only",
                control=REASONING_CONTROL_LEVELS,
                levels=("low", "medium", "high"),
                metadata={"anthropic": {"requires_adaptive_thinking": True}},
            ),
            "none",
            ReasoningIntent(REASONING_INTENT_DEFAULT),
            id="anthropic-adaptive-only-off-sends-nothing",
        ),
        # OpenRouter toggles ``reasoning.enabled``; the effort never reaches it.
        pytest.param(
            OpenRouterAdapter,
            "openrouter",
            _ON_OFF,
            "high",
            ReasoningIntent(REASONING_INTENT_ON),
            id="openrouter-on-off-toggles",
        ),
        # xhigh ties between high and max; the lower rank wins so the render never
        # silently increases cost beyond the selection.
        pytest.param(
            OpenRouterAdapter,
            "openrouter",
            _model("ladder-model", control=REASONING_CONTROL_LEVELS, levels=("low", "high", "max")),
            "xhigh",
            ReasoningIntent(REASONING_INTENT_EFFORT, effort_level="high"),
            id="openrouter-levels-snaps-lower-on-tie",
        ),
        # Copilot's Haiku 4.5 on Messages takes adaptive thinking without an effort,
        # although the catalog reports a thinking budget.
        pytest.param(
            GitHubCopilotAdapter,
            "github-copilot",
            _model(
                "claude-haiku-4.5",
                control=REASONING_CONTROL_BUDGET,
                budget_max=32_000,
                metadata={
                    "github_copilot": {
                        "vendor": "Anthropic",
                        "supported_endpoints": ["/chat/completions", "/v1/messages"],
                    }
                },
            ),
            "high",
            ReasoningIntent(REASONING_INTENT_ON),
            id="copilot-haiku-adaptive-on",
        ),
        # GPT-6.1 Sol has no none rung; its profile spells off as the low effort.
        pytest.param(
            OpenAIAdapter,
            "openai",
            _model(
                "gpt-6.1-sol",
                control=REASONING_CONTROL_LEVELS,
                levels=("low", "medium", "high", "xhigh", "max"),
            ),
            "none",
            ReasoningIntent(REASONING_INTENT_EFFORT, effort_level="low"),
            id="openai-responses-off-is-low",
        ),
        # An xAI Model without a none rung cannot disable reasoning: off is its lowest rung.
        pytest.param(
            XAIAdapter,
            "xai",
            _model("grok-4.5", control=REASONING_CONTROL_LEVELS, levels=("low", "medium", "high")),
            "none",
            ReasoningIntent(REASONING_INTENT_EFFORT, effort_level="low"),
            id="xai-off-is-lowest-rung",
        ),
    ],
)
def test_profile_driven_wires_describe_the_providers_wire_profile(
    adapter_class: type[ProviderAdapter],
    provider_id: str,
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
        provider_config=bearer_config(provider_id),
    )

    assert intent == expected
