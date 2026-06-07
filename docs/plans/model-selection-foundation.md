## Plan: Model-Selection Foundation — single query, unified targets

**Goal:** Capability/task filtering of models lives once in `core/models/` and is reused by both chat model listing (`model.list`) and specialized-task target discovery (`task_model.list_targets`), so specialized selection is task-filtered, provider and (future, user-configurable) local engines share one Target abstraction, and no filter logic or task-type vocabulary is duplicated.

**Context:**
A review of the model stack surfaced a structural fault, not scattered bugs. The good core stays untouched: `Model = (provider_id, model_id-on-the-wire)`, the discovery/refresh pipeline, and the catalog format are sound. The problems all trace to one root cause and a few leftovers:

- **Root cause — filter logic is in the wrong layer.** `_model_matches_filters`, `_model_list_filters`, `_string_filter_values`, `_boolean_model_capability` live in `server/rpc/connection_methods.py` (the RPC layer). `core/models/ModelRegistry` only exposes `get()` and `list_for_provider()` — no query. Because `core/model_tasks/` must not import from `server/` (layer rule: server → core, never the reverse), it **could not** reuse the filter and re-implemented it in `_provider_targets` (`core/model_tasks/model_tasks.py`). The duplication is a forced consequence of placement.
- **Consequence — the specialized picker never uses the task filter.** `model.list?task=text_to_speech` already returns exactly the TTS-capable, credential-gated models (the `_model_response` payload already carries `capabilities.task_types`). The specialized picker re-derives the same set by hand instead of asking for a filtered list.
- **Leftovers.** `TASK_IMAGE_EDIT` is defined but deliberately excluded from `SUPPORTED_TASK_TYPES` (confirmed dead by the user — remove). Local-task option schemas hard-return empty, which blocks the future user-configurable local engines.
- **Already-present seams.** The local Target kind (`kind == "local"`) and the execution dispatch (`speech.py` / `image.py` already call `self._local_executor` for local targets) exist. They are scaffolding that anticipated local engines correctly; they were never fully wired. This plan keeps and cleans them — it does not rebuild them.

Decisions settled with the user before planning:
1. `Model` stays pure (provider catalog facts only). Provider models and local engines are unified at the **Target** layer, not by stuffing local engines into the model catalog.
2. Local TTS/STT engines will be **user-configurable**, but are **not implemented in this pass**. The foundation must not block user-configurable engines later; it must not pre-build them now.
3. `TASK_IMAGE_EDIT` is removed.

**Requirements (verbatim from the user, across the discussion):**
- "das filter system sollte schon fuer die specialized modelle genutzt werden" — wire the existing capability/task filter into specialized-model selection.
- "den task_image_edit gibt es nicht mehr. der kann raus."
- "wir wollen auf jeden fall lokale engines fuer tts/stt benutzen koennen" + "die sachen muessen natuerlich user konfigurierbar sein" + "lokale engines kommen noch nicht rein" — keep the foundation ready for user-configurable local engines; do not implement them now.
- "video generation werden wir auch bald hinzufuegen … fuer all das muss das grundgeruest erstmal ordentlich sein" — the foundation must let video drop in later without selection/binding changes.
- "die theorie ist: wir holen die models durch die models endpoints, speichern sie mit allem … und dann koennen models einfach ausgewaehlt werden."

**Scope:**
- **In:**
  - Lift capability/task/modality/context-window matching from `server/rpc/connection_methods.py` into `core/models/` as a reusable query.
  - `model.list` RPC parses params at the boundary and delegates matching to the core query (behavior unchanged).
  - `task_model.list_targets`' provider half delegates to the core query (task-filtered) instead of re-iterating `list_for_provider()` + manual `task_type in capabilities`. Credential gating, connection expansion, and local-target merge stay.
  - Remove `TASK_IMAGE_EDIT`.
  - Shape the local seam for future user-config: local Target option schema comes from the descriptor (descriptor-owned fields) instead of a hard-coded empty schema. Registry stays empty; no concrete engine.
  - Update `.vorch/specs/models.md` and `.vorch/specs/model_tasks.md`.
  - Tests written alongside each change (same phase/task).
- **Out (explicitly):**
  - Implementing any local engine (Whisper, Piper, local video) or its execution adapter.
  - Settings storage/UI for user-configured local engine instances (the *consumer* of the prepared seam; a later plan).
  - Wiring chat-model filters in the UI ("für später" per the user — the query stays unfiltered for chat for now).
  - `video_generation` UI or execution; it stays a recognized-but-parked task type.
  - Frontend dedup of `modelSelection.js` vs `taskModelSettings.js` parse/label helpers (possible follow-up once both pickers are query-backed; not required for the foundation).
  - Stale static catalogs (`anthropic.json`, `openai.json` placeholder IDs) — unrelated cleanup, separate task.
  - The discovery/refresh pipeline (`core/models/discovery.py`) — unchanged.

**Assumptions & Constraints:**
- `video_generation` is kept in `SUPPORTED_TASK_TYPES` and `MODEL_TASK_ORDER` (parked, no UI/execution). Removing it would only have to be undone "bald"; keeping it also validates the "new task drops in" property of the foundation. Flagged as an assumption, not a settled decision.
- Credential gating stays at two genuinely different granularities and is **not** unified here: `model.list` gates provider-level (`runtime.has_provider_credentials`), target discovery gates per-connection (it must know *which* connections are usable to expand targets). Only the *capability/task matching* is unified.
- The core query is **pure** (capabilities/tasks/modalities/context only) — it takes no runtime/credentials. Credential gating remains at the callers, which already hold the runtime.
- Layer rule holds throughout: `core/` imports nothing from `server/`. The query lives in `core/models/` precisely so `core/model_tasks/` can import it.
- No legacy/compat shims (project convention). RPC request/response shapes for `model.list` and `task_model.list_targets` stay byte-identical so no accessor changes are needed.

### Milestones

| # | Milestone | Deliverable |
|---|---|---|
| M1 | Core query exists | `core/models/` exposes a reusable, unit-tested model query; pure capability/task/modality/context matching, no credentials. |
| M2 | Single filter impl | `model.list` and `task_model.list_targets` both route matching through the core query; the RPC-layer and `_provider_targets` filter re-implementations are gone; outputs byte-identical to today. |
| M3 | Seams clean & forward-ready | `TASK_IMAGE_EDIT` removed; local Target option schema is descriptor-owned (ready for user-config, no engine built); specs updated. |

### Phase Breakdown

#### Phase 1: Core model query
**Goal of this phase:** A reusable, pure query over the registry that does all capability/task/modality/context matching, with the value-normalization the RPC layer does today.
**Can run in parallel with:** none (foundation for all later phases).

- Add `ModelQuery` (frozen dataclass: `provider_id`, `tasks`, `capabilities`, `input_modalities`, `output_modalities`, `min_context_window`) plus a builder that normalizes raw filter values (lowercase/trim/dedupe) and a matcher. Move the logic currently in `_model_matches_filters`, `_model_list_filters`, `_string_filter_values`, `_normalized_filter_values`, `_boolean_model_capability`, `_optional_*` out of `connection_methods.py` into the core module. The matcher operates on `Model`; the registry gains `query(model_query) -> list[tuple[str, Model]]` (provider_id + model, sorted by `(provider_id, model_id)`), no credential awareness. — read: [.vorch/specs/models.md], files: [core/models/query.py (new), core/models/models.py, core/models/__init__.py]
- Unit-test the query: task filter selects only matching `task_types`; boolean caps (`vision`/`tools`/`json_mode`/`reasoning`) map correctly; modality + `min_context_window` filters; empty query returns all; unknown provider returns empty; value normalization (aliases like `task`/`task_type`, list-or-string) behaves as the RPC did. — files: [tests/core/models/test_query.py (new)]

**Dependencies:** none.
**Done when:** `tests/core/models/test_query.py` passes; matching/normalization logic exists only in `core/models/`.

#### Phase 2: `model.list` delegates to the core query ⚡
**Goal of this phase:** The chat-model RPC keeps only its boundary concerns (allowed-field validation, credential gating, response shaping) and delegates matching to the core query.
**Can run in parallel with:** Phase 3 (disjoint file-scope).

- Rewrite `_list_models`: keep `MODEL_LIST_FILTER_FIELDS` boundary validation (RPC API contract) and `_provider_has_credentials` gating; build a `ModelQuery` from params and call `runtime.models.query(...)`; map `KeyError`/validation errors to `RPC_ERROR_INVALID_REQUEST` as today; build `_model_response` from the results. Delete the moved filter helpers from this module. — read: [.vorch/specs/server.md, .vorch/specs/models.md], files: [server/rpc/connection_methods.py]
- Update RPC tests so filter assertions target the new path; confirm `model.list` output is byte-identical for: no filter, `task=...`, `capability=...`, `min_context_window`, `provider_id`, and the unsupported-field error. — files: [tests/server/test_rpc.py]

**Dependencies:** Phase 1.
**Done when:** `model.list` returns identical results to pre-change for the cases above; no filter/matching helpers remain in `connection_methods.py`.

#### Phase 3: Target discovery delegates to the core query ⚡
**Goal of this phase:** The specialized picker becomes genuinely task-filtered via the shared query; `_provider_targets` stops re-implementing the filter.
**Can run in parallel with:** Phase 2 (disjoint file-scope).

- Rewrite `_provider_targets`: for each provider with usable connections, call `self._models.query(ModelQuery(provider_id=..., tasks=(task_type,)))` instead of `list_for_provider()` + manual `task_type in model.capabilities.task_types`; keep credential gating, multi-connection expansion, labels, and the local-target merge unchanged. `TaskModelService` already receives `models`; confirm it can call `.query` (inject the registry's query rather than only `list_for_provider`). — read: [.vorch/specs/model_tasks.md, .vorch/specs/models.md], files: [core/model_tasks/model_tasks.py]
- Update target tests: `list_targets(task)` returns the same provider targets as before for STT/TTS/image; a model lacking the task type is excluded; multi-connection expansion and local merge unchanged. — files: [tests/core/model_tasks/test_model_tasks.py]

**Dependencies:** Phase 1.
**Done when:** `list_targets(...)` output is unchanged; the provider half contains no hand-written task/capability filter.

#### Phase 4: Remove dead vocabulary + descriptor-owned local option schema
**Goal of this phase:** Drop `TASK_IMAGE_EDIT`; make the local Target option schema come from the descriptor so future user-configured engines can declare their own fields — without building any engine.
**Can run in parallel with:** none (touches `constants.py`/`options.py`/`local_targets.py` that earlier-phase tests reference; keep sequential after Phase 3).

- Remove `TASK_IMAGE_EDIT` and its references; verify `SUPPORTED_TASK_TYPES`/`MODEL_TASK_ORDER` no longer mention image-edit; keep `video_generation` parked. — files: [core/model_tasks/constants.py, core/models/models.py]
- Give `LocalTaskTargetDescriptor` an optional option-schema (tuple of the same `TaskModelOptionField` shape used by provider schemas); change `TaskModelService.options(...)` so the `kind == "local"` branch returns the descriptor's schema instead of an empty schema; provider branch unchanged. Registry stays empty — no engine registered. Add a module/docstring note that descriptors will later be constructed from user settings. — read: [.vorch/specs/model_tasks.md], files: [core/model_tasks/local_targets.py, core/model_tasks/options.py, core/model_tasks/model_tasks.py]
- Tests: a test-only local descriptor carrying option fields surfaces them through `task_model.options`; provider option schemas unchanged; removing `TASK_IMAGE_EDIT` does not break validation. — files: [tests/core/model_tasks/test_model_tasks.py]

**Dependencies:** Phase 3.
**Done when:** no reference to `image_edit`/`TASK_IMAGE_EDIT` remains; a local descriptor with fields yields a non-empty schema via `task_model.options`; all `core/model_tasks` tests pass.

#### Phase 5: Specs alignment
**Goal of this phase:** Specs match the new layering so future work reads the right contract.
**Can run in parallel with:** none.

- `.vorch/specs/models.md`: document the `ModelQuery` + `registry.query()` read-path interface and that capability/task matching is owned here (not the RPC layer). — files: [.vorch/specs/models.md]
- `.vorch/specs/model_tasks.md`: document that provider target discovery delegates to `core/models` query; that local Target option schemas are descriptor-owned and reserved for future user-configurable engines; that `TASK_IMAGE_EDIT` is gone. — files: [.vorch/specs/model_tasks.md]

**Dependencies:** Phases 2, 3, 4.
**Done when:** both specs describe the query-owns-matching contract and the descriptor-owned local schema seam; no spec mentions `image_edit` as live vocabulary.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Subtle behavior drift in `model.list`/`list_targets` output after moving the filter | Med | Med | Treat outputs as a frozen contract; Phase 2/3 done-when is byte-identical output; assert against captured pre-change results in tests. |
| Value-normalization edge cases (alias fields, list-vs-string, casing) lost in the move | Med | Med | Port the existing `_string_filter_values`/`_normalized_filter_values` logic verbatim into the core builder and cover each alias in Phase 1 tests. |
| RPC error mapping changes (validation errors must stay `invalid_request`) | Low | Med | Keep boundary validation in `connection_methods.py`; core query raises plain `ValueError`/`KeyError`, mapped by existing `_map_expected_error`. |
| Over-reaching into local-engine implementation despite "not now" | Low | Med | Phase 4 stops at a descriptor-owned schema with an empty registry; no engine, no settings storage; reviewer checks registry stays empty. |
| Layer violation creeps back (core importing server) | Low | High | Query lives in `core/models/`; `core/model_tasks` imports from `core/models` only; no `server` import added to core. |

### Notes for the orchestrator
- **Final size: Medium.** Despite the "redesign" framing, research shows the seams (local Target kind, execution dispatch, option schema) already exist; the work is a contained layering fix + de-duplication, not a rewrite. Five mostly-sequential phases; Phases 2 and 3 parallelize (disjoint files).
- **Open decision (parked):** keep `video_generation` as a recognized-but-unimplemented task type. Default chosen: keep it (video is coming "bald"; keeping it validates the foundation's extensibility). Alternative: strip it until the video plan lands. Low rework either way — reversible.
- **Assumptions to confirm:** frontend picker dedup and stale static catalogs are out of scope (separate follow-ups); credential gating intentionally not unified.
- **No new dependencies.**
- **Architecture concern (flag only, not planned here):** once both pickers are query-backed, the two frontend helpers (`modelSelection.js`, `taskModelSettings.js`) still duplicate parse/label logic for the same `provider/model::connection` value format. A later pass could share one option-builder across the chat and specialized pickers.
