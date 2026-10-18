"""Provider catalog enrichment, Connection membership merge and JSON fact projection."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from core.models.assembly import REASONING_CONTROL_FIELDS
from core.models.models import (
    Model,
    ReasoningCapabilities,
)
from core.models.models_dev import (
    ModelsDevCatalog,
    ModelsDevModel,
)
from core.providers._wire_profile_files import WireProfileFile
from core.providers.adapter_types import ADAPTER_TYPES
from core.providers.providers import ConnectionConfig, ProviderConfig
from core.providers.wire_profiles import standalone_wire_binding
from core.utils.logging import get_logger

_LOGGER = get_logger("models.discovery")


def _project_provider_models(
    normalized_models: Mapping[str, Model],
    provider_config: ProviderConfig,
    catalog: ModelsDevCatalog | None,
    previous_models: Mapping[str, Mapping[str, Any]],
    *,
    previous_canonical: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Serialize provider models to data and enrich each from models.dev.

    Each model's wire facts are serialized (``_model_to_data``). When the
    models.dev catalog is available, the provider's section (looked up by
    ``models_dev_id``) supplies an **auto canonical pointer** for an exact
    wire-id match and a **deviating reasoning ladder** when the provider's own
    ``reasoning_options`` differ from the lab spec. No cross-file merge — this is
    the pure provider projection.

    When models.dev or its exact Provider entry is unavailable, a known wire id
    keeps the facts an earlier enrichment gave it (``previous_models``): an
    outage must not strip pricing, limits, canonical joins or the protocol hint.
    """

    projected: dict[str, dict[str, Any]] = {}
    models_dev_id = provider_config.effective_models_dev_id()
    for wire_id, model in normalized_models.items():
        data = _model_to_data(model)
        if wire_id in previous_models and (
            catalog is None or not catalog.model(models_dev_id, wire_id).present
        ):
            previous = previous_models[wire_id]
            canonical_id = previous.get("canonical") or wire_id
            canonical: Mapping[str, Any] | None
            if catalog is not None:
                canonical = catalog.models.get(canonical_id)
                canonical_support = (
                    canonical.get("reasoning") if isinstance(canonical, Mapping) else None
                )
            else:
                # Check the surviving layer only to decide whether inheritance
                # is still safe; never copy canonical overrides into discovery.
                canonical = previous_canonical.get(canonical_id)
                capabilities = (
                    canonical.get("capabilities") if isinstance(canonical, Mapping) else None
                )
                reasoning = (
                    capabilities.get("reasoning") if isinstance(capabilities, Mapping) else None
                )
                canonical_support = (
                    reasoning.get("supported") if isinstance(reasoning, Mapping) else None
                )
            _carry_forward_enrichment(
                data,
                previous,
                provider_config.id,
                inherit_reasoning=(
                    isinstance(canonical, Mapping)
                    and (
                        model.capabilities.reasoning.supported is not True
                        or canonical_support is True
                    )
                ),
            )
        elif catalog is not None:
            _enrich_provider_model(data, provider_config.id, catalog.model(models_dev_id, wire_id))
        projected[wire_id] = data
    return projected


def _carry_forward_enrichment(
    data: dict[str, Any], previous: Mapping[str, Any], provider_id: str, *, inherit_reasoning: bool
) -> None:
    """Keep what ``_enrich_provider_model`` added to ``previous``, under its own rules.

    Fill, don't overwrite: a fact the endpoint reported now wins over the
    earlier value, modalities only widen, and an adapter-reported reasoning
    control is kept. Otherwise the reasoning control description is taken as
    one unit from ``previous`` - including its absence when a canonical ladder
    can still flow through at load - while adapter facts outside it stay.
    """

    for key in ("canonical", "pricing"):
        if key in previous and key not in data:
            data[key] = copy.deepcopy(previous[key])
    for key in ("context_window", "max_output_tokens", "family"):
        if previous.get(key) and not data.get(key):
            data[key] = previous[key]

    metadata_key = _provider_metadata_key(provider_id)
    previous_metadata = previous.get("metadata")
    previous_facts = (
        previous_metadata.get(metadata_key) if isinstance(previous_metadata, Mapping) else None
    )
    if isinstance(previous_facts, Mapping):
        for fact in ("npm", "interleaved_field"):
            if fact in previous_facts:
                metadata = data.setdefault("metadata", {})
                provider_metadata = metadata.setdefault(metadata_key, {})
                provider_metadata.setdefault(fact, previous_facts[fact])

    capabilities = data.get("capabilities")
    previous_capabilities = previous.get("capabilities")
    if not isinstance(capabilities, dict) or not isinstance(previous_capabilities, Mapping):
        return
    for direction in ("input_modalities", "output_modalities"):
        earlier = previous_capabilities.get(direction)
        if isinstance(earlier, list) and set(capabilities.get(direction) or []) < set(earlier):
            capabilities[direction] = list(earlier)
            if direction == "input_modalities":
                capabilities["vision"] = "image" in earlier
    current = capabilities.get("reasoning")
    if isinstance(current, dict) and (
        current.get("supported") is False or current.get("control") is not None
    ):
        return
    adapter_facts = (
        {key: value for key, value in current.items() if key not in REASONING_CONTROL_FIELDS}
        if isinstance(current, dict)
        else {}
    )
    earlier_reasoning = previous_capabilities.get("reasoning")
    earlier_control = (
        {
            key: copy.deepcopy(value)
            for key, value in earlier_reasoning.items()
            if key in REASONING_CONTROL_FIELDS
        }
        if isinstance(earlier_reasoning, Mapping)
        else {}
    )
    if not earlier_control and not inherit_reasoning:
        # The surviving canonical record may be missing or contradict the
        # endpoint. Keep the fresh flag (including unknown) so this Model loads.
        return
    if (
        isinstance(current, dict)
        and current.get("supported") is True
        and earlier_control
        and earlier_control.get("supported") is not True
    ):
        return
    if earlier_control or adapter_facts:
        capabilities["reasoning"] = {**earlier_control, **adapter_facts}
    else:
        capabilities.pop("reasoning", None)


def _admitted_models(
    projected: dict[str, dict[str, Any]],
    normalized_models: Mapping[str, Model],
    provider_config: ProviderConfig,
    connection: ConnectionConfig | None,
    wire_files: Mapping[str, WireProfileFile] | None,
) -> dict[str, dict[str, Any]]:
    """Keep the Models the Provider's wire profile admits on this Connection.

    Admission is judged on the projected Model: a rule may depend on a fact
    enrichment just added, such as an exact catalog protocol hint. Missing
    compatibility evidence is not a denial. A restricted or retired Model is not
    written, so it never appears in the Model DB.
    """

    adapter_class = ADAPTER_TYPES.get(provider_config.adapter)
    if adapter_class is None:
        return projected

    def projected_model(model_id: str) -> Model | None:
        model = normalized_models.get(model_id)
        data = projected.get(model_id)
        if model is None or data is None:
            return None
        return replace(
            model,
            family=data.get("family") or model.family,
            metadata=data.get("metadata") or model.metadata,
        )

    binding = standalone_wire_binding(
        provider_id=provider_config.id,
        connection_id=connection.id if connection is not None else "",
        protocols=adapter_class.WIRE_PROTOCOLS,
        model_lookup=projected_model,
        files=wire_files,
    )
    admitted: dict[str, dict[str, Any]] = {}
    for model_id, data in projected.items():
        admission = binding.profile(model_id).admission
        if admission.state == "available":
            admitted[model_id] = data
            continue
        _LOGGER.debug(
            "Skipping model during discovery for provider '%s': %s",
            provider_config.id,
            admission.message or f"{model_id} is {admission.state}",
        )
    return admitted


def _provider_metadata_key(provider_id: str) -> str:
    """Return the provider-scoped metadata key for a vBot provider id.

    Hyphens are normalized to underscores so the key is a valid identifier and
    matches the adapter-side convention (e.g. ``opencode-go`` →
    ``opencode_go``, mirroring ``metadata.github_copilot``).
    """

    return provider_id.replace("-", "_")


def _enrich_provider_model(
    data: dict[str, Any],
    provider_id: str,
    facts: ModelsDevModel,
) -> None:
    """Stamp the auto canonical pointer + deviating ladder + wire metadata.

    Mutates ``data`` in place:

    * adds a top-level ``canonical`` pointer for an exact lab-section wire-id
      match;
    * fills ``context_window`` / ``max_output_tokens`` / ``family`` from the
      provider's models.dev section when the endpoint left them empty — a
      gateway endpoint (opencode-go) returns bare ids with no limits, but
      models.dev carries the provider-specific limits; "fill, don't overwrite",
      so a provider that DID report a limit keeps its own;
    * projects the models.dev ``interleaved`` response field into
      ``metadata.<provider>.interleaved_field`` (Phase 5) when present;
    * projects the Model's own AI SDK package (models.dev per-model
      ``provider.npm``) into ``metadata.<provider>.npm``, the catalog protocol
      hint of wire profiles;
    * keeps an endpoint-reported reasoning denial or control; otherwise fills
      a missing control from models.dev's ladder when it *deviates* from the lab
      spec (the provider layer wins at load);
    * when the model joins the canonical layer and does NOT deviate, keeps an
      adapter-reported control description (one with a ``control``) and drops
      an unknown or matching positive ``supported`` flag, so the richer canonical
      ladder is inherited at load. An explicit endpoint flag never yields to a
      contradictory canonical flag.

    Reasoning facts outside the control description (``mandatory``) are always
    kept: they are adapter facts that no models.dev or canonical source carries.
    """

    pointer = facts.canonical_pointer
    if pointer is not None:
        data["canonical"] = pointer

    # A price from the Provider's own catalog wins over its models.dev mirror.
    pricing = facts.pricing
    if pricing is not None and "pricing" not in data:
        data["pricing"] = pricing

    # Fill the limits the endpoint did not report from the provider's models.dev
    # section (the gateway's own limits). "Fill, don't overwrite": a falsy value
    # (None / 0) means the endpoint gave nothing, so the models.dev value wins; a
    # real endpoint limit is kept.
    context_window, max_output_tokens = facts.limits
    if context_window is not None and not data.get("context_window"):
        data["context_window"] = context_window
    if max_output_tokens is not None and not data.get("max_output_tokens"):
        data["max_output_tokens"] = max_output_tokens
    if not data.get("family"):
        family = facts.family
        if family is not None:
            data["family"] = family

    # Widen the modalities from the provider's models.dev section, but only as a
    # strict SUPERSET of what the endpoint reported (add, never drop) — a bare
    # gateway endpoint defaults to text-only while models.dev knows the real
    # image/video inputs; ``vision`` is kept consistent with the input list.
    modalities = facts.modalities
    capabilities = data.get("capabilities")
    if modalities is not None and isinstance(capabilities, dict):
        md_input, md_output = modalities
        if md_input and set(capabilities.get("input_modalities") or []) < set(md_input):
            capabilities["input_modalities"] = md_input
            capabilities["vision"] = "image" in md_input
        if md_output and set(capabilities.get("output_modalities") or []) < set(md_output):
            capabilities["output_modalities"] = md_output

    response_field = facts.interleaved_field
    if response_field is not None:
        metadata = data.setdefault("metadata", {})
        if isinstance(metadata, dict):
            provider_metadata = metadata.setdefault(_provider_metadata_key(provider_id), {})
            if isinstance(provider_metadata, dict):
                provider_metadata["interleaved_field"] = response_field

    npm = facts.npm
    if npm is not None:
        metadata = data.setdefault("metadata", {})
        if isinstance(metadata, dict):
            provider_metadata = metadata.setdefault(_provider_metadata_key(provider_id), {})
            if isinstance(provider_metadata, dict):
                provider_metadata["npm"] = npm

    deviating = facts.reasoning_block
    capabilities = data.get("capabilities")
    if not isinstance(capabilities, dict):
        return
    current = capabilities.get("reasoning")
    if isinstance(current, dict) and (
        current.get("supported") is False or current.get("control") is not None
    ):
        return
    adapter_facts = (
        {key: value for key, value in current.items() if key not in REASONING_CONTROL_FIELDS}
        if isinstance(current, dict)
        else {}
    )
    if deviating is not None:
        if (
            isinstance(current, dict)
            and current.get("supported") is True
            and deviating.get("supported") is not True
        ):
            return
        capabilities["reasoning"] = {**deviating, **adapter_facts}
        return
    current_supported = current.get("supported") if isinstance(current, dict) else None
    md_reasoning = facts.reasoning_supported
    # No deviation: let the canonical ladder flow through at load by dropping an
    # unknown or matching positive flag, only at an existing canonical join.
    # A reported control or negative flag has already been kept above.
    if facts.canonical_id is not None:
        canonical_support = facts.canonical_reasoning_supported
        reported_support = current_supported if current_supported is not None else md_reasoning
        if reported_support is not None and reported_support is not canonical_support:
            capabilities["reasoning"] = {**adapter_facts, "supported": reported_support}
            return
        if adapter_facts:
            capabilities["reasoning"] = adapter_facts
        else:
            capabilities.pop("reasoning", None)
        return
    # No deviating control AND no canonical base to inherit from (a gateway model
    # whose wire-id cannot reach the canonical layer): still project the bare
    # models.dev ``reasoning`` flag, so a model the feed marks reasoning-capable
    # but with no published control ladder is not flattened to the endpoint's
    # unknown support. The effort then snaps against the adapter floor.
    if md_reasoning is not None and current_supported is None:
        capabilities["reasoning"] = {**adapter_facts, "supported": md_reasoning}


def _read_existing_provider_models(output_path: Path) -> dict[str, dict[str, Any]]:
    """Return the existing ``<provider>.json`` ``models`` mapping, or empty.

    A missing file, an unreadable payload, or an entry that is not a mapping
    is treated as "no existing models" so a single bad write never blocks a
    future merge.
    """

    if not output_path.exists():
        return {}
    try:
        payload = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _LOGGER.warning(
            "Could not read existing provider catalog %s for merge: %s",
            output_path,
            exc,
        )
        return {}
    models = payload.get("models") if isinstance(payload, Mapping) else None
    if not isinstance(models, dict):
        return {}
    return {
        model_id: dict(model_data)
        for model_id, model_data in models.items()
        if isinstance(model_data, Mapping)
    }


def _merge_models_by_connection(
    existing_models: Mapping[str, dict[str, Any]],
    fresh_models: Mapping[str, dict[str, Any]],
    connection_id: str | None,
    *,
    capability_defaults: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Combine fresh discovery output with existing catalog models.

    Models are partitioned by the ``connections`` allowlist. A refresh replaces
    only the selected Connection's membership and preserves every other
    membership on the same wire id; a fresh shared entry unions those retained
    memberships back in, retaining their exact Context limits. Capability facts
    remain Provider-wide: identity-only listings do not replace them with their
    unchanged normalizer defaults. Fresh reported/enriched fields still win.
    Passing ``connection_id=None`` (no selected Connection)
    falls back to a full overwrite for backward compatibility with
    non-Connection-aware callers.
    """

    merged: dict[str, dict[str, Any]] = {}
    if connection_id is None:
        merged.update(fresh_models)
        return merged

    for model_id, model_data in existing_models.items():
        model_connections = model_data.get("connections")
        if not isinstance(model_connections, list) or connection_id not in model_connections:
            merged[model_id] = dict(model_data)
            continue

        remaining_connections = [item for item in model_connections if item != connection_id]
        if remaining_connections:
            retained = dict(model_data)
            retained["connections"] = remaining_connections
            _set_connection_contexts(
                retained, _connection_contexts(model_data, remaining_connections)
            )
            merged[model_id] = retained

    for model_id, fresh_model in fresh_models.items():
        data = dict(fresh_model)
        retained_model = merged.get(model_id)
        retained_connections = retained_model.get("connections") if retained_model else None
        fresh_connections = data.get("connections")
        if (
            retained_model is not None
            and isinstance(retained_connections, list)
            and isinstance(fresh_connections, list)
        ):
            data["connections"] = list(dict.fromkeys([*retained_connections, *fresh_connections]))
            contexts = _connection_contexts(retained_model, retained_connections)
            contexts.update(_connection_contexts(fresh_model, fresh_connections))
            _set_connection_contexts(data, contexts)
        elif isinstance(fresh_connections, list) and "connection_context_windows" in data:
            _set_connection_contexts(data, _connection_contexts(data, fresh_connections))
        previous = existing_models.get(model_id)
        baseline = (capability_defaults or {}).get(model_id)
        if previous is not None and baseline is not None:
            previous_capabilities = previous.get("capabilities")
            fresh_capabilities = data.get("capabilities")
            if isinstance(previous_capabilities, Mapping) and isinstance(
                fresh_capabilities, Mapping
            ):
                data["capabilities"] = {
                    key: copy.deepcopy(previous_capabilities[key])
                    if key != "task_types"
                    and value == baseline.get(key)
                    and key in previous_capabilities
                    else value
                    for key, value in fresh_capabilities.items()
                }
        merged[model_id] = data
    return merged


def _connection_contexts(data: Mapping[str, Any], connections: list[str]) -> dict[str, int | None]:
    """Capture effective limits before another Connection changes the fallback."""

    specific = data.get("connection_context_windows")
    limits = specific if isinstance(specific, Mapping) else {}
    result: dict[str, int | None] = {}
    for connection in connections:
        value = limits.get(connection, data.get("context_window"))
        result[connection] = (
            value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
        )
    return result


def _models_for_connection_enrichment(
    models: Mapping[str, dict[str, Any]], connection_id: str | None
) -> Mapping[str, dict[str, Any]]:
    """Carry the refreshed Connection's earlier limit, never a sibling's fallback."""

    if connection_id is None:
        return models
    result: dict[str, dict[str, Any]] = {}
    for model_id, data in models.items():
        connections = data.get("connections")
        context = (
            None
            if isinstance(connections, list) and connections and connection_id not in connections
            else _connection_contexts(data, [connection_id])[connection_id]
        )
        result[model_id] = {**data, "context_window": context}
    return result


def _set_connection_contexts(data: dict[str, Any], contexts: Mapping[str, int | None]) -> None:
    """Keep exact Connection limits and an honest conservative Model fallback."""

    known = {connection: limit for connection, limit in contexts.items() if limit is not None}
    common = min(known.values()) if known and len(known) == len(contexts) else None
    data["context_window"] = common
    exceptions = {connection: limit for connection, limit in known.items() if limit != common}
    if exceptions:
        data["connection_context_windows"] = exceptions
    else:
        data.pop("connection_context_windows", None)


def _tag_fresh_models(
    fresh_models: Mapping[str, dict[str, Any]],
    connection_id: str | None,
) -> dict[str, dict[str, Any]]:
    """Stamp every fresh model dict with ``connections: [connection_id]``.

    Each refreshed entry is scoped to the connection that produced it so a
    later refresh of a *different* connection can replace only its own
    entries. When no connection is selected (``connection_id=None``) the
    fresh models are returned untouched, which keeps the merge step a
    full overwrite in that case.
    """

    if connection_id is None:
        return dict(fresh_models)
    tagged: dict[str, dict[str, Any]] = {}
    for model_id, model_data in fresh_models.items():
        data = dict(model_data)
        data["connections"] = [connection_id]
        tagged[model_id] = data
    return tagged


def _model_to_data(model: Model | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(model, Model):
        data: dict[str, Any] = {
            "name": model.name,
            "capabilities": {
                "vision": model.capabilities.vision,
                "tools": model.capabilities.tools,
                "json_mode": model.capabilities.json_mode,
                "reasoning": _reasoning_to_data(model.capabilities.reasoning),
                "input_modalities": list(model.capabilities.input_modalities),
                "output_modalities": list(model.capabilities.output_modalities),
                "supported_parameters": list(model.capabilities.supported_parameters),
                "supported_voices": list(model.capabilities.supported_voices),
                "task_types": list(model.capabilities.task_types),
            },
            "context_window": model.context_window,
            "max_output_tokens": model.max_output_tokens,
        }
        task_options = _plain_data(model.capabilities.task_options)
        if task_options:
            data["capabilities"]["task_options"] = task_options
        # ``family`` is written only when known, mirroring how connections /
        # metadata are omitted when empty so generated catalogs stay clean.
        if model.family:
            data["family"] = model.family
        if model.pricing is not None:
            data["pricing"] = model.pricing.to_dict()
        if model.connections:
            data["connections"] = list(model.connections)
        if model.connection_context_windows:
            data["connection_context_windows"] = dict(model.connection_context_windows)
        metadata = _plain_data(model.metadata)
        if metadata:
            data["metadata"] = metadata
        return data
    data = dict(model)
    connections = data.get("connections")
    if connections is not None and not isinstance(connections, list):
        raise ValueError("connections must be a list when set")
    if isinstance(connections, list) and not connections:
        data.pop("connections", None)
    return data


def _reasoning_to_data(reasoning: ReasoningCapabilities) -> dict[str, Any]:
    """Serialize the typed reasoning block, omitting unset optional fields.

    ``control``/``levels``/``budget_max`` are emitted only when present and
    ``mandatory`` only when true, so the on-disk form stays minimal — a model
    with no projected ladder serializes back to the bare ``{"supported": bool}``
    shape, matching "absent when not supported (or not yet known)".
    """

    data: dict[str, Any] = {"supported": reasoning.supported}
    if reasoning.control is not None:
        data["control"] = reasoning.control
    if reasoning.levels:
        data["levels"] = list(reasoning.levels)
    if reasoning.budget_max is not None:
        data["budget_max"] = reasoning.budget_max
    if reasoning.mandatory:
        data["mandatory"] = True
    return data


def _plain_data(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain_data(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain_data(item) for item in value]
    return value
