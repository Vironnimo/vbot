"""Model identifiers and provider connection resolution for chat runs."""

from __future__ import annotations

from typing import Any

from core.chat.errors import ChatError


def parse_bare_model(model: str) -> str:
    """Return a model string without an optional ``::connection-suffix`` part."""
    before, separator, _suffix = model.rpartition("::")
    if not separator:
        return model
    return before


def parse_model_with_connection(model: str) -> tuple[str, str, str]:
    """Parse ``<provider>/<model-id>[::connection-id]`` into provider/model/suffix parts."""
    before, suffix_separator, connection_suffix = model.rpartition("::")
    if suffix_separator and not connection_suffix:
        raise ChatError("agent model connection suffix must not be empty")

    bare_model = before if suffix_separator else model
    if not bare_model:
        raise ChatError("agent has no model set")

    provider_id, separator, model_id = bare_model.partition("/")
    if not separator or not provider_id or not model_id:
        raise ChatError("agent model must use <provider>/<model-id>")

    if not suffix_separator:
        connection_suffix = ""
    return provider_id, model_id, connection_suffix


def _split_agent_model(model: str) -> tuple[str, str]:
    provider_id, model_id, _connection_suffix = parse_model_with_connection(model)
    return provider_id, model_id


def _model_input_modalities(runtime: Any, agent: Any) -> frozenset[str]:
    """Return the agent model's input modalities, empty when the model is unknown."""
    try:
        provider_id, model_id = _split_agent_model(agent.model)
        model = runtime.models.get(provider_id, model_id)
    except Exception:
        return frozenset()

    capabilities = getattr(model, "capabilities", None)
    modalities = getattr(capabilities, "input_modalities", ()) or ()
    return frozenset(str(modality) for modality in modalities)


def _resolve_agent_connection(runtime: Any, agent: Any) -> tuple[str, str]:
    model_provider_id, _model_id, connection_suffix = parse_model_with_connection(agent.model)
    if connection_suffix:
        return model_provider_id, f"{model_provider_id}:{connection_suffix}"

    return model_provider_id, _first_usable_connection_id(runtime, model_provider_id)


def _resolve_fallback(runtime: Any, agent: Any) -> tuple[str, str, str] | None:
    fallback_model = getattr(agent, "fallback_model", "")
    if not fallback_model:
        return None

    try:
        fallback_provider_id, _fallback_model_id, fallback_connection_suffix = (
            parse_model_with_connection(fallback_model)
        )
    except ChatError:
        return None

    if fallback_connection_suffix:
        return (
            fallback_model,
            fallback_provider_id,
            f"{fallback_provider_id}:{fallback_connection_suffix}",
        )

    try:
        fallback_connection_id = _first_usable_connection_id(runtime, fallback_provider_id)
    except ChatError:
        return None

    return fallback_model, fallback_provider_id, fallback_connection_id


def _first_usable_connection_id(runtime: Any, provider_id: str) -> str:
    try:
        provider_config = runtime.providers.get(provider_id)
    except KeyError as exc:
        raise ChatError(f"provider not found: {provider_id}") from exc

    credential_resolver = getattr(runtime, "provider_credentials", None)
    if credential_resolver is None:
        raise ChatError(f"agent has no connection set for provider: {provider_id}")

    for connection in provider_config.connections:
        connection_id = f"{provider_id}:{connection.id}"
        if credential_resolver.has_credentials(provider_id, connection_id):
            return connection_id

    raise ChatError(f"provider has no usable connections: {provider_id}")


def _ensure_provider_exists(providers: Any, provider_id: str) -> None:
    try:
        providers.get(provider_id)
    except KeyError as exc:
        raise ChatError(f"provider not found: {provider_id}") from exc
