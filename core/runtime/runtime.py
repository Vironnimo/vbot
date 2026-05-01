"""vBot runtime bootstrap.

The ``Runtime`` class is the single entry point that wires together
all core services and manages the application lifecycle.
"""

from core.runtime.interfaces import ConfigProtocol, LoggerProtocol
from core.utils.logging import LogManager


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
        log_level = config.get("LOG_LEVEL", "INFO")
        self._log_manager = LogManager(level=log_level)
        self.logger: LoggerProtocol | None = None

    def start(self) -> None:
        """Start the runtime and initialise all services.

        Creates the ``vbot.core`` logger and signals that the
        application is ready.
        """
        self.logger = self._log_manager.get_logger("core")
        self.logger.info("Runtime started")

    def stop(self) -> None:
        """Gracefully shut down the runtime.

        Logs the shutdown event and performs cleanup.
        """
        if self.logger is not None:
            self.logger.info("Runtime stopped")
