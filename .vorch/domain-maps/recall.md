# Recall

Backend-selected read model for discovering content in persisted chat Sessions. Canonical Session storage stays owned by `core/sessions/`; curated durable facts stay owned by `core/memory/`.

## Overview

One Agent-facing Tool, `session_search`, returns backend-ranked excerpts with bounded canonical conversation context; its definition, arguments, result shaping and the `vbot-docs` Skill path for extended inspection belong to `tools/session_search.md`. There is no `session_read` Tool.

The selected backend fixes at Tool registration. The selectable first-party backends are `sqlite_fts`, `vector`, and `hybrid`; `sqlite_fts` is the Built-in default. `Runtime.reload_recall_backend()` rebuilds the registry from `settings.recall.backend` and re-registers the Search Tool with the resolved name; an unknown name or non-default backend construction failure falls back to `sqlite_fts` (recording the resolved name so description and behavior align); an unknown name warns once per configured value, not on every reload. Failing to construct `sqlite_fts` itself is fatal; search inside a constructed SQLite backend may still degrade to the Session store's substring scan when the derived index is unavailable.

`core/runtime/_recall.py::RecallIntegration` owns the one Passage index (`core/recall/passage_index.py`) and the one `SemanticIndexer` (`core/recall/semantic_indexer.py`) per Runtime; both outlive backend reloads. `RecallBackendRegistry.with_builtins(passage_index=..., on_waiting=...)` hands that index to `vector` and `hybrid`; a backend built without one (scripts, tests) opens and owns its own. Extension backends keep receiving only `RecallBackendContext`.

## Terms

Session and Tool terms live in `.vorch/GLOSSARY.md`.

### Semantic Recall
Meaning-based search over vector embeddings instead of keywords, so a Session about "vehicles" matches "cars"; the `vector` and `hybrid` backends provide it (Semantic Indexing, Backends). Not keyword search (`sqlite_fts`), curated Memory, or Session browsing.

### Passage
A source-derived, overlapping span of eligible canonical Message text that carries stable Message boundaries and offsets, so results point back exactly; the retrieval and fusion unit of semantic and Hybrid search (Passage Policy). Not a Message, a Session summary, or an independently persisted source of truth.

### Embedding space
The embedding identity whose vectors one Passage index holds, so vectors of two spaces never mix and leaving a space drops every vector (`recall/passage-index.md` -> Index storage). Not the model name a provider reports serving.

## Search Contract

- `RecallSearchCapabilities` declares the active backend's result unit, Agent-facing texts, literal-match support, ordering, and role-filter meaningfulness; one capability value drives both Tool texts and declared defaults while its five public fields stay stable across backends.
- `RecallSearchRequest` normalizes scope (agent/project, Recall visibility via `include_subagents`, excluded Sessions), query, time range, roles, literal mode, order, and pagination into the shared backend contract. Every backend admits only Sessions whose Session-store Recall visibility (see `sessions/lists-and-metadata.md` -> Recall visibility) the request allows: `conversation` always, `subagent` only with `include_subagents`, `hidden` never. Explicit exclusions name individual Sessions only. The Agent-facing Tool uses `all_terms` matching and defaults to ten hits, accepting an unadvertised `limit` of 1-10; it excludes the current conversation and, under an inherited permission ceiling, the Agent's Sessions outside its conversation tree (`tools/session_search.md`). Role/match/order controls and pagination remain lower-layer capabilities, never public Tool controls.
- Hits are backend-ranked Messages or Passages with canonical source identifiers; raw scores never leave Recall. Pages carry deterministic ranking slices, continuation state, candidate counts, and optional explicit degradation. Continuation snapshots bind the backend, query, scope, filters, ordering and canonical history; offset and page size may vary within that selection. Every request scopes by `ToolContext.project_id`, never a model-supplied argument - derived indexes key scope separately so equal Session UUIDs cannot collide.
- All built-in and Extension backends implement `RecallBackend.search_capabilities` and `search_page`; registry construction rejects missing methods or invalid capability values. Synchronous search methods run off the event loop. Session listing and exact reads are outside the Search Tool; the old browse/overview/search/scroll API and payload adapters are removed.

## Passage Policy

Sessions owns canonical searchable roles, persisted Recall-result exclusion and Content Block text extraction in `core/sessions/search_content.py`; storage, Message search and Recall consume that one policy. Passages cover only default conversation roles (`user`, `assistant`, `compaction_checkpoint`), with Compaction summaries as separately labeled Passages; each canonical entry owns its own overlapping windows, which never cross entry boundaries, and `PASSAGE_POLICY_VERSION` rebuilds the index when the policy changes (`recall/passage-index.md` -> Passage windows).

## Passage index

`<data_dir>/recall/passage_index.sqlite` is one kernel disposable projection (`database.md`) serving `vector` and `hybrid`: the Passage catalog, literal Passage FTS, and the vectors of one pinned Embedding space. It is never authoritative and is used only through async reads and writes, so no SQLite work runs on the Event Loop. Passages are stored once per Recall scope and shared by every Session that shows them; a hit is reported for exactly one candidate Session, the owner when it is eligible and still shows it, else the newest eligible Session that shows it - the same attribution as store search (`sessions/search.md`). Tables, refresh, restore after a `sessions.db` snapshot restore, space pinning and damage recovery: `recall/passage-index.md`.

## Semantic Indexing

`SemanticIndexer` is the only owner of document embedding; searches embed only their query. It runs while the selected backend is `vector` or `hybrid` and a `text_embedding` binding is set, as the Runtime loop task `recall-semantic-indexer` (never in safe-startup mode), nudged by every terminal Run, a periodic sweep, `text_embedding` binding changes and searches that find waiting Passages. A pass catalogs every Recall-visible Session of every live scope and embeds the waiting Passages; a text the provider permanently rejects is bisected out and skipped until the space changes or a rebuild, and a local Model's failures read `local_model_missing` or `local_engine_failed`. Its `IndexStatus` reaches listeners on every state change and at most every 2 s while indexing; the server bridges it to the `recall_index_status` event and serves `recall.status`/`recall.rebuild_index` (`server/rpc/recall_methods.py`). Triggers, batching, failures and backoff, space changes and status fields: `recall/semantic-indexing.md`.

## Backends

- `canonical_scan` (`CanonicalSessionRecallBackend`): internal, not registered or selectable in Settings, and the base whose Message page shaping `sqlite_fts` reuses. It calls `ChatSessionManager.search_messages(use_fts=False)` and asks for one hit beyond the page, so `has_more` is exact; a page the 10,000-candidate budget cut short is degraded/partial. Continuations bind to a generation-aware selection digest so changed arguments, recreated Sessions, or changed history fail rather than mix content.
- `sqlite_fts`: lexical search over the Sessions-owned external-content FTS inside `<data_dir>/sessions.db`; the backend keeps no derived index of its own. It calls `search_messages` with FTS, whose literal check keeps pages full and `has_more` exact even when standard FTS over-matches (a `C#` query searches the token `c`), and reports a fallback scan (stale, unavailable or failed index; the reason names the scan order used) or an incomplete candidate budget as an explicit degradation instead of presenting a false complete empty result. Index choice, the literal check, attribution, fallbacks and time filters: `sessions/search.md` -> `search_messages`.
- `vector`: pure semantic top-K Passages by cosine distance; structural filters run inside the vec0 KNN, and missing embedding config or an index/provider failure yields `semantic_unavailable`. `hybrid`: a literal Passage FTS arm and a semantic arm over the same index, fused by Reciprocal Rank Fusion (`k=60`); one failing arm degrades the page, both failing yields `hybrid_unavailable`. Search flows, fusion depth and continuations: `recall/backends.md`.

## Lifecycle & Removal

Canonical Messages always come from `ChatSessionManager`; Recall modules construct no Session paths and index/read only each Session's current view (`load_since(None).active_messages`, `recall_context`), so entries superseded by a history edit cannot be rediscovered. Candidate and freshness enumeration uses version rows (`list_history_revisions` per scope, `list_all_history_revisions` for indexing passes; both carry each Session's Recall visibility) rather than Session summaries or open-ended metadata; complete active histories are loaded only when a stale Session must be re-projected. Vector and Hybrid hits carry canonical Passage boundaries and do not hydrate a complete Session per hit. Freshness comparisons use `(generation_id, history_revision)`, never revision alone, so archive/recreate cannot reuse stale rows, and hold only for one Session history (`history_id`, `recall/passage-index.md` -> Passage Catalog).

- `<data_dir>/recall/` belongs to `RecallIntegration` alone: at construction it deletes every file there except `passage_index.sqlite` and its `-wal`/`-shm`/`-journal` sidecars (earlier layouts such as `session_index.sqlite` and `session_passage_vectors.sqlite`), skipping files a database handle of this process holds open; subdirectories stay.
- The Passage index is a kernel disposable projection - rebuilt on a projection version change or damage, never migrated.
- Backends implement `SupportsClose`: `vector`/`hybrid` close only an index they own. `RecallIntegration` closes a backend replaced by `reload_recall_backend()` and the active one on shutdown, after stopping the indexer; it closes the shared index last.
- Deletion is best-effort and independent of the selected backend: when the archive service archives a Session (`session.delete`, `archive.md`), `Runtime.remove_session_from_recall` evicts it from the Passage index whenever its file exists, and from an Extension backend implementing `SupportsSessionRemoval`; when it archives an Agent (`agent.delete`), `RecallIntegration.remove_agent_from_recall` evicts the Agent's whole scope. Index errors are logged, never fail the delete. A restore needs no call, because its Sessions are live again and the next semantic indexing pass catalogs them again; a purge deletes only archived Sessions, which Recall already left. A Session that vanishes between listing, freshness lookup, and Vector hydration is skipped; a stale derived hit never fails the whole search and is removed by a later refresh's pruning.

Contracts every backend shares (continuation binding, project-scope isolation) live in `tests/core/recall/test_backend_contract.py`; Runtime wiring (stale files, deletion under any backend, Run nudge) is in `tests/core/runtime/test_runtime_recall.py`.

## Constraints & Gotchas

- Never expose backend tuning just to make payloads look alike: capabilities describe real behavior while the public Tool keeps one compact field set with declared defaults.
- Search returns selected excerpts and explicitly partial context, not a transcript. The Skill owns guidance for extended read-only SQLite inspection; runtime Recall callers continue to use `ChatSessionManager`.
- Do not deduplicate Passages by Session - multiplicity is Vector/Hybrid semantics. Do report each shared Passage once: attribution, not per-viewer rows, decides the Session. Do not move structural filters after KNN - post-filtering global top-K produces false empty pages. Do not fuse raw FTS scores with vector distances.
- Never embed documents on the search path, and never ask vec0 for `k > KNN_MAX_K`: sqlite-vec rejects it, and before the cap the kernel classified that rejection as damage and discarded every vector.
- Skipped texts never count as waiting, so a page can be complete while a skipped Passage stays unsearchable by meaning; only a space change or `recall.rebuild_index` retries them.

## Agent-facing text

This domain owns the backends' Agent-facing search texts in `core/recall/`: the `search_capabilities()` Tool summaries and query guidance (`canonical.py`, `sqlite_fts.py`, `vector.py`, `hybrid.py`) and the degradation reasons (`SEMANTIC_PARTIAL_REASON` in `vector.py`, the `_*_REASON` texts in `hybrid.py`, `CANONICAL_FALLBACK_PARTIAL_REASON` and `_fts_fallback_reason` in `canonical.py`). Before you change, add or delete a sentence of them, read `recall/agent-facing-text.md`: it records why each sentence exists. Update its row for every sentence you change and add one for every sentence you add.

## References

Read these only when your task matches - not by default.

- Changing the Passage catalog or index: Passage windows, tables, refresh and restore, attribution, literal FTS or vec0 storage, Embedding space pinning, the KNN limit, or damage recovery -> `recall/passage-index.md`
- Changing semantic indexing: triggers, passes, batching, failures, backoff and skips, space changes, `IndexStatus` fields, or indexer logs -> `recall/semantic-indexing.md`
- Changing vector or hybrid ranking, KNN filtering, RRF depth, degradation, or their continuations -> `recall/backends.md`
- Changing the `session_search` Tool surface, its arguments, result shaping or excerpt caps -> `tools/session_search.md`
- Changing Session FTS, `search_messages` or the scan the `sqlite_fts` and `canonical_scan` backends use -> `sessions/search.md`
