# Recall

Backend-selected read model for discovering content in persisted chat Sessions. Canonical Session storage stays owned by `core/sessions/`; curated durable facts stay owned by `core/memory/`.

## Overview

One Agent-facing Tool: `session_search` requires a query and returns backend-ranked excerpts with bounded canonical conversation context. Optional Session scope supports focused follow-up searches. Session listing, full transcripts, and exact Tool Results are taught by the bundled `vbot-cli` Skill through CLI and read-only SQLite recipes; there is no `session_read` Tool.

`core/tools/session_search.py` owns Tool definitions, validation, visibility and execution; `_session_recall_results.py` holds internal bounded result projection, conversation context and descriptors under the same owner.

The selected backend fixes at Tool registration. The selectable first-party backends are `sqlite_fts`, `vector`, and `hybrid`; `sqlite_fts` is the Built-in default. `Runtime.reload_recall_backend()` rebuilds the registry from `settings.recall.backend` and re-registers the Search Tool with the resolved name; an unknown name or non-default backend construction failure falls back to `sqlite_fts` (recording the resolved name so description and behavior align). Failing to construct `sqlite_fts` itself is fatal; search inside a constructed SQLite backend may still degrade to `canonical_scan` when the derived index is unavailable.

## Terms

Session and Tool terms live in `.vorch/GLOSSARY.md`.

### Semantic Recall
**Definition:** Meaning-based search using vector embeddings instead of keywords - a session about "vehicles" matches "cars". Enabled via `recall.backend: vector` plus a configured `text_embedding` model.
**Not:** Keyword search (that's `canonical_scan`/`sqlite_fts`), curated memory, or session browsing.

### Passage
**Definition:** A source-derived overlapping span of eligible canonical Message text - the retrieval and fusion unit of semantic and Hybrid search - carrying stable Message boundaries and offsets so results point back exactly.
**Not:** A Message, a Session summary, or an independently persisted source of truth.

## Search Contract

- `RecallSearchCapabilities` declares the active backend's result unit, Agent-facing texts, literal-match support, ordering, and role-filter meaningfulness; one capability value drives both Tool texts and declared defaults while its five public fields stay stable across backends.
- `RecallSearchRequest` normalizes scope (agent/project, excluded Sessions), query, time range, roles, literal mode, order, and pagination into the shared backend contract. The Agent-facing Tool always requests ten results and excludes the current conversation; filtered/paginated variants stay lower-layer capabilities, never public Tool controls.
- Hits are backend-ranked Messages or Passages with canonical source identifiers; raw scores never leave Recall. Pages carry deterministic ranking slices, continuation state, candidate counts, and optional explicit degradation. Continuation snapshots bind the backend, query, scope, filters, ordering and canonical history; offset and page size may vary within that selection. Every request scopes by `ToolContext.project_id`, never a model-supplied argument - derived indexes key scope separately so equal Session UUIDs cannot collide.
- All built-in and Extension backends implement `RecallBackend.search_capabilities` and `search_page`; registry construction rejects missing methods or invalid capability values. Synchronous search methods run off the event loop. Session listing and exact reads are outside the Search Tool; the old browse/overview/search/scroll API and payload adapters are removed.

## Passage Policy

Eligible input comes only from default conversation roles (`user`, `assistant`, `compaction_checkpoint`) - Tool Results, reasoning, Tool arguments, errors, notes, Skill contexts, and persisted Recall results are excluded. Only canonical content text contributes; Compaction summaries are built as separate Passages so they can be labeled reliably. Text is used verbatim (no whitespace rewriting); Passages are 1,500-char windows with 200-char overlap, so long Messages become multiple Passages instead of truncating. Each records exact text, policy-derived stable id, boundary Message ids/timestamps/roles, and in-message offsets; fusion keys id together with `session_id` because forks share Message ids. Search excerpts are presentation-only (capped at 1,800 characters within a 50 KiB result budget); canonical identifiers and partial User/Assistant context support focused follow-up searches or Skill-guided inspection. Passage policy version changes invalidate derived indexes.

## Backends

### Internal `canonical_scan` fallback

Not registered or selectable in Settings. Scans canonical Sessions through `ChatSessionManager` on demand with case-insensitive substring matching (`telegram` matches inside longer words; the Tool uses `all_terms`). Globally ordered by canonical Message time, newest first, with deterministic tie-breakers. A bounded scan selects the newest eligible Messages across all Sessions before text matching, with role/time filters applied before the budget; excess candidates mark the result partial. Continuations bind to a generation-aware selection digest so changed arguments, recreated Sessions, or changed history fail rather than mix content.

### `sqlite_fts`
Lexical search over Sessions-owned external-content FTS inside `<data_dir>/sessions.db`: `messages_fts` indexes normalized canonical Message relations with the standard tokenizer, and `messages_fts_trigram` provides substring matching while excluding Tool-role rows to avoid indexing bulk machine output twice. There is no mirrored searchable-text table. Conversation searches use trigram whenever every term has at least three characters; otherwise standard FTS applies whole-token matching to the query. The FTS column filter restricts matching to content and extracted Content Block text before the candidate limit, so metadata-only matches cannot starve real conversation hits. Broader index columns remain available for lower-layer diagnostic use. Each ordinary Search availability check reads only lifecycle/version/stale/high-water markers; canonical coverage anti-joins belong to startup repair and explicit operator verification, not the Search path. A healthy zero-result public Tool search that excludes Tool rows returns empty without loading canonical histories; lower-level requests that include Tool rows retain canonical fallback because the trigram index deliberately omits them. A stale, unavailable, or unsupported integrated index degrades to `canonical_scan`, and FTS corruption detaches the derived indexes without blocking canonical writes. The SQL canonical fallback inspects at most 10,000 newest eligible Messages; typed Search pages explicitly report degraded/partial results when that budget cannot prove complete coverage instead of presenting a false complete empty result. Scope, Session, exclusion, role, and time filters execute in SQL before the bounded candidate limit; SQLite time comparisons use the indexed `julianday(timestamp)` expression rather than raw ISO ordering, so `Z`, `+00:00`, equivalent offsets, and fractional precision remain equivalent. Result shaping decodes the canonical Message payload returned with each FTS hit and reads only targeted Session descriptors and bounded conversation context, never complete histories. Persisted Recall results and Skill-context notes are excluded at canonical write time. A separate disposable Passage FTS at `<data_dir>/recall/session_index.sqlite` remains only for Hybrid's literal Passage arm until integrated Passage FTS lands; it uses the Sessions-owned journal-mode guard, rebuilds on schema mismatch, reconciles against the complete canonical scope, and is never authoritative.

### `vector`
One disposable sqlite-vec cosine index at `<data_dir>/recall/session_passage_vectors.sqlite` stores source-derived Passages. The singleton header pins provider/model/actual-model/embedding-space fingerprint/policy/dimension/schema - any change discards and fully rebuilds in the same search; vectors from different actual models never mix. The store uses the Sessions-owned SQLite journal-mode guard. Typed search returns pure semantic top-K Passages by cosine distance without literal revalidation, per-session collapsing, universal cutoffs, or keyword fallback; missing embedding config or store failure yields stable `semantic_unavailable`. Scope/session/exclusion/time filters execute inside the KNN query before top-K so ineligible Passages cannot starve eligible ones. Invalid or missing indexed timestamps use unbounded interval endpoints, keeping malformed legacy metadata eligible for time-filtered canonical hydration instead of silently assigning it to 1970. Freshness reconciles against the complete canonical scope independently of request filters. Document vs query embedding purposes are distinct (OpenRouter maps them explicitly); context overflow splits multi-input batches recursively, never rewriting stored text. One normalized Usage summary emits per search; schema creation and upserts run in one write transaction, KNN validates headers without mutating schema, and store failures discard the exact file including WAL/SHM then rebuild.

### `hybrid`
Runs Passage FTS and Vector concurrently, fusing ranks via Reciprocal Rank Fusion (`k=60`) - never raw score mixing. Candidate depth grows until the requested prefix cannot be displaced under the unseen-score bound, preserving true rank fusion without over-fetch guesses. One unavailable arm leaves the other usable with explicit degradation; both failing returns `hybrid_unavailable`. Cursors snapshot both arms - source or backend changes invalidate continuation rather than mixing rankings.

## Lifecycle & Removal

Canonical Messages always come from `ChatSessionManager`; Recall modules construct no Session paths and index/read only the folded active lineage, so suffixes superseded by `history_edit` controls cannot be rediscovered. Candidate and freshness enumeration uses normalized Session summaries rather than decoding open-ended metadata; complete active histories are loaded only for canonical scans or when a stale Session must be rechunked for a derived index. Vector and Hybrid hits carry canonical Passage boundaries and do not hydrate a complete Session per hit. Freshness comparisons use `(generation_id, history_revision)`, never revision alone, so archive/recreate cannot reuse stale rows. FTS/vector files are derived and disposable under `<data_dir>/recall/` - rebuild on change, never migrate. Deleted Sessions evict immediately from fts/vector/hybrid (`canonical_scan` needs nothing because it scans current canonical state). A Session that vanishes between listing, freshness lookup, and Vector hydration is skipped; a stale derived hit never fails the whole search and is pruned by subsequent complete-scope reconciliation.

Vector regressions in `tests/core/recall/test_vector*.py` separate typed search, index lifecycle, embedding batching, Passage projection and scope isolation. `vector_helpers.py` owns shared fake embedding engines and canonical Session setup.

## Constraints & Gotchas

- Never expose backend tuning just to make payloads look alike: capabilities describe real behavior while the public Tool keeps one compact field set with declared defaults.
- Search returns selected excerpts and explicitly partial context, not a transcript. The Skill owns guidance for extended read-only SQLite inspection; runtime Recall callers continue to use `ChatSessionManager`.
- Do not deduplicate Passages by Session - multiplicity is Vector/Hybrid semantics. Do not move structural filters after KNN - post-filtering global top-K produces false empty pages. Do not fuse raw FTS scores with vector distances.
