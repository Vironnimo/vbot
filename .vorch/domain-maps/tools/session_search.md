# Session Search Tool

Discovers persisted Sessions through backend-native search and retrieves exact canonical Messages through explicit read references.

## Interfaces

- Tool name: `session_search`; registration receives the selected Recall backend, canonical `ChatSessionManager`, and resolved backend name.
- Input is exactly one top-level `list`, `overview`, `search`, or `read` operation object. Each nested object exposes and structurally requires only its operation-specific fields; a continuation uses the same operation with only its opaque `cursor`. The handler additionally accepts the retired flat `{action, ...}` form for compatibility.
- `list` returns recent Session summaries and paginates with `limit` (default 10, maximum 100).
- `overview` requires `session_id` and returns metadata, total Message and per-role counts, and first/last canonical Message references. It does not return Message excerpts.
- `search` requires a non-blank query and delegates only retrieval/ranking to the configured backend. Common scope, optional Session, time bounds, limit, and cursor controls remain stable; literal matching, role filtering, and ordering appear in the schema only when the active backend declares them.
- `read` requires `session_id` plus either one `message_id` or an inclusive `start_message_id`/`end_message_id` range. `before`/`after` add canonical neighboring Messages (default 0, maximum 100).
- Date-only `since` and `until` are inclusive UTC boundaries; date-only `until` covers the full day. Agent defaults to `ToolContext.agent_id`; project scope always comes from `ToolContext.project_id`.

## Result Contract

- Every success is a normal Tool envelope. Action data includes `action`, `has_more`, and `formatted_bytes`; paginated pages include `next_cursor`.
- Search data identifies the resolved backend, result unit (`message`, `passage`, or `backend_defined`), and ranking. Items have a global rank, canonical identifiers, one source-faithful adaptive `excerpt`, and one `read_ref`. Passage results also carry `passage_id`, end timestamp, and literal/semantic source membership when available.
- Search items do not repeat content in `content`, context windows, bookends, or backend scores. The whole result is capped at 50 KiB; excerpts expand as far as the page budget permits rather than using a fixed per-hit character cap.
- Read returns complete `ChatMessage.to_dict()` records. It has no per-Message truncation. When one serialized Message cannot fit in the 50 KiB result, it returns consecutive `record_json` segments with source offsets and a cursor; concatenating all segments reconstructs the exact canonical JSON record.
- List, read, and first-party search cursors bind action, normalized arguments, project, source snapshot, and (for search) resolved backend. A changed source or backend returns `stale_cursor`.
- An extension backend without the typed first-party search capability is wrapped as one `backend_defined` result and has no claimed first-party match/order semantics.

## Constraints & Gotchas

- Exact content belongs to `read`, not `search`; agents should follow `read_ref` when wording or the full Message matters.
- Search is not implicitly Session-grouped. JSONL/FTS may return many Messages from one Session, and Vector/Hybrid may return many Passages from one Session.
- No-match pages are successful empty results. Invalid action fields, missing canonical references, stale cursors, and backend capability failures are failure envelopes with stable codes.
- Search excerpts preserve source whitespace and Unicode. Their truncation flags and source offsets describe presentation loss only; canonical storage is untouched.
