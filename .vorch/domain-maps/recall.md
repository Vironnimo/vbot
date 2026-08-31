# Recall

Backend-selected read model for discovering content in persisted chat Sessions. Canonical Session storage stays owned by `core/sessions/`; curated durable facts stay owned by `core/memory/`.

## Overview

Two Agent-facing Tools in one deep module: `session_search` owns backend-independent Session listing plus backend-native query discovery; `session_read` retrieves focused canonical conversation blocks or exact Tool Results only through `ChatSessionManager`. This split keeps discovery compact and prevents an index or ranking backend from becoming a second source of truth for Messages.

The selected backend fixes at Tool registration. `Runtime.reload_recall_backend()` rebuilds the registry from `settings.recall.backend` and re-registers both Tools with the resolved name; an unknown name or selected-backend construction failure falls back to `canonical_scan` (recording the resolved name so description and behavior align). Failing to construct `canonical_scan` itself is fatal - no canonical fallback remains.

## Terms

Session and Tool terms live in `.vorch/GLOSSARY.md`.

### Semantic Recall
**Definition:** Meaning-based search using vector embeddings instead of keywords - a session about "vehicles" matches "cars". Enabled via `recall.backend: vector` plus a configured `text_embedding` model.
**Not:** Keyword search (that's `canonical_scan`/`sqlite_fts`), curated memory, or session browsing.

### Passage
**Definition:** A source-derived overlapping span of eligible canonical Message text - the retrieval and fusion unit of semantic and Hybrid search - carrying stable Message boundaries and offsets so results point back exactly.
**Not:** A Message, a Session summary, or an independently persisted source of truth.

## Search Contract

- `RecallSearchCapabilities` declares the active backend's result unit, Agent-facing texts, literal-match support, ordering, and role-filter meaningfulness; one capability value drives both Tool texts and declared defaults while its three public fields stay stable across backends.
- `RecallSearchRequest` normalizes scope (agent/project, excluded Sessions), query, time range, roles, literal mode, order, and pagination into the first-party contract. The Agent-facing Tool always requests ten results and excludes the current conversation; filtered/paginated variants stay lower-layer capabilities, never public Tool controls.
- Hits are backend-ranked Messages or Passages with canonical read references; raw scores never leave Recall. Pages carry deterministic ranking slices, continuation state, candidate counts, and optional explicit degradation. Every request scopes by `ToolContext.project_id`, never a model-supplied argument - derived indexes key scope separately so equal Session UUIDs cannot collide.
- Extension backends implement the older browse/search protocol; `session_search` wraps their payloads without pretending first-party shapes, moving synchronous searches off the loop.

## Passage Policy

Eligible input comes only from default conversation roles (`user`, `assistant`, `error`, `compaction_checkpoint`) - Tool results, notes, skill contexts, and persisted Recall results are excluded. Text is used verbatim (no whitespace rewriting); Passages are 1,500-char windows with 200-char overlap, so long Messages become multiple Passages instead of truncating. Each records exact text, policy-derived stable id, boundary Message ids/timestamps/roles, and in-message offsets; fusion keys id together with `session_id` because forks share Message ids. Search excerpts are presentation-only (capped 800 chars within the result budget) returning a canonical `read_ref`.

## Backends

### `canonical_scan`
Scans canonical Sessions through `ChatSessionManager` on demand with case-insensitive substring matching (`telegram` matches inside longer words; the Tool uses `all_terms`). Globally ordered by canonical Message time, newest first, with deterministic tie-breakers. Continuations bind to a generation-aware selection digest so changed arguments, recreated Sessions, or changed history fail rather than mix content.

### `sqlite_fts`
Lexical search over Sessions-owned external-content FTS inside `<data_dir>/sessions.db`: `messages_fts` indexes normalized canonical Message relations with the standard tokenizer, and `messages_fts_trigram` provides substring matching while excluding Tool-role rows to avoid indexing bulk machine output twice. There is no mirrored searchable-text table. Search tries the standard index first, uses trigram only when standard token matching returns no result and the request does not include Tool rows, and falls back to bounded canonical substring scan for short/unsupported queries or an unavailable index. A separate disposable Passage FTS at `<data_dir>/recall/session_index.sqlite` remains only for Hybrid's literal Passage arm until integrated passage FTS lands. When integrated FTS is stale or unavailable it degrades directly to `canonical_scan`, and FTS corruption detaches the derived indexes without blocking canonical writes. Scope, Session, exclusion, role, and time filters execute before the bounded candidate limit, then results revalidate against canonical history; persisted Recall results and Skill-context notes are excluded at canonical write time. The Passage index uses the Sessions-owned journal-mode guard and rebuilds on schema mismatch; its cleanup reconciles against the complete canonical scope. Index failure falls back to scan; the index is never authoritative.

### `vector`
Two disposable sqlite-vec cosine indexes: typed Passage search and legacy compatibility search use separate files whose policy-tagged headers prevent chunk reuse. The singleton header pins provider/model/actual-model/embedding-space fingerprint/policy/dimension/schema - any change discards and fully rebuilds in the same search; vectors from different actual models never mix. The store uses the Sessions-owned SQLite journal-mode guard. Typed search returns pure semantic top-K Passages by cosine distance without literal revalidation, per-session collapsing, universal cutoffs, or keyword fallback; missing embedding config or store failure yields stable `semantic_unavailable` (the legacy path keeps its old degraded payload for old callers). Scope/session/exclusion/time filters execute inside the KNN query before top-K so ineligible Passages cannot starve eligible ones. Freshness reconciles against the complete canonical scope independently of request filters. Document vs query embedding purposes are distinct (OpenRouter maps them explicitly); context overflow splits multi-input batches recursively, never rewriting stored text. One normalized Usage summary emits per search; schema creation and upserts run in one write transaction, KNN validates headers without mutating schema, and store failures discard the exact file including WAL/SHM then rebuild.

### `hybrid`
Runs Passage FTS and Vector concurrently, fusing ranks via Reciprocal Rank Fusion (`k=60`) - never raw score mixing. Candidate depth grows until the requested prefix cannot be displaced under the unseen-score bound, preserving true rank fusion without over-fetch guesses. One unavailable arm leaves the other usable with explicit degradation; both failing returns `hybrid_unavailable`. Cursors snapshot both arms - source or backend changes invalidate continuation rather than mixing rankings.

## Lifecycle & Removal

Canonical Messages always come from `ChatSessionManager`; Recall modules construct no Session paths and index/read only the folded active lineage, so suffixes superseded by `history_edit` controls cannot be rediscovered. Freshness comparisons use `(generation_id, history_revision)`, never revision alone, so archive/recreate cannot reuse stale rows. FTS/vector files are derived and disposable under `<data_dir>/recall/` - rebuild on change, never migrate. Deleted Sessions evict immediately from fts/vector/hybrid (`canonical_scan` needs nothing because it scans current canonical state).

## Constraints & Gotchas

- Never expose backend tuning just to make payloads look alike: capabilities describe real behavior while the public Tool keeps one compact field set with declared defaults.
- Never return exact Message text through search as a second read API: bounded excerpts plus single-anchor `session_read` references only; unanchored reads give a User-anchor index, `all_messages` reads complete sessions, anchored reads replace large Tool Results with dereferenceable previews.
- Do not deduplicate Passages by Session - multiplicity is Vector/Hybrid semantics. Do not move structural filters after KNN - post-filtering global top-K produces false empty pages. Do not fuse raw FTS scores with vector distances.
