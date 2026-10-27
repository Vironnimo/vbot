"""OpenCode Go provider adapter."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, ClassVar, override

import httpx

from core.models.models import Model
from core.providers._responses_profile import (
    RESPONSES_REASONING_FIELDS,
    profile_responses_policy,
    take_reasoning_renderer,
)
from core.providers._responses_wire import (
    ResponsesStreamState,
    build_responses_payload,
    estimate_responses_input_tokens,
    normalize_responses_response,
    responses_returned_reasoning,
    stream_responses_sse,
)
from core.providers._wire_learning import (
    execute_learning_from_rejections,
    stream_learning_from_rejections,
)
from core.providers.adapter import ModelLookup
from core.providers.anthropic_compatible import (
    ANTHROPIC_OVERLOADED_STATUS,
    ANTHROPIC_VERSION,
    AnthropicCompatibleAdapter,
)
from core.providers.errors import ProviderError
from core.providers.gemini_compatible import GeminiCompatibleAdapter
from core.providers.http import (
    PreparedRequest,
    classify_http_status,
    connect_streaming_with_retry,
    format_http_error_detail,
    post_json_with_retry,
)
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.token_getter import TokenGetter
from core.providers.wire_profile import Protocol
from core.providers.wire_profiles import WireBinding
from core.utils.http_status import parse_retry_after

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder
    from core.providers.http import ProviderHTTPTransportScope


OPENCODE_GO_RESPONSES_ENDPOINT = "/responses"
OPENCODE_SESSION_HEADER = "x-opencode-session"
OPENCODE_SESSION_ID_KWARG = "_opencode_session_id"

# OpenCode returns account/subscription exhaustion through the same HTTP 429
# status as transient throttling. These stable error identifiers and phrases
# mean waiting cannot make the same request succeed; retrying only burns time
# and repeats a non-idempotent generation request.
_PERMANENT_RATE_LIMIT_MARKERS = (
    "gousagelimiterror",
    "freeusagelimiterror",
    "monthly usage limit reached",
    "monthly usage limit has been reached",
    "available balance",
    "insufficient_quota",
    "insufficient balance",
    "out of budget",
    "quota exceeded",
    "billing hard limit",
    "billing limit reached",
)


def _raise_if_permanent_rate_limit(status_code: int, detail: str) -> None:
    if status_code != 429:
        return
    normalized_detail = detail.casefold()
    if not any(marker in normalized_detail for marker in _PERMANENT_RATE_LIMIT_MARKERS):
        return
    raise ProviderError(
        f"OpenCode Go subscription limit reached: {detail}",
        retryable=False,
    )


# Observed gateway failures of one upstream behind Go, as (status, error.code,
# error.message) with error.type ``server_error``; a code of None means the body
# carries none. Each arrived under a status the shared policy treats as fatal,
# while the same Connection and request succeeded moments later:
# - 403, 2026-09-20 (Swarm): the upstream answered with invalid JSON.
# - 400, 2026-10-16 (deepseek-v4.1-flash, thinking effort high): one upstream
#   endpoint refused the thinking field the Model otherwise accepts.
_RECOVERABLE_UPSTREAM_FAILURES = frozenset(
    {
        (
            403,
            "server_error",
            "Upstream request failed: [server_error] Upstream response was not valid JSON",
        ),
        (
            400,
            None,
            "Upstream request failed: [unsupported_request] "
            "The thinking request field is not enabled for this endpoint.",
        ),
    }
)
_RECOVERABLE_UPSTREAM_STATUSES = frozenset(
    status for status, _, _ in _RECOVERABLE_UPSTREAM_FAILURES
)


def _raise_if_upstream_failure(
    status_code: int,
    detail: str,
    response_headers: httpx.Headers,
) -> None:
    """Recover the observed gateway failures without relaxing other errors of their status."""
    if status_code not in _RECOVERABLE_UPSTREAM_STATUSES:
        return
    try:
        body = json.loads(detail.removeprefix(f"{status_code} "))
    except json.JSONDecodeError:
        return
    error = body.get("error") if isinstance(body, dict) else None
    if (
        not isinstance(error, dict)
        or error.get("type") != "server_error"
        or (status_code, error.get("code"), error.get("message"))
        not in _RECOVERABLE_UPSTREAM_FAILURES
    ):
        return
    # Keep recovery in the existing caller-owned budget rather than adding
    # another retry loop.
    upstream_error = ProviderError(f"Provider error: {detail}", retryable=True)
    upstream_error.status_code = status_code
    upstream_error.retry_after = parse_retry_after(response_headers)
    raise upstream_error


def _opencode_request_headers(request_kwargs: dict[str, Any]) -> dict[str, str]:
    session_id = request_kwargs.pop(OPENCODE_SESSION_ID_KWARG, None)
    if not isinstance(session_id, str) or not session_id:
        return {}
    return {OPENCODE_SESSION_HEADER: session_id}


class _OpenCodeGoMessagesAdapter(AnthropicCompatibleAdapter):
    """OpenCode Go's Anthropic Messages wire adapter."""

    @staticmethod
    @override
    def _build_error_detail(status_code: int, response_body: str = "") -> str:
        # Gateway classification needs the structured code as well as the type
        # and message. Preserve the complete body on this wire too.
        return format_http_error_detail(status_code, response_body)

    @override
    def _request_headers_from_kwargs(
        self,
        request_kwargs: dict[str, Any],
    ) -> dict[str, str]:
        return _opencode_request_headers(request_kwargs)

    @override
    def _classify_http_status(
        self,
        status_code: int,
        *,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
        _raise_if_upstream_failure(status_code, detail, response_headers)
        _raise_if_permanent_rate_limit(status_code, detail)
        super()._classify_http_status(
            status_code,
            detail=detail,
            response_headers=response_headers,
        )


class _OpenCodeGoGeminiAdapter(GeminiCompatibleAdapter):
    """Go's Gemini route carries its Session affinity and subscription policy."""

    @override
    def _request_headers_from_kwargs(self, request_kwargs: dict[str, Any]) -> dict[str, str]:
        return _opencode_request_headers(request_kwargs)

    @override
    def _classify_http_status(
        self, status_code: int, *, detail: str, response_headers: httpx.Headers
    ) -> None:
        _raise_if_upstream_failure(status_code, detail, response_headers)
        _raise_if_permanent_rate_limit(status_code, detail)
        super()._classify_http_status(status_code, detail=detail, response_headers=response_headers)


class OpenCodeGoAdapter(OpenAICompatibleAdapter):
    """Adapter for the OpenCode Go gateway's four wires.

    The wire profile (``resources/wire/opencode-go.json``) routes each Model to
    Chat Completions (the inherited codec), Anthropic Messages (an inner
    Messages adapter sharing this Adapter's client and wire profiles), native
    Gemini (another shared-client adapter), or stateless Responses (``/responses``),
    and shapes its reasoning, parameters, media and replay. Every wire carries
    the ``x-opencode-session`` prompt-cache
    affinity header and the gateway's error classification, and resolves this
    Adapter's output limit when it builds a payload.
    """

    WIRE_PROTOCOLS: ClassVar[tuple[Protocol, ...]] = (
        "chat_completions",
        "messages",
        "responses",
        "gemini",
    )

    def __init__(
        self,
        config: ProviderConfig,
        token_getter: TokenGetter | str,
        base_url: str | None = None,
        auth_config: AuthConfig | None = None,
        model_lookup: ModelLookup | None = None,
        debug_recorder: ProviderDebugRecorder | None = None,
        *,
        connection_mode: str | None = None,
        http_transport: ProviderHTTPTransportScope | None = None,
    ) -> None:
        # ``connection_mode`` is accepted for parity with the unified
        # ``get_adapter`` call site but is not used by the OpenCode Go
        # adapter; the inner OpenAI-compatible and Anthropic adapters
        # inherit it through the same parameter.
        del connection_mode
        super().__init__(
            config,
            token_getter,
            base_url,
            auth_config,
            model_lookup=model_lookup,
            debug_recorder=debug_recorder,
            http_transport=http_transport,
        )
        selected_auth_config = auth_config or config.connections[0].auth
        # The inner adapter shares the same recorder, client and wire profiles,
        # so the debug context and the Model's profile are the same whichever
        # wire handles the request.
        self._messages = _OpenCodeGoMessagesAdapter(
            config,
            self._token_getter,
            base_url=base_url,
            auth_config=AuthConfig(
                header="x-api-key",
                prefix="",
                credential_key=selected_auth_config.credential_key,
            ),
            model_lookup=model_lookup,
            debug_recorder=debug_recorder,
            client=self._client,
            api_version=ANTHROPIC_VERSION,
            extra_retryable_statuses=frozenset({ANTHROPIC_OVERLOADED_STATUS}),
            stamp_output_limit=self._apply_model_output_limit,
        )
        self._messages.bind_wire_profiles(self.wire)
        self._gemini = _OpenCodeGoGeminiAdapter(
            config,
            self._token_getter,
            base_url=base_url,
            auth_config=AuthConfig(header="x-goog-api-key", prefix=""),
            model_lookup=model_lookup,
            debug_recorder=debug_recorder,
            client=self._client,
        )
        self._gemini.bind_wire_profiles(self.wire)

    @override
    def bind_wire_profiles(self, binding: WireBinding) -> None:
        super().bind_wire_profiles(binding)
        self._messages.bind_wire_profiles(binding)
        self._gemini.bind_wire_profiles(binding)

    @override
    async def aclose(self) -> None:
        await self._messages.aclose()
        await self._gemini.aclose()
        await super().aclose()

    @override
    def request_context_kwargs(
        self,
        *,
        agent_id: str,
        session_id: str,
        project_id: str | None = None,
        prompt_cache_affinity_id: str | None = None,
    ) -> dict[str, Any]:
        """Pin every wire request to one opaque prompt-cache lineage."""

        if prompt_cache_affinity_id is not None:
            routing_id = prompt_cache_affinity_id
        else:
            address = json.dumps(
                [project_id, agent_id, session_id],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            routing_id = hashlib.sha256(address).hexdigest()[:32]
        return {OPENCODE_SESSION_ID_KWARG: f"vbot-{routing_id}"}

    @override
    def _request_headers_from_kwargs(
        self,
        request_kwargs: dict[str, Any],
    ) -> dict[str, str]:
        return _opencode_request_headers(request_kwargs)

    @override
    def estimate_request_input_tokens(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model_id: str,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> int:
        """Estimate the rendered request for the model-selected OpenCode Go wire."""

        protocol = self.wire_profile(model_id).protocol
        if protocol == "gemini":
            return self._gemini.estimate_request_input_tokens(
                messages, model_id=model_id, tools=tools
            )
        if protocol == "responses":
            return estimate_responses_input_tokens(
                [dict(message) for message in messages],
                model_id=model_id,
                tools=tools,
            )
        if protocol == "messages":
            return self._messages.estimate_request_input_tokens(
                messages, model_id=model_id, tools=tools
            )
        return super().estimate_request_input_tokens(
            messages,
            model_id=model_id,
            tools=tools,
        )

    @override
    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        protocol = self.wire_profile(model_id).protocol
        if protocol == "gemini":
            return await self._gemini.send(messages, model_id=model_id, **kwargs)
        if protocol == "messages":
            return await self._messages.send(messages, model_id=model_id, **kwargs)
        if protocol == "responses":
            request_headers = self._stable_request_headers(model_id, kwargs)

            def build() -> dict[str, Any]:
                return self._build_responses_payload(
                    messages, model_id=model_id, **self._request_kwargs_with_defaults(kwargs)
                )

            response = await execute_learning_from_rejections(
                lambda request: self._post_responses_json(request, request_headers=request_headers),
                self._request_plan(messages, model_id, build),
                wire=self.wire,
                model_id=model_id,
                provider_label=self._config.id,
            )
            if responses_returned_reasoning(response):
                self.wire.observe_reasoning_returned(model_id)
            return response
        return await super().send(messages, model_id=model_id, **kwargs)

    @override
    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        protocol = self.wire_profile(model_id).protocol
        if protocol == "gemini":
            return self._gemini.stream(messages, model_id=model_id, **kwargs)
        if protocol == "messages":
            return self._messages.stream(messages, model_id=model_id, **kwargs)
        if protocol == "responses":
            request_headers = self._stable_request_headers(model_id, kwargs)

            def build() -> dict[str, Any]:
                return self._build_responses_payload(
                    messages,
                    model_id=model_id,
                    stream=True,
                    **self._request_kwargs_with_defaults(kwargs),
                )

            return stream_learning_from_rejections(
                lambda request: self._stream_responses(request, request_headers=request_headers),
                self._request_plan(messages, model_id, build),
                wire=self.wire,
                model_id=model_id,
                provider_label=self._config.id,
            )
        return super().stream(messages, model_id=model_id, **kwargs)

    @override
    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        if model_id is not None:
            protocol = self.wire_profile(model_id).protocol
            if protocol == "gemini":
                return self._gemini.normalize_response(response, model_id=model_id)
            if protocol == "messages":
                return self._messages.normalize_response(response, model_id=model_id)
            if protocol == "responses":
                return normalize_responses_response(response)
            return super().normalize_response(response, model_id=model_id)
        if "candidates" in response or "promptFeedback" in response:
            return self._gemini.normalize_response(response)
        if "choices" in response:
            return super().normalize_response(response, model_id=model_id)
        if isinstance(response.get("output"), list):
            return normalize_responses_response(response)
        return self._messages.normalize_response(response, model_id=model_id)

    @override
    def _classify_http_status(
        self,
        status_code: int,
        *,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
        _raise_if_upstream_failure(status_code, detail, response_headers)
        _raise_if_permanent_rate_limit(status_code, detail)
        super()._classify_http_status(
            status_code,
            detail=detail,
            response_headers=response_headers,
        )

    def _build_responses_payload(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        stream: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Build one stateless ``/responses`` request for a Responses-routed Model.

        Complete history every request, every original output item preserved,
        ``store: false`` — the same stateless Responses shape the OpenRouter
        adapter uses. The wire profile shapes the optional parameters and
        renders the reasoning plan. Body-level session routing kwargs are
        dropped because OpenCode Go receives its cache affinity through a
        request header.

        Raises:
            ProviderError: (not retryable, nothing sent) when the wire profile
                does not admit the Model or refuses a request parameter.
        """

        self._refuse_unadmitted_model(model_id)
        kwargs.pop("session_id", None)
        profile = self.wire_profile(model_id)
        reasoning_renderer = take_reasoning_renderer(profile, kwargs)
        payload = build_responses_payload(
            messages,
            model_id=model_id,
            policy=profile_responses_policy(profile, self._catalog_model(model_id)),
            stream=stream,
            reasoning_renderer=reasoning_renderer,
            output_limit=lambda request_kwargs: self._apply_model_output_limit(
                request_kwargs, model_id, messages
            ),
            **kwargs,
        )
        payload["store"] = False
        rules = profile.request
        rules.apply_body(payload)
        # Configured or learned parameter rules; the reasoning fields are
        # the dialect's own output.
        rules.shape_parameters(
            payload,
            reasoning_active="reasoning" in payload,
            protected=RESPONSES_REASONING_FIELDS,
            provider_label=self._config.name,
        )
        return payload

    def _catalog_model(self, model_id: str) -> Model | None:
        if self._model_lookup is None:
            return None
        for candidate in _model_lookup_candidates(model_id):
            model = self._model_lookup(candidate)
            if model is not None:
                return model
        return None

    def _classify_responses_status(
        self,
        status_code: int,
        *,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
        _raise_if_upstream_failure(status_code, detail, response_headers)
        _raise_if_permanent_rate_limit(status_code, detail)
        classify_http_status(
            status_code,
            idempotent=False,
            detail=detail,
            response_headers=response_headers,
        )

    async def _post_responses_json(
        self,
        request: PreparedRequest,
        *,
        request_headers: Mapping[str, str],
    ) -> dict[str, Any]:
        return await post_json_with_retry(
            self._client,
            OPENCODE_GO_RESPONSES_ENDPOINT,
            request.body,
            build_headers=lambda: self._responses_attempt_headers(request, request_headers),
            handle_error_status=self._handle_responses_error_status,
            provider_context="OpenCode Go Responses",
        )

    async def _stream_responses(
        self,
        request: PreparedRequest,
        *,
        request_headers: Mapping[str, str],
    ) -> AsyncGenerator[dict[str, Any]]:
        response = await self._connect_responses_stream(
            request,
            request_headers=request_headers,
        )
        state = ResponsesStreamState()
        async with aclosing(stream_responses_sse(response, state)) as deltas:
            async for delta in deltas:
                yield delta

    async def _connect_responses_stream(
        self,
        request: PreparedRequest,
        *,
        request_headers: Mapping[str, str],
    ) -> httpx.Response:
        return await connect_streaming_with_retry(
            self._client,
            OPENCODE_GO_RESPONSES_ENDPOINT,
            request.body,
            build_headers=lambda: self._responses_attempt_headers(request, request_headers),
            handle_error_status=self._handle_responses_error_status,
        )

    async def _responses_attempt_headers(
        self, request: PreparedRequest, request_headers: Mapping[str, str]
    ) -> dict[str, str]:
        headers = await self._build_headers()
        headers.update(request.headers)
        headers.update(request_headers)
        return headers

    def _handle_responses_error_status(
        self, status_code: int, error_body: str, response_headers: httpx.Headers
    ) -> None:
        self._classify_responses_status(
            status_code,
            detail=format_http_error_detail(status_code, error_body),
            response_headers=response_headers,
        )

    @override
    def _model_max_output_tokens(self, model_id: str) -> int | None:
        if self._model_lookup is None:
            return None

        for candidate in _model_lookup_candidates(model_id):
            model = self._model_lookup(candidate)
            if (
                model is not None
                and model.max_output_tokens is not None
                and model.max_output_tokens > 0
            ):
                return model.max_output_tokens
        return None

    @override
    def _model_context_window(self, model_id: str) -> int | None:
        if self._model_lookup is None:
            return None

        for candidate in _model_lookup_candidates(model_id):
            model = self._model_lookup(candidate)
            if model is not None and model.context_window is not None and model.context_window > 0:
                return model.context_window
        return None


def _model_lookup_candidates(model_id: str) -> tuple[str, ...]:
    without_connection_suffix = model_id.split("::", 1)[0]
    candidates = [model_id, without_connection_suffix]
    if "/" in without_connection_suffix:
        candidates.append(without_connection_suffix.rsplit("/", 1)[-1])
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))
