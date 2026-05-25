# Prompts Domain Extraction Plan

## Goal

Extract System Prompt assembly and editable prompt-fragment domain rules into `core/prompts/` while preserving behavior.

## Scope

In:
- Create `core/prompts/` for System Prompt assembly, prompt-related Protocols, editable fragment order, variables, and fragment operations.
- Move `SystemPromptManager` and prompt-format helpers out of `core/agents/agents.py`.
- Move server-owned prompt fragment metadata (`PROMPT_FRAGMENT_VARIABLES` and editable order) out of `server/delegates.py`.
- Keep `StorageManager` as the owner of raw prompt fragment file I/O and bundled-resource fallback.
- Update runtime/server/tests to import Prompt APIs from `core.prompts`.
- Add `.vorch/specs/prompts.md` and update neighboring docs.

Out:
- No WebUI redesign or editor behavior change.
- No prompt fragment content changes.
- No change to `settings.json`, resource paths, or `StorageManager` atomic write mechanics.
- No changes to compaction prompt behavior beyond leaving `compaction.md` as storage-readable but not UI-editable.
- No new prompt variables or RPC methods.

## Hidden Constraints

- The System Prompt UI edits exactly five fragments in order: `system.md`, `runtime.md`, `tools.md`, `channels.md`, `skills.md`; `compaction.md` is allowlisted for backend reads but not part of the editor surface.
- User prompt copies in `<data_dir>/prompts/` override bundled resources and must be preserved unless explicitly reset.
- Workspace includes use `{include:filename}`, accept only safe flat filenames, and wrap content as `<file name="filename">...`.
- Tool and skill prompt sections must obey each Agent's allowlists.
- Provider tool definitions include the internal `skill` tool only when the Agent has loadable skills.
- Prompt preview uses the runtime's active `SystemPromptManager` and returns token estimates through the server.
- Server RPC error codes must remain stable: malformed prompt requests map to `invalid_request`; storage/runtime failures map through the normal expected-error path.

## Risks

- Moving `SystemPromptManager` can accidentally create import cycles between Agents, Prompts, Runtime, and Channels.
- Prompt fragment metadata is consumed by WebUI tests; changing names, order, or variable descriptions can break visible behavior.
- `core/agents/__init__.py` currently re-exports prompt types; callers must be updated to use `core.prompts` directly.

## Done When

- [x] `core/prompts/` exists and exports System Prompt assembly plus editable fragment operations.
- [x] `core/agents/agents.py` no longer owns System Prompt assembly code.
- [x] `server/delegates.py` no longer owns prompt fragment metadata or prompt-specific fragment operations.
- [x] Existing prompt RPC, storage, runtime, and WebUI behavior is covered by passing tests.
- [x] `.vorch/specs/prompts.md` exists and `.vorch/PROJECT.md` references it.
- [x] Relevant backend/frontend quality gates pass.
- [x] WebUI smoke test includes System Prompt UI and a real agent Run with tool calls.
- [x] Changes are committed.
