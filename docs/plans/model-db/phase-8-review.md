# Phase 8 — Final review (review subagent)

> Part of [Model DB plan](README.md). This is the **mandatory closing review** the user asked for.
> Run by **one dedicated review subagent** after Phases 0–7 are committed in the worktree.

**Goal:** An independent pass over the whole worktree branch diff, checking it against this plan and
the handoff, before the orchestrator declares the branch merge-ready.

## How the orchestrator dispatches it

Spawn a review subagent (general-purpose). Give it in the prompt:

1. Read in full: `docs/plans/model-db/README.md` and every `phase-*.md`, and
   `stuff/HANDOFF-model-db.md`, and `.vorch/PROJECT.md` + `.vorch/GLOSSARY.md`.
2. The branch under review (the `model-db` worktree branch). Tell it to review the **full diff vs.
   `main`**, not just the last commit.
3. The review checklist below.
4. Instruction: report findings grouped **must-fix** (correctness/security/architecture-violation) vs
   **nice-to-have** (cleanup/simplification). Do **not** fix — report. The orchestrator decides what to
   fix and what to defer to `.vorch/FLAGGED.md`.

The orchestrator may also run the built-in `/code-review` skill on the diff as a second pass — but the
plan-aware review subagent is the required one (it checks against intent, which `/code-review` can't).

## Review checklist (architecture fidelity — the things most likely to drift)

- **Refresh stays dumb, load stays smart.** No cross-provider join or cross-file merge leaked into
  refresh; the 3-layer assembly + join happen at **load**, in code, with no network/key.
- **No fuzzy matching anywhere in the join.** Only exact wire-id / exact canonical-id auto-joins; the
  manual `canonical:` pointer is the only override. The validator exists and runs standalone.
- **3-layer merge is field-level "fill, don't overwrite", override wins, nested replaced wholesale** —
  and the `deepseek-v4-pro` worked example is actually tested with both effective ladders.
- **Typed reasoning** is `{supported, control, levels|budget_max}`; `control` derivation is data-driven
  (effort wins); snapping uses the **per-model effective ladder**, adapter constant only as floor.
- **Phase scope held:** no `budget`/`on_off` *wire* support snuck in; no native pdf/video wire path; no
  custom-provider second read-root built (these are flagged, not built).
- **Wire facts are data, mechanics are code** — the `metadata` blob is provider-scoped; no generic
  top-level wire fields; `_ANTHROPIC_MESSAGES_MODELS` and the Mistral prefix tuple are gone.
- **`context_window` honesty** — optional in data; defaults resolved at provider-config level + global
  floor; every read site tolerates `None`; no fake-fact constant remains.
- **No legacy compatibility** branches; conversion scripts (if any) live in `scripts/converters/` and
  aren't hooked into startup.
- **Project conventions** (CLAUDE.md): no magic numbers, comments say *why*, no commented-out code,
  separation of concerns, i18n for user-facing strings, no secrets in code/logs, structured logging.
- **Security:** no credential ever committed (`.env`), logged, or written under `resources/`; no user
  input straight into SQL/HTML/shell/paths.
- **Docs match code** — `models.md` rewritten, glossary/PROJECT/FLAGGED updated, claims backed by the
  as-built code.
- **Quality gates** — `python scripts/quality.py` (full or per touched module) and
  `python scripts/quality-frontend.py` are green on the final branch.

**Done when:**
- The review subagent has reported must-fix vs nice-to-have findings.
- Every must-fix is fixed (committed) or, with the user's awareness, deferred to `.vorch/FLAGGED.md`
  with a reason.
- Full quality gate green on the final branch.
- The orchestrator tells the user the worktree is **merge-ready** (branch name + one-line status) and
  **waits for his go before merging** (CLAUDE.md → Git). After approval + merge,
  `python scripts/worktree.py delete model-db`.
