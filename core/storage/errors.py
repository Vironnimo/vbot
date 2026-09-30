"""Storage domain errors."""

from __future__ import annotations

from core.utils.errors import StorageError

__all__ = ["SettingsConflictError", "StorageError"]


class SettingsConflictError(StorageError):
    """A Settings update was based on values that changed before it was applied."""

    def __init__(self, paths: tuple[str, ...]) -> None:
        self.paths = paths
        super().__init__(
            "Settings changed since they were read: "
            f"{', '.join(paths)}. Read them again and reapply the change."
        )
