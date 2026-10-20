# Shared Conversation Timeline

Read this when changing conversation ordering, History/live overlap, semantic Run projection, timeline paging or the interactive streaming protocol. WebUI presentation remains in `webui/chat.md`; terminal interaction in `cli/tui.md`.

## Ownership

`core/runs/_timeline.py::RunTimeline` folds provider-neutral Run events at emission, before bounded raw replay can discard them. It retains current semantic elements, not another event log. `core/chat/timeline.py::history_timeline` adapts public canonical History records into the same reducer. Sessions remain the only durable source; no timeline database or format migration exists.

The active Run owns the projection and a change signal. Snapshot copies are cached by sequence so multiple readers share the captured version. Settlement releases the Run's reference; an attached stream retains its projection through terminal delivery, while a later subscriber loads that exact Run's durable records. Raw event streams and their retention contracts remain available to one-shot CLI and other consumers.

`server/timeline.py` owns public snapshot/patch transport and file projection. `server/rpc/chat_methods.py` exposes `chat.timeline` through the existing Chat History read boundary, preserving address, generation, paging, Queue/activity and Usage ownership. Extension page adapters reuse this owner behind their page capability.

## Semantic contract

The server supplies ordered, stable `id` values for User messages, Assistant Runs and their children: readable Reasoning, Assistant output, Tool calls/results, delivered Steer, Compaction and fallback notices, and goal rows (`goal_event_recorded`). Agent takeover and visible errors retain their chronology. A goal row outside any Run is a top-level `goal_event` item; one that falls between a Run's records follows that Run instead of splitting it. A goal row ahead of a Run's first User message keeps that message the Run's input, not a Steer. Reused Provider Tool-call ids in later Model steps denote separate occurrences. Failed stream attempts discard only their transient phase; completed output remains visible. Terminal Runs settle unfinished Tools as cancelled/interrupted rather than leaving them running.

Canonical Assistant Messages finalize all transient text sections of their Model step, including interleaved Reasoning/output and content normalized to empty. Earlier completed steps remain intact. Older-page consumers prepend into their current projection, preserving a newer live sequence; Extension History supplies the active Run identity and timeline sequence required for that comparison.

History pages include whole Run segments even when the requested message count falls inside one. Active History captures overlay the complete in-memory Run, so late attachment works beyond the raw replay window. Older pages do not splice the current Run into the past. `history_generation`, reset flags and `replace_run_ids` make edits, generation changes and complete-Run replacement explicit. Clients do not infer overlap from text or neighboring roles.

## Transport

`chat.timeline` returns `timeline_version: 1`, semantic `items`, paging and Session facts. It retains public `messages` for the existing edit and History bookkeeping callers. An active response supplies its `timeline_sequence` and `active_run.timeline_url`.

Run state retains independent `output_speed` and `output_latency` projections from Chat's `output_measurements`, including the answering Model and Assistant message id. A short step can advance latency without discarding the last measurable rate. Canonical Assistant output and `model_step_usage` update them; terminal events preserve them. Durable reconnects rebuild the same projections from the exact Run's Assistant records, and paged Session History supplies whole-current-view measurements independently (`server/chat-rpcs.md`).

`GET /api/runs/{id}/timeline` starts every connection with `timeline_snapshot`, followed by `timeline_update`. A snapshot replaces its named Run; an update has `run_id`, `sequence` and exact `base_sequence`, changed Run fields, keyed new/replaced children, child field updates and append-only text suffixes, removals and explicit order when needed. Updates are batched at most every 50 ms. They do not re-read durable History or transmit the whole transcript per token. A client with a mismatched base obtains a new snapshot rather than guessing or replaying its own semantic reducer. Heartbeats carry transport liveness only.

Interactive accessors opt into `chat.timeline {child_limit: 1..200}` and the matching `/api/runs/{id}/timeline?child_limit=N`. These responses bound each Run's delivered children, retain raw User records for edit bookkeeping, and represent omitted prefixes with a `history_gap`. `chat.timeline_children` reads earlier children of an exact Agent/Project/Session/Run, validates the Session generation and cursor, and returns positions without replacing newer loaded children. Windowed stream patches send previously undelivered changed children in full, retain suffix edits for delivered text, and use numeric positions instead of full child-order arrays; reconnects preserve a loaded immutable prefix. A history reset discards it. The durable read still projects complete touched Runs before slicing; the option bounds delivery, not canonical reconstruction. Extension pages opt in through their page capabilities (`webui/extension-pages.md` -> Run streams and history); consumers without the option retain the complete-Run contract.

Public projection recursively removes opaque Provider fields and internal file references, signs allowed file links and keeps displayed text consistent with its canonical Message. Extension streams additionally grant only verified file URLs and recheck the current owner/page registration after awaited work. Stream responses explicitly close their generators and pending waits on disconnect, send failure and shutdown.

WebUI and TUI own formatting, local drafts, scroll/selection, disclosure and navigation. `/ws` remains the lifecycle/activity/invalidation channel; high-volume transcript content uses the selected Run's timeline stream. Closing either accessor detaches from server work.

## Source and verification

- Reducer and durable adapter: `core/runs/_timeline.py` (incremental Tool-argument previews in `_timeline_arguments.py`), `core/chat/timeline.py`; contracts in `tests/core/runs/test_runs_timeline.py` and `tests/core/chat/test_timeline.py`.
- Public stream, bounded updates and file sanitization: `server/timeline.py`, `server/app.py`; `tests/server/test_timeline.py`.
- Complete-Run paging and exact durable lookup: `core/sessions/_store_timeline.py`, `session.py`, `store.py`; Session timeline and Chat History tests.
- Extension capability integration: `server/rpc/extensions_methods.py`; `tests/server/rpc/test_extensions_methods.py`, `tests/server/test_app.py`, `tests/server/test_file_delivery.py`.
