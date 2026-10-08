"""Expected errors for the calendar domain."""

from collections.abc import Sequence

from core.utils.errors import VBotError


class CalendarServiceError(VBotError):
    """Base class for expected calendar service errors."""


class CalendarValidationError(CalendarServiceError):
    """Raised when calendar event data is invalid."""


class EventJobTargetMissingError(CalendarValidationError):
    """An event change would let jobs bound to the event run again whose target no longer exists.

    ``problems`` pairs each job id with what is missing. A job that can no
    longer fire is history, so removing its Agent, Project or selected Session
    did not check it; reviving it would only fail.
    """

    def __init__(self, problems: Sequence[tuple[str, str]]) -> None:
        self.problems = tuple(problems)
        subject = "a cron job" if len(self.problems) == 1 else "cron jobs"
        listing = "; ".join(f"{job_id} ({problem})" for job_id, problem in self.problems)
        super().__init__(
            f"This event change would let {subject} run again whose target no longer exists: "
            f"{listing}. Change each job's target or Session, or delete the job, first."
        )


class CalendarEventNotFoundError(CalendarServiceError):
    """Raised when a calendar event id is missing."""


class CalendarStorageError(CalendarServiceError):
    """Raised when calendar storage cannot be read or written."""
