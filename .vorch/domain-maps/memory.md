# Memory

Pinned memory service and backend contracts for durable prompt-visible facts.

## Overview

`core/memory/` owns small, curated, prompt-visible entries as Tool-managed bullets in Identity Agent Workspaces and Project Anchors. It is separate from Sessions (canonical SQLite history) and from Recall (derived search indexes).

The domain owns `USER.md` and `MEMORY.md` in an Identity Agent's Workspace, plus the shared `MEMORY.md` under `<data-dir>/projects/<id>/` for scope `project`. Project Memory belongs to the registered Project, never its repository or an individual Agent. Files are created lazily on the first write, never seeded by the Agent/Project layer, and a later remove-all leaves the file present but empty; reads and disabled Memory never recreate them.

## Modes and gating

`MemoryPromptMode` selects personal Memory: `off`, `agent` (`MEMORY.md`), or `agent_user` (both; default). Project Memory is independent: the Agent's `project_memory_enabled` and the Project's `memory_enabled` must both permit it. A Workspace-less Project or temporary Agent can therefore receive only Project Memory. Enabled Memory activates the `memory` Tool unless its Tool Access Policy denies it or is `none`; Tool denial does not hide prompt Memory, allowing read-only use. `validate_memory_prompt_mode` validates the personal selector.

## Storage contract

- Files hold **only** `- ` bullet entries, one per line - no preamble, no headings, no freeform zone. Reads accept leading indentation before the bullet so hand-edited entries survive; the next mutation normalizes them back to unindented bullets. Non-bullet lines are not entries: invisible in prompts, dropped on next mutation. The whole file is tool-managed.
- Entry ids are 1-based positions re-derived on read, not stable keys - removal shifts higher ids down. Only the RPC surface addresses entries by id; the `memory` Tool addresses them by text (`match_memory_entries`, `find_matches`, `replace_matching`, `remove_matching`, matched under the file lock; contract in `tools/memory.md`).
- Content normalizes to single-line whitespace capped at 2,000 chars/entry; a leading-dash entry round-trips because the `- ` prefix strips exactly once (no escaping).
- Per-scope budgets bound prompt injection: Agent Memory 4,000 chars, User Profile and Project Memory 3,000 each. A growing mutation past budget rejects with `MemoryBudgetError` (a `MemoryError` carrying scope, resulting total and budget); shrinking changes always pass so the model can dig out. Only a hand edit exceeds a budget; the prompt then shows the scope cut to it (Prompt block). Duplicate adds return the existing entry without rejection. A text-addressed replace whose new text equals another entry folds into it instead of duplicating.
- Writes use temp-file atomic replace with LF line endings on every platform. A Working Project never relocates personal Memory: user/agent scopes use the Workspace; project scope uses its Project Anchor. The file backend and its process-wide per-path locks are shared by both ownership kinds.
- Files are UTF-8 and reads are capped at 128 KiB, matching the ceiling for other prompt files. A file that cannot be read, is not UTF-8 or exceeds that ceiling raises `MemoryError` on every read and mutation of that scope and is never rewritten; the `memory` Tool reports it as `memory_error`. An oversized file stays available through file Tools or an editor for inspection and shortening. Read and write failure messages name the file with `/` separators (`model_path`, PROJECT.md -> Model-facing paths) and give the OS reason without its native path (`tests/core/memory/test_memory.py`).

## History

`core/memory/_history.py` keeps one append-only revision log per owner: `<data_dir>/agents/<id>/memory-history.jsonl` for personal Memory and `<data_dir>/projects/<id>/memory-history.jsonl` for shared Project Memory. Each moves with its owner's archive; personal history also follows Agent rename. Project revisions retain the actual writing Agent separately from their Project owner. An edit made outside the service is recorded as `external` first; history failure never fails the mutation. Mutation results name the recorded revision, and revert takes back named revisions all or none, refusing when a later revision changed the same entry. Format, revert rules and RPCs: `memory/history.md`.

## Prompt block

Pinned memory contributes the declared `memory:guidance` block (owner `memory`, static editable text): guidance prose wrapped in `<memory>...</memory>` with an embedded `{generated:memory_files}` marker - one sortable layout unit owned by this domain rather than a prompts-domain placeholder.

- Gate 2 opens for enabled personal Memory in an Identity Workspace or enabled Memory for the admitted Working Project, independent of Tool denial and before the first entry exists. Shared and personal entries occupy the same existing Memory block.
- Chat pins the combined rendering with the personal mode and effective Memory Project id. A mode, Project or Project Memory permission change replaces only that snapshot on the next Run; unchanged-selection file edits remain pinned until successful Compaction (`core/prompts/pinned_context.py`, `tests/core/chat/test_chat_prompt.py`).
- The marker expands to rendered entries only: each selected scope under its heading label with `(used/limit chars used)`, counted as the sum of entry content lengths against the mutation budget, then its bullets or an explicit `No entries yet.` placeholder for missing/empty scopes (zero usage and identical framing before/after creation; reading never creates files), `""` when no selected scope renders. Each bullet JSON-quotes the stored entry through Chat's `quote_external_value`, including escaped angle brackets, so entry text cannot close the Memory wrapper; the Tool's lists use the same encoding. Storage and budget counts keep the original text. Guidance/wrapper live in the declaration; `read_prompt_files` renders personal entries and `read_prompt_scopes` renders Project entries, preceded by their JSON-quoted Project id.
- The mutation budget is also the prompt limit, because only a hand edit exceeds it; the 128 KiB file cap of other prompt files (`prompts/prompt-files.md`) would let a scope 30 times its budget into every request. A scope above its budget shows the entries that fit in file order (an entry that does not fit is skipped, so one runaway entry cannot hide the rest), then `_PROMPT_CUT_NOTICE` counting the left-out entries and characters; the label keeps the true total, so the scope reads as over budget. The notice names the `memory` Tool's `list` only when the prompts domain reports the Agent can call it (`memory_tool`); a denied Tool gets the plain notice. Each cut file warns once without entry text and logs INFO when it fits again; the Tool's `list` (`render_scopes`) stays whole (`test_prompt_cuts_a_scope_above_its_budget_and_counts_what_it_left_out`).
- The guidance text carries the writing-quality half: durable user preferences versus environment/project facts, declarative facts rather than standing self-instructions, and that the shown entries may be older than the stored ones (how the Tool addresses entries is its description's job). Equivalent facts need no write; procedures belong in Skills, and unavailable Memory is not a reason to copy facts there. This guidance also appears for read-only Memory, so Tool use is conditional on availability. Model decision evaluation is documented in `automation.md` under Reflection.

## Interfaces

`memory.py` keeps the public service and prompt entry points. `_definitions.py` owns the scope/budget, result/error and prompt-text declarations; `_file_backend.py` owns file reads and writes, matching, history/revert and bounded scope rendering for both owner kinds.

- `MemoryService` (id-addressed list/add/replace/remove, text-addressed find_matches/replace_matching/remove_matching, the history methods (`memory/history.md`), scope_usage, render_scopes, read_prompt_files) delegates to the file backend; `render_scopes` renders the Tool's `list` and error listings whole, and `read_prompt_files(workspace, mode, *, memory_tool)` renders the same format for the prompt, cut to the budgets; `Runtime.memory` exposes the same instance the Tool uses. Mutations take a `MemoryWriter` (Agent id, `actor`, and the Session, Run and Run kind of a Tool change) for the log line and the history. Each mutation that changes a file logs one INFO line at the backend, under the file lock: Agent, scope, `actor` (`rpc` from the RPCs, `tool` from the `memory` Tool, `internal` by default), resulting entry count and character delta - never entry text; a duplicate add or unchanged replace logs nothing (`tests/core/memory/test_memory.py`). `read_memory_files(workspace, mode, *, provider, memory_tool)` is the thin module-level renderer the prompts producer wraps; The renderer's optional `on_read` observer reports each wholly shown file with metadata captured before its byte read; Chat records that stamp without rereading the file. Missing and cut files report no read, so an Agent cannot overwrite omitted entries or a concurrent replacement as if it had seen them. The block definition imports prompts lazily to avoid an import cycle.
- `read_prompt_scopes(path, scopes, *, memory_tool, on_read)` renders selected scopes with the same budget and read-stamp rules, so callers can compose personal and Project roots without reading a Project file through a personal Workspace. `MemoryService(project_history_root=...)` reuses the file backend with a Project-only history scope set; `MemoryWriter.project_id` selects its history owner while `agent_id` still names the writer.
- Every `memory.*` RPC accepts exactly one owner, `agent_id` or `project_id`. Personal CRUD returns both personal scopes; Project CRUD returns only `project`, and a mutation cannot address a scope owned by the other kind. History operations use the same owner (`memory/history.md`). Accessor operations deliberately ignore Memory activation and Tool policy, so disabled Memory remains editable. Mutations publish content-free `resource_changed(kind="memories")` with that owner. The Agent reference lock spans resolution, worker file work and publication; admitted cancellation settles before releasing it. Project lookup and file work additionally hold `ProjectStore.configuration_guard`, preventing an archive from detaching the Anchor. Agent resolution uses `AgentStore.get_async`; all Memory file work uses the `memory-rpc` worker pool (`tests/server/rpc/test_memory_methods.py`).

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
