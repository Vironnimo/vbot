"""Protocol interfaces for the vBot runtime.

Defines typing.Protocol contracts that enable constructor-injection
and testability without dragging in concrete implementations.
"""

from typing import Any, Protocol

from core.agents.agents import Agent
from core.chat.chat import ChatSession
from core.models.models import Model
from core.providers.providers import ProviderConfig
from core.skills.skills import SkillMetadata
from core.tools.tools import Tool


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


class ProviderCredentialResolverProtocol(Protocol):
    """Protocol for centralized provider credential access."""

    def has_credentials(self, provider_id: str, connection_id: str | None = None) -> bool:
        """Return whether *provider_id* or *connection_id* has a non-empty credential."""
        ...

    def get_credentials(self, provider_id: str, connection_id: str | None = None) -> str:
        """Return the credential value for *provider_id* or *connection_id*."""
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


class StorageManagerProtocol(Protocol):
    """Protocol for runtime storage services."""

    def ensure_directories(self) -> None:
        """Create required data directories."""
        ...

    def read_prompt_fragment(self, fragment_name: str) -> str:
        """Read a prompt fragment by name."""
        ...


class AgentStoreProtocol(Protocol):
    """Protocol for persisted agent CRUD."""

    def create(self, agent_id: str, name: str, **fields: Any) -> Agent:
        """Create a persisted agent."""
        ...

    def get(self, agent_id: str) -> Agent:
        """Load a persisted agent."""
        ...

    def list(self) -> list[Agent]:
        """List persisted agents."""
        ...


class ToolRegistryProtocol(Protocol):
    """Protocol for runtime tool registry access."""

    def list_tools(self, allowed_tools: list[str] | None = None) -> list[Tool]:
        """List tools filtered by allowlist."""
        ...


class SkillRegistryProtocol(Protocol):
    """Protocol for runtime skill metadata access."""

    def list_all(self) -> list[SkillMetadata]:
        """List all loaded skills."""
        ...


class ChatSessionManagerProtocol(Protocol):
    """Protocol for runtime chat session access."""

    def create(self, agent_id: str, session_id: str | None = None) -> ChatSession:
        """Create a chat session for an agent."""
        ...
