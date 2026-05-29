# Memory

Pinned memory service and backend contracts for durable prompt-visible facts.

## Overview

`core/memory/` owns the product memory boundary for small, curated, prompt-visible entries. The current implementation is intentionally narrow: a `MemoryService` delegates to a file-backed pinned memory backend that manages tool-owned bullet entries in agent workspace files.

This domain is separate from Sessions. Sessions remain JSONL-canonical chat history; memory entries are durable guidance that should fit in the system prompt.

## Data Model

- `MemoryScope` currently supports:
  - `user` -> `USER.md`, stable user profile and preferences.
  - `agent` -> `MEMORY.md`, stable agent/workflow notes.
- `MemoryEntry`: `id`, `scope`, `content`.
- Tool-managed entries live under a `## Entries` section in the target Markdown file.

## Interfaces

- `MemoryService(backend=None)`
  - `list_entries(workspace, scope) -> list[MemoryEntry]`
  - `add_entry(workspace, scope, content) -> MemoryEntry`
  - `replace_entry(workspace, scope, entry_id, content) -> MemoryEntry`
  - `remove_entry(workspace, scope, entry_id) -> MemoryEntry`
- `FilePinnedMemoryBackend` implements the same operations against workspace Markdown files.
- `MemoryError` reports expected validation or file I/O failures.

## Storage Contract

- `USER.md` and `MEMORY.md` live in the Agent workspace.
- Existing freeform Markdown before `## Entries` is preserved.
- Optional Markdown after a later `## ...` heading is preserved.
- The memory backend only edits bullet entries inside `## Entries`.
- Missing `MEMORY.md` is created on first write.
- Writes use a same-directory temp file plus atomic replace.
- Entry content is normalized to single-line whitespace and capped at 2,000 characters.
- Duplicate `add` returns the existing entry instead of writing another copy.

## Cross-Domain Rules

- `core/tools/memory.py` owns the provider-visible tool contract and delegates all storage behavior to `MemoryService`.
- `core/prompts/` includes workspace files through `{include:...}`; memory code does not assemble system prompts.
- `core/agents/` seeds `MEMORY.md` for new workspaces through the workspace-template mechanism.
- Sessions and recall search are separate. Do not store chat transcripts or broad search indexes in this domain. SQLite FTS Session recall lives in `core/recall/` as a derived index.

## Future Backend Boundary

The current file backend is the first implementation, not a permanent storage decision. Recall now has a first-class backend registry; future work should add the sibling pinned-memory registry so first-party and extension-provided systems can plug into the same interface.

SQLite FTS belongs behind a recall backend/index contract, not inside this pinned-memory domain.
