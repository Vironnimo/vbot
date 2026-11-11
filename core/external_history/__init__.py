"""Read external conversations without starting their agents or changing their data."""

from core.external_history.history import ExternalHistory, ExternalHistoryError

__all__ = ["ExternalHistory", "ExternalHistoryError"]
