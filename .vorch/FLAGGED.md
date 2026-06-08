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

### 4. Per-session chunking for very long sessions

Sessions longer than the embedding model's `context_length` are truncated to fit. Per-session chunking (split a session into multiple vectors, merge results) is deferred until session lengths actually exceed typical embedding model context windows (commonly 8k–32k tokens).

### 5. Embedding providers other than OpenRouter

The `core/embeddings/` domain is provider-agnostic by design, but only the OpenRouter discovery path and `/api/v1/embeddings` wire are implemented. Adding e.g. direct OpenAI, Anthropic, or local embedding providers requires supplementary discovery + provider-specific `ProviderEmbeddingClient` routing.

### 6. Asymmetric `input_type` query/document embedding

The OpenRouter embeddings API supports `input_type` hints (e.g. `"query"` vs. `"document"`) for models that optimize embeddings differently per task. Currently ignored — both queries and sessions are embedded symmetrically. Fine for general-purpose models, but some specialized embedding models (e.g. Cohere) produce better results with the hint.

---

## 2026-06-08 — Embedding input truncation is a character heuristic, not tokenizer-accurate

Found while debugging a real bge-m3 failure: a German+English session embedded to **8193 tokens** against the model's 8192 cap (OpenRouter returned the upstream `BadRequestError` as a 200-wrapped `error` object; the recall backend then fell back to JSONL scan). Root cause: `VectorStore.truncate_to_input_limit` (`core/recall/vector_store.py`) caps by **characters** using an assumed chars-per-token ratio, and the old default budget (32_000 chars ≈ 4.0 chars/token, no margin) overflowed because mixed German text tokenizes denser (~3.9 chars/token observed; compounds/umlauts/code can go lower). Also, the first-run backfill passed `context_window=None` because `_truncate_to_input_limit` gated on `_resolved_header` (only set *after* the first embed) instead of the already-available binding header.

**Fixed the same day** (commit `fix(recall): keep embedding input under the token cap for dense/German text`): assume a conservative `_CHARS_PER_TOKEN = 3` with `_INPUT_TOKEN_SAFETY = 0.9` headroom, default to the 8192-token floor when the window is unknown, and resolve the context window from the binding header on the first backfill.

**Update (same day): the heuristic alone was not enough.** After the conservative heuristic shipped, the same session still overflowed (the provider reports "at least 8193" — it stops counting at cap+1, so the number never reflects the real size and gave a false "no change" signal). Fix **(b)** was then implemented (commit `fix(recall): retry embedding at half length on context-length overflow`): `_run_embed` in `core/recall/vector.py` catches the provider's context-length rejection (`_is_context_overflow`) and halves the over-long inputs, retrying up to `_EMBED_OVERFLOW_RETRIES` (6) times until they fit. This is tokenizer- and language-independent and self-correcting; the character heuristic now just minimizes how often the shrink loop is needed. **Remaining (still deferred):** option **(a)** a tokenizer-aware budget (e.g. `tiktoken`) to avoid the occasional wasted first request entirely, and the existing per-session chunking item (#4 above) so very long sessions are not represented by their head alone. Neither is worth building yet.
