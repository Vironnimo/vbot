# Phase 2 — Load-assembly backbone + canonical base + deterministic join + validator

> Part of [Model DB plan](README.md). Read README §1 (settled architecture) and
> `stuff/HANDOFF-model-db.md` (sections "Die zwei Zeitpunkte", "Drei Schichten", "Der kanonische
> Join", "Merge beim Laden") first. **This is the heart of the rebuild — "das Rückgrat". Everything
> else hangs off it.** Depends on Phase 1.

**Goal:** The registry read path grows from "read one `<provider>.json`" to "assemble each model from
the canonical base + the provider layer + overrides, resolving the canonical join — live, at load,
with no network and no key."

**Read:** `.vorch/domain-maps/models.md`, `.vorch/domain-maps/providers.md`.

**Settled — don't redesign (from §1):**
- **Three layers, field-level "fill, don't overwrite", highest wins per field:** ① `<provider>.overrides.json`
  (hand, always wins) ② `<provider>.json` (provider layer) ③ `models.json` (canonical, inherited via the
  `canonical` pointer). Nested objects (e.g. `reasoning`) replace **wholesale**, not deep-merge.
- **Deterministic join only, NO fuzzy.** Auto-join when there's an exact match (exact wire-id in the
  canonical model's provider section, or exact canonical-id). No safe match → use the manual
  `canonical: <id>` pointer from the override if present; otherwise the model simply runs on
  provider+override data (a missed join is not damage).
- The join is computed **at load** in code — never baked in, always current.

## Architecture note (builder decides, justify against CLAUDE.md "few deep modules")

The assembly + join logic will grow `core/models/models.py` past comfort. A new sibling module
(e.g. `core/models/assembly.py` and/or `core/models/canonical_join.py`) is justified **if** it keeps
`ModelRegistry` as the single public read interface and hides the merge/join internals behind it. Do
**not** create a shallow pass-through module. Whatever you choose, `ModelRegistry.load()` /
`.get()` / `.query()` stay the public surface (callers and `models.md` describe them).

## Tasks

- **Canonical base loading** — load `resources/models/models.json` (keyed by canonical id `lab/model`)
  and `resources/models/models.overrides.json` into an in-memory canonical layer. These files may not
  exist yet at this phase (Phase 3 generates them) — load defensively (absent = empty canonical layer,
  assembly still works on provider+override data). — files: `core/models/models.py` (+ new assembly
  module if chosen); tests: `tests/core/models/test_models.py` (+ new test module).
- **vBot↔models.dev provider-id mapping — in the provider config, NOT a code table.** Add an optional
  `models_dev_id` field to `ProviderConfig` (`core/providers/providers.py`) and to the
  `resources/providers/<provider>.json` files, defaulting to the vBot provider id when absent (the
  common case). The join reads it to find a provider's section inside a canonical model. The assembly
  needs the loaded provider configs for this — pass them into the load path or read
  `resources/providers/*.json` from the assembly; builder decides, but `ModelRegistry` stays the
  public read surface. Exact ids are confirmed/set in Phase 3 against the real `catalog.providers`
  keys. — files: `core/providers/providers.py`, `resources/providers/*.json`, the assembly module;
  tests: the config field + the mapping lookup.
- **Deterministic auto-join at load** — for each provider model, resolve its canonical id by exact
  match only; attach the canonical base as layer ③. Apply a manual `canonical: <id>` pointer from the
  override when present (it overrides/forces the join target). — files: assembly module; tests.
- **Field-level 3-layer merge** — build the effective `Model` per the precedence above; nested objects
  replaced wholesale. This is the new body of `ModelRegistry.load()`'s per-model construction. — files:
  `core/models/models.py` (+ assembly module); tests covering: provider-only model (no canonical),
  canonical-only inheritance, override-wins, and wholesale nested replace.
- **Worked-example test** — encode the handoff's `deepseek-v4-pro` example: canonical ladder
  `[high, max]`; OpenRouter provider layer deviates `[high, xhigh]` → effective `[high, xhigh]`;
  opencode-go doesn't deviate → effective `[high, max]`. This is the acceptance test for the merge. —
  files: `tests/core/models/test_assembly.py` (or similar) with fixtures under `tests/core/models/fixtures/`.
- **Validator tool (standalone, runnable)** — warns on **dead `canonical` pointers** (target slug not
  in `models.json`) and on **redundant manual joins** (a manual pointer that now also matches exactly
  by the auto rule). Standalone, not hooked into the runtime read path. — files: `scripts/` (e.g.
  `scripts/validate_model_db.py`) or a `core/models/` function invoked by a thin script; tests for the
  detection logic.
- **Cache/invalidation** — assembly must respect the existing `ModelRegistry._cache` + `invalidate()`
  contract; canonical files are part of the cache key's inputs. Ensure a refresh-then-reload picks up
  new canonical data (the `model.refresh_db` RPC reloads via `ModelRegistry.load`; see models.md →
  Discovery & Refresh). — files: `core/models/models.py`; tests.

**Done when:**
- The worked-example test passes exactly as the handoff describes (two providers, two effective ladders).
- A provider model with no canonical match still loads on provider+override data (missed join ≠ error).
- The validator flags a deliberately dead pointer and a deliberately redundant manual join in a fixture.
- `ModelRegistry.load/get/query` remain the only public read surface; existing callers
  (`Runtime.get_model`, RPC `model.list`, `core/model_tasks/` discovery) keep working unchanged.
- `python scripts/quality.py core/models scripts` (precise touched paths) is green.

**Risks / notes:**
- This phase reads files Phase 3 will write (`models.json`, per-provider `canonical` pointers). Build
  against **fixtures** that match the agreed shape; the contract you implement here *is* the contract
  Phase 3 must produce — document the exact expected file shape in this phase's deliverable (a short
  "canonical file format" note) so Phase 3's builder matches it.
- Keep the read path fast and deterministic: **no fuzzy matching, no network, no key** at load.
- Don't filter the canonical mirror — many canonical entries join to no configured provider; that's
  intended (README §1).
