# Phase 1 — Schema foundation (typed reasoning + new fields + metadata blob)

> Part of [Model DB plan](README.md). Read README §1 (settled architecture) and
> `stuff/HANDOFF-model-db.md` first. **Sequential foundation — everything downstream builds on this.**

**Goal:** The vBot model data classes and the registry read path understand the **new on-disk schema**:
reasoning is a typed control field (not a bare boolean), `family` is a first-class field, and a
provider-scoped `metadata` blob is the sanctioned home for per-model wire facts. All committed catalog
files load under the new shape.

**Read:** `.vorch/domain-maps/models.md`, `.vorch/domain-maps/providers.md`.

**Settled — don't redesign (from §1):**
- Typed reasoning: `{ "supported": bool, "control": "levels"|"on_off"|"budget", "levels": [...],
  "budget_max": int? }`. `control`/`levels`/`budget_max` are absent when `supported: false`.
- `metadata` is **provider-scoped** (e.g. `metadata.opencode_go.protocol`), small, immutable after
  load — never raw payloads, policy text, or secrets (matches today's `metadata.github_copilot`).
- No legacy compatibility: the loader reads the new shape and **only** the new shape. Old boolean-only
  `reasoning` files are invalid after this phase.

## Scope of this phase

This phase changes the **shape** and the **single-file load** — it does **not** yet add cross-file
assembly or the canonical join (that's Phase 2). After this phase the loader still reads each
`<provider>.json` on its own, but in the new typed shape.

## Tasks

- **Type `ReasoningCapabilities`** — extend the frozen dataclass in `core/models/models.py` from
  `supported: bool` to the typed shape (`supported`, `control: str | None`, `levels: tuple[str, ...]`,
  `budget_max: int | None`). Validate `control` against an allowed set; `levels` values against
  `THINKING_EFFORT_ORDER` (`core/providers/reasoning.py`). Keep it frozen. — files:
  `core/models/models.py`; tests: `tests/core/models/test_models.py`.
- **Add `family`** as a first-class field on `Model` (or `Capabilities` — builder decides, justify in
  the diff; handoff says "eigenes Feld am Modell"). Optional, defaults empty. — files:
  `core/models/models.py`; tests: `tests/core/models/test_models.py`.
- **Update the loader** `ModelRegistry.load()` to read the typed `reasoning` block and `family` from
  `<provider>.json`; keep the metadata-freezing behavior. — files: `core/models/models.py`; tests:
  `tests/core/models/test_models.py`.
- **Update discovery serialization + validation** so refresh writes and validates the typed shape:
  `_model_to_data()` writes `reasoning: {supported, control, levels, budget_max}`; `_validate_model_data()`
  / `_validate_override_model_data()` validate it; add `family`. — files: `core/models/discovery.py`;
  tests: `tests/core/models/test_discovery.py`.
- **Convert ALL committed catalog seeds to the new shape in this same commit** so tests stay green and
  the app still loads. Refresh-backed catalogs (`openai`, `openrouter`, `mistral`, `github-copilot`)
  get a mechanical boolean→typed transform now (Phase 3 regenerates them properly); hand-maintained
  seeds (`anthropic.json`, `opencode-go.json`, and the `*.overrides.json`) get hand-converted. A
  throwaway conversion helper, if written, goes in `scripts/converters/` (never hooked into startup).
  — files: `resources/models/*.json`, `resources/models/*.overrides.json`, optionally
  `scripts/converters/<name>.py`.

**Done when:**
- `ReasoningCapabilities` carries the typed control fields; `test_models.py` proves a `levels`
  control model, an `on_off`/`budget` model, and a `supported: false` model all load correctly.
- The registry loads **every** file under `resources/models/` without error (no boolean-only
  `reasoning` left).
- `test_discovery.py` proves refresh round-trips the typed reasoning shape and rejects a malformed one.
- `python scripts/quality.py core/models resources` (or the precise touched paths) is green.

**Risks / notes:**
- This is the "schema breaks everything at once" risk from README §6 — the all-seeds conversion in the
  same commit is the mitigation; do not split it out.
- Downstream code still reads `reasoning.supported` (e.g. `model_reasoning_supported` in
  `core/providers/reasoning.py`, snapping). Keep `supported` present and truthful so nothing breaks
  before Phases 4/5 rewire snapping. Do **not** wire snapping against the new `levels` here — that's
  Phase 4.
- Keep `metadata` conventions documented in the dataclass docstring; the full `models.md` rewrite is
  Phase 7, but a localized docstring/comment here is expected.
