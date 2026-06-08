"""Embedding execution domain.

Exposes :class:`EmbeddingService` for the `text_embedding` task-model
binding and the provider HTTP client (:class:`ProviderEmbeddingClient`)
that the service routes to. The service is provider-agnostic — it
resolves the configured binding through :class:`core.model_tasks.TaskModelService`
and delegates wire requests to a provider client built from runtime
provider config and credentials.
"""

from core.embeddings.embeddings import (
    EmbeddingConfigurationError,
    EmbeddingError,
    EmbeddingExecutionError,
    EmbeddingResult,
    EmbeddingService,
    EmbeddingUnsupportedTargetError,
)
from core.embeddings.providers import ProviderEmbeddingClient

__all__ = [
    "EmbeddingConfigurationError",
    "EmbeddingError",
    "EmbeddingExecutionError",
    "EmbeddingResult",
    "EmbeddingService",
    "EmbeddingUnsupportedTargetError",
    "ProviderEmbeddingClient",
]
