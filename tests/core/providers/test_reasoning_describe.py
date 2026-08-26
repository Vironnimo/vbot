"""Tests for the adapter render descriptions behind /status thinking-effort.

``ProviderAdapter.describe_reasoning_render`` is the wire-truthful seam
``/status`` reports from: each adapter describes what a request with the
selected effort would actually carry. Every test here pins a describe result
against the adapter's real render decision — either by rendering a payload
through the adapter directly or by mirroring a render contract that existing
request tests already pin on the wire.
"""

from __future__ import annotations

from typing import Any

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
from core.providers.ollama import OLLAMA_CLOUD_MODE, OllamaAdapter, OllamaCloudAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.openrouter import OpenRouterAdapter
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.reasoning import (
    REASONING_INTENT_BUDGET,
    REASONING_INTENT_DEFAULT,
    REASONING_INTENT_EFFORT,
    REASONING_INTENT_OFF,
    REASONING_INTENT_ON,
)


def _model(
    model_id: str = "glm-5.3-flash",
    *,
    control: str | None = REASONING_CONTROL_ON_OFF,
    levels: tuple[str, ...] = (),
    budget_max: int | None = None,
    supported: bool = True,
) -> Model:
    return Model(
        model_id=model_id,
        name=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=False,
            reasoning=ReasoningCapabilities(
                supported=supported,
                control=control,
                levels=levels,
                budget_max=budget_max,
            ),
        ),
        context_window=1_048_576,
        max_output_tokens=None,
    )


def _lookup(record: Model):
    def model_lookup(model_id: str) -> Model | None:
        if model_id == record.model_id:
            return record
        return None

    return model_lookup


def _cloud_config() -> ProviderConfig:
    return ProviderConfig(
        id="ollama-cloud",
        name="Ollama Cloud",
        adapter="ollama_cloud",
        base_url="https://ollama.com",
        models_endpoint="/api/tags",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API key",
                auth=AuthConfig(
                    header="Authorization", prefix="Bearer ", credential_key="OLLAMA_API_KEY"
                ),
                mode=OLLAMA_CLOUD_MODE,
            )
        ],
    )


# ---------------------------------------------------------------------------
# Generic OpenAI-compatible wire: on_off-declared models still get the level
# ---------------------------------------------------------------------------


def test_generic_compatible_describes_level_for_on_off_model() -> None:
    """The generic render sends the snapped effort even for an on_off Model."""

    intent = OpenAICompatibleAdapter.describe_reasoning_render(
        model_lookup=_lookup(_model()),
        model_id="glm-5.3-flash",
        effort="high",
    )

    assert intent.kind == REASONING_INTENT_EFFORT
    assert intent.effort_level == "high"


def test_generic_compatible_describes_snapped_level_for_unknown_ladder() -> None:
    """Without a feed ladder the generic floor ladder (low/medium/high) applies."""

    intent = OpenAICompatibleAdapter.describe_reasoning_render(
        model_lookup=_lookup(_model()),
        model_id="glm-5.3-flash",
        effort="xhigh",
    )

    assert intent.kind == REASONING_INTENT_EFFORT
    assert intent.effort_level == "high"


def test_generic_compatible_describes_budget_degraded_to_effort() -> None:
    """The generic wire has no budget field: a budget intent renders the level."""

    intent = OpenAICompatibleAdapter.describe_reasoning_render(
        model_lookup=_lookup(
            _model(
                model_id="budget-model",
                control=REASONING_CONTROL_BUDGET,
                budget_max=100_000,
            )
        ),
        model_id="budget-model",
        effort="medium",
    )

    assert intent.kind == REASONING_INTENT_EFFORT
    assert intent.effort_level == "medium"


def test_generic_compatible_describes_none_as_off() -> None:
    intent = OpenAICompatibleAdapter.describe_reasoning_render(
        model_lookup=_lookup(_model()),
        model_id="glm-5.3-flash",
        effort="none",
    )

    assert intent.kind == REASONING_INTENT_OFF


def test_generic_compatible_describes_default_without_effort() -> None:
    intent = OpenAICompatibleAdapter.describe_reasoning_render(
        model_lookup=_lookup(_model()),
        model_id="glm-5.3-flash",
        effort=None,
    )

    assert intent.kind == REASONING_INTENT_DEFAULT


# ---------------------------------------------------------------------------
# Ollama Cloud — the reported case: xhigh maps to max on the Cloud wire
# ---------------------------------------------------------------------------


def test_ollama_cloud_describes_max_for_xhigh_on_off_model() -> None:
    """glm-5.3-flash declares on_off but the Cloud wire carries the effort."""

    intent = OllamaCloudAdapter.describe_reasoning_render(
        model_lookup=_lookup(_model()),
        model_id="glm-5.3-flash",
        effort="xhigh",
        provider_config=_cloud_config(),
    )

    assert intent.kind == REASONING_INTENT_EFFORT
    assert intent.effort_level == "max"


@pytest.mark.parametrize(
    ("effort", "expected"),
    [("low", "low"), ("medium", "medium"), ("high", "high"), ("max", "max")],
)
def test_ollama_cloud_describes_each_selected_level(effort: str, expected: str) -> None:
    intent = OllamaCloudAdapter.describe_reasoning_render(
        model_lookup=_lookup(_model()),
        model_id="glm-5.3-flash",
        effort=effort,
        provider_config=_cloud_config(),
    )

    assert intent.kind == REASONING_INTENT_EFFORT
    assert intent.effort_level == expected


def test_ollama_cloud_describes_off_for_none() -> None:
    intent = OllamaCloudAdapter.describe_reasoning_render(
        model_lookup=_lookup(_model()),
        model_id="glm-5.3-flash",
        effort="none",
        provider_config=_cloud_config(),
    )

    assert intent.kind == REASONING_INTENT_OFF


def test_ollama_cloud_describe_agrees_with_wire_render() -> None:
    """The description matches what ``_apply_reasoning`` puts on the wire."""

    adapter = OllamaCloudAdapter(
        _cloud_config(),
        "ollama-secret",
        model_lookup=_lookup(_model()),
        connection_mode=OLLAMA_CLOUD_MODE,
    )

    for effort in ("xhigh", "high", "medium", "low"):
        payload: dict[str, Any] = {}
        adapter._apply_reasoning(payload, {"thinking_effort": effort}, "glm-5.3-flash")
        intent = OllamaCloudAdapter.describe_reasoning_render(
            model_lookup=_lookup(_model()),
            model_id="glm-5.3-flash",
            effort=effort,
            provider_config=_cloud_config(),
        )
        assert intent.kind == REASONING_INTENT_EFFORT
        assert payload["reasoning_effort"] == intent.effort_level


def test_ollama_cloud_describe_agrees_with_wire_off() -> None:
    adapter = OllamaCloudAdapter(
        _cloud_config(),
        "ollama-secret",
        model_lookup=_lookup(_model()),
        connection_mode=OLLAMA_CLOUD_MODE,
    )

    payload: dict[str, Any] = {}
    adapter._apply_reasoning(payload, {"thinking_effort": "none"}, "glm-5.3-flash")
    intent = OllamaCloudAdapter.describe_reasoning_render(
        model_lookup=_lookup(_model()),
        model_id="glm-5.3-flash",
        effort="none",
        provider_config=_cloud_config(),
    )

    assert payload["reasoning_effort"] == "none"
    assert intent.kind == REASONING_INTENT_OFF


# ---------------------------------------------------------------------------
# Binary-toggle wires keep on/off — no effort level is reported
# ---------------------------------------------------------------------------


def test_openrouter_describes_toggle_for_on_off_model() -> None:
    """OpenRouter toggles ``reasoning.enabled``; the effort never reaches it."""

    intent = OpenRouterAdapter.describe_reasoning_render(
        model_lookup=_lookup(_model(model_id="toggle-model")),
        model_id="toggle-model",
        effort="high",
    )

    assert intent.kind == REASONING_INTENT_ON


def test_openrouter_describes_level_for_levels_model() -> None:
    record = _model(
        model_id="ladder-model",
        control=REASONING_CONTROL_LEVELS,
        levels=("low", "high", "max"),
    )
    intent = OpenRouterAdapter.describe_reasoning_render(
        model_lookup=_lookup(record),
        model_id="ladder-model",
        effort="xhigh",
    )

    # xhigh ties between high and max on this ladder; the lower rank wins so the
    # render never silently increases cost beyond the selection.
    assert intent.kind == REASONING_INTENT_EFFORT
    assert intent.effort_level == "high"


def test_native_ollama_describes_toggle_for_on_off_model() -> None:
    """The native ``think`` control is a boolean: the report stays on/off."""

    intent = OllamaAdapter.describe_reasoning_render(
        model_lookup=_lookup(_model(model_id="thinking-model")),
        model_id="thinking-model",
        effort="high",
    )

    assert intent.kind == REASONING_INTENT_ON


def test_native_ollama_describes_level_for_levels_model() -> None:
    record = _model(
        model_id="gpt-oss:20b",
        control=REASONING_CONTROL_LEVELS,
        levels=("low", "medium", "high"),
    )
    intent = OllamaAdapter.describe_reasoning_render(
        model_lookup=_lookup(record),
        model_id="gpt-oss:20b",
        effort="high",
    )

    assert intent.kind == REASONING_INTENT_EFFORT
    assert intent.effort_level == "high"


# ---------------------------------------------------------------------------
# Budget and always-on wires
# ---------------------------------------------------------------------------


def test_base_default_describes_budget_tokens() -> None:
    """A native-budget wire reports the rendered token budget."""

    intent = ProviderAdapter.describe_reasoning_render(
        model_lookup=_lookup(
            _model(
                model_id="budget-model",
                control=REASONING_CONTROL_BUDGET,
                budget_max=32_000,
            )
        ),
        model_id="budget-model",
        effort="high",
    )

    assert intent.kind == REASONING_INTENT_BUDGET
    assert intent.budget_tokens == 24_000


def test_minimax_openai_wire_describes_toggle_for_m3() -> None:
    """M3's render is the binary adaptive switch — no level is sent."""

    intent = MiniMaxAdapter.describe_reasoning_render(
        model_lookup=_lookup(_model(model_id=MINIMAX_M3_MODEL_ID, control=None)),
        model_id=MINIMAX_M3_MODEL_ID,
        effort="high",
    )

    assert intent.kind == REASONING_INTENT_ON


def test_minimax_openai_wire_describes_on_for_m2() -> None:
    """M2.x reasons by default and takes no reasoning control."""

    intent = MiniMaxAdapter.describe_reasoning_render(
        model_lookup=_lookup(_model(model_id="MiniMax-M2.7", control=None)),
        model_id="MiniMax-M2.7",
        effort="high",
    )

    assert intent.kind == REASONING_INTENT_ON


def test_minimax_messages_wire_describes_on() -> None:
    """The Anthropic-compatible M2.x wire strips every reasoning control."""

    intent = _MiniMaxMessagesAdapter.describe_reasoning_render(
        model_lookup=_lookup(_model(model_id="MiniMax-M2.7")),
        model_id="MiniMax-M2.7",
        effort="high",
    )

    assert intent.kind == REASONING_INTENT_ON
