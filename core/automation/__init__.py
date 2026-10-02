"""Automation domain public API."""

from core.automation.automation import TriggerService
from core.automation.bootstrap import (
    BootstrapService,
    validate_bootstrap_jobs_data,
    validate_bootstrap_jobs_file,
)
from core.automation.cron import (
    CronOccurrence,
    CronService,
    load_validated_cron_jobs_json,
    validate_cron_jobs_data,
    validate_cron_jobs_file,
)
from core.automation.learning import (
    LaterChange,
    LearningChange,
    LearningChanges,
    LearningError,
    LearningSummary,
    LearningUndoConflictError,
    LearningUndoError,
    LearningUndoFailedError,
    LearningUndoResult,
    RunLearningChanges,
)
from core.automation.references import AutomationReference, AutomationReferences
from core.automation.reflection import (
    REFLECTION_COUNTERS_META_KEY,
    REFLECTION_TOOL_RESTRICTION,
    ReflectionResult,
    ReflectionService,
)

__all__ = [
    "REFLECTION_COUNTERS_META_KEY",
    "REFLECTION_TOOL_RESTRICTION",
    "AutomationReference",
    "AutomationReferences",
    "CronOccurrence",
    "LaterChange",
    "LearningChange",
    "LearningChanges",
    "LearningError",
    "LearningSummary",
    "LearningUndoConflictError",
    "LearningUndoError",
    "LearningUndoFailedError",
    "LearningUndoResult",
    "RunLearningChanges",
    "CronService",
    "BootstrapService",
    "ReflectionResult",
    "ReflectionService",
    "TriggerService",
    "load_validated_cron_jobs_json",
    "validate_cron_jobs_data",
    "validate_cron_jobs_file",
    "validate_bootstrap_jobs_data",
    "validate_bootstrap_jobs_file",
]
