"""Shared HTTP plumbing for provider-backed task-model clients.

The provider-backed specialized-task domains each bind one resolved
``(provider, connection, token_getter, model_id)`` tuple to a small
OpenAI-compatible HTTP client. :class:`ProviderTaskClient` owns that
shared plumbing — target resolution from a runtime handle, auth
headers, the POST/classify/parse request cycle, and retry semantics —
while each domain keeps its own payload shaping and response parsing.

Task-specific execution lives in the per-task ``*_providers`` wire clients in
:mod:`core.model_tasks`; only the shared wire plumbing lives here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, Self, TypeVar
from uuid import uuid4

import httpx

from core.providers._http_shared import classify_http_status, wrap_network_error
from core.providers.accounts import ConnectionRef
from core.providers.errors import (
    ProviderError,
    ProviderOutcomeUnknownError,
    ProviderRateLimitError,
)
from core.providers.token_getter import StaticTokenGetter, TokenGetter
from core.utils.retry import retry_async

JsonObject = dict[str, Any]
ParsedResultT = TypeVar("ParsedResultT")
HeaderBuilder = Callable[[], Awaitable[dict[str, str]]]

# Option name of the JSON escape hatch every provider task target carries:
# a free-form object adding provider-specific request fields not authored by
# the task wire client, so an option vBot does not surface stays usable without
# a code change. Owned here because all task wire clients share the semantics.
EXTRA_OPTIONS_KEY = "extra_options"


@dataclass(frozen=True)
class TaskRequestRetryPolicy:
    """Retry contract for one task-provider endpoint.

    ``replay_safe`` covers intrinsically idempotent requests. A verified
    provider idempotency header also makes ambiguous retries safe; one logical
    operation key is generated outside the retry loop and reused on every
    attempt. ``verified_safe_retry_status_codes`` covers statuses whose endpoint
    contract proves that the operation was not processed.
    """

    replay_safe: bool = True
    verified_safe_retry_status_codes: frozenset[int] = frozenset()
    idempotency_header_name: str | None = None

    def __post_init__(self) -> None:
        if self.idempotency_header_name is not None and not self.idempotency_header_name.strip():
            raise ValueError("idempotency_header_name must be non-empty when provided")

    @property
    def can_replay_after_ambiguous_failure(self) -> bool:
        return self.replay_safe or self.idempotency_header_name is not None


DEFAULT_TASK_REQUEST_RETRY_POLICY = TaskRequestRetryPolicy()
NON_IDEMPOTENT_TASK_REQUEST_RETRY_POLICY = TaskRequestRetryPolicy(replay_safe=False)


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
    """Add ``extra_options`` fields without overriding authored payload fields.

    Empty placeholder values are dropped like everywhere else. A collision is
    rejected before mutating *payload* so the escape hatch cannot silently
    redirect a request or replace task-owned content and controls.
    """

    extra = options.get(EXTRA_OPTIONS_KEY)
    if not isinstance(extra, dict):
        return
    additions = {
        key: value
        for key, value in extra.items()
        if isinstance(key, str) and key and not is_omittable_option(value)
    }
    collisions = sorted(payload.keys() & additions.keys())
    if collisions:
        raise ProviderError(
            "extra_options cannot override request fields: " + ", ".join(collisions),
            retryable=False,
        )
    payload.update(additions)


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

    def get_connection_token_getter(self, connection: ConnectionRef) -> TokenGetter:
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

    EXTRA_RETRYABLE_STATUS_CODES: frozenset[int] = frozenset()

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
            ConnectionRef(
                target_ref.provider_id,
                target_ref.connection_id,
            )
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
        retry_policy: TaskRequestRetryPolicy = DEFAULT_TASK_REQUEST_RETRY_POLICY,
    ) -> ParsedResultT:
        """POST to *endpoint*, classify the status, and parse the response.

        The whole cycle — request, status classification, and the
        *parse* callback — runs inside :func:`retry_async`, so parse
        failures raised as retryable ``ProviderError``s are retried the
        same way transient network/HTTP errors are when the endpoint is replay
        safe. Non-idempotent endpoints suppress ambiguous retries unless their
        policy declares an explicit safe status or verified idempotency header.
        """

        operation_key = uuid4().hex

        async def _do_request() -> ParsedResultT:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=timeout,
            ) as client:
                request_headers = dict(await (headers or self._headers)())
                if retry_policy.idempotency_header_name is not None:
                    request_headers[retry_policy.idempotency_header_name] = operation_key
                try:
                    response = await client.post(
                        endpoint,
                        json=json,
                        data=data,
                        files=files,
                        headers=request_headers,
                    )
                except httpx.TransportError as exc:
                    raise _task_transport_error(
                        exc,
                        retry_policy=retry_policy,
                        operation_key=operation_key,
                    ) from exc
                _classify_task_response_for_retry_policy(
                    response,
                    retry_policy=retry_policy,
                    operation_key=operation_key,
                    extra_retryable_status_codes=self.EXTRA_RETRYABLE_STATUS_CODES,
                )
                try:
                    return parse(response)
                except ProviderOutcomeUnknownError:
                    raise
                except (ProviderError, ValueError) as exc:
                    if retry_policy.can_replay_after_ambiguous_failure:
                        raise
                    raise _outcome_unknown(
                        operation_key,
                        f"the provider returned HTTP {response.status_code}, but vBot could not "
                        f"confirm a usable result: {exc}",
                    ) from exc

        return await retry_async(_do_request)

    async def get_and_parse(
        self,
        endpoint: str,
        *,
        timeout: float,
        parse: Callable[[httpx.Response], ParsedResultT],
    ) -> ParsedResultT:
        """GET *endpoint* with task auth, retrying the replay-safe whole cycle."""

        async def _do_request() -> ParsedResultT:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=timeout,
            ) as client:
                try:
                    response = await client.get(endpoint, headers=await self._headers())
                except httpx.TransportError as exc:
                    raise wrap_network_error(exc) from exc
                # A GET is replay-safe by contract here (status polls, result
                # downloads): a transient 500 retries instead of aborting hard.
                classify_task_response(
                    response,
                    extra_retryable_status_codes=self.EXTRA_RETRYABLE_STATUS_CODES,
                    idempotent=True,
                )
                return parse(response)

        return await retry_async(_do_request)

    async def _credential_value(self) -> str:
        """Return the current credential value for this request attempt."""

        return await self._token_getter()

    def _headers_from_credential(self, credential: str) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._connection.type != "none":
            auth = self._connection.auth
            headers[auth.header] = f"{auth.prefix}{credential}"
        if self._provider.extra_headers:
            headers.update(self._provider.extra_headers)
        return headers

    async def _headers(self) -> dict[str, str]:
        return self._headers_from_credential(await self._credential_value())


def _task_transport_error(
    error: httpx.TransportError,
    *,
    retry_policy: TaskRequestRetryPolicy,
    operation_key: str,
) -> Exception:
    """Classify a transport failure without replaying an ambiguous operation."""

    if retry_policy.can_replay_after_ambiguous_failure:
        return wrap_network_error(error)
    if isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return wrap_network_error(error)
    if isinstance(error, (httpx.LocalProtocolError, httpx.UnsupportedProtocol)):
        return ProviderError(f"Provider request could not be sent: {error}", retryable=False)
    return _outcome_unknown(
        operation_key,
        f"the provider connection failed after the request may have been sent: {error}",
    )


def _classify_task_response_for_retry_policy(
    response: httpx.Response,
    *,
    retry_policy: TaskRequestRetryPolicy,
    operation_key: str,
    extra_retryable_status_codes: frozenset[int],
) -> None:
    verified_status_codes = retry_policy.verified_safe_retry_status_codes
    try:
        classify_task_response(
            response,
            extra_retryable_status_codes=extra_retryable_status_codes | verified_status_codes,
        )
    except ProviderRateLimitError:
        # 429 is an explicit refusal rather than an ambiguous execution result.
        raise
    except ProviderError as exc:
        if retry_policy.can_replay_after_ambiguous_failure:
            raise
        if response.status_code in verified_status_codes:
            raise
        if response.status_code >= 500 or exc.retryable:
            raise _outcome_unknown(
                operation_key,
                f"the provider returned an ambiguous HTTP {response.status_code} response: {exc}",
            ) from exc
        raise


def _outcome_unknown(operation_key: str, message: str) -> ProviderOutcomeUnknownError:
    return ProviderOutcomeUnknownError(
        f"{message}. Automatic retry was suppressed because repeating the request could "
        "duplicate a completed or billed result",
        operation_key=operation_key,
    )


def classify_task_response(
    response: httpx.Response,
    *,
    extra_retryable_status_codes: frozenset[int] = frozenset(),
    idempotent: bool = False,
) -> None:
    """Classify a task HTTP response, including body detail on error.

    Callers declare idempotency explicitly so replay-safe reads and billed
    non-idempotent generation cannot share the wrong retry branch: a status
    poll or result download GET may retry HTTP 500, a generation POST may not.
    """

    detail = response.text if response.status_code >= 400 else ""
    classify_http_status(
        response.status_code,
        idempotent=idempotent,
        extra_retryable=set(extra_retryable_status_codes),
        detail=f"{response.status_code} {detail}".strip() if detail else str(response.status_code),
        response_headers=response.headers,
    )
