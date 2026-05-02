"""vBot exception hierarchy.

Base classes for all vBot-specific errors.
"""


class VBotError(Exception):
    """Base exception for all vBot errors."""


class ConfigError(VBotError):
    """Configuration-related errors.

    Raised for missing keys, invalid values, malformed config files,
    or any other configuration problem.
    """


class ProviderError(VBotError):
    """Provider / API errors.

    Base class for all provider-related exceptions.  Carries a ``retryable``
    flag that the retry utility checks to decide whether to re-attempt the
    call.

    Subclasses like ``ProviderAuthError`` hard-code ``retryable`` to a fixed
    value; callers should not override it.
    """

    def __init__(self, message: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
