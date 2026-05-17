"""Provider-specific exception classes.

Domain-specific provider exceptions and related network error classes.

Provider subclasses hard-code their ``retryable`` flags so that the retry
utility can decide whether to re-attempt the call.
"""

from core.utils.errors import ProviderError, VBotError


class NetworkError(VBotError):
    """Network-level error (dropped connection, DNS failure, etc.).

    Not a subclass of ProviderError - a network error is not provider-specific
    and must not trigger model fallback. Retryable - the user can retry once
    connectivity is restored.
    """

    retryable: bool = True


class ProviderAuthError(ProviderError):
    """Authentication or authorization error (HTTP 401 / 403).

    Not retryable — the request will fail again until credentials change.
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(message, retryable=False)


class ProviderRateLimitError(ProviderError):
    """Rate-limit error (HTTP 429).

    Retryable — the server is throttling requests and will accept them later.
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(message, retryable=True)


class ProviderTimeoutError(ProviderError):
    """Connection or read timeout.

    Retryable — transient network issues may resolve on retry.
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(message, retryable=True)
