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

## Interfaces

- `core/prompts/__init__.py` exports `SystemPromptManager`, `PromptFragmentManager`, `PromptError`, prompt Protocols, editable fragment names, and variable metadata.
- `SystemPromptManager(storage, tool_registry, skill_registry, app_version, app_dir, data_root, ...)`
  - `build_system_prompt(agent) -> str` expands `{runtime}`, `{tools}`, `{channels}`, `{skills}`, and `{include:filename}`. The rendered runtime fragment expands `{app_version}` with the application version.
  - `provider_tool_definitions(agent) -> list[dict]` returns provider tool schemas filtered by the Agent allowlist and adds the internal `skill` tool only when the Agent has loadable skills.
  - `update_skill_registry(skill_registry)` refreshes prompt-visible skill filtering after runtime skill reload.
- `PromptFragmentManager(storage)`
  - `list_fragments() -> list[dict]` returns editable fragments in stable UI order with content, modification status, and variable metadata.
  - `update_fragment(name, content) -> dict` validates that `name` is editable, writes the user copy through storage, and returns `{ name, content, is_modified: true }`.
  - `reset_fragment(name) -> dict` validates that `name` is editable, resets through storage, and returns `{ name, content }`.

## Conventions

- Prompt domain code depends on Protocols for Agent, Tool, Skill, Channel, and Storage shapes. Avoid importing concrete AgentStore, ChannelService, or StorageManager classes here unless a new boundary genuinely needs it.
- Workspace includes accept only safe flat filenames. `{include:filename}` resolves under the Agent workspace and wraps content as `<file name="filename">\n...\n</file>`.
- Skill prompt metadata is XML-escaped before insertion.
- Prompt fragment variable metadata is descriptive UI data only; changing it is a user-visible contract change.

## Constraints & Gotchas

- Prompt RPC error behavior must stay stable: prompt-specific public validation errors map to `invalid_request`; storage/runtime failures map through the server's normal domain-error path.
- User-edited prompt fragments in `<data_dir>/prompts/` override bundled resources and must be preserved unless reset explicitly.
- Prompt preview uses the runtime's active `SystemPromptManager`, so it reflects live skill registry, tool registry, channel state, and Agent allowlists.
