"""Task-model bindings, discovery, and task execution (speech, image, embeddings)."""

from core.model_tasks.constants import (
    SPEECH_TASK_TYPES,
    SUPPORTED_TASK_TYPES,
    TASK_IMAGE_GENERATION,
    TASK_SPEECH_TO_TEXT,
    TASK_TEXT_EMBEDDING,
    TASK_TEXT_TO_SPEECH,
    TASK_VIDEO_GENERATION,
)
from core.model_tasks.embeddings import (
    EmbeddingConfigurationError,
    EmbeddingError,
    EmbeddingExecutionError,
    EmbeddingResult,
    EmbeddingService,
    EmbeddingUnsupportedTargetError,
)
from core.model_tasks.embeddings_providers import ProviderEmbeddingClient
from core.model_tasks.image import (
    ImageConfigurationError,
    ImageError,
    ImageExecutionError,
    ImageService,
    ImageUnsupportedTargetError,
)
from core.model_tasks.image_providers import ProviderImageClient
from core.model_tasks.image_types import ImageArtifact, ImageGenerationResult
from core.model_tasks.local_targets import (
    DEFAULT_LOCAL_TASK_TARGET_REGISTRY,
    LocalTaskTargetDescriptor,
    LocalTaskTargetError,
    LocalTaskTargetRegistry,
)
from core.model_tasks.model_tasks import (
    TaskModelBinding,
    TaskModelError,
    TaskModelService,
    TaskModelTarget,
    TaskModelTargetRef,
    TaskModelValidationError,
    parse_task_model_target_id,
    public_provider_target_id,
    validate_task_type,
)
from core.model_tasks.options import (
    TaskModelOptionChoice,
    TaskModelOptionField,
    TaskModelOptionSchema,
    option_schema_for,
)
from core.model_tasks.speech import (
    SpeechArtifact,
    SpeechConfigurationError,
    SpeechError,
    SpeechExecutionError,
    SpeechService,
    SpeechUnsupportedTargetError,
)
from core.model_tasks.speech_local import LocalSpeechError, LocalSpeechExecutor
from core.model_tasks.speech_providers import ProviderSpeechClient, audio_format_from
from core.model_tasks.speech_types import SpeechSynthesisResult, SpeechTranscriptionResult

__all__ = [
    "DEFAULT_LOCAL_TASK_TARGET_REGISTRY",
    "EmbeddingConfigurationError",
    "EmbeddingError",
    "EmbeddingExecutionError",
    "EmbeddingResult",
    "EmbeddingService",
    "EmbeddingUnsupportedTargetError",
    "ImageArtifact",
    "ImageConfigurationError",
    "ImageError",
    "ImageExecutionError",
    "ImageGenerationResult",
    "ImageService",
    "ImageUnsupportedTargetError",
    "LocalSpeechError",
    "LocalSpeechExecutor",
    "LocalTaskTargetDescriptor",
    "LocalTaskTargetError",
    "LocalTaskTargetRegistry",
    "ProviderEmbeddingClient",
    "ProviderImageClient",
    "ProviderSpeechClient",
    "SPEECH_TASK_TYPES",
    "SUPPORTED_TASK_TYPES",
    "SpeechArtifact",
    "SpeechConfigurationError",
    "SpeechError",
    "SpeechExecutionError",
    "SpeechService",
    "SpeechSynthesisResult",
    "SpeechTranscriptionResult",
    "SpeechUnsupportedTargetError",
    "TASK_IMAGE_GENERATION",
    "TASK_SPEECH_TO_TEXT",
    "TASK_TEXT_EMBEDDING",
    "TASK_TEXT_TO_SPEECH",
    "TASK_VIDEO_GENERATION",
    "TaskModelBinding",
    "TaskModelError",
    "TaskModelOptionChoice",
    "TaskModelOptionField",
    "TaskModelOptionSchema",
    "TaskModelService",
    "TaskModelTarget",
    "TaskModelTargetRef",
    "TaskModelValidationError",
    "audio_format_from",
    "option_schema_for",
    "parse_task_model_target_id",
    "public_provider_target_id",
    "validate_task_type",
]
