# Recall

Backend-selected read model for discovering content in persisted chat Sessions. Canonical Session storage stays owned by `core/sessions/`; curated durable facts stay owned by `core/memory/`.

## Overview

Two Agent-facing Tools in one deep module: `session_search` owns backend-independent Session listing plus backend-native query discovery; `session_read` retrieves focused canonical conversation blocks or exact Tool Results only through `ChatSessionManager`. This split keeps discovery compact and prevents an index or ranking backend from becoming a second source of truth for Messages.

The selected backend fixes at Tool registration. `Runtime.reload_recall_backend()` rebuilds the registry from `settings.recall.backend` and re-registers both Tools with the resolved name; an unknown name or selected-backend construction failure falls back to `jsonl_scan` (recording the resolved name so description and behavior align). Failing to construct `jsonl_scan` itself is fatal - no canonical fallback remains.

## Terms

Session and Tool terms live in `.vorch/GLOSSARY.md`.

### Semantic Recall
**Definition:** Meaning-based search using vector embeddings instead of keywords - a session about "vehicles" matches "cars". Enabled via `recall.backend: vector` plus a configured `text_embedding` model.
**Not:** Keyword search (that's `jsonl_scan`/`sqlite_fts`), curated memory, or session browsing.

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

### `jsonl_scan`
Scans canonical Sessions on demand with case-insensitive substring matching (`telegram` matches inside longer words; the Tool uses `all_terms`). Globally ordered by canonical message time, newest first, deterministic tie-breakers. Search and list snapshot both transcript and metadata sidecar; continuations bind to a selection digest so changed arguments or underlying messages fail rather than mix content.

### `sqlite_fts`
Disposable FTS5 trigram index at `<data_dir>/recall/session_index.sqlite`; incompatible schema versions drop-and-rebuild lazily. Same case-insensitive substring semantics as JSONL with BM25 relevance through the Tool; sub-trigram queries fall back to in-memory substring relevance so short literals survive. Candidates filter by scope/roles/time before limiting, then revalidate against canonical JSONL; persisted Recall results and skill-context notes exclude before limiting. A separate Passage table (shared policy) serves as Hybrid's literal arm. Cleanup reconciles against the complete canonical scope, never request-filtered candidates - a Session-filtered search cannot evict unrelated Sessions. Index failure triggers one delete-and-rebuild then canonical scan fallback; the index is never authoritative.

### `vector`
Two disposable sqlite-vec cosine indexes: typed Passage search and legacy compatibility search use separate files whose policy-tagged headers prevent chunk reuse. The singleton header pins provider/model/actual-model/embedding-space fingerprint/policy/dimension/schema - any change discards and fully rebuilds in the same search; vectors from different actual models never mix. Typed search returns pure semantic top-K Passages by cosine distance without literal revalidation, per-session collapsing, universal cutoffs, or keyword fallback; missing embedding config or store failure yields stable `semantic_unavailable` (the legacy path keeps its old degraded payload for old callers). Scope/session/exclusion/time filters execute inside the KNN query before top-K so ineligible Passages cannot starve eligible ones. Freshness reconciles against the complete canonical scope independently of request filters. Document vs query embedding purposes are distinct (OpenRouter maps them explicitly); context overflow splits multi-input batches recursively, never rewriting stored text. One normalized Usage summary emits per search; schema creation and upserts run in one write transaction, KNN validates headers without mutating schema, and store failures discard the exact file including WAL/SHM then rebuild.

### `hybrid`
Runs Passage FTS and Vector concurrently, fusing ranks via Reciprocal Rank Fusion (`k=60`) - never raw score mixing. Candidate depth grows until the requested prefix cannot be displaced under the unseen-score bound, preserving true rank fusion without over-fetch guesses. One unavailable arm leaves the other usable with explicit degradation; both failing returns `hybrid_unavailable`. Cursors snapshot both arms - source or backend changes invalidate continuation rather than mixing rankings.

## Lifecycle & Removal

Canonical Messages always come from `ChatSessionManager`; Recall modules construct no Session paths. FTS/vector files are derived and disposable under `<data_dir>/recall/` - rebuild on change, never migrate. Deleted Sessions evict immediately from fts/vector/hybrid (`jsonl_scan` needs nothing - the live scan reflects deletion).

## Constraints & Gotchas

- Never expose backend tuning just to make payloads look alike: capabilities describe real behavior while the public Tool keeps one compact field set with declared defaults.
- Never return exact Message text through search as a second read API: bounded excerpts plus single-anchor `session_read` references only; unanchored reads give a User-anchor index, `all_messages` reads complete sessions, anchored reads replace large Tool Results with dereferenceable previews.
- Do not deduplicate Passages by Session - multiplicity is Vector/Hybrid semantics. Do not move structural filters after KNN - post-filtering global top-K produces false empty pages. Do not fuse raw FTS scores with vector distances.
