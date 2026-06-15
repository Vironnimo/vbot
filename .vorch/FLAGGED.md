# Flagged Concerns

Append-only log of deferred concerns. Newest at the bottom. Don't reorganize.

---

## 2026-06-07 — Compaction: deferred robustness/edge cases

Found during a review of `core/compaction/`. The main bug (every compaction
re-summarized the full raw history instead of only the delta since the last
checkpoint) was fixed the same day. The items below were deliberately **not**
fixed and are recorded here.

### 1. Small context windows (< ~32k) are not actively supported

**Decision:** Assume a context window of at least 32k tokens. We don't want to
lock smaller models out, but we don't actively support them either.

**Why it can break below ~32k:** Auto-compaction triggers when
`input_tokens / context_window >= threshold` (default `threshold = 0.8`). After
compacting, the next request is roughly `summary + preserved_tail`, where the
preserved tail targets `tail_tokens` (default `15_000`). If `tail_tokens` is
larger than `threshold * context_window`, the preserved tail *alone* already
sits above the trigger threshold — so the very next turn triggers compaction
again, but there is nothing left to remove. Worked example: a 16k model has a
trigger point at `0.8 * 16000 = 12800` tokens, but the tail target is `15000`,
so compaction can never bring usage back under the threshold → it re-fires every
turn (each firing is an LLM call) without ever helping.

At 32k this does not happen: trigger point `25600`, tail `15000` → after
compaction we sit around `15000 + summary`, comfortably under the threshold.

**Residual edges that exist even at 32k+ (low likelihood, left unguarded):**

- **`tail_tokens` is a floor, not a cap.** `find_tail_boundary` always preserves
  *at least* the whole most-recent turn before checking the budget
  (`core/compaction/compaction.py`, the `boundary_index = start_index` line runs
  before the `>=` check). So a single turn with very large tool output can push
  the preserved tail far past `tail_tokens`. There is no clamp of `tail_tokens`
  against the context window anywhere.
- **Empty-delta compaction does a redundant LLM call.** If compaction is invoked
  but nothing has been added since the last checkpoint's boundary
  (`pre_tail_messages` is empty), the strategy still calls the summary model with
  `"(no history before boundary)"` and re-emits essentially the previous summary.
  Harmless, but a wasted call. Not expected at 32k (the trigger math above means
  there is always a real delta to fold in), so left unguarded.

**If we ever want to support small windows:** clamp the effective tail to
something like `min(tail_tokens, ~0.5 * context_window)`, and skip compaction
when the projected result would still be above the threshold or when the delta
is empty.

### 2. Final-response auto-compaction rebuilds request messages then throws them away

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

### 3. Token source differs between the two auto-compaction trigger points

The "are we over the threshold?" check uses a different token number depending on
where it runs:

- **After a final assistant response:** it uses the provider's *real* reported
  `usage.input_tokens` — accurate.
- **After a mid-turn tool-result cycle:** the provider hasn't reported usage for
  the next request yet, so it falls back to the local heuristic
  `estimate_messages_tokens(messages)` (`core/utils/tokens.py`, ~4 chars/token).

**Why it matters:** the heuristic ignores tool/function schemas and other
provider-side overhead, so it tends to *under*-count. The practical effect is
that mid-turn compaction can trigger a bit later than the real context pressure
would warrant — i.e., the threshold behaves slightly differently mid-turn vs.
end-of-turn. Not wrong, just inconsistent. Acceptable as long as the heuristic
stays conservative-ish; worth revisiting only if mid-turn overflows show up.

### 4. The summary is injected as a second consecutive `user` message

When a checkpoint exists, `_build_request_messages` (`core/chat/chat.py`) emits
the summary as a synthetic `role: "user"` message wrapped in `<system-reminder>`
tags, placed immediately before the preserved tail. The tail itself always
starts on a `user` message (boundary invariant). So the provider request
contains **two `user` messages in a row** (summary, then the boundary turn).

**Why to keep an eye on it:** some provider wire protocols (notably Anthropic's
Messages API) historically expected strictly alternating user/assistant roles.
Most adapters tolerate or merge consecutive same-role messages, and this codebase
already injects notes/system-reminders as synthetic user messages elsewhere, so
it's *probably* fine — but it has not been explicitly verified for the summary
injection path against every adapter. If an adapter ever rejects consecutive
user turns, this is where it would surface. Worth a one-time check against the
Anthropic adapter rather than a fix.

---

## 2026-06-08 — Memory/recall: semantic layer wanted + review findings

Found during a review of the `memory` tool (`core/memory/`, `core/tools/memory.py`)
and `session_search` (`core/recall/`, `core/tools/session_search.py`). One bug was
fixed the same day: `sqlite_fts` matched whole words only (`unicode61`), so `gpt`
never found `gpt4o` — switched to the `trigram` tokenizer for substring matching
(commit `fix(recall): match substrings in sqlite search via trigram tokenizer`).
The items below were deliberately **not** done and are recorded here.

### 1. Semantic / "finds similar" recall is the actually-wanted feature, still unbuilt

**What we want:** a real long-term memory where the agent finds *related* past
sessions even when the words differ ("car" surfaces "vehicle"). That is
**meaning-based search via embeddings/vectors**, a different mechanism from the
current keyword search. The keyword index (`sqlite_fts`) can never do this no
matter how it's tuned — even after the trigram fix it's still substring/keyword.

**Where it fits (already designed for):** the original design
(`stuff/researches/hermes-memory-system-research.md`, derived from Nous' Hermes
agent) is layered: (1) pinned memory, (2) searchable JSONL sessions, (3) optional
SQLite keyword index for speed, (4) **semantic/provider layer** — explicitly the
*last* phase ("Later" → `VectorRecallBackend` / memory providers). Layers 1-3 are
built; layer 4 was never started. That is the source of the "what do we even have"
confusion: the SQLite piece is the keyword speed-index from the plan, not the
semantic layer.

**Good news on effort:** the recall layer was built swappable on purpose.
`RecallBackend` (Protocol) + `RecallBackendRegistry` in `core/recall/recall.py`
register backends by name (`jsonl_scan`, `sqlite_fts` today) and the active one is
chosen via `settings.json` `recall.backend` with hot-reload. A `vector`/`semantic`
backend slots in alongside the existing two — it's an addition, not a rewrite.

**Open questions to settle when we plan it:** local embedding model vs. a cloud
provider (vBot already has providers wired, no embedding capability yet — grep for
`embedding`/`vector` returns nothing in `core/`); whether to store vectors in
SQLite (e.g. `sqlite-vec`) or elsewhere; and whether to do hybrid keyword+semantic
(usually best) rather than semantic-only. Not planned in detail yet.

### 2. Memory tool has an unguarded read-modify-write race (silent lost updates)

Tool calls within one assistant turn run **concurrently** (`asyncio.gather`,
per-run limit `DEFAULT_TOOL_CONCURRENCY_LIMIT = 50` in `core/tools/tools.py`). The
memory handler runs its work in a thread (`asyncio.to_thread`, `core/tools/memory.py`)
and `FilePinnedMemoryBackend` does an unlocked read-modify-write
(`add_entry`/`replace_entry`/`remove_entry` in `core/memory/memory.py`): read all
entries → mutate list → atomic file replace. If the model issues two memory
mutations in the same turn, both read the same starting list and the last writer
wins — **one entry is silently lost**, while both tool calls report success. The
atomic `os.replace` only prevents half-written files, not lost updates.

**Fix direction:** a per-workspace-file lock (or an async lock keyed by file path)
around the read-modify-write, so concurrent mutations serialize. Hermes guards the
equivalent path with file locks + reload-under-lock; we have neither.

### 3. `sqlite_fts` still diverges from JSONL in ordering & truncation (left on purpose)

The substring fix made *what matches* agree between the two backends, but not
*which/how many/what order*. JSONL collects matches grouped by session recency
then in-file order, scanning until it has `limit` real hits
(`message_search_result` in `core/recall/jsonl.py`). SQLite orders globally by
message timestamp and fetches `limit + 1` candidates before re-validation
(`_query_matches` / `_search_with_sqlite` in `core/recall/sqlite_fts.py`), so it
can return fewer than `limit` and mislabel `truncated` when re-validation drops
candidates, and its "top N" differs. The user explicitly does **not** require
backend parity, so this is intentionally left. Also minor: the `trigram` tokenizer's
case folding isn't identical to Python `.casefold()` for exotic cases (e.g. `ß`↔`ss`);
since re-validation only *removes* candidates, this can cause rare false negatives
in `sqlite_fts` that the JSONL scan would find. Acceptable.

### 4. Memory file: asymmetric dash escaping + non-bullet lines dropped

In `core/memory/memory.py` the read path unescapes `\-`→`-` (`_unescape_entry_line`)
but the write path never produces `\-` (`_escape_entry_content` only collapses
newlines). Consequences: (a) the documented leading-dash protection doesn't actually
happen — it round-trips only incidentally because the bullet prefix is `"- "` with a
space; (b) an entry whose content contains the literal substring `\-` is silently
corrupted to `-` on read. Either add the escape on write or drop the dead unescape
(and fix `memory.md`, which still claims `\-` is written).

Separately, any non-bullet prose a user hand-writes *inside* the `## Entries` section
is silently dropped on the next tool mutation (`_parse_memory_text` keeps only bullet
lines). Mostly by-design ("the tool only curates bullets") but a data-loss footgun
worth a doc note or guard.

---

## 2026-06-08 — Semantic recall: deferred follow-up work

The `vector` RecallBackend (sqlite-vec + OpenRouter embeddings) shipped in commit `merge: add vector recall backend with sqlite-vec semantic search`. The following were deliberately left for later:

### 1. Local embedding engines

The registry hook for local task targets is available and dependency-free (same pattern as local STT/TTS engines), but no local embedding engine is integrated. The `EmbeddingService` currently rejects local targets with `EmbeddingUnsupportedTargetError`.

### 2. Hybrid keyword+semantic ranking

The `vector` backend ranks purely by cosine distance — semantic only. A hybrid ranking that combines keyword matches (FTS) with semantic proximity would produce better results but requires its own ranking model and integration design.

**Confirmed 2026-06-08:** a live single-word query (`Bild`) returned weak, undifferentiated matches — distances all bunched at 0.52–0.63 (cosine sim ~0.37–0.48), top hit unrelated ("mach mal das licht an"). For short keyword queries the existing `sqlite_fts` path is actually more precise; semantics only pays off when wording differs (the stated goal). Hybrid (FTS precision + vector synonym recall) is the right long-term ranking. **Deferred on purpose** behind per-session chunking (#4), which is being implemented first — much of the "Bild" weakness was the coarse one-vector-per-session granularity, not ranking. Revisit hybrid after chunking lands and re-evaluate whether it's still needed.

### 3. Background/write-time incremental indexing

The store uses eager-on-search backfill: the first `search` after enabling `vector` embeds all missing/stale sessions, and every subsequent search diffs freshness incrementally. A background indexer or write-hook in `core/sessions/` would eliminate the latency spike of first-search backfill for large session histories.

### 4. Per-session chunking for very long sessions ✅

**Done** in commit `feat(recall): per-session chunking with batched embedding and ranked hydration` (2026-06-08). Sessions are now split into message-window chunks (target 1500 chars, 1-message overlap, per-message cap 2000 chars), each embedded separately, with KNN dedup to the best chunk per session and a `_MAX_DISTANCE` relevance cutoff. The snippet and hydration window now reflect the matched region, not the session opener.

~~Sessions longer than the embedding model's `context_length` are truncated to fit. Per-session chunking (split a session into multiple vectors, merge results) is deferred until session lengths actually exceed typical embedding model context windows (commonly 8k–32k tokens).~~

### 5. Embedding providers other than OpenRouter

The `core/embeddings/` domain is provider-agnostic by design, but only the OpenRouter discovery path and `/api/v1/embeddings` wire are implemented. Adding e.g. direct OpenAI, Anthropic, or local embedding providers requires supplementary discovery + provider-specific `ProviderEmbeddingClient` routing.

### 6. Asymmetric `input_type` query/document embedding

The OpenRouter embeddings API supports `input_type` hints (e.g. `"query"` vs. `"document"`) for models that optimize embeddings differently per task. Currently ignored — both queries and sessions are embedded symmetrically. Fine for general-purpose models, but some specialized embedding models (e.g. Cohere) produce better results with the hint.

---

## 2026-06-08 — Embedding input truncation is a character heuristic, not tokenizer-accurate

Found while debugging a real bge-m3 failure: a German+English session embedded to **8193 tokens** against the model's 8192 cap (OpenRouter returned the upstream `BadRequestError` as a 200-wrapped `error` object; the recall backend then fell back to JSONL scan). Root cause: `VectorStore.truncate_to_input_limit` (`core/recall/vector_store.py`) caps by **characters** using an assumed chars-per-token ratio, and the old default budget (32_000 chars ≈ 4.0 chars/token, no margin) overflowed because mixed German text tokenizes denser (~3.9 chars/token observed; compounds/umlauts/code can go lower). Also, the first-run backfill passed `context_window=None` because `_truncate_to_input_limit` gated on `_resolved_header` (only set *after* the first embed) instead of the already-available binding header.

**Fixed the same day** (commit `fix(recall): keep embedding input under the token cap for dense/German text`): assume a conservative `_CHARS_PER_TOKEN = 3` with `_INPUT_TOKEN_SAFETY = 0.9` headroom, default to the 8192-token floor when the window is unknown, and resolve the context window from the binding header on the first backfill.

**Update (same day): the heuristic alone was not enough.** After the conservative heuristic shipped, the same session still overflowed (the provider reports "at least 8193" — it stops counting at cap+1, so the number never reflects the real size and gave a false "no change" signal). Fix **(b)** was then implemented (commit `fix(recall): retry embedding at half length on context-length overflow`): `_run_embed` in `core/recall/vector.py` catches the provider's context-length rejection (`_is_context_overflow`) and halves the over-long inputs, retrying up to `_EMBED_OVERFLOW_RETRIES` (6) times until they fit. This is tokenizer- and language-independent and self-correcting; the character heuristic now just minimizes how often the shrink loop is needed. **Remaining (still deferred):** option **(a)** a tokenizer-aware budget (e.g. `tiktoken`) to avoid the occasional wasted first request entirely, and the existing per-session chunking item (#4 above) so very long sessions are not represented by their head alone. Neither is worth building yet.

## 2026-06-09 — OpenAI provider merge: cosmetic test-fixture debt

The `openai-subscription` provider was collapsed into the existing `openai` provider as a second `subscription` connection (commit `merge: collapse openai-subscription into single openai provider`). All quality gates green (3114 backend + 585 frontend). The items below are **purely cosmetic test-fixture debt** flagged during the review and intentionally not fixed in the same change.

### 1. `OPENAI_DATA` fixture in `tests/core/providers/test_providers.py` still describes the pre-merge shape

`OPENAI_DATA` (line 30) and `OPENROUTER_DATA` (line 60) are pre-merge-era test fixtures used by generic parser tests (validation of `_parse_config`, OAuth device flow parsing, etc.). `OPENAI_DATA` still uses `adapter: "openai_compatible"` and the old `oauth` placeholder connector with `credential_key: "OPENAI_OAUTH_TOKEN"`. The new `resources/providers/openai.json` no longer has either; they were removed in Phase 5.

**Why it still passes:** these fixtures test the *generic* parser, not the real provider config. They construct arbitrary valid config dicts in memory and assert that the parser turns them into the right dataclass. The test at `test_connection_without_mode_or_models_endpoint_remains_none` (line ~860) calls `config.get_connection("oauth")` and `config.get_connection("api-key")` against this fixture — so the test still works as long as the fixture has both connections.

**Why deferred:** cleaning it up means rewriting the fixture, then chasing every test that depends on its specific shape (the `oauth` / `api-key` connection ids, the `OPENAI_OAUTH_TOKEN` env var, etc.). Pure test refactor, no production behavior. Out of scope for the merge, and the merge is a feature change, not a test-hygiene drive. Do it in a focused follow-up PR.

### 2. Test function and fixture names still carry the old `subscription` / `codex` / `oauth` substrings

Several test names and fixture names from earlier in the project still reference the pre-merge terminology. Functionally correct (they exercise the new code path), but reads weird next to the merged provider:

- `tests/core/models/test_discovery.py:144` — fixture function `openai_subscription_config()` now builds a merged `openai` provider with a `subscription` connection. The name is misleading; the function itself is fine.
- `tests/core/providers/test_providers.py:312` — `test_openai_subscription_oauth_device_flow_fields_parse` constructs a temp config with `id: "openai-subscription"` / `adapter: "openai_subscription"` and asserts generic OAuth device-flow parsing. The parser is provider-agnostic, so the test is valid; the name is the only thing that no longer reflects reality.
- `tests/core/providers/test_providers.py:814` — `test_openai_subscription_connection_parses_mode_and_models_endpoint` now tests `id: "openai"` (correct, post-merge) but the function name still says `subscription` (technically accurate — the connection *id* is `subscription` — but easy to misread as the old provider id).
- `webui/src/components/__tests__/DebugView.test.js:93,108` — literal string `openai-subscription-with-a-very-long-name` is a length-testing fixture, not a provider id; left alone on purpose.

**Why deferred:** same as #1 — cosmetic, low risk of confusion in practice (the function body is what matters), and a follow-up rename touches many call sites that would each need re-verification. Not blocking.

### 3. `OpenAIAdapter._build_codex_headers` still defensively merges `self._config.extra_headers`

`core/providers/openai.py:173-174` merges `self._config.extra_headers` into the Codex request headers in addition to the adapter-owned `CODEX_EXTRA_HEADERS`. The domain map (`openai.md`) forbids provider-level `extra_headers` for the OpenAI provider, and the new `resources/providers/openai.json` no longer has the field. The merge is defensive belt-and-suspenders code.

**Why it can be removed safely:** with `extra_headers` gone from the JSON, the merge is a no-op. If a future contributor adds `extra_headers` back to the provider config, the merge would silently re-introduce the leak that Phase 5 was designed to prevent. The current implementation is correct but offers a backdoor.

**Why deferred:** removing it is a one-line change, but it changes behavior under a (currently unused) configuration shape. A test would have to assert that adding `extra_headers` to the provider JSON does *not* cause Codex headers to appear on the wire in the default mode — which is already tested by `test_default_mode_send_targets_chat_completions_endpoint`. Likely safe to remove; better as a deliberate follow-up.

## 2026-06-11 — One-off vitest suite-level failure under quality-frontend.py with mixed path targets

A `python scripts/quality-frontend.py <4 source files + 3 test files>` run failed its vitest gate with `TypeError: Cannot read properties of undefined (reading 'config')` thrown at top-level `describe(...)` in ~10 test files, including files untouched by the change (`toastState.test.js`, `wakewordSettings.test.js`). Re-running the exact same vitest invocation (`npx vitest run --reporter=verbose src/lib src/lib src/components/chat src/components <3 test files>`, overlapping/duplicate directory targets included) passed 36/36 files, as did file-scoped and full-scan runs immediately after.

**Why deferred:** not reproducible in three attempts — looks like a transient Vitest 4 worker/context crash, not a target-translation bug in `quality-frontend.py`. Nothing actionable without a reproduction; noted here so a recurrence has a trail.

## 2026-06-11 — Dead-code sweep: test-only public APIs left in place

A vulture + reference sweep removed confirmed dead code (see commit `chore: remove dead code`).
These candidates were deliberately **not** removed because they are public APIs exercised only by
tests — possibly superseded, but deleting them means rewriting the tests that use them:

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
`os.killpg`, `start_new_session`, bash-tool runs real `bash` off-Windows). Fixed in this pass:
stdin submit sent CRLF on all platforms (`core/tools/process_manager.py` `SUBMIT_BYTES`), and a
Linux installer (`scripts/install.sh` + `uninstall.sh`, systemd user unit) now exists. Still open:

- **No CI / no recurring Linux test runs.** The suite was verified once in WSL Ubuntu during this
  pass; nothing guards against future Windows-only regressions.
- **sqlite-vec on the actual Pi is unverified.** `core/recall/vector_store.py` hard-imports
  `sqlite_vec` and loads it as a native SQLite extension; needs an aarch64 wheel for the Pi's
  Python and a `sqlite3` built with extension loading. Verify on first Pi deploy (64-bit
  Raspberry Pi OS required) before enabling `recall.backend: vector`.

**Why deferred:** CI is an infrastructure decision the user hasn't made; the sqlite-vec check
needs the physical Pi.

## 2026-06-11 — server/app.py pokes ChatLoop privates for compaction wiring

`server/app.py` `_initialize_app_state` builds `CompactionService(SummarizationStrategy())` and
injects it post-hoc via `chat_loop._compaction_service = ...` on the runtime-owned loops (also the
streaming loop). The clean fix is to construct the canonical ChatLoops in `Runtime.start()` with a
compaction service (constructor injection), removing the server-side private poke. Found during the
deep-modules audit (A3); deferred because it changes Runtime bootstrap wiring and the server tests
around `app.state.compaction_service`, which is out of scope for the audit fixes.

## 2026-06-11 — agent.json is validated twice (settings validators + AgentStore's own family)

`core/settings/validation.py` (`validate_agent_data`) and `core/agents/agents.py` (`_validate_string_field`,
`_validate_temperature`, `_validate_thinking_effort`, `_validate_memory_prompt_mode`, …) both encode the
agent.json field rules — two validators for one format, found during the deep-modules audit (A2 symptom).
Consolidating means deciding which side owns the schema (settings as central authority vs. the agent domain)
and rewiring AgentStore create/update paths plus their tests. Deferred: behavior-relevant refactor beyond
the audit's settings-consolidation scope.


## 2026-06-12 — Channel observed-note writes can race with non-channel Runs

`ChannelConversationEngine` serializes `observe_unaddressed` note writes through its
per-conversation FIFO, so they cannot land inside a tool cycle of a Run triggered by that same
channel worker. A Run started for the same group Session through another accessor (for example the
WebUI on a linked Session) is outside that queue, however, and an observed note could still append
between that Run's assistant tool-call message and its tool results.

**Why deferred:** fixing this requires Session-level append coordination across accessors rather
than another channels-only queue rule. It is the same pre-existing exposure as the note written by
`session.link_channel`; address both together when Session append serialization is designed.

## 2026-06-13 — Anthropic full_history replay: live probe + live verification not performed

Phase 2 of the reasoning-replay plan (`docs/plans/reasoning-replay-policy.md`) switched
`AnthropicAdapter` to the `full_history` replay policy and added the thinking-disabled guard.
The plan's live steps could not run: no Anthropic credentials are configured in this environment
(`~/.vbot/.env` has an empty `ANTHROPIC_API_KEY=`, no process env var, no Anthropic OAuth token).

Outstanding once credentials exist:
1. **Probe** — does the Messages API tolerate replayed `thinking`/`redacted_thinking` blocks when
   the outgoing request (a) explicitly disables thinking, (b) omits the thinking parameter? The
   shipped guard is the conservative default per plan: strip on explicit disable or
   reasoning-unsupported model, keep when the parameter is merely absent. If the API tolerates
   blocks under explicit disable, the guard can be lightened.
2. **Live verification** — one real multi-run Anthropic session (run with tool calls → run
   completes → new run in same session) confirming no 4xx and thinking blocks accepted; spot-check
   a mid-session model switch. Record outcomes in `.vorch/domain-maps/providers/anthropic.md`.

**Why deferred:** blocked on credentials, not on code; unit/integration coverage pins the
implemented behavior.

## 2026-06-13 — MiniMax full_history replay: live probe not performed

The per-provider replay rollout moved `MiniMaxAdapter` to `full_history` (its own docs require
cross-turn reasoning preservation — the strongest case of any reviewed provider) and defaulted
`reasoning_split: true` for reasoning-active models so the trace is captured as `reasoning_details`
and becomes replayable through the generic request builder. No MiniMax credentials are configured
in this environment (`~/.vbot/.env` has `MINIMAX_API_KEY=` empty, no process env var), so the
documented mechanism was implemented from MiniMax's published docs and pinned by unit tests, not
verified live.

Outstanding once credentials exist:
1. **Probe** — confirm `reasoning_split: true` actually returns `reasoning_details` (non-streaming
   *and* streaming) for M2.x and M3, and that replaying a prior run's `reasoning_details` on a
   same-model follow-up request returns 200 (no ordering/validation error).
2. **Edge** — verify a `thinking: {type: disabled}` (effort `none`) request with `reasoning_details`
   absent behaves, and that replayed details on a disabled-thinking request are tolerated or need a
   guard like Anthropic's.

**Why deferred:** blocked on credentials, not on code; documented behavior + unit tests pin the
implementation, and the same-model gate prevents cross-model leakage.

## 2026-06-15 — Model DB Phase 3: unseeded canonical reasoning ladders (hand path)

The canonical reasoning ladder lift (refresh) pulls each canonical model's effort ladder from its
**lab** provider's own models.dev section. Of 215 canonical models, 184 lift 1:1; the rest are the
hand path the handoff describes. After projecting the live catalog (2026-06-15), **18
reasoning-capable** canonical models could not lift a ladder deterministically — the lab keys the
model differently or there is no lab provider — so their canonical reasoning is the bare
`{supported: true}` (no fabricated ladder). They are listed here for optional hand-seeding into
`resources/models/models.overrides.json` (keyed by canonical id, `capabilities.reasoning` block):

- **No lab provider:** `tencent/hy3-preview`
- **Lab keys it differently (17):** `deepseek/deepseek-r1`,
  `nvidia/llama-3.1-nemotron-ultra-253b`, `nvidia/llama-3.3-nemotron-super-49b-v1`,
  `nvidia/llama-3.3-nemotron-super-49b-v1.5`, `nvidia/nemotron-3-nano-30b-a3b`,
  `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning`, `nvidia/nemotron-3-super-120b-a12b`,
  `nvidia/nemotron-3-ultra-550b-a55b`, `nvidia/nemotron-3.5-content-safety`,
  `nvidia/nemotron-cascade-2-30b-a3b`, `nvidia/nemotron-content-safety-reasoning-4b`,
  `nvidia/nemotron-nano-12b-v2-vl`, `nvidia/nemotron-nano-9b-v2`, `openai/gpt-5.5-instant`,
  `stepfun/step-3.7-flash`, `zhipuai/glm-5-turbo`, `zhipuai/glm-5.2`

A further ~37 reasoning-capable models carry the bare `reasoning: true` boolean at their lab with no
published `reasoning_options` (e.g. most Alibaba Qwen). Those are **expected**, not a defect — the
adapter effort floor handles snapping at runtime (Phase 4); no seeding needed.

**Why deferred:** seeding requires externally verifying each model's real effort ladder; fabricating
values is forbidden ("Keine Phantasiewerte"). None blocks a configured provider today — the only
reachable ones (`deepseek/deepseek-r1`, `openai/gpt-5.5-instant` via OpenRouter) still run on their
provider-layer ladder when models.dev publishes one for that provider.

## 2026-06-15 — Model DB Phase 3: GitHub Copilot catalog not regenerated (OAuth expired)

During the Phase 3 live regeneration the GitHub Copilot model catalog could not be refreshed: the
stored OAuth token failed to refresh ("OAuth token refresh failed — please reconnect"). Per the
"capture what succeeds, leave the file as-is, flag it" rule, `resources/models/github-copilot.json`
was left at its prior (Phase 1) state and not regenerated with the new canonical-pointer / deviating-
ladder enrichment. MiniMax was also not regenerated — no `MINIMAX_API_KEY` is provisioned in this
environment. Both are pure-credential blockers, not code: re-running `model refresh github-copilot`
(after reconnecting Copilot) and `model refresh minimax` (once a key exists) will pick up the new
projection. Anthropic was deliberately skipped (untested stub, no normalizer).

Note also confirmed during Phase 3: every configured provider's vBot id already equals its models.dev
id (verified against the fetched `catalog.providers` keys, incl. `opencode-go` which is literally
`opencode-go` there), so **no `models_dev_id` overrides were needed** on any provider config.

## 2026-06-15 — Model DB Phase 3 (orchestrator review): opencode-go override ↔ feed reconciliation

Two things surfaced reviewing the Phase 3 regeneration:

1. **Fixed in Phase 3 (code):** the per-provider reasoning projection sourced `supported` from the
   adapter's bare-endpoint normalization while taking control/levels from models.dev — for thin
   providers this produced invalid `reasoning: {supported: false, control: levels, levels: [...]}`
   blocks (3 opencode-go models). `provider_reasoning_block` now sources `supported` from the same
   models.dev provider section as the control, so the block is always internally consistent.

2. **Deferred to Phase 5 (opencode-go override owner):** opencode-go's `/models` endpoint returns
   bare ids with **no context window** (generated `context_window: 0`), so
   `resources/models/opencode-go.overrides.json` is still required — it carries the real
   `context_window`/`max_output_tokens` for all 16 models. But its stale `reasoning: {supported: true}`
   blocks now clobber the regenerated ladders at load. Reconciliation is per-model, not a blanket
   strip: for `deepseek-v4-flash`/`deepseek-v4-pro` models.dev publishes the `[high, max]` ladder and
   the override should inherit it (drop the override's bare `reasoning`); for the other 14 the feed
   reports `reasoning: false` while the hand curation asserts `supported: true` — a deliberate override
   fact that must be KEPT. Phase 5 (which already rewrites this file for the `protocol` field) should
   reconcile so effective opencode-go models keep the real context window AND the correct reasoning
   ladder. Net effect today: opencode-go reasoning still works (override `supported: true`) but snaps
   against the adapter floor instead of `[high, max]` — no regression, just not yet enriched.

## 2026-06-15 — Model DB Phase 5: wire selectors are now data (status + leftovers)

Phase 5 moved the per-model wire FACTS into the provider-scoped `metadata` blob; the wire MECHANICS
stay in the adapters. Done and verified by unit tests + a clean validator run:

- **opencode-go `protocol`** routes on `metadata.opencode_go.protocol` (`resources/models/opencode-go.overrides.json`);
  the stale `_ANTHROPIC_MESSAGES_MODELS` frozenset is removed; unknown models default to OpenAI + a `warn`.
- **Mistral `prompt_mode`** drives off `metadata.mistral.prompt_mode` (`resources/models/mistral.overrides.json`);
  the `MISTRAL_PROMPT_MODE_REASONING_MODEL_PREFIXES` prefix tuple is removed.
- **Reasoning response field** is data-driven and graceful in `normalize_response` (reads
  `metadata.<provider>.reasoning_response_field`, else the hardcoded scan); refresh projects models.dev
  `interleaved` into it (`models_dev.reasoning_response_field` + `discovery._enrich_provider_model`).
- **Copilot family** now comes from `Model.family` (wins over `metadata.github_copilot.family`).

The **2026-06-15 Phase-3 (orchestrator review) opencode-go override ↔ feed reconciliation** item above
is RESOLVED here: effective `opencode-go/deepseek-v4-pro` reasoning == `{supported: true, control:
"levels", levels: ["high","max"]}` (override drops its `capabilities` so the generated ladder is
inherited); the other 14 override models keep `capabilities.reasoning: {supported: true}`; real
context windows are preserved. Validator clean.

Leftovers (deliberately NOT done):

1. **Generated-only opencode-go models keep `context_window: 0`** — `minimax-m3` and `qwen3.7-max` got a
   thin override entry carrying ONLY `metadata.opencode_go.protocol: "anthropic"` (so they route
   correctly), but their required fields still come from the generated provider layer where
   `context_window: 0`. The 3 truly override-less generated models (`kimi-k2.7-code`, …) likewise keep
   `context_window: 0` and route the OpenAI default + a `warn`. This is the **Phase-6** `context_window`
   workstream (provider-config-level default + global floor); not fixed here per the phase scope.
2. **Catalog regeneration to populate `reasoning_response_field` was NOT run** — the projection code path
   is in place and unit-tested, but the credentialed catalogs (`openrouter`, `opencode-go`) were not
   re-refreshed in this phase, so the on-disk generated files do not yet carry the field. The graceful
   fallback means runtime behavior is unchanged until a refresh runs (`model refresh openrouter` /
   `opencode-go` will pick it up). mistral/openai have no `interleaved` in the feed; github-copilot/
   minimax/anthropic are credential-blocked (see prior Phase-3 flags).
3. **Pre-existing (not Phase 5): `tests/server/test_rpc_integration.py::
   test_model_list_and_settings_get_follow_credential_contract`** fails on a stale `reasoning` snapshot
   (the expected dict omits the typed `control`/`levels` fields the model.list serializer now emits).
   Reproduces with all Phase-5 changes stashed → it is an earlier-phase test-snapshot debt, flagged as a
   spawned task. The model.list serializer is correct; only the test fixture is stale.

---

## 2026-06-15 — Phase 6 (context_window optional) — resolved + cross-provider leftovers

Phase 6 landed: `Model.context_window` is now `int | None`; a missing window stays missing
(`null`/absent) and is filled read-side by the shared chain `resolve_context_window(model_window,
provider_config)` (model value -> provider-config `context_window` default -> named global floor
`GLOBAL_CONTEXT_WINDOW_FLOOR` ~8k, in `core/providers/providers.py`). Every read site routes through it
(compaction/token-budget in `core/chat/chat.py`, `/status` in `core/chat/commands.py` +
`core/tools/status.py`, the agent payload in `server/rpc/payloads.py`; `model.list` keeps the honest raw
`null`; the WebUI token badge already tolerates `null`).

**RESOLVES the prior leftover #1 above** ("Generated-only opencode-go models keep `context_window: 0`"):
opencode-go was re-refreshed with the fixed normalization, so its generated layer now carries
`context_window: null` for every model (the 16 override-backed models still get their real windows from
the override layer at load; `kimi-k2.7-code`/`minimax-m3`/`qwen3.7-max` are honestly `null` and resolve to
the global floor). No fake `0` remains in opencode-go.

New leftovers (deliberately NOT done — out of Phase 6 scope, safe meanwhile):

1. **Stale `context_window: 0` in `github-copilot.json` (3 embedding models) and `openrouter.json` (23
   non-chat STT/image/video models)** — these generated catalogs predate the Phase-6 normalization fix
   (GitHub Copilot now emits `null` for an absent window; OpenRouter now normalizes its `context_length:
   0` for non-chat models to `null`). The fix is in the adapter code and unit-tested; the on-disk files
   self-correct on the next `model refresh github-copilot` / `model refresh openrouter`. github-copilot
   is OAuth/credential-gated; openrouter was not re-refreshed this phase. Runtime is unaffected: a stray
   `0` is treated as "unknown" by `resolve_context_window` and resolves to the floor, and these are
   non-chat models anyway.

2. **`core/recall/vector_store.py` `_DEFAULT_CONTEXT_WINDOW = 8192` is a SEPARATE concept** (the
   embedding-model input-truncation budget, with its own documented justification), NOT the chat
   `Model.context_window`. Left untouched on purpose; it already treats an unknown window as "assume this
   floor" and is not a fake fact masquerading as a discovered catalog value. Noted so a future reader does
   not conflate the two `_DEFAULT_CONTEXT_WINDOW` names.

## 2026-06-15 — Model DB Phase 6 (orchestrator note): metadata wholesale-merge interaction

At load the `metadata` blob is a top-level field replaced WHOLESALE by the highest layer (assembly
contract — only `capabilities` merges one level deep). So a model that has metadata in BOTH the
generated `<provider>.json` AND the hand `<provider>.overrides.json` keeps only the override's. Real
case: opencode-go's override carries `metadata.opencode_go.protocol`, which shadows the generated
`metadata.opencode_go.reasoning_response_field` (projected from models.dev `interleaved`). No
functional harm today — opencode-go speaks the OpenAI protocol, so `normalize_response`'s graceful
fallback finds `reasoning_content` anyway (Phase 5). If a future need requires both to coexist, either
deep-merge the provider-scoped `metadata` sub-object at load (a Phase-2 assembly change) or carry both
keys in the override. Recorded so the final review treats this as a known design choice, not a bug.

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

5. **`apply_overrides` in `core/models/discovery.py` is dead and should be removed.**
   Refresh no longer bakes `<provider>.overrides.json` into `<provider>.json` — that
   cross-file merge moved to LOAD (`assembly.py`). `apply_overrides` (and its private
   `_validate_override_model_data`/`_overrides_path` helpers, if unused elsewhere) is
   retained only for legacy callers/tests. Deferred because removing it is a
   test-refactor, not a behavior change; do it when those tests are touched anyway.
