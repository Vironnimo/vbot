# Prompts

System Prompt assembly as a block model: every contribution to the prompt is a declared block, ordered by a per-scope layout, gated, and joined deterministically.

## Overview

`core/prompts/` owns the product-facing System Prompt domain. The prompt is assembled from **blocks**: each piece (shared Runtime, Identity Environment, Working Project context, tools/channels/skills text, SOUL, a config agent's body, pinned memory, and tool/extension/user contributions) is one block with a stable id, an owner, and a content kind. A per-scope **layout** decides order and on/off state; three **gates** decide which blocks render; survivors are trimmed and joined. The domain also expands prompt variables, renders the tool/skill/channel/memory lists, validates public block edit/layout requests, and filters provider tool schemas through prompt-time Agent policy.

It does not own raw file I/O, workspace memory, HTTP/RPC transport, or tool execution. Storage persists layout and text overrides (`PromptBlockStore`) plus bundled fragments; memory declares its block and renders files; tools/skills/channels provide catalogs; extensions declare blocks; Chat sends the assembled system message whole. The module splits into the pure assembly engine (`blocks.py`, everything injected - fully unit-testable) and the manager facade (`prompts.py`).

## Terms

Domain-specific vocabulary for System Prompt assembly. These are all prompt-domain internals; no core prompt term lives in the glossary.

### Prompt Block
**Definition:** The unit the System Prompt is assembled from - an ordered, gated contribution that is either an editable **text** block (text may carry `{include:...}` and `{generated:...}` markers) or a non-editable **data** block (SOUL, Working Project context, config-agent body, auto-list, or dynamic render). Every contributor hands over a block *definition*; the prompt is the layout-ordered, three-gate-filtered, normalized concatenation of survivors.
**Not:** A prompt fragment - the old closed set of five fragment files is gone. A block is fragment-sized but reorderable, toggleable, gated, and contributable from any source.

### Block Layout
**Definition:** The persisted per-scope ordered list of `{id, enabled, source}` owning order and on/off state (`layout.json` per scope). A block absent from the layout inserts at its definition's `default_rank` with `default_enabled`; an entry whose definition is gone is inert - skipped at build, pruned on next write.
**Not:** Definitions or text - the layout owns order + enabled only.

### Block Owner (three-gate filter)
**Definition:** A block renders only when all three gates hold: user-enabled (layout on) + owner-active + non-empty after expansion. The **owner** - `always` / `identity` / `memory` / `tool:<name>` / `channel` / `extension:<name>` - is gate 2: `identity` requires an Identity/Memory Workspace, `channel` drops entirely without an enabled Channel config for that Agent, `memory` renders whenever `memory_prompt_mode` is on regardless of Tool permission.
**Not:** The Source prefix - owner is the activation condition, source is provenance; both may read `tool:`/`extension:` but are distinct fields.

### Source prefix
**Definition:** The namespace before the first colon in a block id - `core:` / `tool:` / `extension:` / `user:` / `memory:` - mapping to its override folder `blocks/<namespace>/<slug>.md` (the colon never reaches the path).
**Not:** The Block Owner (see above).

### Producer
**Definition:** A function registered under a marker name rendering a `{generated:NAME}` placeholder at build time - the auto-lists `tool_list` / `skill_catalog` / `channel_list` and `memory_files`. Unknown markers render empty with a warning; empty results leave no residue.
**Not:** A dynamic block - a producer fills one marker inside an editable block; a dynamic block's entire body is a render function (a deliberate cache break).

## Data Model

A **block** (`BlockDefinition`) carries: fully-qualified `id` `<source>:<slug>` (source is an open string, no-colon ids are contract errors); `owner` driving gate 2 (`always`, `identity`, `memory`, `channel`, `tool:<name>` = Tool in exact effective policy + current provider definitions, `extension:<name>` = in loaded set); `kind` `text` (override cascade + markers) or `data` (not user-editable, `default_text` inserted verbatim, `{...}` never interpreted); exactly one of `default_text`/`render`; `default_rank`; `default_enabled` (`False` ships an opt-in block staying off even under older layouts).

Core/data ids: `core:runtime`, `core:identity_runtime` (owner `identity`), `core:tools`, `core:tools_list` (**ships disabled** - providers already receive Tool descriptions natively, so the prompt copy is an opt-in booster for models attending poorly to schemas), `core:channels`, `core:skills`, `core:skill_maintenance` (owner `tool:skill_manage`, Identity Agents only), `core:soul`, `core:working_project`, `core:agent_body` (data, per-run), `memory:guidance`. Custom user blocks are `user:<slug>`, owner `always`.

The bundled default layout lives in `resources/prompts/layout.json` shipping soul -> memory -> runtime -> identity_runtime -> tools -> project/subagent dynamic blocks -> tools_list (off) -> channels -> skills -> skill_maintenance -> agent_body -> working_project. A broken file reads as empty layout - defaults apply, never taking a build down. The seven core text blocks read defaults from their fragment resources; `working_project.md` backs the non-editable dynamic block. There is no fragment-edit facade anymore - the only prompt-edit surface is the manager's block facade below.

## Assembly Pipeline

`assemble_system_prompt(definitions, layout, context, *, owner_activity, override_resolver, producers, replacements)` is deterministic - same inputs, same output (prompt cache depends on it):

1. **Dedupe** definitions by id, first-collected wins (core/data/memory first, then contributed, then custom - nothing shadows a core id); collisions log both sources.
2. **Resolve layout:** matching entries keep order/enabled; entries without definitions are inert (pruned later); absent definitions append at `default_rank` - the user's chosen order is never disturbed by new blocks.
3. **Resolve text:** dynamic blocks call `render(context)` in isolation (exception drops only that block); static text takes the override cascade then expands `{generated:...}` and `{include:...}` fail-soft plus build-time replacements; data stays verbatim.
4. **Three gates:** user-enabled + owner-active (delegated to injected `OwnerActivity`, never hardcoded) + non-empty.
5. **Normalize:** trim, drop empties, join with exactly one blank line - no padding traces anywhere.

**Producers:** closures over manager-held registries; `memory_files` emits Chat's pinned epoch rendering verbatim instead of re-reading. `BlockRenderContext` carries `nesting_depth` for context-dependent block prose and deliberately **no conversation messages** - message-dependent content belongs to the `context` extension hook.

**Runtime variables:** `{server_hostname}`, `{vbot_version}`, `{operating_system}`, `{model}`, `{identity_workspace}`, `{vbot_root}`, `{data_root}`, `{thinking_effort}`, `{current_local_date}`, `{timezone}` substitute exactly, one pass, non-recursively; unrelated `{...}` stays literal; vBot-authored path values use the forward-slash Model presentation; retired names have no aliases and remain literal. `runtime.md` (owner `always`) carries OS/model/thinking effort plus the current date in the configured IANA timezone and the timezone name - unset effort renders "provider default", never blank. The runtime block deliberately exposes no clock time: its date changes at most once per configured-zone day, preserving the stable prompt prefix and provider prompt-cache reuse. `identity_runtime.md` (owner `identity`) carries hostname/version/workspace/roots, so Config Agents receive none of those.

**Override cascade:** agent override <- default-scope override <- owner default; composed by the manager from two store reads, never by the store. Dynamic blocks have no override path.

**Data blocks:** `core:soul` renders workspace SOUL.md through the single include-expansion path plus a framing prefix (kept out of shared wrapping so it never leaks onto other includes; missing/empty workspace returns "" before framing so the block gates out clean), pinned per prompt epoch. `core:working_project` renders the file-backed template from `ProjectPromptContext` (only `{project_name}/{project_id}/{project_workspace}/{project_files}`; retired `$project_*` unresolved); metadata renders once in Markdown while `<project_context>` wraps auto-load files verbatim; auto-load paths are taken verbatim with no location restriction, reads lazy and fail-soft. Rooted Identity Agents and Config Agents receive Chat's pinned prompt-epoch snapshot instead of re-reading; Unrooted Agents gate the block out. `core:agent_body` carries a Config Agent's body verbatim (empty for Identity Agents -> gates out).

## Interfaces

`build_system_prompt_async`, `provider_tool_definitions_async`, and `render_working_project_context_async` are Event-Loop-safe entry points running reads/expansion/schema generation through the dedicated four-worker pool with backpressure.

- `SystemPromptManager.build_system_prompt(...)` collects the full definition list for Agent/Run and routes through assembly. Key inputs: Chat's prompt-epoch snapshots (`working_project_context`, `soul_context`, `memory_files_context`) emit verbatim and prevent the corresponding file reads - Working Project pinning covers only its own block; `nesting_depth` is build context only; `read_paths` is a pure side channel reporting files actually read (only when rendering/refreshing a snapshot, never on reuse). Without explicit scope, the Agent scope applies only under `custom_system_prompt_enabled`.
- For Rooted Identity Agents, SOUL/memory still render from Workspace while Project context comes from the admitted Project; live Runs, compaction rebuilds, and preview share one resolver failing closed.
- The `core:skills` catalog is origin-grouped and path-free (`SkillPromptMetadata`: name/description/origin; English labels owned here, vocabulary from `core.skills`). `render_skill_catalog(...)` produces the `PinnedSkillCatalog` Chat pins per epoch.
- `core/prompts/pinned_context.py` owns the Chat-side pinned-snapshot assembly persisted into Session metadata between Compactions, plus `stamp_prompt_files_read` for read-before-write state - called directly by ChatLoop/Compaction via a narrow dependency slice, no chat-owned copy.
- `provider_tool_definitions(...)` filters schemas by the single Tools-domain `resolve_tool_access` result, passing stable Definition Profile context so profile-based visibility cannot drift between native schemas, `core:tools_list`, and owner gates. Takes no Skill registry/catalog params - Tool presence is never derived from the pinned catalog.
- `update_block_definitions(...)` / `update_skill_registry(...)` refresh contributed blocks and gate-2 membership on every extension/skill reload without restart.

**Block-edit facade** (the `prompt.*` RPC surface): `list_scopes()` reports each custom-prompt Agent scope with `has_customizations` (saved layout or any override) so the editor can confirm before disabling custom prompts. `list_blocks(scope)` gives static metadata in layout order including effective text, `is_modified`, and inheritance layer for editable blocks - owner-active status deliberately excluded (preview's job). `update_block` accepts overrides on editable blocks only (data/dynamic reject); `reset_block` drops to inherited/default (a `user:` block has no default - delete instead). `set_layout` tolerates contributor-gone ids (store prunes them on write); `reset_layout` restores the bundled default leaving text overrides untouched. `create_block`/`remove_block` manage `user:<slug>` blocks only - core/tool/extension blocks toggle off, never delete. `prompt.preview` returns rendered `text`, token counts including the Tool-definition footprint, and `estimated`.

## Conventions

- Prompt code depends on Protocols for Agent/Tool/Skill/Channel/Storage shapes and imports no concrete domain classes; contributions arrive as plain `BlockDefinition` objects handed in via the runtime.
- Bundled default texts are **English Markdown in resources** - a deliberate signed-off i18n exception: the System Prompt is the model-facing English contract, not localized UI string.
- `{include:filename}` accepts safe flat filenames under the agent workspace, wraps as `<file name="...">`, drops missing/unreadable files with a warning (never aborting a run); unsafe paths raise `PromptError`. An **empty workspace** drops every include without reading - it must never resolve against `Path("")` (= CWD), which would read server-side files.
- The `core:channels` block gates out entirely without enabled Channels - no `- None` fallback. Its producer lists persisted enabled Channels independent of adapter liveness, keeping the prompt prefix stable through transient failures; a single allowed chat id shows "default target available".
- `resources/prompts/channels.md` carries the static group-authorization contract (member attribution format, admin/member authorization split, authority-by-message rule); per-group identities and live roles never enter blocks or producers, so access changes never break the cache.
- Provider Tool permissions come from resolved policy plus Chat-derived grants - admitted Runs cannot inject any; the `core:tools`/`core:tools_list` blocks control only prompt-visible text, not permission. The dynamic `tool:project` and `tool:subagent` blocks render exactly when their Tool is effectively available and agree with provider visibility and addressing routing; preview supplies the same scope.
- The dynamic `tool:bash` block renders only with permanent `tools.bash.allowed_env` grants and lists credential names plus the `env_keys` contract; Skill-derived grants never enter the System Prompt (their payload carries its own notice at load time).
- `memory_prompt_mode` alone controls the memory block and independently gates the `memory` Tool: `agent`/`agent_user` keep prompt Memory visible even when the Tool is denied (intentional read-only Memory); `off` removes both; `tool_access.mode: none` removes the Tool but not prompt Memory.

## Constraints & Gotchas

- RPC error split: block edit/layout/preview validation maps `PromptError` -> `invalid_request`; assembly/storage/runtime failures during preview use the normal domain-error path.
- User overrides and per-scope layouts persist until explicitly reset; agent scopes are owned only while `custom_system_prompt_enabled` is true - the store ignores their files otherwise.
- Preview uses the live manager (current registries, extensions, layout, overrides), renders the Working Project frame prospectively without persisting a snapshot, has no Session and therefore no Session-scoped Tools such as `history`; no scope shows the Agent's baseline, `scope: {type: "default"}` previews the default scope.
- Chat builds the prompt per request and omits the provider system message entirely when the result is empty/whitespace-only.
- Do not move block behavior into Agent or WebUI code: Agents own only the flag and Workspace path, WebUI owns editing UX, this domain owns assembly semantics and the block contract.
- Adding a keyword parameter to `build_system_prompt` breaks every duplicated prompt-manager test double that spells out the full signature - grep `class .*Prompts` under `tests/` (eight today, across `tests/core/chat`, `tests/core/runtime`, and `tests/server`). The failure appears at runtime as `TypeError: ... unexpected keyword argument` inside server/integration tests, not at import time; update every double in the same change.

## References

Read these only when your task matches - not by default.

- Adding a new pinned prompt-epoch input -> `prompts/adding-a-pinned-input.md`
