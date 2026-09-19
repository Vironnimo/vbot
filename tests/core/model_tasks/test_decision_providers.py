import json

import httpx
import pytest
import respx

from core.model_tasks.decision_providers import ProviderDecisionClient
from core.providers.errors import ProviderOutcomeUnknownError
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig


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
        "state": [{"TRUE": "3.0"}],
        "questions": {"id": {"type": "noul", "instructions": "Has data?"}},
    }
    assert result["answers"]["id"]["noul"] == 0.75


@pytest.mark.asyncio
@respx.mock
async def test_ambiguous_transport_failure_never_replays_billed_request():
    route = respx.post("https://openrouter.ai/api/alpha/decisions").mock(
        side_effect=httpx.ReadTimeout("fixture")
    )
    with pytest.raises(ProviderOutcomeUnknownError):
        await client().evaluate(
            "state", [{"id": "id", "type": "noul", "instructions": "Has data?"}]
        )
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_control_can_reuse_caller_owned_connection_without_losing_timeout_or_auth():
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
