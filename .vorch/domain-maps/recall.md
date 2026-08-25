# Recall

Recall is the backend-selected read model for discovering content in persisted chat Sessions; canonical Session storage remains owned by `core/sessions/`, and curated durable facts remain owned by `core/memory/`.

## Overview

Session Recall has two Agent-facing Tools in one deep module. `session_search` owns backend-independent Session listing plus backend-native query discovery; `session_read` retrieves focused canonical conversation blocks or exact Tool Results only through `ChatSessionManager`. This split keeps discovery compact and prevents an index or ranking backend from becoming a second source of truth for Messages.

The selected backend is fixed for a Tool registration. `Runtime.reload_recall_backend()` rebuilds the registry, resolves `settings.json` `recall.backend`, and re-registers both Session Recall Tools with the resolved backend name and canonical `ChatSessionManager`. An unknown selected name or a construction failure in a selected non-default backend falls back to `jsonl_scan` and records that resolved name, so the Agent-facing description and behavior stay aligned. Failure to construct `jsonl_scan` itself remains fatal because no canonical fallback remains.

## Terms

The cross-cutting Session and Tool terms live in `.vorch/GLOSSARY.md`.

### Semantic Recall
**Definition:** Meaning-based session search using vector embeddings instead of keyword matching. A session about "vehicles" can match a query for "cars" because their vectors are nearby in embedding space, even though they share no literal words. Enabled by switching `recall.backend` to `vector` and configuring a `text_embedding` model.
**Not:** Keyword search (substring or FTS - that's what `jsonl_scan` and `sqlite_fts` do). Not curated memory or session browsing. Semantic recall retrieves past sessions by meaning, not by exact terms.

### Passage
**Definition:** A source-derived, overlapping span of eligible canonical Message text used as the retrieval and fusion unit by semantic and Hybrid search. It carries stable Message boundaries and source offsets so a result can point back to exact canonical Messages.
**Not:** A Message, a Session summary, a rendered excerpt, or an independently persisted source of truth.

## Search Contract

- `RecallSearchCapabilities` declares the active backend's result unit, Agent-facing Tool summary and query-parameter description, internal literal-match support, supported ordering, and whether role filtering is meaningful. `session_search` uses one capability value to build both Agent-facing texts and select the backend's declared defaults; its three public fields remain stable across backends.
- `RecallSearchRequest` is the normalized first-party query contract: agent/project scope, excluded Session ids, query, time range, roles, literal match mode, backend-supported order, internal offset/limit, and optional source snapshot. The Agent-facing Tool always requests the first ten results and uses the exclusion field for the current Agent/Session pair so that conversation never enters candidate snapshots or ranking; filtered and paginated variants remain lower-layer capabilities rather than public Tool controls.
- `RecallSearchHit` is one backend-ranked Message or Passage with canonical read references. Raw backend scores stay inside Recall and are not exposed by the Tool. The Session Recall Tool hydrates canonical Session context separately and emits it once per unique Session on the returned page rather than widening or duplicating backend hits.
- `RecallSearchPage` carries one deterministic ranking slice, result type, ranking label, source snapshot, continuation state, candidate-Session count, and optional explicit degradation.
- `SupportsRecallSearch` is the runtime-checkable typed capability implemented by all first-party backends. The older `RecallBackend` browse/overview/search/scroll protocol remains for extension compatibility and internal legacy callers; `session_search` wraps an extension's backend-defined search payload instead of pretending it follows a first-party result shape. Synchronous extension search is moved to a worker thread.
- Every request is scoped by `ToolContext.project_id`, never by a model-supplied project argument. Derived indexes key scope, agent, and Session separately, so equal Session UUIDs in global and Project scopes cannot collide.

## Passage Policy

`core/recall/passages.py` is the shared owner for first-party Passage construction.

- Eligible text comes from the default conversation roles (`user`, `assistant`, `error`, `compaction_checkpoint`). Tool results, ordinary notes, skill-context notes, and persisted `session_search`/`session_read` results are not Passage input.
- Message search text is used without whitespace rewriting or a per-Message character cap. Passages are 1,500-character source windows with 200-character overlap; a long Message therefore becomes multiple Passages instead of being truncated.
- Each Passage records its exact text, stable `passage_id`, start/end Message IDs, start/end timestamps and roles, and offsets within the boundary Messages. The ID derives from the policy version and canonical boundaries; fusion keys it together with `session_id` because forked Sessions may share Message IDs.
- A search excerpt is presentation only. The Tool caps each source-faithful excerpt at 800 characters within its whole-result byte budget and returns a canonical `read_ref`; it does not store excerpt text as canonical history.

## Backend Semantics

### `jsonl_scan`

- Scans canonical Sessions on demand and returns every eligible matching Message, including multiple Messages from one Session.
- Matching is case-insensitive literal substring matching. `telegram` therefore matches `Telegraminstallation`; the Agent-facing Tool uses `all_terms`.
- Results are globally ordered by canonical Message time, newest first, with deterministic Session/message tie-breakers. Other match/order modes remain available only to internal compatibility callers.
- Search and list snapshot both the canonical transcript and Session metadata sidecar because either can change the Agent-facing result. Agent-facing read continuations bind their position to the complete canonical selection digest, so changing the selection arguments or underlying Messages fails instead of mixing content.

### `sqlite_fts`

- Maintains a disposable FTS5 trigram index at `<data_dir>/recall/session_index.sqlite`; schema version 4 drops and lazily rebuilds incompatible indexes.
- Message search has the same case-insensitive substring semantics as JSONL and uses BM25 relevance through the Agent-facing Tool. Queries shorter than the trigram minimum use an in-memory substring relevance scan, so short literals are not lost.
- Candidate rows are filtered by scope, Session, default conversation roles, and time before limiting, then revalidated and hydrated from canonical JSONL. Persisted Session Recall Tool results and skill-context notes are excluded before candidate limiting.
- A separate Passage FTS table, built from the shared Passage policy, is the literal arm used by Hybrid. Normal queries use Passage BM25; short queries use source-derived substring relevance.
- Cleanup reconciles indexed Session ids against the complete canonical agent/project scope, never the request-filtered candidate set; a Session-filtered search therefore cannot evict unrelated Sessions from the shared index.
- Index failure triggers one delete-and-rebuild attempt, then a canonical scan fallback for Message search. The index is never authoritative.

### `vector`

- Maintains two disposable sqlite-vec cosine indexes: typed Passage search uses `<data_dir>/recall/session_passage_vectors.sqlite`, while legacy `RecallBackend.search` keeps `<data_dir>/recall/session_vectors.sqlite`. Their policy-tagged headers and physical files prevent typed Passage vectors from reusing legacy chunks.
- Schema version 7 uses a singleton header that pins provider, configured model, provider-reported actual model, full embedding-space fingerprint, index policy, observed dimension, and schema version. Target/Connection/Account/options, actual model, policy, dimension, or schema changes discard and fully rebuild the affected derived index in the same Search; document and query vectors from different actual models are never mixed.
- Typed search returns pure semantic top-K Passages ordered by cosine distance. It does not literal-revalidate, collapse to one result per Session, apply a universal distance cutoff, or fall back to keyword search.
- Missing embedding configuration or an embedding/store failure returns the stable `semantic_unavailable` failure. This is intentionally different from the legacy `RecallBackend.search` compatibility path, which retains its old degraded JSONL payload for old callers.
- The vec0 table stores scope as a partition key plus Session and Passage time metadata. Scope, optional Session, excluded Sessions, and time-overlap filters execute inside the KNN query before top-K selection, preventing ineligible Passages from starving eligible results.
- Freshness compares canonical Session mtime/size with indexed metadata. Reconciliation always uses the complete canonical agent/project scope, independently of request Session/time filters; missing/stale Sessions are rebuilt under that index's Passage/chunk policy and deleted Sessions are dropped.
- Recall embeds index content with purpose `document` and live searches with purpose `query`; OpenRouter maps these to `search_document`/`search_query`, while unverified compatible Providers remain symmetric. Context overflow recursively splits only multi-input batches, never rewrites the stored Passage text, and a single overlong Passage fails safely.
- Every semantic Search emits at most one normalized Usage log summary aggregated across document backfill/rebuild batches and the query request. It reports successful request counts, token/cost report coverage, summed tokens/cost, and query/document input counts without logging raw Provider payloads or text.
- Schema creation/replacement and vector upserts run in one explicit write transaction; KNN validates an existing matching header and never mutates schema. Store/index failures trigger one exact-file discard (including WAL/SHM sidecars) and full rebuild before surfacing stable unavailability or legacy fallback.

### `hybrid`

- Runs Passage FTS and Vector search concurrently and fuses their independent rankings with Reciprocal Rank Fusion (`k = 60`). A Passage may be marked `literal`, `semantic`, or both; multiple Passages from one Session remain eligible.
- Candidate depth starts above the requested page and grows until the requested prefix cannot be displaced under the unseen RRF score bound, or both arms are exhausted. This preserves true rank fusion without a fixed over-fetch guess.
- Hybrid exposes relevance order. Its Agent-facing literal arm uses the default `all_terms` behavior; internal compatibility requests may still choose another literal mode, which never changes semantic retrieval.
- If one arm is unavailable, the other arm remains usable and the page is explicitly marked degraded with the reason. If both arms fail, search returns `hybrid_unavailable`.
- Hybrid cursors snapshot both arm sources. A canonical source change or backend change invalidates continuation rather than silently mixing rankings.

## Storage, Lifecycle, and Removal

- Canonical Messages always come from `ChatSessionManager`; Recall modules must not construct Session paths.
- FTS and Vector files are derived, disposable indexes under `<data_dir>/recall/`. Schema, embedding-space, dimension, or index-policy changes rebuild rather than migrate them.
- `SupportsSessionRemoval` lets `sqlite_fts`, `vector`, and `hybrid` evict a deleted Session immediately. `jsonl_scan` has no removal method because the canonical live scan already reflects deletion.
- First-party search snapshots remain a backend contract for deterministic lower-layer pages, although the Agent-facing Tool intentionally exposes only the first ten results and no continuation. Index state is not treated as canonical source state.

## Constraints & Gotchas

- Do not expose backend tuning merely to make backend payloads look alike. Backend capabilities must describe real behavior in the Tool summary and `query` description while the public Tool keeps one compact, backend-stable field set and uses declared defaults.
- Do not return exact Message text through search as a second read API. Search returns bounded discovery excerpts and single-anchor `session_read` references and may restrict matching to one known past Session; an unanchored `session_read` returns a compact User-anchor index for navigation, `all_messages` reads the complete canonical Session, and anchored reads derive conversation blocks while replacing large in-block Tool Results with dereferenceable previews and retaining exact single-Result access.
- Do not deduplicate Passage results by Session. Passage-level multiplicity is part of Vector and Hybrid semantics.
- Do not move structural Vector filters after KNN. Post-filtering a global top-K can produce false empty pages even when eligible Passages exist.
- Do not fuse raw FTS scores with vector distances; they are incomparable. Hybrid combines ranks via RRF.
