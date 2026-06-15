# Phase 3 — Refresh = fetch models.dev catalog.json + project + lift the ladder

> Part of [Model DB plan](README.md). Read README §1 and `stuff/HANDOFF-model-db.md` (sections
> "Datenquelle: models.dev", "reasoning_options — das Herzstück", "Feld-Mapping", "Feld-Projektion")
> first. Depends on Phase 2 (must produce exactly the file shape Phase 2's loader reads).

**Goal:** `refresh` becomes the dumb half of the model: it **fetches** (provider APIs + models.dev
`catalog.json`) and **projects** onto disk — no cross-provider joins, no cross-file merging (that's
load's job). It writes the canonical `models.json` (with the lifted reasoning ladder), the per-provider
`<provider>.json` (with an auto `canonical` pointer and any provider-reported deviating ladder), and
keeps a raw `catalog.json` dump as the safety net.

**Read:** `.vorch/domain-maps/models.md`, `.vorch/domain-maps/providers.md`, and the provider child
maps for any provider whose normalization you touch.

**Settled — don't redesign (from §1 + handoff):**
- **Consume `catalog.json`** (not `models.json`/`api.json`): `{ "models": {…}, "providers": {…} }` in
  one fetch, consistent. Only the provider side carries `reasoning_options`, per-provider limits, cost.
- **`reasoning.control` derivation (data-driven):** `reasoning_options` has `effort` → `levels` (with
  its `values`); else `budget_tokens` → `budget`; else `toggle` → `on_off`. **Effort wins if present.**
- **Canonical ladder lift:** for canonical id `lab/X`, take the **lab provider** `lab`, read its model
  with wire-id `X`, write that model's `effort` values as the canonical `levels` into `models.json`.
  **No union.** ~184/215 lift 1:1; ~27 are keyed differently at the lab → hand/agent path; 4 have no
  lab provider → hand. The ~31 rest reuse the same hand path as the join.
- **Field projection** ("keep what plausibly helps; storage is cheap"): keep `id, name, family,
  modalities, limit, reasoning, reasoning_options, tool_call, structured_output, temperature, cost,
  status, interleaved, knowledge, release_date, last_updated, experimental`. Drop `benchmarks`,
  `weights`, `provider` (redundant), `open_weights`, `attachment` (derive file capability from
  `modalities`). Store modalities **verbatim** (incl. `pdf`/`video`) — no normalization, no task
  derivation change here.
- **Auto-detected per-provider deviations are generated into `<provider>.json` at refresh** (decision
  (i) from handoff "Zu klären"); hand-override stays for corrections only.
- **`models.json` is a full mirror** of models.dev (~215+) — do not filter.
- `structured_output` missing → `json_mode: false`. Reasoning-as-two-ids → mirror 1:1.

## Field mapping (models.dev → vBot) — implement exactly this

| models.dev | vBot | Note |
|---|---|---|
| `tool_call` | `capabilities.tools` | |
| `structured_output` | `capabilities.json_mode` | missing → `false` |
| `reasoning` (bool) | `capabilities.reasoning.supported` | |
| `reasoning_options[]` | `reasoning.control` + `levels`/`budget_max` | per derivation above |
| `modalities.input/output` | `input_modalities`/`output_modalities` | verbatim, incl. `pdf`/`video` |
| `temperature` (bool) | `supported_parameters` candidate | real signal (some `false`) |
| `family` | `Model.family` | replaces Copilot's family-from-name guessing |
| `limit.context`/`limit.output` | `context_window` / `max_output_tokens` | **per-provider** values from `catalog.providers` |
| `cost`, `status`, `interleaved` | keep | cost · `deprecated`/`beta` later used to hide · reasoning response field |
| `knowledge`, `release_date`, `last_updated`, `experimental` | keep, unused for now | on reserve |

## Tasks

- **models.dev client** — fetch `catalog.json` once, parse `{models, providers}`. Re-verify the shape
  against the handoff table before projecting; abort with a clear error if it diverges. Reuse the
  project's HTTP + retry conventions (`retry_async`, `classify_http_status`, `wrap_network_error`;
  providers.md → transient handling). — files: a new `core/models/models_dev.py`; tests with a fixture
  capture of `catalog.json`.
- **Raw safety net** — write the raw `catalog.json` dump to disk (analog to `<provider>.raw.json`,
  e.g. `resources/models/models.dev.catalog.raw.json`), so a later wanted field is a projection edit,
  not a re-fetch. — files: `models_dev.py` / `discovery.py`.
- **Project the canonical base** — build `resources/models/models.json` keyed by canonical id from
  `catalog.models`, applying the field projection above, **plus the lifted reasoning ladder** from the
  lab provider. Write `resources/models/models.overrides.json` as the hand part for the ~31 lab-keying
  / no-lab-provider cases (seed it with the known ones; structure for the rest). — files:
  `core/models/discovery.py` (+ `models_dev.py`); tests.
- **Confirm & set `models_dev_id`** — Phase 2 added the optional `models_dev_id` field to
  `ProviderConfig` / `resources/providers/<provider>.json`. Here, look up each configured provider's
  **exact** models.dev id in the fetched `catalog.providers` keys (e.g. confirm whether `opencode-go`
  is their `opencode`) and set `models_dev_id` where it differs from the vBot id. Never guess — verify
  against the fetched keys. — files: `resources/providers/<provider>.json`.
- **Project the per-provider layer** — extend `refresh_models()` so each `<provider>.json` model
  carries: its wire facts, an **auto `canonical` pointer** where the wire-id exactly matches a canonical
  provider section (looked up via `models_dev_id`), and a **deviating reasoning ladder** when
  `catalog.providers[<provider>]` reports one different from the lab spec. The reasoning `control`
  derivation runs here too (per-provider). —
  files: `core/models/discovery.py`; the per-adapter `normalize_catalog_entry` where provider-specific
  (`OpenRouterAdapter`, `MistralAdapter`, `MiniMaxAdapter`, `GitHubCopilotAdapter`, etc.); tests +
  fixtures: `tests/core/models/test_discovery.py`, `tests/core/providers/test_<provider>.py`.
- **Regenerate the committed refresh-backed catalogs** to the final shape (this supersedes Phase 1's
  mechanical transform for `openai`, `openrouter`, `mistral`, `github-copilot`). Refresh is free (no
  inference) — run it against a credentials-bearing data dir (README §0.2) and commit the result. —
  files: `resources/models/<provider>.json`, `resources/models/models.json`,
  `resources/models/models.overrides.json`.

**Done when:**
- A refresh against a fixture `catalog.json` produces `models.json` (canonical, with lifted ladders)
  and per-provider files (with `canonical` pointers + deviating ladders) in exactly the shape Phase 2's
  loader reads — proven by a test that refreshes then loads via `ModelRegistry`.
- The `deepseek-v4-pro` data really exists end-to-end: canonical `[high, max]`, OpenRouter `[high, xhigh]`.
- The reasoning `control` derivation is unit-tested across `effort`, `budget_tokens`, `toggle`, and
  "effort wins when both present".
- The raw `catalog.json` dump is written and not read by the runtime read path.
- `python scripts/quality.py core/models resources` (precise touched paths) is green.

**Risks / notes:**
- **Match Phase 2's file-format contract exactly** — read its deliverable note first. If you find the
  loader expects a different shape than is natural to write, reconcile with the orchestrator, don't
  fork the format.
- Catalog refresh is **free** (no inference) — use it liberally to validate. Do **not** send inference
  here.
- The lab-keying mismatches (~27) and no-lab-provider (4) are a known hand path — seed the ones the
  handoff names, leave a clear structure (and a `FLAGGED.md` note) for the rest rather than guessing.
- Keep refresh "dumb": no cross-provider joins, no cross-file merge. If you're tempted to merge layers
  here, that belongs in load (Phase 2).
