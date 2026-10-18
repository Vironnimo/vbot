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
- Per-scope budgets bound prompt injection: MEMORY.md 4,000 chars, USER.md 3,000. A growing mutation past budget rejects with `MemoryBudgetError` (a `MemoryError` carrying scope, resulting total and budget); shrinking changes always pass so the model can dig out. Only a hand edit exceeds a budget; the prompt then shows the scope cut to it (Prompt block). Duplicate adds return the existing entry without rejection. A text-addressed replace whose new text equals another entry folds into it instead of duplicating.
- Writes use temp-file atomic replace with LF line endings on every platform. A Working Project never changes this boundary: Memory always uses Workspace even when file/shell Tools work in a Project repo.
- Files are UTF-8 and reads are capped at 128 KiB, matching the ceiling for other prompt files. A file that cannot be read, is not UTF-8 or exceeds that ceiling raises `MemoryError` on every read and mutation of that scope and is never rewritten; the `memory` Tool reports it as `memory_error`. An oversized file stays available through file Tools or an editor for inspection and shortening. Read and write failure messages name the file with `/` separators (`model_path`, PROJECT.md -> Model-facing paths) and give the OS reason without its native path (`tests/core/memory/test_memory.py`).

## History

`core/memory/_history.py` keeps one append-only revision log per Identity Agent, `<data_dir>/agents/<id>/memory-history.jsonl`, beside the Workspace and outside it, so file Tools do not see it and it moves with rename and archive (`agent.md`). Every service mutation with a writer that names an Agent records its change, and an edit made outside the service is recorded as `external` first. A history failure never fails the mutation. Mutation results name the revision that recorded them, which the `memory` Tool shows the user. Revert takes back named revisions all or none and refuses when a later revision changed the same entry. Format, revert rules and RPCs: `memory/history.md`.

## Prompt block

Pinned memory contributes the declared `memory:guidance` block (owner `memory`, static editable text): guidance prose wrapped in `<memory>...</memory>` with an embedded `{generated:memory_files}` marker - one sortable layout unit owned by this domain rather than a prompts-domain placeholder.

- Gate 2 requires a non-empty Identity Workspace and renders it whenever mode != off, independent of Tool denial - including before the first entry exists (the block's own non-empty default text guarantees the guidance appears exactly when it helps).
- Chat pins rendered entries and scope usage with the selected mode. A mode change replaces only the Memory snapshot on the next Run; unchanged-mode file edits remain pinned until successful Compaction (`core/prompts/pinned_context.py`, `tests/core/chat/test_chat_prompt.py`).
- The marker expands to rendered entries only: each selected scope under its heading label with `(used/limit chars used)`, counted as the sum of entry content lengths against the mutation budget, then its bullets or an explicit `No entries yet.` placeholder for missing/empty scopes (zero usage and identical framing before/after creation; reading never creates files), `""` only for `off`. Guidance/wrapper live in the declaration, entries come from `read_prompt_files`.
- The mutation budget is also the prompt limit, because only a hand edit exceeds it; the 128 KiB file cap of other prompt files (`prompts/prompt-files.md`) would let a scope 30 times its budget into every request. A scope above its budget shows the entries that fit in file order (an entry that does not fit is skipped, so one runaway entry cannot hide the rest), then `_PROMPT_CUT_NOTICE` counting the left-out entries and characters; the label keeps the true total, so the scope reads as over budget. The notice names the `memory` Tool's `list` only when the prompts domain reports the Agent can call it (`memory_tool`); a denied Tool gets the plain notice. Each cut file warns once without entry text and logs INFO when it fits again; the Tool's `list` (`render_scopes`) stays whole (`test_prompt_cuts_a_scope_above_its_budget_and_counts_what_it_left_out`).
- The guidance text carries the writing-quality half: durable user preferences versus environment/project facts, declarative facts rather than standing self-instructions, and that the shown entries may be older than the stored ones (how the Tool addresses entries is its description's job). Equivalent facts need no write; procedures belong in Skills, and unavailable Memory is not a reason to copy facts there. This guidance also appears for read-only Memory, so Tool use is conditional on availability. Model decision evaluation is documented in `automation.md` under Reflection.

## Interfaces

`memory.py` keeps the public service and prompt entry points. `_definitions.py` owns the scope/budget, result/error and prompt-text declarations; `_file_backend.py` owns Workspace file reads and writes, matching, history/revert and bounded scope rendering.

- `MemoryService` (id-addressed list/add/replace/remove, text-addressed find_matches/replace_matching/remove_matching, the history methods (`memory/history.md`), scope_usage, render_scopes, read_prompt_files) delegates to the file backend; `render_scopes` renders the Tool's `list` and error listings whole, and `read_prompt_files(workspace, mode, *, memory_tool)` renders the same format for the prompt, cut to the budgets; `Runtime.memory` exposes the same instance the Tool uses. Mutations take a `MemoryWriter` (Agent id, `actor`, and the Session, Run and Run kind of a Tool change) for the log line and the history. Each mutation that changes a file logs one INFO line at the backend, under the file lock: Agent, scope, `actor` (`rpc` from the RPCs, `tool` from the `memory` Tool, `internal` by default), resulting entry count and character delta - never entry text; a duplicate add or unchanged replace logs nothing (`tests/core/memory/test_memory.py`). `read_memory_files(workspace, mode, *, provider, memory_tool)` is the thin module-level renderer the prompts producer wraps; `memory_prompt_file_paths(workspace, mode)` reports existing on-disk paths within their budget so Chat stamps them read-before-write (uncreated scopes deliberately omitted - nothing to stamp; a cut file is omitted so the Agent cannot overwrite entries it never saw). The block definition imports prompts lazily to avoid an import cycle.
- `memory.list/add/replace/remove` are Identity-Agent RPCs resolving Workspace server-side, returning both scope projections after every operation. `memory.history`, `memory.show`, `memory.diff` and `memory.revert` expose the history (`memory/history.md`). They deliberately ignore mode and Tool policy for CRUD - mode controls visibility/activation, policy controls callability. Mutations publish `resource_changed(kind="memories")` without content. The handlers hold the Agent reference lock from Workspace resolution through file work, preventing rename/relocation from detaching that path. Mutations also settle publication before releasing the lock on caller cancellation; cancellation while waiting starts no write. They resolve the Agent with `AgentStore.get_async` and do all memory file work on the `memory-rpc` worker pool, never on the Event Loop; the invalidation is published from the loop after the write returns (`tests/server/rpc/test_memory_methods.py`).

## Cross-Domain Rules

- `core/tools/memory.py` owns the provider-visible contract plus the per-run thrash guard (`tools/memory.md`); `MemoryService` stays a pure facade. Server RPC owns Accessor validation and Agent-to-Workspace resolution; the WebUI edits structured entries through RPC, never freeform files.
- Do not store transcripts or broad indexes here: FTS Session recall lives behind `core/recall/`.

## Constraints & Gotchas

- No origin tracking on entries: a hand-typed bullet is a real entry indistinguishable from tool-added ones. Only the history records who made a change, and a hand edit shows there as `external`.
- Keep new code behind `MemoryService` - the file backend is the first implementation, not a permanent decision; a later backend registry replaces it without touching callers.

## Agent-facing text

This domain owns the `memory:guidance` System Prompt block, its cut notice (`_PROMPT_CUT_NOTICE`, `_PROMPT_CUT_LIST_HINT`) and the Memory size-limit refusal. Before you change, add or delete a sentence of them, read `memory/agent-facing-text.md`: it records why each sentence exists. Update its row for every sentence you change and add one for every sentence you add.

## References

Read these only when your task matches - not by default.

- Changing `memory-history.jsonl`, its revision kinds, writers, revert, `check_revert` or `strict` revert, or `memory.history`/`show`/`diff`/`revert` -> `memory/history.md`
