# Agents

Persisted agent configuration and workspace lifecycle management.

## Overview

`core/agents/` owns `agent.json` CRUD under `<data_dir>/agents/<agent-id>/`. Creating an agent also creates its sessions directory and seeds a workspace from bundled templates. Deleting an agent archives its active agent directory and workspace instead of permanently deleting them.

## Data Model

`Agent` fields match the current schema: `id`, `name`, `model`, `fallback_model`, `workspace`, `current_session_id`, `temperature`, `thinking_effort`, `memory_prompt_mode`, `allowed_tools`, `allowed_skills`, `custom_system_prompt_enabled`, `created_at`, `updated_at`.

- `id` is immutable and used as the filesystem directory name.
- `model` is user-facing `<provider>/<model-id>` and may optionally carry a pinned local connection suffix as `<provider>/<model-id>::<connection-local-id>[:<account-id>]`. The persisted value may be empty; when empty it resolves to `defaults.agent.model` at read time if configured, without rewriting disk. If no default is configured, the effective value stays empty.
- `fallback_model` is an optional secondary `<provider>/<model-id>` with the same optional `::<connection-local-id>[:<account-id>]` suffix support. It is used when a retryable provider error escapes the primary adapter's built-in retries during a Run. Once activated, it stays active only for the rest of that Run; the next turn starts from `model` again.
- Persisted `model` and `fallback_model` may be empty, and persisted `temperature` and `thinking_effort` may be `null`, to mean "no explicit per-agent override". `AgentStore` applies `settings.json` `defaults.agent` values for those four fields at read time for `get()`, `list()`, and returned create/update results; raw disk state remains unresolved.
- `memory_prompt_mode` controls prompt-visible pinned memory for the Agent: `off` includes no pinned memory, `agent` includes only `MEMORY.md`, and `agent_user` includes both `MEMORY.md` and `USER.md`. The default is `agent_user`.
- `workspace` defaults to `<data_dir>/workspace-<id>/` and is stored as an absolute path. Public WebUI/RPC create keeps workspace server-assigned, while public `agent.update` may change workspace. Updating workspace normalizes the new path to an absolute path and seeds missing workspace template files there.
- `current_session_id` stores the agent's active Session. Every new Agent gets an initial empty Session immediately; configs without this field, with an empty value, or whose referenced Session no longer exists are normalized to a valid Session when loaded. That normalization creates a new Session and rewrites `agent.json` during the read.
- `allowed_tools` and `allowed_skills` default to `['*']`. `allowed_tools` stores only independently configurable tools; `memory` is runtime-derived from `memory_prompt_mode` and is stripped from persisted allowlists on create/update.
- `custom_system_prompt_enabled` is an optional persisted boolean. Missing or `false` means the Agent uses the default prompt scope; `true` means chat and preview may read `<data_dir>/agents/<agent-id>/prompts/` for editable system-prompt fragments. Disabling the toggle ignores Agent prompt files without deleting them.

## Interfaces

- `core/agents/__init__.py` exports `Agent` and store/error types. System Prompt assembly lives in `core/prompts/`.
- `AgentStore(data_dir, template_dir=None, defaults_provider=None)` — CRUD store rooted at a data directory, with optional read-time agent-default injection.
- `create(agent_id, name, **fields) -> Agent` — persists `agent.json`, creates `sessions/`, creates the first Session, sets `current_session_id`, and seeds workspace files. Returned value is the effective resolved Agent; persisted raw unset fields stay unset on disk.
- `get(agent_id) -> Agent` — returns the effective Agent with `defaults.agent` applied.
- `list() -> list[Agent]` — returns effective Agents with `defaults.agent` applied.
- Agent reads pass `agent.json` through `core/settings/validation.py` before constructing `Agent` objects. Malformed JSON or schema errors raise `AgentError` with file/path diagnostics instead of being normalized later. The load path validates **once**: `_agent_from_dict` trusts the validated mapping and only normalizes shapes (workspace fallback, `allowed_tools` sanitization, optional-field defaults).
- Field schema rules live in `core/settings` (single authority): the agent-id format is `is_valid_agent_id` / `AGENT_ID_PATTERN` (shared by `AgentStore`, file-schema validation, and prompt-fragment storage — no local copies); value rules are `validate_temperature` / `validate_thinking_effort` plus the constants `ALLOWED_THINKING_EFFORTS`, `MIN_TEMPERATURE`, `MAX_TEMPERATURE`. `AgentStore` create/update and the server's `agent.*` RPC param validation both delegate to the value validators, wrapping `SettingsValidationError` into `AgentError` / `invalid_request` respectively.
- `update(agent_id, **changes) -> Agent` — updates mutable fields only; `id` is immutable. Raw values are written unchanged and the returned Agent is resolved after write.
- `delete(agent_id) -> Path` — moves active data under `<data_dir>/archive/<agent-id>/`.

## Uniform Agent Resolution

Run paths no longer call `runtime.agents.get(...)` directly. They go through one seam, `AgentResolver.resolve_agent(project_id, agent_id) -> RuntimeAgent` (owned by `core/projects/`, see `projects.md`), so identity and project agents load through the same call. The fork is at exactly one place:

- `project_id is None` → the identity `AgentStore` (this domain), **byte-identical** to the old `agents.get`: the `defaults.agent` model→global→empty injection and the workspace are exactly as before.
- `project_id` set → a **config agent** synthesized from that project's team scan (no `agent.json`, no workspace, `memory_prompt_mode="off"`, `allowed_tools/skills=["*"]`).

`RuntimeAgent` is a `Protocol` that the store `Agent` already satisfies, so consumers read the same attribute surface (`id`, `model`, `fallback_model`, `workspace`, `temperature`, `thinking_effort`, `allowed_tools`, `allowed_skills`, `memory_prompt_mode`, …) regardless of source; a config agent is a `ConfigAgent` carrying that surface plus its verbatim prompt `body`.

**Two freshness levels:** team membership (which agents exist) comes from the **scan**, cached per project and refreshed at open / explicit re-scan — not per turn. A single agent's **config** (model/body) is read **fresh from the repo file on every resolve**, mirroring how identity agents re-read `agent.json` each turn.

**Model chain.** Identity agents keep model→global→empty (the store). Config agents resolve model→project-default→global→**error**: a model counts only if it is *configured in this instance* (provider registered, model in catalog, a usable credential); an unconfigured model falls through the chain and is surfaced as a `BAD_MODEL` scan-report finding at scan time, not at first run. If the chain falls all the way through, resolution raises `AgentResolutionError`. The run paths map that to a clean failure (chat → domain error at the RPC edge via `error_mapping.py`; `subagent` → `agent_not_found` tool failure; `status` → `agent_not_found`). The identity-CRUD and server-RPC paths (`agent_methods`, `chat_methods`, `channel_methods`, automation) deliberately stay on `AgentStore` until project RPC lands.

**Temperature & thinking chain.** For **config agents**, `temperature` and `thinking_effort` resolve through the *same* three-tier chain as the model: agent value → project default (`project.default_temperature` / `project.default_thinking_effort`) → global `defaults.agent` → provider default. The first non-`None` tier wins; falling through every tier yields `None`, so the field is dropped at the wire and the provider default applies. For thinking effort, `""` is a real value meaning "provider default" that **stops** the chain (a project `default_thinking_effort=""` blocks the global default), while `null`/absent falls through; for temperature, `0.0` is a real value (the floor) that stops the chain. The global tier is the live `defaults.agent` map (read once per resolve). **Identity agents are unchanged**: they keep the existing two-tier `defaults.agent` injection (agent value → global) applied by `AgentStore` at read time — the project tier is config-agent-only, exactly like the model chain.

## Conventions

- Agent IDs must be conservative filesystem-safe slugs: start with a letter or number, then use only letters, numbers, hyphen, or underscore, max 64 characters.
- Writes to `agent.json` use a same-directory temp file plus atomic replace.
- Workspace templates are `SOUL.md`, `USER.md`, and `MEMORY.md` in `resources/workspace-templates/`. `SOUL.md` is seeded with a generic vBot-agent identity/persona starter. `USER.md` is seeded as a user-profile starter for durable user facts such as preferences, communication style, expectations, and workflow habits. `MEMORY.md` is seeded as agent/workflow memory for concise durable notes managed by the `memory` tool.
- Prompt bodies are file-backed and assembled through `core/prompts/`, not hardcoded in Agent code.

## Constraints & Gotchas

- Agent deletion currently replaces an existing archive for the same ID.
- Workspace seeding does not overwrite existing workspace files.
- The server rejects deleting the last remaining Agent, an Agent with active or queued Runs (`agent_busy`), or an Agent referenced by Channels or Cron jobs (`agent_in_use`), and serializes those checks plus delete under the server Agent-reference `asyncio.Lock`; core `AgentStore.delete()` remains a filesystem archive operation and does not enforce product-level guards itself. Server RPC context lives in `server.md`.
- Agent mutable fields are validated server/core-side. `thinking_effort` is one of `null`, `""`, `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`; `temperature` may be `null` or a finite number from `0.0` through `2.0`. `null` means "inherit defaults.agent" while `""` for `thinking_effort` means "provider default". `allowed_tools` and `allowed_skills` must be lists of strings. When present, `memory_prompt_mode` must be one of `off`, `agent`, or `agent_user`; it also drives the effective `memory` tool: `off` disables the tool, while the other modes enable it even when `allowed_tools` is otherwise empty. The `memory` tool is not configured independently through `allowed_tools`. `custom_system_prompt_enabled` must be a boolean; when omitted, the custom prompt toggle defaults to `false`.
- Enabling `custom_system_prompt_enabled` through public Agent RPC seeds the Agent prompt directory from the current effective default prompt fragments. Re-enabling preserves existing Agent prompt files.
- Automatic fallback is run-local only. It does not mutate persisted `model` or `fallback_model`.
- The optional `::<connection-local-id>[:<account-id>]` suffix stores the provider-local connection slug, optionally with a pinned credential account. Runtime reconstructs the full connection ID as `<provider>:<connection-local-id>[:<account-id>]` from the model prefix; account semantics live in `providers.md` → Accounts.
