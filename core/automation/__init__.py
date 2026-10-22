"""Automation domain public API."""

from core.automation._cron_projection import CronProjection
from core.automation.automation import TriggerService
from core.automation.bootstrap import (
    BootstrapService,
    validate_bootstrap_jobs_data,
    validate_bootstrap_jobs_file,
)
from core.automation.cron import (
    CronOccurrence,
    CronService,
    validate_cron_jobs_data,
    validate_cron_jobs_file,
)
from core.automation.goals import GoalService
from core.automation.learning import (
    LaterChange,
    LearningChange,
    LearningChanges,
    LearningError,
    LearningRunActiveError,
    LearningSummary,
    LearningUndoConflictError,
    LearningUndoError,
    LearningUndoFailedError,
    LearningUndoResult,
    RunLearningChanges,
)
from core.automation.librarian import (
    LibrarianBusyError,
    LibrarianError,
    LibrarianService,
    LibrarianUnavailableError,
    validate_librarian_state_file,
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
    "CronProjection",
    "LaterChange",
    "LearningChange",
    "LearningChanges",
    "LearningError",
    "LearningRunActiveError",
    "LearningSummary",
    "LearningUndoConflictError",
    "LearningUndoError",
    "LearningUndoFailedError",
    "LearningUndoResult",
    "RunLearningChanges",
    "CronService",
    "BootstrapService",
    "LibrarianBusyError",
    "LibrarianError",
    "LibrarianService",
    "LibrarianUnavailableError",
    "ReflectionResult",
    "ReflectionService",
    "TriggerService",
    "GoalService",
    "validate_cron_jobs_data",
    "validate_cron_jobs_file",
    "validate_bootstrap_jobs_data",
    "validate_bootstrap_jobs_file",
    "validate_librarian_state_file",
]
