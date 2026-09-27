"""Model tasks: the OpenAI-compatible embeddings client over the wire.

Every case goes through the public ``ProviderEmbeddingClient.embed`` against a
mocked ``POST /embeddings`` endpoint: the request body the Provider receives,
the vectors and usage the caller gets back, and how malformed responses fail.
Retryable parse failures are retried like transient HTTP errors.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx

from core.model_tasks.embeddings_providers import (
    DEFAULT_EMBEDDING_TIMEOUT,
    EmbeddingUsage,
    ProviderEmbeddingClient,
)
from core.providers.errors import ProviderAuthError, ProviderError
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.utils.retry import MAX_RETRIES

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_EMBEDDINGS_URL = f"{OPENROUTER_BASE_URL}/embeddings"
GENERIC_BASE_URL = "https://example.test/v1"
MODEL_ID = "google/gemini-embedding-2"


def _client(
    provider_id: str = "openrouter", base_url: str = OPENROUTER_BASE_URL
) -> ProviderEmbeddingClient:
    provider = ProviderConfig(
        id=provider_id,
        name=provider_id,
        adapter=provider_id,
        base_url=base_url,
        connections=[],
        extra_headers={"X-Title": "vBot"},
    )
    connection = ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(
            header="Authorization", prefix="Bearer ", credential_key="OPENROUTER_API_KEY"
        ),
    )
    return ProviderEmbeddingClient(
        provider=provider, connection=connection, credential="sk-test", model_id=MODEL_ID
    )


def _vectors_for(inputs: list[str]) -> dict[str, Any]:
    return {"data": [{"index": i, "embedding": [0.1 * (i + 1)]} for i in range(len(inputs))]}


_BASE_BODY = {"model": MODEL_ID, "encoding_format": "float"}


@pytest.mark.parametrize(
    ("provider_id", "inputs", "options", "purpose", "expected_fields"),
    [
        pytest.param("openrouter", ["a", "b"], {}, None, {}, id="defaults"),
        # A single text still travels as a one-element array.
        pytest.param("openrouter", ["only"], {"dimensions": None}, None, {}, id="unset-dimensions"),
        pytest.param(
            "openrouter", ["a"], {"dimensions": 256}, None, {"dimensions": 256}, id="dimensions"
        ),
        pytest.param(
            "openrouter",
            ["a"],
            {"dimensions": 256, "extra_options": {"user": "abc", "empty": ""}},
            None,
            {"dimensions": 256, "user": "abc"},
            id="extra-options-merged-without-empty-values",
        ),
        pytest.param(
            "openrouter", ["a"], {}, "query", {"input_type": "search_query"}, id="query-purpose"
        ),
        pytest.param(
            "openrouter",
            ["a"],
            {},
            "document",
            {"input_type": "search_document"},
            id="document-purpose",
        ),
        # ``input_type`` is verified for OpenRouter only.
        pytest.param("generic", ["a"], {}, "query", {}, id="other-provider-omits-input-type"),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_embed_sends_the_expected_request(
    provider_id: str,
    inputs: list[str],
    options: dict[str, Any],
    purpose: str | None,
    expected_fields: dict[str, Any],
) -> None:
    base_url = OPENROUTER_BASE_URL if provider_id == "openrouter" else GENERIC_BASE_URL
    route = respx.post(f"{base_url}/embeddings").mock(
        return_value=httpx.Response(200, json=_vectors_for(inputs))
    )

    await _client(provider_id, base_url).embed(inputs, options=options, purpose=purpose)

    request = route.calls.last.request
    assert json.loads(request.content) == {**_BASE_BODY, "input": inputs, **expected_fields}
    assert request.headers["Authorization"] == "Bearer sk-test"
    assert request.extensions["timeout"]["read"] == DEFAULT_EMBEDDING_TIMEOUT


@pytest.mark.parametrize(
    ("options", "purpose"),
    [
        pytest.param({"dimensions": 0}, None, id="zero-dimensions"),
        pytest.param({"dimensions": 256.0}, None, id="float-dimensions"),
        *(
            pytest.param({"extra_options": {field: "override"}}, None, id=f"reserved-{field}")
            for field in ("model", "input", "encoding_format", "dimensions", "input_type")
        ),
        pytest.param({}, "classification", id="unknown-purpose"),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_invalid_request_options_fail_before_any_request(
    options: dict[str, Any], purpose: str | None
) -> None:
    route = respx.post(OPENROUTER_EMBEDDINGS_URL).mock(
        return_value=httpx.Response(200, json=_vectors_for(["a"]))
    )

    with pytest.raises(ProviderError) as raised:
        await _client().embed(["a"], options=options, purpose=purpose)

    assert raised.value.retryable is False
    assert route.call_count == 0


@pytest.mark.parametrize(
    ("body", "vectors", "model_id", "usage"),
    [
        pytest.param(
            {
                "object": "list",
                "data": [
                    {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]},
                    {"object": "embedding", "index": 1, "embedding": [0.3, 0.4]},
                ],
                "model": "served/model-v2",
                "usage": {"prompt_tokens": 4, "total_tokens": 5, "cost": "0.00014"},
            },
            ([0.1, 0.2], [0.3, 0.4]),
            "served/model-v2",
            EmbeddingUsage(
                requests=1,
                token_reports=1,
                cost_reports=1,
                input_tokens=4,
                total_tokens=5,
                cost=0.00014,
                input_token_reports=1,
            ),
            id="indexed-with-model-and-usage",
        ),
        pytest.param(
            {
                "data": [
                    {"index": 1, "embedding": [0.3, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ],
                "usage": {"input_tokens": 7},
            },
            ([0.1, 0.2], [0.3, 0.4]),
            None,
            EmbeddingUsage(
                requests=1,
                token_reports=1,
                input_tokens=7,
                total_tokens=7,
                input_token_reports=1,
            ),
            id="out-of-order-indices-are-sorted",
        ),
        pytest.param(
            {"data": [{"embedding": [1, 2]}, {"embedding": [3, 4]}]},
            ([1.0, 2.0], [3.0, 4.0]),
            None,
            EmbeddingUsage(requests=1),
            id="unindexed-entries-keep-wire-order-and-ints-become-floats",
        ),
        pytest.param(
            {
                "data": [{"index": 0, "embedding": [0.1]}, {"index": 1, "embedding": [0.2]}],
                "usage": {"prompt_tokens": 7, "cost": 10**400},
            },
            ([0.1], [0.2]),
            None,
            EmbeddingUsage(
                requests=1,
                token_reports=1,
                input_tokens=7,
                total_tokens=7,
                input_token_reports=1,
            ),
            id="unrepresentable-cost-is-unreported",
        ),
        # A reported zero is a report; malformed counters are not.
        pytest.param(
            {**_vectors_for(["alpha", "beta"]), "usage": {"prompt_tokens": 0, "cost": 0}},
            ([0.1], [0.2]),
            None,
            EmbeddingUsage(requests=1, token_reports=1, cost_reports=1, input_token_reports=1),
            id="zero-counters-are-reported",
        ),
        pytest.param(
            {**_vectors_for(["alpha", "beta"]), "usage": {"total_tokens": 7}},
            ([0.1], [0.2]),
            None,
            EmbeddingUsage(requests=1, token_reports=1, total_tokens=7),
            id="total-tokens-only",
        ),
        pytest.param(
            {**_vectors_for(["alpha", "beta"]), "usage": {"prompt_tokens": True, "cost": -1}},
            ([0.1], [0.2]),
            None,
            EmbeddingUsage(requests=1),
            id="malformed-counters-are-unreported",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_embed_returns_vectors_in_input_order_with_normalized_usage(
    body: dict[str, Any],
    vectors: tuple[list[float], ...],
    model_id: str | None,
    usage: EmbeddingUsage,
) -> None:
    respx.post(OPENROUTER_EMBEDDINGS_URL).mock(return_value=httpx.Response(200, json=body))

    response = await _client().embed(["alpha", "beta"], options={})

    assert response.vectors == vectors
    assert all(type(component) is float for vector in response.vectors for component in vector)
    assert response.model_id == model_id
    assert response.usage == usage


def _entries(*embeddings: Any, indices: tuple[Any, ...] = (0, 1)) -> dict[str, Any]:
    return {
        "data": [
            {"index": index, "embedding": embedding}
            for index, embedding in zip(indices, embeddings, strict=True)
        ]
    }


@pytest.mark.parametrize(
    ("response", "error_type", "retryable"),
    [
        pytest.param(httpx.Response(401, text="Unauthorized"), ProviderAuthError, False, id="401"),
        pytest.param(httpx.Response(200, json=["nope"]), ProviderError, False, id="not-an-object"),
        # A 200 without data is transient unless it carries a definitive error.
        pytest.param(httpx.Response(200, json={"data": []}), ProviderError, True, id="empty-data"),
        pytest.param(
            httpx.Response(200, json=_entries([0.1], indices=(0,))),
            ProviderError,
            True,
            id="fewer-vectors-than-inputs",
        ),
        pytest.param(
            httpx.Response(200, json={"data": [{"index": 0}, {"index": 1, "embedding": [0.1]}]}),
            ProviderError,
            True,
            id="missing-embedding",
        ),
        pytest.param(
            httpx.Response(200, json={"data": ["a", "b"]}),
            ProviderError,
            False,
            id="entry-not-an-object",
        ),
        pytest.param(
            httpx.Response(200, json=_entries([0.1], [0.2], indices=(None, 1))),
            ProviderError,
            False,
            id="null-index",
        ),
        pytest.param(
            httpx.Response(
                200, json={"data": [{"index": 0, "embedding": [0.1]}, {"embedding": [0.2]}]}
            ),
            ProviderError,
            False,
            id="mixed-index-presence",
        ),
        pytest.param(
            httpx.Response(200, json=_entries([0.1], [0.2], indices=(0, 0))),
            ProviderError,
            False,
            id="duplicate-indices",
        ),
        pytest.param(
            httpx.Response(200, json=_entries([0.1], [0.2], indices=(0, 2))),
            ProviderError,
            False,
            id="index-out-of-range",
        ),
        pytest.param(
            httpx.Response(200, json=_entries([0.1, 0.2], [0.3])),
            ProviderError,
            False,
            id="inconsistent-dimensions",
        ),
        pytest.param(
            httpx.Response(200, json=_entries([True], [False])),
            ProviderError,
            False,
            id="boolean-component",
        ),
        pytest.param(
            httpx.Response(200, json=_entries(["0.1"], ["0.2"])),
            ProviderError,
            False,
            id="string-component",
        ),
        pytest.param(
            httpx.Response(
                200,
                content=b'{"data": [{"index": 0, "embedding": [NaN]}, '
                b'{"index": 1, "embedding": [0.2]}]}',
            ),
            ProviderError,
            False,
            id="non-finite-component",
        ),
        pytest.param(
            httpx.Response(200, json=_entries([10**400], [0.2])),
            ProviderError,
            False,
            id="unrepresentable-component",
        ),
        pytest.param(
            httpx.Response(200, json={**_entries([0.1], [0.2]), "model": ""}),
            ProviderError,
            False,
            id="blank-model",
        ),
        pytest.param(
            httpx.Response(200, json={**_entries([0.1], [0.2]), "model": 123}),
            ProviderError,
            False,
            id="non-string-model",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_unusable_response_fails_and_only_transient_ones_are_retried(
    response: httpx.Response, error_type: type[ProviderError], retryable: bool
) -> None:
    route = respx.post(OPENROUTER_EMBEDDINGS_URL).mock(return_value=response)

    with pytest.raises(ProviderError) as raised:
        await _client().embed(["alpha", "beta"], options={})

    assert type(raised.value) is error_type
    assert raised.value.retryable is retryable
    assert route.call_count == (MAX_RETRIES + 1 if retryable else 1)


@respx.mock
@pytest.mark.asyncio
async def test_provider_error_object_in_a_200_is_final_and_passes_its_message_through() -> None:
    route = respx.post(OPENROUTER_EMBEDDINGS_URL).mock(
        return_value=httpx.Response(
            200, json={"error": {"message": "No endpoints found for baai/bge-m3.", "code": 404}}
        )
    )

    with pytest.raises(ProviderError, match="No endpoints found for baai/bge-m3") as raised:
        await _client().embed(["alpha"], options={})

    assert raised.value.retryable is False
    assert "code=404" in str(raised.value)
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_overloaded_provider_is_retried() -> None:
    route = respx.post(OPENROUTER_EMBEDDINGS_URL).mock(
        side_effect=[
            httpx.Response(529, text="overloaded"),
            httpx.Response(200, json=_vectors_for(["alpha"])),
        ]
    )

    response = await _client().embed(["alpha"], options={})

    assert response.vectors == ([0.1],)
    assert route.call_count == 2
