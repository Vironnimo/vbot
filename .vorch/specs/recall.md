# Recall

Session recall read model for tools that search or browse persisted chat Sessions.

## Overview

`core/recall/` is separate from both canonical Session persistence and curated memory. Sessions remain JSONL files owned by `core/sessions/`; curated durable facts remain in `core/memory/`. Recall backends provide a read/search model over stored Sessions for `session_search`.

## Interfaces

- `RecallRequest` carries normalized tool arguments: `agent_id`, optional `session_id`, optional `around_message_id`, optional `query`, time bounds, roles, match mode, limit, context size, bookend size, and sort.
- `RecallBackend` is a `Protocol` with `browse(request)`, `search(request)`, and `scroll(request)`, each returning the existing JSON-compatible `session_search` result payload.
- `RecallBackendContext` supplies `data_dir`, `ChatSessionManager`, and an optional logger.
- `RecallBackendRegistry` registers factories by lowercase snake_case name and creates backends from a context. `register()` raises `ValueError` on a duplicate name and on a name that is not lowercase snake_case.
- `register_session_search_tool()` and `session_search_handler()` also accept a bare `ChatSessionManager` and auto-wrap it in `JsonlSessionRecallBackend`, so callers and tests without a configured backend still work.

Built-in backend names:

- `jsonl_scan` - default backend. Scans canonical JSONL through `ChatSessionManager`.
- `sqlite_fts` - optional derived SQLite FTS5 index under `<data_dir>/recall/session_index.sqlite`.
- `vector` - optional derived sqlite-vec semantic index under `<data_dir>/recall/session_vectors.sqlite`.

Backend selection:

- Raw config uses `settings.json` `recall.backend`.
- `settings.get` exposes `{ backend, available_backends }` for the Settings Recall panel; `available_backends` is `sorted(FIRST_PARTY_RECALL_BACKENDS)`, assembled in `server/rpc/settings_methods.py` (source of truth for the panel list).
- `settings.update({ recall: { backend } })` accepts first-party backend names and calls `Runtime.reload_recall_backend()` so `session_search` uses the new backend without an app restart.
- If the persisted `recall.backend` name is unknown to the registry, `Runtime._create_recall_backend` logs a warning and falls back to `DEFAULT_RECALL_BACKEND` (`jsonl_scan`) instead of crashing.

## JSONL Backend

`JsonlSessionRecallBackend` owns the current browse/search/anchored-view behavior and result rendering formerly implemented directly in `core/tools/session_search.py`.

Rules:

- It uses `ChatSessionManager.list_with_metadata()` and `ChatSessionManager.get(...).load()`; it must not construct Session file paths.
- Skill-context notes are excluded from search and context windows even when `note` is requested.
- `session_search` tool-result messages (the recall tool's own persisted output, identified by `is_recall_artifact_message`: `role="tool"` + `name == RECALL_TOOL_RESULT_NAME`) are excluded from matches, context windows, and bookends — indexing or returning them makes a search match its own prior results. `RECALL_TOOL_RESULT_NAME` duplicates `core.tools.session_search.SESSION_SEARCH_TOOL_NAME` (recall is below tools, no import; a `test_session_search` test asserts they match). The vector backend also drops their text from chunk embeddings.
- Search text includes textual content, text content blocks, file/media filename and media type, assistant reasoning, tool names, error kind, and assistant tool call names plus JSON arguments.

## SQLite FTS Backend

`SqliteFtsRecallBackend` is a disposable index, not Session storage.

- The DB lives at `<data_dir>/recall/session_index.sqlite`.
- Schema initialization uses stdlib `sqlite3`, attempts WAL and normal synchronous mode, and sets a short busy timeout. The schema is versioned via `PRAGMA user_version` (`_SCHEMA_VERSION`); a mismatched on-disk index is dropped and rebuilt, so the disposable index needs no migrations.
- The FTS5 table uses the `trigram` tokenizer, so `MATCH` does case-insensitive **substring** lookup that mirrors the JSONL scanner's `term in haystack` (e.g. `gpt` matches `gpt4o`). Indexed `search_text` is whitespace-compacted at index time so it aligns with the compacted haystack used during re-validation.
- Query terms are split the same way as the JSONL backend (whitespace), not on word boundaries, so both backends agree on what a term is.
- Index freshness is checked lazily per candidate Session by JSONL file mtime and size.
- Missing or stale Sessions are reindexed by loading canonical messages through `ChatSessionManager`, deleting prior rows, inserting searchable text, and updating `indexed_sessions` in one transaction.
- Browse and anchored scroll use the JSONL behavior; SQLite is used only for query candidate lookup.
- Trigram needs ≥3 characters: an empty/punctuation-only query, or any query whose terms (or phrase) are shorter than 3 characters, produces no FTS expression and falls back to JSONL scan for that call (where short substrings still match correctly).
- SQLite is only a candidate filter: every FTS hit is re-validated during JSONL hydration through `message_matches_request` + `text_matches_query`, so role/time/skill-note filtering and final matching never trust the index alone. Result windows/bookends are always hydrated from canonical JSONL after FTS candidate lookup.
- If the index file is missing, it is rebuilt lazily. If SQLite operations fail, the backend deletes the index and retries once, then falls back to JSONL scan for that call.

## Vector Backend

`VectorRecallBackend` is a disposable semantic index, not Session storage. It inherits from `JsonlSessionRecallBackend` so `browse` and `scroll` reuse the canonical JSONL implementation; only `search` is overridden.

- The DB lives at `<data_dir>/recall/session_vectors.sqlite`.
- Schema initialization uses `sqlite_vec.load()` with the `enable_load_extension` dance, WAL, normal synchronous mode, and a short busy timeout. The schema is versioned via `PRAGMA user_version`; a mismatched on-disk index is dropped and rebuilt, so the disposable index needs no migrations.
- The store header pins `(embedding_provider_id, embedding_model_id, dimension, schema_version)`. On mismatch (different model, provider, dimension, or schema version) → drop and rebuild the entire store before any query.
- The `vec0` virtual table uses `distance_metric=cosine` at a fixed dimension observed from the first embed response (never from the catalog). The dimension is stored in the header.
- KNN query: `select rowid, distance from vectors where embedding match vec_f32(?) order by distance limit K`.
- **Per-chunk granularity:** multiple vectors per session, built by walking a session's messages in order and packing consecutive messages' search-text into chunks (target: `_CHUNK_TARGET_CHARS = 1500` characters, ~500 tokens). A `_CHUNK_OVERLAP_MESSAGES = 1` boundary overlap provides context across chunk boundaries. Each message's contributed text is capped at `_PER_MESSAGE_CHAR_CAP = 2000` characters before packing; a single message larger than the target becomes its own chunk, hard-capped via `VectorStore.truncate_to_input_limit`. The schema is chunk-keyed (`chunks` metadata table, `UNIQUE(agent_id, session_id, chunk_index)`). `PRAGMA user_version` tracks on-disk index validity and is bumped on a build/index-policy change as well as a DDL change (currently v3; v3 stopped indexing empty-text chunks, so older indexes are dropped and rebuilt to purge their constant-vector noise rows). The store drops and rebuilds on a version bump automatically.
  - A chunk whose packed text is empty after whitespace-collapse is **not indexed** (e.g. a window of only `run_summary` records, which carry no searchable content) — an empty string embeds to a constant vector that would surface in every query as identical-distance, empty-snippet noise.
  - Chunks are embedded in batches of `_EMBED_BATCH_SIZE` (64), with the existing half-on-overflow shrink-retry per batch.
  - KNN returns chunk rows; results are **deduped to the single nearest chunk per session**, then filtered by a `_MAX_DISTANCE` (0.7) cosine-distance relevance cutoff.
  - The chunk's recorded `anchor_message_id` is the first `is_context_message` in the chunk (user/assistant/tool/error/compaction_checkpoint, excluding skill-context notes), falling back to the chunk's first message — never a kernel-internal `note`/`run_summary` when a real message is present.
- **Eager-on-search backfill:** the first `search` after enabling `vector` embeds all missing or stale sessions (batched, logged), then queries. Missing/stale detection diffs `ChatSessionManager.list_with_metadata()` against the stored `mtime`/`size` in the `chunks` metadata table.
- **Semantic ranking only:** chunk rows are ranked by cosine distance, deduped to the nearest chunk per session, and filtered by a `_MAX_DISTANCE` (0.7) relevance cutoff with no `text_matches_query` re-validation (semantic match has no literal term). Result windows/bookends are hydrated from canonical JSONL after KNN candidate lookup.
- **Hydration applies the request's structural filters** (`message_matches_request`: role membership in `request.roles`, time bounds, skill-note exclusion) to the anchor — the same per-message gate the JSONL backend uses. If the recorded anchor is filtered out (a role the caller did not ask for — `run_summary` is never a recall role; a message outside the time window), the backend **re-anchors to the first message inside the chunk's `[start_message_id, end_message_id]` span that does match**; if no message in the span is eligible the chunk is dropped. The index itself is role-agnostic (all roles chunked), so narrowing `request.roles` is honored at read time without reindexing.
- **Graceful fallback:** empty query, no configured embedding binding, or any sqlite-vec/embed failure → log a warning and fall back to JSONL scan for that call (mirrors `sqlite_fts`'s delete-and-retry-then-fallback).
- The backend requires `RecallBackendContext.embeddings` (the `EmbeddingService`) to be supplied; the context `model_registry` is wired but no longer used for truncation (per-chunk text sizes stay well under embedding model token caps).

## Cross-Domain Rules

- `core/tools/session_search.py` owns provider-visible schema, argument parsing, invalid-argument envelopes, and dispatch to `RecallBackend`.
- `core/sessions/` remains the source of truth for Session messages and metadata.
- `core/memory/` remains the curated-memory boundary. Do not put pinned memory CRUD or prompt-visible fact storage in recall.
