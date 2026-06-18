# Prompts

System Prompt assembly and editable prompt-fragment domain rules.

## Overview

`core/prompts/` owns the product-facing System Prompt domain: choosing the default or Agent prompt scope, assembling a prompt from fragments, expanding prompt variables, rendering prompt-visible tools/skills/channels/memory, validating public prompt edit/reset requests, and filtering provider tool schemas through prompt-time Agent policy.

It does not own raw file I/O, workspace memory, HTTP/RPC transport, or tool execution. `core/storage/` reads/writes fragment files and bundled defaults, `core/memory/` renders `{memory}`, `core/tools/`, `core/skills/`, and `core/channels/` provide catalogs, `core/chat/` sends the assembled system message, and `server/rpc/operations_methods.py` maps `prompt.*` methods to this domain.

## Data Model

Editable System Prompt fragments are exactly these five names, in stable UI order:

- `system.md`
- `runtime.md`
- `tools.md`
- `channels.md`
- `skills.md`

`compaction.md` is storage-readable for backend compaction, but it is not a System Prompt fragment, is not Agent-scoped, and must not become editable through the System Prompt UI.

Prompt scopes:

- `default` reads and writes `<data_dir>/prompts/`, with `resources/prompts/` fallback when no user copy exists.
- `agent` reads and writes `<data_dir>/agents/<agent-id>/prompts/`, is listed/accepted only for Agents with `custom_system_prompt_enabled: true`, and is ignored by runtime prompt assembly while that flag is false.
- Missing Agent-scope fragments read as `""`, report `is_modified: false`, and are created on save. An empty Agent `system.md` is valid and results in no prompt-visible blocks unless it contains placeholders.

Variable metadata lives in `PROMPT_FRAGMENT_VARIABLES` and is user-visible editor data. It is not a generic template engine: add new placeholders in `core/prompts/prompts.py` intentionally and document the runtime behavior here.

## Interfaces

- `SystemPromptManager.build_system_prompt(agent, scope=None, *, agent_body="", project_context=None) -> str` builds the complete system prompt. With no explicit scope it uses the Agent scope only when `custom_system_prompt_enabled` is true; otherwise it uses `default`. Explicit Agent scopes are mainly used by preview callers and must match the Agent passed to the build. `agent_body` (a config agent's verbatim prompt body, `""` for identity agents) and `project_context` (`ProjectPromptContext(cwd, auto_load)`, `None` off a project) are passed in by the chat loop **and** the `prompt.preview` RPC — the prompt domain stays on Protocols and never imports `ConfigAgent`; the caller extracts the body once via `core.projects.runtime_agent_body(agent)`.
- `SystemPromptManager.render_project_files(project_context) -> str` renders the `{project_files}` block (AGENTS.md first, then `auto_load` in order, each `<file name="…">`-wrapped). It is reused as the **single source** for both the in-prompt block and the visiting `<system-reminder>` content (the chat loop's `inject_visiting_project_files`).
- `SystemPromptManager.provider_tool_definitions(agent) -> list[dict]` returns provider-ready tool schemas filtered by Agent policy. It applies the same derived `memory` tool visibility as prompt rendering and adds the internal `skill` tool only when the Agent has loadable allowed skills.
- `SystemPromptManager.update_skill_registry(skill_registry)` is called by `Runtime.reload_skills()` so prompt catalogs and provider `skill` visibility refresh without an app restart.
- `SystemPromptManager.app_dir` (read-only property) exposes the application source directory the manager was built with; chat tool dispatch reads it instead of probing private attributes.
- `PromptFragmentManager.list_scopes()` returns `default` plus enabled Agent scopes only.
- `PromptFragmentManager.list_fragments(scope=None)`, `update_fragment(name, content, scope=None)`, and `reset_fragment(name, scope=None)` expose the editable fragment surface. Update always returns `is_modified: true`; default reset restores bundled content and returns `is_modified: false`; Agent reset copies the current effective default-scope content and returns `is_modified: true`.
- RPC methods are `prompt.list`, `prompt.update`, `prompt.reset`, and `prompt.preview`. `prompt.preview` returns rendered `text`, token count, and `estimated`; for an explicit Agent scope the server can infer `agent_id` from the scope, while default/no scope requires `agent_id`. The `agent_id` param is an `agent@projekt` **address** (parsed once at the edge via `parse_agent_address`): a bare value previews the identity agent unchanged; a project-qualified value resolves that project's config agent through `AgentResolver` and threads its `agent_body` + the project's `ProjectPromptContext` into the build, so `{project_files}` and the imported body render exactly like a project-born run. An Agent prompt scope is identity-only — project context never applies on that path.

## Conventions

- Prompt domain code depends on Protocols for Agent, Tool, Skill, Channel, and Storage shapes. Avoid importing concrete AgentStore, ChannelService, or StorageManager classes here unless a new boundary genuinely needs it.
- `system.md` is the root. It may include `{memory}`, `{runtime}`, `{tools}`, `{channels}`, `{skills}`, `{include:filename}`, and direct `{app_version}` replacement.
- `runtime.md` expands `{host}`, `{app_version}`, `{os}`, `{model}`, `{agent_workspace}`, `{app_dir}`, `{data_root}`, `{thinking_effort}`, and `{current_date}`. The date is UTC ISO date only; the bundled prompt tells agents to use the `status` tool when they need current time.
- `tools.md`, `channels.md`, and `skills.md` expand `{tool_list}`, `{channel_list}`, and `{skill_list}` respectively.
- Workspace includes accept only safe flat filenames. `{include:filename}` resolves under the Agent workspace, wraps content as `<file name="filename">\n...\n</file>`, omits missing files with a warning, and raises `PromptError` for unsafe paths or other read failures. An **empty workspace** (a config agent, `workspace == ""`) means "no includes": every `{include:…}` is dropped, with no file read and no warning. It must never resolve against `Path("")` (= `Path(".")`), which would read SOUL.md/USER.md from the server's process CWD.
- The bundled default `system.md` includes `SOUL.md` through `{include:SOUL.md}` and pinned memory through `{memory}`. It does not include `USER.md` or `MEMORY.md` directly; those files belong to the memory service's rendered block.
- Two collapsing project placeholders sit in the root `system.md`: `{agent_body}` in the **identity slot** (just before `{include:SOUL.md}`) and `{project_files}` **after** the identity block. They carry their own separator newlines, so an empty value collapses to nothing — an identity agent at home is byte-identical to a `system.md` without them. "Identity first, then project" falls out of the placement + emptiness: a config agent has empty SOUL/memory (body leads), an identity agent has an empty body (SOUL leads), and a rooted identity agent gets both — identity then project.
- **`{agent_body}` is verbatim.** It is substituted **last**, after all vBot placeholder and `{include}` expansion, so a config agent's body may contain `{…}` (even `{include:…}`/`{memory}`) without it being interpreted — the OpenCode body is inserted like an include, never re-evaluated.
- **`{project_files}`** renders the project's auto-load files from the **cwd**: AGENTS.md first (if present), then `auto_load` entries in order, each wrapped via the same `_wrap_include_file` source as `{include}`. Lazy (missing files skipped silently), project-root only, and **no size limit / truncation / warning** (technical-user contract: the file goes in 1:1). Config agents keep the inherited `{runtime}`/`{tools}`/`{channels}`/`{skills}` blocks from the same root.
- Optional blocks are lazy: `{runtime}`, `{memory}`, `{tools}`, `{channels}`, and `{skills}` are rendered only when the placeholder appears in the active root `system.md`. Custom Agent roots do not implicitly include default blocks; placeholders read child fragments from the same active scope.
- Skill prompt metadata is XML-escaped before insertion. Leave escaping in `core/prompts/`; Skills owns discovery/availability, not prompt serialization.
- Prompt fragment variable metadata is descriptive UI data only; changing it is a user-visible contract change.
- Provider tool permissions and schemas continue to come from `allowed_tools`. The `{tools}` prompt placeholder controls only prompt-visible explanatory text and does not grant tool access.
- Channels render only active, enabled, running channels for the current Agent. A single allowed chat id is shown as `default target available`; multiple/zero allowed ids require an explicit target.

## Constraints & Gotchas

- Prompt RPC error behavior is intentionally split: list/update/reset and preview scope validation map `PromptError` to `invalid_request`, while prompt assembly/storage/runtime failures during preview go through the normal domain-error path.
- User-edited prompt fragments in `<data_dir>/prompts/` override bundled resources and must be preserved unless reset explicitly.
- Agent prompt fragments are ignored unless that Agent's `custom_system_prompt_enabled` flag is true. Enabling through public Agent RPC seeds missing Agent fragments from the current effective default scope; re-enabling preserves existing Agent files; disabling ignores them without deleting them.
- Prompt preview uses the runtime's active `SystemPromptManager`, so it reflects live skill registry, tool registry, channel state, and Agent allowlists.
- `prompt.preview` without a `scope` parameter shows the Agent's effective runtime prompt, including its Agent scope when custom prompts are enabled. Passing `scope: {type: "default"}` previews the default scope explicitly.
- Chat builds the system prompt for every request and omits the provider system message entirely when the assembled prompt is empty or whitespace-only.
- `memory_prompt_mode` controls both `{memory}` rendering and derived provider visibility of the `memory` tool. Even if `allowed_tools` is empty, `agent` and `agent_user` modes make `memory` available; `off` removes it from prompt and provider definitions.
- Do not move prompt-body behavior into Agent code or WebUI code. Agents own only the `custom_system_prompt_enabled` flag and workspace path; WebUI owns editing UX; this domain owns assembly semantics.
