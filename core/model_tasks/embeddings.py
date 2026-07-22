"""Provider-neutral embedding execution service.

``EmbeddingService`` is the public surface used by the recall backend
(M4) and any other domain that needs vectors. It resolves the
configured ``text_embedding`` binding through
:class:`core.model_tasks.TaskModelService`, merges stored options over
the backend schema defaults, parses the target into a provider
reference, and routes to :class:`ProviderEmbeddingClient`.

Embedding execution is provider-agnostic: the binding is the only
input the service takes. Local targets are out of scope for this
iteration (the spec keeps the local-target hook dependency-free, the
same way local speech/image engines stay optional). A configured
local target raises :class:`EmbeddingUnsupportedTargetError` so
callers (the recall backend in particular) can fall back to JSONL
with a logged warning — mirroring the recall backend's pattern for
missing bindings.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from core.model_tasks.constants import TASK_TEXT_EMBEDDING
from core.model_tasks.embeddings_providers import ProviderEmbeddingClient
from core.model_tasks.task_execution import TaskBindingResolver
from core.providers.task_client import TaskClientRuntime
from core.utils.errors import EmbeddingError as _BaseEmbeddingError
from core.utils.errors import VBotError
from core.utils.logging import get_logger

JsonObject = dict[str, Any]
_LOGGER = get_logger("embeddings")
_EMBEDDING_SPACE_CONTRACT_VERSION = 1


class EmbeddingError(_BaseEmbeddingError):
    """Base class for expected embedding errors."""


class EmbeddingConfigurationError(EmbeddingError):
    """Raised when embedding execution is not configured."""


class EmbeddingUnsupportedTargetError(EmbeddingError):
    """Raised when a configured embedding target has no execution adapter."""


class EmbeddingExecutionError(EmbeddingError):
    """Raised when a provider embedding request fails."""


@dataclass(frozen=True)
class EmbeddingSpaceIdentity:
    """Stable identity for one configured embedding execution space."""

    provider_id: str
    model_id: str
    fingerprint: str


@dataclass(frozen=True)
class EmbeddingResult:
    """Normalized embedding response.

    Attributes:
        vectors: One vector per input, in input order.
        model_id: Provider-side model id that produced the vectors.
        provider_id: Provider id from the resolved binding — the
            recall store pins ``(provider_id, model_id, dimension)``
            together so a binding switch triggers a rebuild.
        dimension: ``len(vectors[0])`` when at least one vector was
            returned. ``0`` when *vectors* is empty (the runtime never
            produces an empty batch, but we keep the field valid for
            defensiveness).
        space_fingerprint: Stable identity of the target, Connection/Account,
            effective options, and embedding wire contract used for the call.
    """

    vectors: tuple[list[float], ...]
    model_id: str
    provider_id: str
    dimension: int
    space_fingerprint: str = ""

    @property
    def resolved_model_id(self) -> tuple[str, str]:
        """``(provider_id, model_id)`` tuple for identity pinning."""

        return (self.provider_id, self.model_id)


class EmbeddingService:
    """Execute text embeddings through the configured task-model binding."""

    def __init__(
        self,
        model_tasks: Any,
        runtime: TaskClientRuntime,
    ) -> None:
        self._runtime = runtime
        self._resolver = TaskBindingResolver(
            model_tasks, configuration_error=EmbeddingConfigurationError
        )

    async def embed(self, texts: list[str]) -> EmbeddingResult:
        """Embed a batch of texts using the configured binding.

        Raises:
            EmbeddingConfigurationError: No ``text_embedding`` binding
                is configured, the target is malformed, or the input
                list is empty.
            EmbeddingUnsupportedTargetError: The configured binding
                targets a local engine. Local embedding engines are
                deliberately out of scope for this iteration.
            EmbeddingExecutionError: The provider request failed for
                any other reason (network, auth, schema).
        """

        if not isinstance(texts, list) or not texts:
            raise EmbeddingConfigurationError("Embedding input must be a non-empty list of strings")
        for index, text in enumerate(texts):
            if not isinstance(text, str):
                raise EmbeddingConfigurationError(
                    f"Embedding input at index {index} is not a string"
                )

        _binding, options, target_ref = self._resolver.resolve(TASK_TEXT_EMBEDDING)

        if target_ref.kind == "local":
            raise EmbeddingUnsupportedTargetError(
                f"Embedding does not support local targets: {target_ref.target}"
            )

        identity = self._space_identity(target_ref, options)
        provider_client = ProviderEmbeddingClient.from_runtime(self._runtime, target_ref)
        try:
            vectors = await provider_client.embed(list(texts), options=options)
        except EmbeddingError:
            raise
        except VBotError as exc:
            # ProviderError / NetworkError / ProviderAuthError / …
            # are expected provider failures and surface as execution failures.
            _LOGGER.warning(
                "Embedding request failed for provider=%s model=%s: %s",
                target_ref.provider_id,
                target_ref.model_id,
                exc,
            )
            raise EmbeddingExecutionError(str(exc)) from exc
        except Exception as exc:
            _LOGGER.error(
                "Embedding request raised unexpected error for provider=%s model=%s",
                target_ref.provider_id,
                target_ref.model_id,
                exc_info=True,
            )
            raise EmbeddingExecutionError(str(exc)) from exc

        if not vectors:
            raise EmbeddingExecutionError(
                f"Embedding provider returned no vectors for model {target_ref.model_id}"
            )

        dimension = len(vectors[0])
        return EmbeddingResult(
            vectors=tuple(vectors),
            model_id=target_ref.model_id,
            provider_id=target_ref.provider_id,
            dimension=dimension,
            space_fingerprint=identity.fingerprint,
        )

    def resolve_space(self) -> EmbeddingSpaceIdentity:
        """Return the full configured embedding-space identity without executing it.

        The fingerprint covers the normalized target (including connection and
        account), the effective options after schema defaults, and this wire
        contract version. Recall uses it to reject vectors produced by any
        materially different execution configuration, even when provider and
        model ids stay unchanged.
        """

        _binding, options, target_ref = self._resolver.resolve(TASK_TEXT_EMBEDDING)
        if target_ref.kind == "local":
            raise EmbeddingUnsupportedTargetError(
                f"Embedding does not support local targets: {target_ref.target}"
            )
        return self._space_identity(target_ref, options)

    def resolve_model_id(self) -> tuple[str, str]:
        """Return ``(provider_id, model_id)`` for the configured binding.

        Compatibility projection of :meth:`resolve_space`; new callers that
        persist vectors must use the full fingerprint. It raises the same
        errors as :meth:`embed` for unconfigured or malformed bindings, but
        never executes a request.
        """

        identity = self.resolve_space()
        return (identity.provider_id, identity.model_id)

    @staticmethod
    def _space_identity(target_ref: Any, options: JsonObject) -> EmbeddingSpaceIdentity:
        try:
            encoded = json.dumps(
                {
                    "contract_version": _EMBEDDING_SPACE_CONTRACT_VERSION,
                    "target": target_ref.target,
                    "options": options,
                },
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise EmbeddingConfigurationError(
                f"Embedding options cannot form a stable execution identity: {error}"
            ) from error
        return EmbeddingSpaceIdentity(
            provider_id=target_ref.provider_id,
            model_id=target_ref.model_id,
            fingerprint=hashlib.sha256(encoded).hexdigest(),
        )
