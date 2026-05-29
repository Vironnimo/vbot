# Memory System Plan

Status legend: `[ ]` not started, `[~]` in progress, `[x]` completed.

## Goals

- Keep the first implementation simple: `USER.md` and `MEMORY.md` are the pinned-memory heart.
- Keep session transcripts JSONL-canonical for now.
- Improve recall before adding semantic memory: `session_search` should become more useful on current JSONL data.
- Put stable interfaces in core so optional backends can be swapped by config or registered by extensions later.
- Treat SQLite FTS as an optional recall backend/index, not as the canonical session store.

## Architecture Shape

```text
System prompt
  <- pinned memory snapshot from workspace files

memory tool
  -> MemoryService
      -> PinnedMemoryBackend
          -> FilePinnedMemoryBackend (USER.md, MEMORY.md)

session_search tool
  -> RecallBackend
      -> JsonlSessionRecallBackend now
      -> optional SqliteFtsRecallBackend later

extensions later
  -> register_memory_backend(...)
  -> register_recall_backend(...)
  -> observe memory writes / run ends / tool results
```

## Phase Checklist

- [x] Phase 0: Write this plan and choose the MVP slice.
- [x] Phase 1: Add pinned memory service and `memory` tool for `USER.md` / `MEMORY.md`.
- [x] Phase 2: Include `MEMORY.md` in new agent workspaces and system prompt assembly.
- [x] Phase 3: Improve JSONL `session_search` with anchored windows and bookends.
- [x] Phase 4: Update specs/docs for memory, prompts, tools, sessions, agent, runtime.
- [x] Phase 5: Run focused and full backend quality gates.
- [x] Phase 6: Review diff and commit the completed logical unit.

## Phase 1 Details: Pinned Memory Service And Tool

Add `core/memory/` with a small, stable boundary:

- `MemoryScope`: `user` and `agent` for now.
- `MemoryEntry`: `id`, `scope`, `content`.
- `MemoryService`: list/add/replace/remove entries for an agent workspace.
- `FilePinnedMemoryBackend`: stores entries in Markdown files inside the agent workspace.

File mapping:

| Scope | File | Prompt role |
|---|---|---|
| `user` | `USER.md` | stable user profile and preferences |
| `agent` | `MEMORY.md` | stable agent/workflow notes |

Storage rule:

- Preserve any existing freeform file prose.
- Manage tool-owned entries under a `## Entries` section.
- Add/replace/remove only bullet entries in that section.
- Create `MEMORY.md` when missing.

Tool shape:

```text
memory(action="list|add|replace|remove", scope="user|agent", ...)
```

Arguments:

- `action`: required.
- `scope`: required.
- `content`: required for `add`; optional new content for `replace`.
- `entry_id`: 1-based entry id for `replace` and `remove`.

## Phase 2 Details: Prompt Integration

- Add `MEMORY.md` to workspace templates.
- Add `{include:MEMORY.md}` to the default system prompt.
- Existing workspaces without `MEMORY.md` should keep working because workspace includes already skip missing files with a warning.
- Update prompt tests so both `USER.md` and `MEMORY.md` are included.

## Phase 3 Details: JSONL Recall Improvements

Keep the existing `session_search` tool but add better result shapes:

- `around_message_id`: when supplied with `session_id`, return an anchored view around that message.
- `bookends`: number of start/end session messages to include for orientation.
- Query matches should include:
  - existing `context.before` / `context.after` for compatibility;
  - ordered `window` around the match;
  - `bookend_start` and `bookend_end` when requested.

The initial backend stays JSONL scanning through `ChatSessionManager`. The later `RecallBackend` extraction should preserve the public tool contract.

## Future Backend Work

After the MVP is stable:

- Extract `RecallBackend` from `session_search` internals.
- Add `JsonlSessionRecallBackend` as the default implementation.
- Add optional `SqliteFtsRecallBackend` as a derived index over JSONL.
- Extend `HooksAPI` or runtime extension loading with official backend registration calls.
- Allow extension-provided memory/recall backends without using tool-hook interception as the primary integration path.

## Non-Goals For This Pass

- Do not migrate sessions from JSONL to SQLite.
- Do not build the SQLite FTS backend yet.
- Do not add semantic/vector search yet.
- Do not add automatic memory extraction yet.
- Do not add WebUI memory management yet.