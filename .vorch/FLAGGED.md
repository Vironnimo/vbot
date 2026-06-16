# Flagged Concerns

Append-only log of deferred concerns. Newest at the bottom. Don't reorganize.

---

## 2026-06-07 — Compaction: deferred robustness/edge cases

Found during a review of `core/compaction/`. The items below were deliberately
**not** fixed and are recorded here.

### 1. Final-response auto-compaction rebuilds request messages then throws them away

`core/chat/chat.py`: when a turn ends with a final assistant message and no tool
calls, the loop calls `_maybe_auto_compact(...)` and assigns the result to
`messages` — but the very next line is `return assistant_message`, so that
rebuilt `messages` list is never used.

**Why it's wasteful:** the turn is already over, so the only thing that needs to
happen at this point is persisting the checkpoint (`session.append(checkpoint)`),
which sets up the *next* turn. But `_maybe_auto_compact` also runs the full
`_build_request_messages(...)` rebuild — re-assembling the system prompt and
re-running attachment resolution — purely to produce a list that the caller
immediately discards. It's CPU work with no effect.

Contrast with the **mid-tool** call site, where the rebuilt `messages` *is*
needed: there the loop continues and sends another provider request, so it must
continue with the compacted (smaller) message list. There the rebuild is
correct and necessary.

**Possible cleanup:** split the two cases so the final-response path only
persists the checkpoint and skips the request rebuild.

### 2. Investigate a precise context size during a run (mid-turn)

The auto-compaction threshold check uses the provider's *real* reported
`usage.input_tokens` at the end of a turn, but mid-turn (between tool-result
cycles) the provider hasn't reported usage for the next request yet, so it falls
back to the character heuristic `estimate_messages_tokens(messages)`
(`core/utils/tokens.py`, ~4 chars/token), which under-counts tool/function
schemas and other provider-side overhead.

**To look into:** whether a precise context size is obtainable *during* a run —
a provider token-count endpoint, a real count carried over from the previous
request's reported usage, or a tokenizer-aware local measurement — so the
mid-turn threshold no longer leans on the estimate. Today the estimate stays
conservative enough to be acceptable; this is "do better if a precise source
exists," not a known bug.

---

### 1. Local embedding engines

The registry hook for local task targets is available and dependency-free (same pattern as local STT/TTS engines), but no local embedding engine is integrated. The `EmbeddingService` currently rejects local targets with `EmbeddingUnsupportedTargetError`.

### 3. Background/write-time incremental indexing

The store uses eager-on-search backfill: the first `search` after enabling `vector` embeds all missing/stale sessions, and every subsequent search diffs freshness incrementally. A background indexer or write-hook in `core/sessions/` would eliminate the latency spike of first-search backfill for large session histories.

### 5. Embedding providers other than OpenRouter

The `core/embeddings/` domain is provider-agnostic by design, but only the OpenRouter discovery path and `/api/v1/embeddings` wire are implemented. Adding e.g. direct OpenAI, Anthropic, or local embedding providers requires supplementary discovery + provider-specific `ProviderEmbeddingClient` routing.

### 6. Asymmetric `input_type` query/document embedding

The OpenRouter embeddings API supports `input_type` hints (e.g. `"query"` vs. `"document"`) for models that optimize embeddings differently per task. Currently ignored — both queries and sessions are embedded symmetrically. Fine for general-purpose models, but some specialized embedding models (e.g. Cohere) produce better results with the hint.

---

## 2026-06-08 — Embedding input truncation: remaining deferred work

Embedding input is kept under the model's token cap by a conservative character heuristic
(`_CHARS_PER_TOKEN`/`_INPUT_TOKEN_SAFETY` in `core/recall/vector_store.py`) plus a self-correcting
shrink-and-retry loop on context-length overflow (`_run_embed` in `core/recall/vector.py`). **Still
deferred:** option **(a)** a tokenizer-aware budget (e.g. `tiktoken`) to avoid the occasional wasted
first embedding request entirely, and per-session chunking so very long sessions are not represented
by their head alone. Neither is worth building yet.

## 2026-06-09 — OpenAI provider merge: cosmetic test-fixture debt

**Purely cosmetic test-fixture debt** flagged while reviewing the merge of the `openai-subscription` provider into the existing `openai` provider (now a second `subscription` connection). Intentionally not fixed in the same change.

### 1. `OPENAI_DATA` fixture in `tests/core/providers/test_providers.py` still describes the pre-merge shape

`OPENAI_DATA` (line 30) and `OPENROUTER_DATA` (line 60) are pre-merge-era test fixtures used by generic parser tests (validation of `_parse_config`, OAuth device flow parsing, etc.). `OPENAI_DATA` still uses `adapter: "openai_compatible"` and the old `oauth` placeholder connector with `credential_key: "OPENAI_OAUTH_TOKEN"`. The new `resources/providers/openai.json` no longer has either; they were removed in Phase 5.

**Why it still passes:** these fixtures test the *generic* parser, not the real provider config. They construct arbitrary valid config dicts in memory and assert that the parser turns them into the right dataclass. The test at `test_connection_without_mode_or_models_endpoint_remains_none` (line ~860) calls `config.get_connection("oauth")` and `config.get_connection("api-key")` against this fixture — so the test still works as long as the fixture has both connections.

**Why deferred:** cleaning it up means rewriting the fixture, then chasing every test that depends on its specific shape (the `oauth` / `api-key` connection ids, the `OPENAI_OAUTH_TOKEN` env var, etc.). Pure test refactor, no production behavior. Out of scope for the merge, and the merge is a feature change, not a test-hygiene drive. Do it in a focused follow-up PR.

## 2026-06-11 — Dead-code sweep: test-only public APIs left in place

These candidates were deliberately **not** removed in the dead-code sweep because they are public
APIs exercised only by tests — possibly superseded, but deleting them means rewriting the tests that
use them:

- `core/storage/storage.py` — per-section `update_appearance_settings` / `update_skill_directory_settings` /
  `update_recall_settings` / `update_debug_settings` / `update_web_search_settings` / `update_defaults` /
  `update_compaction_settings`. Production goes through `update_settings_sections` (one transaction over
  the private `_apply_*` helpers); the public per-section wrappers are used only by
  `tests/core/storage/test_storage*.py` and mirrored by the fake in `tests/server/test_rpc.py`.
- `core/recall/vector_store.py` — `upsert_session` is unused by the vector backend (`vector.py` writes via
  `upsert_many_chunks`) but is the seeding helper for `tests/core/recall/test_vector_store.py`.
- `core/providers/github_copilot_responses.py` — `iter_responses_sse_deltas` is a stateless wrapper around
  `iter_responses_sse_deltas_with_state`; production uses only the `_with_state` variant, ~20 tests use the wrapper.

**Why deferred:** removing them is a test refactor, not a dead-code deletion — each needs its tests
rewritten against the surviving API and re-verified. Do it per-domain when those tests are touched anyway.

## 2026-06-11 — Linux readiness: remaining unverified pieces

A Linux-readiness audit found the process layer already platform-branched (POSIX kill via
`os.killpg`, `start_new_session`, bash-tool runs real `bash` off-Windows). Still open:

- **sqlite-vec on the actual Pi is unverified.** `core/recall/vector_store.py` hard-imports
  `sqlite_vec` and loads it as a native SQLite extension; needs an aarch64 wheel for the Pi's
  Python and a `sqlite3` built with extension loading. Verify on first Pi deploy (64-bit
  Raspberry Pi OS required) before enabling `recall.backend: vector`.

## 2026-06-15 — Model DB Phase 7 (docs): consciously deferred rebuild items

Gathered while making the living docs describe the as-built Model DB. None block a
configured provider today; recorded so the deliberate scope-outs have a trail. (The
unseeded canonical ladders and the github-copilot/minimax not-regenerated blockers
are already flagged above under the Phase-3 entries and are not repeated here.)

1. **`budget` / `on_off` reasoning control — no WIRE support yet.** The typed
   `reasoning.control` projects `"budget"` and `"on_off"` at refresh
   (`derive_reasoning_control` in `core/models/models_dev.py`), and the data model
   carries `budget_max`, but no adapter currently *sends* a token budget or a
   binary thinking toggle on the wire — only `"levels"` (effort) is wired through
   snapping (`closest_supported_effort`). A `budget`/`on_off` model still works
   (effort snaps against the adapter floor); it just can't use its native control
   shape. Wiring those request shapes is deferred until a reachable model needs it.

2. **Native `pdf` / `video` input modalities have no wire path.** The canonical
   projection stores modality lists VERBATIM (incl. `pdf`/`video`) from models.dev,
   and `task_types` derivation understands `file`/`video` inputs, but no provider
   adapter builds a native PDF or video request part — these modalities are carried
   as facts/filters only, not yet sent. Deferred until a target provider + use case
   exists.

3. **No second model-DB read-root for custom user providers.** `ModelRegistry.load`
   reads exactly one `<resources_dir>/models` tree and caches by that resolved path.
   A user-defined provider whose model files live outside the bundled `resources/`
   (e.g. under the data dir) has no read root and no invalidation hook. Adding a
   second root + cache-invalidation across both is deferred until custom providers
   are a feature.

4. **Anthropic is an untested stub.** `resources/models/anthropic.json` exists but
   there is no Anthropic normalizer registered in `discovery._DISCOVERY_ADAPTER_MAP`,
   so Anthropic catalogs cannot be refreshed through the pipeline. Anthropic was
   deliberately skipped in the Phase-3 regeneration (no normalizer, no key). Building
   the normalizer is deferred.

## 2026-06-15 - Model DB live-test follow-ups (opencode-go minimax-m3)

Residual cleanup/scope noted while the user live-tested the refreshed DB in the worktree.

1. **opencode-go override mixes two enrichment styles.** 16 models carry explicit
   hand `context_window`/`max_output_tokens` (the gateway's own limits, which can
   deviate from the lab), while `minimax-m3` / `qwen3.7-max` carry a `canonical`
   pointer and inherit those fields from the canonical base (the gateway matches the
   lab for them). Both are correct, but the split is inconsistent. A future cleanup
   could convert every opencode-go model whose gateway limits equal the lab's to a
   pointer (DRY, single source) and keep explicit numbers only where the gateway
   genuinely deviates. Deferred: it is an audit of all ~18 entries against canonical,
   not a behavior fix.

2. **Native `on_off`/`budget` reasoning wiring still pending (see Phase-7 item 1
   above).** The wire still sends a generic effort (or Anthropic adaptive
   thinking), not the model's native toggle/budget parameter. Wiring those request
   shapes remains deferred until a reachable model needs the native shape.

