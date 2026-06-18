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

## 2026-06-11 — Linux readiness: remaining unverified pieces

A Linux-readiness audit found the process layer already platform-branched (POSIX kill via
`os.killpg`, `start_new_session`, bash-tool runs real `bash` off-Windows). Still open:

- **sqlite-vec on the actual Pi is unverified.** `core/recall/vector_store.py` hard-imports
  `sqlite_vec` and loads it as a native SQLite extension; needs an aarch64 wheel for the Pi's
  Python and a `sqlite3` built with extension loading. Verify on first Pi deploy (64-bit
  Raspberry Pi OS required) before enabling `recall.backend: vector`.

## 2026-06-15 — Model DB Phase 7 (docs): consciously deferred rebuild items

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


## 2026-06-16 - Provider usage probe: blind Copilot/MiniMax fetchers

The live provider usage probe (`core/providers/usage.py`, Statistics → Limits subtab)
ships with one live-verified fetcher and two blind ones.

1. **OpenAI `openai:subscription` is live-verified** (2026-06-16, HTTP 200): the real
   `/wham/usage` body matches openclaw's shape and the parser yields correct 5h +
   weekly windows. No follow-up.

2. **GitHub Copilot `github-copilot:oauth` is blind.** `copilot_internal/user` parsing
   is implemented from openclaw's field names but not live-verified (no Copilot login in
   this environment). Pinned by unit tests; degrades to an "unavailable" snapshot on
   shape mismatch. Live-verify when a Copilot login exists.

3. **MiniMax `minimax:api-key` is blind.** `token_plan/remains` parsing — especially the
   remaining-vs-total count keys (MiniMax misnames "usage" as "remaining") — is an
   assumption pinned only by unit tests. Live-verify when a MiniMax Token Plan key is
   available; the percent could be inverted if the count semantics differ.


## 2026-06-17 — Native reasoning wiring landed; provider assumptions unverified

The unified reasoning-intent layer is built and all five adapters render through it
(`resolve_reasoning_intent` in `core/providers/reasoning.py`). This **resolves** the
previously-flagged "no wire support for `budget`/`on_off`" items (2026-06-15 Phase-7 #1
and 2026-06-15 live-test #2): budget Claudes now send native `thinking_budget`, `on_off`
models toggle natively, and `/status` reports the rendered budget/state. Remaining
unverified pieces (no credentials in this environment — the Phase-0 gate could not probe):

1. **Anthropic `budget_max` numbers are hand-seeded, not live-verified.**
   `resources/models/anthropic.overrides.json` seeds `control: budget` +
   `budget_max` for `claude-opus-4-20250219` (32000) and `claude-sonnet-4-20250219`
   (64000), using each model's max output as the budget ceiling. The feed leaves
   `budget_max` `None` for every Claude, so these are conservative estimates pinned by
   tests. Live-verify the accepted thinking budget (and whether these older Claudes
   accept `budget_tokens` vs the newer adaptive `output_config.effort`) when Anthropic
   credentials exist; correct the override numbers if the API disagrees.

2. **OpenRouter `on_off` off-shape is assumed.** A `none` selection on an `on_off`
   OpenRouter model renders `reasoning: {enabled: false}` (a documented OpenRouter
   param); the exact disable shape (`enabled:false` vs `exclude` vs omit) was not
   probed. Effort-spelled-off wires keep the byte-identical `{effort: "none"}`.
   Live-verify when an OpenRouter key + a reachable `on_off` model exist.


## 2026-06-18 — Projects (Plan 1, kern+cli): consciously deferred edges

Found during a plan-vs-code audit of the project feature. Each is a small,
out-of-scope-for-Plan-1 gap, deliberately not fixed.

1. **`channel` tool still resolves a path against `workspace`, not `effective_cwd`.**
   The workspace→cwd switch covered `read`/`write`/`edit`/`search`/`grep`/`glob`/`bash`
   (and `memory` deliberately stays on workspace). The `channel` tool
   (`core/tools/channel.py`, the path-resolving line) was not switched. Low impact:
   channels-on-project is deferred, so a channel session never carries a project cwd and
   `effective_cwd` would fall back to `workspace` anyway. Switch it to `effective_cwd`
   when channels learn projects, for consistency.

2. **Project-agent prompt preview has no project context.** `prompt.preview`
   (`server/rpc/operations_methods.py`) calls `build_system_prompt` without a project
   context, so previewing a config/project agent shows the body but **not** the project
   files (`{project_files}`). The preview RPC has no `project_id` param. Wire project
   context through the preview path when the WebUI project-agent preview lands (Plan 2).

3. **`/status` in a project session degrades to empty.** The `CommandDispatcher` carries
   no `project_id`, so the `/status` slash command's agent lookup
   (`core/chat/commands.py`, `self._agents.get`) stays identity-only and returns
   `agent=None` for a project session (handled, not a crash). Threading `project_id`
   through the dispatcher to resolve the config agent is a broader change, out of M5
   scope.


## 2026-06-18 — Projects (Plan 2, WebUI): project-agent run loses the /ws backstop

Found while reviewing the Phase 2 two-bar chat. One out-of-scope (backend) gap,
deliberately not fixed in the client-only Plan 2.

1. **`/ws` run-lifecycle events carry no `project_id`, so a project-agent run is not
   re-attached through the WebSocket backstop.** The WebUI keys a project-agent's
   session state by the full `agent@projekt` address (so chat/session/history address
   correctly — RPC-contract trap 2), but the server's run lifecycle event
   (`RunEvent.to_dict()` in `core/runs/runs.py`) serializes only the bare `agent_id`
   (the project dimension rides the in-memory `Run`, not the event). So
   `chatRunStream.handleRunServerEvent` (`webui/src/lib/chatRunStream.js`), which keys
   on the bare `agent_id`, builds `builder::<session>` and never matches the
   address-keyed `builder@vbot::<session>` displayed session — the `/ws` re-attach /
   cross-session tracking path is inert for project agents. **Primary SSE foreground
   streaming is unaffected** (the send attaches directly to the run's `sse_url`), so a
   project-agent run streams live normally; only the WebSocket reconnect/catch-up
   backstop and cross-session sub-agent status are missed, and the run still completes
   server-side and shows on the next history load. Fixing it needs a backend change —
   add `project_id` to the run lifecycle event payload so the client can rebuild the
   address key — which is out of scope for the client-only Plan 2.
