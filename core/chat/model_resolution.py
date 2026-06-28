"""Model identifiers and provider connection resolution for chat runs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.chat.errors import ChatError
from core.utils.logging import get_logger

if TYPE_CHECKING:
    from core.providers.providers import ProviderRegistry
    from core.runtime.interfaces import RuntimeServices

_LOGGER = get_logger("chat")


def parse_bare_model(model: str) -> str:
    """Return a model string without an optional ``::connection[:account]`` suffix."""
    before, separator, _suffix = model.rpartition("::")
    if not separator:
        return model
    return before


def parse_model_with_connection(model: str) -> tuple[str, str, str]:
    """Parse ``<provider>/<model-id>[::connection[:account]]`` into parts.

    The returned suffix is passed through verbatim — it may be a bare
    connection id or ``connection:account``; account resolution happens
    downstream in the credential resolver.
    """
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


def _model_input_modalities(runtime: RuntimeServices, agent: Any) -> frozenset[str]:
    """Return the agent model's input modalities, empty when the model is unknown.

    Degrading to an empty set silently drops image/audio attachments, so the
    expected lookup failures (malformed agent model -> ``ChatError``; unknown
    model -> ``KeyError``) are logged at ``warning`` with the model id to make
    the degrade visible.
    """
    try:
        provider_id, model_id = _split_agent_model(agent.model)
        model = runtime.models.get(provider_id, model_id)
    except (ChatError, KeyError) as error:
        _LOGGER.warning(
            "Could not resolve input modalities for model %r; "
            "treating model as having no input modalities: %s",
            getattr(agent, "model", None),
            error,
        )
        return frozenset()

    capabilities = getattr(model, "capabilities", None)
    modalities = getattr(capabilities, "input_modalities", ()) or ()
    return frozenset(str(modality) for modality in modalities)


def _resolve_agent_connection(runtime: RuntimeServices, agent: Any) -> tuple[str, str]:
    model_provider_id, model_id, connection_suffix = parse_model_with_connection(agent.model)
    if connection_suffix:
        return model_provider_id, f"{model_provider_id}:{connection_suffix}"

    allowed_connections = _model_connection_allowlist(runtime, model_provider_id, model_id)
    return model_provider_id, _first_usable_connection_id(
        runtime, model_provider_id, allowed_connections
    )


def _resolve_fallback(runtime: RuntimeServices, agent: Any) -> tuple[str, str, str] | None:
    fallback_model = getattr(agent, "fallback_model", "")
    if not fallback_model:
        return None

    try:
        fallback_provider_id, fallback_model_id, fallback_connection_suffix = (
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
        fallback_connection_id = _first_usable_connection_id(
            runtime,
            fallback_provider_id,
            _model_connection_allowlist(runtime, fallback_provider_id, fallback_model_id),
        )
    except ChatError:
        return None

    return fallback_model, fallback_provider_id, fallback_connection_id


def _model_connection_allowlist(
    runtime: RuntimeServices, provider_id: str, model_id: str
) -> tuple[str, ...]:
    """Return the model's connection allowlist, empty when the model is unknown.

    An empty tuple means "no restriction", matching ``Model.connections`` /
    ``Model.allows_connection``. An unknown or custom model id (``KeyError``) is
    treated as unrestricted, mirroring the save-time guard
    ``_ensure_model_connection_supported``.
    """
    try:
        model = runtime.models.get(provider_id, model_id)
    except KeyError:
        return ()
    return tuple(model.connections)


def _first_usable_connection_id(
    runtime: RuntimeServices,
    provider_id: str,
    allowed_connections: tuple[str, ...] = (),
) -> str:
    """Return the first usable ``<provider>:<connection>`` for a bare (unpinned) model.

    ``allowed_connections`` is the model's connection allowlist: when non-empty,
    only those connection ids are eligible, so a connection-bound model (e.g. a
    subscription-only model) can never silently resolve onto a forbidden
    connection just because it is configured and listed first. Empty means no
    restriction.
    """
    try:
        provider_config = runtime.providers.get(provider_id)
    except KeyError as exc:
        raise ChatError(f"provider not found: {provider_id}") from exc

    credential_resolver = runtime.provider_credentials
    for connection in provider_config.connections:
        if allowed_connections and connection.id not in allowed_connections:
            continue
        connection_id = f"{provider_id}:{connection.id}"
        if credential_resolver.has_credentials(provider_id, connection_id):
            return connection_id

    if allowed_connections:
        allowed = ", ".join(allowed_connections)
        raise ChatError(
            f"provider {provider_id} has no usable connection in the model's allowlist [{allowed}]"
        )
    raise ChatError(f"provider has no usable connections: {provider_id}")


def _ensure_provider_exists(providers: ProviderRegistry, provider_id: str) -> None:
    try:
        providers.get(provider_id)
    except KeyError as exc:
        raise ChatError(f"provider not found: {provider_id}") from exc
