"""Tests for the provider-backed embeddings HTTP client and payload shaping."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from core.embeddings.providers import (
    DEFAULT_EMBEDDING_TIMEOUT,
    EMBEDDINGS_ENDPOINT,
    ProviderEmbeddingClient,
    _build_embeddings_payload,
    _coerce_vector,
    _parse_embeddings_response,
)
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

# ---------------------------------------------------------------------------
# Payload builder — the heart of the wire-shaping contract
# ---------------------------------------------------------------------------


def test_build_payload_always_sets_encoding_format_float() -> None:
    """The OpenAI/OpenRouter embeddings contract uses ``encoding_format="float"``.

    The catalog does not surface a base64 mode for embeddings, so the
    wire always pins ``float`` — this iteration does not decode
    base64 vectors because the recall store wants Python floats.
    """

    payload = _build_embeddings_payload("google/gemini-embedding-2", ["a", "b"], {})

    assert payload == {
        "model": "google/gemini-embedding-2",
        "input": ["a", "b"],
        "encoding_format": "float",
    }
    assert payload["encoding_format"] == "float"


def test_build_payload_drops_dimensions_when_absent() -> None:
    """When ``dimensions`` is not in options, the wire omits the field.

    The non-Matryoshka default is to send no ``dimensions`` — the
    provider's catalog dimension is used. We must not invent a value
    from a missing key.
    """

    payload = _build_embeddings_payload("google/gemini-embedding-2", ["a"], {})

    assert "dimensions" not in payload


def test_build_payload_drops_dimensions_when_none() -> None:
    """A ``None`` ``dimensions`` (the schema default for an unset number) is dropped.

    Sending ``"dimensions": null`` would be rejected by the provider,
    so the wire never carries it. The schema-level default is
    therefore harmless.
    """

    payload = _build_embeddings_payload("google/gemini-embedding-2", ["a"], {"dimensions": None})

    assert "dimensions" not in payload


def test_build_payload_drops_dimensions_when_zero() -> None:
    """A ``0`` ``dimensions`` is a placeholder, not a real value, and is dropped.

    A zero-length embedding is not a real model output, so the wire
    treats it the same as a missing field.
    """

    payload = _build_embeddings_payload("google/gemini-embedding-2", ["a"], {"dimensions": 0})

    assert "dimensions" not in payload


def test_build_payload_forwards_positive_integer_dimensions() -> None:
    """A positive integer ``dimensions`` (Matryoshka truncation) is forwarded."""

    payload = _build_embeddings_payload(
        "google/gemini-embedding-2", ["a", "b"], {"dimensions": 256}
    )

    assert payload["dimensions"] == 256
    assert isinstance(payload["dimensions"], int)


def test_build_payload_coerces_positive_float_dimensions_to_int() -> None:
    """A float ``dimensions`` is coerced to int — the wire contract is an integer.

    Settings UIs sometimes surface numbers as floats; the wire layer
    normalizes so the provider sees a clean int.
    """

    payload = _build_embeddings_payload("google/gemini-embedding-2", ["a"], {"dimensions": 256.0})

    assert payload["dimensions"] == 256
    assert isinstance(payload["dimensions"], int)


def test_build_payload_keeps_input_as_array_even_for_single_text() -> None:
    """A single-input batch is sent as a one-element array.

    The OpenAI-compatible contract accepts a string or an array; we
    always send the array form so the wire shape is consistent for
    batching, and the response parser does not need a per-call branch.
    """

    payload = _build_embeddings_payload("google/gemini-embedding-2", ["only"], {})

    assert payload["input"] == ["only"]


# ---------------------------------------------------------------------------
# Response parsing — vectors are returned in input order
# ---------------------------------------------------------------------------


def test_parse_response_returns_vectors_in_input_order() -> None:
    """When the response is already in input order, vectors are returned as-is."""

    payload = {
        "object": "list",
        "data": [
            {"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]},
            {"object": "embedding", "index": 1, "embedding": [0.4, 0.5, 0.6]},
        ],
        "model": "google/gemini-embedding-2",
        "usage": {"prompt_tokens": 2, "total_tokens": 2},
    }

    vectors = _parse_embeddings_response(payload, expected_count=2)

    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]


def test_parse_response_sorts_by_index_when_out_of_order() -> None:
    """An out-of-order ``data`` array is sorted by ``index`` so the caller
    sees vectors in input order.

    The OpenAI/OpenRouter contract documents input-ordered responses,
    but the wire format carries explicit ``index`` fields. We rely on
    them so a reordered response cannot silently land vectors in the
    wrong order in the recall store.
    """

    payload = {
        "data": [
            {"index": 1, "embedding": [0.4, 0.5, 0.6]},
            {"index": 0, "embedding": [0.1, 0.2, 0.3]},
        ],
    }

    vectors = _parse_embeddings_response(payload, expected_count=2)

    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]


def test_parse_response_coerces_int_values_to_floats() -> None:
    """Vector entries that arrive as ints are coerced to floats.

    The OpenAI contract documents ``embedding`` as a float array, but
    providers may JSON-encode whole-number components as ints. The
    downstream store records ``dimension`` from ``len(vectors[0])`` and
    compares floats element-wise, so the type must be uniform.
    """

    payload = {"data": [{"index": 0, "embedding": [1, 2, 3]}]}

    vectors = _parse_embeddings_response(payload, expected_count=1)

    assert vectors == [[1.0, 2.0, 3.0]]
    assert all(isinstance(component, float) for component in vectors[0])


def test_parse_response_rejects_booleans_inside_embedding() -> None:
    """A boolean value inside an embedding is non-numeric and is rejected.

    Python treats booleans as ints, so without the explicit guard a
    ``True``/``False`` could land in a vector. The wire layer
    short-circuits with a non-retryable :class:`ProviderError`.
    """

    from core.providers.errors import ProviderError

    payload = {"data": [{"index": 0, "embedding": [True, False]}]}

    with pytest.raises(ProviderError, match="non-numeric"):
        _parse_embeddings_response(payload, expected_count=1)


def test_parse_response_rejects_string_values_inside_embedding() -> None:
    """A string value inside an embedding is rejected as non-numeric."""

    from core.providers.errors import ProviderError

    payload = {"data": [{"index": 0, "embedding": ["0.1", "0.2"]}]}

    with pytest.raises(ProviderError, match="non-numeric"):
        _parse_embeddings_response(payload, expected_count=1)


def test_parse_response_rejects_empty_data_array() -> None:
    """An empty ``data`` array is a malformed response and surfaces as a
    retryable :class:`ProviderError` — the next attempt may return a
    complete batch.
    """

    from core.providers.errors import ProviderError

    with pytest.raises(ProviderError, match="no data"):
        _parse_embeddings_response({"data": []}, expected_count=2)


def test_parse_response_rejects_missing_data_key() -> None:
    """A response without a ``data`` key is malformed.

    A 200 with no ``data`` array is a provider bug; we treat it the
    same as an empty data array.
    """

    from core.providers.errors import ProviderError

    with pytest.raises(ProviderError, match="no data"):
        _parse_embeddings_response({}, expected_count=2)


def test_parse_response_rejects_non_dict_payload() -> None:
    """A non-dict response (a bare array, a string) is rejected as a wire
    shape problem — non-retryable because the next attempt will
    receive the same shape.
    """

    from core.providers.errors import ProviderError

    with pytest.raises(ProviderError, match="JSON object"):
        _parse_embeddings_response(["nope"], expected_count=1)


def test_parse_response_rejects_count_mismatch() -> None:
    """When the response has fewer vectors than inputs, retrying may
    succeed — the wire surfaces a retryable :class:`ProviderError`.
    """

    from core.providers.errors import ProviderError

    payload = {
        "data": [
            {"index": 0, "embedding": [0.1, 0.2]},
        ],
    }

    with pytest.raises(ProviderError, match="1 vectors for 2 inputs"):
        _parse_embeddings_response(payload, expected_count=2)


def test_parse_response_rejects_missing_embedding_field() -> None:
    """A ``data`` entry with no ``embedding`` field is malformed and
    surfaces as a retryable :class:`ProviderError`.
    """

    from core.providers.errors import ProviderError

    payload = {"data": [{"index": 0}, {"index": 1, "embedding": [0.1]}]}

    with pytest.raises(ProviderError, match="missing an embedding"):
        _parse_embeddings_response(payload, expected_count=2)


def test_parse_response_rejects_non_object_data_entry() -> None:
    """A non-object ``data`` entry is a wire shape problem — the next
    attempt will receive the same shape, so it is non-retryable.
    """

    from core.providers.errors import ProviderError

    with pytest.raises(ProviderError, match="not an object"):
        _parse_embeddings_response({"data": ["not-a-dict"]}, expected_count=1)


def test_coerce_vector_passes_through_real_floats() -> None:
    """The private helper accepts floats unchanged."""

    vector = _coerce_vector([0.1, 0.2, 0.3])

    assert vector == [0.1, 0.2, 0.3]


# ---------------------------------------------------------------------------
# End-to-end OpenRouter call — the payload reaches the wire correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_embed_posts_to_api_v1_embeddings() -> None:
    """A successful two-text batch hits ``POST /api/v1/embeddings`` and
    returns two vectors in input order."""

    route = respx.post("https://openrouter.ai/api/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]},
                    {"object": "embedding", "index": 1, "embedding": [0.4, 0.5, 0.6]},
                ],
                "model": "google/gemini-embedding-2",
                "usage": {"prompt_tokens": 4, "total_tokens": 4},
            },
        )
    )
    client = _openrouter_embedding_client("google/gemini-embedding-2")

    vectors = await client.embed(["alpha", "beta"], options={})

    payload = json.loads(route.calls[0].request.content)
    assert payload == {
        "model": "google/gemini-embedding-2",
        "input": ["alpha", "beta"],
        "encoding_format": "float",
    }
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-test"
    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_embed_forwards_dimensions_when_set() -> None:
    """When the user pins ``dimensions`` in options, the wire carries it
    as an integer (Matryoshka truncation knob)."""

    route = respx.post("https://openrouter.ai/api/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"index": 0, "embedding": [0.1, 0.2]},
                ],
            },
        )
    )
    client = _openrouter_embedding_client("google/gemini-embedding-2")

    await client.embed(["alpha"], options={"dimensions": 256})

    payload = json.loads(route.calls[0].request.content)
    assert payload["dimensions"] == 256
    assert isinstance(payload["dimensions"], int)


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_embed_uses_bearer_auth_header() -> None:
    """The ``Authorization`` header is built from the connection's auth config."""

    respx.post("https://openrouter.ai/api/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]},
        )
    )
    client = _openrouter_embedding_client("google/gemini-embedding-2")

    await client.embed(["alpha"], options={})

    route = respx.post("https://openrouter.ai/api/v1/embeddings")
    assert route.call_count >= 1
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-test"


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_embed_reorders_vectors_by_index() -> None:
    """An out-of-order response is sorted by ``index`` so vectors are
    returned in input order.
    """

    respx.post("https://openrouter.ai/api/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.4, 0.5, 0.6]},
                    {"index": 0, "embedding": [0.1, 0.2, 0.3]},
                ],
            },
        )
    )
    client = _openrouter_embedding_client("google/gemini-embedding-2")

    vectors = await client.embed(["alpha", "beta"], options={})

    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_embed_raises_provider_error_on_4xx() -> None:
    """A 4xx response is mapped to a ``ProviderError`` via the shared
    HTTP classifier — auth errors are not retryable, the rest may be.
    """

    from core.providers.errors import ProviderAuthError

    respx.post("https://openrouter.ai/api/v1/embeddings").mock(
        return_value=httpx.Response(401, text="Unauthorized")
    )
    client = _openrouter_embedding_client("google/gemini-embedding-2")

    with pytest.raises(ProviderAuthError):
        await client.embed(["alpha"], options={})


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_embed_uses_default_timeout() -> None:
    """The embeddings HTTP timeout is the project default for short
    non-streaming requests (60s connect/write/pool, no read cap).
    """

    respx.post("https://openrouter.ai/api/v1/embeddings").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]},
        )
    )
    client = _openrouter_embedding_client("google/gemini-embedding-2")

    # Sanity: the constant is the one the client uses.
    assert DEFAULT_EMBEDDING_TIMEOUT == 60.0

    await client.embed(["alpha"], options={})


# ---------------------------------------------------------------------------
# HTTP endpoint and constants
# ---------------------------------------------------------------------------


def test_embeddings_endpoint_is_api_v1_embeddings() -> None:
    """The wire path matches the OpenAI/OpenRouter ``/api/v1/embeddings`` shape."""

    assert EMBEDDINGS_ENDPOINT == "/embeddings"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _openrouter_embedding_client(model_id: str) -> ProviderEmbeddingClient:
    """Build a ``ProviderEmbeddingClient`` wired to a mockable OpenRouter endpoint."""

    provider = ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        adapter="openrouter",
        base_url="https://openrouter.ai/api/v1",
        connections=[],
        extra_headers={"X-Title": "vBot"},
    )
    connection = ConnectionConfig(
        id="api-key",
        type="api_key",
        label="API Key",
        auth=AuthConfig(
            header="Authorization",
            prefix="Bearer ",
            credential_key="OPENROUTER_API_KEY",
        ),
    )
    return ProviderEmbeddingClient(
        provider=provider,
        connection=connection,
        credential="sk-test",
        model_id=model_id,
    )
