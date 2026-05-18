# Agents

Persisted agent configuration and workspace lifecycle management.

## Overview

`core/agents/` owns `agent.json` CRUD under `<data_dir>/agents/<agent-id>/`. Creating an agent also creates its sessions directory and seeds a workspace from bundled templates. Deleting an agent archives its active agent directory and workspace instead of permanently deleting them.

## Data Model

`Agent` fields match the current schema: `id`, `name`, `model`,
`fallback_model`, `workspace`, `current_session_id`, `temperature`,
`thinking_effort`, `allowed_tools`, `allowed_skills`, `created_at`,
`updated_at`.

- `id` is immutable and used as the filesystem directory name.
- `model` is user-facing `<provider>/<model-id>` and may optionally carry a pinned local connection suffix as `<provider>/<model-id>::<connection-local-id>`. It may be empty until chat time.
- `fallback_model` is an optional secondary `<provider>/<model-id>` with the same optional `::<connection-local-id>` suffix support. It is used when a retryable provider error escapes the primary adapter's built-in retries during a Run. Once activated, it stays active only for the rest of that Run; the next turn starts from `model` again.
- `workspace` defaults to `<data_dir>/workspace-<id>/` and is stored as an absolute path.
  Public WebUI/RPC create/update does not accept workspace mutation in Phase 4;
  this avoids archiving arbitrary user paths if an Agent is later deleted.
- `current_session_id` stores the agent's active Session. Every new Agent gets
  an initial empty Session immediately; legacy configs without this field are
  normalized to a valid Session when loaded.
- `allowed_tools` and `allowed_skills` default to `['*']`.

## Interfaces

- `core/agents/__init__.py` exports `Agent`, store/error types, `SystemPromptManager`, and prompt protocol types.
- `AgentStore(data_dir, template_dir=None)` — CRUD store rooted at a data directory.
- `create(agent_id, name, **fields) -> Agent` — persists `agent.json`, creates
  `sessions/`, creates the first Session, sets `current_session_id`, and seeds
  workspace files.
- `get(agent_id) -> Agent`
- `list() -> list[Agent]`
- `update(agent_id, **changes) -> Agent` — updates mutable fields only; `id` is immutable.
- `delete(agent_id) -> Path` — moves active data under `<data_dir>/archive/<agent-id>/`.
- `SystemPromptManager(storage, tool_registry, skill_registry, app_version, app_dir, data_root, ...)`
  - `build_system_prompt(agent) -> str` — expands `{app_version}`, `{runtime}`, `{tools}`, `{skills}`, and `{include:<filename>}`.
  - `provider_tool_definitions(agent) -> list[dict]` — returns allowed provider tool schemas.

## Conventions

- Agent IDs must be conservative filesystem-safe slugs: letters, numbers, hyphen, underscore, max 64 characters.
- Writes to `agent.json` use a same-directory temp file plus atomic replace.
- Workspace templates are `SOUL.md`, `IDENTITY.md`, `AGENTS.md`, and `USER.md` in `resources/workspace-templates/`.
- Prompt bodies are file-backed through `StorageManager.read_prompt_fragment()`, not hardcoded in code.
- Workspace includes accept any safe flat filename (no path separators, not absolute). The `{include:filename}` directive wraps the resolved content as `<file name="{filename}">\n{content}\n</file>` in the built prompt.

## Constraints & Gotchas

- Agent deletion currently replaces an existing archive for the same ID.
- Workspace seeding does not overwrite existing workspace files.
- The server rejects deleting the last remaining Agent; core `AgentStore.delete()`
  remains a filesystem archive operation and does not enforce product-level
  minimum-one-Agent behavior itself.
- Agent mutable fields are validated server/core-side. `thinking_effort` is one
  of `""`, `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`;
  `allowed_tools` and `allowed_skills` must be lists of strings.
- Automatic fallback is run-local only. It does not mutate persisted `model` or `fallback_model`.
- The optional `::<connection-local-id>` suffix stores only the provider-local connection slug. Runtime reconstructs the full connection ID as `<provider>:<connection-local-id>` from the model prefix.
- Skill prompt XML escapes metadata values before insertion.
