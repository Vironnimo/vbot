"""Structured logging setup for vBot.

Provides a ``LogManager`` that creates per-module loggers with consistent
formatting and a shared stream handler.  Module names are automatically
prefixed with ``vbot.`` (e.g., ``vbot.core``, ``vbot.server``).
"""

import logging
from typing import Optional


class LogManager:
    """Creates and manages per-module structured loggers.

    Each logger returned by :meth:`get_logger` writes to stdout with a
    uniform format that includes a timestamp, log level, and module name.

    Usage::

        manager = LogManager(level="DEBUG")
        log = manager.get_logger("core")
        log.info("Runtime started")   # -> vbot.core
    """

    _FORMAT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"
    _DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

    def __init__(self, level: int | str = logging.INFO) -> None:
        """Initialise the log manager.

        Args:
            level: Default log level for all loggers created by this
                   manager.  May be an ``int`` (e.g. ``logging.DEBUG``)
                   or a level name string (e.g. ``"INFO"``).
        """
        self._level: int = self._resolve_level(level)
        self._handler: Optional[logging.Handler] = None
        self._loggers: dict[str, logging.Logger] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_logger(self, name: str) -> logging.Logger:
        """Return (or create) a logger for *name*.

        The internal logger name becomes ``vbot.<name>``.  Repeated calls
        with the same *name* return the same logger instance.

        Args:
            name: Module name **without** the ``vbot.`` prefix
                  (e.g. ``"core"``, ``"server"``).

        Returns:
            A configured :class:`logging.Logger`.
        """
        if name not in self._loggers:
            logger = logging.getLogger(f"vbot.{name}")
            logger.setLevel(self._level)
            logger.propagate = False
            logger.addHandler(self.handler)
            self._loggers[name] = logger
        return self._loggers[name]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def handler(self) -> logging.Handler:
        """Lazily create (once) the shared stream handler + formatter."""
        if self._handler is None:
            handler = logging.StreamHandler()
            handler.setLevel(self._level)
            handler.setFormatter(
                logging.Formatter(self._FORMAT, datefmt=self._DATE_FORMAT)
            )
            self._handler = handler
        return self._handler

    @staticmethod
    def _resolve_level(level: int | str) -> int:
        """Normalise a log level name (string) or int to an int."""
        if isinstance(level, int):
            return level
        return logging.getLevelName(level.upper())
