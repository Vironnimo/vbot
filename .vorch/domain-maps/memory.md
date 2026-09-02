# Memory

Pinned memory service and backend contracts for durable prompt-visible facts.

## Overview

`core/memory/` owns small, curated, prompt-visible entries as Tool-managed bullets in Agent workspace files - intentionally narrow. It is separate from Sessions (canonical SQLite history) and from Recall (derived search indexes).

The domain **owns its two workspace files**: `USER.md` (user profile/preferences) and `MEMORY.md` (agent/workflow notes). They are created lazily on the first tool write, never seeded by the agent/workspace layer (which seeds only `SOUL.md`), and a later remove-all leaves the file present but empty - so a memory-off agent never gets them and deletion while off does not resurrect them.

## Modes and gating

`MemoryPromptMode`: `off` (no prompt-visible memory), `agent` (`MEMORY.md`), `agent_user` (both; default). The same mode gates activation of the provider-visible `memory` Tool: `off` keeps it inactive, `agent`/`agent_user` activate it unless Tool Access Policy denies it or is `none`. Mode alone controls prompt visibility, so an on mode plus a denial intentionally creates read-only Memory. `validate_memory_prompt_mode` is exported for `core/agents/` field validation.

## Storage contract

- Files hold **only** `- ` bullet entries, one per line - no preamble, no headings, no freeform zone. Reads accept leading indentation before the bullet so hand-edited entries survive; the next mutation normalizes them back to unindented bullets. Non-bullet lines are not entries: invisible in prompts, dropped on next mutation. The whole file is tool-managed.
- Entry ids are 1-based positional positions re-derived on read, not stable keys - removal shifts higher ids down. The tool returns the full list after every mutation so the model re-reads ids before the next operation.
- Content normalizes to single-line whitespace capped at 2,000 chars/entry; a leading-dash entry round-trips because the `- ` prefix strips exactly once (no escaping).
- Per-scope budgets bound prompt injection: MEMORY.md 4,000 chars, USER.md 3,000. A growing mutation past budget rejects with `MemoryError`; shrinking changes always pass so the model can dig out. Duplicate adds return the existing entry without rejection.
- Writes use temp-file atomic replace. Rooting never changes this boundary: Memory always uses Workspace even when file/shell Tools work in a Project repo.

## Prompt block

Pinned memory contributes the declared `memory:guidance` block (owner `memory`, static editable text): guidance prose wrapped in `<memory>...</memory>` with an embedded `{generated:memory_files}` marker - one sortable layout unit owned by this domain rather than a prompts-domain placeholder.

- Gate 2 renders it whenever mode != off, independent of Tool denial - including before the first entry exists (the block's own non-empty default text guarantees the guidance appears exactly when it helps).
- The marker expands to rendered entries only: each selected scope under its heading label with its bullets, an explicit `No entries yet.` placeholder for missing/empty scopes (identical framing before/after creation; reading never creates files), `""` only for `off`. Guidance/wrapper live in the declaration, entries come from `read_prompt_files`.
- The guidance text carries the writing-quality half: what justifies permanent prompt cost, a proactive save-as-you-go nudge, and the one non-obvious rule - write durable declarative facts, not imperative self-instructions that later sessions re-read as standing directives.

## Interfaces

- `MemoryService` (list/add/replace/remove/read_prompt_files) delegates to the file backend; `Runtime.memory` exposes the same instance the Tool uses. `read_memory_files(workspace, mode, *, provider)` is the thin module-level renderer the prompts producer wraps; `memory_prompt_file_paths(workspace, mode)` reports existing on-disk paths so Chat stamps them read-before-write (uncreated scopes deliberately omitted - nothing to stamp). The block definition imports prompts lazily to avoid an import cycle.
- `memory.list/add/replace/remove` are Identity-Agent RPCs resolving Workspace server-side, returning both scope projections after every operation. They deliberately ignore mode and Tool policy for CRUD - mode controls visibility/activation, policy controls callability. Mutations publish `resource_changed(kind="memories")` without content.

## Cross-Domain Rules

- `core/tools/memory.py` owns the provider-visible contract plus the per-run thrash guard (tool-UX state below); `MemoryService` stays a pure facade. Server RPC owns Accessor validation and Agent-to-Workspace resolution; the WebUI edits structured entries through RPC, never freeform files.
- Agents seed only SOUL.md - USER/MEMORY are this domain's, created lazily, no templates shipped.
- Do not store transcripts or broad indexes here: FTS Session recall lives behind `core/recall/`.

## Constraints & Gotchas

- No origin tracking: a hand-typed bullet is a real entry indistinguishable from tool-added ones.
- Thrash guard against memory-write loops: after 3 consecutive failed mutations in one Run the Tool returns a terminal "stop retrying - answer the user" failure instead of another retryable error, so a failing side effect can never loop a turn into budget exhaustion. Streak resets on first success; `list` never counts; the tracker lives in the Tool layer keyed by run id (direct service calls keep always-recoverable behavior).
- Keep new code behind `MemoryService` - the file backend is the first implementation, not a permanent decision; a later backend registry replaces it without touching callers.
