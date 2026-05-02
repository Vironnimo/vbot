"""vBot runtime bootstrap.

The ``Runtime`` class is the single entry point that wires together
all core services and manages the application lifecycle.
"""

import os
from collections.abc import Callable
from pathlib import Path

from core.models.models import Model, ModelRegistry
from core.providers.adapter import ProviderAdapter
from core.providers.anthropic import AnthropicAdapter
from core.providers.openai_compatible import OpenAICompatibleAdapter
from core.providers.providers import ProviderConfig, ProviderRegistry
from core.runtime.interfaces import ConfigProtocol, LoggerProtocol
from core.utils.errors import ConfigError
from core.utils.logging import LogManager

# ---------------------------------------------------------------------------
# Project root / default resources directory
# ---------------------------------------------------------------------------

# Three directories up from this file (core/runtime/runtime.py) → project root.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_RESOURCES_DIR = _PROJECT_ROOT / "resources"

# ---------------------------------------------------------------------------
# Adapter factory mapping
# ---------------------------------------------------------------------------

_ADAPTER_MAP: dict[str, Callable[[ProviderConfig, str], ProviderAdapter]] = {
    "openai_compatible": OpenAICompatibleAdapter,
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
        log_level = config.get("LOG_LEVEL", "INFO")
        self._log_manager = LogManager(level=log_level)
        self.logger: LoggerProtocol | None = None
        self._started: bool = False
        self._providers: ProviderRegistry | None = None
        self._models: ModelRegistry | None = None

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

        self.logger = self._log_manager.get_logger("core")

        # Load provider and model registries from resources.
        resources_path_raw = self._config.get("RESOURCES_PATH")
        if resources_path_raw is not None:
            resources_path = Path(resources_path_raw)
        else:
            resources_path = _DEFAULT_RESOURCES_DIR

        self._providers = ProviderRegistry.load(resources_path)
        self._models = ModelRegistry.load(resources_path)

        self._started = True
        self.logger.info("Runtime started")

    def stop(self) -> None:
        """Gracefully shut down the runtime.

        Logs the shutdown event and performs cleanup.
        """
        if self.logger is not None:
            self.logger.info("Runtime stopped")
        self._started = False

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
        if self._providers is None:
            raise RuntimeError("Runtime not started — call start() first")
        return self._providers

    @property
    def models(self) -> ModelRegistry:
        """Read-only access to the model registry.

        Returns:
            The populated ``ModelRegistry``.

        Raises:
            RuntimeError: If the runtime has not been started.
        """
        if self._models is None:
            raise RuntimeError("Runtime not started — call start() first")
        return self._models

    # ------------------------------------------------------------------
    # Adapter factory
    # ------------------------------------------------------------------

    def get_adapter(self, provider_id: str) -> ProviderAdapter:
        """Return a wired adapter instance for the given provider.

        Looks up the provider config from the registry, resolves the
        API key from the environment using the provider's ``env_key``,
        and instantiates the correct adapter class.

        Args:
            provider_id: Unique provider identifier (e.g. ``"openai"``).

        Returns:
            A ``ProviderAdapter`` instance ready to make API calls.

        Raises:
            RuntimeError: If the runtime has not been started.
            KeyError: If no provider with *provider_id* is registered.
            ConfigError: If the API key is not set in the environment,
                or if the adapter type is unknown.
        """
        if not self._started:
            raise RuntimeError("Runtime not started — call start() first")

        provider_config = self.providers.get(provider_id)

        api_key = os.environ.get(provider_config.auth.env_key, "")
        if not api_key:
            raise ConfigError(
                f"API key not found for provider '{provider_id}': "
                f"environment variable '{provider_config.auth.env_key}' is not set"
            )

        adapter_class = _ADAPTER_MAP.get(provider_config.adapter)
        if adapter_class is None:
            raise ConfigError(
                f"Unknown adapter type '{provider_config.adapter}' for provider '{provider_id}'"
            )

        return adapter_class(provider_config, api_key)

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
