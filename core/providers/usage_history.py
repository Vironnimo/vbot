"""Durable append-only history for normalized Provider subscription usage."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from threading import RLock
from typing import Any

from core.utils.logging import get_logger

JsonObject = dict[str, Any]

USAGE_HISTORY_DIRECTORY_NAME = "provider-usage"
USAGE_HISTORY_SCHEMA_VERSION = 1
USAGE_HISTORY_FILE_SUFFIX = ".jsonl"

_LOGGER = get_logger("providers.usage")


class UsageHistoryError(Exception):
    """A durable usage-history read, append, or deletion failure."""


@dataclass(frozen=True)
class UsageHistorySample:
    """One persisted automatic collection attempt with meaningful targets."""

    sampled_at: str
    providers: list[JsonObject] = field(default_factory=list)

    def to_dict(self) -> JsonObject:
        return {
            "sampled_at": self.sampled_at,
            "providers": [dict(snapshot) for snapshot in self.providers],
        }


@dataclass(frozen=True)
class UsageHistoryReport:
    """Time-windowed durable Provider-usage samples."""

    generated_at: str
    samples: list[UsageHistorySample] = field(default_factory=list)

    def to_dict(self) -> JsonObject:
        return {
            "generated_at": self.generated_at,
            "samples": [sample.to_dict() for sample in self.samples],
        }


@dataclass(frozen=True)
class UsageHistoryClearResult:
    """Outcome of an explicit full history deletion."""

    deleted_samples: int
    deleted_files: int

    def to_dict(self) -> dict[str, int]:
        return {
            "deleted_samples": self.deleted_samples,
            "deleted_files": self.deleted_files,
        }


class ProviderUsageHistoryStore:
    """Append-only monthly JSONL history under the vBot data directory.

    The Provider domain owns these files as primary upstream observations.
    Statistics may read their normalized projection, but never writes them.
    """

    def __init__(self, data_root: str | Path) -> None:
        self._directory = Path(data_root) / USAGE_HISTORY_DIRECTORY_NAME
        self._lock = RLock()

    @property
    def directory(self) -> Path:
        return self._directory

    def append(self, generated_at: str, providers: list[JsonObject]) -> bool:
        """Persist one meaningful automatic report.

        Empty reports mean no supported usable Connection exists and are not
        written. Error snapshots are meaningful: they preserve why an expected
        observation is missing.
        """

        if not providers:
            return False
        payload: JsonObject = {
            "schema_version": USAGE_HISTORY_SCHEMA_VERSION,
            "sampled_at": generated_at,
            "providers": providers,
        }
        sample = _history_sample_from_dict(payload)
        encoded = (
            json.dumps(
                {
                    "schema_version": USAGE_HISTORY_SCHEMA_VERSION,
                    **sample.to_dict(),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        path = self._path_for(sample.sampled_at)
        with self._lock:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                _append_bytes(path, encoded)
            except OSError as exc:
                raise UsageHistoryError(f"Cannot append Provider usage history: {exc}") from exc
        return True

    def report(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> UsageHistoryReport:
        """Read all valid samples in the inclusive UTC window."""

        with self._lock:
            samples: list[UsageHistorySample] = []
            for path in self._paths_for_window(since=since, until=until):
                samples.extend(self._read_file(path, since=since, until=until))
        samples.sort(key=lambda sample: sample.sampled_at)
        return UsageHistoryReport(
            generated_at=datetime.now(UTC).isoformat(),
            samples=samples,
        )

    def latest_sampled_at(self) -> datetime | None:
        """Return the newest valid sample timestamp without inventing state."""

        with self._lock:
            for path in reversed(self._history_paths()):
                samples = self._read_file(path, since=None, until=None)
                if samples:
                    timestamps = [
                        parsed
                        for sample in samples
                        if (parsed := _parse_iso_timestamp(sample.sampled_at)) is not None
                    ]
                    if timestamps:
                        return max(timestamps)
        return None

    def clear(self) -> UsageHistoryClearResult:
        """Delete every history file after an explicit caller confirmation."""

        with self._lock:
            paths = self._history_paths()
            deleted_samples = sum(
                len(self._read_file(path, since=None, until=None)) for path in paths
            )
            deleted_files = 0
            try:
                for path in paths:
                    path.unlink()
                    deleted_files += 1
            except OSError as exc:
                raise UsageHistoryError(f"Cannot clear Provider usage history: {exc}") from exc
        _LOGGER.info(
            "Provider usage history cleared (samples=%s files=%s)",
            deleted_samples,
            deleted_files,
        )
        return UsageHistoryClearResult(
            deleted_samples=deleted_samples,
            deleted_files=deleted_files,
        )

    def _path_for(self, sampled_at: str) -> Path:
        parsed = _parse_iso_timestamp(sampled_at)
        if parsed is None:
            raise UsageHistoryError("Provider usage sample timestamp is invalid")
        return self._directory / f"{parsed:%Y-%m}{USAGE_HISTORY_FILE_SUFFIX}"

    def _history_paths(self) -> list[Path]:
        if not self._directory.is_dir():
            return []
        return sorted(self._directory.glob(f"*{USAGE_HISTORY_FILE_SUFFIX}"))

    def _paths_for_window(
        self,
        *,
        since: datetime | None,
        until: datetime | None,
    ) -> list[Path]:
        paths = self._history_paths()
        if since is None and until is None:
            return paths
        first_month = since.strftime("%Y-%m") if since is not None else None
        last_month = until.strftime("%Y-%m") if until is not None else None
        return [
            path
            for path in paths
            if (first_month is None or path.stem >= first_month)
            and (last_month is None or path.stem <= last_month)
        ]

    def _read_file(
        self,
        path: Path,
        *,
        since: datetime | None,
        until: datetime | None,
    ) -> list[UsageHistorySample]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise UsageHistoryError(f"Cannot read Provider usage history: {exc}") from exc

        samples: list[UsageHistorySample] = []
        for line_number, line in enumerate(lines, start=1):
            try:
                sample = _history_sample_from_dict(json.loads(line))
            except (TypeError, ValueError, UsageHistoryError) as exc:
                _LOGGER.warning(
                    "Skipping invalid Provider usage history row %s:%s: %s",
                    path.name,
                    line_number,
                    exc,
                )
                continue
            sampled_at = _parse_iso_timestamp(sample.sampled_at)
            if sampled_at is None:
                continue
            if since is not None and sampled_at < since:
                continue
            if until is not None and sampled_at > until:
                continue
            samples.append(sample)
        return samples


def _history_sample_from_dict(raw: Any) -> UsageHistorySample:
    if not isinstance(raw, dict):
        raise UsageHistoryError("row must be an object")
    _require_exact_keys(raw, {"schema_version", "sampled_at", "providers"}, "row")
    if raw["schema_version"] != USAGE_HISTORY_SCHEMA_VERSION:
        raise UsageHistoryError("unsupported schema_version")
    sampled_at = _normalize_iso_timestamp(_required_string(raw["sampled_at"], "sampled_at"))
    providers_raw = raw["providers"]
    if not isinstance(providers_raw, list) or not providers_raw:
        raise UsageHistoryError("providers must be a non-empty list")
    return UsageHistorySample(
        sampled_at=sampled_at,
        providers=[_snapshot_from_dict(item) for item in providers_raw],
    )


def _snapshot_from_dict(raw: Any) -> JsonObject:
    if not isinstance(raw, dict):
        raise UsageHistoryError("provider snapshot must be an object")
    _require_exact_keys(
        raw,
        {
            "connection",
            "account",
            "display_name",
            "plan",
            "windows",
            "credits",
            "error",
        },
        "provider snapshot",
    )
    windows_raw = raw["windows"]
    if not isinstance(windows_raw, list):
        raise UsageHistoryError("provider windows must be a list")
    return {
        "connection": _required_string(raw["connection"], "connection"),
        "account": _required_string(raw["account"], "account"),
        "display_name": _required_string(raw["display_name"], "display_name"),
        "plan": _optional_string(raw["plan"], "plan"),
        "windows": [_window_from_dict(item) for item in windows_raw],
        "credits": _credits_from_dict(raw["credits"]),
        "error": _optional_string(raw["error"], "error"),
    }


def _window_from_dict(raw: Any) -> JsonObject:
    if not isinstance(raw, dict):
        raise UsageHistoryError("usage window must be an object")
    _require_exact_keys(
        raw,
        {
            "label",
            "used_percent",
            "reset_at",
            "window_seconds",
            "used_units",
            "remaining_units",
            "total_units",
            "unit",
            "unlimited",
        },
        "usage window",
    )
    used_percent = _optional_number(raw["used_percent"], "used_percent")
    if used_percent is None:
        raise UsageHistoryError("used_percent must be a number")
    window_seconds = raw["window_seconds"]
    if window_seconds is not None and (
        isinstance(window_seconds, bool)
        or not isinstance(window_seconds, int)
        or window_seconds <= 0
    ):
        raise UsageHistoryError("window_seconds must be a positive integer or null")
    unlimited = raw["unlimited"]
    if unlimited is not None and not isinstance(unlimited, bool):
        raise UsageHistoryError("unlimited must be a boolean or null")
    reset_at = _optional_string(raw["reset_at"], "reset_at")
    if reset_at is not None:
        reset_at = _normalize_iso_timestamp(reset_at)
    return {
        "label": _required_string(raw["label"], "label"),
        "used_percent": max(0.0, min(100.0, used_percent)),
        "reset_at": reset_at,
        "window_seconds": window_seconds,
        "used_units": _optional_number(raw["used_units"], "used_units"),
        "remaining_units": _optional_number(raw["remaining_units"], "remaining_units"),
        "total_units": _optional_number(raw["total_units"], "total_units"),
        "unit": _optional_string(raw["unit"], "unit"),
        "unlimited": unlimited,
    }


def _credits_from_dict(raw: Any) -> JsonObject | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise UsageHistoryError("credits must be an object or null")
    _require_exact_keys(raw, {"enabled", "balance"}, "credits")
    if not isinstance(raw["enabled"], bool):
        raise UsageHistoryError("credits.enabled must be a boolean")
    return {
        "enabled": raw["enabled"],
        "balance": _optional_number(raw["balance"], "credits.balance"),
    }


def _required_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise UsageHistoryError(f"{field_name} must be a non-empty string")
    return value


def _optional_string(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise UsageHistoryError(f"{field_name} must be a non-empty string or null")
    return value


def _optional_number(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise UsageHistoryError(f"{field_name} must be a number or null")
    number = float(value)
    if not isfinite(number):
        raise UsageHistoryError(f"{field_name} must be finite")
    return number


def _require_exact_keys(raw: JsonObject, expected: set[str], context: str) -> None:
    if set(raw) != expected:
        raise UsageHistoryError(f"{context} fields are invalid")


def _parse_iso_timestamp(value: str) -> datetime | None:
    try:
        normalized = value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _normalize_iso_timestamp(value: str) -> str:
    parsed = _parse_iso_timestamp(value)
    if parsed is None:
        raise UsageHistoryError("timestamp must be ISO 8601 with an explicit offset")
    return parsed.isoformat()


def _append_bytes(path: Path, data: bytes) -> None:
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    file_descriptor = os.open(path, flags, 0o600)
    try:
        written = 0
        while written < len(data):
            count = os.write(file_descriptor, data[written:])
            if count <= 0:
                raise OSError("Provider usage history append wrote zero bytes")
            written += count
        os.fsync(file_descriptor)
    finally:
        os.close(file_descriptor)
