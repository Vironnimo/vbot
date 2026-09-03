"""OpenCode Go provider adapter."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

from core.providers._http_shared import (
    build_streaming_request,
    classify_http_status,
    decode_response_json,
    wrap_network_error,
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
)
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.reasoning import (
    REASONING_REPLAY_FIDELITY_READABLE_ONLY,
    ReasoningReplayFidelity,
    closest_supported_effort,
    model_reasoning_levels,
    normalize_thinking_effort,
)
from core.providers.token_getter import TokenGetter
from core.utils.logging import get_logger
from core.utils.retry import retry_async

_LOGGER = get_logger("providers.opencode_go")

# Provider-scoped metadata blob + field carrying the per-model wire protocol
# (Phase 5). The published opencode-go protocol table is a per-model FACT, so it
# lives in data (the opencode-go override's ``metadata.opencode_go.protocol``),
# not in a hardcoded adapter set. The adapter only owns the MECHANICS — how to
# build an Anthropic ``/messages``, an OpenAI ``/chat/completions``, or an
# OpenAI ``/responses`` request.
OPENCODE_GO_METADATA_KEY = "opencode_go"
PROTOCOL_METADATA_KEY = "protocol"
PROTOCOL_ANTHROPIC = "anthropic"
PROTOCOL_OPENAI = "openai"
PROTOCOL_RESPONSES = "responses"
OPENCODE_GO_RESPONSES_ENDPOINT = "/responses"
OPENCODE_SESSION_HEADER = "x-opencode-session"
OPENCODE_SESSION_ID_KWARG = "_opencode_session_id"
THINKING_KEEP_METADATA_KEY = "thinking_keep"
THINKING_KEEP_ALL = "all"
THINKING_CONTROL_METADATA_KEY = "thinking_control"
THINKING_CONTROL_TOGGLE = "toggle"
THINKING_CONTROL_ALWAYS_ENABLED = "always_enabled"
MINIMUM_REASONING_EFFORT_METADATA_KEY = "minimum_reasoning_effort"
# The endpoint returns bare ids with no protocol, so a model the override does
# not mark is unknown: route it the SAFE default (OpenAI chat/completions) and
# warn, so a newly added model is never silently misrouted onto the wrong wire.
_DEFAULT_PROTOCOL = PROTOCOL_OPENAI

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

# ``_model_protocol`` runs on every send/stream, so an unmarked model would re-log
# its routing warning on each request and flood the log. Track which model ids have
# already warned in this process and emit each once — "once per server runtime"
# (a restart re-warns). Mirrors the skill-validation dedup in core/skills/skills.py.
_warned_unmarked_models: set[str] = set()

# The Responses wire documents a minimal..max effort ladder and rejects an
# explicit ``"none"`` rung with HTTP 400 (live-verified 2026-08-25); omitting
# the whole reasoning object is accepted instead. Used as the default ladder
# when the Model DB carries no per-model levels.
_OPENCODE_GO_RESPONSES_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh"})


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


def _opencode_request_headers(request_kwargs: dict[str, Any]) -> dict[str, str]:
    session_id = request_kwargs.pop(OPENCODE_SESSION_ID_KWARG, None)
    if not isinstance(session_id, str) or not session_id:
        return {}
    return {OPENCODE_SESSION_HEADER: session_id}


@dataclass(frozen=True)
class OpenCodeGoResponsesPolicy:
    """Request-shaping facts for one OpenCode Go Responses-routed Model.

    Implements the shared ``ResponsesRequestPolicy`` surface so
    ``build_responses_payload`` can shape the stateless ``/responses``
    request from Provider-neutral data.
    """

    allowed_reasoning_efforts: frozenset[str]
    minimum_reasoning_effort: str | None = None
    supports_tools: bool = True
    supports_parallel_tool_calls: bool = True
    supports_structured_outputs: bool = True

    @property
    def allows_any_reasoning_controls(self) -> bool:
        return bool(self.allowed_reasoning_efforts)

    @property
    def supports_explicit_none_effort(self) -> bool:
        return "none" in self.allowed_reasoning_efforts

    def filter_request_kwargs(self, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        filtered = {key: value for key, value in kwargs.items() if value is not None}
        if not self.allows_any_reasoning_controls:
            for name in ("thinking_effort", "reasoning_effort", "reasoning", "include_reasoning"):
                filtered.pop(name, None)
        else:
            self._normalize_effort(filtered, "thinking_effort")
            self._normalize_effort(filtered, "reasoning_effort")
        return filtered

    def closest_reasoning_effort(self, effort: Any) -> str | None:
        normalized = normalize_thinking_effort(effort)
        if not normalized:
            return None
        if normalized == "none":
            if self.supports_explicit_none_effort:
                return "none"
            # Models without an off rung fall back to their cheapest
            # documented rung when one is known; otherwise omission lets the
            # Provider apply its fixed/default reasoning behavior.
            return self.minimum_reasoning_effort
        return closest_supported_effort(normalized, self.allowed_reasoning_efforts)

    def supports_request_parameter(self, parameter_name: str) -> bool:
        # Conservative like Copilot's Responses route: temperature is omitted
        # because GPT-5-family reasoning models reject it; no gateway evidence
        # exists for prompt_cache_key or service_tier on this endpoint.
        return parameter_name in ("max_output_tokens", "top_p")

    def _normalize_effort(
        self,
        filtered: dict[str, Any],
        parameter_name: str,
    ) -> None:
        if parameter_name not in filtered:
            return
        safe_effort = self.closest_reasoning_effort(filtered[parameter_name])
        if safe_effort is None:
            filtered.pop(parameter_name, None)
        else:
            filtered[parameter_name] = safe_effort


class _OpenCodeGoMessagesAdapter(AnthropicCompatibleAdapter):
    """OpenCode Go's Anthropic Messages wire adapter."""

    def _request_headers_from_kwargs(
        self,
        request_kwargs: dict[str, Any],
    ) -> dict[str, str]:
        return _opencode_request_headers(request_kwargs)

    def _classify_http_status(
        self,
        status_code: int,
        *,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
        _raise_if_permanent_rate_limit(status_code, detail)
        super()._classify_http_status(
            status_code,
            detail=detail,
            response_headers=response_headers,
        )


class OpenCodeGoAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible adapter for the OpenCode Go gateway.

    Chat Models normalize readable Reasoning from their profiled response
    carrier and replay it as ``reasoning_content``. The two field names are
    intentionally independent: Kimi K3 and Hy currently respond with
    ``reasoning`` plus ``reasoning_details``, while their compatible Assistant
    history uses ``reasoning_content``.

    Replay scope follows the shared System → Provider → Model hierarchy. Wire
    controls remain explicit per-Model facts because some gateway backends need
    a different request representation than the field they return.
    """

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
        # The inner adapter shares the same recorder, so the single context
        # set via set_debug_context() is seen by whichever client handles the
        # request (OpenAI chat/completions or the Anthropic messages path).
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
            prompt_caching=True,
            extra_retryable_statuses=frozenset({ANTHROPIC_OVERLOADED_STATUS}),
        )

    async def aclose(self) -> None:
        await self._messages.aclose()
        await super().aclose()

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

    def _request_headers_from_kwargs(
        self,
        request_kwargs: dict[str, Any],
    ) -> dict[str, str]:
        return _opencode_request_headers(request_kwargs)

    def reasoning_replay_fidelity(self, model_id: str) -> ReasoningReplayFidelity:
        """Declare the reasoning class accepted by the selected wire.

        OpenCode's own OpenAI-compatible client serializes historical
        Reasoning as readable ``reasoning_content`` and does not replay
        ``reasoning_details``. The Messages and Responses routes already own
        their native block/item fidelity and bypass this Chat serializer.
        """

        if self._model_protocol(model_id) == PROTOCOL_OPENAI:
            return REASONING_REPLAY_FIDELITY_READABLE_ONLY
        return super().reasoning_replay_fidelity(model_id)

    def wire_media_support(self, model_id: str) -> frozenset[str]:
        """Resolve media support from the wire selected for this model."""

        if self._uses_anthropic_messages_path(model_id):
            return self._messages.wire_media_support(model_id)
        return super().wire_media_support(model_id)

    def estimate_request_input_tokens(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        model_id: str,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> int:
        """Estimate the rendered request for the model-selected OpenCode Go wire."""

        if self._model_protocol(model_id) == PROTOCOL_RESPONSES:
            return estimate_responses_input_tokens(
                [dict(message) for message in messages],
                tools=tools,
            )
        return super().estimate_request_input_tokens(
            messages,
            model_id=model_id,
            tools=tools,
        )

    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        request_kwargs = self._kwargs_with_model_output_limit(model_id, messages, kwargs)
        protocol = self._model_protocol(model_id)
        if protocol == PROTOCOL_ANTHROPIC:
            return await self._messages.send(
                messages,
                model_id=model_id,
                **request_kwargs,
            )
        if protocol == PROTOCOL_RESPONSES:
            request_headers = self._request_headers_from_kwargs(request_kwargs)
            payload = self._build_responses_payload(
                messages,
                model_id=model_id,
                **self._request_kwargs_with_defaults(request_kwargs),
            )
            return await self._post_responses_json(payload, request_headers=request_headers)
        return await super().send(messages, model_id=model_id, **request_kwargs)

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        request_kwargs = self._kwargs_with_model_output_limit(model_id, messages, kwargs)
        protocol = self._model_protocol(model_id)
        if protocol == PROTOCOL_ANTHROPIC:
            return self._messages.stream(
                messages,
                model_id=model_id,
                **request_kwargs,
            )
        if protocol == PROTOCOL_RESPONSES:
            request_headers = self._request_headers_from_kwargs(request_kwargs)
            payload = self._build_responses_payload(
                messages,
                model_id=model_id,
                stream=True,
                **self._request_kwargs_with_defaults(request_kwargs),
            )
            return self._stream_responses(payload, request_headers=request_headers)
        return super().stream(messages, model_id=model_id, **request_kwargs)

    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        if model_id is not None:
            protocol = self._model_protocol(model_id)
            if protocol == PROTOCOL_ANTHROPIC:
                return self._messages.normalize_response(response, model_id=model_id)
            if protocol == PROTOCOL_RESPONSES:
                return normalize_responses_response(response)
            return super().normalize_response(response, model_id=model_id)
        if "choices" in response:
            return super().normalize_response(response, model_id=model_id)
        if isinstance(response.get("output"), list):
            return normalize_responses_response(response)
        return self._messages.normalize_response(response, model_id=model_id)

    def _classify_http_status(
        self,
        status_code: int,
        *,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
        _raise_if_permanent_rate_limit(status_code, detail)
        super()._classify_http_status(
            status_code,
            detail=detail,
            response_headers=response_headers,
        )

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        selected_effort = normalize_thinking_effort(
            kwargs.get("thinking_effort") or kwargs.get("reasoning_effort")
        )
        payload = super()._build_payload(messages, model_id, **kwargs)
        thinking_control = self._profile_value(model_id, THINKING_CONTROL_METADATA_KEY)
        if thinking_control in (THINKING_CONTROL_TOGGLE, THINKING_CONTROL_ALWAYS_ENABLED):
            # Kimi K2.5/K2.6/K2.7 use the ``thinking`` object, not the generic
            # OpenAI-compatible ``reasoning_effort`` field. K2.7 is always-on;
            # K2.5/K2.6 expose only a binary toggle.
            payload.pop("reasoning_effort", None)
            thinking_enabled = (
                thinking_control == THINKING_CONTROL_ALWAYS_ENABLED or selected_effort != "none"
            )
            thinking: dict[str, str] = {"type": "enabled" if thinking_enabled else "disabled"}
            if thinking_enabled and self._thinking_keep(model_id) == THINKING_KEEP_ALL:
                thinking["keep"] = THINKING_KEEP_ALL
            payload["thinking"] = thinking
        elif selected_effort == "none" and (
            minimum_effort := self._profile_value(model_id, MINIMUM_REASONING_EFFORT_METADATA_KEY)
        ) in {"minimal", "low", "medium", "high", "xhigh", "max"}:
            # Always-reasoning effort models cannot honor ``none``. Explicitly
            # request their cheapest supported rung instead of omitting the
            # field and accidentally falling back to a much higher default.
            payload["reasoning_effort"] = minimum_effort
        return payload

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
        adapter uses. Body-level session routing kwargs are dropped because
        OpenCode Go receives its cache affinity through a request header.
        """

        kwargs.pop("session_id", None)
        payload = build_responses_payload(
            messages,
            model_id=model_id,
            policy=self._responses_policy_for_model(model_id),
            stream=stream,
            **kwargs,
        )
        payload["store"] = False
        return payload

    def _responses_policy_for_model(self, model_id: str) -> OpenCodeGoResponsesPolicy:
        model = (
            self._model_lookup(_bare_model_id(model_id)) if self._model_lookup is not None else None
        )
        capabilities = model.capabilities if model is not None else None
        reasoning_supported = capabilities.reasoning.supported if capabilities is not None else True
        levels = frozenset(model_reasoning_levels(self._model_lookup, model_id) or ())
        allowed_efforts = (
            levels
            if reasoning_supported and levels
            else (_OPENCODE_GO_RESPONSES_EFFORTS if reasoning_supported else frozenset())
        )
        minimum_effort = self._profile_value(model_id, MINIMUM_REASONING_EFFORT_METADATA_KEY)
        return OpenCodeGoResponsesPolicy(
            allowed_reasoning_efforts=allowed_efforts,
            minimum_reasoning_effort=(minimum_effort if isinstance(minimum_effort, str) else None),
        )

    def _classify_responses_status(
        self,
        status_code: int,
        *,
        detail: str,
        response_headers: httpx.Headers,
    ) -> None:
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
    ) -> AsyncIterator[dict[str, Any]]:
        response = await self._connect_responses_stream(
            payload,
            request_headers=request_headers,
        )
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

    async def _connect_responses_stream(
        self,
        payload: dict[str, Any],
        *,
        request_headers: Mapping[str, str],
    ) -> httpx.Response:
        async def _connect() -> httpx.Response:
            headers = await self._build_headers()
            headers.update(request_headers)
            request = build_streaming_request(
                self._client,
                "POST",
                OPENCODE_GO_RESPONSES_ENDPOINT,
                json=payload,
                headers=headers,
            )
            try:
                response = await self._client.send(request, stream=True)
            except httpx.TransportError as exc:
                raise wrap_network_error(exc) from exc
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                await response.aclose()
                self._classify_responses_status(
                    response.status_code,
                    detail=_opencode_go_http_error_detail(response, body),
                    response_headers=response.headers,
                )
                raise ProviderError(
                    f"Provider error: {response.status_code}",
                    retryable=False,
                )
            return response

        return await retry_async(_connect)

    def _kwargs_with_model_output_limit(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        """Copy the caller kwargs with the model output ceiling defaulted in.

        Both wire routes need it stamped here: the OpenAI path funnels through
        the shared ``_build_payload`` (which would apply it anyway), but the
        Anthropic messages path bypasses ``_build_payload``, so the ceiling has
        to be resolved before the request splits. The explicit-vs-ceiling logic
        lives in the base ``_apply_model_output_limit``; this adapter only
        contributes flat-namespace candidate resolution via its
        ``_model_max_output_tokens`` override.
        """

        request_kwargs = dict(kwargs)
        self._apply_model_output_limit(request_kwargs, model_id, messages)
        return request_kwargs

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

    def _model_context_window(self, model_id: str) -> int | None:
        if self._model_lookup is None:
            return None

        for candidate in _model_lookup_candidates(model_id):
            model = self._model_lookup(candidate)
            if model is not None and model.context_window is not None and model.context_window > 0:
                return model.context_window
        return None

    def _uses_anthropic_messages_path(self, model_id: str) -> bool:
        """Route by the per-model ``metadata.opencode_go.protocol`` wire fact.

        ``"anthropic"`` → the internal Messages adapter; anything else →
        the OpenAI ``/chat/completions`` default. A model the override does not
        mark (no metadata, or no ``protocol`` key) is unknown: it takes the
        safe OpenAI default AND logs a ``warn``, so a newly added model is never
        silently misrouted onto the wrong wire.
        """

        return self._model_protocol(model_id) == PROTOCOL_ANTHROPIC

    def _model_protocol(self, model_id: str) -> str:
        protocol = self._lookup_protocol(model_id)
        if protocol in (PROTOCOL_ANTHROPIC, PROTOCOL_OPENAI, PROTOCOL_RESPONSES):
            return protocol
        # Unknown model (or a malformed/absent protocol fact): default safe and
        # warn so a misroute surfaces in logs instead of silently picking a wire.
        # Warn once per model id per process so a hot request path does not flood.
        if model_id not in _warned_unmarked_models:
            _warned_unmarked_models.add(model_id)
            _LOGGER.warning(
                "OpenCode Go model '%s' has no metadata protocol; defaulting to '%s' "
                "(chat/completions). Add metadata.opencode_go.protocol to its override "
                "entry to route it explicitly.",
                model_id,
                _DEFAULT_PROTOCOL,
            )
        return _DEFAULT_PROTOCOL

    def _lookup_protocol(self, model_id: str) -> str | None:
        value = self._profile_value(model_id, PROTOCOL_METADATA_KEY)
        return value if isinstance(value, str) else None

    def _profile_value(self, model_id: str, key: str) -> Any:
        model_lookup = getattr(self, "_model_lookup", None)
        if model_lookup is None:
            return None
        for candidate in _model_lookup_candidates(model_id):
            model = model_lookup(candidate)
            if model is None:
                continue
            opencode_go = model.metadata.get(OPENCODE_GO_METADATA_KEY)
            if isinstance(opencode_go, Mapping):
                return opencode_go.get(key)
            # The model exists but carries no profile — stop here so callers do
            # not scan weaker candidates after resolving an exact Model.
            return None
        return None

    def _thinking_keep(self, model_id: str) -> str | None:
        value = self._profile_value(model_id, THINKING_KEEP_METADATA_KEY)
        return value if isinstance(value, str) else None


def _bare_model_id(model_id: str) -> str:
    return model_id.split("::", 1)[0]


def _opencode_go_http_error_detail(
    response: httpx.Response,
    body: str | None = None,
) -> str:
    reason = response.text if body is None else body
    return f"{response.status_code} {reason}".strip() if reason else str(response.status_code)


def _model_lookup_candidates(model_id: str) -> tuple[str, ...]:
    without_connection_suffix = model_id.split("::", 1)[0]
    candidates = [model_id, without_connection_suffix]
    if "/" in without_connection_suffix:
        candidates.append(without_connection_suffix.rsplit("/", 1)[-1])
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))
