# History Tool

Session-scoped, lossless access to canonical messages hidden by Compaction.

## Overview

`core/tools/history.py` owns the built-in `history` Tool. It is bound to the current `ToolContext` Session, has no Agent/Project/Session addressing arguments, and becomes model-visible only when that Session already contains at least one persisted `compaction_checkpoint`. Its canonical Session read, snapshot construction, search, cursor validation, and page rendering run through the cancellation-safe Tool worker boundary. It reads checkpoint metadata and bounded canonical record batches through `ChatSessionManager`; it does not load the complete active Session, use Recall indexes, or alter provider Context.

## Interface

- Input is one open flat object with required `action` `overview`, `search`, `read`, or `around` plus the shared optional-property superset. Parameter descriptions identify action-dependent requirements and omission behavior; the handler requires `query` for `search`, `message_id` for `around`, accepts only fields applicable to the selected action, and permits a continuation only as the same `action` plus its opaque `cursor`. Retired nested `request.operation` and operation-key shapes are rejected.
- Default roles are `user`, `assistant`, and `error`. Callers may opt into other supported canonical roles, including Tool messages and checkpoints; `run_summary` annotations are not content records.
- The first call freezes a snapshot at the latest checkpoint present at that moment. Results are divided into fixed checkpoint sections, so later Session appends cannot shift an existing cursor's view.
- Success data carries frozen snapshot/checkpoint identity, selected roles, section records, truncation state, and an opaque continuation cursor when more content remains. Search snippets are deterministic and at most 320 characters including ellipses. The Tool row derives its presentation-only `results` count from the returned `items`; `has_more: true` renders that page count as a lower bound, and failures publish no count.
- Every complete success envelope is capped at 51,200 UTF-8 bytes. When one record cannot fit whole, the Tool returns lossless Unicode-safe segments and continues within that record before advancing.
- Cursors are compact, versioned, base64url-encoded JSON with an integrity digest. Frozen identity and continuation use Session generation plus internal checkpoint/record sequence, never public Message id alone, because duplicate checkpoint ids are valid. They are validated against the current Session id, action, generation, frozen snapshot, scope, and continuation position; malformed, cross-action, cross-Session, or fork-reused cursors fail as invalid arguments.

## Canonical Filtering

- Prior `history` calls and results are excluded so the Tool cannot recursively retrieve its own output. A mixed Assistant carrier keeps unrelated text and Tool calls while removing only the `history` call portion.
- Canonical ordering and content are preserved. Matching uses Unicode case-folding and whitespace compaction for deterministic literal search; returned read segments remain lossless.
- `read` and `around` issue bounded SQL reads, `overview` obtains per-section count/bookends through SQL aggregates, and exact search scans canonical records in fixed 128-record batches until it can fill the requested page plus one lookahead. A no-match exact search may still scan the frozen range, but never materializes that range as one Python transcript.
- The Tool emits only safe presentation metadata and logs request/result metadata rather than message bodies.

## Cross-Domain Contracts

- Chat derives the `history` Session grant from persisted checkpoint presence and uses the same loaded Session snapshot to build provider definitions, System Prompt ownership gates, effective Context, and dispatch configuration.
- A newly appended checkpoint carries model-facing guidance that History is available. If Compaction occurs after a Tool batch, the next provider request in the same Run advertises `history`; if it occurs after a final Assistant response, availability begins on the next Run.
- Moving or taking over a Session preserves its id and transcript, so History and existing cursors remain valid in the destination scope. Forking copies checkpoints into a fresh Session id, so History is available there but source cursors are invalid.
- `session_search` remains the cross-Session Recall Tool and may use derived indexes. `history` is current-Session-only, checkpoint-gated, canonical, and exact.

## Constraints & Gotchas

- Existing checkpoints activate History without being rewritten. Only newly created checkpoints receive the guidance text.
- Failed Compaction appends no checkpoint and therefore cannot activate History.
- Agent policies and Project Tool Whitelists never independently select `history`; the ephemeral Session Grant is its only activation source. `tool.list` still exposes the Tool as automatic so configuration surfaces can explain and explicitly deny it; an absolute Tool-policy denial wins over a current grant, and a Run restriction can still deny execution after advertisement.
