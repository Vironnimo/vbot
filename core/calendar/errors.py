"""Expected errors for the calendar domain."""

from collections.abc import Sequence

from core.utils.errors import VBotError


class CalendarServiceError(VBotError):
    """Base class for expected calendar service errors."""


class CalendarValidationError(CalendarServiceError):
    """Raised when calendar event data is invalid."""


class CalendarActionTargetMissingError(CalendarValidationError):
    """An event change would let actions run again whose target no longer exists.

    ``problems`` pairs each action id with what is missing. An action that can
    no longer fire is history, so removing its Agent, Project or selected
    Session did not check it; reviving it would only fail.
    """

    def __init__(self, problems: Sequence[tuple[str, str]]) -> None:
        self.problems = tuple(problems)
        super().__init__(
            f"This event change would let {self.subject} run again whose target no longer "
            f"exists: {self.listing}. Change each action's target or Session, or delete the "
            "action, first."
        )

    @property
    def subject(self) -> str:
        return "an action" if len(self.problems) == 1 else "actions"

    @property
    def listing(self) -> str:
        return "; ".join(f"{action_id} ({problem})" for action_id, problem in self.problems)


class CalendarEventNotFoundError(CalendarServiceError):
    """Raised when a calendar event id is missing."""


class CalendarStorageError(CalendarServiceError):
    """Raised when calendar storage cannot be read or written."""
