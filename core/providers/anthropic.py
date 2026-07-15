"""Native Anthropic provider policy layered over the reusable Messages wire."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from core.debug import ProviderDebugRecorder

from core.models.models import (
    REASONING_CONTROL_BUDGET,
    REASONING_CONTROL_LEVELS,
    Capabilities,
    Model,
    ReasoningCapabilities,
)
from core.providers.adapter import IMAGE_WIRE_MEDIA_TYPES, ModelLookup
from core.providers.anthropic_compatible import (
    ANTHROPIC_OVERLOADED_STATUS,
    ANTHROPIC_VERSION,
    AnthropicCompatibleAdapter,
)
from core.providers.providers import AuthConfig, ProviderConfig
from core.providers.token_getter import TokenGetter

MODELS_DISCOVERY_PAGE_SIZE = "1000"
ANTHROPIC_METADATA_KEY = "anthropic"
SUPPORTS_TEMPERATURE_METADATA_FIELD = "supports_temperature"
ANTHROPIC_EFFORT_LEVEL_ORDER = ("low", "medium", "high", "xhigh", "max")


class AnthropicAdapter(AnthropicCompatibleAdapter):
    """Concrete Anthropic provider with native discovery and model policy."""

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
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            config,
            token_getter,
            base_url,
            auth_config,
            model_lookup,
            debug_recorder,
            connection_mode=connection_mode,
            client=client,
            api_version=ANTHROPIC_VERSION,
            wire_media_types=IMAGE_WIRE_MEDIA_TYPES | {"application/pdf"},
            prompt_caching=True,
            extra_retryable_statuses=frozenset({ANTHROPIC_OVERLOADED_STATUS}),
        )

    @classmethod
    def discovery_headers(
        cls,
        _provider_config: ProviderConfig,
        credential_value: str,
        headers: Mapping[str, str],
    ) -> dict[str, str]:
        """Add the native version header required by Anthropic discovery."""

        del credential_value
        return {**headers, "anthropic-version": ANTHROPIC_VERSION}

    @classmethod
    def discovery_params(cls) -> dict[str, str]:
        """Request the full native Anthropic model lineup."""

        return {"limit": MODELS_DISCOVERY_PAGE_SIZE}

    @classmethod
    def normalize_catalog_entry(
        cls,
        raw: Mapping[str, Any],
        defaults: Mapping[str, Any] | None = None,
    ) -> Model:
        """Normalize one rich native Anthropic catalog entry."""

        del defaults
        model_id = _read_catalog_string(raw, "id")
        name = _read_catalog_string(raw, "display_name") or model_id
        caps = _catalog_mapping(raw, "capabilities")
        thinking = _catalog_mapping(caps, "thinking")
        thinking_types = _catalog_mapping(thinking, "types")
        reasoning_supported = thinking.get("supported") is True
        adaptive = _catalog_supported(thinking_types, "adaptive")
        enabled = _catalog_supported(thinking_types, "enabled")
        effort_levels = tuple(
            level
            for level in ANTHROPIC_EFFORT_LEVEL_ORDER
            if _catalog_supported(_catalog_mapping(caps, "effort"), level)
        )
        control, levels = _anthropic_reasoning_control(
            reasoning_supported,
            adaptive=adaptive,
            enabled=enabled,
            effort_levels=effort_levels,
        )

        image = _catalog_supported(caps, "image_input")
        pdf = _catalog_supported(caps, "pdf_input")
        input_modalities = ("text", *(("image",) if image else ()), *(("pdf",) if pdf else ()))

        return Model(
            model_id=model_id,
            name=name,
            capabilities=Capabilities(
                vision=image,
                tools=True,
                json_mode=_catalog_supported(caps, "structured_outputs"),
                reasoning=ReasoningCapabilities(
                    supported=reasoning_supported,
                    control=control,
                    levels=levels,
                ),
                input_modalities=input_modalities,
                output_modalities=("text",),
            ),
            context_window=_read_catalog_int(raw, "max_input_tokens"),
            max_output_tokens=_read_catalog_int(raw, "max_tokens"),
            metadata={
                ANTHROPIC_METADATA_KEY: {
                    SUPPORTS_TEMPERATURE_METADATA_FIELD: _anthropic_supports_temperature(
                        reasoning_supported,
                        adaptive=adaptive,
                        enabled=enabled,
                    )
                }
            },
        )

    def _model_supports_temperature(self, model_id: str) -> bool:
        """Read Anthropic's discovery-derived per-model sampling policy."""

        if self._model_lookup is None:
            return True
        model = self._model_lookup(model_id.split("::", 1)[0])
        if model is None:
            return True
        provider_metadata = model.metadata.get(ANTHROPIC_METADATA_KEY)
        if isinstance(provider_metadata, Mapping):
            value = provider_metadata.get(SUPPORTS_TEMPERATURE_METADATA_FIELD)
            if isinstance(value, bool):
                return value
        return True


def _anthropic_reasoning_control(
    supported: bool,
    *,
    adaptive: bool,
    enabled: bool,
    effort_levels: tuple[str, ...],
) -> tuple[str | None, tuple[str, ...]]:
    if not supported:
        return None, ()
    if adaptive:
        return REASONING_CONTROL_LEVELS, effort_levels
    if enabled:
        return REASONING_CONTROL_BUDGET, ()
    return None, ()


def _anthropic_supports_temperature(
    supported: bool,
    *,
    adaptive: bool,
    enabled: bool,
) -> bool:
    return not (supported and adaptive and not enabled)


def _read_catalog_string(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    return value if isinstance(value, str) else ""


def _read_catalog_int(raw: Mapping[str, Any], key: str) -> int | None:
    value = raw.get(key)
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def _catalog_mapping(raw: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = raw.get(key)
    return value if isinstance(value, Mapping) else {}


def _catalog_supported(raw: Mapping[str, Any], key: str) -> bool:
    node = raw.get(key)
    return isinstance(node, Mapping) and node.get("supported") is True
