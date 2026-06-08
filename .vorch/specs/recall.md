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

## Cross-Domain Rules

- `core/tools/session_search.py` owns provider-visible schema, argument parsing, invalid-argument envelopes, and dispatch to `RecallBackend`.
- `core/sessions/` remains the source of truth for Session messages and metadata.
- `core/memory/` remains the curated-memory boundary. Do not put pinned memory CRUD or prompt-visible fact storage in recall.
