"""Provider HTTP client for the `text_embedding` task-model binding.

OpenAI-compatible embeddings POST ``/api/v1/embeddings`` with a single
``input`` string or an array of strings, return ``data[].embedding``
floats in the same order as the request (sort/zip by ``index`` to be
safe), and let callers pin ``encoding_format="float"`` and (Matryoshka)
``dimensions`` when the model supports it. The shape is verified
against OpenRouter's :code:`/api/v1/embeddings` endpoint — the same
shape the standard OpenAI platform endpoint returns — so the client
is reusable for any provider that mirrors that contract.

Mirrors :mod:`core.image.providers` and :mod:`core.speech.providers`:
the client takes the resolved provider/connection/credential/model
tuple, builds an ``httpx`` request with the connection's auth header,
and runs it through :func:`core.utils.retry.retry_async` so transient
network/HTTP errors follow the project retry policy.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from core.providers._http_shared import classify_http_status, wrap_network_error
from core.providers.errors import ProviderError
from core.utils.retry import retry_async

JsonObject = dict[str, Any]
EMBEDDINGS_ENDPOINT = "/embeddings"
DEFAULT_EMBEDDING_TIMEOUT = 60.0
_PAYLOAD_DETAIL_LIMIT = 500


class ProviderEmbeddingClient:
    """OpenAI-compatible embedding HTTP client bound to one target."""

    def __init__(
        self,
        *,
        provider: Any,
        connection: Any,
        credential: str,
        model_id: str,
    ) -> None:
        self._provider = provider
        self._connection = connection
        self._credential = credential
        self._model_id = model_id
        self._base_url = connection.base_url or provider.base_url

    @classmethod
    def from_runtime(cls, runtime: Any, target_ref: Any) -> ProviderEmbeddingClient:
        """Create a client from runtime provider configuration and credentials."""

        provider = runtime.providers.get(target_ref.provider_id)
        connection = provider.get_connection(target_ref.local_connection_id)
        credential = runtime.provider_credentials.get_credentials(
            target_ref.provider_id,
            target_ref.connection_id,
        )
        return cls(
            provider=provider,
            connection=connection,
            credential=credential,
            model_id=target_ref.model_id,
        )

    async def embed(self, inputs: list[str], *, options: JsonObject) -> list[list[float]]:
        """Call the provider's ``/api/v1/embeddings`` endpoint.

        *inputs* is forwarded verbatim as the ``input`` array — the
        wire contract accepts a single string or an array, and we always
        pass an array so callers can batch. *options* is the merged
        task-model options dict; only ``dimensions`` is currently
        forwarded (when set as a real value — empty placeholders are
        dropped so the provider does not see a stray ``null``).
        """

        payload = _build_embeddings_payload(self._model_id, inputs, options)

        async def _do_request() -> list[list[float]]:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=DEFAULT_EMBEDDING_TIMEOUT,
            ) as client:
                try:
                    response = await client.post(
                        EMBEDDINGS_ENDPOINT,
                        json=payload,
                        headers=self._headers(),
                    )
                except (httpx.TimeoutException, httpx.ConnectError) as exc:
                    raise wrap_network_error(exc) from exc
                _classify_embeddings_response(response)
                return _parse_embeddings_response(response.json(), expected_count=len(inputs))

        return await retry_async(_do_request)

    def _headers(self) -> dict[str, str]:
        auth = self._connection.auth
        headers = {auth.header: f"{auth.prefix}{self._credential}"}
        if self._provider.extra_headers:
            headers.update(self._provider.extra_headers)
        return headers


def _build_embeddings_payload(
    model_id: str,
    inputs: list[str],
    options: JsonObject,
) -> JsonObject:
    """Build the OpenAI/OpenRouter ``/api/v1/embeddings`` request payload.

    ``input`` is always an array — a single-element array is the
    OpenAI-compatible way to request one embedding. ``encoding_format``
    is pinned to ``"float"`` for this iteration (the catalog does not
    surface a base64 mode, and decoding base64 floats would complicate
    the recall store). ``dimensions`` is forwarded only when it carries
    a real value (Matryoshka models use it to truncate the embedding
    length; the schema injects an empty default that we drop here so
    non-Matryoshka models never see the field).
    """

    payload: JsonObject = {
        "model": model_id,
        "input": list(inputs),
        "encoding_format": "float",
    }
    dimensions = options.get("dimensions")
    if isinstance(dimensions, int) and not isinstance(dimensions, bool) and dimensions > 0:
        payload["dimensions"] = dimensions
    elif isinstance(dimensions, float) and not isinstance(dimensions, bool) and dimensions > 0:
        payload["dimensions"] = int(dimensions)
    return payload


def _classify_embeddings_response(response: httpx.Response) -> None:
    """Classify an embeddings HTTP response, including body detail on error."""

    detail = response.text if response.status_code >= 400 else ""
    classify_http_status(
        response.status_code,
        detail=f"{response.status_code} {detail}".strip() if detail else str(response.status_code),
    )


def _parse_embeddings_response(payload: Any, *, expected_count: int) -> list[list[float]]:
    """Normalize ``data[].embedding`` into vectors in input order.

    The OpenAI/OpenRouter response shape is
    ``{"data": [{"index": <int>, "embedding": [<float>, ...]}, ...]}``
    where ``data`` follows the request order. We sort by ``index`` so
    out-of-order responses still return vectors in input order — the
    upstream behavior is documented as input-ordered, but the verified
    shape ships with explicit ``index`` fields and we do not want a
    silent reorder to land in the recall store.
    """

    if not isinstance(payload, dict):
        raise ProviderError(
            f"Embeddings response must be a JSON object: {_describe_payload(payload)}",
            retryable=False,
        )
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        # OpenRouter reports routing/credit/availability failures as an
        # ``error`` object with an HTTP 200, so a missing ``data`` array
        # usually carries the real reason. Surface it instead of a bare
        # "no data", and treat a definitive ``error`` as non-retryable —
        # retrying returns the same error. A genuinely empty ``data: []``
        # with no error may be a transient blip, so that stays retryable.
        has_error = bool(payload.get("error"))
        raise ProviderError(
            f"Embeddings response contains no data: {_describe_payload(payload)}",
            retryable=not has_error,
        )

    indexed: list[tuple[int, list[float]]] = []
    for entry in data:
        if not isinstance(entry, dict):
            raise ProviderError(
                "Embeddings response data entry is not an object",
                retryable=False,
            )
        index = entry.get("index")
        embedding = entry.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise ProviderError(
                "Embeddings response data entry is missing an embedding",
                retryable=True,
            )
        normalized_index = index if isinstance(index, int) else len(indexed)
        normalized_vector = _coerce_vector(embedding)
        indexed.append((normalized_index, normalized_vector))

    indexed.sort(key=lambda pair: pair[0])
    vectors = [vector for _, vector in indexed]

    if expected_count and len(vectors) != expected_count:
        # ``expected_count`` mismatches are a retryable shape problem —
        # the next attempt may return a complete batch.
        raise ProviderError(
            f"Embeddings response returned {len(vectors)} vectors for {expected_count} inputs",
            retryable=True,
        )
    return vectors


def _describe_payload(payload: Any) -> str:
    """Summarize an unexpected embeddings payload for the error message.

    A 200 with no usable ``data`` array is almost always an OpenRouter
    ``error`` object (``{"error": {"message": ..., "code": ...}}``) —
    routing, credit, or model-availability failures arrive this way. We
    extract that message so the failure is diagnosable from the log
    instead of a bare "no data". When there is no ``error`` object, we
    fall back to a truncated JSON dump of whatever did arrive.
    """

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            parts: list[str] = []
            message = error.get("message")
            if message:
                parts.append(str(message))
            code = error.get("code")
            if code is not None:
                parts.append(f"code={code}")
            if parts:
                return "; ".join(parts)
        elif isinstance(error, str) and error:
            return error
    try:
        rendered = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = repr(payload)
    if len(rendered) > _PAYLOAD_DETAIL_LIMIT:
        rendered = rendered[:_PAYLOAD_DETAIL_LIMIT] + "…"
    return rendered


def _coerce_vector(raw: list[Any]) -> list[float]:
    """Coerce a JSON-decoded embedding list into a strict ``list[float]``.

    Decimals, ints, and floats all pass through :class:`float`; non-
    numeric entries raise as ``ProviderError(retryable=False)`` because
    the wire shape is broken and retrying cannot help.
    """

    vector: list[float] = []
    for value in raw:
        if isinstance(value, bool):
            # Booleans are technically ints in Python; reject them so a
            # weird catalog cannot land a ``True``/``False`` in a vector.
            raise ProviderError(
                "Embeddings response embedding contains a non-numeric value",
                retryable=False,
            )
        if isinstance(value, (int, float)):
            vector.append(float(value))
            continue
        raise ProviderError(
            "Embeddings response embedding contains a non-numeric value",
            retryable=False,
        )
    return vector
