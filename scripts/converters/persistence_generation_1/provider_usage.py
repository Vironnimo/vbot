"""Generation 1 conversion of the monthly Provider usage JSONL history.

Before Generation 1, ``statistics/provider-usage/YYYY-MM.jsonl`` held one JSON
row per automatic sample: ``{"schema_version": 1, "sampled_at", "providers"}``.
This area reads every file, validates each row the way the file-based reader
did (exact keys at every level, finite numbers, explicit UTC offsets) and
stages all valid samples, oldest first, in a new ``provider-usage.db``. Invalid
lines and unreadable files are skipped and reported; duplicate rows stay
duplicates, as they were. Every JSONL file is retired, because the Generation 1
layout keeps no usage files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core.database import open_offline_database
from core.providers.usage_history import (
    ProviderUsageHistoryStore,
    UsageHistoryError,
    UsageHistorySample,
    provider_usage_database_spec,
    usage_history_sample,
)
from scripts.converters.persistence_generation_1._context import ConversionContext

AREA = "provider_usage"
SOURCE_DIRECTORY = "statistics/provider-usage"
TARGET_DATABASE = "provider-usage.db"

_ROW_SCHEMA_VERSION = 1
_ROW_KEYS = frozenset({"schema_version", "sampled_at", "providers"})


class _InvalidRowError(Exception):
    """A JSONL row the file-based reader would have skipped."""


def convert(context: ConversionContext) -> None:
    """Stage ``provider-usage.db`` from the source JSONL history and retire the files."""
    samples: list[UsageHistorySample] = []
    for path in _history_files(context):
        relative = path.relative_to(context.source).as_posix()
        context.retire(relative)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            context.report.skip(AREA, relative, f"unreadable file dropped ({error})")
            continue
        context.report.count(AREA, "files")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                samples.append(_sample_from_line(line))
            except (_InvalidRowError, UsageHistoryError) as error:
                context.report.skip(AREA, f"{relative}:{line_number}", f"invalid row ({error})")
                context.report.count(AREA, "invalid_rows")
    samples.sort(key=lambda sample: sample.sampled_at)

    target = context.staged(TARGET_DATABASE)
    # A repeated run replaces its own staged output instead of appending to it.
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{target}{suffix}").unlink(missing_ok=True)
    store = ProviderUsageHistoryStore(open_offline_database(provider_usage_database_spec(target)))
    try:
        imported = store.import_samples(samples)
    finally:
        store.close()
    context.report.count(AREA, "samples", imported)


def _history_files(context: ConversionContext) -> list[Path]:
    directory = context.source_path(SOURCE_DIRECTORY)
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.jsonl") if path.is_file())


def _sample_from_line(line: str) -> UsageHistorySample:
    try:
        row: Any = json.loads(line)
    except ValueError as error:
        raise _InvalidRowError(f"not JSON: {error}") from error
    if not isinstance(row, dict):
        raise _InvalidRowError("row must be an object")
    if set(row) != _ROW_KEYS:
        raise _InvalidRowError("row fields are invalid")
    version = row["schema_version"]
    if isinstance(version, bool) or version != _ROW_SCHEMA_VERSION:
        raise _InvalidRowError(f"unsupported schema_version {version!r}")
    providers = row["providers"]
    if not isinstance(providers, list):
        raise _InvalidRowError("providers must be a list")
    return usage_history_sample(row["sampled_at"], providers)
