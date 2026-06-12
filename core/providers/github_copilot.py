"""GitHub Copilot provider adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers._http_shared import (
    classify_http_status,
    decode_response_json,
    iter_sse_data,
    parse_sse_json_data,
    wrap_network_error,
)
from core.providers.errors import NetworkError, ProviderError
from core.providers.github_copilot_messages import (
    CopilotMessagesStreamState,
    build_copilot_messages_payload,
    normalize_copilot_messages_response,
    normalize_copilot_messages_stream_event,
)
from core.providers.github_copilot_policy import (
    CHAT_COMPLETIONS_ENDPOINT,
    COPILOT_METADATA_KEY,
    MESSAGES_ENDPOINT,
    RESPONSES_ENDPOINT,
    GitHubCopilotModelPolicy,
    copilot_model_policy,
)
from core.providers.github_copilot_responses import (
    ResponsesStreamState,
    build_responses_payload,
    iter_responses_sse_deltas_with_state,
    normalize_responses_response,
)
from core.providers.openai_compatible import (
    DEFAULT_CONTEXT_WINDOW,
    OpenAICompatibleAdapter,
    _read_mapping,
    _read_non_empty_string,
    _read_optional_mapping,
    _read_string,
)
from core.providers.reasoning import (
    REASONING_REPLAY_CURRENT_RUN,
    REASONING_REPLAY_FULL_HISTORY,
    ReasoningReplayPolicy,
)
from core.utils.retry import retry_async


class GitHubCopilotAdapter(OpenAICompatibleAdapter):
    """Routing adapter for GitHub Copilot endpoint families."""

    def reasoning_replay_policy(self, model_id: str) -> ReasoningReplayPolicy:
        """Replay persisted reasoning across runs on the verified endpoint families.

        ``/responses`` (reasoning items incl. ``encrypted_content``) and
        ``/v1/messages`` (signed thinking blocks) both accept cross-run replay —
        verified against the real Copilot endpoints (2026-06-13). The
        ``/chat/completions`` fallback wire stays on ``current_run``: replaying
        ``reasoning_meta`` fields there is unverified, and the conservative
        endpoint family omits uncertain request shapes by convention.
        """
        policy = self._policy_for_model(model_id)
        if policy.endpoint_path in {RESPONSES_ENDPOINT, MESSAGES_ENDPOINT}:
            return REASONING_REPLAY_FULL_HISTORY
        return REASONING_REPLAY_CURRENT_RUN

    # ------------------------------------------------------------------
    # Payload / request helpers
    # ------------------------------------------------------------------

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        policy = self._policy_for_model(model_id)
        return super()._build_payload(
            messages,
            model_id,
            **self._chat_request_kwargs(policy, kwargs),
        )

    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Send one Copilot request through the model-selected endpoint."""

        policy = self._policy_for_model(model_id)
        if policy.endpoint_path == CHAT_COMPLETIONS_ENDPOINT:
            return await super().send(
                messages,
                model_id=model_id,
                **self._chat_request_kwargs(policy, kwargs),
            )
        if policy.endpoint_path == RESPONSES_ENDPOINT:
            payload = build_responses_payload(
                messages,
                model_id=model_id,
                policy=policy,
                **self._request_kwargs_with_defaults(kwargs),
            )
            return await self._post_json(RESPONSES_ENDPOINT, payload)
        if policy.endpoint_path == MESSAGES_ENDPOINT:
            payload = build_copilot_messages_payload(
                messages,
                model_id=model_id,
                policy=policy,
                **self._request_kwargs_with_defaults(kwargs),
            )
            return await self._post_json(MESSAGES_ENDPOINT, payload)
        return await super().send(
            messages,
            model_id=model_id,
            **self._chat_request_kwargs(policy, kwargs),
        )

    async def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream one Copilot request as normalized vBot deltas."""

        policy = self._policy_for_model(model_id)
        if policy.endpoint_path == CHAT_COMPLETIONS_ENDPOINT:
            emitted_visible_reasoning = ""
            async for delta in super().stream(
                messages,
                model_id=model_id,
                **self._chat_request_kwargs(policy, kwargs),
            ):
                normalized_deltas, emitted_visible_reasoning = _normalize_copilot_chat_stream_delta(
                    delta,
                    emitted_visible_reasoning,
                )
                for normalized_delta in normalized_deltas:
                    yield normalized_delta
            return
        if policy.endpoint_path == RESPONSES_ENDPOINT:
            payload = build_responses_payload(
                messages,
                model_id=model_id,
                policy=policy,
                stream=True,
                **self._request_kwargs_with_defaults(kwargs),
            )
            async for delta in self._stream_responses(payload):
                yield delta
            return
        if policy.endpoint_path == MESSAGES_ENDPOINT:
            payload = build_copilot_messages_payload(
                messages,
                model_id=model_id,
                policy=policy,
                **self._request_kwargs_with_defaults(kwargs),
            )
            payload["stream"] = True
            async for delta in self._stream_messages(payload):
                yield delta
            return
        async for delta in super().stream(
            messages,
            model_id=model_id,
            **self._chat_request_kwargs(policy, kwargs),
        ):
            yield delta

    def normalize_response(self, response: dict[str, Any]) -> dict[str, Any]:
        """Normalize any supported Copilot endpoint response."""

        if isinstance(response.get("output"), list):
            return normalize_responses_response(response)
        if isinstance(response.get("content"), list):
            return normalize_copilot_messages_response(response)
        return _normalize_copilot_chat_response(super().normalize_response(response))

    def _policy_for_model(self, model_id: str) -> GitHubCopilotModelPolicy:
        metadata = None
        if self._model_lookup is not None:
            model = self._model_lookup(model_id)
            if model is not None:
                metadata = model.metadata
        return copilot_model_policy(model_id, metadata)

    def _request_kwargs_with_defaults(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        request_kwargs: dict[str, Any] = {}
        if self._config.defaults:
            request_kwargs.update(self._config.defaults)
        request_kwargs.update(kwargs)
        return request_kwargs

    def _chat_request_kwargs(
        self,
        policy: GitHubCopilotModelPolicy,
        kwargs: Mapping[str, Any],
    ) -> dict[str, Any]:
        request_kwargs = policy.filter_request_kwargs(kwargs)
        for endpoint_specific_key in ("thinking", "thinking_budget", "output_config"):
            request_kwargs.pop(endpoint_specific_key, None)
        return request_kwargs

    async def _post_json(self, endpoint_path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async def _do_request() -> dict[str, Any]:
            headers = await self._build_headers()
            try:
                response = await self._client.post(endpoint_path, json=payload, headers=headers)
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc

            classify_http_status(response.status_code, detail=_http_error_detail(response))
            return dict(decode_response_json(response, "GitHub Copilot provider"))

        return await retry_async(_do_request)

    async def _connect_stream(
        self,
        endpoint_path: str,
        payload: dict[str, Any],
    ) -> httpx.Response:
        async def _connect() -> httpx.Response:
            # Rebuild headers per attempt: the Copilot session token may refresh
            # during a retry backoff, and the getter must be re-consulted each time.
            headers = await self._build_headers()
            request = self._client.build_request(
                "POST",
                endpoint_path,
                json=payload,
                headers=headers,
            )
            try:
                response = await self._client.send(request, stream=True)
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc

            if response.status_code >= 400:
                error_body = (await response.aread()).decode("utf-8", errors="replace")
                await response.aclose()
                detail = _http_error_detail(response, error_body)
                classify_http_status(response.status_code, detail=detail)
                raise ProviderError(f"Provider error: {response.status_code}", retryable=False)

            return response

        return await retry_async(_connect)

    async def _stream_responses(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        response = await self._connect_stream(RESPONSES_ENDPOINT, payload)
        state = ResponsesStreamState()
        event_lines: list[str] = []
        seen_finish_delta = False
        try:
            async for line in response.aiter_lines():
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

    async def _stream_messages(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        response = await self._connect_stream(MESSAGES_ENDPOINT, payload)
        state = CopilotMessagesStreamState()
        seen_finish_delta = False
        try:
            async for data in iter_sse_data(response):
                if not data.strip():
                    continue
                parsed = parse_sse_json_data(data, context="GitHub Copilot Messages provider")
                if not isinstance(parsed, dict):
                    raise ProviderError(
                        "GitHub Copilot Messages provider sent non-object JSON in stream",
                        retryable=False,
                    )
                for delta in normalize_copilot_messages_stream_event(parsed, state):
                    if delta.get("type") == "finish":
                        seen_finish_delta = True
                    yield delta
            if not seen_finish_delta:
                raise NetworkError("Stream ended without message stop reason")
        except httpx.TimeoutException as exc:
            raise wrap_network_error(exc) from exc
        except httpx.TransportError as exc:
            raise NetworkError(f"Stream read failed: {exc}") from exc
        finally:
            await response.aclose()

    @classmethod
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize one captured GitHub Copilot ``/models`` entry."""

        capabilities = _read_mapping(raw, "capabilities")
        limits = _read_optional_mapping(capabilities, "limits")
        supports = _read_optional_mapping(capabilities, "supports")
        vision_supported = supports.get("vision") is True
        tools_supported = supports.get("tool_calls") is True
        json_mode_supported = supports.get("structured_outputs") is True
        reasoning_supported = _copilot_supports_reasoning(supports)

        return Model(
            model_id=_read_non_empty_string(raw, "id"),
            name=_read_string(raw, "name"),
            capabilities=Capabilities(
                vision=vision_supported,
                tools=tools_supported,
                json_mode=json_mode_supported,
                reasoning=ReasoningCapabilities(supported=reasoning_supported),
                input_modalities=("text", "image") if vision_supported else ("text",),
                output_modalities=("text",),
                supported_parameters=tuple(
                    _copilot_supported_parameters(
                        supports,
                        tools_supported,
                        json_mode_supported,
                        reasoning_supported,
                    )
                ),
            ),
            context_window=_read_token_limit_or_default(
                limits,
                "max_context_window_tokens",
                DEFAULT_CONTEXT_WINDOW,
            ),
            max_output_tokens=_read_optional_token_limit(limits, "max_output_tokens"),
            metadata=_copilot_runtime_metadata(raw, capabilities, supports),
        )


def _copilot_supports_reasoning(supports: Mapping[str, Any]) -> bool:
    reasoning_effort = supports.get("reasoning_effort")
    if isinstance(reasoning_effort, list) and reasoning_effort:
        return True
    return "min_thinking_budget" in supports or "max_thinking_budget" in supports


def _copilot_supported_parameters(
    supports: Mapping[str, Any],
    tools_supported: bool,
    json_mode_supported: bool,
    reasoning_supported: bool,
) -> list[str]:
    supported_parameters: list[str] = []
    if tools_supported:
        supported_parameters.append("tool_calls")
    if json_mode_supported:
        supported_parameters.append("structured_outputs")
    if reasoning_supported:
        supported_parameters.append("reasoning_effort")
    if supports.get("parallel_tool_calls") is True:
        supported_parameters.append("parallel_tool_calls")
    return supported_parameters


def _http_error_detail(response: httpx.Response, body: str | None = None) -> str:
    reason = response.text if body is None else body
    return f"{response.status_code} {reason}".strip() if reason else str(response.status_code)


def _copilot_runtime_metadata(
    raw: Mapping[str, Any],
    capabilities: Mapping[str, Any],
    supports: Mapping[str, Any],
) -> Mapping[str, Any]:
    metadata: dict[str, Any] = {}
    for source_key in ("vendor", "version"):
        value = raw.get(source_key)
        if isinstance(value, str) and value:
            metadata[source_key] = value
    family = capabilities.get("family")
    if isinstance(family, str) and family:
        metadata["family"] = family

    supported_endpoints = raw.get("supported_endpoints")
    if isinstance(supported_endpoints, list):
        endpoints = [endpoint for endpoint in supported_endpoints if isinstance(endpoint, str)]
        if endpoints:
            metadata["supported_endpoints"] = endpoints

    reasoning_effort = supports.get("reasoning_effort")
    if isinstance(reasoning_effort, list):
        efforts = [effort for effort in reasoning_effort if isinstance(effort, str)]
        if efforts:
            metadata["reasoning_efforts"] = efforts

    for support_key in (
        "min_thinking_budget",
        "max_thinking_budget",
        "adaptive_thinking",
        "parallel_tool_calls",
        "streaming",
        "structured_outputs",
        "tool_calls",
    ):
        value = supports.get(support_key)
        if isinstance(value, bool | int) and not (
            isinstance(value, bool) and support_key.endswith("budget")
        ):
            metadata[support_key] = value

    return {COPILOT_METADATA_KEY: metadata} if metadata else {}


def _read_optional_token_limit(
    data: Mapping[str, Any],
    key: str,
) -> int | None:
    value = data.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _read_token_limit_or_default(
    data: Mapping[str, Any],
    key: str,
    fallback: int,
) -> int:
    value = _read_optional_token_limit(data, key)
    return value if value is not None else fallback


def _normalize_copilot_chat_response(response: dict[str, Any]) -> dict[str, Any]:
    reasoning = response.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        return response
    visible_reasoning = _copilot_visible_reasoning_from_meta(response.get("reasoning_meta"))
    if visible_reasoning is None:
        return response
    normalized_response = dict(response)
    normalized_response["reasoning"] = visible_reasoning
    return normalized_response


def _normalize_copilot_chat_stream_delta(
    delta: Mapping[str, Any],
    emitted_visible_reasoning: str,
) -> tuple[list[dict[str, Any]], str]:
    delta_type = delta.get("type")
    if delta_type == "reasoning_delta":
        text = delta.get("text")
        if isinstance(text, str) and text:
            emitted_visible_reasoning += text
        return [dict(delta)], emitted_visible_reasoning
    if delta_type != "reasoning_meta":
        return [dict(delta)], emitted_visible_reasoning

    visible_reasoning = _copilot_visible_reasoning_from_meta(delta.get("reasoning_meta"))
    if visible_reasoning is None:
        return [dict(delta)], emitted_visible_reasoning

    reasoning_backfill, updated_visible_reasoning = _copilot_reasoning_backfill_delta(
        visible_reasoning,
        emitted_visible_reasoning,
    )
    normalized_deltas: list[dict[str, Any]] = []
    if reasoning_backfill is not None:
        normalized_deltas.append({"type": "reasoning_delta", "text": reasoning_backfill})
    normalized_deltas.append(dict(delta))
    return normalized_deltas, updated_visible_reasoning


def _copilot_visible_reasoning_from_meta(reasoning_meta: Any) -> str | None:
    if not isinstance(reasoning_meta, Mapping):
        return None
    reasoning_details = reasoning_meta.get("reasoning_details")
    if not isinstance(reasoning_details, list):
        return None

    parts: list[str] = []
    for item in reasoning_details:
        if not isinstance(item, Mapping):
            continue
        detail_type = item.get("type")
        text = item.get("text")
        if not isinstance(text, str) or not text:
            continue
        if isinstance(detail_type, str) and detail_type.startswith("reasoning"):
            parts.append(text)
    return "".join(parts) or None


def _copilot_reasoning_backfill_delta(
    reasoning: str,
    emitted_visible_reasoning: str,
) -> tuple[str | None, str]:
    if not emitted_visible_reasoning:
        return reasoning, reasoning
    if reasoning == emitted_visible_reasoning or emitted_visible_reasoning.endswith(reasoning):
        return None, emitted_visible_reasoning
    if reasoning.startswith(emitted_visible_reasoning):
        return reasoning[len(emitted_visible_reasoning) :] or None, reasoning

    overlap = _copilot_suffix_prefix_overlap(emitted_visible_reasoning, reasoning)
    if overlap > 0:
        backfill = reasoning[overlap:]
        return backfill or None, f"{emitted_visible_reasoning}{backfill}"
    return None, emitted_visible_reasoning


def _copilot_suffix_prefix_overlap(left: str, right: str) -> int:
    max_overlap = min(len(left), len(right))
    for overlap in range(max_overlap, 0, -1):
        if left.endswith(right[:overlap]):
            return overlap
    return 0
