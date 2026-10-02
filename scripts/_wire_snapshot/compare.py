"""Compare two wire snapshots and print a concise, grouped diff.

Each changed record is reduced to its change pattern: the set of JSON paths
whose values differ, with old and new values. The record's own Model id is
replaced by ``<model>`` in those values, so the same change across many Models
collapses into one group. Groups are reported per Provider, largest first, with
their record count, record kinds and example keys.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from scripts._wire_snapshot.records import read_snapshot

_VALUE_DISPLAY_LIMIT = 200
_EXAMPLE_KEYS = 3
_LISTED_KEYS = 10
_MISSING = object()

type Change = tuple[str, str, str]
"""One changed JSON path with its old and new value, both as display JSON."""


@dataclass
class _ProviderDiff:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    patterns: dict[tuple[Change, ...], list[str]] = field(default_factory=dict)

    @property
    def changed(self) -> int:
        return sum(len(keys) for keys in self.patterns.values())

    @property
    def different(self) -> bool:
        return bool(self.added or self.removed or self.patterns)


def compare_snapshots(
    baseline_dir: Path,
    new_dir: Path,
    *,
    providers: Iterable[str] | None = None,
    max_groups: int = 20,
) -> tuple[bool, list[str]]:
    """Return ``(different, report_lines)`` for two snapshot directories.

    Raises ``ValueError`` when either path is not a directory.
    """

    selected = sorted(set(providers)) if providers else None
    for directory in (baseline_dir, new_dir):
        if not directory.is_dir():
            raise ValueError(f"not a snapshot directory: {directory}")
    baseline = read_snapshot(baseline_dir, selected)
    new = read_snapshot(new_dir, selected)
    lines: list[str] = []
    different = False
    total_records = 0
    for provider_id in sorted(set(baseline) | set(new)):
        if provider_id not in new:
            different = True
            lines.append(f"Provider {provider_id}: missing from the new snapshot")
            continue
        if provider_id not in baseline:
            different = True
            lines.append(f"Provider {provider_id}: missing from the baseline snapshot")
            continue
        total_records += len(new[provider_id])
        diff = _diff_provider(baseline[provider_id], new[provider_id])
        if diff.different:
            different = True
            lines.extend(_render_provider(provider_id, diff, max_groups))
    if selected:
        for provider_id in selected:
            if provider_id not in baseline and provider_id not in new:
                different = True
                lines.append(f"Provider {provider_id}: in neither snapshot")
    if not different:
        provider_count = len(set(baseline) | set(new))
        lines.append(f"identical: {total_records} records across {provider_count} Providers")
    return different, lines


def _diff_provider(old: dict[str, dict[str, Any]], new: dict[str, dict[str, Any]]) -> _ProviderDiff:
    diff = _ProviderDiff(
        added=sorted(set(new) - set(old)),
        removed=sorted(set(old) - set(new)),
    )
    patterns: defaultdict[tuple[Change, ...], list[str]] = defaultdict(list)
    for key in sorted(set(old) & set(new)):
        if old[key] == new[key]:
            continue
        model_id = _model_id(key)
        changes = tuple(
            sorted(
                (path, _display(old_value, model_id), _display(new_value, model_id))
                for path, old_value, new_value in _changed_paths(old[key], new[key], "")
            )
        )
        patterns[changes].append(key)
    diff.patterns = dict(patterns)
    return diff


def _changed_paths(old: Any, new: Any, path: str) -> Iterator[tuple[str, Any, Any]]:
    """Yield ``(path, old, new)`` for every leaf that differs; lists of unequal length whole."""

    if isinstance(old, dict) and isinstance(new, dict):
        for name in sorted(set(old) | set(new)):
            child = f"{path}.{name}" if path else str(name)
            yield from _changed_paths(old.get(name, _MISSING), new.get(name, _MISSING), child)
        return
    if isinstance(old, list) and isinstance(new, list) and len(old) == len(new):
        for index, (old_item, new_item) in enumerate(zip(old, new, strict=True)):
            yield from _changed_paths(old_item, new_item, f"{path}[{index}]")
        return
    if old != new:
        yield path or "<record>", old, new


def _model_id(key: str) -> str | None:
    """Return the Model id a record key names, if any."""

    parts = key.split("|")
    if len(parts) >= 3 and parts[0] in {"catalog", "model", "render", "response"}:
        return parts[2]
    return None


def _examples(keys: list[str]) -> list[str]:
    """Return a few example keys, preferring different Connections and Models."""

    examples: list[str] = []
    seen: set[tuple[str, ...]] = set()
    for key in keys:
        target = tuple(key.split("|")[1:3])
        if target not in seen:
            seen.add(target)
            examples.append(key)
            if len(examples) == _EXAMPLE_KEYS:
                return examples
    return examples + [key for key in keys if key not in examples][: _EXAMPLE_KEYS - len(examples)]


def _display(value: Any, model_id: str | None) -> str:
    if value is _MISSING:
        return "<absent>"
    text = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if model_id and len(model_id) >= 3:
        text = text.replace(model_id, "<model>")
    if len(text) > _VALUE_DISPLAY_LIMIT:
        text = f"{text[: _VALUE_DISPLAY_LIMIT - 3]}..."
    return text


def _render_provider(provider_id: str, diff: _ProviderDiff, max_groups: int) -> list[str]:
    lines = [
        f"Provider {provider_id}: {diff.changed} changed, {len(diff.added)} added, "
        f"{len(diff.removed)} removed records ({len(diff.patterns)} change patterns)"
    ]
    for label, keys in (("added", diff.added), ("removed", diff.removed)):
        if keys:
            lines.append(f"  {label}: {len(keys)} records")
            lines.extend(f"    {key}" for key in keys[:_LISTED_KEYS])
            if len(keys) > _LISTED_KEYS:
                lines.append(f"    ... {len(keys) - _LISTED_KEYS} more")
    ordered = sorted(diff.patterns.items(), key=lambda item: (-len(item[1]), item[1][0]))
    for changes, keys in ordered[:max_groups]:
        kinds = ", ".join(sorted({key.split("|", 1)[0] for key in keys}))
        examples = ", ".join(_examples(keys))
        more = f" (+{len(keys) - _EXAMPLE_KEYS} more)" if len(keys) > _EXAMPLE_KEYS else ""
        lines.append(f"  [{len(keys)}x {kinds}] e.g. {examples}{more}")
        for path, old_value, new_value in changes:
            lines.append(f"      {path}: {old_value} -> {new_value}")
    hidden = ordered[max_groups:]
    if hidden:
        hidden_records = sum(len(keys) for _, keys in hidden)
        hidden_paths = sorted({path for changes, _ in hidden for path, _, _ in changes})
        shown_paths = ", ".join(hidden_paths[:_LISTED_KEYS])
        extra = (
            f" (+{len(hidden_paths) - _LISTED_KEYS} more)"
            if len(hidden_paths) > _LISTED_KEYS
            else ""
        )
        lines.append(
            f"  ... {len(hidden)} more patterns over {hidden_records} records, "
            f"paths: {shown_paths}{extra}"
        )
    return lines
