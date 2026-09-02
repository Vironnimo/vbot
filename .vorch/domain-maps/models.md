# Models

`core/models/` owns the Model DB: layered on-disk model facts, the **at-load assembly** turning them into effective models, the registry read path, and the typed model contract runtime and accessors consume. A loaded model is always a model at one provider; the canonical id (`lab/model`) is an internal join key used only during assembly - it never goes on the wire.

## Overview - two times, one rule

The system splits cleanly into two moments:

- **Refresh** (`discovery.py` + `models_dev.py`) - the DUMB half. Fetches provider `/models`, the public models.dev `catalog.json`, and any adapter-declared task-capability catalog, projecting results to disk per file - no cross-file merges, no provider joins. Needs network plus a credential for provider catalogs; rare and explicit. Normal `model.refresh_db` starts from a complete copy of the active DB and atomically publishes under `<data_dir>/artifacts/models/`; `scripts/refresh_model_db.py` is the maintainer-only path publishing the same shape to `resources/models/`. Where a `/models` endpoint omits facts, refresh fills them from that provider's own models.dev section ("fill, don't overwrite"); advertised-but-unusable ids drop via `ProviderConfig.catalog_exclusions`. Per-model enrichment (`enrich_discovered_models`, e.g. Ollama `POST /api/show`) and task-capability catalogs (e.g. OpenRouter image/video APIs) are adapter-declared hooks merging typed facts after normalization; failures degrade per model/catalog, never failing the refresh.
- **Load** (`assembly.py` behind `ModelRegistry.load`) - the SMART half. Selects the newer schema-compatible generated root by manifest, then assembles each effective model in memory from generated data plus current bundled Overrides, resolving the canonical join and field-level merge, finally overlaying normalized manual Custom Provider models. No network, no key, frequent. Invalid layer files or individual records log-and-omit independently; a missing runtime manifest falls back to system.

The rule tying them: **Refresh publishes one complete snapshot; Load selects one generated catalog and applies current bundled Overrides.** Overrides apply at Load, never baked into generated files; generated files are not a manual edit surface, while bundled `resources/models/*.overrides.json` files are the hand-correction surface discovered by filename without registration.

## Terms

Domain vocabulary. Core terms (Provider, Model, Reasoning) live in `.vorch/GLOSSARY.md`.

### Canonical id
**Definition:** A models.dev-style `lab/model` identifier naming a model independent of any provider - purely an internal join/DB key during assembly, **never sent on the wire**. A provider model reaches its canonical base via explicit `canonical` pointer or exact wire-id match; resolution is deterministic only.
**Not:** The wire model-id. A missed join is not an error - the model runs on provider + override data.

### Refresh
**Definition:** The DUMB half: fetch feeds, project per file inside an isolated complete copy, publish atomically after validation. Network + credential; rare and explicit.
**Not:** Load. Refresh writes disk from the network; Load selects and assembles without fetching.

### Load
**Definition:** The SMART half (`ModelRegistry.load`): select newer compatible root, assemble canonical/provider projections with current Overrides in memory. No network, frequent (startup, cache invalidation).
**Not:** Refresh, or mixing generated files across roots.

### Reasoning control
**Definition:** How a provider steers reasoning on the wire - `levels` (effort ladder), `on_off` (toggle), or `budget` (token budget with max); derived at refresh from models.dev `reasoning_options` into `capabilities.reasoning.control`.
**Not:** The agent's `thinking_effort` setting - control is the capability, effort snaps against the ladder at request time (wiring in `providers/request-policy.md`).

## Complete Model DB snapshots

System root is the tracked `resources/models/`; runtime root is `<data_dir>/artifacts/models/`. Each snapshot holds generated catalogs, raw inspection dumps, a bundled Override snapshot, plus `manifest.json` (`schema_version`, UTC `refreshed_at`, source). Load compares manifests and picks the newer entire root (system wins ties; incompatible/missing runtime manifest falls back to system), then applies current bundled Overrides so an older runtime snapshot cannot hide update changes. Both refresh targets stage and validate before atomic directory replacement - a failed fetch/publish leaves the previous root intact. Runtime refresh starts from the active snapshot and replaces staged overrides with the complete current bundled set before discovery writes, preserving additions and removing deletions.

## The three layers

| Layer | Files | Keyed by | Written by | Holds |
|---|---|---|---|---|
| Canonical | selected-root `models.json` + bundled `models.overrides.json` | canonical id | Refresh / hand | provider-agnostic base: `name`, `family`, `capabilities`, `context_window`, `max_output_tokens`. **No `provider_id`.** |
| Provider | selected-root `<provider>.json` + bundled `<provider>.overrides.json` | wire-id | Refresh / hand | what the provider reports, incl. deviating reasoning ladders, optional `canonical` pointer, wire `metadata` |
| Adapter fallbacks | adapter code | - | - | send-time defaults |

Both canonical inputs may be absent - assembly runs on Provider + Override alone. The `canonical` JSON key is an internal join key: stripped from assembled records, never a `Model` attribute.

## At-load assembly

`ModelRegistry.load` is the single public read surface; everything in `assembly.py` hides behind it (its module docstring is the file-format source of truth).

**The deterministic join** (`resolve_canonical_id`, no fuzzy matching ever): explicit `canonical` pointer wins (manual beats auto); else exact wire-id-in-canonical-keys match; else no join - enrichment, not dependency.

**The merge** (`merge_layers`, "fill, don't overwrite", highest layer wins per top-level field): override > provider > canonical. Only `capabilities` merges **one level deep** (per sub-field); every other nested object/list replaces wholesale (including `reasoning` and `metadata` blobs - an override needing generated metadata must repeat it explicitly). A `null` never counts as defining a field, so it cannot un-fill lower layers. Records failing required fields (`name`, `capabilities`, `reasoning.supported`) omit individually without hiding siblings or aborting startup. The offline validator (`scripts/validate_model_db.py`) reports dead pointers and redundant manual joins - not part of the read path.

## Typed reasoning

`capabilities.reasoning` is typed: `supported` (the load-bearing flag), `control` (`levels`/`on_off`/`budget`), `levels`, `budget_max`. Derived at refresh from `reasoning_options` - effort option wins over budget over toggle; the canonical ladder lifts from the lab provider only, never unioned across providers; a deviating provider gets its ladder stamped on its provider file while a conforming one inherits canonically. Request-time wiring turns `(control, thinking_effort)` into a neutral intent each adapter renders - full wiring in `providers/request-policy.md`.

## Wire selectors and context windows

Per-model wire **facts** live in the Provider-scoped `metadata` blob keyed by underscored provider id (e.g. `metadata.opencode_go.protocol`); replay *scope* is the cross-provider exception configured through overrides (`Model.reasoning_replay`, root provider override) with precedence owned by `providers.md`. `metadata` is frozen after load and limited to wire facts - never payloads, policies, or secrets.

`context_window`/`max_output_tokens` are `int | None`: absent means honestly unknown, never faked. `connection_context_windows` is the optional per-Connection exception when one wire exposes a different Context limit for the same Provider Model id; `Model.context_window_for(connection_id)` returns that exact value or the Model-wide fallback. Chat resolves it from the Connection actually selected for the Run, and Adapters with Connection-dependent output budgeting use the same value. Other readers resolve through the shared chain `resolve_context_window(model value -> provider-config default -> global floor)` living at Provider-config level; non-positive values count as unknown. Flagged-local models (`metadata.<provider>.local`) resolve through `resolve_effective_context_window`: user setting from `local_models.context_windows`, else capped at `LOCAL_CONTEXT_DEFAULT_CAP` - a local endpoint reports theoretical max, not hardware reality; enforced on the wire (Ollama `num_ctx`), in compaction budgets, `/status`, and picker filtering. Connections with `auto_refresh: true` get a throttled background sweep republishing without models.dev fetches, recording reachability (semantics in `providers/catalog-discovery.md`). `recommended_temperature` is a vendor-documented fallback applied only when the Agent sets none, coerced to [0.0, 2.0].

## Interfaces

- Frozen dataclasses (`Model`, `Capabilities`, `ReasoningCapabilities`); `Capabilities.task_options` holds typed task option specs merged one level deep like other capabilities sub-fields, consumed by `model_tasks.md` option builders.
- `Model.model_id` is the exact wire string - no remapping anywhere. `Model.connections` binds a model to a subset of connection ids (empty = all); `allows_connection(id)` is the single source read by target expansion, WebUI filtering, and save guards. `Model.connection_context_windows` is a frozen positive-int map for the rare case where those allowed Connections expose different Context limits.
- `ModelRegistry.load/reload` select one root, assemble with overrides, overlay Custom Provider models (a manual Custom Model wins same-id collisions). **Reload updates in place** so services holding the registry see refreshed catalogs without restart - do **not** rebind `runtime._models` to a fresh `load()`; that rebind was the bug (chat fresh, specialized targets stale).
- `query(model_query)` is the pure filtered read path; credential gating happens outside it (RPC `model.list`, task-target discovery). `get(provider, model)` raises on unknown pairs; `list_for_provider` sorts ascending.
- Bundled-only registries cache by database locations; supplying `custom_providers` always returns an isolated registry instead of touching the shared cache.

## Capabilities & Tasks

Capabilities are facts about one model through one provider. `task_types` derives from modalities for coarse routing; sparse catalogs default text-in/text-out so conservative providers stay selectable. Speech/audio aliases are intentionally strict: `transcription` -> text+STT, `speech` -> TTS+audio-generation, generic `audio` -> `audio_generation` only (never `text_to_speech`), `embeddings` -> `text_embedding`.

## Constraints & Gotchas

- **Code wins** - `assembly.py`'s module docstring is the load contract; fix this map when they disagree.
- **Refresh is dumb, Load is smart** - never push merge/join logic into Refresh, never make Load fetch.
- The full canonical mirror is intentionally unfiltered; do not add discovery defaults.
- `metadata` replaces wholesale at load (unlike one-level-deep `capabilities`).
- Override-only models/providers work and must supply loader-required fields.
- An override entry for a wire-id that is missing from the generated `<provider>.json` is dropped with "Ignoring invalid Model DB model ..." - a pin-only entry is valid only when the model exists in the generated file. Fix by refreshing the Model DB (`scripts/refresh_model_db.py`), never by duplicating required fields into the override; overrides carry only what they override (user rule 2026-08-26).
- Models are immutable after load - change layer files, then invalidate/reload; never mutate a loaded `Model`.
