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

    Placeholder — will be expanded with provider-specific subclasses
    (rate limiting, authentication, timeout, etc.) in later phases.
    """
