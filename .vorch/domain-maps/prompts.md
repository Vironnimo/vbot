# Prompts

System Prompt assembly as a block model: every contribution to the prompt is a declared block, ordered by a per-scope layout, gated, and joined deterministically.

## Overview

`core/prompts/` owns the product-facing System Prompt domain. The prompt is assembled from **blocks**: each piece (core text, SOUL, Working Project context, a config agent's body, pinned memory, Tool/Extension/user contributions) is one block with a stable id, an owner, and a content kind. A per-scope **layout** decides order and on/off state; three **gates** decide which blocks render; survivors are trimmed and joined. It also expands variables and Tool/Skill/Channel/Memory catalogs, validates block/layout edits, and applies prompt-time Agent Tool policy. Explicit bound Identity delegation choices are checked against the Agent store so a recreated Agent is not advertised under an earlier incarnation's grant (`agent.md`, `subagents.md`); this projection never replaces the coordinator's admission check.

It does not own configuration persistence, workspace memory, HTTP/RPC transport, or tool execution. Storage persists layout and text overrides (`PromptBlockStore`) plus bundled fragments; memory declares its block and renders files; tools/skills/channels provide catalogs; extensions declare blocks; Chat sends the assembled system message whole. `blocks.py` is the pure assembly engine; `prompts.py` (`SystemPromptManager`) is the RPC and Chat boundary; `_render_inputs.py` owns variables, catalogs and owner gates; `briefs.py` composes learning-Run briefs, which are a Run's input, not System Prompt blocks.

## Terms

### Prompt Block
One ordered, gated piece of the System Prompt with a stable id, an owner and a content kind: an editable text block or a non-editable data block (Data Model). Not a prompt fragment, the bundled resource file that may supply a block's default text.

### Block Layout
The per-scope saved order and on/off state of the blocks, without their definitions or text (Assembly Pipeline; stored form: `storage/prompt-files.md` -> Block persistence).

### Block Owner
The activation condition of a block: a block renders only when the user enabled it, its owner is active and it is not empty (Data Model, Assembly Pipeline). Not the Source prefix, which records where a block comes from.

### Source prefix
The namespace before the first colon of a block id, which records where the block comes from and names its override folder (`prompts/block-editing-and-preview.md`). Not the Block Owner.

### Producer
A named function that fills one `{generated:...}` marker inside a text block at build time, such as the Tool or Skill catalog (`prompts/core-blocks.md` -> Producers). Not a dynamic block, whose whole body is a render function that Chat pins per prompt epoch (Interfaces -> `render_dynamic_blocks`).

## Data Model

A **block** (`BlockDefinition`) carries: fully-qualified `id` `<source>:<slug>` (source is an open string, no-colon ids are contract errors); `owner` driving gate 2 (`always`, `identity` = the Agent has an Identity/Memory Workspace, `memory` = enabled personal or Working Project Memory, regardless of Tool permission (`memory.md` -> Modes and gating), `channel` = an enabled Channel config of the Agent, `subagent_session` = a prompt built for a Sub-Agent Session, `builtin:<kind>` = the built-in Agent of that kind, `tool:<name>` = Tool among the request's Tool definitions when the caller passes them, else in exact effective policy + current provider definitions; a trailing `*` (`tool:mcp_*`) matches any such Tool with that name prefix, used by Tool-gated Extension blocks (`extensions/capabilities.md`); `extension:<name>` = in loaded set); `kind` `text` (override cascade + markers) or `data` (not user-editable, `default_text` inserted verbatim, `{...}` never interpreted); exactly one of `default_text`/`render`; `default_rank`; `default_enabled` (`False` ships an opt-in block staying off even under older layouts); `requires_generated` (a Producer name: the static text renders empty while that Producer renders nothing, so a heading around an empty list leaves no trace).

Core, memory and data block ids and what each renders: `prompts/core-blocks.md`. Custom user blocks are `user:<slug>`, owner `always`. A missing, explicitly empty, or broken persisted layout uses the bundled defaults (`resources/prompts/layout.json`; broken files: `storage/prompt-files.md`). Empty means no custom order or enablement, not no blocks: disabling all blocks requires explicit `enabled: false` entries. The nine core text blocks read defaults from their fragment resources; an Agent scope reads its Agent copies, except `core:system_reminders` and `core:subagent_role`, which render their bundled fragment in every scope because custom-prompt Agent copies predate them (block overrides and layout enablement still apply).

## Assembly Pipeline

`assemble_system_prompt(definitions, layout, context, *, owner_activity, override_resolver, producers, replacements)` is deterministic - same inputs, same output (prompt cache depends on it):

1. **Dedupe** definitions by id, first-collected wins (core/data/memory first, then contributed, then custom - nothing shadows a core id); collisions log both sources.
2. **Resolve layout:** matching entries keep order/enabled; entries without definitions are inert (pruned later); absent definitions append after them by `default_rank` - the user's chosen order is never disturbed by new blocks.
3. **Resolve text:** skip disabled or inactive blocks before rendering; dynamic blocks call `render(context)` in isolation (exception drops only that block), except that Chat builds contributed blocks, temporary Session blocks and `core:channels` from their epoch snapshots (`pinned_blocks`, `pinned_block_order`), retaining membership and positions even after contributor removal; static text takes the override cascade, then `expand_block_template` tokenizes that author-controlled template once and substitutes `{generated:...}` and `{include:...}` (fail-soft) plus build-time replacements in the same pass. Producer output, included file content, and replacement values are inserted verbatim and never scanned again, so a Memory entry or Skill description containing `{include:...}` or `{model}` stays literal and cannot read files or fail the build; data stays verbatim.
4. **Three gates:** user-enabled + owner-active (delegated to injected `OwnerActivity`, never hardcoded) + non-empty.
5. **Normalize:** trim, drop empties, join with exactly one blank line - no padding traces anywhere.

**Render inputs:** Producers are closures over manager-held registries and request-scoped overrides; static contribution snapshots and change planning pass the same Skill registry, pinned Skill catalog and prompt-epoch effort as ordinary assembly. `BlockRenderContext` carries `subagent_session` for role-dependent prose, `tool_available` (which Tools the receiving Agent can call: the given `effective_tool_definitions` or On-demand Tools, else the Agent's live policy) and deliberately **no conversation messages** - message-dependent content belongs to the `context` extension hook.

**Prompt files:** `render_prompt_file` (`blocks.py`) is the single reader for every file vBot puts into a Model's Context on its own: `{include:...}` files (SOUL included) and Project instruction files (`prompts/prompt-files.md`); Memory is not a file here (`memory.md`).

**Override cascade:** agent override <- default-scope override <- owner default; composed by the manager from two store reads, never by the store. Dynamic and data blocks have no override path. Request-local data namespaces therefore never reach the persistent override store.

**Prompt-epoch pins:** Chat pins every file-backed input and every contributed or temporary block's text per prompt epoch; they change only at a successful Compaction, mid-epoch changes arrive as System Reminders (`prompts/adding-a-pinned-input.md`).

## Interfaces

`build_system_prompt_async`, `provider_tool_definitions_async`, and `render_working_project_context_async` are Event-Loop-safe entry points: reads, expansion and schema generation run on the dedicated four-worker pool.

- `SystemPromptManager.build_system_prompt(...)` collects the full definition list for Agent/Run and routes through assembly. Key inputs: Chat's prompt-epoch snapshots (`working_project_context`, `soul_context`, `memory_files_context`) emit verbatim and prevent the corresponding file reads; `subagent_session` activates the `subagent_session` block owner (`core:subagent_role`); `pinned_blocks` (block id -> text) fixes the contributed/temporary block inventory and text; `pinned_block_order` fixes positions, so later live gates or missing definitions cannot change them. Without explicit scope, the Agent scope applies only under `custom_system_prompt_enabled`.
- `render_dynamic_blocks(agent, *, block_ids=None, skip=(), <build_system_prompt render inputs>)` renders the blocks that follow live state and pass gates 1 and 2, keyed by id: all contributed Tool/Extension blocks, temporary `request_block_definitions`, and `core:channels`, whose text resolves like any static text block, for Chat's per-epoch pin and change check (`chat/request-building.md` -> Dynamic prompt blocks per prompt epoch). An empty render is kept (it pins as empty); failures are reported through `failed_blocks` so change planning retains the last known state.
- Personal SOUL/Memory render from the Identity Workspace; shared Project Memory joins the same Memory block for the admitted Working Project, including Workspace-less Agents. `ProjectPromptContext` carries its Memory anchor and Project switch; live Runs, Compaction and preview share the rendering path and respect the Agent switch independently of Tool permission.
- `core:skills` renders the Skill catalog (`render_skill_catalog(...)`, pinned by Chat as `PinnedSkillCatalog`); format and gating: `skills/loading.md` -> Catalog rendering.
- `provider_tool_definitions(...)` filters schemas by the single Tools-domain `resolve_tool_access` result (`ready_only=False` also returns allowed Tools that are not ready, which Chat uses to tell a removed Tool from a not-ready one), passing stable Definition Profile context so profile-based visibility cannot drift between native schemas, `core:tools_list`, and owner gates. Takes no Skill registry/catalog params - Tool presence is never derived from the pinned catalog.
- `update_block_definitions(...)` / `update_skill_registry(...)` refresh contributed blocks and gate-2 membership on every extension/skill reload without restart.

## Conventions

- Prompt code consumes Protocols for Agent/Tool/Skill/Channel/Storage shapes; contributions arrive as plain `BlockDefinition` objects handed in via Runtime. The manager uses the Memory domain's default renderer when no custom memory provider is injected.
- Bundled default texts are **English Markdown in resources** - a deliberate signed-off i18n exception: the System Prompt is the model-facing English contract, not localized UI string.
- `{include:filename}` reads safe flat filenames under the agent workspace through `render_prompt_file` (`prompts/prompt-files.md`). Includes do not nest: markers and runtime variables inside an included file stay literal. An **empty workspace** drops every include without reading - it must never resolve against `Path("")` (= CWD), which would read server-side files.
- Provider Tool permissions come from resolved policy plus Chat-derived grants - admitted Runs cannot inject any; `core:tools`/`core:tools_list` control only prompt-visible text, not permission. With the epoch's pinned definitions as `effective_tool_definitions`, gate 2 for `tool:<name>` is plain membership (no readiness or registry check) and `core:tools_list` lists exactly them; previews follow live policy (`chat/request-building.md` -> Tool catalog per prompt epoch).

## Constraints & Gotchas

- User overrides and per-scope layouts persist until explicitly reset; agent scopes are owned only while `custom_system_prompt_enabled` is true - the store ignores their files otherwise.
- Chat builds the prompt per request and omits the provider system message when the result is empty/whitespace-only.
- Do not move block behavior into Agent or WebUI code: Agents own only the flag and Workspace path, WebUI owns editing UX, this domain owns assembly semantics and the block contract.
- Adding a keyword parameter to `build_system_prompt` breaks every duplicated prompt-manager test double that spells out the full signature - grep `def build_system_prompt` under `tests/` and `scripts/` (two today: `tests/core/chat/chat_loop_support.py`, `tests/server/rpc_test_support_runtime.py`; the double in `scripts/probe_background_completion.py` implements only `build_system_prompt_async(**_kwargs)`). It fails only at runtime (`TypeError: ... unexpected keyword argument` in server/integration tests), not at import; update every double in the same change. Chat also calls `render_dynamic_blocks`, so each double implements it (returning `{}` contributes no dynamic block).

## Agent-facing text

This domain owns the System Prompt framing and notices `core/prompts/` renders (such as `SOUL_FRAMING`, `FILE_READ_HINT`, `FILE_TOO_LARGE_NOTICE` and `identity_runtime.md`), the Reflection and `/learn` brief fragments, and guidance sentences it renders for other domains. Before you change, add or delete a sentence of them, read `prompts/agent-facing-text.md`: it records why each sentence exists. Update its row for every sentence you change and add one for every sentence you add.

## References

Read these only when your task matches - not by default.

- Adding or changing a pinned prompt-epoch input, `pinned_context.py`, the `render_*` snapshot methods or a `BlockCatalog` -> `prompts/adding-a-pinned-input.md`
- Changing what a core, memory or data block renders (`core:runtime`, `core:soul`, `core:working_project`, `core:channels`, ...), the Runtime variables, the Producers or the bundled layout -> `prompts/core-blocks.md`
- Changing Tool-, Extension- or temporary-Session blocks (On-demand Tools, `tool:load_tools`, `tool:subagent`, `tool:bash`, `prompt_blocks`, `block_details`) -> `prompts/contributed-blocks.md`
- Changing include, SOUL or Project file reads (`render_prompt_file`, `render_project_files`, size cap, notices) -> `prompts/prompt-files.md`
- Changing `prompt.*` RPCs (block edit/layout, `user:` blocks, scopes) or `prompt.preview` -> `prompts/block-editing-and-preview.md`
- Changing Reflection, `/learn` or Librarian briefs or `BRIEF_FRAGMENT_NAMES` -> `prompts/learning-briefs.md`
