# Plan: Per-session chunking for the `vector` recall backend

**Goal:** Replace the one-vector-per-session design with multiple vectors per session (chunked by message windows), so semantic recall ranks on the *region* that actually matches and shows that region as the snippet — fixing the coarse, undifferentiated results the session-level vectors produce.

**Context:** The `vector` recall backend (`core/recall/vector.py` + `core/recall/vector_store.py`) currently embeds each session's whole concatenated text into **one** averaged vector. A live test exposed why this is weak: query `Bild` returned distances all bunched at 0.52–0.63 with the top hit unrelated ("mach mal das licht an"), and snippets always showed the *session opener* (first user message), never the matching content. Root cause is granularity: a one-word query compared against a whole-session average vector has no signal. Owner decided to fix the granularity (this plan). Hybrid keyword+semantic ranking and a settings-driven chunk size are explicitly deferred (see `.vorch/FLAGGED.md`).

The embedding pipeline itself is healthy: input truncation + half-on-overflow retry keep every request under the model's token cap (bge-m3, 8192), and the store already drops+rebuilds on a schema-version or binding change. So this is a **derived-index rebuild**, not a migration — bumping the schema version triggers an automatic rebuild on the next search.

**Scope:**
- **In:** chunked indexing (multiple vec0 rows per session); store schema v2 with chunk-level metadata; chunk building from message windows with overlap and per-message caps; batched embedding of chunks; KNN dedup to best chunk per session; a relevance (max-distance) cutoff; chunk-anchored hydration so the snippet/window reflect the matched region; tests written with each change; spec/FLAGGED/PROJECT doc updates.
- **Out:** hybrid keyword+semantic ranking (FLAGGED #2); chunk-level multi-hit results (>1 result per session) — kept session-oriented for contract stability; making chunk size / threshold user-settings (kept as documented module constants this iteration); asymmetric query/document `input_type` (FLAGGED #6); background/write-time indexing (FLAGGED #3); changes to the `jsonl_scan` / `sqlite_fts` backends.

**Assumptions & Constraints:**
- The on-disk index is **disposable**. Bumping `_SCHEMA_VERSION` is the migration: the next `search` re-chunks and re-embeds every session for the agent. One-time cost only; document it. No backwards-compat with the v1 layout.
- Result contract stays **session-oriented**: one match per session (its best/nearest chunk), with the same payload shape consumers already get from `_hydrate_*` (`session_id`, `message_id`/anchor, `window`, `bookend_*`, `distance`, `snippet`). Only `snippet`/anchor *content* improves; no consumer-visible key is removed.
- `EmbeddingService.embed` is unchanged. The recall backend owns chunking and batch sizing (per `.vorch/specs/embeddings.md`: "the caller is responsible for staying within provider rate/batch limits").
- Quality gates run green per changed file: `python scripts/quality.py <paths>` (format → lint → type-check → test). Tests are written in the same task as the code they cover.

---

## Architecture Decisions (settled — do not re-decide)

These were delegated to the planner; the builder implements them as written.

1. **Chunk boundaries — message-aware packing with overlap.** Walk a session's messages in order; pack consecutive messages' search-text into a chunk until it reaches `_CHUNK_TARGET_CHARS`, then start a new chunk. Carry the last `_CHUNK_OVERLAP_MESSAGES` message(s) into the next chunk for boundary context. A single message larger than the target becomes its own chunk, hard-capped via `VectorStore.truncate_to_input_limit(...)`. Rationale: respects message boundaries so chunk anchors map cleanly onto real message ids for hydration (window/bookends machinery is unchanged), gives uniform-ish chunk sizes, and bounds a single huge tool dump.
2. **Per-message cap.** Before packing, cap each message's contributed search-text to `_PER_MESSAGE_CHAR_CAP` so one giant tool-result JSON cannot swallow a chunk or push a chunk semantically off-topic. Tunable; generous default.
3. **Chunk size targets** (module constants, tunable, documented in the spec): `_CHUNK_TARGET_CHARS = 1500` (~500 tokens — small chunks retrieve better), `_CHUNK_OVERLAP_MESSAGES = 1`, `_PER_MESSAGE_CHAR_CAP = 2000`. Each chunk is far under the model cap, so the existing shrink-retry should rarely fire (keep it as the safety net).
4. **Result granularity — best chunk per session.** KNN returns chunk rows; dedup to the single nearest chunk per session before hydration, so the result list stays one-per-session (stable contract) but is now anchored at the matching chunk with the matching snippet. Multi-hit per session is out of scope (future toggle).
5. **Relevance cutoff.** Drop chunks whose cosine `distance > _MAX_DISTANCE` (default `0.7`; tunable constant). Applied after dedup, before taking `limit`. Conservative default — only removes clearly-weak matches; chunking tightens real-match distances so this becomes meaningful. Open decision flagged below since the right value is model-specific.
6. **Batched embedding.** Embed chunks in groups of `_EMBED_BATCH_SIZE` (default `64`) per request, concatenating results in order, instead of one giant request. Chunking multiplies input count (potentially thousands of chunks), and provider per-request input-count limits vary. Verify OpenRouter's max inputs/request and adjust the default (flagged below).
7. **Schema is chunk-keyed.** Metadata table becomes chunk rows with `UNIQUE(agent_id, session_id, chunk_index)`; `_SCHEMA_VERSION` bumps to `2` → automatic drop+rebuild. Per-session `mtime_ns`/`size_bytes` stay on every chunk row (identical across a session's chunks) for freshness diffing.

---

### Milestones

| # | Milestone | Deliverable |
|---|---|---|
| M1 | Store supports chunks | `vector_store.py` v2 schema + chunk record + multi-chunk upsert/lookup, all store tests green |
| M2 | Backend chunks + ranks | `vector.py` builds chunks, embeds in batches, dedups to best chunk per session with cutoff, hydrates the matched region; all backend tests green |
| M3 | Docs current | recall/embeddings specs, FLAGGED, PROJECT reflect the chunked design |

---

## Phase Breakdown

### Phase 1 — Store layer: chunk-keyed schema + records
**Goal of this phase:** `VectorStore` stores and retrieves multiple vectors per session, keyed by chunk.
**Can run in parallel with:** none (Phase 2 depends on these APIs).

**read:** `.vorch/specs/recall.md`
**files:** `core/recall/vector_store.py`, `tests/core/recall/test_vector_store.py`

Tasks:
- Bump `_SCHEMA_VERSION` to `2`. Rename `_METADATA_TABLE_NAME` value to `"chunks"` (constant name can stay or become `_CHUNK_TABLE_NAME` — builder's call; keep one). The schema bump drops the old tables automatically; no manual migration.
- Rename `SessionVectorRecord` → `ChunkVectorRecord` and extend its fields:
  - keep: `session_id`, `agent_id`, `started_at`, `mtime_ns`, `size_bytes`, `anchor_message_id`, `snippet`
  - add: `chunk_index: int`, `start_message_id: str`, `end_message_id: str`
  - `snippet` now holds the **chunk's** text snippet (the matched region), not the session opener.
- Update the metadata `CREATE TABLE` (both the `_initialize_schema` and the binding-change rebuild branch in `_ensure_header`) to the new columns and `UNIQUE(agent_id, session_id, chunk_index)`. Keep the `agent_id` index; add an index on `(agent_id, session_id)` for the freshness/delete path.
- Replace `upsert_many_sessions` → `upsert_many_chunks(*, header, records: Iterable[tuple[ChunkVectorRecord, Sequence[float]]]) -> int`. **Critical:** delete each `(agent_id, session_id)` exactly **once** before inserting that session's chunks (collect the distinct set first), otherwise inserting chunk 2 would delete chunk 1 of the same session via the per-row delete. Keep the dimension check and the vec0 `INSERT ... vec_f32(?)` per chunk row. (Keep or drop the single-row `upsert_session` — only used by tests; update or remove.)
- `_delete_session_rows` already deletes *all* rows for an `(agent_id, session_id)` — reuse as-is for re-index and staleness; verify it still works against the new table.
- `list_indexed_sessions(agent_id) -> dict[str, tuple[int, int]]`: dedup to one entry per `session_id` (GROUP BY `session_id`, or `DISTINCT`) since every chunk row of a session shares `mtime_ns`/`size_bytes`.
- Rename `get_sessions_by_rowids` → `get_chunks_by_rowids(row_ids) -> dict[int, ChunkVectorRecord]`; hydrate the new columns.
- `knn_search` signature/behavior unchanged (returns `(rowid, distance)` rows ordered by distance). Keep `_KNN_OVERSHOOT`; the backend over-fetches more (Phase 2) for chunk→session dedup.
- Keep `truncate_to_input_limit` (now used to hard-cap an oversized single-message chunk).
- Tests (`test_vector_store.py`): multi-chunk upsert for one session writes N vec0 rows + N metadata rows without clobbering; re-upserting a session replaces all its chunks; `list_indexed_sessions` returns one entry per session; `get_chunks_by_rowids` round-trips the new fields; `knn_search` returns the nearest chunk rows across sessions; binding-change still drops+rebuilds. Update existing truncation tests only if constants moved.

**Dependencies:** none.
**Done when:** `python scripts/quality.py core/recall/vector_store.py tests/core/recall/test_vector_store.py` is green and the store holds multiple distinct vectors per session keyed by `chunk_index`.

---

### Phase 2 — Backend: chunk building, batched embedding, ranked hydration
**Goal of this phase:** Searches embed the query, find the nearest chunk per session within a relevance cutoff, and return results anchored at the matching chunk with the matching snippet.
**Can run in parallel with:** none (depends on Phase 1).

**read:** `.vorch/specs/recall.md`, `.vorch/specs/embeddings.md`
**files:** `core/recall/vector.py`, `tests/core/recall/test_vector.py`

Tasks:
- Add module constants with doc-comments: `_CHUNK_TARGET_CHARS = 1500`, `_CHUNK_OVERLAP_MESSAGES = 1`, `_PER_MESSAGE_CHAR_CAP = 2000`, `_EMBED_BATCH_SIZE = 64`, `_MAX_DISTANCE = 0.7`, and a KNN over-fetch multiple for chunk→session dedup (e.g. `_CHUNK_FETCH_MULTIPLIER = 8`). Keep `_KNN_FETCH_MARGIN`, `_EMBED_OVERFLOW_RETRIES`.
- Replace `build_session_search_text` with `build_session_chunks(messages) -> list[Chunk]`, where `Chunk` is a small local dataclass: `anchor_message_id`, `start_message_id`, `end_message_id`, `text`, `snippet`.
  - Reuse `message_search_text` (from `core/recall/jsonl.py`) per message; skip messages where it is empty and skill-context notes (mirror `representative_window`'s skip of `skill_context_note`/`note` for the anchor; the anchor is the first non-note content message in the chunk, fallback to the chunk's first message).
  - Cap each message's text to `_PER_MESSAGE_CHAR_CAP` before packing. Pack to `_CHUNK_TARGET_CHARS`; carry `_CHUNK_OVERLAP_MESSAGES` trailing message(s) into the next chunk. An oversized single message → its own chunk, capped via `VectorStore.truncate_to_input_limit(text, context_window=...)`.
  - `snippet = build_snippet(chunk_text)` (reuse existing helper).
- `_ensure_fresh_index`: for each stale/missing session, call `build_session_chunks`; build a `ChunkVectorRecord` (without vector) per chunk carrying `chunk_index`, `anchor_message_id`, `start/end_message_id`, the session's `started_at`/`mtime_ns`/`size_bytes`, and the chunk `snippet`. Accumulate `(record, text)` across all stale sessions, embed all texts (batched), then `upsert_many_chunks`. Keep the existing staleness diff (`list_indexed_sessions` vs live mtime/size) and stale-session drop; a changed session re-chunks wholesale (delete-all-chunks then insert).
- `_truncate_to_input_limit(text, header)` stays for the oversized-single-message cap.
- Rename `_embed_sessions` → `_embed_chunks(texts) -> (vectors, header)`. Add batching to `_run_embed`: split `texts` into `_EMBED_BATCH_SIZE` groups, embed each (keeping the half-on-overflow shrink-retry per batch), concatenate vectors **in input order**, assert a consistent dimension/provider/model across batches, and return one combined `EmbeddingResult`. A single-input query stays one batch.
- `_search_with_vector_store`: over-fetch `request.limit * _CHUNK_FETCH_MULTIPLIER + _KNN_FETCH_MARGIN` chunks; map rowids → `ChunkVectorRecord` via `get_chunks_by_rowids`; **dedup to the nearest chunk per `session_id`**; drop chunks with `distance > _MAX_DISTANCE`; then hydrate + structural-filter per surviving session and take `request.limit`. Set `truncated` when more qualifying sessions exist than `limit`.
- Rename `_hydrate_session` → `_hydrate_chunk`: anchor at `record.anchor_message_id` (fallback `_first_indexable_message`), build the window/bookends via the existing `message_match_payload` path, set `match["snippet"] = record.snippet` (the chunk snippet) and `match["distance"] = distance`; optionally include `match["chunk_index"]` for debugging.
- `render_vector_matches` unchanged in shape; it now naturally shows the matched-region snippet.
- Tests (`test_vector.py`): a session split into multiple chunks writes >1 vector (assert via `store`/embed-call count); a query whose match is **buried mid-session** ranks that session and returns the matching chunk's snippet/anchor (not the session opener) — this is the core regression test for the `Bild`-style failure; dedup returns one result per session even when several of its chunks rank highly; `_MAX_DISTANCE` drops weak matches; re-indexing after a session changes replaces its chunks; `_run_embed` batches when `len(texts) > _EMBED_BATCH_SIZE` and preserves vector order; existing shrink-retry tests still pass; existing fallback/rebuild tests still pass (update the `_StubEmbeddings` stub if record/field names changed).

**Dependencies:** Phase 1 APIs (`ChunkVectorRecord`, `upsert_many_chunks`, `get_chunks_by_rowids`, deduped `list_indexed_sessions`).
**Done when:** `python scripts/quality.py core/recall/vector.py tests/core/recall/test_vector.py` is green and a mid-session match returns that session anchored at the matching chunk with the matching snippet.

---

### Phase 3 — Docs & specs
**Goal of this phase:** Project docs describe the chunked design accurately.
**Can run in parallel with:** none (needs final API/constant names from Phases 1–2; small, one builder).

**read:** `.vorch/workflows/spec-workflow.md`
**files:** `.vorch/specs/recall.md`, `.vorch/specs/embeddings.md`, `.vorch/FLAGGED.md`, `.vorch/PROJECT.md`

Tasks:
- `recall.md`: rewrite the "Per-session granularity" bullet → **per-chunk** granularity: chunking strategy (message-aware packing, overlap, per-message cap, target size constants), schema v2 (`chunks` table, `UNIQUE(agent_id, session_id, chunk_index)`), KNN dedup to best chunk per session, the `_MAX_DISTANCE` cutoff, batched embedding, and that the snippet/anchor now reflect the matched region. Keep claims factual and backed by the code.
- `embeddings.md`: note that the recall caller now batches inputs (`_EMBED_BATCH_SIZE`) to respect provider per-request limits (the spec already says the caller owns batch limits — make it concrete).
- `FLAGGED.md`: mark item #4 ("Per-session chunking") of the 2026-06-08 "Semantic recall: deferred follow-up work" section as **done** with the commit ref; leave hybrid (#2) deferred (already annotated). Append-only — don't reorganize.
- `PROJECT.md`: update the recall specs-index line / any one-vector-per-session description if present.

**Dependencies:** Phases 1–2 merged (final names).
**Done when:** specs match the shipped code; `spec-workflow.md` rules respected (factual, source-backed, no field dumps).

---

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Chunk→session dedup leaves fewer than `limit` sessions because top chunks cluster in few sessions | Med | Med | Over-fetch `limit * _CHUNK_FETCH_MULTIPLIER + margin` chunks before dedup; accept rare short results (documented). Raise multiplier if tests show shortfalls. |
| `upsert_many_chunks` deletes earlier chunks of the same session within one batch (per-row delete bug) | Med | High | Delete each distinct `(agent_id, session_id)` once *before* inserting any of its chunks; covered by a dedicated multi-chunk upsert test. |
| `_MAX_DISTANCE` default too aggressive → hides real matches, or too loose → keeps garbage | Med | Med | Conservative default (0.7); document as tunable; open decision below. Verify against the live index after rebuild. |
| `_EMBED_BATCH_SIZE` exceeds OpenRouter's per-request input limit | Low | Med | Conservative default (64); verify the real limit (open decision) and the shrink-retry still guards per-request token totals. |
| Rebuild cost/latency on first search after the schema bump (more chunks than before) | High | Low | Expected and one-time; still cents at \$0.01/M. Document in recall.md and the FLAGGED #3 note (background indexing remains deferred). |
| Hydration anchor missing (chunk anchor id not found in reloaded messages) | Low | Low | Existing `_first_indexable_message` fallback already handles a missing anchor; keep it. |

### Open Decisions (resolve when reviewing / after first rebuild)
- **`_MAX_DISTANCE` value** — default `0.7` (cosine distance). Alternatives: `0.6` (stricter, risks dropping valid synonym matches) or no cutoff (return top-`limit` always). Default chosen to remove only clearly-weak matches; re-tune against the live index since the right value is model-specific (bge-m3).
- **`_EMBED_BATCH_SIZE`** — default `64`. Verify OpenRouter's max inputs per `/embeddings` request and raise if safely higher (fewer round-trips). The per-request token cap is already guarded by truncation + shrink-retry.
- **Chunk size (`_CHUNK_TARGET_CHARS = 1500`)** — smaller (e.g. 800) sharpens retrieval but grows the index; larger blurs it. Default is a middle ground; left as a documented constant (not a setting) this iteration.

### Notes for the executing agent
- Work directly on `main` (see `CLAUDE.md`); commit per logical unit (one for the store, one for the backend, one for docs) with conventional messages; run the relevant quality gate green before each commit.
- The index auto-rebuilds on the schema bump — no migration code. After Phase 2, a manual sanity check (rebuild + re-run the `Bild` query) is worthwhile but not required by tests.
- Do not touch `jsonl_scan` / `sqlite_fts`. Do not add settings/UI. Do not implement hybrid ranking.
