# Models

`core/models/` owns the Model DB: the layered on-disk model facts, the **at-load
assembly** that turns those layers into effective models, the registry read path,
and the typed model contract runtime and accessors consume. A loaded model is
always a model at one provider; the *canonical* id (`lab/model`) is an internal
join key used only during assembly — it never goes on the wire.

## Overview — two times, one rule

The system splits cleanly into two moments:

- **Refresh** (`discovery.py` + `models_dev.py`) — the DUMB half. It *fetches*
  (provider `/models` endpoints + the public models.dev `catalog.json`) and
  *projects* the results to disk. Needs network and, for provider catalogs, a
  credential. Rare and explicit (the `model.refresh_db` RPC / the regen script).
  It writes the **pure projection per file** — it does NOT merge across files and
  does NOT join across providers. Where a provider's `/models` endpoint omits
  facts (a bare gateway like opencode-go returns ids only), refresh **fills them
  from that provider's own models.dev section** — `context_window`,
  `max_output_tokens`, `family`, the bare `reasoning` flag, and modalities
  (widened only as a strict superset) — so the provider layer carries the
  provider's real facts instead of a hand-maintained override
  (`discovery._enrich_provider_model`; "fill, don't overwrite").
- **Load** (`assembly.py` behind `ModelRegistry.load`) — the SMART half. It
  *assembles* each effective model in memory from the on-disk layers, resolving
  the canonical join and the field-level merge. **No network, no key.** Frequent.

The rule that ties them: **a hand-edit to an override takes effect on the next
LOAD, not on a refresh.** Override files are read at load time, so correcting a
fact is "edit the override, reload the registry" — never "re-run refresh".

## The three layers under `resources/models/`

Each layer is a different home with a clear responsibility (assembly file-format
contract in `assembly.py`'s module docstring — the source of truth):

| Layer | Files | Keyed by | Written by | Holds |
|---|---|---|---|---|
| Canonical | `models.json` (+ `models.overrides.json`) | canonical id `lab/model` | refresh (`models_dev.py`); overrides by hand | provider-agnostic base: `name`, `family`, `capabilities` (incl. the lifted lab-spec `reasoning` ladder), `context_window`, `max_output_tokens`. **No `provider_id`.** |
| Provider | `<provider>.json` (generated) + `<provider>.overrides.json` (hand) | wire-id (exact id sent on the wire) | `<provider>.json` by refresh; `.overrides.json` by hand | what the provider/endpoint authoritatively reports, incl. a `capabilities.reasoning` ladder that *deviates* from the lab spec; an optional `canonical` pointer; per-model wire `metadata` |
| Adapter fallbacks | (in adapter code) | — | — | send-time defaults (out of scope for the registry) |

Both canonical files may be **absent** — assembly then runs on provider +
override data alone and still loads every model without error
(`load_canonical_layer` returns an empty layer for a missing file). The raw
safety-net dump `models.dev.catalog.raw.json` is kept so a later wanted field is a
projection edit, not a re-fetch; the runtime never reads it.

The `canonical` pointer (JSON key `"canonical"` on a provider/override model
entry) is an **internal join key only**. It is stripped from the assembled record;
it is never a `Model` attribute and never goes on the wire.

## At-load assembly (`assembly.py`)

`ModelRegistry.load` is the single public read surface; everything in
`assembly.py` is hidden behind it. Per provider model it does two things:

**The deterministic join** (`resolve_canonical_id`) — resolves the canonical id
for a wire-id, **no fuzzy matching, ever**:

1. **Explicit `canonical` pointer wins.** A manual pointer (override layer) beats
   an auto pointer (provider layer); both are the JSON key `"canonical"`.
2. **Else exact canonical-id match** — the wire-id is itself a key in the
   canonical layer (covers OpenRouter/Mistral-style `lab/model` wire-ids that
   already equal the canonical id).
3. **Else no join** — the model runs on provider + override data only. A missed
   join is NOT an error; the join is enrichment, not a dependency.

**The 3-layer field-level merge** (`merge_layers` / `_merge_two`) — "fill, don't
overwrite", highest layer wins per top-level field. Precedence highest first:

1. `<provider>.overrides.json` (hand) — always wins
2. `<provider>.json` (provider) — what the provider reports
3. canonical record (base/default, reached via the join)

- `capabilities` is the one field merged **one level deep**: each sub-field
  (`vision`, `tools`, `reasoning`, modality lists, …) is taken from the highest
  layer that defines THAT sub-field, so a provider model can inherit `reasoning`
  from canonical while keeping its own other capabilities.
- Every other nested object or list is taken **wholesale** from the highest layer
  that defines it — never deep-merged or concatenated. In particular the whole
  `reasoning` object and the whole `metadata` blob are replaced wholesale (the
  `metadata` wholesale rule has a known interaction — see Constraints).
- A `null` does **not** count as defining a top-level field — it never overwrites a
  value a lower layer supplied ("fill, don't overwrite, don't un-fill"). This is what
  lets a provider's `context_window: null` fall through to the canonical window the
  join inherits (e.g. opencode-go `minimax-m3` → `512000`); without it the higher
  `null` would clobber the base value and the join would yield nothing.

The merged record (pointer stripped) is constructed into a typed `Model` by
`_model_from_record`; a layer set that fails to supply a required field (`name`,
`capabilities`, `reasoning.supported`) surfaces as a `KeyError` — the correct
"data is incomplete" signal.

**The standalone validator** (`validation.py`, run via
`scripts/validate_model_db.py`) is an offline integrity check — NOT hooked into
the read path. It reports two findings: a **dead `canonical` pointer** (target
absent from the canonical layer — models.dev likely renamed the slug) and a
**redundant manual join** (a manual override pointer equal to the wire-id where
the wire-id is itself a canonical key, so the exact-match auto-join already covers
it). Exit 0 clean, 1 on findings.

## Typed reasoning

`capabilities.reasoning` is a typed block, **no longer a bare boolean**
(`ReasoningCapabilities` in `models.py`):

- `supported: bool` — the only required field; the load-bearing flag runtime and
  snapping read (`model_reasoning_supported`).
- `control: "levels" | "on_off" | "budget" | None` — how the provider steers
  reasoning on the wire (`REASONING_CONTROLS`). Absent when `supported` is false,
  and may be absent for a supported model with no projected ladder yet.
- `levels: tuple[str, ...]` — the effort ladder for `control == "levels"`
  (a subset of `THINKING_EFFORT_ORDER`).
- `budget_max: int | None` — the max thinking-token budget for
  `control == "budget"`.

**Derived at refresh** from models.dev `reasoning_options`
(`derive_reasoning_control`): an `effort` option → `levels` (**effort wins** when
multiple types are present), else `budget_tokens` → `budget`, else `toggle` →
`on_off`. The **canonical ladder is LIFTED from the lab provider only**
(`lift_canonical_ladder`) — the lab's own models.dev section, **no union across
providers**. A canonical model whose ladder can't be lifted deterministically
(lab keys it differently, or no lab provider) keeps the bare `{supported: true}`
and is a hand-path candidate for `models.overrides.json` (see FLAGGED). A provider
that *deviates* from the lab ladder gets its own block stamped on `<provider>.json`
(`provider_reasoning_block`); a non-deviating provider drops its bare `reasoning`
so the canonical ladder is inherited at load.

**One reasoning policy, many renders.** The wire layer turns
`(capabilities.reasoning.control, agent effort)` into a provider-neutral intent
via `resolve_reasoning_intent(...)` and each adapter renders it: `levels` snaps
the effort against `capabilities.reasoning.levels` (via `model_reasoning_levels`,
falling back to the adapter floor for an empty ladder), `on_off` toggles, and
`budget` derives a native token budget scaled by `budget_max` when known, else via
the absolute fallback ladder (`model_reasoning_control` /
`model_reasoning_budget_max` are the accessors). Full wiring in `providers.md` →
"Reasoning is one policy, many renders".

## Wire selectors as data

Per-model wire **facts** live in a **provider-scoped** `metadata` blob keyed by
the underscored provider id, so one provider's quirk never pollutes the schema for
all: `metadata.opencode_go.protocol` (Anthropic vs OpenAI routing),
`metadata.mistral.prompt_mode`, `metadata.<provider>.reasoning_response_field`
(projected at refresh from models.dev `interleaved`). The wire **mechanics** stay
in the adapter — it reads the fact via its injected `model_lookup` and owns only
the *how*. Generalizes the original `metadata.github_copilot` pattern. The full
convention lives in `providers.md` → "Per-model wire SELECTORS are data".

## Optional `context_window`

`context_window` and `max_output_tokens` are both `int | None`. `None`/absent
means the fact is honestly **unknown** (a thin/window-less endpoint, a custom
model) — a missing window **stays missing** in the data, never faked with a
constant (no fake-fact constants in catalogs). Read-side callers resolve a usable
window through the shared chain `resolve_context_window(model.context_window,
provider_config)` (`core/providers/providers.py`): model value → provider-config
`context_window` default → the named global floor `GLOBAL_CONTEXT_WINDOW_FLOOR`.
Non-positive values at any layer (a stray `0` from an old catalog) are treated as
unknown and skipped. The chain is the single source of truth and lives at the
provider-config level, **not** in an adapter. See `providers.md` → "Context-window
resolution is read-side and shared".

## Interfaces

- Data classes live in `models.py`: `Model`, `Capabilities`,
  `ReasoningCapabilities` are frozen. Keep the map at the contract level; the exact
  field list belongs in the dataclasses.
- `Model.model_id` is the exact string sent to the provider API — no remapping,
  no alias layer between registry lookup and adapter request. `family` is a
  first-class fact on the model (the provider/feed lineage), replacing per-adapter
  family-from-name guessing.
- `Model.metadata` is the sanctioned home for provider-scoped per-model wire facts
  (see Wire selectors). It is frozen on construction (immutable after load), small,
  and limited to wire facts — never raw payloads, policy text, credentials, or
  secrets.
- `Model.connections: tuple[str, ...]` binds a model to a subset of its provider's
  connection ids; empty means "all connections". Refresh tags each discovered model
  with `connections: [<connection.id>]` and merges per connection (see
  `providers.md` / `model_tasks.md` for the read-side filter).
  `Model.allows_connection(connection_id)` is the single source of the rule (empty
  allowlist → all permitted, else membership) — read by task-target expansion, the
  WebUI dropdown filter, and the server save-time guards so they cannot drift.
- `ModelRegistry.load(resources_dir)` assembles from the layers and caches by
  resolved `resources_dir`. `is_provider_file` decides what counts as a provider
  file — it excludes `*.raw.json`, `*.overrides.json`, and the canonical
  `models.json`/`models.overrides.json` (shared with the offline validator so the
  classification can't drift).
- `ModelRegistry.get(provider_id, model_id)` raises `KeyError` on a missing pair;
  `list_for_provider` returns models sorted by `model_id`, empty for an unknown
  provider; `invalidate(resources_dir)` clears the cache (used by tests / discovery).
- `ModelRegistry.reload(resources_dir)` re-assembles **in place** — it swaps the
  instance's contents and repoints the cache at the same object, so the registry
  keeps its identity. This is the runtime refresh path (`model.refresh_db` →
  `_reload_runtime_model_registry`): services that captured the registry at
  construction (task-model targets for speech/image/embeddings, the `/status`
  display, the recall backend) hold the one instance and see the new catalog
  without a restart. Do **not** rebind `runtime._models` to a fresh `load()` — that
  rebind was the bug (chat list fresh, specialized targets/status stale).
- `ModelRegistry.query(model_query)` is the filtered read path — pure, no
  credential awareness, lives in `core/models/query.py`. Capability/task/modality/
  context-window matching happens once there; callers that need credential gating
  (RPC `model.list`, `core/model_tasks/` discovery) apply it outside the query.
- `Runtime.models` / `Runtime.get_model(...)` are available only after
  `Runtime.start()`; `get_model` delegates to the registry.

## Capabilities & Tasks

Capabilities are facts about one model through one provider — the same underlying
family can differ per provider. `task_types` is a coarse filtering/routing
projection derived from modalities (`derive_model_task_types`), aligned with
`MODEL_TASK_ORDER` in `models.py` and `.vorch/domain-maps/model_tasks.md`. Sparse
catalogs stay usable: missing modality data defaults to text-in/text-out, and
conservative-optional-fact providers must not vanish from selection.

Speech/audio aliases are intentionally strict: `transcription` output → text
output + STT; `speech` output → TTS + audio-generation; generic `audio` output →
`audio_generation` only (NOT `text_to_speech`); `embeddings` output →
`text_embedding` (vector, not chat/text).

## Constraints & Gotchas

- **Code wins.** When this map and the code disagree, the code (`assembly.py`'s
  module docstring is the load contract) wins; fix the map.
- **Refresh is dumb, load is smart.** Do not push merge/join logic into refresh,
  and do not make load fetch anything. Refresh writes the pure per-file
  projection; load does the cross-file assembly with no I/O beyond reading the
  layer files.
- **The full canonical mirror is intentionally unfiltered — no discovery
  defaults.** Many canonical entries join to no configured provider; that is
  wanted. Do not add a filter to drop them.
- **`metadata` is replaced wholesale at load**, unlike `capabilities` (one level
  deep). A model with `metadata` in BOTH `<provider>.json` and its override keeps
  only the override's blob — e.g. opencode-go's override `metadata.opencode_go`
  shadows the generated `reasoning_response_field`. No harm today (graceful
  fallback); recorded in FLAGGED as a known design choice.
- **Override-only models** are supported: an override file may carry a wire-id
  absent from the provider file; assembly builds it from override + the join. It
  must supply the loader's required fields.
- **`apply_overrides` in `discovery.py` is dead** — refresh no longer bakes
  overrides into `<provider>.json` (that merge moved to load). The helper is
  retained only for legacy callers/tests and is flagged for removal.
- Model objects are immutable after load. Change a layer file or the projection,
  then `invalidate`/reload — never mutate a loaded `Model`.

## References

Read these only when your task matches — not by default.

- The exact on-disk file-format contract + merge/join semantics →
  `core/models/assembly.py` module docstring (source of truth).
