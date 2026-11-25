"""Model availability and permitted Connection validation."""

from __future__ import annotations

from typing import Protocol

from core.providers.account_catalogs import account_model_routes


class ModelConfigurationError(ValueError):
    """A Model reference cannot run with this instance's configured routes."""


class ConnectionRestrictedModel(Protocol):
    """The catalog-model slice the checker reads: the per-model connection rule.

    ``allows_connection`` is the single source of the connection allowlist
    (``core.models.Model.allows_connection``): an empty allowlist permits every
    connection, a non-empty one restricts the model to the listed connection ids.
    """

    @property
    def connections(self) -> tuple[str, ...]: ...

    def allows_connection(self, connection_id: str) -> bool: ...


class ModelProbe(Protocol):
    """The model-registry slice used to answer "does this model exist?"."""

    def get(self, provider_id: str, model_id: str) -> ConnectionRestrictedModel: ...


class ProviderProbe(Protocol):
    """The provider-registry slice used to find a provider's connections."""

    def get(self, provider_id: str) -> object: ...


class CredentialProbe(Protocol):
    """The credential slice used to answer "is a connection usable?".

    Usability = enabled (settings override or type default) AND credentialed —
    owned by ``ProviderCredentialResolver.is_usable``.
    """

    def is_usable(self, provider_id: str, connection_id: str | None = None) -> bool: ...


class ModelConfigurationChecker:
    """Decides whether a ``<provider>/<model-id>[::connection[:account]]`` can run here.

    "Configured in this instance" = the provider is registered, the model is in
    that provider's catalog, and a connection is usable **for this model**: it
    has a usable Account whose workspace catalog permits the Model, and its Connection allowlist
    (``Model.allows_connection`` — empty means unrestricted) permits it. A pinned
    ``::connection[:account]`` suffix narrows the question to exactly that
    connection (and account). This mirrors what the chat runtime enforces at
    request time (``core/chat/model_resolution.py``). It is the single rule the model
    chain, the scan's ``BAD_MODEL`` check, and the ``/model`` set-time gate all
    consult, so they cannot drift.
    """

    def __init__(
        self,
        models: ModelProbe,
        providers: ProviderProbe,
        provider_credentials: CredentialProbe,
    ) -> None:
        self._models = models
        self._providers = providers
        self._provider_credentials = provider_credentials

    def is_configured(self, model: str, *, defer_workspace_access: bool = False) -> bool:
        """Return whether the Model passes the same checks used for explicit selection."""
        try:
            self.require_configured(model, defer_workspace_access=defer_workspace_access)
        except ModelConfigurationError:
            return False
        return True

    def require_configured(self, model: str, *, defer_workspace_access: bool = False) -> None:
        """Validate a binding. Run resolution can leave workspace access to its fallback chain."""
        parsed = _parse_provider_model(model)
        if parsed is None:
            raise self._unusable_error(model)
        provider_id, model_id, connection_suffix = parsed

        try:
            provider_config = self._providers.get(provider_id)
            catalog_model = self._models.get(provider_id, model_id)
        except KeyError as error:
            raise self._unusable_error(model) from error

        connections = getattr(provider_config, "connections", [])
        if connection_suffix:
            connection_local_id = connection_suffix.partition(":")[0]
            if not catalog_model.allows_connection(connection_local_id):
                allowed = ", ".join(catalog_model.connections)
                raise ModelConfigurationError(
                    f"model {provider_id}/{model_id} is not available on connection "
                    f"'{connection_local_id}' (allowed connections: {allowed})"
                )
            if all(connection.id != connection_local_id for connection in connections):
                raise self._unusable_error(model)
            candidates = [connection_suffix]
        else:
            candidates = [
                connection.id
                for connection in connections
                if catalog_model.allows_connection(connection.id)
            ]
        for suffix in candidates:
            connection_id = f"{provider_id}:{suffix}"
            if not self._provider_credentials.is_usable(provider_id, connection_id):
                continue
            if (
                defer_workspace_access
                or account_model_routes(self._provider_credentials, provider_id, connection_id)(
                    model_id
                )
                is not None
            ):
                return
        raise self._unusable_error(model)

    def require_chat_configured(self, model: str) -> None:
        """Require a usable Chat route, retaining sparse catalogs' text default."""
        self.require_configured(model)
        parsed = _parse_provider_model(model)
        assert parsed is not None
        catalog = self._models.get(parsed[0], parsed[1])
        tasks = getattr(getattr(catalog, "capabilities", None), "task_types", ())
        if tasks and "chat" not in tasks:
            raise ModelConfigurationError(f"model {model!r} does not support Chat")

    @staticmethod
    def _unusable_error(model: str) -> ModelConfigurationError:
        return ModelConfigurationError(
            f"model {model!r} is not usable in this instance "
            "(unknown provider/model or no usable Account with access on an allowed Connection)"
        )


def _parse_provider_model(model: str) -> tuple[str, str, str] | None:
    """Split ``<provider>/<model-id>[::connection[:account]]`` into its parts.

    Returns ``(provider, model_id, suffix)`` with ``suffix == ""`` when unpinned,
    or ``None`` for an empty or malformed string (no provider/model split, or an
    empty suffix after ``::``), which the chain treats as "no model" so it falls
    through cleanly. Uses ``rpartition`` like the canonical chat-side parse
    (``parse_model_with_connection``) so the two can never split differently.
    """
    if not model:
        return None
    before, suffix_separator, connection_suffix = model.rpartition("::")
    if suffix_separator and not connection_suffix:
        return None
    bare = before if suffix_separator else model
    provider_id, separator, model_id = bare.partition("/")
    if not separator or not provider_id or not model_id:
        return None
    return provider_id, model_id, connection_suffix if suffix_separator else ""
