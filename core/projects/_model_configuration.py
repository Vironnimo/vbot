"""Model availability and permitted Connection validation."""

from __future__ import annotations

from typing import Protocol


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
    has usable credentials and the model's per-model connection allowlist
    (``Model.allows_connection`` — empty means unrestricted) permits it. A pinned
    ``::connection[:account]`` suffix narrows the question to exactly that
    connection (and account). This mirrors what the chat runtime enforces at
    request time (``core/chat/model_resolution.py`` — allowlist-filtered
    connection pick, verbatim pinned suffix), so a model this gate accepts never
    fails connection resolution at run time. It is the single rule the model
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

    def is_configured(self, model: str) -> bool:
        """Return whether *model* names a model that can actually run here."""
        parsed = _parse_provider_model(model)
        if parsed is None:
            return False
        provider_id, model_id, connection_suffix = parsed

        try:
            self._providers.get(provider_id)
        except KeyError:
            return False

        try:
            catalog_model = self._models.get(provider_id, model_id)
        except KeyError:
            return False

        if connection_suffix:
            return self._pinned_connection_usable(provider_id, catalog_model, connection_suffix)
        return self._has_usable_allowed_connection(provider_id, catalog_model)

    def require_configured(self, model: str) -> None:
        """Require *model* to be runnable and retain precise Connection failures."""
        parsed = _parse_provider_model(model)
        if parsed is None:
            raise self._unusable_error(model)
        provider_id, model_id, connection_suffix = parsed

        try:
            provider_config = self._providers.get(provider_id)
            catalog_model = self._models.get(provider_id, model_id)
        except KeyError as error:
            raise self._unusable_error(model) from error

        if connection_suffix:
            connection_local_id = connection_suffix.partition(":")[0]
            if not catalog_model.allows_connection(connection_local_id):
                allowed = ", ".join(catalog_model.connections)
                raise ModelConfigurationError(
                    f"model {provider_id}/{model_id} is not available on connection "
                    f"'{connection_local_id}' (allowed connections: {allowed})"
                )
            connections = getattr(provider_config, "connections", [])
            if all(connection.id != connection_local_id for connection in connections):
                raise self._unusable_error(model)
            if not self._provider_credentials.is_usable(
                provider_id, f"{provider_id}:{connection_suffix}"
            ):
                raise self._unusable_error(model)
            return

        if not self._has_usable_allowed_connection(provider_id, catalog_model):
            raise self._unusable_error(model)

    @staticmethod
    def _unusable_error(model: str) -> ModelConfigurationError:
        return ModelConfigurationError(
            f"model {model!r} is not usable in this instance "
            "(unknown provider/model or no usable credential on an allowed connection)"
        )

    def _has_usable_allowed_connection(
        self, provider_id: str, catalog_model: ConnectionRestrictedModel
    ) -> bool:
        """Whether any connection is both allowed by the model and credentialed.

        Mirrors the runtime's unpinned pick (``_first_usable_connection_id``): a
        connection outside the model's allowlist never counts, so a
        connection-bound model (e.g. subscription-only) with credentials only on
        a forbidden connection is *not* configured — the chain falls through
        instead of the run failing later.
        """
        provider_config = self._providers.get(provider_id)
        # ProviderConfig.connections is a list of ConnectionConfig with an ``id``
        # local part; the usable check uses the compositional ``provider:conn`` id.
        connections = getattr(provider_config, "connections", [])
        for connection in connections:
            if not catalog_model.allows_connection(connection.id):
                continue
            connection_id = f"{provider_id}:{connection.id}"
            if self._provider_credentials.is_usable(provider_id, connection_id):
                return True
        return False

    def _pinned_connection_usable(
        self, provider_id: str, catalog_model: ConnectionRestrictedModel, connection_suffix: str
    ) -> bool:
        """Whether the pinned ``connection[:account]`` exists, is allowed, and is usable.

        The runtime reconstructs the pinned connection verbatim and resolves its
        credential downstream, so the gate checks exactly that path: the local
        connection id must exist on the provider, pass the model's allowlist, and
        ``is_usable`` (enabled + credentialed) must hold for the full (possibly
        account-pinned) id.
        """
        connection_local_id = connection_suffix.partition(":")[0]
        if not catalog_model.allows_connection(connection_local_id):
            return False
        provider_config = self._providers.get(provider_id)
        connections = getattr(provider_config, "connections", [])
        if all(connection.id != connection_local_id for connection in connections):
            return False
        return self._provider_credentials.is_usable(
            provider_id, f"{provider_id}:{connection_suffix}"
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
