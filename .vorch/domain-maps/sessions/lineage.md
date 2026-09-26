# Session lineage, edits and deletion

Task-gated detail for `sessions.md`: how forks share history, how a history edit supersedes, and how deletion materializes copies. The terms current view, own audit, lineage, fork point, superseded entry and materialized copy are defined in `sessions.md` -> Terms. Source: `core/sessions/_store_lineage.py`, `_store_mutations.py` (fork, move, delete and detachment), `_store_operations.py` (edit), `_store_codec.py` (`copy_entries`). Tests: `tests/core/sessions/test_lineage_model.py` (random appends, forks, edits and deletes against a reference model: every live Session's current view and own audit, the narrow reads (status facts, User count, newest prefixed Note, and the own spend that excludes a fork's inherited prefix), and search coverage of exactly the entries some view shows, each reported once), `test_lineage_storage.py`, `test_store_query_plans.py`.

## Views

- `view_ranges(connection, session_key)` returns the current view as sorted disjoint `ViewRange(source_key, from_seq, upto_seq, as_of_seq)` covering `[0, MAX_SEQ)`: each `session_lineage` row reads its ancestor, and every other seq reads the Session's own entries with `as_of_seq = MAX_SEQ`.
- One predicate decides visibility for every range: `session_key = source AND from_seq <= seq < upto_seq AND (superseded_at_seq IS NULL OR superseded_at_seq >= as_of_seq)`. Own ranges admit only non-superseded entries; an inherited range also admits ancestor entries superseded at or after the ancestor's `next_seq` at fork time. `_store_lineage.admits` is its one SQL fragment: `range_predicate` (bound parameters), `VIEW_MATCH` (a `VALUES` CTE), `segment_admits` (a `session_lineage` row: search candidates and attribution, FTS membership) and `copy_entries` all build on it, and `own_current` is its own-range case (`superseded_at_seq IS NULL`). Do not restate the predicate in SQL.
- `ordered_rows` reads a view in seq order with one `(session_key, seq)` index range scan per range and stops at `limit`; `view_query` builds one set-wise statement that joins a `VALUES` CTE of the ranges to `entries` (`VIEW_MATCH`); `clip` restricts ranges to a seq window. `entry_id_at` returns the id stored at one seq of the range holding it, superseded or not (cursor anchors, a fork's `last_entry_id`).
- Own audit (`_store_history._own_audit`): own entries with `seq >= fork_point_seq` (0 without a fork), superseded ones included. Own spend (`session_usage`) sums the same range's Assistant usage.
- A fork owns no row for an ancestor's Runs; inherited entries reference the ancestor's Runs, so the fork can neither append to nor settle them.

## Fork

`_store_mutations.fork` runs in one write transaction and copies no entries:
- Fork point: `MIN(start_seq)` of the source's running non-inherited Runs, else the source's `next_seq`. A fork therefore never contains a running Run's partial output.
- `create_fork_lineage` clips the source's view to `[0, fork point)`: the source's own ranges become segments on the source with `as_of_seq` = the source's current `next_seq`; the source's own segments are copied with their `as_of_seq`. The result is flat, so a fork of a fork references the storing Session directly and no read follows lineage recursively.
- The new row: fresh id and generation, `next_seq` and `fork_point_seq` = fork point, `last_entry_id` = the id at `fork point - 1`, `created_at` and `last_activity_at` copied from the source, `forked_at` = now, `fork_parent_key` = the source's key.
- Metadata: the source's facade minus `SESSION_FORK_ALWAYS_STRIP_META_KEYS` (`source_channel_id`, `platform`, `platform_conv_id`, `last_reply_target`, `is_subagent_session`, `subagent_parent`, `reflection_counters`, `run_kinds`) and `fork_source`; a given title replaces the source's, an empty one clears it; a given Run kind is recorded.
- Prompt state follows `carry_prompt_state` (`sessions.md` -> Interfaces -> `fork`): same scope keeps pins, seen Skills and the effective affinity id; another scope drops `AGENT_BOUND_PROMPT_PIN_SLOTS` and the seen Skills and starts a new affinity id. Pins share their content-addressed blobs.
- An owner-managed source is rejected unless the manager passes `allow_owner_managed_source` (a named target Agent in another scope). Temporary bindings, receipts and execution owners never carry over.

`move` updates the one row's address; lineage references `session_key`, so descendants and ancestors stay attached. It rejects owner-managed Sessions and an occupied destination and applies the same prompt-state scope rule. Archive changes only the row state: an archived ancestor's entries stay readable by its descendants.

## History edit

`_store_operations.apply_edit` (one transaction, 60s transcript budget):
1. Validate: the replacement contains a User Message and only appendable roles; the target is the newest current User entry whose id is `target_message_id`, and `_store_timeline._editable` holds (plain text, no named sender, after the latest current Agent takeover). A `run_id` must name a running non-inherited Run of this Session.
2. Collect FTS candidates: own non-superseded entries at or after the target, plus superseded entries of the inherited ranges the edit removes (their search membership may hang on this Session's lineage). Forget them.
3. Supersede own non-superseded entries at or after the target at `marker_seq` (the current `next_seq`), then `truncate` the lineage at the target (segments starting at or after it are deleted, longer ones shortened). `fork_point_seq` does not change.
4. Insert the `history_edit` marker at `marker_seq`, superseded at its own seq; set `next_seq` and `cursor_floor_seq` to `marker_seq + 1`, so every earlier cursor stops continuing.
5. Append the replacement Messages, then re-index the candidates; only entries some view still holds return.
6. Delete the Continuation and fold `continuation_records`; record `seen_skills` if given; rotate the prompt-cache affinity id.
7. If no current User entry precedes the target, clear `auto_title` and `auto_title_initialized` (no title callback).
8. Return the complete own audit and current view with the new affinity id; `ChatSession.apply_edit` then drops its Skill activation cache.

## Delete

`delete_session(connection, session_key)` is the only way a `sessions` row is removed (`delete`, `delete_temporary_group`); `session_lineage.ancestor_key` is `ON DELETE RESTRICT`, so a plain row delete fails while any descendant inherits from the Session. `_detach_session` (`_store_mutations.py`) runs first:
1. Forget the FTS rows of all the Session's entries while their membership is still readable.
2. For the Session's own inherited ranges, collect superseded entries whose membership may hang on its lineage and forget them.
3. For each descendant segment that names this Session (`session_lineage_by_ancestor`): `copy_entries` gives the descendant its own copies of the entries that segment admits (same `seq` and id, new keys, current; side rows, Tool calls with payloads, and referenced Runs), deletes the segment, indexes the copies, and bumps the descendant's `history_revision` and `state_revision`. Copied Runs are `inherited = 1` with `contributes_to_activity = 0` and no `end_entry_key` until their copied `run_summary` sets it; running ones and unfinished calls end `interrupted`.
4. Delete the Session's own lineage rows and re-index the collected candidates.

Then the `sessions` row is deleted (entries, Runs, side rows, prompt pins, seen Skills, Continuation and owned relations cascade; direct forks' `fork_parent_key` becomes NULL, so their `fork_source` disappears), and prompt blobs no other Session pins are deleted. A descendant's cursor stays valid across the materialization (same seqs and ids), while its revision bump makes Recall and Statistics re-read it.

Materialization copies every live `entries` column, including additive columns unknown to this version, while replacing only the entry key, owning Session, Run reference and supersession state. Side-row and Run copies, including Run change paths, likewise preserve additive columns; deleting an ancestor must not erase newer fields from the descendant's history.
