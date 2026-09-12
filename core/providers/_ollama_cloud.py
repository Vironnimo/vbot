"""Ollama cloud."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import httpx

from core.providers._chat_completions_stream import (
    _stream_choices,
)
from core.providers._chat_completions_wire import (
    _first_choice_message,
)
from core.providers._ollama_constants import (
    _OLLAMA_CLOUD_OPENAI_PATH,
    _OLLAMA_CLOUD_REASONING_FIELD_DEFAULT,
    _OLLAMA_CLOUD_REASONING_FIELDS,
    _OLLAMA_CLOUD_REASONING_PARAMETERS,
    OLLAMA_CLOUD_REASONING_EFFORTS,
)
from core.providers.adapter import (
    IMAGE_WIRE_MEDIA_TYPES,
    ModelLookup,
)
from core.providers.errors import NetworkError
from core.providers.openai_compatible import (
    OpenAICompatibleAdapter,
)
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.reasoning import (
    REASONING_REPLAY_FIDELITY_READABLE_ONLY,
    ReasoningIntent,
    ReasoningReplayFidelity,
    model_reasoning_levels,
    normalize_thinking_effort,
    remove_reasoning_kwargs,
)
from core.providers.token_getter import TokenGetter

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder


class OllamaCloudAdapter(OpenAICompatibleAdapter):
    """OpenAI-compatible chat transport for direct Ollama Cloud connections.

    Discovery deliberately remains mapped to :class:`OllamaAdapter`; this class
    owns only the Cloud chat wire and its verified response quirks.
    """

    def request_body_limit(self, model_id: str) -> int | None:
        """Direct Cloud Chat rejects bodies above 16 MiB (verified 2026-09-11)."""
        del model_id
        return 16 * 1024 * 1024

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
        native_base_url = base_url or config.base_url
        self._cloud_base_url = _ollama_cloud_openai_base_url(native_base_url)
        # Run-local carrier observation: the first real response of an
        # unprofiled Model decides which reasoning field replay uses.
        self._scanned_reasoning_field: str | None = None
        super().__init__(
            config,
            token_getter,
            self._cloud_base_url,
            auth_config,
            model_lookup=model_lookup,
            debug_recorder=debug_recorder,
            connection_mode=connection_mode,
        )

    def wire_media_support(self, model_id: str) -> frozenset[str]:
        """Return the Model's verified Cloud image formats when profiled."""
        model = self._model_lookup(model_id.split("::", 1)[0]) if self._model_lookup else None
        metadata = model.metadata.get("ollama_cloud") if model else None
        media_types = metadata.get("image_media_types") if isinstance(metadata, Mapping) else None
        if isinstance(media_types, tuple | list):
            return frozenset(
                media_type
                for media_type in media_types
                if isinstance(media_type, str) and media_type in IMAGE_WIRE_MEDIA_TYPES
            )
        return IMAGE_WIRE_MEDIA_TYPES

    def reasoning_replay_fidelity(self, model_id: str) -> ReasoningReplayFidelity:
        """The compatible Cloud wire round-trips readable reasoning text only."""
        del model_id
        return REASONING_REPLAY_FIDELITY_READABLE_ONLY

    def _wrap_transport_error(self, exc: httpx.TransportError) -> Exception:
        """Preserve the Provider-specific direct Cloud connection diagnostic."""

        if isinstance(exc, httpx.ConnectError):
            return NetworkError(f"Ollama Cloud is not reachable at {self._cloud_base_url} ({exc})")
        return super()._wrap_transport_error(exc)

    def _supported_reasoning_efforts(self, model_id: str) -> tuple[str, ...]:
        """Intersect the Model ladder with Ollama Cloud's accepted wire values."""

        return self._reasoning_effort_ladder(self._model_lookup, self._config, model_id)

    @classmethod
    def _reasoning_effort_ladder(
        cls,
        model_lookup: ModelLookup | None,
        provider_config: ProviderConfig | None,
        model_id: str,
    ) -> tuple[str, ...]:
        """Class-level twin of :meth:`_supported_reasoning_efforts`.

        The render path snaps against this through the instance; the render
        description (``describe_reasoning_render``) snaps against the same
        Cloud ladder without needing an adapter instance.
        """

        del provider_config
        declared = model_reasoning_levels(model_lookup, model_id)
        if declared is None:
            return OLLAMA_CLOUD_REASONING_EFFORTS
        supported: list[str] = ["none"]
        for effort in declared:
            wire_effort = "max" if effort == "xhigh" else effort
            if wire_effort in OLLAMA_CLOUD_REASONING_EFFORTS and wire_effort not in supported:
                supported.append(wire_effort)
        return tuple(supported)

    def _apply_reasoning(
        self,
        payload: dict[str, Any],
        request_kwargs: dict[str, Any],
        model_id: str,
    ) -> None:
        """Render the Cloud effort vocabulary, mapping vBot ``xhigh`` to ``max``."""

        if self._model_reasoning_supported(model_id) is not True:
            # Match Ollama's catalog contract: only send a reasoning control
            # after /api/show has positively identified the Model as a thinker.
            remove_reasoning_kwargs(
                request_kwargs,
                *_OLLAMA_CLOUD_REASONING_PARAMETERS,
            )
            return

        selected_key = (
            "thinking_effort" if request_kwargs.get("thinking_effort") else "reasoning_effort"
        )
        selected_effort = normalize_thinking_effort(request_kwargs.get(selected_key))
        if selected_effort == "xhigh":
            request_kwargs[selected_key] = "max"
        super()._apply_reasoning(payload, request_kwargs, model_id)
        if selected_effort == "none" and self._model_reasoning_supported(model_id) is True:
            # Ollama Cloud defaults thinking on. Its OpenAI wire spells the off
            # switch as an effort even when native /api/show describes the Model
            # as a binary on/off thinker.
            payload["reasoning_effort"] = "none"

    @classmethod
    def describe_reasoning_render(
        cls,
        *,
        model_lookup: ModelLookup | None,
        model_id: str,
        effort: str | None,
        provider_config: ProviderConfig | None = None,
    ) -> ReasoningIntent:
        """Describe the Cloud render, mapping vBot ``xhigh`` to Ollama's ``max``.

        Mirrors :meth:`_apply_reasoning`: the selected effort is normalized
        into Ollama Cloud's wire vocabulary before the shared generic-wire
        description resolves it against the Cloud ladder.
        """

        if normalize_thinking_effort(effort) == "xhigh":
            effort = "max"
        return super().describe_reasoning_render(
            model_lookup=model_lookup,
            model_id=model_id,
            effort=effort,
            provider_config=provider_config,
        )

    def _format_assistant_message(
        self,
        message: dict[str, Any],
        *,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Replay readable reasoning under the Model's declared wire field.

        Ollama Cloud's OpenAI-compatible endpoint returns reasoning in a
        provider-specific ``reasoning_content`` or ``reasoning`` field. The
        GLM and Kimi backends require the
        full historical reasoning to be replayed for multi-turn quality, and
        MiniMax documents that preserving the reasoning chain is essential for
        best performance. The base ``readable_only`` fidelity already injects
        ``reasoning_content``; this override renames it to the Model's scanned
        carrier field when that differs. Gated on the Model's catalog
        ``reasoning_response_field`` so only Models whose wire actually carries
        the field get it injected.
        """

        formatted = super()._format_assistant_message(message, model_id=model_id)
        target_model_id = (model_id or str(message.get("model") or "")).rsplit("/", 1)[-1]
        field = self._reasoning_replay_field(target_model_id)
        if field not in _OLLAMA_CLOUD_REASONING_FIELDS:
            return formatted
        reasoning = message.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            if field != "reasoning_content":
                formatted.pop("reasoning_content", None)
            formatted[field] = reasoning
        return formatted

    def _reasoning_replay_field(self, model_id: str) -> str:
        """Resolve the wire field for replaying readable reasoning.

        Precedence: the Model's catalog ``reasoning_response_field`` (the
        verified profile wins), then the field observed on this Run's real
        provider responses (the scan), then the de-facto standard
        ``reasoning_content`` for the first replay of an unprofiled Model.
        """

        profiled = self._reasoning_response_field(model_id)
        if profiled in _OLLAMA_CLOUD_REASONING_FIELDS:
            return profiled
        if self._scanned_reasoning_field in _OLLAMA_CLOUD_REASONING_FIELDS:
            return self._scanned_reasoning_field
        return _OLLAMA_CLOUD_REASONING_FIELD_DEFAULT

    def _scan_reasoning_field(self, raw_message: Any) -> None:
        """Remember which reasoning carrier this Run's responses actually use.

        Unprofiled Models cannot be trusted to a guessed field, so the first
        real response decides: the first non-empty carrier among
        ``reasoning_content`` and ``reasoning`` wins for the rest of the Run.
        Profiled Models never reach the scan — their override already won.
        """

        if self._scanned_reasoning_field in _OLLAMA_CLOUD_REASONING_FIELDS:
            return
        if not isinstance(raw_message, Mapping):
            return
        for field in _OLLAMA_CLOUD_REASONING_FIELDS:
            value = raw_message.get(field)
            if isinstance(value, str) and value:
                self._scanned_reasoning_field = field
                return

    def normalize_response(
        self, response: dict[str, Any], *, model_id: str | None = None
    ) -> dict[str, Any]:
        """Normalize a Cloud response without trusting impossible zero input usage."""

        self._scan_reasoning_field(_first_choice_message(response))
        normalized = super().normalize_response(response, model_id=model_id)
        _drop_ollama_cloud_zero_prompt_tokens(normalized.get("usage"), response.get("usage"))
        return normalized

    def _normalize_stream_chunk(
        self,
        raw_chunk: dict[str, Any],
        tool_call_slots: set[int],
        normalization_state: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        deltas = super()._normalize_stream_chunk(
            raw_chunk,
            tool_call_slots,
            normalization_state,
        )
        raw_usage = raw_chunk.get("usage")
        for delta in deltas:
            if delta.get("type") == "usage":
                _drop_ollama_cloud_zero_prompt_tokens(delta, raw_usage)
        for choice in _stream_choices(raw_chunk):
            raw_delta = choice.get("delta")
            if isinstance(raw_delta, dict):
                self._scan_reasoning_field(raw_delta)
        return deltas


def _ollama_cloud_openai_base_url(native_base_url: str) -> str:
    """Return the direct Cloud OpenAI base without disturbing native endpoints."""

    normalized = native_base_url.rstrip("/")
    return (
        normalized
        if normalized.endswith(_OLLAMA_CLOUD_OPENAI_PATH)
        else (f"{normalized}{_OLLAMA_CLOUD_OPENAI_PATH}")
    )


def _drop_ollama_cloud_zero_prompt_tokens(normalized_usage: Any, raw_usage: Any) -> None:
    """Treat Cloud ``prompt_tokens: 0`` as absent for non-empty chat requests.

    MiniMax M3 returns zero for short and Tool requests while returning positive
    counts for longer prompts. Every vBot chat request has at least one message,
    so zero cannot be a truthful input-token measurement. Removing only that
    field lets the chat layer retain measured output while estimating input.
    """

    if not isinstance(normalized_usage, dict) or not isinstance(raw_usage, Mapping):
        return
    prompt_tokens = raw_usage.get("prompt_tokens")
    if (
        isinstance(prompt_tokens, int)
        and not isinstance(prompt_tokens, bool)
        and prompt_tokens == 0
    ):
        normalized_usage.pop("input_tokens", None)
