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
  - `build_prompt_block(workspace, mode) -> str`
- `FilePinnedMemoryBackend` implements the same operations against workspace Markdown files.
- `validate_memory_prompt_mode(mode) -> MemoryPromptMode` is exported for `core/agents/` to validate the Agent field; it raises `MemoryError` on an unknown mode.
- `MemoryError` reports expected validation or file I/O failures.

## Storage Contract

- `USER.md` and `MEMORY.md` live in the Agent workspace.
- Existing freeform Markdown before `## Entries` is preserved.
- Optional Markdown after a later `## ...` heading is preserved.
- The memory backend only edits bullet entries inside `## Entries`.
- A missing file is created on first write with a default preamble (`# Agent Memory` for `MEMORY.md`, `# User Profile` for `USER.md`); an empty `## Entries` section renders the placeholder line "No tool-managed memory entries are recorded yet."
- Prompt rendering embeds each selected file's *entire* raw content (preamble + `## Entries` + suffix) as one `<file name="...">...</file>` block, all wrapped in a single `<memory>...</memory>` block; `agent_user` orders `MEMORY.md` before `USER.md`. Missing selected files are omitted.
- Writes use a same-directory temp file plus atomic replace.
- Entry content is normalized to single-line whitespace and capped at 2,000 characters.
- Duplicate `add` returns the existing entry instead of writing another copy.

## Cross-Domain Rules

- `core/tools/memory.py` owns the provider-visible tool contract and delegates all storage behavior to `MemoryService`.
- `core/tools/availability.py` derives `memory` tool availability from `memory_prompt_mode`; Agent `allowed_tools` stores only independently configurable tools and must not carry `memory` as a separate toggle.
- `core/prompts/` expands the `{memory}` placeholder by asking the memory service to render the selected prompt block for the Agent's `memory_prompt_mode`. Other workspace files may still be included through `{include:...}`.
- `core/agents/` seeds `USER.md` and `MEMORY.md` (alongside `SOUL.md`) for new workspaces through the `WORKSPACE_TEMPLATE_FILES` mechanism.
- Sessions and recall search are separate. Do not store chat transcripts or broad search indexes in this domain. SQLite FTS Session recall lives in `core/recall/` as a derived index.

## Constraints & Gotchas

- Entry IDs are ephemeral positions, not stable keys: a `remove` shifts every higher ID down by one. The tool returns the full `entries` list after every mutation so the model can re-read current IDs before the next `replace`/`remove`; do not reuse an ID across mutations.
- `{memory}` shows more than the tool's entries: hand-written freeform text in `USER.md`/`MEMORY.md` is prompt-visible too, because rendering injects the whole file. The tool only curates the `## Entries` bullets.
- Entry escaping: newlines and carriage returns collapse to spaces, and a leading `-` is written as `\-` (unescaped on read) so an entry that starts with `-` round-trips instead of splitting into a new bullet.
- The `## Entries` heading match is case-insensitive; only the first following `## ` heading starts the preserved suffix.

## Future Backend Boundary

The file backend is the first implementation, not a permanent storage decision — keep new code behind `MemoryService` so a later pinned-memory backend registry (sibling to recall's) can replace it. SQLite FTS belongs behind a recall backend/index contract, not inside this pinned-memory domain.
