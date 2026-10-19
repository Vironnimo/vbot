# Models

`core/models/` owns the Model DB: layered on-disk model facts, the **at-load assembly** turning them into effective models, the registry read path, and the typed model contract runtime and accessors consume. A loaded model is always a model at one provider; the canonical id (`lab/model`) is an internal join key used only during assembly - it never goes on the wire.

## Overview - two times, one rule

- **Refresh** (`discovery.py` + `models_dev.py`) - the DUMB half. Fetches provider `/models`, the public models.dev `catalog.json`, and any adapter-declared task-capability catalog, projecting results to disk per file - no cross-file merges, no provider joins. Needs network plus a credential for provider catalogs; rare and explicit. `model.refresh_db` publishes the catalogs this installation fetched under `<data_dir>/artifacts/models/`; `scripts/refresh_model_db.py` is the maintainer-only path publishing the complete DB to `resources/models/`. Refresh fills facts a `/models` endpoint omits from that provider's own models.dev section ("fill, don't overwrite") and writes only Models the wire profile admits (`models/refresh.md` -> Refresh pipeline).
- **Load** (`assembly.py` behind `ModelRegistry.load`) - the SMART half. Selects each generated catalog file from the root whose manifest records the later fetch, then assembles each effective model in memory from generated data plus current bundled Overrides, resolving the canonical join and field-level merge, finally overlaying normalized manual Custom Provider models. No network, no key, frequent. Invalid layer files or individual records log-and-omit independently; a missing or incompatible runtime manifest falls back to system for every catalog.

The rule tying them: **Refresh records which catalogs it fetched; Load takes each generated catalog from the root that fetched it last and applies current bundled Overrides.** Overrides apply at Load, never baked into generated files; generated files are not a manual edit surface, while bundled `resources/models/*.overrides.json` files are the hand-correction surface discovered by filename without registration.

Roots: the tracked system root `resources/models/` is complete for shared Providers (their generated catalogs, all bundled Overrides, `manifest.json`); the runtime root `<data_dir>/artifacts/models/` holds only the catalogs this installation fetched plus its manifest. So a vBot update's newer bundled catalogs win over older runtime copies, while catalogs only this installation produces (Custom Providers, local servers) keep coming from the runtime root. Refresh stages and validates before atomic directory replacement; a failure leaves the previous root intact (`models/refresh.md`).

## Terms

Domain vocabulary. Core terms (Provider, Model, Reasoning) live in `.vorch/GLOSSARY.md`.

### Canonical id
**Definition:** A models.dev-style `lab/model` identifier naming a model independent of any provider - purely an internal join/DB key during assembly, **never sent on the wire**. A provider model reaches its canonical base via explicit `canonical` pointer or exact wire-id match; resolution is deterministic only.
**Not:** The wire model-id. A missed join is not an error - the model runs on provider + override data.

### Refresh
**Definition:** The DUMB half: fetch feeds, project per file inside an isolated copy of the effective DB, record the fetched catalogs, publish atomically after validation. Network + credential; rare and explicit.
**Not:** Load. Refresh writes disk from the network; Load selects and assembles without fetching.

### Load
**Definition:** The SMART half (`ModelRegistry.load`): select each generated catalog file from the root that fetched it last, assemble canonical/provider projections with current Overrides in memory. No network, frequent (startup, cache invalidation).
**Not:** Refresh, or merging two copies of one catalog file: roots mix per file, never within one.

### Reasoning control
**Definition:** How a provider steers reasoning on the wire - `levels` (effort ladder), `on_off` (toggle), or `budget` (token budget with max); derived at refresh from models.dev `reasoning_options` into `capabilities.reasoning.control`.
**Not:** The agent's `thinking_effort` setting - control is the capability, effort snaps against the ladder at request time (wiring in `providers/request-policy.md`).

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

**The merge** (`merge_layers`, "fill, don't overwrite", highest layer wins per field): override > provider > canonical. `capabilities` merges **one level deep** (per sub-field). Inside `capabilities.reasoning` the control description (`supported`, `control`, `levels`, `budget_max`) is **one unit** from the highest layer defining any of those keys - a ladder never mixes layers, and a higher `{"supported": true}` does not inherit a lower ladder - while every other reasoning fact (`mandatory`) merges per field. `metadata` merges **per Provider key, then per field**, so an override states only the fields it corrects. Every other nested object/list (modality lists, `pricing`, `connections`, `task_options`, a metadata field's value) replaces wholesale. A `null` never counts as defining a field at any level, so it cannot un-fill lower layers. Records failing required fields (`name`, `capabilities` with `vision`, `tools`, `json_mode` and `reasoning.supported`) omit individually without hiding siblings or aborting startup; the warning names the missing field path (`missing required field 'capabilities'`). The offline validator (`scripts/validate_model_db.py`) reports dead pointers and redundant manual joins - not part of the read path.

## Typed reasoning

`capabilities.reasoning` is typed: `supported` (the load-bearing flag; `null` means unknown - a generic Chat Completions listing without any reasoning signal stores `null`, so a lower layer or the wire profile decides and requests render the selected effort, while only an explicit `false` denies support), `control` (`levels`/`on_off`/`budget`), `levels`, `budget_max`, and `mandatory` (the Provider always reasons and rejects disabling it; default `false`, only valid with `supported: true` - a non-boolean or unsupported `mandatory` is reported and ignored). Refresh derives it from models.dev `reasoning_options` and Adapter-reported controls (`models/refresh.md` -> Typed reasoning derivation). The wire profile's catalog layer reads `mandatory` from the capability (`core/providers/wire_profiles.py`); request-time wiring turns `(control, thinking_effort)` into a neutral intent each adapter renders - full wiring in `providers/request-policy.md`.

## Wire selectors and context windows

Per-model **facts a Provider or catalog reports** live in the Provider-scoped `metadata` blob keyed by underscored provider id (e.g. `metadata.github_copilot.supported_endpoints`, the models.dev `interleaved` projection `metadata.opencode_go.interleaved_field`, the protocol hint `metadata.opencode_zen.npm`), frozen after load and limited to reported facts - never payloads, policies, or secrets. Every wire **decision** - protocol, admission, reasoning controls and carriers, replay scope, media types, whether announced Tools are listed in `tools[]` - lives in the wire profile (`providers.md` -> Boundaries and invariants), never in Model data or hand overrides; overrides correct catalog facts only. Model facts enter a wire profile only through its catalog layer (`providers/wire-profiles.md` -> Profile layers), for example a manual Custom Model's Reasoning switch: off makes that layer set `reasoning.supported: false`. The retired wire keys `reasoning_replay` (override root or Model) and `capabilities.unlisted_tool_calls` are unknown fields that load ignores.

`context_window`/`max_output_tokens` are `int | None`: absent means honestly unknown, never faked. `connection_context_windows` is the optional per-Connection exception when one wire exposes a different Context limit for the same Provider Model id; `Model.context_window_for(connection_id)` returns that exact value or the Model-wide fallback (how refresh maintains it: `models/refresh.md` -> Per-Connection Context windows). At Load, a null limit still inherits a canonical Model limit. Chat resolves the window from the Connection selected for the Run, and Adapters with Connection-dependent output budgeting use the same value. Other readers and flagged-local Models (`metadata.<provider>.local`) resolve through the Provider-level chain and local cap (`providers/request-policy.md` -> Request limits and context). `recommended_temperature` / `recommended_top_p` are vendor-documented values, coerced to [0.0, 2.0] / [0.0, 1.0]; they are shown (`/status`; `model.list`/`model.get` project them for the WebUI sampling settings) and never sent - an unset Agent value leaves sampling to the Provider's own default.

## Interfaces

- `models.py` owns the registry and public imports of frozen `Model`, `Capabilities`, and `ReasoningCapabilities`; `_model_record.py` owns records, validation and task derivation. `Capabilities.task_options` holds typed task option specs merged one level deep like other capabilities sub-fields, consumed by `model_tasks.md` option builders. Parameter spec types: `enum`, `range`, `boolean`, and `string`; a spec may carry an optional `default`.
- `Model.model_id` is the exact wire string - no remapping anywhere. `Model.connections` binds a model to a subset of connection ids (empty = all); `allows_connection(id)` is the single source read by target expansion, `model.list`, the Extension host `catalog()`, WebUI filtering, and save guards.
- `ModelRegistry.load/reload` select each catalog file's root, assemble with overrides, overlay Custom Provider models (a manual Custom Model wins same-id collisions). **Reload updates in place** so services holding the registry see refreshed catalogs without restart - do **not** rebind `runtime._models` to a fresh `load()` (that bug left Chat fresh and specialized targets stale). `reload_async` is the Event-Loop-safe variant: it assembles on the `model-registry` worker pool and swaps the contents on the calling loop, so loop readers never see a half-replaced registry (generation guard and change result: `models/refresh.md` -> Reload concurrency).
- `query(model_query)` is the pure filtered read path; credential gating happens outside it. `ModelQuery.search_terms` (RPC/CLI `query`, split at whitespace) requires every term as a case-insensitive substring of the Model id, name or family. `get(provider, model)` raises on unknown pairs; `list_for_provider` sorts ascending.
- `Model.pricing` is optional immutable `TokenPricing` (`core/models/pricing.py`); `ModelRegistry.pricing_for(bare_or_qualified_reference)` returns the effective exact price or None, and Chat and Compaction snapshot the computed cost in canonical Usage (sources, tiers, computation: `models/pricing.md`).
- `model.list` and `model.get` (`server/rpc/model_methods.py`) add per-Connection `wire_profiles` statuses and, on `model.get`, the resolved wire facts (`providers/wire-profiles.md` -> Wire reads and learned facts).
- Bundled-only registries cache by database locations; supplying `custom_providers` always returns an isolated registry instead of touching the shared cache.

## Capabilities & Tasks

Capabilities are facts about one model through one provider. `task_types` derives from modalities for coarse routing; sparse catalogs default text-in/text-out so conservative providers stay selectable. Speech/audio aliases are intentionally strict: `transcription` -> text+STT, `speech` -> TTS+audio-generation, generic `audio` -> `audio_generation` only (never `text_to_speech`), `embeddings` -> `text_embedding` (embedding Models normalize through `text_embedding_capabilities()` and derive no chat task, `providers/catalog-discovery.md`), and the output modality `decisions` derives only the `decision` task type, never Chat eligibility (`model_tasks/decisions.md`). `live_voice` is explicit-only: no modality combination derives it, so voice Models declare it in `task_types` through overrides. Because enrichment and overrides may widen a generated catalog's modalities later, Load (`_settle_task_types` in `core/models/_model_record.py`) re-derives the input tasks (`INPUT_TASK_TYPES`: `image_input`, `image_understanding`, `file_*`, `audio_input`, `video_*`) from the assembled modalities and keeps every other task as the catalog lists it, because those name Provider routes (transcription, image generation, embeddings) that widened modalities do not create. An override's `task_types` is a complete hand list and stays as written.

## Constraints & Gotchas

- **Refresh is dumb, Load is smart** - never push merge/join logic into Refresh, never make Load fetch.
- The full canonical mirror is intentionally unfiltered; do not add discovery defaults.
- `MODEL_DATABASE_SCHEMA_VERSION` (`core/models/database.py`, mirrored in the bundled `manifest.json`) changes when Load starts reading a fact older generated roots lack or the manifest format changes - no fallback branch reads an old field. An incompatible runtime root is ignored, so every catalog comes from the system root (v2: `capabilities.reasoning.mandatory` and the `catalogs` records) until the startup restore rebuilds the installation's catalogs (`providers/catalog-discovery.md` -> Startup restore of installation catalogs).
- A bundled generated `<provider>.json` can be a hand-curated discovery seed (`nous.json`): the bundled manifest does not record it, so any runtime copy a refresh fetched wins. An Override entry always creates its Model, so account-dependent availability keeps a seed in the generated layer (`providers/nous.md`), while an allowlist fixed in code keeps its hand catalog in overrides (`providers/stepfun.md`).
- Override-only models/providers load when assembly supplies `name` and `capabilities.reasoning.supported`, directly or through a canonical join; otherwise the "Ignoring invalid Model DB model ..." warning (like every Load issue, logged once per process per distinct message) marks the entry Override-only - typically a Model the Provider catalog no longer lists, whose stale Override should be removed. Keep overrides minimal instead of duplicating inherited fields.
- Directory enumeration and canonical reads report permission and I/O failures; only confirmed missing paths count as absent layers.
- Models are immutable after load - change layer files, then invalidate/reload; never mutate a loaded `Model`.

## References

Read these only when your task matches - not by default.

- Changing Model DB roots, the manifest, root selection, refresh staging/publication, `model.refresh_db`, carry-forward enrichment, refresh-time reasoning derivation or per-Connection Context windows, or registry reload concurrency -> `models/refresh.md`
- Changing token pricing, repricing, tiers or cost computation -> `models/pricing.md`
