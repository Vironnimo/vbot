# Recall Agent-facing text

Reasons for the vector backend's partial-coverage degradation reason (`SEMANTIC_PARTIAL_REASON`, `core/recall/vector.py`) and Hybrid query guidance, as shown by `session_search`. Rules for this file: `.vorch/workflows/domain-map-workflow.md` -> Agent-facing text. The other Recall texts named in `recall.md` -> Agent-facing text (the remaining Tool summaries/query guidance and degradation reasons in `hybrid.py` and `canonical.py`) have no recorded reasons yet; add a row for each sentence you change or add.

| Text | Reason |
|---|---|
| `SEMANTIC_PARTIAL_REASON`: `Semantic search has not finished indexing the eligible Sessions and continues in the background.` | Agents read a page with missing vectors as complete; the sentence says coverage is partial and still improving. |
| `Results are incomplete: repeat the search later for complete results.` | Names the one action that helps. Searches embed no documents, so narrowing `period` or `session_id` does not speed indexing. |
| `An empty result does not establish that no related conversation exists.` | Prevents an empty partial page from being reported as proof of absence. |
| `LITERAL_ONLY_REASON` in `_literal_search.py`: `Only keyword search succeeded; paraphrases may be missing.` | Vector and Hybrid fallback results must not be presented as complete semantic matches when embeddings fail. |
| `Use words likely to appear in the conversation; rephrasing cannot restore semantic search.` | Gives an actionable query strategy for a degraded page without inviting repeated attempts to restore an unavailable embedding service. |
| Hybrid query: `Keywords or a short topic description.` | Supports both distinctive names and natural-language recall without requiring a query grammar. |
| `Keyword search matches any query term; semantic search also finds related wording.` | Makes partial keyword matching explicit; the 2026-11-07 production search lost all keyword evidence because one query term was absent under the previous all-terms default. |
| `Results are ranked by combined relevance, not time.` | Prevents interpreting rank as conversation chronology or the latest decision. |
