# Session Recall Tools

Discovers persisted Sessions through backend-native search and retrieves exact canonical Messages through a separate read-only Tool.

## Interfaces

- `core/tools/session_search.py` owns both Tools and their shared cursor/result machinery. `session_search` receives the selected Recall backend plus canonical `ChatSessionManager`; `session_read` uses only canonical `ChatSessionManager`.
- `session_search` has one flat model-facing schema with exactly `query`, `period`, `agent_id`, `session_id`, `limit`, and `cursor`, `required: []`, and no `additionalProperties` keyword. Omitting `query` lists recent Sessions; providing it performs backend-native search. `agent_id` defaults to the current Agent, `session_id` narrows the target, and `limit` has the stable numeric schema and handler default 10 with a maximum of 100. The handler rejects unknown or malformed arguments and enforces cursor-only continuation.
- `period` is one inclusive ISO-8601 `start/end` interval. Either endpoint may be open; a date-only start begins at 00:00 UTC and a date-only end covers the complete UTC day. Listing selects Sessions containing at least one default-role conversation Message in the interval; query search applies the same bounds inside Recall.
- `session_read` has one flat model-facing schema with exactly `session_id`, `agent_id`, `start_message_id`, `end_message_id`, and `cursor`, `required: []`, and no `additionalProperties` keyword. A new read requires `session_id`; omitted boundaries read the whole Session, one omitted boundary leaves that side open, and two boundaries select an inclusive canonical range. The handler enforces these conditional requirements and rejects unknown or malformed arguments.
- Both Tools accept an opaque `cursor` by itself for continuation; the handler rejects a cursor mixed with any other field. Retired `request.operation`, `action`, operation-key objects, public backend match/order/role controls, split time bounds, single `message_id`, and neighbor-count arguments are rejected.
- Search uses each backend's declared default ranking and matching behavior. Backend capabilities still describe the real internal search contract and append backend-specific guidance to the stable public Tool description, but never change the flat six-field Provider schema.

## Result Contract

- Every success is a normal Tool envelope with `items`, `has_more`, and `formatted_bytes`; paginated pages add `next_cursor`. Results carry no public `action`.
- Session-list items contain Agent/Session identity, creation/activity timestamps, and title. A period-filtered item also carries a directly callable `read_ref` spanning the matching conversation blocks, so fuzzy requests such as “what did we discuss last weekend?” can list by time and immediately read only the relevant range. Search data identifies the resolved backend, result unit (`message`, `passage`, or `backend_defined`), ranking, and candidate-Session count.
- Every first-party search item has a global rank, canonical identifiers, one source-faithful adaptive `excerpt`, and a directly callable `session_read` `read_ref` containing `agent_id`, `session_id`, `start_message_id`, and `end_message_id`. The backend's Message or Passage boundaries seed the reference, then canonical Session data expands both ends from the nearest preceding User Message through the last Message before the next User Message so a multi-Message answer is read as one conversation block.
- Search items do not repeat exact content in a second field. The whole result is capped at 50 KiB; excerpts expand as far as the page budget permits rather than using a fixed per-hit character cap.
- `session_read` returns Session metadata/counts/boundary references plus complete `ChatMessage.to_dict()` records. When one serialized Message cannot fit in the 50 KiB result, consecutive `record_json` segments and cursors reconstruct the exact canonical JSON record without loss.
- List, read, and first-party search cursors bind the internal operation, normalized arguments, Project, source snapshot, and search backend where applicable. A cursor is accepted only by its owning Tool; changed canonical source or backend returns `stale_cursor`.
- Cursor version 2 intentionally invalidates cursors issued by the retired combined Tool contract so old read arguments cannot be replayed with new whole-Session semantics.
- An extension backend without the typed first-party search capability is wrapped as one `backend_defined` result and has no claimed first-party ranking semantics.

## Availability

- `session_search` is the single persisted/configurable capability. Runtime availability derives `session_read` whenever `session_search` is allowed, so existing allowlists gain the companion automatically and a search result never advertises an unavailable reader.
- `session_read` is not independently Project-configurable and is removed from persisted Agent allowlists. Wildcard allowlists expose both registered Tools.

## Constraints & Gotchas

- Exact content belongs to `session_read`, not `session_search`; agents should pass a returned `read_ref` directly when wording or the complete conversation block matters.
- Search is not implicitly Session-grouped. JSONL/FTS may return many Messages from one Session, and Vector/Hybrid may return many Passages from one Session.
- Persisted results from both Session Recall Tools are derived artifacts and are excluded from JSONL matching, Passage construction, FTS indexing, and Vector indexing to prevent recall feedback loops.
- No-match pages are successful empty results. Invalid fields, missing canonical references, cross-Tool cursors, stale cursors, and backend failures are failure envelopes with stable codes.
- Search excerpts preserve source whitespace and Unicode. Their truncation flags and source offsets describe presentation loss only; canonical storage is untouched.
