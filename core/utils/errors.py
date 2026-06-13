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


class StorageError(VBotError):
    """Raised for invalid storage data or unsafe storage paths.

    Lives here (not in ``core/storage/``) because the settings domain's
    section normalizers raise it too; a storage-package home would force
    a settings -> storage import cycle. ``core.storage.errors`` re-exports
    it as the storage domain's canonical error.
    """


class ProviderError(VBotError):
    """Provider / API errors.

    Base class for all provider-related exceptions.  Carries a ``retryable``
    flag that the retry utility checks to decide whether to re-attempt the
    call.

    Subclasses like ``ProviderAuthError`` hard-code ``retryable`` to a fixed
    value; callers should not override it.

    ``retry_after`` carries the server-requested minimum wait before the next
    attempt, in seconds, when the response included a ``Retry-After`` (or
    ``retry-after-ms``) header on a retryable status. ``None`` means the
    response gave no such hint. ``retry_async`` honors it as a floor over its
    own exponential backoff.
    """

    retry_after: float | None = None

    def __init__(self, message: str = "", retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class TaskError(VBotError):
    """Common base for expected task-model execution errors.

    The specialized task domains (speech, image, embeddings) mirror the
    same error family: configuration missing, target unsupported, or
    provider execution failed. Their domain bases (``SpeechError``,
    ``ImageError``, ``EmbeddingError``) derive from this class so
    callers can treat "a task-model execution failed" uniformly without
    importing every domain.
    """


class EmbeddingError(TaskError):
    """Base class for expected embedding errors.

    Embedding execution sits between :class:`ImageError`-style provider
    isolation and :class:`core.model_tasks.TaskModelError` binding
    resolution: the binding lookup may be unconfigured, the parsed
    target may be local-only, or the provider request may fail. The
    :class:`EmbeddingService` raises domain-specific subclasses so
    callers (the recall backend, settings RPC, future tooling) can map
    them to the right error category without re-parsing the message.
    """
