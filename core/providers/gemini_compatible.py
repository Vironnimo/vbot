"""Gemini generateContent transport shared by Provider-owned protocol routers.

Owns native request rendering, Context estimation, response/SSE normalization,
rejection learning and OAuth recovery. Composing Providers supply their client,
auth config, wire profile and error/session-header policy like Messages routers.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import aclosing
from functools import partial
from typing import Any, ClassVar, override

import httpx

from core.providers._gemini_wire import (
    _apply_gemini_response_format,
    _gemini_tool_choice,
    _move_integer,
    _move_number,
    _normalize_gemini_response,
    _normalize_gemini_stream_chunk,
    gemini_input_payload,
    gemini_returned_reasoning,
)
from core.providers._http_shared import (
    PreparedRequest,
    RequestPlan,
    connect_streaming_with_retry,
    decode_response_json,
    iter_sse_events,
    json_body_headers,
    parse_sse_json_data,
    wrap_network_error,
)
from core.providers._responses_profile import take_reasoning_renderer
from core.providers._wire_learning import (
    execute_learning_from_rejections,
    stream_learning_from_rejections,
)
from core.providers.errors import NetworkError, ProviderError
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.token_getter import OAuthRequestRecovery
from core.providers.wire_profile import Protocol
from core.utils.retry import retry_async
from core.utils.tokens import estimate_structured_tokens


class GeminiCompatibleAdapter(OpenAICompatibleAdapter):
    """Native Gemini codec using the compatible base's HTTP/request lifecycle."""

    WIRE_PROTOCOLS: ClassVar[tuple[Protocol, ...]] = ("gemini",)

    @override
    def estimate_request_input_tokens(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model_id: str,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> int:
        self._refuse_unadmitted_model(model_id)
        profile = self.wire_profile(model_id)
        payload = gemini_input_payload(messages, media_types=profile.media.types, tools=tools)
        history = estimate_structured_tokens(payload.pop("contents"), model_id=model_id)[0]
        return history + estimate_structured_tokens(payload, model_id=model_id)[0]

    @override
    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        return _normalize_gemini_response(response)

    async def _gemini_headers(self, request_headers: Mapping[str, str]) -> dict[str, str]:
        return {**await self._build_headers(), **request_headers}

    @override
    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        request_headers = self._stable_request_headers(model_id, kwargs)
        auth_recovery = self._gemini_auth_recovery()

        async def _request(request: PreparedRequest) -> dict[str, Any]:
            headers = await self._gemini_headers({**request.headers, **request_headers})
            try:
                response = await self._client.post(
                    f"/models/{model_id}:generateContent",
                    content=request.body,
                    headers=json_body_headers(headers),
                )
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc
            auth_recovery.record_response(
                response.status_code, headers, response.text if response.status_code >= 400 else ""
            )
            self._classify_http_status(
                response.status_code,
                detail=_response_detail(response),
                response_headers=response.headers,
            )
            return dict(decode_response_json(response, "Gemini provider"))

        reply = await execute_learning_from_rejections(
            lambda request: auth_recovery.run(lambda: retry_async(lambda: _request(request))),
            self._gemini_request_plan(messages, model_id, kwargs),
            wire=self.wire,
            model_id=model_id,
            provider_label=self._config.id,
        )
        if gemini_returned_reasoning(reply):
            self.wire.observe_reasoning_returned(model_id)
        return reply

    @override
    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any]]:
        request_headers = self._stable_request_headers(model_id, kwargs)
        auth_recovery = self._gemini_auth_recovery()
        async with aclosing(
            stream_learning_from_rejections(
                lambda request: self._gemini_stream_deltas(
                    request, model_id, auth_recovery, request_headers
                ),
                self._gemini_request_plan(messages, model_id, kwargs),
                wire=self.wire,
                model_id=model_id,
                provider_label=self._config.id,
            )
        ) as deltas:
            async for delta in deltas:
                yield delta

    async def _gemini_stream_deltas(
        self,
        request: PreparedRequest,
        model_id: str,
        auth_recovery: OAuthRequestRecovery,
        request_headers: Mapping[str, str],
    ) -> AsyncGenerator[dict[str, Any]]:
        def _handle_error_status(status: int, body: str, headers: httpx.Headers) -> None:
            self._classify_http_status(
                status,
                detail=f"{status} {body}".strip(),
                response_headers=headers,
            )

        attempt_headers = {**request.headers, **request_headers}
        response = await connect_streaming_with_retry(
            self._client,
            f"/models/{model_id}:streamGenerateContent?alt=sse",
            request.body,
            build_headers=lambda: self._gemini_headers(attempt_headers),
            handle_error_status=_handle_error_status,
            auth_recovery=auth_recovery,
        )
        replay_parts: list[dict[str, Any]] = []
        seen_finish = False
        has_tool_calls = False
        try:
            async for event in iter_sse_events(response):
                data = event.data
                if data is None:
                    # Every event without data is a transport comment.
                    yield {"type": "heartbeat"}
                    continue
                raw = parse_sse_json_data(data, context="Gemini provider")
                if not isinstance(raw, dict):
                    raise ProviderError(
                        "Gemini provider sent non-object JSON in stream",
                        retryable=False,
                    )
                deltas, chunk_has_tools, chunk_finished = _normalize_gemini_stream_chunk(
                    raw,
                    replay_parts,
                    has_tool_calls=has_tool_calls,
                )
                has_tool_calls = has_tool_calls or chunk_has_tools
                seen_finish = seen_finish or chunk_finished
                for delta in deltas:
                    yield delta
            if not seen_finish:
                raise NetworkError("Stream ended without a Gemini finish reason")
        except httpx.TimeoutException as exc:
            raise wrap_network_error(exc) from exc
        except httpx.TransportError as exc:
            raise NetworkError(f"Stream read failed: {exc}") from exc
        finally:
            await response.aclose()

    def _gemini_auth_recovery(self) -> OAuthRequestRecovery:
        return OAuthRequestRecovery(self._token_getter, self._auth_config)

    def _gemini_request_plan(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        kwargs: Mapping[str, Any],
    ) -> RequestPlan:
        return self._request_plan(
            messages, model_id, partial(self._build_gemini_payload, messages, model_id, kwargs)
        )

    def _build_gemini_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        kwargs: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Render one Gemini ``generateContent`` request from the wire profile.

        Raises:
            ProviderError: (not retryable, nothing sent) when the profile does
                not admit the Model, a media type or parameter is not carried,
                or the request holds too many images.
        """

        self._refuse_unadmitted_model(model_id)
        profile = self.wire_profile(model_id)
        request = {key: value for key, value in kwargs.items() if value is not None}
        reasoning_renderer = take_reasoning_renderer(profile, request)
        if profile.request.parameters:
            # Configured or learned parameter rules shape the request
            # parameters before they move into ``generationConfig``.
            thinking: dict[str, Any] = {}
            reasoning_renderer(thinking)
            profile.request.shape_parameters(
                request, reasoning_active=bool(thinking), provider_label=self._config.name
            )
        self._apply_model_output_limit(request, model_id, messages)
        if model_ceiling := self._model_max_output_tokens(model_id):
            for output_key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
                value = request.get(output_key)
                if isinstance(value, int) and not isinstance(value, bool):
                    request[output_key] = min(value, model_ceiling)
        payload = gemini_input_payload(
            messages,
            media_types=profile.media.types,
            tools=request.pop("tools", None),
        )

        generation: dict[str, Any] = {}
        output_limits = [
            request.pop(key)
            for key in ("max_tokens", "max_completion_tokens", "max_output_tokens")
            if isinstance(request.get(key), int) and not isinstance(request.get(key), bool)
        ]
        if output_limits:
            generation["maxOutputTokens"] = min(output_limits)
        _move_number(request, generation, "temperature", "temperature", minimum=0, maximum=2)
        _move_number(request, generation, "top_p", "topP", minimum=0, maximum=1)
        _move_integer(request, generation, "top_k", "topK", minimum=1)
        _move_integer(request, generation, "seed", "seed")
        _move_number(
            request, generation, "presence_penalty", "presencePenalty", minimum=-2, maximum=2
        )
        _move_number(
            request, generation, "frequency_penalty", "frequencyPenalty", minimum=-2, maximum=2
        )
        stop = request.pop("stop", None)
        if isinstance(stop, str):
            generation["stopSequences"] = [stop]
        elif isinstance(stop, list) and all(isinstance(item, str) for item in stop):
            generation["stopSequences"] = stop
        elif stop is not None:
            raise ProviderError("Gemini stop must be a string or list of strings", retryable=False)

        response_format = request.pop("response_format", None)
        if response_format is not None:
            _apply_gemini_response_format(generation, response_format)

        tool_choice = request.pop("tool_choice", None)
        if tool_choice is not None:
            payload["toolConfig"] = {"functionCallingConfig": _gemini_tool_choice(tool_choice)}
        request.pop("parallel_tool_calls", None)
        if generation:
            payload["generationConfig"] = generation
        reasoning_renderer(payload)
        if request:
            unsupported = ", ".join(sorted(request))
            raise ProviderError(
                f"Gemini does not support request parameters: {unsupported}",
                retryable=False,
            )
        # Body rules name top-level request keys, so a ``generationConfig``
        # entry replaces or fills the whole object.
        profile.request.apply_body(payload)
        return payload


def _response_detail(response: httpx.Response) -> str:
    return f"{response.status_code} {response.text}".strip()
