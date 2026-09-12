# Session Search

Finds conversation content in past Sessions and returns bounded excerpts with canonical conversation context. Extended inspection belongs to the `vbot-cli` Skill, not another Recall Tool.

## Interfaces

- `core/tools/session_search.py` owns the definition, validation, visibility and execution; `_session_recall_results.py` owns internal result shaping. The selected Recall backend ranks hits; canonical `ChatSessionManager` supplies descriptors and bounded context.
- One flat Provider schema: required `query`, optional `period`, `agent_id`, `session_id`, and boolean `include_subagents`. No `additionalProperties` keyword. Backend capabilities supply the summary and query wording. Omitting or blanking `query` fails; there is no Session-list mode or `session_read` Tool.
- Omitted `agent_id` uses the current Agent; omitted `session_id` searches across that Agent's eligible Sessions. The current Agent/Session pair is excluded before ranking. An equal Session id belonging to another Agent remains eligible. Project scope comes only from `ToolContext.project_id`.
- Legacy Sessions without reliable `run_kinds` and Sessions carrying `user`, `channel`, or `cron` are eligible. Reflection kinds and system-only Sessions are always hidden. Sub-Agent markers or Run kinds require `include_subagents: true`; the opt-in never reveals reflection or system-only Sessions. Mixed reflection/User Sessions stay hidden; mixed User/System Sessions stay eligible. Visibility is applied before ranking and rechecked during shaping.
- `period` is an inclusive ISO-8601 `start/end` interval with either endpoint optionally open. Date-only starts begin at 00:00 UTC; date-only ends include the full UTC day. Period applies to matching evidence; nearby context can lie outside it.
- Ordinary search uses User/Assistant content plus labeled Compaction summaries. Tool Results, Tool arguments, reasoning, errors, Skill contexts, and persisted Recall results are excluded. Content Blocks contribute their searchable text projection; search is not an attachment-content reader.
- `sqlite_fts` is the default: all whitespace-separated terms must match. When every term has at least three characters, trigram provides substring matching; otherwise standard FTS provides whole-token matching for the query. `vector` and `hybrid` retain semantic and combined retrieval. `canonical_scan` is an internal degraded fallback, not a selectable backend.
- Public limit, cursor, role, match/order, neighbor count, read/operation selectors, and retired reader arguments are rejected. Lower-layer pagination and role filters are not Agent-facing controls.

## Results

- Success uses the normal Tool envelope. Data includes `project_id`, `result_type`, `items`, `sessions`, `has_more`, and candidate-Session count; degraded pages identify the limitation. Empty search is a successful result. The display derives a presentation-only result count, a lower bound when `has_more` is true.
- At most ten backend-ranked Message or Passage hits are returned. Multiple hits from one Session remain distinct so later corrections or separate matching passages are not collapsed. `session_id` narrows the same search and limit; it requires no prior listing call.
- Hits include rank, Agent/Session/Message identity, role, timestamp, an adaptive source-faithful excerpt, and explicit truncation/offset fields. Passage hits include their passage id and end timestamp. Sub-Agent hits include `include_subagents: true` for a follow-up query. Compaction hits carry `content_kind: compaction_summary` and remain separate from verbatim conversation passages.
- `context` contains at most the enclosing User question and the last nonempty Assistant answer before the next User Message, excluding the hit itself. Each context record includes canonical index/id/role/time, up to 800 characters of text and a truncation flag. Context is always marked `context_is_partial`; it is not a full transcript or proof that no later correction exists. Missing/inactive anchors and summary anchors return no context.
- Context uses `ChatSessionManager.recall_context`, a targeted active-lineage SQL projection; ordinary FTS search never loads a full transcript or Tool graph to decorate a hit. Context is bounded in SQLite before text leaves storage.
- One curated descriptor per unique represented Session appears in first-hit order: title, timestamps, classified Run kinds, tri-state Sub-Agent classification, safe parent/fork provenance, Channel platform, cached Message count, and first User excerpt capped at 240 characters. Open-ended metadata is never returned. Missing legacy classification or unavailable canonical descriptor content remains null.
- Excerpts preserve whitespace and Unicode and are capped at 1,800 characters. The complete result is capped at 50 KiB including descriptors and context; shaping first adapts excerpt size, then reduces hit count if fixed context/metadata requires it. `has_more` advises narrowing query, period or Session.

## Extended inspection

The bundled `vbot-cli` Skill advertises extended Session search and transcript retrieval and routes to `references/session-search.md`. It teaches Session listing through the CLI, read-only SQLite scope selection, complete active User/Assistant transcripts, exact substring searches including Tool Results, and generation-bound exact Result reads. It adds no Bash availability prerequisite, Tool grant, or new runtime API.

## Constraints & Gotchas

- Search is evidence discovery with selected context, not lossless transcript retrieval. There are no `read_ref`, reader continuations, or automatic companion Tool.
- Backend tuning and scores stay internal. Visibility belongs to Tool shaping plus backend exclusions; relevance ordering remains backend-owned.
- Historical persisted `session_read` results remain excluded as derived Recall artifacts even though the Tool no longer exists.
- Extension backends must return typed Search pages; visibility shaping filters their hits by canonical eligible Session ids just like first-party pages.
- Tests: `tests/core/tools/test_session_search*.py`, `tests/core/skills/test_session_search_reference.py`; backend behavior in `tests/core/recall/`; Provider call matrix in `scripts/provider_probe/scenario_history.py`.
