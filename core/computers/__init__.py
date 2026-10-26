"""Connected computers, Session execution choices and native capability routing."""

from .computers import ComputerError, ComputerInfo, ComputerRegistry
from .policy import DEFAULT_EXECUTION, normalize_execution

__all__ = [
    "DEFAULT_EXECUTION",
    "ComputerError",
    "ComputerInfo",
    "ComputerRegistry",
    "normalize_execution",
]
