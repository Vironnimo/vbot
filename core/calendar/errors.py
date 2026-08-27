"""Expected errors for the calendar domain."""

from core.utils.errors import VBotError


class CalendarServiceError(VBotError):
    """Base class for expected calendar service errors."""


class CalendarValidationError(CalendarServiceError):
    """Raised when calendar event data is invalid."""


class CalendarEventNotFoundError(CalendarServiceError):
    """Raised when a calendar event id is missing."""


class CalendarStorageError(CalendarServiceError):
    """Raised when calendar storage cannot be read or written."""
