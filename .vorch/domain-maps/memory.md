# Memory

Pinned memory service and backend contracts for durable prompt-visible facts.

## Overview

`core/memory/` owns the product memory boundary for small, curated, prompt-visible entries. The current implementation is intentionally narrow: a `MemoryService` delegates to a file-backed pinned memory backend that manages tool-owned bullet entries in agent workspace files.

This domain is separate from Sessions. Sessions remain JSONL-canonical chat history; memory entries are durable guidance that should fit in the system prompt.

## Data Model

- `MemoryScope` currently supports:
  - `user` -> `USER.md`, stable user profile and preferences.
  - `agent` -> `MEMORY.md`, stable agent/workflow notes.
- `MemoryEntry`: `id`, `scope`, `content`. `id` is a 1-based positional int re-derived from file order on every read, not a stable identifier.
- Tool-managed entries live under a `## Entries` section in the target Markdown file.
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
  - `read_prompt_files(workspace, mode) -> str` — the `<file>`-wrapped pinned-memory file contents only (no `<memory>` wrapper, no guidance), `""` when empty/absent or mode `off`. This is the **data half** of the memory block; the guidance and the wrapper moved into the block declaration (see Prompt Block).
- `FilePinnedMemoryBackend` implements the same operations against workspace Markdown files.
- `memory_block_definition() -> BlockDefinition` returns the `memory:guidance` System Prompt block the memory domain declares (see Prompt Block). The prompts package is imported lazily inside it so the memory domain carries no import-time dependency on prompts (which depends back on memory through the tool-availability seam).
- `read_memory_files(workspace, mode, *, provider) -> str` is the module-level renderer the `{generated:memory_files}` producer wraps (a thin delegate to `provider.read_prompt_files`), kept in the memory domain so the producer the prompt manager registers stays a thin closure.
- `validate_memory_prompt_mode(mode) -> MemoryPromptMode` is exported for `core/agents/` to validate the Agent field; it raises `MemoryError` on an unknown mode.
- `MemoryError` reports expected validation or file I/O failures.

## Prompt Block

Pinned memory contributes to the System Prompt as the declared **`memory:guidance` block** (owner `memory`, a static editable `text` block) — the memory domain ships its own block instead of the prompts domain hardcoding a `{memory}` placeholder. Its default text is the guidance prose wrapped in `<memory>…</memory>` with an embedded `{generated:memory_files}` marker, so it is **one sortable unit** in the block layout.

- **Owner `memory`** drives gate 2: the block renders whenever the memory tool is enabled for the agent (`memory_prompt_mode != off`), resolved by the prompt manager's owner-active seam.
- **The empty-memory suppression is fixed.** Because the guidance is the block's *own* non-empty default text and the gate is "memory tool enabled" (not "memory files non-empty"), the guidance now appears whenever `memory_prompt_mode != off` — including before the first entry, when the agent needs it most. (Previously the old block rendered nothing when no file had content, so the guidance was absent exactly when it would help.)
- **The file contents are the data half.** The `{generated:memory_files}` marker expands to `read_prompt_files(workspace, mode)` — the `<file>`-wrapped file contents only. The marker renders `""` when files are empty/absent, and block normalization trims the gap, so an empty memory still shows the guidance inside a clean `<memory>` wrapper.
- The guidance text complements the memory tool's WHEN/SKIP description with the one non-obvious writing-quality rule: write durable, **declarative** facts, not imperative self-instructions (which get re-read as standing directives in later sessions). As block default text it is now user-editable through the override cascade, no longer hardcoded as a prompt string. The old `MemoryService.build_prompt_block(workspace, mode)` (which returned guidance + `<memory>` wrapper + files together) is **gone** — that composition now lives split across the block declaration (guidance + wrapper) and `read_prompt_files`/`read_memory_files` (files).

## Storage Contract

- `USER.md` and `MEMORY.md` live in the Agent workspace.
- Existing freeform Markdown before `## Entries` is preserved.
- Optional Markdown after a later `## ...` heading is preserved.
- The memory backend only edits bullet entries inside `## Entries`.
- A missing file is created on first write with a default preamble (`# Agent Memory` for `MEMORY.md`, `# User Profile` for `USER.md`); an empty `## Entries` section renders the placeholder line "No tool-managed memory entries are recorded yet."
- `read_prompt_files(workspace, mode)` embeds each selected file's *entire* raw content (preamble + `## Entries` + suffix) as one `<file name="...">...</file>` block, joined with a blank line; `agent_user` orders `MEMORY.md` before `USER.md`. Missing selected files are omitted, and an empty/absent selection returns `""`. This is **only** the file contents — the `<memory>` wrapper and the guidance live in the `memory:guidance` block declaration (see Prompt Block), and the marker that injects these files is `{generated:memory_files}`.
- Writes use a same-directory temp file plus atomic replace.
- Entry content is normalized to single-line whitespace and capped at 2,000 characters per entry.
- Each scope has a per-scope total budget over the sum of its tool-managed entry contents (`agent`/`MEMORY.md` = 4,000 chars, `user`/`USER.md` = 3,000 chars), bounding how much pinned memory is injected into every prompt. An `add` or `replace` that pushes a scope past its budget is rejected with a `MemoryError` ("Memory '<scope>' scope is full (X/Y characters)…"), which the tool surfaces to the model as a failure so it removes or shortens an entry first. The budget counts only tool-managed entries, not the freeform preamble/suffix (which the tool cannot edit). A non-increasing change (a shrinking `replace`, or any `remove`) is always allowed even when already over budget, so the model can always dig out.
- Duplicate `add` returns the existing entry instead of writing another copy (and is never budget-rejected, since it does not grow the store).

## Cross-Domain Rules

- `core/tools/memory.py` owns the provider-visible tool contract and delegates all storage behavior to `MemoryService`.
- `core/tools/availability.py` derives `memory` tool availability from `memory_prompt_mode`; Agent `allowed_tools` stores only independently configurable tools and must not carry `memory` as a separate toggle.
- `core/prompts/` collects the `memory:guidance` block (via `memory_block_definition()`) into the System Prompt's block list and registers a `memory_files` producer that calls `read_memory_files(...)`; the block (guidance + embedded files) renders in layout order, gated on `memory_prompt_mode != off`. Other workspace files may still be included through `{include:...}`.
- `core/agents/` seeds `USER.md` and `MEMORY.md` (alongside `SOUL.md`) for new workspaces through the `WORKSPACE_TEMPLATE_FILES` mechanism.
- Sessions and recall search are separate. Do not store chat transcripts or broad search indexes in this domain. SQLite FTS Session recall lives in `core/recall/` as a derived index.

## Constraints & Gotchas

- Entry IDs are ephemeral positions, not stable keys: a `remove` shifts every higher ID down by one. The tool returns the full `entries` list after every mutation so the model can re-read current IDs before the next `replace`/`remove`; do not reuse an ID across mutations.
- The memory block shows more than the tool's entries: hand-written freeform text in `USER.md`/`MEMORY.md` is prompt-visible too, because `read_prompt_files` injects the whole file. The tool only curates the `## Entries` bullets.
- Entry normalization: all whitespace (newlines and carriage returns included) collapses to single spaces, so every entry is exactly one line. An entry that starts with `-` round-trips because the `- ` bullet prefix (dash + space) is stripped exactly once on read; there is no `\-` escaping, and literal `\-` in an entry is preserved verbatim.
- Only `- ` bullet lines inside `## Entries` survive a tool mutation; any other prose hand-written into that section is dropped on the next `add`/`replace`/`remove`. Keep durable freeform notes outside `## Entries` (the section is fully tool-managed).
- The `## Entries` heading match is case-insensitive; only the first following `## ` heading starts the preserved suffix.

## Future Backend Boundary

The file backend is the first implementation, not a permanent storage decision — keep new code behind `MemoryService` so a later pinned-memory backend registry (sibling to recall's) can replace it. SQLite FTS belongs behind a recall backend/index contract, not inside this pinned-memory domain.
