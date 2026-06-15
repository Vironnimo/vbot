# Phase 7 — Specs, domain maps, glossary

> Part of [Model DB plan](README.md). Read `stuff/HANDOFF-model-db.md` (section "Spec-/Doku-
> Liefergegenstände") first. Depends on Phases 1–6 (documents the system as actually built). The
> handoff is explicit: rewriting the affected specs is a **deliverable, not collateral damage**.

**Goal:** The project's living docs describe the **new** system — at-load assembly, the canonical
layer, the 3-layer merge, the join as a load task, typed reasoning — not the old "no canonical entity /
refresh bakes everything" world.

**Read first (mandatory before editing any domain map):**
`.vorch/workflows/domain-map-workflow.md` — it defines what belongs in a domain map (factual working
notes, every claim backed by source/tests, no exhaustive API/field dumps) and the create/maintain/index
rules. Then `.vorch/domain-maps/models.md`, `.vorch/PROJECT.md`, `.vorch/GLOSSARY.md`.

**Settled — what to change (from handoff):**

- **`.vorch/domain-maps/models.md` — rewrite.** Canonical base (models.dev) + per-provider layer +
  adapter fallbacks; **at-load assembly** (refresh fetches, load assembles); the 3-layer field-level
  merge (override wins, live at load); the join as a load task with a hand fallback; `reasoning.supported`
  boolean → typed control field; "no discovery defaults". The current map's lines that describe the
  *old* world — e.g. "no canonical cross-provider model entity … never remaps" — describe only the
  before-state and must be replaced. Every claim backed by the code as actually built (code wins over
  the map).
- **`.vorch/GLOSSARY.md`** — run the `glossary` skill for these (don't hand-edit raw): *Model* /
  *Provider* updated for the join-key-≠-wire-id split; *Reasoning* from "is a boolean" to the typed
  ladder; new entries *canonical (id)*, *reasoning_options* / *control*, *Refresh* vs *Laden* (Load).
- **`.vorch/PROJECT.md`** — update the Context bullets that mirror the old catalog philosophy:
  "Model catalogs are refreshable artifacts…" and "Overrides are for research-only gaps" now need the
  at-load-assembly + canonical-layer framing. Add the new canonical files to any file-layout mention.
- **`.vorch/FLAGGED.md`** — append (append-only, don't reorganize) the consciously deferred items:
  `budget`/`on_off` wire support, native `pdf`/`video` modality wire paths, custom user-provider second
  read-root + invalidation, the Anthropic stub, and any island found-but-deferred in Phase 5, plus the
  ~31 lab-keying/no-lab-provider hand cases left unseeded in Phase 3.

## Tasks

- **Rewrite `models.md`** per the above — files: `.vorch/domain-maps/models.md`.
- **Update affected provider child maps** where Phase 4/5 changed behavior (reasoning snapping source,
  opencode-go protocol field, mistral prompt_mode, copilot family) — files:
  `.vorch/domain-maps/providers.md`, `.vorch/domain-maps/providers/{openai,openrouter,mistral,opencode-go,github-copilot}.md`
  (only those actually changed).
- **Glossary** via the `glossary` skill — files: `.vorch/GLOSSARY.md` (through the skill).
- **PROJECT.md context bullets + file layout** — files: `.vorch/PROJECT.md`.
- **FLAGGED deferrals** — files: `.vorch/FLAGGED.md`.

**Done when:**
- `models.md` no longer contains any "no canonical entity / never remaps / refresh bakes everything"
  claim, and accurately describes refresh-fetches/load-assembles + the 3-layer merge + the join.
- The glossary entries above exist and match the built behavior.
- PROJECT.md's old catalog-philosophy bullets are updated.
- FLAGGED carries the deferred list.
- No code changes in this phase (docs only) — so no quality gate, but every factual claim is checked
  against the merged code.

**Risks / notes:**
- Docs only — but the rule "code wins over the map" means you must read the *as-built* code, not this
  plan, when they differ. The plan is intent; the code is truth.
- Keep the `glossary` skill in charge of glossary edits (project rule in CLAUDE.md).
