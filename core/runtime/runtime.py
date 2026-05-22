"""vBot runtime bootstrap.

The ``Runtime`` class is the single entry point that wires together
all core services and manages the application lifecycle.
"""

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from core.agents.agents import AgentStore, SkillPromptRegistry, SystemPromptManager
from core.attachments import AttachmentStore
from core.automation import CronService, TriggerService
from core.channels import ChannelService
from core.chat import ChatLoop, ChatRunManager, CommandDispatcher
from core.chat.block_resolver import ContentBlockResolver
from core.chat.chat import ChatSessionManager
from core.extensions import ExtensionRegistry
from core.models.models import Model, ModelRegistry
from core.providers.adapter import ModelLookup, ProviderAdapter
from core.providers.anthropic import AnthropicAdapter
from core.providers.credentials import ProviderCredentialResolver
from core.providers.github_copilot import GitHubCopilotAdapter
from core.providers.mistral import MistralAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.opencode_go import OpenCodeGoAdapter
from core.providers.openrouter import OpenRouterAdapter
from core.providers.providers import ConnectionConfig, ProviderConfig, ProviderRegistry
from core.providers.token_getter import OAuthTokenGetter, StaticTokenGetter, TokenGetter
from core.providers.token_store import TokenStore
from core.runtime.interfaces import (
    ConfigProtocol,
    LoggerProtocol,
    ProviderCredentialResolverProtocol,
)
from core.skills.skills import SkillRegistry
from core.storage.storage import StorageManager
from core.tools import (
    register_bash_tool,
    register_edit_tool,
    register_glob_tool,
    register_grep_tool,
    register_process_tool,
    register_read_tool,
    register_skill_tool,
    register_web_fetch_tool,
    register_web_search_tool,
    register_write_tool,
)
from core.tools.cron import register_cron_tool
from core.tools.process_manager import ProcessManager
from core.tools.status import register_status_tool
from core.tools.subagent import SubAgentBatchTracker, register_subagent_tools
from core.tools.tools import ToolRegistry
from core.utils.errors import ConfigError
from core.utils.logging import LogManager

# ---------------------------------------------------------------------------
# Project root / default resources directory
# ---------------------------------------------------------------------------

# Three directories up from this file (core/runtime/runtime.py) → project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_RESOURCES_DIR = _PROJECT_ROOT / "resources"
_DEFAULT_APP_VERSION = "0.1.0"
_DEFAULT_ATTACHMENT_MAX_SIZE_BYTES = 20_971_520

# ---------------------------------------------------------------------------
# Adapter factory mapping
# ---------------------------------------------------------------------------

_ADAPTER_MAP: dict[
    str,
    type[ProviderAdapter],
] = {
    "openai_compatible": OpenAICompatibleAdapter,
    "openrouter": OpenRouterAdapter,
    "mistral": MistralAdapter,
    "opencode_go": OpenCodeGoAdapter,
    "github_copilot": GitHubCopilotAdapter,
    "anthropic": AnthropicAdapter,
}


class Runtime:
    """Bootstraps and manages the vBot application lifecycle.

    Constructor injection via :class:`ConfigProtocol` keeps the
    runtime decoupled from any concrete configuration implementation.

    Usage::

        from core.runtime.runtime import Runtime
        from core.utils.config import Config

        runtime = Runtime(Config())
        runtime.start()
        # ... application runs ...
        runtime.stop()
    """

    def __init__(self, config: ConfigProtocol) -> None:
        """Initialise the runtime with injected configuration.

        Creates the core services (currently only ``LogManager``)
        using settings from *config*.

        Args:
            config: Any object satisfying :class:`ConfigProtocol`.
        """
        self._config: ConfigProtocol = config
        self._data_dir = self._resolve_data_dir()
        self._fallback_environment: dict[str, str] = {}
        log_level = config.get("LOG_LEVEL", "INFO")
        self._log_manager = LogManager(level=log_level, data_dir=self._data_dir)
        self.logger: LoggerProtocol | None = None
        self._started: bool = False
        self._providers: ProviderRegistry | None = None
        self._provider_credentials: ProviderCredentialResolverProtocol | None = None
        self._token_store: TokenStore | None = None
        self._models: ModelRegistry | None = None
        self._storage: StorageManager | None = None
        self._attachment_store: AttachmentStore | None = None
        self._agents: AgentStore | None = None
        self._tools: ToolRegistry | None = None
        self._process_manager: ProcessManager | None = None
        self._skills: SkillRegistry | None = None
        self._extensions: ExtensionRegistry | None = None
        self._chat_sessions: ChatSessionManager | None = None
        self._chat_run_manager: ChatRunManager | None = None
        self._command_dispatcher: CommandDispatcher | None = None
        self.chat_runs: ChatRunManager | None = None
        self._chat_loop: ChatLoop | None = None
        self._streaming_chat_loop: ChatLoop | None = None
        self._started_at: datetime | None = None
        self._trigger_service: TriggerService | None = None
        self._channel_service: ChannelService | None = None
        self._cron_service: CronService | None = None
        self._subagent_batch_tracker: SubAgentBatchTracker | None = None
        self._system_prompts: SystemPromptManager | None = None

    def start(self) -> None:
        """Start the runtime and initialise all services.

        Creates the ``vbot.core`` logger, loads provider and model
        registries from the resources directory, and signals that the
        application is ready.  Idempotent — calling ``start()``
        more than once is a no-op (logged at debug level).
        """
        if self._started:
            logger = self._log_manager.get_logger("core")
            logger.debug("Runtime already started — skipping")
            return

        self._started_at = datetime.now(UTC)
        self.logger = self._log_manager.get_logger("core")
        self.logger.info("Runtime startup initiated")

        resources_path = self._resolve_resources_path()

        self._storage = StorageManager(config=self._config, resources_dir=resources_path)
        storage = self._storage
        if storage is None:
            raise RuntimeError("Storage service not available")
        self._storage.ensure_directories()
        settings = self._storage.load_settings()
        attachment_max_size_bytes = self._attachment_max_size_bytes(settings)
        self._attachment_store = AttachmentStore(
            self._storage.data_dir,
            max_size_bytes=attachment_max_size_bytes,
        )
        data_dir_credentials = self._storage.load_environment()
        self._fallback_environment = dict(data_dir_credentials)
        self._storage.copy_prompt_fragments()

        self._providers = ProviderRegistry.load(resources_path)
        self._token_store = TokenStore(self._storage.data_dir)
        self._provider_credentials = ProviderCredentialResolver(
            self._providers,
            fallback_credentials=data_dir_credentials,
            token_store=self._token_store,
        )
        self._models = ModelRegistry.load(resources_path)
        self._agents = AgentStore(
            self._storage.data_dir,
            template_dir=resources_path / "workspace-templates",
            defaults_provider=lambda: storage.load_defaults().get("agent", {}),
        )
        self._process_manager = ProcessManager()
        self._start_process_manager()
        self._tools = ToolRegistry()
        register_read_tool(self._tools)
        register_edit_tool(self._tools)
        register_glob_tool(self._tools)
        register_grep_tool(self._tools)
        register_write_tool(self._tools)
        register_web_fetch_tool(self._tools)
        register_web_search_tool(self._tools, self.resolve_environment_credential)
        register_process_tool(self._tools, self._process_manager)
        skill_directories = [resources_path / "skills", *self._extra_skill_directories(settings)]
        self._skills = SkillRegistry.load(
            self._storage.data_dir / "skills",
            extra_dirs=skill_directories,
        )
        invalid_skill_count = len(self._skills.invalid_diagnostics())
        if invalid_skill_count > 0:
            self.logger.warning(
                "Loaded skills with %s invalid skill directories; "
                "see vbot.skills warnings for details",
                invalid_skill_count,
            )
        register_skill_tool(self._tools, self._skills)
        extension_dirs = self._extra_extension_directories(settings)
        self._extensions = ExtensionRegistry.load(
            self._storage.data_dir / "extensions",
            extra_dirs=extension_dirs,
        )
        self._chat_sessions = ChatSessionManager(self._storage.data_dir)
        self._chat_run_manager = ChatRunManager()
        self._command_dispatcher = CommandDispatcher(
            self._chat_run_manager,
            agents=self._agents,
            sessions=self._chat_sessions,
            models=self._models,
            started_at=self._started_at,
        )
        self.chat_runs = self._chat_run_manager
        if self._attachment_store is None:
            raise RuntimeError("Attachment store not available")
        resolver = ContentBlockResolver(self._attachment_store)
        self._chat_loop = ChatLoop(self, streaming=False, attachment_resolver=resolver)
        self._streaming_chat_loop = ChatLoop(self, streaming=True, attachment_resolver=resolver)
        self._trigger_service = TriggerService(self._chat_loop, self._chat_run_manager, self)
        self._channel_service = ChannelService(
            self._trigger_service,
            self._chat_sessions,
            self,
            attachment_store=self._attachment_store,
            command_dispatcher=self._command_dispatcher,
        )
        self._channel_service._notify_tool_registration_changed_hook = (
            self._reload_channel_tool_if_started
        )
        self._start_channel_service()
        self._sync_channel_tool_registration()
        self._cron_service = CronService(self._trigger_service, self._storage.data_dir)
        self._start_cron_service()
        register_cron_tool(self._tools, self._cron_service)
        register_bash_tool(self._tools, self._process_manager, self._trigger_service)
        self._subagent_batch_tracker = SubAgentBatchTracker(self._trigger_service)
        register_subagent_tools(
            self._tools,
            self,
            self._trigger_service,
            self._subagent_batch_tracker,
        )
        register_status_tool(
            self._tools,
            self._agents,
            self._chat_sessions,
            self._models,
            self._started_at,
        )
        self._ensure_bootstrap_agent()
        self._system_prompts = SystemPromptManager(
            self._storage,
            self._tools,
            cast(SkillPromptRegistry, self._skills),
            channel_registry=cast(ChannelService, self._channel_service),
            app_version=str(self._config.get("APP_VERSION", _DEFAULT_APP_VERSION)),
            app_dir=_PROJECT_ROOT,
            data_root=self._storage.data_dir,
        )

        self._started = True
        self.logger.info("Runtime started")

    def stop(self) -> None:
        """Gracefully shut down the runtime.

        Logs the shutdown event and performs cleanup.
        """
        if self.logger is not None:
            self.logger.info("Runtime stopped")
        self._log_manager.close()
        self._started = False
        self._providers = None
        self._provider_credentials = None
        self._token_store = None
        self._fallback_environment = {}
        self._models = None
        self._storage = None
        self._attachment_store = None
        self._agents = None
        self._tools = None
        if self._process_manager is not None:
            self._process_manager.stop()
        self._process_manager = None
        self._skills = None
        self._extensions = None
        self._chat_sessions = None
        if self._channel_service is not None:
            self._channel_service.stop()
        self._channel_service = None
        if self._cron_service is not None:
            self._cron_service.stop()
        self._cron_service = None
        self._trigger_service = None
        self._subagent_batch_tracker = None
        self._chat_loop = None
        self._streaming_chat_loop = None
        self._command_dispatcher = None
        self._chat_run_manager = None
        self.chat_runs = None
        self._system_prompts = None

    def _resolve_resources_path(self) -> Path:
        resources_path_raw = self._config.get("RESOURCES_PATH")
        if resources_path_raw is not None:
            return Path(resources_path_raw)
        return _DEFAULT_RESOURCES_DIR

    def _resolve_data_dir(self) -> Path:
        data_dir_raw = self._config.get("DATA_DIR") or self._config.get("VBOT_DATA_DIR")
        if data_dir_raw:
            return Path(cast(str, data_dir_raw)).expanduser()
        if hasattr(self._config, "data_dir"):
            return Path(cast(Any, self._config).data_dir).expanduser()
        raise ConfigError("Runtime requires a data directory to initialize logging")

    def _ensure_bootstrap_agent(self) -> None:
        if self._agents is None:
            raise RuntimeError("Agent service not available")
        if not self._agents.list():
            self._agents.create("main", "Main")

    def _attachment_max_size_bytes(self, settings: dict[str, object]) -> int:
        raw_limit = settings.get("attachment_max_size_bytes", _DEFAULT_ATTACHMENT_MAX_SIZE_BYTES)
        if isinstance(raw_limit, int) and not isinstance(raw_limit, bool) and raw_limit > 0:
            return raw_limit
        if self.logger is not None:
            self.logger.warning(
                "settings.attachment_max_size_bytes must be a positive integer; using default %s",
                _DEFAULT_ATTACHMENT_MAX_SIZE_BYTES,
            )
        return _DEFAULT_ATTACHMENT_MAX_SIZE_BYTES

    def _extra_skill_directories(self, settings: dict[str, object]) -> list[Path]:
        raw_directories = settings.get("skill_directories", [])
        if not isinstance(raw_directories, list):
            if self.logger is not None:
                cast(Any, self.logger).warning(
                    "settings.skill_directories must be a list; ignoring value"
                )
            return []

        directories: list[Path] = []
        for raw_directory in raw_directories:
            if not isinstance(raw_directory, str) or not raw_directory.strip():
                if self.logger is not None:
                    cast(Any, self.logger).warning(
                        "Ignoring invalid skill directory setting: %r", raw_directory
                    )
                continue
            directories.append(Path(raw_directory).expanduser())
        return directories

    def _extra_extension_directories(self, settings: dict[str, object]) -> list[Path]:
        raw_directories = settings.get("extension_directories", [])
        if not isinstance(raw_directories, list):
            if self.logger is not None:
                cast(Any, self.logger).warning(
                    "settings.extension_directories must be a list; ignoring value"
                )
            return []

        directories: list[Path] = []
        for raw_directory in raw_directories:
            if not isinstance(raw_directory, str) or not raw_directory.strip():
                if self.logger is not None:
                    cast(Any, self.logger).warning(
                        "Ignoring invalid extension directory setting: %r", raw_directory
                    )
                continue
            directories.append(Path(raw_directory).expanduser())
        return directories

    def _start_process_manager(self) -> None:
        if self._process_manager is None:
            raise RuntimeError("Process manager service not available")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._process_manager.start()

    def _start_cron_service(self) -> None:
        if self._cron_service is None:
            raise RuntimeError("Cron service not available")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._cron_service.start()

    def _start_channel_service(self) -> None:
        if self._channel_service is None:
            raise RuntimeError("Channel service not available")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._channel_service.start()

    def resolve_environment_credential(self, key: str) -> str:
        """Resolve one environment credential using runtime precedence rules."""
        if key in os.environ:
            return os.environ[key]
        return self._fallback_environment.get(key, "")

    def _reload_channel_tool_if_started(self) -> None:
        if not self._started:
            return
        self.reload_channel_tool()

    def _sync_channel_tool_registration(self) -> None:
        if self._tools is None:
            raise RuntimeError("Tool service not available")
        if self._channel_service is None:
            raise RuntimeError("Channel service not available")
        if self._chat_sessions is None:
            raise RuntimeError("Chat session service not available")

        self._tools.unregister("channel_send")
        if not self._channel_service.has_active_channels():
            return

        try:
            from core.tools.channel import register_channel_send_tool
        except ModuleNotFoundError as error:
            raise RuntimeError("Channel tool registration is unavailable") from error

        register_channel_send_tool(self._tools, self._channel_service, self._chat_sessions)

    def reload_channel_tool(self) -> None:
        """Re-register channel_send based on current active channel adapters."""
        self._ensure_started()
        self._sync_channel_tool_registration()

    def reload_skills(self) -> None:
        """Reload the runtime skill registry from current persisted settings."""
        self._ensure_started()
        settings = self.storage.load_settings()
        resources_path = self._resolve_resources_path()
        skill_directories = [resources_path / "skills", *self._extra_skill_directories(settings)]
        self._skills = SkillRegistry.load(
            self.storage.data_dir / "skills",
            extra_dirs=skill_directories,
        )
        invalid_skill_count = len(self._skills.invalid_diagnostics())
        if self.logger is not None:
            self.logger.info("Reloaded skill registry")
            if invalid_skill_count > 0:
                self.logger.warning(
                    "Reloaded skills with %s invalid skill directories; "
                    "see vbot.skills warnings for details",
                    invalid_skill_count,
                )
        if self._tools is not None:
            self._tools.unregister("skill")
            register_skill_tool(self._tools, self._skills)
        if self._system_prompts is not None:
            self._system_prompts.update_skill_registry(cast(SkillPromptRegistry, self._skills))

    # ------------------------------------------------------------------
    # Read-only registry access
    # ------------------------------------------------------------------

    @property
    def providers(self) -> ProviderRegistry:
        """Read-only access to the provider registry.

        Returns:
            The populated ``ProviderRegistry``.

        Raises:
            RuntimeError: If the runtime has not been started.
        """
        self._ensure_started()
        if self._providers is None:
            raise RuntimeError("Provider registry not available")
        return self._providers

    @property
    def models(self) -> ModelRegistry:
        """Read-only access to the model registry.

        Returns:
            The populated ``ModelRegistry``.

        Raises:
            RuntimeError: If the runtime has not been started.
        """
        self._ensure_started()
        if self._models is None:
            raise RuntimeError("Model registry not available")
        return self._models

    @property
    def provider_credentials(self) -> ProviderCredentialResolverProtocol:
        """Access to centralized provider credential resolution."""
        self._ensure_started()
        if self._provider_credentials is None:
            raise RuntimeError("Provider credential service not available")
        return self._provider_credentials

    @property
    def token_store(self) -> TokenStore:
        """Access to persisted OAuth provider tokens."""
        self._ensure_started()
        if self._token_store is None:
            raise RuntimeError("Token store not available")
        return self._token_store

    @property
    def storage(self) -> StorageManager:
        """Access to data-directory and prompt-fragment storage."""
        self._ensure_started()
        if self._storage is None:
            raise RuntimeError("Storage service not available")
        return self._storage

    @property
    def attachment_store(self) -> AttachmentStore:
        """Access to persisted blob attachment storage."""
        self._ensure_started()
        if self._attachment_store is None:
            raise RuntimeError("Attachment store not available")
        return self._attachment_store

    @property
    def agents(self) -> AgentStore:
        """Access to persisted agent CRUD and workspace lifecycle."""
        self._ensure_started()
        if self._agents is None:
            raise RuntimeError("Agent service not available")
        return self._agents

    @property
    def tools(self) -> ToolRegistry:
        """Access to the runtime tool registry."""
        self._ensure_started()
        if self._tools is None:
            raise RuntimeError("Tool service not available")
        return self._tools

    @property
    def process_manager(self) -> ProcessManager:
        """Access to shared host process lifecycle management."""
        self._ensure_started()
        if self._process_manager is None:
            raise RuntimeError("Process manager service not available")
        return self._process_manager

    @property
    def skills(self) -> SkillRegistry:
        """Access to local skill prompt metadata."""
        self._ensure_started()
        if self._skills is None:
            raise RuntimeError("Skill service not available")
        return self._skills

    @property
    def extensions(self) -> ExtensionRegistry | None:
        return self._extensions

    @property
    def chat_sessions(self) -> ChatSessionManager:
        """Access to agent chat session files."""
        self._ensure_started()
        if self._chat_sessions is None:
            raise RuntimeError("Chat session service not available")
        return self._chat_sessions

    @property
    def chat_run_manager(self) -> ChatRunManager:
        """Access to shared chat run lifecycle management."""
        self._ensure_started()
        if self._chat_run_manager is None:
            raise RuntimeError("Chat run manager service not available")
        return self._chat_run_manager

    @property
    def command_dispatcher(self) -> CommandDispatcher:
        """Access to built-in slash command dispatch for chat entry points."""
        self._ensure_started()
        if self._command_dispatcher is None:
            raise RuntimeError("Command dispatcher service not available")
        return self._command_dispatcher

    @property
    def trigger_service(self) -> TriggerService:
        """Access to programmatic run triggering."""
        self._ensure_started()
        if self._trigger_service is None:
            raise RuntimeError("Trigger service not available")
        return self._trigger_service

    @property
    def streaming_chat_loop(self) -> ChatLoop:
        """Access to the resolver-wired streaming chat loop."""
        self._ensure_started()
        if self._streaming_chat_loop is None:
            raise RuntimeError("Streaming chat loop is not available")
        return self._streaming_chat_loop

    @property
    def channel_service(self) -> ChannelService:
        """Access to channel config management and adapter lifecycle."""
        self._ensure_started()
        if self._channel_service is None:
            raise RuntimeError("Channel service not available")
        return self._channel_service

    @property
    def cron_service(self) -> CronService:
        """Access to persisted cron scheduling and job execution."""
        self._ensure_started()
        if self._cron_service is None:
            raise RuntimeError("Cron service not available")
        return self._cron_service

    @property
    def system_prompts(self) -> SystemPromptManager:
        """Access to system prompt assembly."""
        self._ensure_started()
        if self._system_prompts is None:
            raise RuntimeError("System prompt service not available")
        return self._system_prompts

    # ------------------------------------------------------------------
    # Adapter factory
    # ------------------------------------------------------------------

    def get_adapter(self, provider_id: str, connection_id: str) -> ProviderAdapter:
        """Return a wired adapter instance for the given provider.

        Looks up the provider config from the registry, resolves the
        provider credential through the runtime's central credential
        resolver, and instantiates the correct adapter class.

        Args:
            provider_id: Unique provider identifier (e.g. ``"openai"``).
            connection_id: Compositional connection identifier
                (e.g. ``"openai:api-key"``).

        Returns:
            A ``ProviderAdapter`` instance ready to make API calls.

        Raises:
            RuntimeError: If the runtime has not been started.
            KeyError: If no provider with *provider_id* is registered.
            ConfigError: If the provider credential is not configured,
                or if the adapter type is unknown.
        """
        if not self._started:
            raise RuntimeError("Runtime not started — call start() first")

        provider_config = self.providers.get(provider_id)
        connection = self._get_connection_config(provider_config, connection_id)
        token_getter = self._get_token_getter(provider_id, connection_id, connection)

        adapter_class = _ADAPTER_MAP.get(provider_config.adapter)
        if adapter_class is None:
            raise ConfigError(
                f"Unknown adapter type '{provider_config.adapter}' for provider '{provider_id}'"
            )

        adapter = cast(Any, adapter_class)(
            provider_config,
            token_getter,
            connection.base_url,
            connection.auth,
            model_lookup=self._model_lookup_for(provider_id),
        )
        return cast(ProviderAdapter, adapter)

    def _model_lookup_for(self, provider_id: str) -> ModelLookup:
        def _lookup(model_id: str) -> Model | None:
            try:
                return self.models.get(provider_id, model_id)
            except KeyError:
                return None

        return _lookup

    def _get_token_getter(
        self,
        provider_id: str,
        connection_id: str,
        connection: ConnectionConfig,
    ) -> TokenGetter:
        if connection.type == "api_key":
            raw_token = self.provider_credentials.get_credentials(provider_id, connection_id)
            return StaticTokenGetter(raw_token)
        if connection.type == "oauth":
            if connection.oauth is None:
                # OAuth stubs with a credential_key still resolve through the
                # central credential path until they get token-store metadata.
                raw_token = self.provider_credentials.get_credentials(provider_id, connection_id)
                return StaticTokenGetter(raw_token)
            return OAuthTokenGetter(self.token_store, provider_id, connection.id, connection.oauth)
        raise ConfigError(
            f"Unknown connection type '{connection.type}' for provider '{provider_id}' "
            f"connection '{connection.id}'"
        )

    def _get_connection_config(
        self,
        provider_config: ProviderConfig,
        connection_id: str,
    ) -> ConnectionConfig:
        parts = connection_id.split(":", 1)
        if len(parts) != 2 or parts[0] != provider_config.id or not parts[1]:
            raise ConfigError(
                f"Unknown connection id '{connection_id}' for provider '{provider_config.id}'"
            )
        try:
            return provider_config.get_connection(parts[1])
        except KeyError as error:
            raise ConfigError(
                f"Unknown connection id '{connection_id}' for provider '{provider_config.id}'"
            ) from error

    def has_provider_credentials(self, provider_id: str) -> bool:
        """Return whether *provider_id* has usable configured credentials."""

        if not self._started:
            raise RuntimeError("Runtime not started — call start() first")

        return self.provider_credentials.has_credentials(provider_id)

    def get_provider_credentials(self, provider_id: str) -> str:
        """Return the configured credential value for *provider_id*."""

        if not self._started:
            raise RuntimeError("Runtime not started — call start() first")

        return self.provider_credentials.get_credentials(provider_id)

    # ------------------------------------------------------------------
    # Model lookup convenience
    # ------------------------------------------------------------------

    def get_model(self, provider_id: str, model_id: str) -> Model:
        """Look up a model by provider ID and model ID.

        Convenience method that delegates to
        :meth:`ModelRegistry.get`.

        Args:
            provider_id: The provider identifier (e.g. ``"openai"``).
            model_id: The exact model ID sent in API requests.

        Returns:
            The matching :class:`Model` entry.

        Raises:
            RuntimeError: If the runtime has not been started.
            KeyError: If no model matches the given provider and model ID.
        """
        if not self._started:
            raise RuntimeError("Runtime not started — call start() first")

        return self.models.get(provider_id, model_id)

    def _ensure_started(self) -> None:
        if not self._started:
            raise RuntimeError("Runtime not started — call start() first")
