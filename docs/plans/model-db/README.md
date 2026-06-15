# Plan: Provider-independent Model DB

> **Source of truth for intent:** `stuff/HANDOFF-model-db.md` (untracked). Read it in full before
> starting — this plan is the *execution* layer; the handoff carries the *why* and the verified data
> facts (models.dev fetch 2026-06-15). Where the two disagree, the handoff wins and this plan is
> wrong — stop and reconcile.

**Goal:** vBot gets its own provider-independent **Model DB** as a knowledge layer over models. It
collects the per-model knowledge that today is hardcoded and scattered across adapters into *one*
place, fed from models.dev instead of maintained in code. New leading principle: **refresh only
fetches, load assembles.** First concrete beneficiary: reasoning levels.

**Final size:** Large. Multi-file plan — this README is the orchestrator control document; each
`phase-N-*.md` is self-contained and handed to one builder subagent.

---

## 0. How this plan is executed (read first — orchestrator instructions)

This plan is **not** run by a single agent in one pass. In the execution session **you are the
orchestrator**: you dispatch each phase to a builder subagent, review its output, commit, and move
on. The user confirmed this model explicitly.

### 0.1 Worktree — mandatory

All work happens in a dedicated worktree, never on `main` directly:

```bash
python scripts/worktree.py create model-db
```

`create` prints the worktree **path**, an assigned **port**, a **data dir**, and a **URL**. Do all
work and all commits **inside that worktree**. When everything is done, committed, and reviewed,
tell the user the worktree is merge-ready (branch name + one-line status) and **wait for his go
before merging** — he decides when. After he approves and you merge, remove it:

```bash
python scripts/worktree.py delete model-db
```

### 0.2 Credentials & the worktree data dir — handle with care

API keys (`.env`) and OAuth tokens (`oauth/*.json`) live in the **default data dir** (`~/.vbot`). The
worktree gets its **own** data dir (path printed by `worktree.py create`), which starts **without**
those credentials — so model refresh and live inference fail there until you provision them.

**The credential form differs per provider — `.env` alone is NOT enough for the subscription target:**

| Test target | Credential form | Provision into worktree data dir |
|---|---|---|
| `opencode-go` | API key `OPENCODE_GO_API_KEY` in `.env` | copy `.env` |
| `openrouter` | API key `OPENROUTER_API_KEY` in `.env` | copy `.env` |
| `openai` (subscription) | **OAuth token** at `oauth/openai-subscription.json` (not `.env`) | copy that token file |

So before any refresh or live test in the worktree:

```powershell
Copy-Item "$HOME/.vbot/.env" "<worktree-data-dir>/.env"
New-Item -ItemType Directory -Force "<worktree-data-dir>/oauth" | Out-Null
Copy-Item "$HOME/.vbot/oauth/openai-subscription.json" "<worktree-data-dir>/oauth/"
```

This leaves settings/sessions fresh (no pollution of real data) while making all four allowlisted test
targets usable. (Verified 2026-06-15: `~/.vbot/.env` carries the opencode-go / openrouter / openai API
keys; `~/.vbot/oauth/` holds `openai-subscription.json`.)

**Hard rules:** never commit `.env` or any `oauth/*.json` (untracked/data-dir-only by design); never
echo a key/token value into logs or chat; never write credentials under `resources/`. `.env` is read
at startup as a fallback; process env keeps higher precedence (see PROJECT.md → Configuration).

### 0.3 Cost control — the model allowlist (NON-NEGOTIABLE)

Most of this work is verifiable by **unit/integration tests** (pytest/Vitest) and by **catalog
refresh** — and *catalog refresh costs nothing* (it is a `GET /models` + a models.dev `GET`, no
inference). **Live inference is only needed to confirm wire behavior** (reasoning snapping actually
landing, protocol routing, reasoning replay). Reach for it last, not first.

When live inference *is* required, send it **only** to these exact models — nothing else, ever:

| Provider (connection) | Allowed model selector | Why this one |
|---|---|---|
| `opencode-go` | `opencode-go/deepseek-v4-flash` | cheap, OpenAI-compat route |
| `opencode-go` | `opencode-go/mimo-v2.5` | cheap, exercises a second opencode-go model |
| `openrouter` | `openrouter/deepseek/deepseek-v4-flash` | cheap reasoning model on OpenRouter |
| `openai` (subscription) | `openai/gpt-5.4-mini::subscription` | cheapest subscription reasoning model |

(Wire IDs verified against the committed catalogs on 2026-06-15: `deepseek-v4-flash` and `mimo-v2.5`
in `resources/models/opencode-go.json`; `deepseek/deepseek-v4-flash` in `openrouter.json`;
`gpt-5.4-mini` in `openai.json`. If a refresh renames any of them, re-confirm from the freshly
refreshed catalog before sending — do **not** guess a different model.)

**Rules you MUST pass verbatim to every builder subagent whose task could trigger inference:**
- Prefer pytest/Vitest with fixtures and stubbed HTTP. Default to **no** real provider calls.
- Catalog **refresh** is allowed freely (no inference cost).
- Real inference only to confirm wire behavior that tests can't, and only with the four allowlisted
  models above. Keep prompts tiny ("hi", one tool call). One or two requests, not a sweep.
- Anthropic is an untested stub with **no key** — do not attempt to test it (see handoff → "Zu klären").
- `mistral`, `minimax`, `github-copilot` are **not** in the allowlist → do not send inference to them;
  rely on tests + catalog refresh for those.

### 0.4 Dispatching a phase to a builder subagent

For each phase, spawn a builder subagent (general-purpose) and give it, in the prompt:

1. **The phase file path** — `docs/plans/model-db/phase-N-<name>.md` — and tell it to read that file
   in full first.
2. **The handoff path** — `stuff/HANDOFF-model-db.md` — as the rationale source of truth.
3. **The session-start reads** — instruct it to read `.vorch/PROJECT.md` and `.vorch/GLOSSARY.md`
   (project rules), plus the domain maps named in the phase's `read:` lists.
4. **A short briefing** in your own words: what this phase delivers, what depends on it, what is
   explicitly out of scope, and "write tests with the code, run `python scripts/quality.py <paths>`
   green before reporting done."
5. **The cost rules from §0.3** verbatim if the phase can touch inference.

Briefing template:

```
Read these in full first, in order:
  1. docs/plans/model-db/phase-N-<name>.md   ← your task
  2. stuff/HANDOFF-model-db.md               ← the rationale / verified data facts
  3. .vorch/PROJECT.md and .vorch/GLOSSARY.md ← project rules you must follow
  4. the domain maps listed under `read:` in the phase file

Your job: <one-paragraph restatement of the phase goal>.
Stay inside the file-scope listed in the phase file. Write tests together with the code.
Before reporting done, run `python scripts/quality.py <the paths you touched>` and make it green;
paste the final verdict line. If you hit a decision the phase file doesn't settle, pick the most
defensible option from codebase evidence, implement it, and flag it in your final report — don't stop.
<paste §0.3 cost rules here if this phase can trigger inference>
```

After a builder reports done: skim its diff, run the quality gate yourself on the touched paths if in
doubt, then **commit one logical unit** (conventional format, see CLAUDE.md → Git) before starting
the next phase. Never commit broken code; never batch unrelated phases into one commit.

### 0.5 Final review — mandatory

After all code phases are committed, dispatch **one review subagent** (see
[`phase-8-review.md`](phase-8-review.md)) to review the whole worktree branch diff against the plan
and the handoff. Address its findings (fix or consciously defer to `.vorch/FLAGGED.md`) before
declaring merge-ready.

---

## 1. Architecture decisions (settled — do not re-litigate)

These come straight from the handoff. Builders must not redesign them.

- **Two times, one rule:** *Refresh* fetches + projects raw onto disk (dumb, needs net+key, rare).
  *Load* assembles per model from the layers into memory (smart, no net/key, frequent). A hand-edit
  to an override file takes effect on the next load — no refresh.
- **Three layers, field-level merge ("fill, don't overwrite"), highest wins per field:**
  1. `<provider>.overrides.json` (hand) — always wins.
  2. `<provider>.json` (provider layer) — what the provider/endpoint authoritatively reports,
     including a *deviating* reasoning ladder if models.dev carries one for that provider.
  3. `models.json` (canonical, inherited via the `canonical` pointer) — the base/default.
  Nested objects (e.g. `reasoning`) are replaced **wholesale**, not deep-merged (as overrides do today).
- **Canonical layer is set** (no longer in question). Open universe → hand curation doesn't scale →
  canonical base + deterministic join is the only way new providers light up with correct facts.
- **The join is enrichment, not a dependency.** Deterministic exact matches auto-join **at load**
  (exact wire-id in the canonical provider section, or exact canonical-id). **No fuzzy matcher in
  code** — a wrong auto-join silently attaches wrong facts (wrong window, wrong ladder). No safe
  match → hand `canonical: <id>` pointer in the override, or full standalone facts. A missed join is
  not damage; the model runs on provider+override data.
- **Reasoning becomes a typed field** (replaces the bare boolean): `{supported, control, levels|budget_max}`
  where `control ∈ {levels, on_off, budget}`. Derivation is data-driven from models.dev
  `reasoning_options` (effort→levels wins; else budget_tokens→budget; else toggle→on_off).
- **Canonical ladder lift mechanism (at refresh):** for canonical id `lab/X`, take the **lab
  provider** `lab`, read its model with wire-id `X`, write that model's `effort` values as the
  canonical ladder into `models.json`. **No union** across providers; provider deviations live in the
  provider layer.
- **Snapping snaps against the effective (merged) per-model ladder from the DB**, not the hardcoded
  adapter constant. The adapter constant stays only as a floor when a model has no ladder from the feed.
- **Phase-1 reasoning scope = effort/levels.** `control: budget` and `on_off` are **not yet wired**
  on the OpenAI-compat path (it only sends the `reasoning_effort` string). Budget/on_off wire support
  is a later step, explicitly out of scope here.
- **Per-model wire facts become data in a provider-scoped `metadata` blob**, not generic top-level
  fields (so Mistral's `prompt_mode` can't pollute the schema for all). The mechanics stay in the
  adapter. This generalizes the existing GitHub-Copilot pattern.
- **`context_window` becomes optional** (a missing fact stays missing; adapter/provider-config
  supplies a default at send time) — its own strand, **not** coupled to reasoning.
- **`models.json` is a full mirror of models.dev (~215+), deliberately not filtered** — many entries
  join to no configured provider today; that's wanted (a provider may add the model later).
- Reasoning-as-two-ids (xAI Grok `…-reasoning`/`…-non-reasoning`): **mirror 1:1, don't merge.**
  `structured_output` missing → `json_mode: false`.
- **No legacy compatibility** (project rule). The new on-disk schema replaces the old; old-shape files
  are simply invalid. Refresh-backed catalogs are regenerated; hand-maintained seeds are converted in
  Phase 1. No migration branches in app code; any conversion script goes in `scripts/converters/`.

---

## 2. Scope

**In:** the canonical Model DB (`models.json` + `models.overrides.json`), the per-provider layer
(`<provider>.json` + `<provider>.overrides.json`), the at-load 3-layer assembly + deterministic join
+ validator, refresh that consumes models.dev `catalog.json` and projects it, the typed reasoning
field + model-specific snapping (effort/levels), wire selectors moved into the `metadata` blob
(opencode-go `protocol`, Mistral `prompt_mode`, the `interleaved` response field, Copilot folded into
the common pattern), `context_window` optional as a separate strand, the small `/status` + effort-
dropdown UI, the observability quick wins, and all spec/domain-map/glossary updates.

**Out (explicitly):** `control: budget` and `on_off` **wire** support (data is captured, wiring is
later); native `pdf`/`video` modality wire paths (capability stored on spec, derivation untouched);
custom user-provider/-model settings UI + second read-root (designed for, not built — see handoff →
"Zu klären"; flag what the assembly must eventually support); making the Anthropic stub real.

---

## 3. Milestones

| # | Milestone | Deliverable / verifiable |
|---|---|---|
| M0 | Observability landed | 400-on-bad-effort surfaced; thinking-token mismatch logged; tests pass |
| M1 | New schema loads | Typed `reasoning` + `family` + `metadata` conventions in the dataclasses; loader + validators read the new shape; all committed seeds converted; `test_models.py`/`test_discovery.py` green |
| M2 | Assembly backbone | Loader assembles per model from 3 layers + deterministic join; canonical base loads; validator tool runs; tests prove the worked example (`deepseek-v4-pro` over two providers) |
| M3 | Refresh from catalog.json | `refresh` fetches+projects models.dev `catalog.json`, writes `models.json`(+overrides) and per-provider files with auto `canonical` pointer + deviating ladder; raw `catalog.json` kept; refresh tests green |
| M4 | Reasoning + UI | Typed reasoning derived from `reasoning_options`; adapters snap against the effective ladder; `/status` shows selected vs actual effort; effort dropdown gated by `reasoning.supported`; tests + one cheap live confirmation per allowlisted provider |
| M5 | Wire selectors as data | opencode-go `protocol`, Mistral `prompt_mode`, `interleaved` response field in `metadata`; Copilot folded in; hardcoded sets removed; tests + cheap live routing check on opencode-go |
| M6 | context_window optional | `Model.context_window: int | None`; read-side defaults at provider-config level + global floor; compaction/chat/status don't crash on `None`; tests green |
| M7 | Docs current | `models.md` rewritten; GLOSSARY + PROJECT.md context bullets updated; FLAGGED appended if needed |
| M8 | Reviewed & merge-ready | Review subagent ran; findings addressed or flagged; branch reported merge-ready to the user |

---

## 4. Phases & dependency graph

| Phase | File | Depends on | Can parallel with |
|---|---|---|---|
| 0 — Observability | [phase-0-observability.md](phase-0-observability.md) | none | anything (DB-independent) |
| 1 — Schema foundation | [phase-1-data-model.md](phase-1-data-model.md) | none | Phase 0 |
| 2 — Load-assembly + join + validator | [phase-2-load-assembly.md](phase-2-load-assembly.md) | Phase 1 | Phase 0 |
| 3 — Refresh from catalog.json | [phase-3-refresh-projection.md](phase-3-refresh-projection.md) | Phase 2 (file-format contract) | Phase 0 |
| 4 — Reasoning snapping + UI | [phase-4-reasoning-typed.md](phase-4-reasoning-typed.md) | Phase 3 | Phase 6 |
| 5 — Wire selectors in metadata | [phase-5-wire-selectors.md](phase-5-wire-selectors.md) | Phase 3 | Phase 6 |
| 6 — context_window optional | [phase-6-context-window.md](phase-6-context-window.md) | Phase 2 | Phase 5 (see note re: Phase 4) |
| 7 — Docs / specs / glossary | [phase-7-docs-specs.md](phase-7-docs-specs.md) | Phases 1–6 | — |
| 8 — Final review | [phase-8-review.md](phase-8-review.md) | Phases 0–7 | — |

**Why the core chain (1→2→3) is sequential, not parallel:** Phases 1, 2 and 3 all touch
`core/models/models.py` and/or `core/models/discovery.py` and share the on-disk file-format contract.
Running them in parallel would mean two builders editing the same files and re-deciding the same
contract — exactly the file-overlap the planner forbids. Keep them sequential. The real parallelism
lives at the edges: Phase 0 is fully independent (run it first or alongside), and Phase 6
(`context_window`) touches mostly different files than Phase 5 (adapters/overrides), so **6 ∥ 5 is
safe**. **Phase 6 ∥ Phase 4 is NOT clean** — both edit `core/tools/status.py` (Phase 4 splits the
`/status` effort display, Phase 6 makes it tolerate a `None` window) → sequence them (run Phase 4
first, then Phase 6 adapts `status.py`). **Phases 4 and 5 are NOT parallel with each other** — both
edit `mistral.py` and `openai_compatible.py`. (Phase 2 and Phase 6 both touch
`core/providers/providers.py`, but Phase 6 depends on Phase 2 so they never run concurrently.)

Recommended execution order: **0 → 1 → 2 → 3 → 4 → (5 ∥ 6) → 7 → 8.**

---

## 5. Cross-cutting rules for every phase

- **Tests live with the code** — never a trailing "write tests" phase. Each phase file assigns its
  tests to the same task as the feature.
- **Quality gate before every commit:** `python scripts/quality.py <touched paths>` (backend),
  `python scripts/quality-frontend.py <touched paths>` (frontend) — green first.
- **No magic numbers, no commented-out code, comments say *why*** (CLAUDE.md → Code quality).
- **User-facing strings through i18n** (any `/status`/UI text).
- **Domain maps are the *current* state, not the target** — when your change makes a map wrong,
  updating that map is part of *your* phase if it's localized; the big `models.md` rewrite is Phase 7.
- **Talk to the user in product language** (CLAUDE.md) — the orchestrator's status updates to the
  user are about behavior/capabilities, not file paths.

---

## 6. Risks & mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Schema change breaks every committed catalog at once → red tests between phases | High | Med | Phase 1 converts **all** committed seeds to the loadable new shape in the same commit; refresh-backed ones get regenerated in Phase 3 |
| Wrong auto-join attaches silently wrong facts | Med | High | Deterministic exact-match only, **no fuzzy**; Phase 2 ships the validator (dead pointers + redundant manual joins) |
| Live inference cost creep | Med | Med | §0.3 allowlist passed to every inference-capable builder; prefer tests + free refresh; tiny prompts, no sweeps |
| Credentials leak into the repo via worktree | Low | High | §0.2 hard rules: `.env` never committed, never logged, never under `resources/` |
| `context_window` optionality crashes a read-side caller (compaction/token budget) | Med | High | Phase 6 enumerates every read site; provider-config default + conservative global floor so nothing ever sees raw `None` |
| Builders re-decide settled architecture mid-task | Med | Med | §1 decisions are restated in each phase file's "Settled — don't redesign" box |
| models.dev structure differs from the 2026-06-15 snapshot at run time | Low | Med | Phase 3 re-verifies `catalog.json` shape against the handoff table before projecting; keep the raw dump as the safety net |

---

## 7. Decisions settled during planning (not user questions — builders must not re-open)

The handoff left these to the plan; they are now fixed:

1. **Auto-detected per-provider deviations are generated into `<provider>.json` at refresh** (handoff
   variant (i)); hand-override is for corrections only. (handoff → "Zu klären", finalized here.)
2. **The vBot↔models.dev provider-id mapping lives in `resources/providers/<provider>.json`** as an
   optional `models_dev_id` field on `ProviderConfig`, defaulting to the vBot provider id when absent.
   The provider config is the home for provider-level static facts — **not** a separate code table.
3. **Credential provisioning** for the worktree is the §0.2 recipe (`.env` + the subscription OAuth
   token file `oauth/openai-subscription.json`).
4. **Plan location** — `docs/plans/` per CLAUDE.md (matches existing plans).

**One runtime-verification item** (a check, not a decision): when wiring Phase 3, confirm each
provider's exact models.dev id (e.g. is `opencode-go` → `opencode`?) against the fetched
`catalog.providers` keys and set `models_dev_id` accordingly — never from memory.
