"""OpenCode Go provider adapter."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

from core.providers.adapter import ModelLookup
from core.providers.anthropic_compatible import (
    ANTHROPIC_OVERLOADED_STATUS,
    ANTHROPIC_VERSION,
    AnthropicCompatibleAdapter,
)
from core.providers.openai_compatible import OpenAICompatibleAdapter, _to_openai_assistant_message
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.reasoning import (
    REASONING_REPLAY_CURRENT_RUN,
    REASONING_REPLAY_POLICIES,
    ReasoningReplayPolicy,
    normalize_thinking_effort,
)
from core.providers.token_getter import TokenGetter
from core.utils.logging import get_logger

_LOGGER = get_logger("providers.opencode_go")

# Provider-scoped metadata blob + field carrying the per-model wire protocol
# (Phase 5). The published opencode-go protocol table is a per-model FACT, so it
# lives in data (the opencode-go override's ``metadata.opencode_go.protocol``),
# not in a hardcoded adapter set. The adapter only owns the MECHANICS — how to
# build an Anthropic ``/messages`` vs an OpenAI ``/chat/completions`` request.
OPENCODE_GO_METADATA_KEY = "opencode_go"
PROTOCOL_METADATA_KEY = "protocol"
PROTOCOL_ANTHROPIC = "anthropic"
PROTOCOL_OPENAI = "openai"
REASONING_REPLAY_METADATA_KEY = "reasoning_replay"
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

# ``_model_protocol`` runs on every send/stream, so an unmarked model would re-log
# its routing warning on each request and flood the log. Track which model ids have
# already warned in this process and emit each once — "once per server runtime"
# (a restart re-warns). Mirrors the skill-validation dedup in core/skills/skills.py.
_warned_unmarked_models: set[str] = set()


class OpenCodeGoAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible adapter for the OpenCode Go gateway.

    Models with reasoning capability (DeepSeek, Kimi, GLM, ...) return
    ``reasoning_content`` in assistant messages.

    Replay scope and request controls are explicit per-Model facts. Both wire
    routes can carry historical reasoning, but acceptance by a protocol does
    not prove that every underlying Model uses reasoning from earlier Runs.
    History shaping is owned by the chat layer; this adapter translates only
    the reasoning that survives the selected Model profile.
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
        self._messages = AnthropicCompatibleAdapter(
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

    def reasoning_replay_policy(self, model_id: str) -> ReasoningReplayPolicy:
        """Replay persisted reasoning only for explicitly profiled Models.

        ``metadata.opencode_go.reasoning_replay`` is independent of the wire
        protocol: two Models on the same endpoint may have different history
        semantics. An absent or malformed profile keeps only active-Run
        reasoning instead of inheriting a Provider-wide assumption.
        """
        replay = self._profile_value(model_id, REASONING_REPLAY_METADATA_KEY)
        if replay in REASONING_REPLAY_POLICIES:
            return cast(ReasoningReplayPolicy, replay)
        return REASONING_REPLAY_CURRENT_RUN

    def wire_media_support(self, model_id: str) -> frozenset[str]:
        """Resolve media support from the wire selected for this model."""

        if self._uses_anthropic_messages_path(model_id):
            return self._messages.wire_media_support(model_id)
        return super().wire_media_support(model_id)

    async def send(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        request_kwargs = self._kwargs_with_model_output_limit(model_id, messages, kwargs)
        if self._uses_anthropic_messages_path(model_id):
            return await self._messages.send(
                messages,
                model_id=model_id,
                **request_kwargs,
            )
        return await super().send(messages, model_id=model_id, **request_kwargs)

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        request_kwargs = self._kwargs_with_model_output_limit(model_id, messages, kwargs)
        if self._uses_anthropic_messages_path(model_id):
            return self._messages.stream(
                messages,
                model_id=model_id,
                **request_kwargs,
            )
        return super().stream(messages, model_id=model_id, **request_kwargs)

    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        if model_id is not None:
            if self._uses_anthropic_messages_path(model_id):
                return self._messages.normalize_response(response, model_id=model_id)
            return super().normalize_response(response, model_id=model_id)
        if "choices" in response:
            return super().normalize_response(response, model_id=model_id)
        return self._messages.normalize_response(response, model_id=model_id)

    def _format_assistant_message(self, message: dict[str, Any]) -> dict[str, Any]:
        wire = _to_openai_assistant_message(message)
        reasoning = message.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            wire["reasoning_content"] = reasoning
        return wire

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
        if protocol in (PROTOCOL_ANTHROPIC, PROTOCOL_OPENAI):
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
        if self._model_lookup is None:
            return None
        for candidate in _model_lookup_candidates(model_id):
            model = self._model_lookup(candidate)
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


def _model_lookup_candidates(model_id: str) -> tuple[str, ...]:
    without_connection_suffix = model_id.split("::", 1)[0]
    candidates = [model_id, without_connection_suffix]
    if "/" in without_connection_suffix:
        candidates.append(without_connection_suffix.rsplit("/", 1)[-1])
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate))
