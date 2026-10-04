"""OpenCode Go provider adapter."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, override

import httpx

from core.models.models import Model
from core.providers._http_shared import (
    classify_http_status,
    connect_streaming_with_retry,
    decode_response_json,
    format_http_error_detail,
    iter_stream_lines,
    wrap_network_error,
)
from core.providers._responses_profile import (
    RESPONSES_REASONING_FIELDS,
    profile_responses_policy,
    take_reasoning_renderer,
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
from core.providers.errors import NetworkError, ProviderError
from core.providers.github_copilot_responses import (
    ResponsesStreamState,
    build_responses_payload,
    estimate_responses_input_tokens,
    iter_responses_sse_deltas_with_state,
    normalize_responses_response,
    responses_returned_reasoning,
)
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.token_getter import TokenGetter
from core.providers.wire_profile import Protocol
from core.providers.wire_profiles import WireBinding
from core.utils.http_status import parse_retry_after
from core.utils.retry import retry_async

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder


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


def _raise_if_upstream_json_failure(
    status_code: int,
    detail: str,
    response_headers: httpx.Headers,
) -> None:
    """Recover the observed gateway failure without relaxing ordinary auth errors."""
    if status_code != 403:
        return
    try:
        body = json.loads(detail.removeprefix(f"{status_code} "))
    except json.JSONDecodeError:
        return
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict) or (
        error.get("type") != "server_error"
        or error.get("code") != "server_error"
        or error.get("message")
        != "Upstream request failed: [server_error] Upstream response was not valid JSON"
    ):
        return
    # Live Go responses use 403 for this upstream failure despite subsequent
    # successful requests with the same credentials. Keep recovery in the
    # existing caller-owned budget rather than adding another retry loop.
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
        _raise_if_upstream_json_failure(status_code, detail, response_headers)
        _raise_if_permanent_rate_limit(status_code, detail)
        super()._classify_http_status(
            status_code,
            detail=detail,
            response_headers=response_headers,
        )


class OpenCodeGoAdapter(OpenAICompatibleAdapter):
    """Adapter for the OpenCode Go gateway's three wires.

    The wire profile (``resources/wire/opencode-go.json``) routes each Model to
    Chat Completions (the inherited codec), Anthropic Messages (an inner
    Messages adapter sharing this Adapter's client and wire profiles) or
    stateless Responses (``/responses``), and shapes its reasoning, parameters,
    media and replay. Every wire carries the ``x-opencode-session`` prompt-cache
    affinity header and the gateway's error classification.
    """

    WIRE_PROTOCOLS: ClassVar[tuple[Protocol, ...]] = ("chat_completions", "messages", "responses")

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
        )
        self._messages.bind_wire_profiles(self.wire)

    @override
    def bind_wire_profiles(self, binding: WireBinding) -> None:
        super().bind_wire_profiles(binding)
        self._messages.bind_wire_profiles(binding)

    @override
    async def aclose(self) -> None:
        await self._messages.aclose()
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
        request_kwargs = self._kwargs_with_model_output_limit(model_id, messages, kwargs)
        protocol = self.wire_profile(model_id).protocol
        if protocol == "messages":
            return await self._messages.send(
                messages,
                model_id=model_id,
                **request_kwargs,
            )
        if protocol == "responses":
            request_headers = self._stable_request_headers(model_id, request_kwargs)

            def build() -> dict[str, Any]:
                return self._build_responses_payload(
                    messages,
                    model_id=model_id,
                    **self._request_kwargs_with_defaults(request_kwargs),
                )

            payload = build()
            response = await execute_learning_from_rejections(
                lambda: self._post_responses_json(payload, request_headers=request_headers),
                payload,
                rebuild=build,
                wire=self.wire,
                model_id=model_id,
                provider_label=self._config.id,
            )
            if responses_returned_reasoning(response):
                self.wire.observe_reasoning_returned(model_id)
            return response
        return await super().send(messages, model_id=model_id, **request_kwargs)

    @override
    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        request_kwargs = self._kwargs_with_model_output_limit(model_id, messages, kwargs)
        protocol = self.wire_profile(model_id).protocol
        if protocol == "messages":
            return self._messages.stream(
                messages,
                model_id=model_id,
                **request_kwargs,
            )
        if protocol == "responses":
            request_headers = self._stable_request_headers(model_id, request_kwargs)

            def build() -> dict[str, Any]:
                return self._build_responses_payload(
                    messages,
                    model_id=model_id,
                    stream=True,
                    **self._request_kwargs_with_defaults(request_kwargs),
                )

            payload = build()
            return stream_learning_from_rejections(
                lambda: self._stream_responses(payload, request_headers=request_headers),
                payload,
                rebuild=build,
                wire=self.wire,
                model_id=model_id,
                provider_label=self._config.id,
            )
        return super().stream(messages, model_id=model_id, **request_kwargs)

    @override
    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        if model_id is not None:
            protocol = self.wire_profile(model_id).protocol
            if protocol == "messages":
                return self._messages.normalize_response(response, model_id=model_id)
            if protocol == "responses":
                return normalize_responses_response(response)
            return super().normalize_response(response, model_id=model_id)
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
        _raise_if_upstream_json_failure(status_code, detail, response_headers)
        _raise_if_permanent_rate_limit(status_code, detail)
        super()._classify_http_status(
            status_code,
            detail=detail,
            response_headers=response_headers,
        )

    def _request_kwargs_with_defaults(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        """Merge provider ``defaults`` under caller kwargs for the Responses route.

        The Chat Completions path applies defaults inside the shared
        ``_build_payload``; this custom route bypasses it, so it mirrors the
        OpenRouter adapter's explicit merge (caller kwargs win).
        """

        request_kwargs: dict[str, Any] = {}
        if self._config.defaults:
            request_kwargs.update(self._config.defaults)
        request_kwargs.update(kwargs)
        return request_kwargs

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
        _raise_if_upstream_json_failure(status_code, detail, response_headers)
        _raise_if_permanent_rate_limit(status_code, detail)
        classify_http_status(
            status_code,
            idempotent=False,
            detail=detail,
            response_headers=response_headers,
        )

    async def _post_responses_json(
        self,
        payload: dict[str, Any],
        *,
        request_headers: Mapping[str, str],
    ) -> dict[str, Any]:
        async def _do_request() -> dict[str, Any]:
            headers = await self._build_headers()
            headers.update(request_headers)
            try:
                response = await self._client.post(
                    OPENCODE_GO_RESPONSES_ENDPOINT,
                    json=payload,
                    headers=headers,
                )
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc
            self._classify_responses_status(
                response.status_code,
                detail=_opencode_go_http_error_detail(response),
                response_headers=response.headers,
            )
            return dict(decode_response_json(response, "OpenCode Go Responses"))

        return await retry_async(_do_request)

    async def _stream_responses(
        self,
        payload: dict[str, Any],
        *,
        request_headers: Mapping[str, str],
    ) -> AsyncGenerator[dict[str, Any]]:
        response = await self._connect_responses_stream(
            payload,
            request_headers=request_headers,
        )
        state = ResponsesStreamState()
        event_lines: list[str] = []
        seen_finish_delta = False
        try:
            async for line in iter_stream_lines(response):
                if line:
                    event_lines.append(line)
                    continue
                for delta in iter_responses_sse_deltas_with_state(event_lines, state):
                    if delta.get("type") == "finish":
                        seen_finish_delta = True
                    yield delta
                event_lines = []
            if event_lines:
                for delta in iter_responses_sse_deltas_with_state(event_lines, state):
                    if delta.get("type") == "finish":
                        seen_finish_delta = True
                    yield delta
            if not seen_finish_delta:
                raise NetworkError("Stream ended without response completion event")
        except httpx.TimeoutException as exc:
            raise wrap_network_error(exc) from exc
        except httpx.TransportError as exc:
            raise NetworkError(f"Stream read failed: {exc}") from exc
        finally:
            await response.aclose()

    async def _connect_responses_stream(
        self,
        payload: dict[str, Any],
        *,
        request_headers: Mapping[str, str],
    ) -> httpx.Response:
        async def _build_headers() -> dict[str, str]:
            headers = await self._build_headers()
            headers.update(request_headers)
            return headers

        def _handle_error_status(
            status_code: int,
            error_body: str,
            response_headers: httpx.Headers,
        ) -> None:
            self._classify_responses_status(
                status_code,
                detail=format_http_error_detail(status_code, error_body),
                response_headers=response_headers,
            )

        return await connect_streaming_with_retry(
            self._client,
            OPENCODE_GO_RESPONSES_ENDPOINT,
            payload,
            build_headers=_build_headers,
            handle_error_status=_handle_error_status,
        )

    def _kwargs_with_model_output_limit(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Copy the caller kwargs with the model output ceiling defaulted in.

        Every wire needs it stamped here: the Chat path funnels through the
        shared ``_build_payload`` (which would apply it anyway), but the
        Messages and Responses paths bypass ``_build_payload``, so the ceiling
        has to be resolved before the request splits. The explicit-vs-ceiling
        logic lives in the base ``_apply_model_output_limit``; this adapter
        only contributes flat-namespace candidate resolution via its
        ``_model_max_output_tokens`` override.
        """

        request_kwargs = dict(kwargs)
        self._apply_model_output_limit(request_kwargs, model_id, messages)
        return request_kwargs

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


def _opencode_go_http_error_detail(response: httpx.Response) -> str:
    reason = response.text
    return f"{response.status_code} {reason}".strip() if reason else str(response.status_code)


def _model_lookup_candidates(model_id: str) -> tuple[str, ...]:
    without_connection_suffix = model_id.split("::", 1)[0]
    candidates = [model_id, without_connection_suffix]
    if "/" in without_connection_suffix:
        candidates.append(without_connection_suffix.rsplit("/", 1)[-1])
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))
