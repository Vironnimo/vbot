"""Shared Provider configurations and catalog Models for the Provider-neutral core tests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.models.models import Capabilities, Model, ReasoningCapabilities
from core.providers.providers import AuthConfig, ConnectionConfig, ProviderConfig

TOKEN = "test-token"


class AdapterHookDefaults:
    """The ``ProviderAdapter`` hook defaults for Adapter test doubles outside the ABC.

    Every real Adapter inherits these from ``ProviderAdapter``; a plain double
    mixes them in so request paths can call each hook unconditionally.
    """

    def image_size_limit(self, model_id: str) -> int | None:
        del model_id
        return None

    def request_image_limit(self, model_id: str) -> int | None:
        del model_id
        return None

    def request_context_kwargs(self, **context: Any) -> dict[str, Any]:
        del context
        return {}

    def set_debug_context(self, context: Any) -> None:
        del context


def bearer_config(
    provider_id: str,
    *,
    adapter: str | None = None,
    base_url: str | None = None,
    **fields: Any,
) -> ProviderConfig:
    """A Provider with one ``api-key`` Connection sending ``Authorization: Bearer``."""

    return ProviderConfig(
        id=provider_id,
        name=provider_id,
        adapter=adapter or provider_id.replace("-", "_"),
        base_url=base_url or f"https://{provider_id}.example.test/v1",
        connections=[
            ConnectionConfig(
                id="api-key",
                type="api_key",
                label="API Key",
                auth=AuthConfig(
                    header="Authorization",
                    prefix="Bearer ",
                    credential_key=f"{provider_id.upper().replace('-', '_')}_API_KEY",
                ),
            )
        ],
        **fields,
    )


def catalog_model(
    model_id: str,
    *,
    context_window: int | None = None,
    max_output_tokens: int | None = None,
    metadata: Mapping[str, Any] | None = None,
    reasoning_replay: str | None = None,
) -> Model:
    """A Tool-capable, non-reasoning catalog Model carrying the given wire facts."""

    return Model(
        model_id=model_id,
        name=model_id,
        family=model_id,
        capabilities=Capabilities(
            vision=False,
            tools=True,
            json_mode=False,
            reasoning=ReasoningCapabilities(supported=False),
        ),
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        metadata=dict(metadata or {}),
        reasoning_replay=reasoning_replay,
    )
