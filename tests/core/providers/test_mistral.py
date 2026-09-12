"""Mistral: requests behavior."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.errors import CatalogEntrySkipped
from core.providers.mistral import MistralAdapter
from core.providers.providers import ProviderConfig
from tests.core.providers.mistral_helpers import (
    API_KEY,
    MISTRAL_URL,
    SAMPLE_MESSAGES,
)
from tests.core.providers.mistral_helpers import (
    mistral_adapter as mistral_adapter,
)
from tests.core.providers.mistral_helpers import (
    mistral_config as mistral_config,
)

SUCCESS_RESPONSE = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
}


def _prompt_mode_metadata(model_id: str) -> dict[str, object]:
    """Return ``metadata.mistral.prompt_mode`` data for magistral-medium models.

    Reproduces the wire fact formerly carried for deprecated magistral-medium
    models: reasoning engages via ``prompt_mode`` when model data says so, not
    through a name-prefix guess.
    """

    if model_id.startswith("magistral-medium"):
        return {"mistral": {"prompt_mode": "reasoning"}}
    return {}


@pytest.fixture()
def mistral_adapter_with_reasoning_lookup(mistral_config: ProviderConfig) -> MistralAdapter:
    def _model_lookup(model_id: str) -> Model | None:
        if model_id == "mistral-medium-latest":
            reasoning_supported = False
        elif model_id.startswith("magistral-medium"):
            reasoning_supported = True
        else:
            return None

        return Model(
            model_id=model_id,
            name=model_id,
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=reasoning_supported),
            ),
            context_window=128000,
            max_output_tokens=8192,
            metadata=_prompt_mode_metadata(model_id),
        )

    return MistralAdapter(
        mistral_config,
        API_KEY,
        model_lookup=_model_lookup,
    )


@pytest.fixture()
def mistral_adapter_with_prompt_mode_lookup(mistral_config: ProviderConfig) -> MistralAdapter:
    """A reasoning-capable lookup that carries the magistral ``prompt_mode`` fact.

    Unlike ``mistral_adapter_with_reasoning_lookup`` it reports every queried
    model as reasoning-capable, so the prompt-mode wire branch is exercised
    purely from the ``metadata.mistral.prompt_mode`` data.
    """

    def _model_lookup(model_id: str) -> Model:
        return Model(
            model_id=model_id,
            name=model_id,
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(supported=True),
            ),
            context_window=128000,
            max_output_tokens=8192,
            metadata=_prompt_mode_metadata(model_id),
        )

    return MistralAdapter(mistral_config, API_KEY, model_lookup=_model_lookup)


def raw_mistral_model(
    *,
    model_id: str = "mistral-large-latest",
    name: str = "Mistral Large",
    completion_chat: bool = True,
    function_calling: bool = True,
    reasoning: bool = False,
    vision: bool = True,
    archived: bool = False,
    max_context_length: int | None = 128000,
) -> dict:
    raw = {
        "id": model_id,
        "name": name,
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


def test_reasoning_replay_policy_defaults_to_full_history(
    mistral_adapter_with_prompt_mode_lookup: MistralAdapter,
    mistral_adapter: MistralAdapter,
) -> None:
    """Mistral docs require cross-turn replay; verified accepted against the live API."""
    assert (
        mistral_adapter_with_prompt_mode_lookup.reasoning_replay_policy("mistral-medium-3-5")
        == "full_history"
    )
    assert mistral_adapter.reasoning_replay_policy("unknown-model") == "full_history"


def test_normalize_catalog_entry_maps_chat_model_capabilities() -> None:
    model = MistralAdapter.normalize_catalog_entry(raw_mistral_model(), {"max_tokens": 8192})

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
            task_types=(
                "chat",
                "text_output",
                "image_input",
                "image_understanding",
            ),
        ),
        context_window=128000,
        max_output_tokens=None,
    )


def test_normalize_catalog_entry_marks_magistral_models_as_reasoning_capable() -> None:
    model = MistralAdapter.normalize_catalog_entry(
        raw_mistral_model(
            model_id="magistral-medium-latest",
            name="Magistral Medium",
            reasoning=True,
        ),
        {"max_tokens": 8192},
    )

    assert model.capabilities.reasoning.supported is True


def test_normalize_catalog_entry_marks_non_magistral_reasoning_models_as_reasoning_capable() -> (
    None
):
    model = MistralAdapter.normalize_catalog_entry(
        raw_mistral_model(
            model_id="mistral-small-2603",
            name="mistral-small-2603",
            reasoning=True,
        ),
        {"max_tokens": 8192},
    )

    assert model.capabilities.reasoning.supported is True


def test_normalize_catalog_entry_rejects_non_chat_models() -> None:
    with pytest.raises(CatalogEntrySkipped):
        MistralAdapter.normalize_catalog_entry(
            raw_mistral_model(completion_chat=False),
            {"max_tokens": 8192},
        )


def test_normalize_catalog_entry_rejects_archived_models() -> None:
    with pytest.raises(CatalogEntrySkipped):
        MistralAdapter.normalize_catalog_entry(
            raw_mistral_model(archived=True),
            {"max_tokens": 8192},
        )


def test_normalize_catalog_entry_leaves_missing_context_window_unknown() -> None:
    # A missing max_context_length is honestly unknown (None), not a fake 0
    # (Phase 6); the read-side default chain fills it at use time.
    model = MistralAdapter.normalize_catalog_entry(
        raw_mistral_model(max_context_length=None),
        {"max_tokens": 8192},
    )

    assert model.context_window is None


def test_normalize_catalog_entry_preserves_unknown_max_output_tokens() -> None:
    model = MistralAdapter.normalize_catalog_entry(raw_mistral_model(), {"max_tokens": 8192})

    assert model.max_output_tokens is None


def test_normalize_catalog_entry_sets_json_mode_true_for_chat_models() -> None:
    model = MistralAdapter.normalize_catalog_entry(
        raw_mistral_model(function_calling=False, vision=False),
        {"max_tokens": 8192},
    )

    assert model.capabilities.json_mode is True


@pytest.mark.parametrize(
    "thinking_effort",
    ["minimal", "low", "medium", "high", "xhigh", "max"],
)
@respx.mock
@pytest.mark.asyncio
async def test_build_payload_maps_active_reasoning_efforts_to_high(
    mistral_adapter: MistralAdapter,
    thinking_effort: str,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter.send(
        SAMPLE_MESSAGES,
        model_id="mistral-large-latest",
        thinking_effort=thinking_effort,
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning_effort"] == "high"


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_snaps_against_effective_model_ladder(
    mistral_config: ProviderConfig,
) -> None:
    """A feed ladder is consulted; any active snapped effort engages thinking.

    With an effective ladder of ``[low, medium, high]`` a ``medium`` selection
    snaps to ``medium`` (in-ladder) — a value the binary ``{none, high}`` floor
    could never produce — and the wire still engages Mistral's single thinking
    mode (``reasoning_effort: high``) rather than silently dropping it.
    """

    def _model_lookup(model_id: str) -> Model:
        return Model(
            model_id=model_id,
            name=model_id,
            capabilities=Capabilities(
                vision=False,
                tools=True,
                json_mode=True,
                reasoning=ReasoningCapabilities(
                    supported=True,
                    control="levels",
                    levels=("low", "medium", "high"),
                ),
            ),
            context_window=128000,
            max_output_tokens=8192,
        )

    adapter = MistralAdapter(mistral_config, API_KEY, model_lookup=_model_lookup)
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await adapter.send(
        SAMPLE_MESSAGES,
        model_id="mistral-medium-3-5",
        thinking_effort="medium",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning_effort"] == "high"


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_sets_reasoning_effort_none_when_disabled(
    mistral_adapter: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter.send(
        SAMPLE_MESSAGES,
        model_id="mistral-large-latest",
        thinking_effort="none",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning_effort"] == "none"


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_magistral_medium_uses_prompt_mode_reasoning(
    mistral_adapter_with_prompt_mode_lookup: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter_with_prompt_mode_lookup.send(
        SAMPLE_MESSAGES,
        model_id="magistral-medium-latest",
        thinking_effort="high",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["prompt_mode"] == "reasoning"
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_magistral_medium_2509_uses_prompt_mode_reasoning(
    mistral_adapter_with_prompt_mode_lookup: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter_with_prompt_mode_lookup.send(
        SAMPLE_MESSAGES,
        model_id="magistral-medium-2509",
        thinking_effort="high",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["prompt_mode"] == "reasoning"
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_magistral_small_without_prompt_mode_metadata_uses_reasoning_effort(
    mistral_adapter_with_prompt_mode_lookup: MistralAdapter,
) -> None:
    """A reasoning model lacking the prompt_mode fact uses the reasoning_effort wire."""

    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter_with_prompt_mode_lookup.send(
        SAMPLE_MESSAGES,
        model_id="magistral-small-latest",
        thinking_effort="high",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning_effort"] == "high"
    assert "prompt_mode" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_magistral_small_uses_reasoning_effort(
    mistral_adapter: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter.send(
        SAMPLE_MESSAGES,
        model_id="magistral-small-latest",
        thinking_effort="high",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning_effort"] == "high"
    assert "prompt_mode" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_mistral_small_uses_reasoning_effort(
    mistral_adapter: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter.send(
        SAMPLE_MESSAGES,
        model_id="mistral-small-latest",
        thinking_effort="high",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["reasoning_effort"] == "high"
    assert "prompt_mode" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_mistral_medium_suppresses_reasoning_when_lookup_disables_it(
    mistral_adapter_with_reasoning_lookup: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter_with_reasoning_lookup.send(
        SAMPLE_MESSAGES,
        model_id="mistral-medium-latest",
        thinking_effort="high",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert "reasoning_effort" not in request_body
    assert "prompt_mode" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_magistral_medium_lookup_keeps_prompt_mode_reasoning(
    mistral_adapter_with_reasoning_lookup: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter_with_reasoning_lookup.send(
        SAMPLE_MESSAGES,
        model_id="magistral-medium-latest",
        thinking_effort="high",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert request_body["prompt_mode"] == "reasoning"
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_magistral_medium_none_effort_sends_no_reasoning_params(
    mistral_adapter_with_prompt_mode_lookup: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter_with_prompt_mode_lookup.send(
        SAMPLE_MESSAGES,
        model_id="magistral-medium-latest",
        thinking_effort="none",
    )

    request_body = json.loads(route.calls.last.request.content)
    assert "reasoning_effort" not in request_body
    assert "prompt_mode" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_build_payload_omits_reasoning_effort_when_not_provided(
    mistral_adapter: MistralAdapter,
) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    await mistral_adapter.send(SAMPLE_MESSAGES, model_id="mistral-large-latest")

    request_body = json.loads(route.calls.last.request.content)
    assert "reasoning_effort" not in request_body


@respx.mock
@pytest.mark.asyncio
async def test_send_returns_normal_response(mistral_adapter: MistralAdapter) -> None:
    route = respx.post(MISTRAL_URL).mock(return_value=httpx.Response(200, json=SUCCESS_RESPONSE))

    response = await mistral_adapter.send(SAMPLE_MESSAGES, model_id="mistral-large-latest")

    assert route.called
    assert response == SUCCESS_RESPONSE
