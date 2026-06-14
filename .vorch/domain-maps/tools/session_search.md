# Session Search Tool

Searches persisted chat Sessions for agent-relevant history.

## Interfaces

- Tool name: `session_search`
- Registration: `register_session_search_tool(registry, recall_backend)`
- Bound service: `RecallBackend`; backend implementations must use the Sessions API rather than constructing session file paths.
- Schema: optional `query`, `agent_id`, `session_id`, `around_message_id`, `since`, `until`, `roles`, `match`, `limit`, `context`, `bookends`, and `sort`; `additionalProperties: false`.
- Four dispatch modes, chosen by arguments (in `session_search_handler`): `around_message_id` set → anchored view (`scroll`); else no `query` + `session_id` set → single-session overview (`overview`); else no `query` + no `session_id` → recent session summaries (`browse`); else → message search (`search`).
- `query`: case-insensitive keywords or phrase. Omit or leave blank to list Session summaries (no `session_id`) or one Session's overview (with `session_id`) instead of message matches.
- `agent_id`: defaults to `ToolContext.agent_id`.
- `session_id`: restricts a query search to one Session; alone (no `query`, no `around_message_id`) it returns that Session's overview.
- `around_message_id`: with `session_id`, returns an anchored context view around that message. It requires `session_id` and cannot be combined with `query`. The anchored message is surfaced even when its role is outside `roles` (it was requested by explicit id); neighbors and bookends still respect `roles`.
- `since` / `until`: inclusive UTC ISO-8601 timestamp or `YYYY-MM-DD`; date-only `until` covers the full day.
- `roles`: defaults to visible chat history plus `compaction_checkpoint`; include `note` explicitly to search kernel notes.
- `match`: `all_terms` (default), `any_term`, or `phrase`.
- `limit`: maximum returned matches or Session summaries, default 20, max 100. Does not apply to the single-session overview.
- `context`: number of neighboring visible messages before and after each match, default 0 for query matches and 2 for anchored views, max 2. Not used by the overview.
- `bookends`: number of start/end Session messages to include for orientation, default 2, max 5. Drives the single-session overview, and adds Session edges to query matches and anchored views.
- `sort`: Session activity order, `newest` default or `oldest`.

## Result Contract

- Success data always includes `content`, `truncated`, and `request`.
- Query searches return `matches`, `searched_sessions`, and `total_candidate_sessions`.
- Anchored searches return `session`, `around_message_id`, `window`, `bookend_start`, and `bookend_end`.
- Summary searches return `sessions` and `total_candidates`.
- Single-session overviews return `session`, `bookend_start`, `bookend_end`, and `total_messages` (count of role-eligible messages); `truncated` is true when messages are omitted between the bookends. A missing `session_id` returns a `session: null` overview with content `No session found: <id>`.
- Match rows include `agent_id`, `session_id`, `message_id`, `timestamp`, `role`, a compact `snippet`, and an ordered `window`; `context` appears only when requested; `bookend_start` and `bookend_end` appear when `bookends` is greater than 0.
- Session rows include `agent_id`, `session_id`, `created_at`, `last_active_at`, and sidecar fields under `metadata`.

## Constraints & Gotchas

- Skill-context notes are not searched even when `note` is requested; they are prompt context, not discoverable history.
- The default implementation scans JSONL Sessions through `JsonlSessionRecallBackend`; optional SQLite FTS is a derived index behind the same `RecallBackend` contract and does not change this public tool shape.
- No-match cases are success envelopes so agents can continue refining the query.
- Invalid arguments and expected Session lookup/storage errors return failure envelopes.
