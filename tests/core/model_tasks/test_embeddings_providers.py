"""Model tasks: the embeddings client over the wire.

Every case goes through the public ``ProviderEmbeddingClient.embed`` against a
mocked ``POST /embeddings`` endpoint (Google: native ``batchEmbedContents``,
classified with the Google Adapter's error semantics): the request body the
Provider receives, the vectors and usage the caller gets back, and how
malformed responses fail.
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
from core.providers.errors import ProviderAuthError, ProviderError, ProviderRateLimitError
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig
from core.utils.retry import MAX_RETRIES, caller_owns_retries

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_EMBEDDINGS_URL = f"{OPENROUTER_BASE_URL}/embeddings"
MODEL_ID = "google/gemini-embedding-2"


def _client() -> ProviderEmbeddingClient:
    provider = ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        adapter="openrouter",
        base_url=OPENROUTER_BASE_URL,
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
    ("inputs", "options", "input_type", "expected_fields"),
    [
        pytest.param(["a", "b"], {}, None, {}, id="defaults"),
        # A single text still travels as a one-element array.
        pytest.param(["only"], {"dimensions": None}, None, {}, id="unset-dimensions"),
        pytest.param(["a"], {"dimensions": 256}, None, {"dimensions": 256}, id="dimensions"),
        pytest.param(
            ["a"],
            {"dimensions": 256, "extra_options": {"user": "abc", "empty": ""}},
            None,
            {"dimensions": 256, "user": "abc"},
            id="extra-options-merged-without-empty-values",
        ),
        pytest.param(["a"], {}, "search_query", {"input_type": "search_query"}, id="input-type"),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_embed_sends_the_expected_request(
    inputs: list[str],
    options: dict[str, Any],
    input_type: str | None,
    expected_fields: dict[str, Any],
) -> None:
    route = respx.post(OPENROUTER_EMBEDDINGS_URL).mock(
        return_value=httpx.Response(200, json=_vectors_for(inputs))
    )

    await _client().embed(inputs, options=options, input_type=input_type)

    request = route.calls.last.request
    assert json.loads(request.content) == {**_BASE_BODY, "input": inputs, **expected_fields}
    assert request.headers["Authorization"] == "Bearer sk-test"
    assert request.extensions["timeout"]["read"] == DEFAULT_EMBEDDING_TIMEOUT


@pytest.mark.parametrize(
    "options",
    [
        pytest.param({"dimensions": 0}, id="zero-dimensions"),
        pytest.param({"dimensions": 256.0}, id="float-dimensions"),
        *(
            pytest.param({"extra_options": {field: "override"}}, id=f"reserved-{field}")
            for field in ("model", "input", "encoding_format", "dimensions", "input_type")
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_invalid_request_options_fail_before_any_request(options: dict[str, Any]) -> None:
    route = respx.post(OPENROUTER_EMBEDDINGS_URL).mock(
        return_value=httpx.Response(200, json=_vectors_for(["a"]))
    )

    with pytest.raises(ProviderError) as raised:
        await _client().embed(["a"], options=options)

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


# --- Google's native batchEmbedContents route ---------------------------------

GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
GOOGLE_MODEL_ID = "gemini-embedding-001"
GOOGLE_BATCH_URL = f"{GOOGLE_BASE_URL}/models/{GOOGLE_MODEL_ID}:batchEmbedContents"


def _google_client() -> ProviderEmbeddingClient:
    provider = ProviderConfig(
        id="google",
        name="Google AI Studio",
        adapter="google",
        base_url=GOOGLE_BASE_URL,
        connections=[],
    )
    connection = ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(header="x-goog-api-key", prefix="", credential_key="GEMINI_API_KEY"),
    )
    return ProviderEmbeddingClient(
        provider=provider, connection=connection, credential="g-test", model_id=GOOGLE_MODEL_ID
    )


def _google_vectors(count: int, *, offset: int = 0, tokens: int | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "embeddings": [{"values": [float(offset + i), 1.0]} for i in range(count)]
    }
    if tokens is not None:
        body["usageMetadata"] = {
            "promptTokenCount": tokens,
            "promptTokenDetails": [{"modality": "TEXT", "tokenCount": tokens}],
        }
    return body


@pytest.mark.parametrize(
    ("options", "input_type", "expected_fields"),
    [
        pytest.param({}, None, {}, id="defaults"),
        pytest.param({}, "search_query", {"taskType": "RETRIEVAL_QUERY"}, id="query"),
        pytest.param(
            {"dimensions": 768, "extra_options": {"title": "Notes"}},
            "search_document",
            {"taskType": "RETRIEVAL_DOCUMENT", "outputDimensionality": 768, "title": "Notes"},
            id="document-with-dimensions-and-extra",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_google_embeds_one_native_request_per_text(
    options: dict[str, Any], input_type: str | None, expected_fields: dict[str, Any]
) -> None:
    route = respx.post(GOOGLE_BATCH_URL).mock(
        return_value=httpx.Response(200, json=_google_vectors(2, tokens=6))
    )

    response = await _google_client().embed(["a", "b"], options=options, input_type=input_type)

    request = route.calls.last.request
    assert request.headers["x-goog-api-key"] == "g-test"
    assert json.loads(request.content) == {
        "requests": [
            {
                "model": f"models/{GOOGLE_MODEL_ID}",
                "content": {"parts": [{"text": text}]},
                "embedContentConfig": {"autoTruncate": False},
                **expected_fields,
            }
            for text in ("a", "b")
        ]
    }
    assert response.vectors == ([0.0, 1.0], [1.0, 1.0])
    assert response.model_id is None
    assert response.usage == EmbeddingUsage(
        requests=1, token_reports=1, input_tokens=6, total_tokens=6, input_token_reports=1
    )


@respx.mock
@pytest.mark.asyncio
async def test_google_splits_batches_beyond_its_limit_and_keeps_input_order() -> None:
    route = respx.post(GOOGLE_BATCH_URL).mock(
        side_effect=[
            httpx.Response(200, json=_google_vectors(100)),
            httpx.Response(200, json=_google_vectors(5, offset=100, tokens=5)),
        ]
    )
    texts = [f"t{i}" for i in range(105)]

    response = await _google_client().embed(texts, options={})

    assert [len(json.loads(call.request.content)["requests"]) for call in route.calls] == [100, 5]
    assert [vector[0] for vector in response.vectors] == [float(i) for i in range(105)]
    # Only one response reported tokens (gemini-embedding-001 reports none);
    # the aggregate keeps the unreported request visible.
    assert response.usage == EmbeddingUsage(
        requests=2, token_reports=1, input_tokens=5, total_tokens=5, input_token_reports=1
    )


@pytest.mark.parametrize(
    ("options", "input_type"),
    [
        pytest.param({"extra_options": {"taskType": "CLUSTERING"}}, "search_query", id="override"),
        pytest.param({}, "search_code", id="unknown-purpose"),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_google_request_conflicts_fail_before_any_request(
    options: dict[str, Any], input_type: str
) -> None:
    route = respx.post(GOOGLE_BATCH_URL).mock(
        return_value=httpx.Response(200, json=_google_vectors(1))
    )

    with pytest.raises(ProviderError) as raised:
        await _google_client().embed(["a"], options=options, input_type=input_type)

    assert raised.value.retryable is False
    assert route.call_count == 0


def _google_error(status: int, error: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, **error}})


@pytest.mark.parametrize(
    ("response", "error_type", "retryable", "retry_after"),
    [
        pytest.param(
            _google_error(
                400,
                {
                    "message": "API key not valid. Please pass a valid API key.",
                    "status": "INVALID_ARGUMENT",
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                            "reason": "API_KEY_INVALID",
                        }
                    ],
                },
            ),
            ProviderAuthError,
            False,
            None,
            id="invalid-key",
        ),
        pytest.param(
            _google_error(
                429,
                {
                    "message": "Quota exceeded for metric: embed_content_free_tier_requests, "
                    "limit: 0, model: gemini-embedding-001",
                    "status": "RESOURCE_EXHAUSTED",
                },
            ),
            ProviderError,
            False,
            None,
            id="zero-quota",
        ),
        # The Decisions API's shape: a string code and the wait only in the message.
        pytest.param(
            httpx.Response(
                429,
                json={
                    "error": {
                        "code": "too_many_requests",
                        "message": "Rate limit reached. Please retry in 9.2s.",
                    }
                },
            ),
            ProviderRateLimitError,
            True,
            9.2,
            id="throttle-with-retry-hint",
        ),
    ],
)
@respx.mock
@pytest.mark.asyncio
async def test_google_task_errors_follow_googles_semantics(
    response: httpx.Response,
    error_type: type[ProviderError],
    retryable: bool,
    retry_after: float | None,
) -> None:
    route = respx.post(GOOGLE_BATCH_URL).mock(return_value=response)

    with caller_owns_retries(), pytest.raises(ProviderError) as raised:
        await _google_client().embed(["a"], options={})

    assert type(raised.value) is error_type
    assert raised.value.retryable is retryable
    assert getattr(raised.value, "retry_after", None) == retry_after
    assert route.call_count == 1
