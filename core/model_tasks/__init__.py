"""Task-model bindings and discovery."""

from core.model_tasks.constants import (
    SPEECH_TASK_TYPES,
    SUPPORTED_TASK_TYPES,
    TASK_IMAGE_GENERATION,
    TASK_SPEECH_TO_TEXT,
    TASK_TEXT_TO_SPEECH,
    TASK_VIDEO_GENERATION,
)
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

__all__ = [
    "DEFAULT_LOCAL_TASK_TARGET_REGISTRY",
    "LocalTaskTargetDescriptor",
    "LocalTaskTargetError",
    "LocalTaskTargetRegistry",
    "SPEECH_TASK_TYPES",
    "SUPPORTED_TASK_TYPES",
    "TASK_IMAGE_GENERATION",
    "TASK_SPEECH_TO_TEXT",
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
    "option_schema_for",
    "parse_task_model_target_id",
    "public_provider_target_id",
    "validate_task_type",
]
