# Session Search Tool

Searches persisted chat Sessions for agent-relevant history.

## Interfaces

- Tool name: `session_search`
- Registration: `register_session_search_tool(registry, sessions)`
- Bound service: `ChatSessionManager`; the tool must use the Sessions API rather than constructing session file paths.
- Schema: optional `query`, `agent_id`, `session_id`, `since`, `until`, `roles`, `match`, `limit`, `context`, and `sort`; `additionalProperties: false`.
- `query`: case-insensitive keywords or phrase. Omit or leave blank to list matching Session summaries instead of message matches.
- `agent_id`: defaults to `ToolContext.agent_id`.
- `session_id`: restricts the search to one Session.
- `since` / `until`: inclusive UTC ISO-8601 timestamp or `YYYY-MM-DD`; date-only `until` covers the full day.
- `roles`: defaults to visible chat history plus `compaction_checkpoint`; include `note` explicitly to search kernel notes.
- `match`: `all_terms` (default), `any_term`, or `phrase`.
- `limit`: maximum returned matches or Session summaries, default 20, max 100.
- `context`: number of neighboring visible messages before and after each match, default 0, max 2.
- `sort`: Session activity order, `newest` default or `oldest`.

## Result Contract

- Success data always includes `content`, `truncated`, and `request`.
- Query searches return `matches`, `searched_sessions`, and `total_candidate_sessions`.
- Summary searches return `sessions` and `total_candidates`.
- Match rows include `agent_id`, `session_id`, `message_id`, `timestamp`, `role`, and a compact `snippet`; `context` appears only when requested.
- Session rows include `agent_id`, `session_id`, `created_at`, `last_active_at`, and sidecar fields under `metadata`.

## Constraints & Gotchas

- Skill-context notes are not searched even when `note` is requested; they are prompt context, not discoverable history.
- The current implementation scans JSONL Sessions through `ChatSessionManager`; the tool shape should remain stable if Sessions later move to SQLite.
- No-match cases are success envelopes so agents can continue refining the query.
- Invalid arguments and expected Session lookup/storage errors return failure envelopes.