"""Passive subscription windows observed from native Claude streams."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from threading import Lock
from typing import Any, TypeGuard

from core.providers._usage_parsers import _as_number, _epoch_to_iso, clamp_percent
from core.providers._usage_types import UsageWindow

_WINDOWS: dict[str, tuple[str, int | None]] = {
    "five_hour": ("5h", 5 * 3600),
    "seven_day": ("Week", 7 * 24 * 3600),
    "seven_day_opus": ("Week (Opus)", 7 * 24 * 3600),
    "seven_day_sonnet": ("Week (Sonnet)", 7 * 24 * 3600),
    "overage": ("Extra usage", None),
}


class ClaudeAgentUsage:
    """Keep only normalized public window facts, never raw SDK or login data."""

    def __init__(self, *, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._clock = clock
        self._windows: dict[str, UsageWindow] = {}
        self._lock = Lock()

    def observe(self, info: Mapping[str, Any]) -> None:
        unified = info.get("unifiedWindows")
        raw = info.get("raw")
        if not isinstance(unified, Mapping) and isinstance(raw, Mapping):
            unified = raw.get("unifiedWindows")
        if isinstance(unified, Mapping):
            # Claude Code reports every active window here; its top-level entry
            # names only the window that decides the status and has no utilization.
            windows = {
                window_type: window
                for window_type, entry in unified.items()
                if isinstance(entry, Mapping)
                and _known(window_type, entry.get("status", info.get("status")))
                and (window := _window(window_type, entry)) is not None
            }
            with self._lock:
                self._windows = windows
            return
        window_type = info.get("rate_limit_type") or info.get("rateLimitType")
        if not _known(window_type, info.get("status")):
            return
        window = _window(window_type, info)
        with self._lock:
            if window is None:
                # A new window with unknown consumption cannot reuse a previous
                # observation's percentage, even when its label stayed unchanged.
                self._windows.pop(window_type, None)
            else:
                self._windows[window_type] = window

    def windows(self) -> tuple[UsageWindow, ...]:
        """Return fresh immutable facts; a reset does not imply measured zero usage."""
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        with self._lock:
            for window_type, window in tuple(self._windows.items()):
                if window.reset_at is not None and datetime.fromisoformat(window.reset_at) <= now:
                    self._windows.pop(window_type)
            return tuple(self._windows[key] for key in _WINDOWS if key in self._windows)

    def clear(self) -> None:
        with self._lock:
            self._windows.clear()


def _known(window_type: object, status: object) -> TypeGuard[str]:
    return (
        isinstance(window_type, str)
        and window_type in _WINDOWS
        and status in {"allowed", "allowed_warning", "rejected"}
    )


def _window(window_type: str, entry: Mapping[str, Any]) -> UsageWindow | None:
    try:
        utilization = _as_number(entry.get("utilization"))
    except OverflowError:
        utilization = None
    if utilization is None or utilization < 0:
        return None
    try:
        reset_value = entry.get("resets_at")
        reset_at = _epoch_to_iso(reset_value if reset_value is not None else entry.get("resetsAt"))
    except OverflowError:
        reset_at = None
    label, duration = _WINDOWS[window_type]
    return UsageWindow(
        label=label,
        used_percent=clamp_percent(min(utilization, 1.0) * 100),
        reset_at=reset_at,
        window_seconds=duration,
    )
