# Vector and Hybrid Backends

Read this reference when you change vector or hybrid search: the search flow, KNN filtering, RRF fusion and depth, degradation, or their continuations. The cross-backend search contract and the ranking invariants stay in `recall.md`; the Passage index they read is `recall/passage-index.md`.

## `vector`

Pure semantic top-K Passages by cosine distance over the Passage index, without literal revalidation, per-session collapsing, universal cutoffs, or keyword fallback; missing embedding config or an index/provider failure yields stable `semantic_unavailable`. The scope partition, the candidate filter (`rowid IN` the Passages the request's candidates show) and time filters execute inside the vec0 KNN before top-K, so ineligible Passages cannot starve eligible ones; each hit is then attributed to one Session (`recall/passage-index.md` -> Passage Catalog). A ranking cut at `KNN_MAX_K` still reports `has_more` on a page that reaches its end.

Search flow: the shared scope read (`CanonicalSessionRecallBackend._read_scope`: one `list_history_revisions` query on the Session database's worker pool) yields every live Session version and Recall visibility of the scope, the request's candidates with their creation order, and the continuation snapshot; with a pinned header of the binding's space a stale continuation fails before any embedding. The query embeds (purpose `query`) and fixes the dimension; the snapshot binds the space, so any space change invalidates continuations. A different space is pinned at once (dropping every vector). The catalog then refreshes for the candidates; the search embeds no documents. While some of the candidates' Passages in the period still wait (skipped texts do not count), the page is `degraded` with `SEMANTIC_PARTIAL_REASON` and the search nudges the indexer. One normalized Usage summary logs at DEBUG per search (`typed_search`). A damaged index is discarded and rebuilt once while a search prepares; damage found by KNN discards it for the next search.

## `hybrid`

Reads the scope once, refreshes the catalog for its candidates once, and prepares the literal arm (Passage FTS) and the semantic arm (query embedding) over the same Passage index, then fuses ranks via Reciprocal Rank Fusion (`k=60`) - never raw score mixing. Candidate depth grows until every selected hit's arm contributions are resolved and the requested prefix cannot be displaced under the unseen-score bound, preserving exact ordering and source labels as well as membership; each deeper pass reruns only the two ranked queries. Depth stops at `_RRF_MAX_DEPTH` (2048, below `KNN_MAX_K`), where the fused prefix is accepted as it stands and a page reaching the end of the fused ranking still reports `has_more` while an arm has more. An arm that fails to prepare or rank stays out for the rest of the search with explicit degradation; both failing returns `hybrid_unavailable`. A semantic arm that answered before indexing finished also degrades the fused page (`hybrid._degradation_reason`: keyword matches complete, or, when the literal arm failed, results incomplete). Cursors snapshot both arms - source or backend changes invalidate continuation rather than mixing rankings.

## Source and tests

- `core/recall/vector.py`, `core/recall/hybrid.py`; shared scope read in `CanonicalSessionRecallBackend` (`core/recall/canonical.py`)
- `tests/core/recall/test_vector*.py`, `test_hybrid.py`; contracts every backend shares (continuation binding, project-scope isolation) live in `test_backend_contract.py`.
