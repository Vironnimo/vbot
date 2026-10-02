"""Turn a verification run into a wire profile Model entry.

Learned facts become explicit profile values (so the entry no longer depends
on any learned-facts cache) and a clean run adds a ``verified`` record for the
checked Connection. ``write_entry`` merges the entry into the bundled
``resources/wire/<provider>.json`` file.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.providers._wire_profile_files import WIRE_PROFILE_FORMAT_VERSION
from core.providers.wire_observations import ObservedFacts
from core.providers.wire_profile import WireProfile
from core.storage.storage import StorageManager
from core.utils.errors import StorageError
from scripts._wire_verify.checks import CheckResult


def propose_entry(
    profile: WireProfile,
    facts: ObservedFacts,
    results: Sequence[CheckResult],
    *,
    connection_id: str,
    today: str | None = None,
) -> dict[str, Any]:
    """Return the Model entry the run supports (``set`` and, if clean, ``verified``)."""

    values: dict[str, Any] = {}
    if facts.reasoning_field:
        rest = [
            field for field in profile.response.reasoning_fields if field != facts.reasoning_field
        ]
        values.setdefault("response", {})["reasoning_fields"] = [facts.reasoning_field, *rest]
        if profile.source_of("replay.history_field") == "observed":
            values.setdefault("replay", {})["history_field"] = profile.replay.history_field
    if facts.rejected_efforts:
        values.setdefault("reasoning", {})["levels"] = list(profile.reasoning.ladder)
    if facts.rejected_parameters:
        values.setdefault("request", {})["parameters"] = {
            name: {"mode": "drop"} for name in facts.rejected_parameters
        }
    if facts.reasoning_returned and profile.source_of("reasoning.supported") == "observed":
        values.setdefault("reasoning", {})["supported"] = True

    entry: dict[str, Any] = {"set": values} if values else {}
    if results and all(result.status in ("ok", "skipped") for result in results):
        passed = [result.name for result in results if result.status == "ok"]
        entry["verified"] = {
            "date": today or datetime.now(UTC).date().isoformat(),
            "connections": [connection_id],
            "evidence": "scripts/verify_wire_profile.py: " + ", ".join(passed),
        }
    return entry


def write_entry(wire_dir: Path, provider_id: str, model_id: str, entry: Mapping[str, Any]) -> Path:
    """Merge ``entry`` into ``models[model_id]`` of a bundled Provider's wire file."""

    path = wire_dir / f"{provider_id}.json"
    document: dict[str, Any] = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.is_file()
        else {"format_version": WIRE_PROFILE_FORMAT_VERSION}
    )
    _merge_entry(document, model_id, entry)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def write_custom_provider_entry(
    storage: StorageManager, provider_id: str, model_id: str, entry: Mapping[str, Any]
) -> str:
    """Merge ``entry`` into ``wire.models[model_id]`` of a Custom Provider's Settings.

    Returns the Settings path written. Refuses a ``wire`` value that is not an
    object instead of replacing it.
    """

    def update(record: dict[str, Any]) -> dict[str, Any]:
        wire = record.get("wire")
        if wire is not None and not isinstance(wire, Mapping):
            raise StorageError(
                f"providers.custom.{provider_id}.wire is not an object; "
                "fix it before writing a verified entry"
            )
        document = dict(wire or {})
        _merge_entry(document, model_id, entry)
        return {**record, "wire": document}

    storage.update_custom_provider_settings(provider_id, update)
    return f"providers.custom.{provider_id}.wire.models.{model_id.split('::', 1)[0]}"


def _merge_entry(document: dict[str, Any], model_id: str, entry: Mapping[str, Any]) -> None:
    """Merge ``entry`` into ``document["models"][model_id]`` in place."""

    models = document.setdefault("models", {})
    current = models.setdefault(model_id.split("::", 1)[0], {})
    if "set" in entry:
        current["set"] = _merged(current.get("set", {}), entry["set"])
    if "verified" in entry:
        current["verified"] = dict(entry["verified"])


def _merged(base: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in update.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merged(merged[key], value)
        else:
            merged[key] = value
    return merged
