"""GitHub Copilot provider adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers._http_shared import classify_http_status, wrap_network_error
from core.providers.errors import NetworkError, ProviderError
from core.providers.github_copilot_messages import (
    CopilotMessagesStreamState,
    build_copilot_messages_payload,
    normalize_copilot_messages_response,
    normalize_copilot_messages_sse_line,
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
    DEFAULT_MAX_OUTPUT_TOKENS,
    OpenAICompatibleAdapter,
    _read_mapping,
    _read_non_empty_string,
    _read_optional_mapping,
    _read_string,
)
from core.utils.retry import retry_async

OPENAI_REASONING_COPILOT_MODEL_POLICY = copilot_model_policy("gpt-5-mini")


class GitHubCopilotAdapter(OpenAICompatibleAdapter):
    """Routing adapter for GitHub Copilot endpoint families."""

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
            except httpx.TimeoutException as exc:
                raise wrap_network_error(exc) from exc
            except httpx.ConnectError as exc:
                raise wrap_network_error(exc) from exc

            classify_http_status(response.status_code, detail=_http_error_detail(response))
            return dict(response.json())

        return await retry_async(_do_request)

    async def _connect_stream(
        self,
        endpoint_path: str,
        payload: dict[str, Any],
    ) -> httpx.Response:
        headers = await self._build_headers()

        async def _connect() -> httpx.Response:
            request = self._client.build_request(
                "POST",
                endpoint_path,
                json=payload,
                headers=headers,
            )
            try:
                response = await self._client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                raise wrap_network_error(exc) from exc
            except httpx.ConnectError as exc:
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
        try:
            async for line in response.aiter_lines():
                if line:
                    event_lines.append(line)
                    continue
                for delta in iter_responses_sse_deltas_with_state(event_lines, state):
                    yield delta
                event_lines = []
            if event_lines:
                for delta in iter_responses_sse_deltas_with_state(event_lines, state):
                    yield delta
        except httpx.ReadError as exc:
            raise NetworkError(f"Stream read failed: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise wrap_network_error(exc) from exc
        finally:
            await response.aclose()

    async def _stream_messages(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        response = await self._connect_stream(MESSAGES_ENDPOINT, payload)
        state = CopilotMessagesStreamState()
        try:
            async for line in response.aiter_lines():
                for delta in normalize_copilot_messages_sse_line(line, state):
                    yield delta
        except httpx.ReadError as exc:
            raise NetworkError(f"Stream read failed: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise wrap_network_error(exc) from exc
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

        return Model(
            model_id=_read_non_empty_string(raw, "id"),
            name=_read_string(raw, "name"),
            capabilities=Capabilities(
                vision=supports.get("vision") is True,
                tools=supports.get("tool_calls") is True,
                json_mode=supports.get("structured_outputs") is True,
                reasoning=ReasoningCapabilities(supported=_copilot_supports_reasoning(supports)),
            ),
            context_window=_read_optional_token_limit(
                limits,
                "max_context_window_tokens",
                DEFAULT_CONTEXT_WINDOW,
            ),
            max_output_tokens=_read_optional_token_limit(
                limits,
                "max_output_tokens",
                _provider_default_max_tokens(defaults),
            ),
            metadata=_copilot_runtime_metadata(raw, capabilities, supports),
        )


def _copilot_supports_reasoning(supports: Mapping[str, Any]) -> bool:
    reasoning_effort = supports.get("reasoning_effort")
    if isinstance(reasoning_effort, list) and reasoning_effort:
        return True
    return "min_thinking_budget" in supports or "max_thinking_budget" in supports


def _copilot_model_policy(model_id: str) -> GitHubCopilotModelPolicy:
    return copilot_model_policy(model_id)


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
    fallback: int,
) -> int:
    value = data.get(key)
    if isinstance(value, bool):
        return fallback
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return fallback


def _provider_default_max_tokens(defaults: Mapping[str, Any] | None) -> int:
    if defaults is None:
        return DEFAULT_MAX_OUTPUT_TOKENS
    max_tokens = defaults.get("max_tokens")
    if isinstance(max_tokens, bool):
        return DEFAULT_MAX_OUTPUT_TOKENS
    if isinstance(max_tokens, int):
        return max_tokens
    if isinstance(max_tokens, str) and max_tokens.isdecimal():
        return int(max_tokens)
    return DEFAULT_MAX_OUTPUT_TOKENS


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
