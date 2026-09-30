# Memory

Pinned memory service and backend contracts for durable prompt-visible facts.

## Overview

`core/memory/` owns small, curated, prompt-visible entries as Tool-managed bullets in Agent workspace files - intentionally narrow. It is separate from Sessions (canonical SQLite history) and from Recall (derived search indexes).

The domain **owns its two workspace files**: `USER.md` (user profile/preferences) and `MEMORY.md` (agent/workflow notes). They are created lazily on the first tool write, never seeded by the agent/workspace layer (which seeds only `SOUL.md`), and a later remove-all leaves the file present but empty - so a memory-off agent never gets them and deletion while off does not resurrect them.

## Modes and gating

`MemoryPromptMode`: `off` (no prompt-visible memory), `agent` (`MEMORY.md`), `agent_user` (both; default). The same mode gates activation of the provider-visible `memory` Tool: `off` keeps it inactive, `agent`/`agent_user` activate it unless Tool Access Policy denies it or is `none`. Mode alone controls prompt visibility, so an on mode plus a denial intentionally creates read-only Memory. `validate_memory_prompt_mode` is exported for `core/agents/` field validation.

## Storage contract

- Files hold **only** `- ` bullet entries, one per line - no preamble, no headings, no freeform zone. Reads accept leading indentation before the bullet so hand-edited entries survive; the next mutation normalizes them back to unindented bullets. Non-bullet lines are not entries: invisible in prompts, dropped on next mutation. The whole file is tool-managed.
- Entry ids are 1-based positions re-derived on read, not stable keys - removal shifts higher ids down. Only the RPC surface addresses entries by id; the `memory` Tool addresses them by text (`match_memory_entries`, `find_matches`, `replace_matching`, `remove_matching`, matched under the file lock; contract in `tools/memory.md`).
- Content normalizes to single-line whitespace capped at 2,000 chars/entry; a leading-dash entry round-trips because the `- ` prefix strips exactly once (no escaping).
- Per-scope budgets bound prompt injection: MEMORY.md 4,000 chars, USER.md 3,000. A growing mutation past budget rejects with `MemoryBudgetError` (a `MemoryError` carrying scope, resulting total and budget); shrinking changes always pass so the model can dig out. Duplicate adds return the existing entry without rejection. A text-addressed replace whose new text equals another entry folds into it instead of duplicating.
- Writes use temp-file atomic replace with LF line endings on every platform. Rooting never changes this boundary: Memory always uses Workspace even when file/shell Tools work in a Project repo.
- Files are UTF-8. A file that cannot be read or is not UTF-8 raises `MemoryError` on every read and mutation of that scope and is never rewritten; the `memory` Tool reports it as `memory_error` (`tests/core/memory/test_memory.py`).

## History

`core/memory/_history.py` keeps one append-only JSON Lines file per Identity Agent, `<data_dir>/agents/<id>/memory-history.jsonl` - beside the Workspace, outside it, so file Tools do not see it and it moves with rename and archive (`agent.md`). The runtime passes `history_root=layout.agents`; a `MemoryService()` without it records nothing. An id that is not path-safe or has no Agent directory has no history, and its mutations still work.

- Each line is one revision of one scope with a sequential id: `v`, `id`, `at` (UTC), `scope`, `kind`, `actor`, `state` (hash of the resulting entries), `changes` (`added`/`removed`/`replaced` with `text`, `index` and `previous`), optional `session_id`/`run_id`, `reverts`. Kinds: `edit` (a service mutation), `revert`, `baseline` (entries found when a scope's history starts) and `external` (the file changed outside the service). `baseline` and `external` also carry the complete `entries`; replay applies `changes` in between. Unreadable lines (not UTF-8, not JSON, a missing or mistyped field, a torn last line) are skipped with a warning. An append after a last line without its newline writes a newline first, so a torn fragment stays its own skipped line and the new revision keeps its id and writer after a restart.
- Every backend mutation with a writer that names an Agent first compares the file with the last recorded state and records a difference as `external`, dated by the file's modification time, then records its own change. Lock order: scope file lock, then history lock; multi-scope operations take both file locks in `MEMORY_SCOPES` order. A history failure never fails the mutation: a `MemoryError` logs a warning, any other exception an error with traceback, and the unrecorded change is noticed as `external` later.
- Mutation results name the revision that recorded them (`MemoryEntry.revision`, `MemoryTextChange.revision`; `None` without a history, for a listed entry or an unchanged result), which the `memory` Tool shows the user (`tools/memory.md`).
- `history`, `entries_at` and `compare` sync both scopes first, so they include edits made since the last Memory operation. `revert(revision_ids)` takes back the named revisions newest first against the current entries, all or none: a removed entry returns at its old position, a replaced entry gets its previous text, an added entry is removed, and an effect that is already gone needs nothing. When a later revision changed the same entry again it raises `MemoryRevertError` naming that revision and writes nothing. The budget applies to growing reverts; the `baseline` revision cannot be reverted; a revert is itself a revertible revision (`tests/core/memory/test_memory_history.py`).

## Prompt block

Pinned memory contributes the declared `memory:guidance` block (owner `memory`, static editable text): guidance prose wrapped in `<memory>...</memory>` with an embedded `{generated:memory_files}` marker - one sortable layout unit owned by this domain rather than a prompts-domain placeholder.

- Gate 2 requires a non-empty Identity Workspace and renders it whenever mode != off, independent of Tool denial - including before the first entry exists (the block's own non-empty default text guarantees the guidance appears exactly when it helps).
- Chat pins rendered entries and scope usage with the selected mode. A mode change replaces only the Memory snapshot on the next Run; unchanged-mode file edits remain pinned until successful Compaction (`core/prompts/pinned_context.py`, `tests/core/chat/test_chat_prompt.py`).
- The marker expands to rendered entries only: each selected scope under its heading label with `(used/limit chars used)`, counted as the sum of entry content lengths against the mutation budget, then its bullets or an explicit `No entries yet.` placeholder for missing/empty scopes (zero usage and identical framing before/after creation; reading never creates files), `""` only for `off`. Guidance/wrapper live in the declaration, entries come from `read_prompt_files`.
- The guidance text carries the writing-quality half: durable user preferences versus environment/project facts, declarative facts rather than standing self-instructions, and that the shown entries may be older than the stored ones (how the Tool addresses entries is its description's job). Equivalent facts need no write; procedures belong in Skills, and unavailable Memory is not a reason to copy facts there. This guidance also appears for read-only Memory, so Tool use is conditional on availability. Model decision evaluation is documented in `automation.md` under Reflection.

## Interfaces

- `MemoryService` (id-addressed list/add/replace/remove, text-addressed find_matches/replace_matching/remove_matching, history/entries_at/compare/revert, scope_usage, render_scopes, read_prompt_files) delegates to the file backend; `render_scopes` is the one renderer for both the prompt block and the Tool's `list`; `Runtime.memory` exposes the same instance the Tool uses. Mutations take a `MemoryWriter` (Agent id, `actor`, and the Session and Run of a Tool change) for the log line and the history. Each mutation that changes a file logs one INFO line at the backend, under the file lock: Agent, scope, `actor` (`rpc` from the RPCs, `tool` from the `memory` Tool, `internal` by default), resulting entry count and character delta - never entry text; a duplicate add or unchanged replace logs nothing (`tests/core/memory/test_memory.py`). `read_memory_files(workspace, mode, *, provider)` is the thin module-level renderer the prompts producer wraps; `memory_prompt_file_paths(workspace, mode)` reports existing on-disk paths so Chat stamps them read-before-write (uncreated scopes deliberately omitted - nothing to stamp). The block definition imports prompts lazily to avoid an import cycle.
- `memory.list/add/replace/remove` are Identity-Agent RPCs resolving Workspace server-side, returning both scope projections after every operation. `memory.history` (newest first, optional `scope` and `limit`, plus `total`), `memory.show` (entries after a `revision`), `memory.diff` (`from`, optional `to`, else current) and `memory.revert` (`revisions`, returning the projections and the recorded revisions) expose the history; revert is a guarded mutation and publishes only when it changed something. They deliberately ignore mode and Tool policy for CRUD - mode controls visibility/activation, policy controls callability. Mutations publish `resource_changed(kind="memories")` without content. The handlers hold the Agent reference lock from Workspace resolution through file work, preventing rename/relocation from detaching that path. Mutations also settle publication before releasing the lock on caller cancellation; cancellation while waiting starts no write. They resolve the Agent with `AgentStore.get_async` and do all memory file work on the `memory-rpc` worker pool, never on the Event Loop; the invalidation is published from the loop after the write returns (`tests/server/rpc/test_memory_methods.py`).

## Cross-Domain Rules

- `core/tools/memory.py` owns the provider-visible contract plus the per-run thrash guard (tool-UX state below); `MemoryService` stays a pure facade. Server RPC owns Accessor validation and Agent-to-Workspace resolution; the WebUI edits structured entries through RPC, never freeform files.
- Agents seed only SOUL.md - USER/MEMORY are this domain's, created lazily, no templates shipped.
- Do not store transcripts or broad indexes here: FTS Session recall lives behind `core/recall/`.

## Constraints & Gotchas

- No origin tracking on entries: a hand-typed bullet is a real entry indistinguishable from tool-added ones. Only the history records who made a change, and a hand edit shows there as `external`.
- Thrash guard against memory-write loops: after 3 consecutive failed mutations in one Run the Tool returns a terminal "stop retrying - answer the user" failure instead of another corrective error, so a failing side effect can never loop a turn into budget exhaustion. Streak resets on first success; `list` and argument refusals never count; the tracker lives in the Tool layer keyed by run id (direct service calls keep always-recoverable behavior).
- Keep new code behind `MemoryService` - the file backend is the first implementation, not a permanent decision; a later backend registry replaces it without touching callers.
