# Phase 5 — Wire selectors become data (the provider-scoped metadata blob)

> Part of [Model DB plan](README.md). Read README §0.3 (cost rules — **this phase can trigger
> inference**) and `stuff/HANDOFF-model-db.md` (sections "Wire-Verhalten teilweise als Daten",
> "Arbeitsliste"). Depends on Phase 3 (refresh must populate the metadata). **Not parallel with Phase
> 4** (shares `mistral.py`/`openai_compatible.py`). **Parallel-safe with Phase 6.**

**Goal:** Per-model wire **facts** move out of hardcoded adapter islands into a provider-scoped
`metadata` blob on the model; the wire **mechanics** stay in the adapter. This generalizes the
existing GitHub-Copilot pattern and removes the stale/hand-maintained sets.

**Read:** `.vorch/domain-maps/providers.md`, `.vorch/domain-maps/providers/opencode-go.md`,
`.vorch/domain-maps/providers/mistral.md`, `.vorch/domain-maps/providers/github-copilot.md`,
`.vorch/domain-maps/models.md`.

**Settled — don't redesign (from §1):**
- Wire facts live in a **provider-scoped** `metadata` blob (`metadata.opencode_go.protocol`,
  `metadata.mistral.prompt_mode`, …) — **not** generic top-level fields (so one provider's quirk can't
  pollute the schema for all). The mechanics (how to build an Anthropic vs OpenAI request) stay in the
  adapter. This is exactly how `metadata.github_copilot` works today
  (`core/providers/github_copilot_policy.py`).
- The **published opencode-go protocol table is the source of truth**, NOT the current code set
  (`_ANTHROPIC_MESSAGES_MODELS` is stale/incomplete). Do not copy the old set.

## The islands this phase collects

| Island | File | Today | Becomes |
|---|---|---|---|
| opencode-go protocol routing | `core/providers/opencode_go.py` (`_ANTHROPIC_MESSAGES_MODELS` L18, used L194) | stale hardcoded frozenset | `metadata.opencode_go.protocol: "anthropic"|"openai"` per model |
| Mistral reasoning mode | `core/providers/mistral.py` (`MISTRAL_PROMPT_MODE_REASONING_MODEL_PREFIXES` L29, prompt_mode logic L113-137) | name-prefix guess | `metadata.mistral.prompt_mode` (or equivalent) per model |
| Reasoning response field | adapters' `normalize_response` (the `interleaved`/`reasoning_content`/`reasoning_details` handling) | hardcoded/probed | `metadata.<provider>.reasoning_response_field` (`reasoning_content` 479, `reasoning_details` 15, `true` 30) → data-driven `normalize_response` |
| Copilot family/endpoint | `core/providers/github_copilot_policy.py` | family-from-name guess | family from `Model.family` (Phase 3); endpoint *decision* stays |

### opencode-go published protocol table (source of truth)

```
openai-compatible: glm-5.1, glm-5, kimi-k2.7, kimi-k2.6, deepseek-v4-pro, deepseek-v4-flash,
                   mimo-v2.5, mimo-v2.5-pro
anthropic:         minimax-m3, minimax-m2.7, minimax-m2.5, qwen3.7-max, qwen3.7-plus, qwen3.6-plus
```
(`…/v1/chat/completions` = openai, `…/v1/messages` = anthropic.) A new model **without** an entry must
not be silently misrouted — pick a safe default (openai-compatible) and log when an unknown opencode-go
model is routed by default.

## Tasks

- **opencode-go `protocol` field** — populate `metadata.opencode_go.protocol` per model (source: the
  table above, lives in the opencode-go override/catalog since the endpoint returns bare IDs), and
  make `opencode_go.py` route on it instead of `_ANTHROPIC_MESSAGES_MODELS`. Remove the frozenset. —
  files: `core/providers/opencode_go.py`, `resources/models/opencode-go.overrides.json` (and/or the
  generated catalog), tests: `tests/core/providers/test_opencode_go.py`.
- **Mistral `prompt_mode` field** — drive the magistral reasoning-mode decision from
  `metadata.mistral.*` instead of the name prefix; remove the prefix tuple. — files:
  `core/providers/mistral.py`, the mistral catalog/override; tests:
  `tests/core/providers/test_mistral.py`.
- **Data-driven reasoning response field** — read `metadata.<provider>.reasoning_response_field` (from
  `interleaved`) in `normalize_response` so the field the reasoning comes back in is data, not
  hardcoded. — files: the affected adapter(s) (`openai_compatible.py` and any provider override),
  tests in the owning adapter test modules.
- **Copilot fold-in** — replace family-from-name guessing with `Model.family` (now provided by Phase
  3); keep the endpoint decision. Confirm nothing else still guesses what's now data. — files:
  `core/providers/github_copilot_policy.py`; tests: `tests/core/providers/test_github_copilot_policy.py`.
- **Sweep for more islands** — the handoff warns the island list is "erfahrungsgemäß nicht vollständig".
  Grep for other per-model hardcoded sets/prefix tricks while here and fold any found into the same
  pattern (or flag to `.vorch/FLAGGED.md` if out of this phase's reach). — files: as found; note in
  FLAGGED if deferred.

**Done when:**
- opencode-go routes by `metadata` `protocol`, the stale frozenset is gone, and a test proves a model
  marked `anthropic` hits the messages route and one marked `openai` hits chat/completions, plus an
  unknown model defaults safely with a log.
- Mistral's reasoning mode and the reasoning-response field are data-driven; the name-prefix tuple is
  removed; tests prove both shapes.
- Copilot no longer guesses family.
- **One** cheap live routing check: send a tiny prompt to `opencode-go/deepseek-v4-flash` (openai
  route) and `opencode-go/mimo-v2.5`, confirm correct routing + a clean response (README §0.3). Tiny
  prompts only.
- `python scripts/quality.py <touched paths>` is green.

**Risks / notes:**
- The opencode-go endpoint returns bare IDs → its protocol facts are hand/override-driven but enriched
  via the canonical join (Phase 2). Keep the table in the override file, not in code.
- Shares `mistral.py`/`openai_compatible.py` with Phase 4 → run **after** Phase 4 (or strictly
  sequenced), never concurrently.
- Don't move *mechanics* into data — only facts. The "how to build the request" stays in the adapter.
