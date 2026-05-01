"""Protocol interfaces for the vBot runtime.

Defines typing.Protocol contracts that enable constructor-injection
and testability without dragging in concrete implementations.
"""

from typing import Any, Protocol


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
