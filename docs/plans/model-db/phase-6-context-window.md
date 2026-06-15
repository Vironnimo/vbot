# Phase 6 — `context_window` optional (its own strand)

> Part of [Model DB plan](README.md). Read README §1 and `stuff/HANDOFF-model-db.md` (section
> "`context_window` optional — eigener Strang, NICHT ans Reasoning koppeln"). Depends on Phase 2.
> **Parallel-safe with Phase 5** (different files). **NOT clean with Phase 4** — both edit
> `core/tools/status.py`, so run Phase 4 first and let this phase adapt `status.py` for a `None`
> window. This phase also edits `core/providers/providers.py` (the provider-config default), which
> Phase 2 touched too — fine, since Phase 6 runs after Phase 2.

**Goal:** A missing context window stays **missing** in the data (`null`/absent) instead of being
faked with a constant that looks like a fact. The honest gap is filled at **send time** by a
provider-config-level default plus a conservative global floor, so nothing downstream ever crashes —
especially custom models.

**Read:** `.vorch/domain-maps/models.md`, `.vorch/domain-maps/providers.md`,
`.vorch/domain-maps/compaction.md`, `.vorch/domain-maps/chat.md`.

**Settled — don't redesign (from §1 + handoff):**
- A missing fact stays missing in the data. The fallback is supplied at send time, **not** baked in.
  No more `DEFAULT_CONTEXT_WINDOW`-style numbers masquerading as facts.
- The fine line: **request-shaping defaults → adapter; read-side facts like `context_window` →
  provider-config level.** `context_window` is read *outside* the adapter (compaction, token budget),
  so its default belongs at the provider-config level, with a conservative global floor as the last
  resort so custom models never crash.
- **Do not couple this to reasoning** — it's a separate strand with blast radius outside reasoning.

## Tasks

- **Make `Model.context_window` optional** — `int | None` (today a required `int`,
  `core/models/models.py`); update the loader and discovery serialization/validation to allow
  `null`/absent (mirror how `max_output_tokens: int | None` is already handled). — files:
  `core/models/models.py`, `core/models/discovery.py`; tests: `tests/core/models/test_models.py`,
  `tests/core/models/test_discovery.py`.
- **Enumerate every read site** of `context_window` and make each tolerate `None` by resolving through
  the new default chain (provider-config default → global floor). Known read sites to start from (grep
  to confirm completeness): `core/compaction/compaction.py`, `core/chat/chat.py`,
  `core/tools/status.py`, `server/rpc/payloads.py`, plus any token-budget computation. — files: those
  read sites; tests: their mirrored test modules.
- **Provider-config-level default + global floor** — add an optional per-provider `context_window`
  default to the provider config (`core/providers/providers.py` / `resources/providers/*.json`) and a
  single conservative global floor constant (named, not magic) used when neither the model nor the
  provider config supplies one. Resolution helper lives where read-side callers can share it (not in an
  adapter). — files: `core/providers/providers.py`, the resolution helper's home, relevant
  `resources/providers/*.json`; tests.

**Done when:**
- A model with `context_window: null` loads, and compaction/token-budget/status all resolve a usable
  number via provider-config default or the global floor — proven by tests that feed a `None`-window
  model through each read site without crashing.
- No constant in the codebase pretends a guessed context window is a discovered fact (the old
  `DEFAULT_CONTEXT_WINDOW`-style fake is gone or clearly relabeled as a floor).
- `python scripts/quality.py <touched paths>` is green.

**Risks / notes:**
- This is the highest "silent crash" risk in the plan (README §6) — the read-site enumeration must be
  exhaustive. Grep `context_window` across `core/`, `server/`, `webui/` and account for each hit.
- Touches `core/models/models.py` (the `context_window` field) — coordinate with the orchestrator so
  no other phase is editing `models.py` at the same moment.
- Custom models (no provider in models.dev) are the motivating case: the global floor is what keeps
  them alive. Keep it conservative.
