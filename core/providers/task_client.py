"""Shared HTTP plumbing for provider-backed task-model clients.

The speech, image, and embeddings domains each bind one resolved
``(provider, connection, credential, model_id)`` tuple to a small
OpenAI-compatible HTTP client. :class:`ProviderTaskClient` owns that
shared plumbing — target resolution from a runtime handle, auth
headers, the POST/classify/parse request cycle, and retry semantics —
while each domain keeps its own payload shaping and response parsing.

Task-specific execution lives in the per-task wire clients in
:mod:`core.model_tasks` (``speech_providers``, ``image_providers``,
``embeddings_providers``); only the wire plumbing lives here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, Self, TypeVar

import httpx

from core.providers._http_shared import classify_http_status, wrap_network_error
from core.utils.retry import retry_async

JsonObject = dict[str, Any]
ParsedResultT = TypeVar("ParsedResultT")


class _ProviderLookupProtocol(Protocol):
    """Provider-config lookup surface used during target resolution."""

    def get(self, provider_id: str) -> Any: ...


class _ProviderCredentialsProtocol(Protocol):
    """Credential resolution surface used during target resolution."""

    def get_credentials(self, provider_id: str, connection_id: str | None = None) -> str: ...


class TaskClientRuntime(Protocol):
    """The runtime surface a provider task client needs.

    Defined locally (not imported from ``core.runtime.interfaces``)
    because a runtime import of the ``core.runtime`` package would pull
    in the full ``Runtime`` bootstrap and create an import cycle with
    the task domains.
    """

    @property
    def providers(self) -> _ProviderLookupProtocol: ...

    @property
    def provider_credentials(self) -> _ProviderCredentialsProtocol: ...


class TaskTargetRef(Protocol):
    """Structural shape of a parsed provider task-model target.

    Mirrors the fields of ``core.model_tasks.TaskModelTargetRef`` that
    target resolution reads; typed structurally so this module never
    imports from ``core.model_tasks`` (import-cycle risk).
    """

    @property
    def provider_id(self) -> str: ...

    @property
    def model_id(self) -> str: ...

    @property
    def connection_id(self) -> str: ...

    @property
    def local_connection_id(self) -> str: ...


class ProviderTaskClient:
    """Base HTTP client bound to one resolved provider task target."""

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
    def from_runtime(cls, runtime: TaskClientRuntime, target_ref: TaskTargetRef) -> Self:
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

    async def post_and_parse(
        self,
        endpoint: str,
        *,
        timeout: float,
        parse: Callable[[httpx.Response], ParsedResultT],
        json: JsonObject | None = None,
        data: dict[str, str] | None = None,
        files: Any | None = None,
    ) -> ParsedResultT:
        """POST to *endpoint*, classify the status, and parse the response.

        The whole cycle — request, status classification, and the
        *parse* callback — runs inside :func:`retry_async`, so parse
        failures raised as retryable ``ProviderError``s are retried the
        same way transient network/HTTP errors are.
        """

        async def _do_request() -> ParsedResultT:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=timeout,
            ) as client:
                try:
                    response = await client.post(
                        endpoint,
                        json=json,
                        data=data,
                        files=files,
                        headers=self._headers(),
                    )
                except httpx.TransportError as exc:
                    # Classify every transport failure (timeout, read/write,
                    # protocol, proxy, connect) the way the chat adapters do, so
                    # a flaky read is retried instead of escaping unwrapped.
                    raise wrap_network_error(exc) from exc
                classify_task_response(response)
                return parse(response)

        return await retry_async(_do_request)

    def _headers(self) -> dict[str, str]:
        auth = self._connection.auth
        headers = {auth.header: f"{auth.prefix}{self._credential}"}
        if self._provider.extra_headers:
            headers.update(self._provider.extra_headers)
        return headers


def classify_task_response(response: httpx.Response) -> None:
    """Classify a task HTTP response, including body detail on error."""

    detail = response.text if response.status_code >= 400 else ""
    classify_http_status(
        response.status_code,
        detail=f"{response.status_code} {detail}".strip() if detail else str(response.status_code),
        response_headers=response.headers,
    )
