"""Shared HTTP plumbing for provider-backed task-model clients.

The speech, image, and embeddings domains each bind one resolved
``(provider, connection, token_getter, model_id)`` tuple to a small
OpenAI-compatible HTTP client. :class:`ProviderTaskClient` owns that
shared plumbing — target resolution from a runtime handle, auth
headers, the POST/classify/parse request cycle, and retry semantics —
while each domain keeps its own payload shaping and response parsing.

Task-specific execution lives in the per-task wire clients in
:mod:`core.model_tasks` (``speech_providers``, ``image_providers``,
``embeddings_providers``); only the wire plumbing lives here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, Self, TypeVar

import httpx

from core.providers._http_shared import classify_http_status, wrap_network_error
from core.providers.token_getter import StaticTokenGetter, TokenGetter
from core.utils.retry import retry_async

JsonObject = dict[str, Any]
ParsedResultT = TypeVar("ParsedResultT")
HeaderBuilder = Callable[[], Awaitable[dict[str, str]]]

# Option name of the JSON escape hatch every provider task target carries:
# a free-form object merged into the request payload by the task wire
# clients, so an option vBot does not surface stays usable without a code
# change. Owned here because all task wire clients share the semantics.
EXTRA_OPTIONS_KEY = "extra_options"


def is_omittable_option(value: Any) -> bool:
    """True when an option value carries nothing to forward to the provider.

    Empty placeholders (``None``, ``""``, ``[]``, ``{}``) are option-form
    defaults that mean "unset" — they are injected by the schema's
    ``default_options`` for optional text/json fields and must not reach the
    wire (providers reject stray empties). Real values such as numeric
    ``0``/``0.0`` and ``False`` carry information and are kept.
    """

    if value is None:
        return True
    return isinstance(value, str | list | dict) and len(value) == 0


def merge_extra_options(payload: JsonObject, options: JsonObject) -> None:
    """Merge the ``extra_options`` escape hatch into *payload* (extra wins).

    Letting the escape hatch override authored keys keeps it a true last
    word; empty placeholder values are dropped like everywhere else.
    """

    extra = options.get(EXTRA_OPTIONS_KEY)
    if not isinstance(extra, dict):
        return
    for key, value in extra.items():
        if isinstance(key, str) and key and not is_omittable_option(value):
            payload[key] = value


class _ProviderLookupProtocol(Protocol):
    """Provider-config lookup surface used during target resolution."""

    def get(self, provider_id: str) -> Any: ...


class TaskClientRuntime(Protocol):
    """The runtime surface a provider task client needs.

    Defined locally (not imported from ``core.runtime.interfaces``)
    because a runtime import of the ``core.runtime`` package would pull
    in the full ``Runtime`` bootstrap and create an import cycle with
    the task domains.
    """

    @property
    def providers(self) -> _ProviderLookupProtocol: ...

    def get_connection_token_getter(self, provider_id: str, connection_id: str) -> TokenGetter:
        """Return a refresh-capable token getter for one provider connection."""
        ...


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
        model_id: str,
        credential: str | None = None,
        token_getter: TokenGetter | None = None,
    ) -> None:
        if token_getter is None:
            if credential is None:
                raise ValueError("Provider task clients require a credential or token getter")
            token_getter = StaticTokenGetter(credential)
        self._provider = provider
        self._connection = connection
        self._token_getter = token_getter
        self._model_id = model_id
        self._base_url = connection.base_url or provider.base_url

    @classmethod
    def from_runtime(cls, runtime: TaskClientRuntime, target_ref: TaskTargetRef) -> Self:
        """Create a client from runtime provider configuration and credentials."""

        provider = runtime.providers.get(target_ref.provider_id)
        connection = provider.get_connection(target_ref.local_connection_id)
        token_getter = runtime.get_connection_token_getter(
            target_ref.provider_id,
            target_ref.connection_id,
        )
        return cls(
            provider=provider,
            connection=connection,
            model_id=target_ref.model_id,
            token_getter=token_getter,
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
        headers: HeaderBuilder | None = None,
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
                        headers=await (headers or self._headers)(),
                    )
                except httpx.TransportError as exc:
                    # Classify every transport failure (timeout, read/write,
                    # protocol, proxy, connect) the way the chat adapters do, so
                    # a flaky read is retried instead of escaping unwrapped.
                    raise wrap_network_error(exc) from exc
                classify_task_response(response)
                return parse(response)

        return await retry_async(_do_request)

    async def _credential_value(self) -> str:
        """Return the current credential value for this request attempt."""

        return await self._token_getter()

    def _headers_from_credential(self, credential: str) -> dict[str, str]:
        auth = self._connection.auth
        headers = {auth.header: f"{auth.prefix}{credential}"}
        if self._provider.extra_headers:
            headers.update(self._provider.extra_headers)
        return headers

    async def _headers(self) -> dict[str, str]:
        return self._headers_from_credential(await self._credential_value())


def classify_task_response(response: httpx.Response) -> None:
    """Classify a task HTTP response, including body detail on error."""

    detail = response.text if response.status_code >= 400 else ""
    classify_http_status(
        response.status_code,
        detail=f"{response.status_code} {detail}".strip() if detail else str(response.status_code),
        response_headers=response.headers,
    )
