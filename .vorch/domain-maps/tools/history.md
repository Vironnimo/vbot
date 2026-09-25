# History Tool

Session-scoped access to the canonical conversation records hidden by Compaction, complete except for internal Message metadata.

## Overview

`core/tools/history.py` owns the built-in `history` Tool. It is bound to the current `ToolContext` Session, has no Agent/Project/Session addressing arguments, and becomes model-visible only when that Session already contains at least one persisted `compaction_checkpoint`. Its canonical Session read, snapshot construction, search, cursor validation, and page rendering run through the cancellation-safe Tool worker boundary. `_history_protocol.py` owns request validation and snapshot-bound cursor encoding/validation; the Tool file retains bounded reads, canonical filtering, search, and page rendering. It reads checkpoint metadata and bounded canonical record batches through `ChatSessionManager`; it does not load the complete active Session, use Recall indexes, or alter provider Context.

## Interface

- Input is one open flat object with required `action` `overview`, `search`, `read`, or `around` plus the shared optional-property superset. Parameter descriptions identify action-dependent requirements and omission behavior; the handler requires `query` for `search` and `message_id` for `around`. Known action wrappers and action/match/direction formatting are repaired by the Tool-owned argument normalizer before handler validation; cursor and Message ids remain exact.
- Fields the selected action does not use (`_history_protocol.py::_unused_field_notes`): `query` outside search and `message_id` outside around ask for another action's result and fail `invalid_arguments` with the corrected call as JSON (e.g. read with query names the search call with the same checkpoint/roles/limit); `direction` other than `start` outside read fails because order cannot be honored. Every other unused field runs the call and the page carries `note` (`Ignored checkpoint: not used by overview.`; before/after on search/read add the around hint; limit on around says around returns before + after + 1 records).
- A continuation is the same `action` plus its opaque `cursor`. Fields the action uses may be repeated beside it when they equal the cursor's values; a different value fails `invalid_arguments` naming the cursor's value and the `{"action", "cursor"}` call; a new `limit` (search/read/overview) sets the next page size and is carried by the next cursor (`_continued_request`).
- Default roles are `user`, `assistant`, and `error`. Callers may opt into other supported canonical roles, including Tool messages and checkpoints; `run_summary` annotations are not content records.
- The first call freezes a snapshot at the latest checkpoint present at that moment. Results are divided into fixed checkpoint sections, so later Session appends cannot shift an existing cursor's view.
- Success data carries frozen snapshot/checkpoint identity, selected roles, section records, truncation state, an opaque continuation cursor when more content remains, and `note` for ignored fields. Records are the stored Message dictionaries without `reasoning_meta`, `reasoning_scope`, `reasoning_timing`, `usage`, `timing` and `tool_display` (provider replay data, accounting and presentation; `_INTERNAL_MESSAGE_FIELDS`). An Assistant turn whose only reasoning is opaque metadata still counts as a record, as the store's record filter does, and returns without it. Search snippets are deterministic and at most 320 characters including ellipses. The Tool row derives its presentation-only `results` count from the returned `items`; `has_more: true` renders that page count as a lower bound, and failures publish no count.
- Every complete success envelope is capped at 51,200 UTF-8 bytes. When one record cannot fit whole, the Tool returns Unicode-safe segments of its returned form and continues within that record before advancing.
- Cursors are compact, versioned, base64url-encoded JSON with an integrity digest. Frozen identity and continuation use Session generation plus internal checkpoint/record sequence, never public Message id alone, because duplicate checkpoint ids are valid. They are validated against the current Session id, action, generation, frozen snapshot, scope, and continuation position; malformed, cross-action, cross-Session, or fork-reused cursors fail as invalid arguments.

## Canonical Filtering

- Prior `history` calls and results are excluded so the Tool cannot recursively retrieve its own output. A mixed Assistant carrier keeps unrelated text and Tool calls while removing only the `history` call portion.
- Canonical ordering and content are preserved. Matching uses Unicode case-folding and whitespace compaction for deterministic literal search; a record split into segments reassembles exactly into its returned form.
- `read` and `around` issue bounded SQL reads, `overview` obtains per-section count/bookends through SQL aggregates, and exact search scans canonical records in fixed 128-record batches until it can fill the requested page plus one lookahead. A no-match exact search may still scan the frozen range, but never materializes that range as one Python transcript.
- The Tool emits only safe presentation metadata and logs request/result metadata rather than message bodies.

## Cross-Domain Contracts

- Chat derives the `history` Session grant from persisted checkpoint presence and uses the same loaded Session snapshot to build provider definitions, System Prompt ownership gates, effective Context, and dispatch configuration.
- A newly appended checkpoint carries model-facing guidance that History is available. If Compaction occurs after a Tool batch, the next provider request in the same Run advertises `history`; if it occurs after a final Assistant response, availability begins on the next Run.
- Moving or taking over a Session preserves its id and transcript, so History and existing cursors remain valid in the destination scope. A fork inherits the source's checkpoints through lineage (its current view) under a fresh Session id and generation, so History is available there but source cursors are invalid.
- `session_search` remains the cross-Session Recall Tool and may use derived indexes. `history` is current-Session-only, checkpoint-gated, canonical, and exact.

## Constraints & Gotchas

- Existing checkpoints activate History without being rewritten. Only newly created checkpoints receive the guidance text.
- Failed Compaction appends no checkpoint and therefore cannot activate History.
- Agent policies and Project Tool Whitelists never independently select `history`; the ephemeral Session Grant is its only activation source. `tool.list` still exposes the Tool as automatic so configuration surfaces can explain and explicitly deny it; an absolute Tool-policy denial wins over a current grant, and a Run restriction can still deny execution after advertisement.
