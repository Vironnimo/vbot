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
- Persisted `temperature` and `thinking_effort` may be `null` in `agent.json` to mean "no explicit per-agent override". `AgentStore` applies `settings.json` `defaults.agent` values at read time for `get()`, `list()`, and returned create/update results; raw disk state remains unresolved.
- `workspace` defaults to `<data_dir>/workspace-<id>/` and is stored as an absolute path.
  Public WebUI/RPC create keeps workspace server-assigned, while public
  `agent.update` may change workspace. Updating workspace normalizes the new
  path to an absolute path and seeds missing workspace template files there.
- `current_session_id` stores the agent's active Session. Every new Agent gets
  an initial empty Session immediately; configs without this field are
  normalized to a valid Session when loaded.
- `allowed_tools` and `allowed_skills` default to `['*']`.

## Interfaces

- `core/agents/__init__.py` exports `Agent` and store/error types. System Prompt assembly lives in `core/prompts/`.
- `AgentStore(data_dir, template_dir=None, defaults_provider=None)` — CRUD store rooted at a data directory, with optional read-time agent-default injection.
- `create(agent_id, name, **fields) -> Agent` — persists `agent.json`, creates
  `sessions/`, creates the first Session, sets `current_session_id`, and seeds
  workspace files. Returned value is the effective resolved Agent; persisted
  raw unset fields stay unset on disk.
- `get(agent_id) -> Agent` — returns the effective Agent with `defaults.agent`
  applied.
- `list() -> list[Agent]` — returns effective Agents with `defaults.agent`
  applied.
- Agent reads pass `agent.json` through `core/settings/validation.py` before
  constructing `Agent` objects. Malformed JSON or schema errors raise
  `AgentError` with file/path diagnostics instead of being normalized later.
- `update(agent_id, **changes) -> Agent` — updates mutable fields only; `id` is immutable. Raw values are written unchanged and the returned Agent is resolved after write.
- `delete(agent_id) -> Path` — moves active data under `<data_dir>/archive/<agent-id>/`.
## Conventions

- Agent IDs must be conservative filesystem-safe slugs: letters, numbers, hyphen, underscore, max 64 characters.
- Writes to `agent.json` use a same-directory temp file plus atomic replace.
- Workspace templates are `SOUL.md` and `USER.md` in `resources/workspace-templates/`. `SOUL.md` is seeded with the Hermes-style default identity wording adapted to a vBot agent. `USER.md` is seeded as a user-profile starter for durable user facts such as preferences, communication style, expectations, and workflow habits.
- Prompt bodies are file-backed and assembled through `core/prompts/`, not hardcoded in Agent code.

## Constraints & Gotchas

- Agent deletion currently replaces an existing archive for the same ID.
- Workspace seeding does not overwrite existing workspace files.
- The server rejects deleting the last remaining Agent; core `AgentStore.delete()`
  remains a filesystem archive operation and does not enforce product-level
  minimum-one-Agent behavior itself.
- Agent mutable fields are validated server/core-side. `thinking_effort` is one
  of `null`, `""`, `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or
  `max`; `temperature` may also be `null`. `null` means "inherit
  defaults.agent" while `""` for `thinking_effort` means "provider default".
  `allowed_tools` and `allowed_skills` must be lists of strings.
- Automatic fallback is run-local only. It does not mutate persisted `model` or `fallback_model`.
- The optional `::<connection-local-id>` suffix stores only the provider-local connection slug. Runtime reconstructs the full connection ID as `<provider>:<connection-local-id>` from the model prefix.
