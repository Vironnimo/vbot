# Recall Agent-facing text

Why each sentence of the vector backend's partial-coverage degradation reason (`SEMANTIC_PARTIAL_REASON`, `core/recall/vector.py`), which `session_search` shows on a degraded page, exists. Rules for this file: `.vorch/workflows/domain-map-workflow.md` -> Agent-facing text. The other Recall texts named in `recall.md` -> Agent-facing text (the Tool summaries and query guidance, the degradation reasons in `hybrid.py` and `canonical.py`) have no recorded reasons yet; add a row for each sentence you change or add.

| Text | Reason |
|---|---|
| `SEMANTIC_PARTIAL_REASON`: `Semantic search has not finished indexing the eligible Sessions and continues in the background.` | Agents read a page with missing vectors as complete; the sentence says coverage is partial and still improving. |
| `Results are incomplete: repeat the search later for complete results.` | Names the one action that helps. Searches embed no documents, so narrowing `period` or `session_id` does not speed indexing. |
| `An empty result does not establish that no related conversation exists.` | Prevents an empty partial page from being reported as proof of absence. |
