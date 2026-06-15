"""Standalone validator for the Model DB canonical join.

NOT hooked into the runtime read path — this is an offline integrity check, run
explicitly (see ``scripts/validate_model_db.py``). It loads the canonical layer
and every provider/override layer and reports two classes of finding the handoff
calls for ("Dazu ein Validator"):

* **Dead ``canonical`` pointer** — a model's ``canonical`` pointer (auto in
  ``<provider>.json`` or manual in ``<provider>.overrides.json``) targets an id
  that is not present in the canonical layer. models.dev may have renamed the
  slug; the join silently enriches nothing.
* **Redundant manual join** — a *manual* (override-layer) pointer where
  ``pointer == wire_id`` and ``wire_id`` is itself a canonical-layer key, so the
  deterministic exact-match auto-join (rule 2 in :mod:`core.models.assembly`)
  would already produce the same join without the hand pointer.

Findings are data (:class:`ValidationFinding`); the thin script renders + exits.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from core.models.assembly import (
    CANONICAL_FILE_NAME,
    CANONICAL_OVERRIDES_FILE_NAME,
    CANONICAL_POINTER_KEY,
    load_canonical_layer,
)
from core.models.models import OVERRIDES_FILE_SUFFIX, RAW_FILE_SUFFIX, is_provider_file

DEAD_POINTER = "dead_pointer"
REDUNDANT_MANUAL_JOIN = "redundant_manual_join"


@dataclass(frozen=True)
class ValidationFinding:
    """One model-DB integrity warning.

    Attributes:
        kind: One of ``DEAD_POINTER`` / ``REDUNDANT_MANUAL_JOIN``.
        provider_id: The provider whose model carries the issue.
        wire_id: The provider wire-id of the offending model.
        pointer: The ``canonical`` pointer value involved.
        message: A human-readable, source-grounded description.
    """

    kind: str
    provider_id: str
    wire_id: str
    pointer: str
    message: str


def validate_model_db(resources_dir: Path) -> list[ValidationFinding]:
    """Validate canonical pointers across all layers under ``resources_dir``.

    Args:
        resources_dir: The resources root (containing a ``models/`` subdir).

    Returns:
        Findings sorted by ``(provider_id, wire_id, kind)``; empty when clean.
    """

    models_dir = resources_dir / "models"
    canonical_layer = load_canonical_layer(models_dir)
    findings: list[ValidationFinding] = []

    for provider_id, wire_id, pointer, is_manual in _iter_pointers(models_dir):
        if pointer not in canonical_layer:
            findings.append(
                ValidationFinding(
                    kind=DEAD_POINTER,
                    provider_id=provider_id,
                    wire_id=wire_id,
                    pointer=pointer,
                    message=(
                        f"{provider_id}/{wire_id}: canonical pointer '{pointer}' "
                        "is not present in the canonical layer (dead pointer)"
                    ),
                )
            )
            continue
        if is_manual and pointer == wire_id:
            findings.append(
                ValidationFinding(
                    kind=REDUNDANT_MANUAL_JOIN,
                    provider_id=provider_id,
                    wire_id=wire_id,
                    pointer=pointer,
                    message=(
                        f"{provider_id}/{wire_id}: manual canonical pointer equals the "
                        "wire-id and the wire-id is a canonical key; the auto exact-match "
                        "join already covers it (redundant manual join)"
                    ),
                )
            )

    return sorted(findings, key=lambda f: (f.provider_id, f.wire_id, f.kind))


def _iter_pointers(models_dir: Path) -> Iterator[tuple[str, str, str, bool]]:
    """Yield ``(provider_id, wire_id, pointer, is_manual)`` for every pointer.

    Walks each ``<provider>.json`` (auto pointers, ``is_manual=False``) and its
    sibling ``<provider>.overrides.json`` (manual pointers, ``is_manual=True``).
    Override files may omit ``provider_id`` — the id is derived from the filename.
    The canonical files themselves carry no pointers and are skipped.
    """

    for json_file in sorted(models_dir.glob("*.json")):
        name = json_file.name
        if name in {CANONICAL_FILE_NAME, CANONICAL_OVERRIDES_FILE_NAME}:
            continue
        if name.endswith(RAW_FILE_SUFFIX):
            continue

        if name.endswith(OVERRIDES_FILE_SUFFIX):
            provider_id = name[: -len(OVERRIDES_FILE_SUFFIX)]
            is_manual = True
        elif is_provider_file(name):
            provider_id = json_file.stem
            is_manual = False
        else:
            continue

        for wire_id, pointer in _pointers_in_file(json_file):
            yield provider_id, wire_id, pointer, is_manual


def _pointers_in_file(path: Path) -> Iterator[tuple[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    models = data.get("models", {})
    if not isinstance(models, Mapping):
        return
    for wire_id, model in models.items():
        if not isinstance(model, Mapping):
            continue
        pointer = model.get(CANONICAL_POINTER_KEY)
        if isinstance(pointer, str) and pointer:
            yield wire_id, pointer
