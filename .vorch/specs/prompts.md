# Prompts

System Prompt assembly and editable prompt-fragment domain rules.

## Overview

`core/prompts/` owns the product-facing System Prompt domain: prompt assembly, prompt-visible tool/skill/channel rendering, workspace include expansion, editable fragment order, fragment variables, and prompt-specific edit/reset validation.

`core/storage/` still owns raw prompt fragment file I/O, bundled-resource fallback, atomic writes, and the broader storage allowlist that includes backend-only fragments such as `compaction.md`. Server delegates map prompt RPC requests to `core/prompts/` and translate prompt errors to stable RPC codes.

## Data Model

Editable prompt fragments are exactly these five names, in UI order:

- `system.md`
- `runtime.md`
- `tools.md`
- `channels.md`
- `skills.md`

`compaction.md` is storage-readable for backend compaction, but it is not editable through the System Prompt UI.

Prompt fragments can be edited in scopes:

- The `default` scope reads and writes `<data_dir>/prompts/`, falling back to `resources/prompts/` when a default user copy is absent.
- An `agent` scope reads and writes `<data_dir>/agents/<agent-id>/prompts/` and is available only when that Agent has `custom_system_prompt_enabled: true`.
- Missing Agent-scope fragments read as empty strings and are created on save. `system.md` may intentionally be empty.

## Interfaces

- `core/prompts/__init__.py` exports `SystemPromptManager`, `PromptFragmentManager`, `PromptError`, prompt Protocols, editable fragment names, and variable metadata.
- `SystemPromptManager(storage, tool_registry, skill_registry, app_version, app_dir, data_root, ...)`
  - `build_system_prompt(agent, scope=None) -> str` expands `{memory}`, `{runtime}`, `{tools}`, `{channels}`, `{skills}`, and `{include:filename}`. The rendered memory fragment comes from the memory service and follows the Agent's `memory_prompt_mode`. The rendered runtime fragment expands `{app_version}` with the application version. With `scope=None`, it uses the Agent scope only when the Agent explicitly enables custom system prompts; otherwise it uses the default scope. Passing an explicit scope is used by preview and must match the preview Agent for Agent scopes.
  - `provider_tool_definitions(agent) -> list[dict]` returns provider tool schemas filtered by the Agent allowlist and adds the internal `skill` tool only when the Agent has loadable skills.
  - `update_skill_registry(skill_registry)` refreshes prompt-visible skill filtering after runtime skill reload.
- `PromptFragmentManager(storage, agent_store=None)`
  - `list_scopes() -> list[dict]` returns the default scope plus enabled Agent scopes only.
  - `validate_scope(scope=None) -> PromptScope` validates public scope payloads for RPC callers.
  - `list_fragments(scope=None) -> list[dict]` returns editable fragments in stable UI order with content, modification status, and variable metadata for the chosen scope.
  - `update_fragment(name, content, scope=None) -> dict` validates that `name` is editable, writes the scoped user copy through storage, and returns `{ name, content, is_modified: true }`.
  - `reset_fragment(name, scope=None) -> dict` validates that `name` is editable and resets through storage. Default-scope reset restores the bundled default and returns `is_modified: false`; Agent-scope reset copies the current effective default-scope content and returns `is_modified: true`.

## Conventions

- Prompt domain code depends on Protocols for Agent, Tool, Skill, Channel, and Storage shapes. Avoid importing concrete AgentStore, ChannelService, or StorageManager classes here unless a new boundary genuinely needs it.
- Workspace includes accept only safe flat filenames. `{include:filename}` resolves under the Agent workspace and wraps content as `<file name="filename">\n...\n</file>`.
- The bundled default system prompt includes `SOUL.md` through `{include:SOUL.md}` and pinned memory through `{memory}`. It does not include `USER.md` or `MEMORY.md` directly; those files belong to the memory service's rendered block.
- Custom Agent `system.md` expands optional blocks lazily: `{runtime}`, `{memory}`, `{tools}`, `{channels}`, and `{skills}` are rendered only when the placeholder appears in the Agent's `system.md`. This keeps an empty or minimal Agent root from implicitly pulling in default fragments.
- Skill prompt metadata is XML-escaped before insertion.
- Prompt fragment variable metadata is descriptive UI data only; changing it is a user-visible contract change.
- Provider tool permissions and schemas continue to come from `allowed_tools`. The `{tools}` prompt placeholder controls only prompt-visible explanatory text and does not grant tool access.

## Constraints & Gotchas

- Prompt RPC error behavior must stay stable: prompt-specific public validation errors map to `invalid_request`; storage/runtime failures map through the server's normal domain-error path.
- User-edited prompt fragments in `<data_dir>/prompts/` override bundled resources and must be preserved unless reset explicitly.
- Agent prompt fragments are ignored unless that Agent's `custom_system_prompt_enabled` flag is true. Disabling the flag does not delete Agent prompt files.
- Prompt preview uses the runtime's active `SystemPromptManager`, so it reflects live skill registry, tool registry, channel state, and Agent allowlists.
- `prompt.preview` without a `scope` parameter shows the Agent's effective runtime prompt, including its Agent scope when custom prompts are enabled. Passing `scope: {type: "default"}` previews the default scope explicitly.
