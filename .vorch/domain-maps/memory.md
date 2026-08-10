# Memory

Pinned memory service and backend contracts for durable prompt-visible facts.

## Overview

`core/memory/` owns the product memory boundary for small, curated, prompt-visible entries. The current implementation is intentionally narrow: a `MemoryService` delegates to a file-backed pinned memory backend that manages tool-owned bullet entries in agent workspace files.

The memory system **owns its two workspace files** (`USER.md`, `MEMORY.md`): created lazily on the first tool write (a later remove-all leaves the file present but empty), and **never** seeded by the agent/workspace layer (which owns `SOUL.md` only). So a memory-off agent never gets them, and deleting them while memory is off does not resurrect them. Before a file physically exists, its scope still renders (the empty-scope placeholder — see Prompt Block), so behavior is seamless.

This domain is separate from Sessions. Sessions remain JSONL-canonical chat history; memory entries are durable guidance that should fit in the system prompt.

## Data Model

- `MemoryScope` currently supports:
  - `user` -> `USER.md`, stable user profile and preferences.
  - `agent` -> `MEMORY.md`, stable agent/workflow notes.
- `MemoryEntry`: `id`, `scope`, `content`. `id` is a 1-based positional int re-derived from file order on every read, not a stable identifier.
- The target Markdown file holds **only the entries** — one per line as a bare `- ` bullet, no preamble and no section heading.
- `MemoryPromptMode` controls prompt rendering:
  - `off` -> no prompt-visible pinned memory.
  - `agent` -> `MEMORY.md`.
  - `agent_user` -> `MEMORY.md` plus `USER.md` (default).
- The same mode controls the provider-visible `memory` tool: `off` removes it from the Agent's effective tool set, while `agent` and `agent_user` make it available regardless of the configurable tool allowlist.

## Interfaces

- `MemoryService(backend=None)`
  - `list_entries(workspace, scope) -> list[MemoryEntry]`
  - `add_entry(workspace, scope, content) -> MemoryEntry`
  - `replace_entry(workspace, scope, entry_id, content) -> MemoryEntry`
  - `remove_entry(workspace, scope, entry_id) -> MemoryEntry`
  - `read_prompt_files(workspace, mode) -> str` — the rendered pinned-memory entries only: each selected scope under its heading label (`# Agent Memory` / `# User Profile`) followed by its `- ` bullet entries, or the empty-scope placeholder (`No entries yet.`) when it has none — no `<memory>` wrapper, no guidance, `""` for mode `off`. This is the **data half** of the memory block; the guidance and the `<memory>` wrapper live in the block declaration (see Prompt Block).
- `Runtime.memory` exposes the same `MemoryService` instance used by the provider-visible Tool so accessors and Tools share one backend boundary.
- `memory.list` / `memory.add` / `memory.replace` / `memory.remove` are Identity-Agent RPCs. They resolve the target Agent's Workspace server-side, always return both `agent` and `user` scope projections after a successful read or mutation, and deliberately do not gate CRUD on `memory_prompt_mode`; that mode controls prompt/tool availability only. Mutations publish `resource_changed(kind="memories", scope:{agent_id})` without shipping Memory content.
- `FilePinnedMemoryBackend` implements the same operations against workspace Markdown files.
- `memory_block_definition() -> BlockDefinition` returns the `memory:guidance` System Prompt block the memory domain declares (see Prompt Block). The prompts package is imported lazily inside it so the memory domain carries no import-time dependency on prompts (which depends back on memory through the tool-availability seam).
- `read_memory_files(workspace, mode, *, provider) -> str` is the module-level renderer the `{generated:memory_files}` producer wraps (a thin delegate to `provider.read_prompt_files`), kept in the memory domain so the producer the prompt manager registers stays a thin closure.
- `memory_prompt_file_paths(workspace, mode) -> list[Path]` returns the resolved absolute paths of the mode's pinned-memory files that **exist on disk** (mode order; `off` → `[]`). The `memory_files` producer reports these to the prompt read-observer so the chat loop stamps them read-before-write (`prompts.md`, `file_state.md`). A not-yet-created file (its scope rendered via the empty-scope placeholder) is deliberately omitted — nothing on disk to stamp, and a later `write` to it is a new-file write anyway. The caller must not pass an empty-string workspace (a config agent) — it would resolve against `Path(".")`; config agents are `off` mode and the producer skips them regardless.
- `validate_memory_prompt_mode(mode) -> MemoryPromptMode` is exported for `core/agents/` to validate the Agent field; it raises `MemoryError` on an unknown mode.
- `MemoryError` reports expected validation or file I/O failures.

## Prompt Block

Pinned memory contributes to the System Prompt as the declared **`memory:guidance` block** (owner `memory`, a static editable `text` block) — the memory domain ships its own block instead of the prompts domain hardcoding a `{memory}` placeholder. Its default text is the guidance prose wrapped in `<memory>…</memory>` with an embedded `{generated:memory_files}` marker, so it is **one sortable unit** in the block layout.

- **Owner `memory`** drives gate 2: the block renders whenever the memory tool is enabled for the agent (`memory_prompt_mode != off`), resolved by the prompt manager's owner-active seam.
- **The empty-memory suppression is fixed.** Because the guidance is the block's *own* non-empty default text and the gate is "memory tool enabled" (not "memory files non-empty"), the guidance now appears whenever `memory_prompt_mode != off` — including before the first entry, when the agent needs it most. (Previously the old block rendered nothing when no file had content, so the guidance was absent exactly when it would help.)
- **The entries are the data half.** The `{generated:memory_files}` marker expands to `read_prompt_files(workspace, mode)` — each selected scope under its heading label with its `- ` bullet entries. When the block renders (memory tool on), every scope the mode selects is embedded; a scope with no entries (a **not-yet-created** or emptied file) renders the empty-scope placeholder (`No entries yet.`), so the framing is identical before and after the file physically exists — and reading never creates the file. Only `off` mode yields `""` (it selects no scope, and gate 2 drops the block anyway).
- The guidance text complements the memory tool's WHEN/SKIP description with the writing-quality half: what makes an entry worth its permanent prompt cost (it spares the user future steering), a light cue to save such facts as they surface rather than waiting to be asked (the always-on proactive nudge, since the tool description's WHEN is only read when the agent already eyes the tool), and the one non-obvious rule — write durable, **declarative** facts, not imperative self-instructions (which get re-read as standing directives in later sessions). As block default text it is now user-editable through the override cascade, no longer hardcoded as a prompt string. The old `MemoryService.build_prompt_block(workspace, mode)` (which returned guidance + `<memory>` wrapper + files together) is **gone** — that composition now lives split across the block declaration (guidance + wrapper) and `read_prompt_files`/`read_memory_files` (entries).

## Storage Contract

- `USER.md` and `MEMORY.md` live in the Agent workspace.
- Rooting never changes this storage boundary: Memory always reads and writes Workspace even when relative file/shell Tools use a selected Project repository.
- The file holds **only the entries**: one per line as a bare `- ` bullet, no preamble and no section heading. Anything else (freeform prose, a hand-written heading) is not an entry — it is ignored on read and dropped on the next write. The memory tool fully owns the file's content.
- A missing file is created on first write; the last `remove` leaves the file present but empty (which reads back as no entries). This first-write creation is the **only** way these files come into being — nothing seeds them ahead of time.
- `read_prompt_files(workspace, mode)` renders each selected scope under its heading label (`# Agent Memory` / `# User Profile`) followed by its `- ` bullet entries, joined with a blank line; `agent_user` orders the agent scope before the user scope. A scope with no entries (a missing or emptied file) renders the empty-scope placeholder `No entries yet.` (never omitted, never created); only `off` (no scope selected) returns `""`. This is **only** the entries — the `<memory>` wrapper and the guidance live in the `memory:guidance` block declaration (see Prompt Block), and the marker that injects these entries is `{generated:memory_files}`.
- Writes use a same-directory temp file plus atomic replace.
- Entry content is normalized to single-line whitespace and capped at 2,000 characters per entry.
- Each scope has a per-scope total budget over the sum of its entry contents (`agent`/`MEMORY.md` = 4,000 chars, `user`/`USER.md` = 3,000 chars), bounding how much pinned memory is injected into every prompt. An `add` or `replace` that pushes a scope past its budget is rejected with a `MemoryError` ("Memory '<scope>' scope is full (X/Y characters)…"), which the tool surfaces to the model as a failure so it removes or shortens an entry first. A non-increasing change (a shrinking `replace`, or any `remove`) is always allowed even when already over budget, so the model can always dig out.
- Duplicate `add` returns the existing entry instead of writing another copy (and is never budget-rejected, since it does not grow the store).

## Cross-Domain Rules

- `core/tools/memory.py` owns the provider-visible tool contract and delegates all storage behavior to `MemoryService`. It also owns the per-run thrash guard (see Constraints & Gotchas) — tool-UX state that never touches the storage layer, so `MemoryService` stays a pure facade.
- `server/rpc/memory_methods.py` owns Accessor CRUD validation and Agent-to-Workspace resolution. The WebUI edits structured entries through this boundary rather than treating `USER.md` or `MEMORY.md` as freeform text.
- `core/tools/availability.py` derives `memory` tool availability from `memory_prompt_mode`; Agent `allowed_tools` stores only independently configurable tools and must not carry `memory` as a separate toggle.
- `core/prompts/` collects the `memory:guidance` block (via `memory_block_definition()`) into the System Prompt's block list and registers a `memory_files` producer that calls `read_memory_files(...)`; the block (guidance + embedded files) renders in layout order, gated on `memory_prompt_mode != off`. Other workspace files may still be included through `{include:...}`.
- `core/agents/` seeds **only** `SOUL.md` for new workspaces (`WORKSPACE_TEMPLATE_FILES`). `USER.md`/`MEMORY.md` are the memory system's, not the agent/workspace layer's: they are created lazily on the first tool write (see Storage Contract), never seeded. There is no `USER.md`/`MEMORY.md` template under `resources/workspace-templates/` — the memory backend writes them as a bare `- ` bullet list on first write, with no preamble.
- Sessions and recall search are separate. Do not store chat transcripts or broad search indexes in this domain. SQLite FTS Session recall lives in `core/recall/` as a derived index.

## Constraints & Gotchas

- Entry IDs are ephemeral positions, not stable keys: a `remove` shifts every higher ID down by one. The tool returns the full `entries` list after every mutation so the model can re-read current IDs before the next `replace`/`remove`; do not reuse an ID across mutations.
- The memory block shows exactly the tool's entries: the file holds only `- ` bullets and the renderer injects those (under a scope heading), not the raw file. There is **no origin tracking** — a `- ` bullet typed into the file by hand is a real entry too, indistinguishable from a tool-added one. A non-bullet line is not an entry: not prompt-visible, and dropped on the next mutation.
- Entry normalization: all whitespace (newlines and carriage returns included) collapses to single spaces, so every entry is exactly one line. An entry that starts with `-` round-trips because the `- ` bullet prefix (dash + space) is stripped exactly once on read; there is no `\-` escaping, and literal `\-` in an entry is preserved verbatim.
- Only `- ` bullet lines survive a tool mutation; any non-bullet line hand-written into the file is dropped on the next `add`/`replace`/`remove`. The whole file is tool-managed — there is no freeform zone.
- Thrash guard against a memory-write loop: a failed mutation (`add`/`replace`/`remove`) is returned `retryable` so the model can consolidate and retry, but after `_MAX_MEMORY_FAILURES_PER_RUN` (3) **consecutive** failed mutations in one run the tool returns a terminal, non-`retryable` "stop retrying — answer the user" failure instead. This exists so a failed memory side effect can never loop a turn to budget exhaustion and starve the user's reply. The streak resets on the first successful mutation; `list` never counts. The counter (`_MemoryThrashTracker`, keyed by `run_id`, bounded and lock-guarded) lives in the tool layer, so a direct `memory_handler`/`MemoryService` call with no tracker (tests, non-runtime callers) keeps the old always-recoverable behavior.

## Future Backend Boundary

The file backend is the first implementation, not a permanent storage decision — keep new code behind `MemoryService` so a later pinned-memory backend registry (sibling to recall's) can replace it. SQLite FTS belongs behind a recall backend/index contract, not inside this pinned-memory domain.
