"""On-demand census of the objects the cyclic garbage collector tracks.

A census counts the tracked objects of each collector generation by type. It
answers what fills the old generation, whose size drives full collection
pauses. Objects frozen at startup sit in the permanent generation and are
only counted in total.

Listing the objects of one generation is a single interpreter call that holds
the GIL, roughly as long as a collection's traversal of the same objects. The
counting that follows runs in slices, so the Event Loop gets the GIL back
between them. Only type and module names leave this module, never values.
"""

from __future__ import annotations

import gc
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

_GENERATIONS = 3
_COUNT_SLICE = 20_000
_MODULE_SEGMENTS = 2


@dataclass(frozen=True, slots=True)
class HeapCensus:
    """Tracked objects by generation and by module-qualified type name."""

    taken_at: datetime
    duration_ms: float
    generations: tuple[int, ...]
    frozen: int
    collections: tuple[dict[str, int], ...]
    types: dict[str, int]


def take_census() -> HeapCensus:
    """Count the tracked objects of every generation; call off the Event Loop."""
    taken_at = datetime.now(UTC)
    started = perf_counter()
    by_type: Counter[type] = Counter()
    generations: list[int] = []
    for generation in range(_GENERATIONS):
        objects = gc.get_objects(generation)
        generations.append(len(objects))
        for start in range(0, len(objects), _COUNT_SLICE):
            by_type.update(map(type, objects[start : start + _COUNT_SLICE]))
        # Drop the references at once so the objects can be freed again.
        del objects
    types: Counter[str] = Counter()
    for kind, count in by_type.items():
        types[type_name(kind)] += count
    return HeapCensus(
        taken_at=taken_at,
        duration_ms=(perf_counter() - started) * 1000.0,
        generations=tuple(generations),
        frozen=gc.get_freeze_count(),
        collections=tuple(dict(stats) for stats in gc.get_stats()),
        types=dict(types),
    )


def census_result(census: HeapCensus, previous: HeapCensus | None, *, top: int) -> dict[str, Any]:
    """Return the public census projection, with changes since ``previous``."""
    previous_types = None if previous is None else previous.types
    modules: Counter[str] = Counter()
    for name, count in census.types.items():
        modules[_module_group(name)] += count
    previous_modules: Counter[str] | None = None
    if previous_types is not None:
        previous_modules = Counter()
        for name, count in previous_types.items():
            previous_modules[_module_group(name)] += count
    growth: list[dict[str, Any]] = []
    if previous_types is not None:
        changes = {
            name: census.types.get(name, 0) - previous_types.get(name, 0)
            for name in census.types.keys() | previous_types.keys()
        }
        grown = sorted(
            (item for item in changes.items() if item[1] > 0), key=lambda item: (-item[1], item[0])
        )
        growth = [
            {"name": name, "count": census.types.get(name, 0), "change": change}
            for name, change in grown[:top]
        ]
    return {
        "taken_at": census.taken_at.isoformat(),
        "duration_ms": round(census.duration_ms, 1),
        "previous_taken_at": None if previous is None else previous.taken_at.isoformat(),
        "tracked": sum(census.generations),
        "generations": [
            {"objects": objects, **stats}
            for objects, stats in zip(census.generations, census.collections, strict=False)
        ],
        "frozen": census.frozen,
        "types": _top_rows(census.types, previous_types, top),
        "modules": _top_rows(modules, previous_modules, top),
        "growth": growth,
    }


def type_name(kind: type) -> str:
    """Return ``module.qualname`` of one type."""
    module = getattr(kind, "__module__", None)
    qualname = getattr(kind, "__qualname__", None) or getattr(kind, "__name__", "?")
    if not isinstance(module, str) or not module:
        return str(qualname)
    return f"{module}.{qualname}"


def _module_group(name: str) -> str:
    # Group by package, e.g. ``core.runs`` or ``asyncio.events``; the type's
    # own name is the last segment and never part of the group.
    segments = name.split(".")[:-1] or ["?"]
    return ".".join(segments[:_MODULE_SEGMENTS])


def _top_rows(
    counts: dict[str, int], previous: dict[str, int] | None, top: int
) -> list[dict[str, Any]]:
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:top]
    return [
        {
            "name": name,
            "count": count,
            "change": None if previous is None else count - previous.get(name, 0),
        }
        for name, count in ordered
    ]
