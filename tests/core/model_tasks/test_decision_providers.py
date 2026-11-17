import json

import httpx
import pytest
import respx

from core.model_tasks.decision_profile import DecisionProfile
from core.model_tasks.decision_providers import ProviderDecisionClient
from core.model_tasks.decision_types import DecisionError
from core.models.pricing import TokenPricing
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.providers.wire_profile import DecisionWire


def client():
    return ProviderDecisionClient(
        provider=ProviderConfig(
            id="openrouter",
            name="OpenRouter",
            adapter="openrouter",
            base_url="https://openrouter.ai/api/v1",
            connections=[],
        ),
        connection=ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(
                header="Authorization", prefix="Bearer ", credential_key="OPENROUTER_API_KEY"
            ),
        ),
        credential="fixture-token",
        model_id="~typesafe/jev-latest",
    )


def native_client(provider_id, base_url, model_id):
    return ProviderDecisionClient(
        provider=ProviderConfig(
            id=provider_id,
            name=provider_id,
            adapter="openai_compatible",
            base_url=base_url,
            connections=[],
        ),
        connection=ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(header="Authorization", prefix="Bearer ", credential_key="TEST_KEY"),
        ),
        credential="fixture-token",
        model_id=model_id,
    )


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize("provider_id", ["ollama-cloud", "another-native-provider"])
async def test_system_one_route_preserves_data_maps_ids_and_respects_declared_options(provider_id):
    profile = DecisionProfile(
        DecisionWire(
            protocol="system_one",
            endpoint="/v1/systemone",
            allowed_options=("keep_alive",),
        )
    )
    state = {"messages": [{"role": "system", "content": "ordinary data"}], "image_url": "data"}
    questions = [{"id": "Unsicher?", "type": "noul", "instructions": "Enough information?"}]
    route = respx.post("https://native.example/v1/systemone").respond(
        200,
        json={
            "model": "decision-model",
            "answers": {"q1": {"type": "noul", "noul": 0.5}},
            "usage": {"input_tokens": 10, "output_tokens": 3},
        },
    )
    result = await native_client(
        provider_id, "https://native.example/api", "decision-model"
    ).evaluate(
        state,
        questions,
        profile=profile,
        options={"keep_alive": "5m", "temperature": 1, "reasoning": "high"},
    )
    assert json.loads(route.calls[0].request.content) == {
        "model": "decision-model",
        "state": json.dumps(state, ensure_ascii=False, separators=(",", ":")),
        "questions": {"q1": {"type": "noul", "instructions": "Enough information?"}},
        "keep_alive": "5m",
    }
    assert result["answers"] == {"Unsicher?": {"type": "noul", "noul": 0.5}}
    assert result["usage"]["input_tokens"] == 10
    assert result["usage"]["output_tokens"] == 3
    assert result["usage"]["cost"]["source"] == "unknown"


@pytest.mark.asyncio
@respx.mock
async def test_openai_decisions_translates_all_primitives_refusal_and_cache_usage():
    questions = [
        {
            "id": "yes",
            "type": "noul",
            "instructions": "Explicit?",
            "criteria": {"true": "Explicit evidence", "false": "No explicit evidence"},
        },
        {
            "id": "kind",
            "type": "choice",
            "instructions": "Which?",
            "criteria": {"bug": "Broken", "unknown": "Insufficient information"},
        },
        {
            "id": "severity",
            "type": "score",
            "instructions": "How severe?",
            "criteria": ["Low", "High"],
        },
        {"id": "declined", "type": "noul", "instructions": "Declined?"},
    ]
    profile = DecisionProfile(
        DecisionWire(
            protocol="openai_decisions",
            endpoint="/v1/decisions",
            allowed_options=("prompt_cache_key",),
        )
    )
    route = respx.post("https://api.openai.com/v1/decisions").respond(
        200,
        json={
            "model": "gpt-6-luna",
            "answers": [
                {"name": "yes", "type": "predicate", "probability": 0.75},
                {
                    "name": "kind",
                    "type": "choice",
                    "choice": "unknown",
                    "confidence": 0.6,
                    "probabilities": [
                        {"value": "bug", "probability": 0.2},
                        {"value": "unknown", "probability": 0.8},
                    ],
                },
                {
                    "name": "severity",
                    "type": "score",
                    "score": 0.2,
                    "probabilities": [
                        {"value": 0, "label": "0", "probability": 0.8},
                        {"value": 1, "label": "1", "probability": 0.2},
                    ],
                },
                {"name": "declined", "type": "refusal"},
            ],
            "usage": {
                "input_tokens": 100,
                "output_tokens": 0,
                "input_tokens_details": {"cached_tokens": 40, "cache_write_tokens": 20},
            },
        },
    )
    state = {"role": "system", "messages": ["evidence"], "image_url": "ordinary data"}
    result = await native_client("openai", "https://api.openai.com/v1", "gpt-6-luna").evaluate(
        state,
        questions,
        profile=profile,
        options={"prompt_cache_key": "test"},
    )
    body = json.loads(route.calls[0].request.content)
    assert set(body) == {"model", "input", "questions", "prompt_cache_key"}
    assert json.loads(body["input"]) == state
    assert body["questions"] == [
        {
            "name": "yes",
            "type": "predicate",
            "instructions": (
                "Explicit?\nYes means: Explicit evidence\nNo means: No explicit evidence"
            ),
        },
        {
            "name": "kind",
            "type": "choice",
            "instructions": "Which?",
            "choices": [
                {"value": "bug", "description": "Broken"},
                {"value": "unknown", "description": "Insufficient information"},
            ],
        },
        {
            "name": "severity",
            "type": "score",
            "instructions": "How severe?",
            "levels": [{"label": "0", "description": "Low"}, {"label": "1", "description": "High"}],
        },
        {"name": "declined", "type": "predicate", "instructions": "Declined?"},
    ]
    assert result["answers"]["yes"] == {"type": "noul", "noul": 0.75}
    assert result["answers"]["kind"]["choice"] == "unknown"
    assert result["answers"]["severity"]["score"] == 0.2
    assert result["answers"]["declined"] == {"type": "refusal"}
    assert {
        key: result["usage"][key]
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    } == {
        "input_tokens": 100,
        "output_tokens": 0,
        "cache_read_tokens": 40,
        "cache_write_tokens": 20,
    }


@pytest.mark.asyncio
@respx.mock
async def test_a_state_field_wire_wraps_the_entire_evidence_and_routing():
    profile = DecisionProfile(
        DecisionWire(
            protocol="system_one",
            endpoint="/api/alpha/decisions",
            state_field="text",
            allowed_options=("provider_options",),
        )
    )
    route = respx.post("https://openrouter.ai/api/alpha/decisions").respond(200, json=_ANSWER)
    state = {"context": "task", "item": {"text": "evidence", "images": ["ordinary data"]}}
    await client().evaluate(
        state,
        [{"id": "id", "type": "noul", "instructions": "Has data?"}],
        profile=profile,
        options={"provider_options": {"allow_fallbacks": False}},
    )
    body = json.loads(route.calls[0].request.content)
    assert json.loads(body["state"]["text"]) == state
    assert body["provider"] == {"allow_fallbacks": False}


@pytest.mark.asyncio
@respx.mock
async def test_exact_alpha_wire_uses_existing_auth_and_preserves_json():
    route = respx.post("https://openrouter.ai/api/alpha/decisions").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "typesafe/jev-version",
                "answers": {"id": {"type": "noul", "noul": 0.75}},
                "usage": {"input_tokens": 12, "output_tokens": 2, "cost": 0.001},
            },
        )
    )
    result = await client().evaluate(
        [{"TRUE": "3.0"}], [{"id": "id", "type": "noul", "instructions": "Has data?"}]
    )
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer fixture-token"
    assert json.loads(request.content) == {
        "model": "~typesafe/jev-latest",
        "state": '[{"TRUE":"3.0"}]',
        "questions": {"id": {"type": "noul", "instructions": "Has data?"}},
    }
    assert result["answers"]["id"]["noul"] == 0.75


_ANSWER = {
    "model": "typesafe/jev-version",
    "answers": {"id": {"type": "noul", "noul": 0.5}},
    "usage": {"input_tokens": 10, "output_tokens": 1},
}


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(529, json={"error": {"code": 529, "message": "Provider overloaded"}}),
        httpx.Response(503, json={"error": {"code": 503, "message": "Service unavailable"}}),
        httpx.Response(524, json={"error": {"code": 524, "message": "Request timed out"}}),
        httpx.ReadTimeout("fixture"),
    ],
    ids=["overloaded", "unavailable", "edge-timeout", "read-timeout"],
)
async def test_an_unanswered_judgment_is_asked_again(failure):
    route = respx.post("https://openrouter.ai/api/alpha/decisions").mock(
        side_effect=[failure, httpx.Response(200, json=_ANSWER)]
    )

    result = await client().evaluate(
        "state", [{"id": "id", "type": "noul", "instructions": "Has data?"}]
    )

    assert result["answers"]["id"]["noul"] == 0.5
    assert route.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_a_body_that_is_not_json_is_an_invalid_result_and_not_repeated():
    route = respx.post("https://openrouter.ai/api/alpha/decisions").respond(200, text="<html>")

    with pytest.raises(DecisionError) as error:
        await client().evaluate(
            "state", [{"id": "id", "type": "noul", "instructions": "Has data?"}]
        )

    assert error.value.code == "invalid_result"
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_a_shared_connection_keeps_timeout_and_auth_for_every_request():
    route = respx.post("https://openrouter.ai/api/alpha/decisions").mock(
        return_value=httpx.Response(
            200,
            json={
                "model": "typesafe/jev-version",
                "answers": {"q": {"type": "noul", "noul": 0.9}},
                "usage": {"input_tokens": 10, "output_tokens": 1},
            },
        )
    )
    async with httpx.AsyncClient(timeout=1) as shared:
        for _ in range(2):
            await client().evaluate(
                "same",
                [{"id": "q", "type": "noul", "instructions": "Has data?"}],
                http_client=shared,
            )
            assert not shared.is_closed
    assert route.call_count == 2
    for call in route.calls:
        assert call.request.headers["Authorization"] == "Bearer fixture-token"
        assert call.request.extensions["timeout"]["read"] == 60


_GOOGLE_PROFILE = DecisionProfile(
    DecisionWire(protocol="google_decisions", endpoint="/v1beta/decisions"),
    pricing=TokenPricing.from_cost({"input": 0.1, "output": 0}, source="google:decision"),
)
_GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta/decisions"


def google_client():
    return ProviderDecisionClient(
        provider=ProviderConfig(
            id="google",
            name="Google AI Studio",
            adapter="google",
            base_url="https://generativelanguage.googleapis.com/v1beta",
            connections=[],
        ),
        connection=ConnectionConfig(
            id="api-key",
            type="api_key",
            label="API Key",
            auth=AuthConfig(header="x-goog-api-key", prefix="", credential_key="GEMINI_API_KEY"),
        ),
        credential="fixture-token",
        model_id="gemini-decision-1",
    )


@pytest.mark.asyncio
@respx.mock
async def test_google_decisions_translates_ids_criteria_answers_and_usage():
    questions = [
        {
            "id": "Beschädigt?",
            "type": "noul",
            "instructions": "Damaged?",
            "criteria": {"true": "Reported broken", "false": "No damage reported"},
        },
        {
            "id": "queue",
            "type": "choice",
            "instructions": "Which queue?",
            "criteria": {"billing": "Payments", "replacements": "Damaged items"},
        },
        {
            "id": "anger",
            "type": "score",
            "instructions": "How upset?",
            "criteria": ["Calm", "Angry"],
        },
    ]
    # The answer shape Google returned live on 2026-11-17: no Model echo, total_* usage.
    route = respx.post(_GOOGLE_URL).respond(
        200,
        json={
            "answers": {
                "q1": {"type": "noul", "noul": 0.97, "confidence": 0.97},
                "q2": {
                    "type": "choice",
                    "choice": "replacements",
                    "confidence": 0.99,
                    "probabilities": {"billing": 0.004, "replacements": 0.996},
                },
                "q3": {
                    "type": "score",
                    "score": 0.7,
                    "confidence": 0.7,
                    "legend": {"0": "Calm", "1": "Angry"},
                    "probabilities": {"0": 0.3, "1": 0.7},
                },
            },
            "usage": {
                "total_tokens": 2000,
                "total_input_tokens": 2000,
                "input_tokens_by_modality": [{"modality": "text", "tokens": 2000}],
                "total_output_tokens": 0,
            },
        },
    )
    state = {"ticket": "The mug arrived cracked.", "image_url": "ordinary data"}

    result = await google_client().evaluate(state, questions, profile=_GOOGLE_PROFILE)

    request = route.calls[0].request
    assert request.headers["x-goog-api-key"] == "fixture-token"
    assert json.loads(request.content) == {
        "model": "gemini-decision-1",
        "input": json.dumps(state, ensure_ascii=False, separators=(",", ":")),
        "questions": {
            "q1": {
                "type": "noul",
                "instructions": "Damaged?",
                "criteria": {
                    "true_description": "Reported broken",
                    "false_description": "No damage reported",
                },
            },
            "q2": {
                "type": "choice",
                "instructions": "Which queue?",
                "criteria": {"billing": "Payments", "replacements": "Damaged items"},
            },
            "q3": {"type": "score", "instructions": "How upset?", "criteria": ["Calm", "Angry"]},
        },
    }
    assert result["model"] == "gemini-decision-1"
    assert result["answers"]["Beschädigt?"] == {"type": "noul", "noul": 0.97}
    assert result["answers"]["queue"]["choice"] == "replacements"
    assert result["answers"]["anger"]["score"] == 0.7
    assert (result["usage"]["input_tokens"], result["usage"]["output_tokens"]) == (2000, 0)
    assert result["usage"]["cost"]["amount_usd"] == pytest.approx(0.0002)


@pytest.mark.asyncio
@respx.mock
@pytest.mark.parametrize(
    "blocked",
    [
        httpx.Response(
            400, json={"error": {"code": "safety", "message": "Request blocked (harmful content)."}}
        ),
        httpx.Response(
            200,
            json={
                "answers": {},
                "prompt_feedback": {"block_reason": "prohibited_content"},
                "usage": {"total_input_tokens": 12, "total_output_tokens": 0},
            },
        ),
    ],
    ids=["http-400", "prompt-feedback"],
)
async def test_a_google_input_block_declines_every_question_without_retry(blocked):
    route = respx.post(_GOOGLE_URL).mock(return_value=blocked)
    questions = [
        {"id": "a", "type": "noul", "instructions": "Harmful?"},
        {"id": "b", "type": "score", "instructions": "How bad?", "criteria": ["Low", "High"]},
    ]

    result = await google_client().evaluate("text", questions, profile=_GOOGLE_PROFILE)

    assert result["answers"] == {"a": {"type": "refusal"}, "b": {"type": "refusal"}}
    assert route.call_count == 1
