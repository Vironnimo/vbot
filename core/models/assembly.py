"""At-load model assembly: canonical base + provider layer + overrides.

This module is the *backbone* of vBot's Model DB. It owns the smart part of the
load path — the part the handoff calls "Laden baut zusammen" (loading assembles).
The registry read path no longer reads one ``<provider>.json`` per provider; it
assembles each effective model from up to three layers, resolving a deterministic
canonical join, **live at load, with no network and no key**.

``ModelRegistry.load()`` stays the single public read surface; everything here is
hidden behind it. The two public entry points are :func:`load_canonical_layer`
(reads the provider-agnostic base once per ``resources_dir``) and
:func:`assemble_provider_model` (builds one effective per-model record from the
layers + the join). Callers outside ``core/models/`` should not import this module.

================================================================================
THE ON-DISK FILE-FORMAT CONTRACT  (Phase 3 must produce exactly this shape)
================================================================================

All files live under ``<resources_dir>/models/``.

Three layers, each a different home with a clear responsibility:

1. CANONICAL layer — ``models.json`` (+ hand corrections ``models.overrides.json``)
   - Keyed by **canonical id** ``lab/model`` (e.g. ``"deepseek/deepseek-v4-pro"``).
   - Provider-agnostic base facts: ``name``, ``family``, ``capabilities``
     (incl. the lifted lab-spec ``reasoning`` ladder), ``context_window``,
     ``max_output_tokens``. NO ``provider_id`` (it is not a provider file).
   - ``models.overrides.json`` is keyed the same way and is a field-level patch
     over ``models.json`` (override wins, nested objects wholesale).
   - BOTH files may be ABSENT — Phase 3 generates ``models.json``; until then the
     canonical layer is simply empty and assembly runs on provider+override only.
   - Shape::

       {
         "models": {
           "deepseek/deepseek-v4-pro": {
             "name": "DeepSeek V4 Pro",
             "family": "deepseek-v4",
             "capabilities": {
               "vision": false, "tools": true, "json_mode": true,
               "reasoning": {"supported": true, "control": "levels",
                             "levels": ["high", "max"]},
               "input_modalities": ["text"], "output_modalities": ["text"]
             },
             "context_window": 1000000,
             "max_output_tokens": 384000
           }
         }
       }

2. PROVIDER layer — ``<provider>.json`` (generated) + ``<provider>.overrides.json``
   (hand-maintained)
   - Keyed by **wire-id** — the exact id sent on the wire.
   - ``<provider>.json`` carries ``provider_id`` plus a ``models`` map. Each model
     holds what the provider/endpoint authoritatively reports, INCLUDING a
     ``capabilities.reasoning`` ladder that *deviates* from the lab spec when
     models.dev carries one for that provider. May carry an **auto** ``canonical``
     pointer (a top-level string on the model entry, written by Phase-3 refresh).
   - ``<provider>.overrides.json`` is keyed by wire-id, MAY omit ``provider_id``
     (the provider id is derived from the filename), and ALWAYS wins. It is the
     home for a **manual** ``canonical`` pointer and per-provider corrections.

3. (Adapter fallbacks at send time are out of scope here.)

The ``canonical`` pointer — the JSON key ``"canonical"`` on a provider/override
model entry — is an INTERNAL join key. It is stripped from the assembled record;
it is never a ``Model`` attribute and never goes on the wire.

================================================================================
THE DETERMINISTIC JOIN  (no fuzzy matching, ever)
================================================================================

For a provider model with wire-id ``W`` under provider ``P``, the canonical id is
resolved by :func:`resolve_canonical_id`:

1. **Explicit pointer wins.** ``override["canonical"] or provider_model["canonical"]``
   — a manual pointer (override layer) beats an auto pointer (provider layer);
   both are the JSON key ``"canonical"``.
2. **Else exact canonical-id match.** If ``W`` is itself exactly a key in the
   canonical layer, the canonical id is ``W``. (Covers OpenRouter/Mistral-style
   ``lab/model`` wire-ids that already equal the canonical id.)
3. **Else NO join.** The model runs on provider + override data only. A missed
   join is NOT an error — the join is enrichment, not a dependency.

================================================================================
THE 3-LAYER MERGE  (field-level "fill, don't overwrite", highest wins per field)
================================================================================

Precedence, highest first, applied PER top-level field:

  (1) ``<provider>.overrides.json``  (hand)      — always wins
  (2) ``<provider>.json``            (provider)   — what the provider reports
  (3) canonical record               (canonical)  — base/default, via the join

Rules (:func:`merge_layers`):

* For each top-level field, take the value from the HIGHEST layer that defines it.
* ``capabilities`` is merged ONE LEVEL DEEP: each capability sub-field
  (``vision``, ``tools``, ``json_mode``, ``reasoning``, ``input_modalities``, …)
  is taken from the highest layer that defines THAT sub-field. This lets a
  provider model inherit ``reasoning`` from canonical while keeping its own other
  capabilities.
* Any nested object or list is taken WHOLESALE from the highest layer that defines
  it — never deep-merged or concatenated. In particular ``reasoning`` (the whole
  ``{supported, control, levels|budget_max}`` object) is replaced wholesale, and
  modality lists are replaced wholesale.

The merged record is then stripped of ``"canonical"`` and handed to the typed
``Model`` construction in ``models.py``. It MUST satisfy the loader's required
fields (``name``, ``capabilities`` incl. ``reasoning.supported``,
``context_window``, ``max_output_tokens``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# The JSON key that points a provider/override model entry at its canonical id.
# Internal join key only — stripped from the assembled record, never a Model
# attribute, never on the wire (GLOSSARY: canonical id "geht nie auf den Draht").
CANONICAL_POINTER_KEY = "canonical"

# Canonical layer files under ``<resources_dir>/models/``. Both may be absent
# (Phase 3 generates them); an absent file contributes an empty layer.
CANONICAL_FILE_NAME = "models.json"
CANONICAL_OVERRIDES_FILE_NAME = "models.overrides.json"

# The capabilities sub-object is the one place we merge a level deeper than the
# top level, so a provider model can inherit ``reasoning`` from canonical while
# overriding its own other capability sub-fields.
CAPABILITIES_FIELD = "capabilities"


def load_canonical_layer(models_dir: Path) -> dict[str, dict[str, Any]]:
    """Load and merge the canonical base + canonical overrides, keyed by canonical id.

    Reads ``models.json`` and applies ``models.overrides.json`` on top (override
    wins per field, nested objects wholesale — the same merge rule used between
    provider layers). Both files are optional: an absent ``models.json`` yields an
    empty layer; an absent overrides file leaves the base untouched. This is the
    DEFENSIVE behavior the handoff requires — Phase 3 has not yet generated these
    files, and assembly must still load every provider model without error.

    Args:
        models_dir: The ``<resources_dir>/models`` directory.

    Returns:
        A mapping ``canonical_id -> canonical record`` (plain dicts). Empty when
        no canonical file exists.
    """

    base = _read_models_map(models_dir / CANONICAL_FILE_NAME)
    overrides = _read_models_map(models_dir / CANONICAL_OVERRIDES_FILE_NAME)

    canonical: dict[str, dict[str, Any]] = {
        canonical_id: dict(record) for canonical_id, record in base.items()
    }
    for canonical_id, override_record in overrides.items():
        existing = canonical.get(canonical_id)
        if existing is None:
            canonical[canonical_id] = dict(override_record)
        else:
            canonical[canonical_id] = _merge_two(existing, override_record)
    return canonical


def resolve_canonical_id(
    wire_id: str,
    provider_model: Mapping[str, Any],
    override_model: Mapping[str, Any] | None,
    canonical_layer: Mapping[str, Any],
) -> str | None:
    """Resolve the canonical id for one provider model — deterministic, no fuzzy.

    Resolution order (handoff "Der kanonische Join"):

    1. Explicit ``canonical`` pointer wins — the manual pointer in the override
       layer beats the auto pointer in the provider layer.
    2. Else an exact canonical-id match: ``wire_id`` is itself a key in the
       canonical layer.
    3. Else ``None`` — no join (not an error; the model runs standalone).

    Args:
        wire_id: The provider wire-id (the per-provider file key).
        provider_model: The provider-layer model entry.
        override_model: The override-layer model entry, or ``None`` when absent.
        canonical_layer: The loaded canonical layer (canonical id -> record).

    Returns:
        The canonical id to join against, or ``None`` for no join.
    """

    explicit = None
    if override_model is not None:
        explicit = override_model.get(CANONICAL_POINTER_KEY)
    if explicit is None:
        explicit = provider_model.get(CANONICAL_POINTER_KEY)
    if isinstance(explicit, str) and explicit:
        return explicit

    if wire_id in canonical_layer:
        return wire_id

    return None


def assemble_provider_model(
    wire_id: str,
    provider_model: Mapping[str, Any],
    override_model: Mapping[str, Any] | None,
    canonical_layer: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the effective per-model record from the three layers + the join.

    Resolves the canonical id, then merges canonical (lowest) → provider →
    override (highest) field-by-field, and strips the internal ``canonical``
    pointer from the result. The returned dict is the input to the typed
    ``Model`` construction in ``models.py``.

    Args:
        wire_id: The provider wire-id.
        provider_model: The provider-layer model entry.
        override_model: The override-layer model entry, or ``None``.
        canonical_layer: The loaded canonical layer.

    Returns:
        The assembled effective model record (a plain dict), without the
        ``canonical`` pointer.
    """

    canonical_id = resolve_canonical_id(wire_id, provider_model, override_model, canonical_layer)
    canonical_record = canonical_layer.get(canonical_id) if canonical_id is not None else None

    layers = [layer for layer in (canonical_record, provider_model, override_model) if layer]
    merged = merge_layers(layers)
    merged.pop(CANONICAL_POINTER_KEY, None)
    return merged


def merge_layers(layers: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Merge layers low-to-high, "fill, don't overwrite", highest wins per field.

    ``layers`` is ordered lowest-precedence first. For every top-level field the
    value from the highest layer that defines it wins. ``capabilities`` is merged
    one level deep (each capability sub-field from its highest definer); every
    other nested object or list is taken wholesale.

    Args:
        layers: Layer records ordered from lowest to highest precedence.

    Returns:
        The merged record (a fresh plain dict; inputs are never mutated).
    """

    merged: dict[str, Any] = {}
    for layer in layers:
        merged = _merge_two(merged, layer)
    return merged


def _merge_two(low: Mapping[str, Any], high: Mapping[str, Any]) -> dict[str, Any]:
    """Field-level merge of two layers; ``high`` wins per field.

    ``capabilities`` is merged one level deep; all other fields (including any
    other nested object or list) are taken wholesale from whichever layer defines
    them, with ``high`` winning. Neither input is mutated.
    """

    result: dict[str, Any] = {key: _plain(value) for key, value in low.items()}
    for key, value in high.items():
        if (
            key == CAPABILITIES_FIELD
            and isinstance(value, Mapping)
            and isinstance(result.get(key), Mapping)
        ):
            # One-level-deep capabilities merge: each sub-field (vision, tools,
            # reasoning, modality lists, …) is replaced wholesale by the higher
            # layer, but sub-fields the higher layer omits are inherited.
            result[key] = {**result[key], **{k: _plain(v) for k, v in value.items()}}
        else:
            result[key] = _plain(value)
    return result


def _read_models_map(path: Path) -> dict[str, Any]:
    """Return the ``models`` map of a canonical file, or ``{}`` when absent.

    A missing file is the expected case before Phase 3 generates the canonical
    layer, so absence is not an error — it yields an empty layer.
    """

    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    models = data.get("models", {})
    if not isinstance(models, dict):
        raise ValueError(f"Canonical file '{path}' must contain a 'models' object")
    return models


def _plain(value: Any) -> Any:
    """Deep-copy a JSON value into plain dict/list/scalar containers.

    Keeps the merge non-mutating: nested mappings/lists are copied so an
    assembled record never aliases a layer's nested structure.
    """

    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value
