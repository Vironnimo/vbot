"""Protocol interfaces for the vBot runtime.

Defines typing.Protocol contracts that enable constructor-injection
and testability without dragging in concrete implementations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from core.providers.accounts import ProviderAccount

if TYPE_CHECKING:
    from core.agents.agents import AgentStore
    from core.chat import ChatLoop
    from core.extensions import ExtensionRegistry
    from core.models.models import ModelRegistry
    from core.projects import AgentResolver, ProjectStore
    from core.prompts import SystemPromptManager
    from core.providers.adapter import ProviderAdapter
    from core.providers.providers import ProviderRegistry
    from core.runs import ChatRunManager
    from core.sessions import ChatSessionManager
    from core.skills.skills import SkillMetadata, SkillRegistry
    from core.storage import StorageManager
    from core.tools.process_manager import ProcessManager
    from core.tools.tools import ToolRegistry


class LoggerProtocol(Protocol):
    """Protocol for any logger-like object.

    Any object with these three methods satisfies the contract,
    whether it is a standard ``logging.Logger``, a mock, or a
    custom implementation.
    """

    def info(self, msg: str, *args: Any) -> None:
        """Log an informational message."""
        ...

    def error(self, msg: str, *args: Any) -> None:
        """Log an error message."""
        ...

    def debug(self, msg: str, *args: Any) -> None:
        """Log a debug message."""
        ...

    def warning(self, msg: str, *args: Any) -> None:
        """Log a warning message."""
        ...


class ConfigProtocol(Protocol):
    """Protocol for any configuration provider.

    Any object with a ``get(key, default)`` method satisfies the
    contract.
    """

    def get(self, key: str, default: Any = None) -> Any:
        """Return the value for *key*, or *default* if not found."""
        ...


class ProviderCredentialResolverProtocol(Protocol):
    """Protocol for centralized provider credential access."""

    def has_credentials(self, provider_id: str, connection_id: str | None = None) -> bool:
        """Return whether *provider_id* or *connection_id* has a non-empty credential."""
        ...

    def get_credentials(self, provider_id: str, connection_id: str | None = None) -> str:
        """Return the credential value for *provider_id* or *connection_id*."""
        ...

    def list_accounts(self, provider_id: str, local_connection_id: str) -> list[ProviderAccount]:
        """Return the connection's accounts, default first then sorted."""
        ...

    def resolve_account_id(
        self,
        provider_id: str,
        local_connection_id: str,
        account_id: str | None = None,
    ) -> str:
        """Resolve an explicit or implicit account id to a usable account."""
        ...


class RuntimeServices(Protocol):
    """Service surface of a started runtime, as consumed by core modules.

    Mirrors the service accessors :class:`core.runtime.runtime.Runtime`
    exposes after ``start()``. Modules that genuinely need the whole
    runtime handle (chat loop, sub-agent coordination) type their
    parameter with this protocol and access services directly — a missing
    attribute is a wiring bug, not a silently disabled feature.
    """

    @property
    def agents(self) -> AgentStore:
        """Persisted agent store."""
        ...

    @property
    def providers(self) -> ProviderRegistry:
        """Provider config registry."""
        ...

    @property
    def models(self) -> ModelRegistry:
        """Model catalog registry."""
        ...

    @property
    def provider_credentials(self) -> ProviderCredentialResolverProtocol:
        """Central provider credential resolution."""
        ...

    @property
    def storage(self) -> StorageManager:
        """Data-directory, settings, and prompt-fragment storage."""
        ...

    @property
    def chat_sessions(self) -> ChatSessionManager:
        """Persisted chat session manager."""
        ...

    @property
    def projects(self) -> ProjectStore:
        """Persisted project anchor store (cwd, default agent/model, sessions)."""
        ...

    @property
    def agent_resolver(self) -> AgentResolver:
        """Uniform ``(project_id | None, agent_id)`` → runtime-agent resolution."""
        ...

    @property
    def chat_run_manager(self) -> ChatRunManager:
        """Shared run lifecycle manager and busy-session queue."""
        ...

    @property
    def tools(self) -> ToolRegistry:
        """Runtime tool registry."""
        ...

    @property
    def skills(self) -> SkillRegistry:
        """Loaded skill metadata registry (the global/identity pool)."""
        ...

    def skills_for(self, project_id: str | None, agent_id: str | None = None) -> SkillRegistry:
        """Return the skill registry for a run, scoped to its project and agent.

        ``project_id``/``agent_id`` both ``None`` returns the global registry
        (identity runs, unchanged); a set ``project_id`` returns the project's
        merged registry (its own ``.opencode/skills`` first, then the bundled pool);
        an ``agent_id`` with a private skills home layers that on top (always-allowed
        for the owner). The single seam every run-time skill consumer resolves
        through.
        """
        ...

    def project_skill_names(self, project_id: str | None) -> frozenset[str]:
        """Return the names of a project's own scanned skills (empty for ``None``)."""
        ...

    def project_own_skills(self, project_id: str) -> list[SkillMetadata]:
        """Return a project's own scanned skills (name/description/path) for the visit reminder."""
        ...

    @property
    def extensions(self) -> ExtensionRegistry | None:
        """Loaded extension hooks, or ``None`` when none are loaded."""
        ...

    @property
    def system_prompts(self) -> SystemPromptManager:
        """System prompt assembly."""
        ...

    @property
    def process_manager(self) -> ProcessManager:
        """Shared host process lifecycle management."""
        ...

    @property
    def streaming_chat_loop(self) -> ChatLoop:
        """Canonical resolver-wired streaming chat loop."""
        ...

    def get_adapter(self, provider_id: str, connection_id: str) -> ProviderAdapter:
        """Build a wired provider adapter for one connection."""
        ...
