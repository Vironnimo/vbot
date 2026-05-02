"""Protocol interfaces for the vBot runtime.

Defines typing.Protocol contracts that enable constructor-injection
and testability without dragging in concrete implementations.
"""

from typing import Any, Protocol

from core.models.models import Model
from core.providers.providers import ProviderConfig


class LoggerProtocol(Protocol):
    """Protocol for any logger-like object.

    Any object with these three methods satisfies the contract,
    whether it is a standard ``logging.Logger``, a mock, or a
    custom implementation.
    """

    def info(self, msg: str) -> None:
        """Log an informational message."""
        ...

    def error(self, msg: str) -> None:
        """Log an error message."""
        ...

    def debug(self, msg: str) -> None:
        """Log a debug message."""
        ...


class ConfigProtocol(Protocol):
    """Protocol for any configuration provider.

    Any object with a ``get(key, default)`` method satisfies the
    contract.
    """

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value for *key*, or *default* if not found."""
        ...


class ProviderRegistryProtocol(Protocol):
    """Protocol for a provider registry.

    Any object with ``get(provider_id)`` and ``list_ids()`` methods
    satisfies the contract.
    """

    def get(self, provider_id: str) -> ProviderConfig:
        """Return the provider config for *provider_id*.

        Args:
            provider_id: Unique provider identifier.

        Returns:
            The matching ``ProviderConfig``.

        Raises:
            KeyError: If no provider with *provider_id* is registered.
        """
        ...

    def list_ids(self) -> list[str]:
        """Return a sorted list of all registered provider IDs."""
        ...


class ModelRegistryProtocol(Protocol):
    """Protocol for a model registry.

    Any object with ``get(provider_id, model_id)`` and
    ``list_for_provider(provider_id)`` methods satisfies the contract.
    """

    def get(self, provider_id: str, model_id: str) -> Model:
        """Look up a model by provider ID and model ID.

        Args:
            provider_id: The provider identifier.
            model_id: The exact model ID sent in API requests.

        Returns:
            The matching ``Model`` entry.

        Raises:
            KeyError: If no model matches the given provider and model ID.
        """
        ...

    def list_for_provider(self, provider_id: str) -> list[Model]:
        """Return all models for a given provider, sorted by model_id.

        Args:
            provider_id: The provider identifier.

        Returns:
            A sorted list of ``Model`` entries for the provider.
        """
        ...
