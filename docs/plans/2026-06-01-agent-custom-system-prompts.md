## Plan: Agent Custom System Prompts

**Goal:** Users can enable a custom system-prompt scope per Agent, edit the same prompt fragments available in the default System Prompt UI, and have chat/preview rendering use the Agent scope only when that Agent explicitly enables it.

**Context:** Today vBot has one global editable system prompt rooted in `<data_dir>/prompts/`, with bundled-resource fallback from `resources/prompts/`. Agents already live under `<data_dir>/agents/<agent-id>/`, and the desired behavior is to let a user opt one Agent into a private prompt scope under that Agent directory. The custom scope is user-controlled: the Agent does not edit it autonomously. Enabling custom prompts seeds all editable fragments from the currently effective default prompt; after that, the user may delete text, leave fragments empty, or omit placeholders from `system.md` so runtime/tool/channel/skill sections are not included. Tool availability remains governed by `allowed_tools`, not by whether `{tools}` appears in prompt text.

**Requirements:**
- Add a per-Agent persisted toggle in `agent.json` for custom system prompts.
- Agent Settings exposes only a custom prompt on/off toggle.
- Enabling custom prompts copies all currently effective default editable prompt fragments into `<data_dir>/agents/<agent-id>/prompts/`.
- Disabling custom prompts ignores the Agent prompt files without deleting them.
- The System Prompt tab scope selector contains only `Default` and Agents with custom prompts enabled.
- Everything currently editable in the System Prompt tab is also editable for an enabled Agent scope.
- In an Agent scope, `system.md` is the root and may be empty.
- Optional Agent fragment files such as `skills.md` may be absent; the UI shows an empty editor, and saving creates the file.
- A custom `system.md` only includes runtime/tools/channels/skills when the user includes the corresponding placeholder.

**Scope:**
- In: Agent schema/RPC support, Agent prompt fragment file storage, prompt assembly with custom Agent scopes, prompt editor scope switching, Agent settings toggle, tests, and `.vorch` docs/spec updates.
- Out: Changing tool permission semantics, adding new prompt fragment names, making `compaction.md` Agent-specific, adding new dependencies, or building a separate prompt-template marketplace/import flow.

**Assumptions & Constraints:**
- Initial enable seeds all five editable fragments: `system.md`, `runtime.md`, `tools.md`, `channels.md`, and `skills.md`.
- Re-enabling after disable should preserve existing Agent prompt files instead of overwriting user work; use a reset action if the user wants to refresh from defaults.
- Custom prompt assembly must not fall back to default fragments when a custom Agent fragment is missing; missing custom fragments render as empty strings.
- `system.md` being empty is valid and should render an empty system prompt.
- Existing provider tool schemas still follow `allowed_tools`; hiding `{tools}` from the prompt is presentation/context control, not permission control.
- Follow existing no-legacy-compatibility conventions: the current `agent.json` schema should include the new field, with tests updated accordingly.

### Milestones

| # | Milestone | Deliverable |
|---|---|---|
| M1 | Backend model and storage | Agent configs can persist the custom toggle; Agent prompt files can be seeded, read, written, reset, and rendered. |
| M2 | RPC contract | Prompt and Agent RPCs expose custom scopes and seed fragments when the toggle is enabled. |
| M3 | WebUI | Agent Settings has the toggle; System Prompt tab switches between Default and enabled Agent scopes and edits the selected scope. |
| M4 | Verification and docs | Backend/frontend tests and relevant `.vorch` specs reflect the new behavior. |

### Phase Breakdown

#### Phase 1: Agent Schema And Scoped Prompt Storage
**Goal of this phase:** Persist the custom-prompt toggle and provide safe file operations for Agent-scoped prompt fragments.
**Can run in parallel with:** none

- Add `custom_system_prompt_enabled: bool` to the Agent model, create/update validation, JSON validation, and RPC payload shape - read: [.vorch/specs/agent.md, .vorch/specs/settings.md], files: [core/agents/agents.py, core/settings/validation.py, server/rpc/agent_methods.py, server/rpc/payloads.py, tests/core/agents/test_agents.py, tests/core/settings/test_settings.py, tests/server/test_rpc.py]
- Add storage support for Agent prompt fragment directories, including seeding all editable fragments from the currently effective default scope, reading missing Agent fragments as empty, writing optional fragments, and reset semantics for Agent scope - read: [.vorch/specs/storage.md, .vorch/specs/prompts.md], files: [core/storage/storage.py, tests/core/storage/test_storage.py]
- Extend prompt-domain tests/stubs so custom-enabled Agents expose the new field and scoped fragment behavior can be tested without coupling prompt logic to concrete storage classes - read: [.vorch/specs/prompts.md], files: [core/prompts/prompts.py, tests/core/prompts/test_prompts.py]

**Dependencies:** Existing prompt fragment allowlist and Agent ID validation.
**Done when:** Core tests show Agent configs round-trip the toggle, unsafe Agent fragment paths are rejected, enabling can seed all editable fragments, missing custom fragments read as empty, and default prompt behavior is unchanged.

#### Phase 2: Prompt Assembly And RPC Scope Contract
**Goal of this phase:** Make prompt assembly and prompt RPCs aware of Default vs enabled-Agent scopes.
**Can run in parallel with:** none

- Update `SystemPromptManager` so `build_system_prompt(agent)` uses Agent-scoped `system.md` only when `agent.custom_system_prompt_enabled` is true; placeholder expansion should be lazy so omitted `{runtime}`, `{tools}`, `{channels}`, or `{skills}` placeholders do not load or inject those blocks - read: [.vorch/specs/prompts.md, .vorch/specs/chat.md], files: [core/prompts/prompts.py, tests/core/prompts/test_prompts.py]
- Extend `PromptFragmentManager` with explicit prompt scopes: default scope uses current global behavior; Agent scope validates the Agent is custom-enabled, lists all editable fragment names, returns empty content for missing Agent files, writes Agent files on save, and resets Agent fragments to the currently effective default content - read: [.vorch/specs/prompts.md], files: [core/prompts/prompts.py, tests/core/prompts/test_prompts.py]
- Extend prompt RPCs so `prompt.list`, `prompt.update`, `prompt.reset`, and `prompt.preview` accept an optional scope payload while preserving default no-param behavior; include available scopes in the list response or add a focused scope listing response if that better fits existing dispatcher patterns - read: [.vorch/specs/server.md, .vorch/specs/prompts.md], files: [server/rpc/operations_methods.py, tests/server/test_rpc.py]
- Wire Agent toggle enable behavior in the server Agent update path: when `custom_system_prompt_enabled` transitions false to true, seed all editable Agent fragments from the current default scope before returning the updated Agent response; when false, leave files untouched - read: [.vorch/specs/server.md, .vorch/specs/agent.md], files: [server/rpc/agent_methods.py, tests/server/test_rpc.py]

**Dependencies:** Phase 1 storage and Agent schema work.
**Done when:** RPC tests prove default prompt endpoints still work, Agent scope endpoints reject disabled/unknown Agents, enabled Agent scopes list/edit/reset fragments, preview renders custom empty `system.md` as empty text, and chat prompt assembly uses the same custom rendering path as preview.

#### Phase 3: WebUI Agent Toggle And Prompt Scope Editing
**Goal of this phase:** Let users toggle custom prompts per Agent and edit Default or enabled-Agent scopes from the System Prompt tab.
**Can run in parallel with:** none

- Add `custom_system_prompt_enabled` to frontend Agent form normalization, dirty-field filtering, create/edit defaults, and helper tests - read: [.vorch/specs/webui.md, .vorch/DESIGN.md], files: [webui/src/lib/agentForm.js, webui/src/lib/__tests__/agentForm.test.js]
- Add a compact switch in the Agent Settings/Agents detail view for `Custom system prompt`; saving the toggle should use existing `agent.update` auto-save behavior and reload/update local Agent state from the server response - read: [.vorch/specs/webui.md, .vorch/DESIGN.md], files: [webui/src/components/AgentsView.svelte, webui/src/components/__tests__/AgentsView.test.js]
- Rework `SystemPromptView` loading state around prompt scopes: load Agents and prompt scopes, show a scope selector containing `Default` plus enabled custom Agents only, request fragments for the selected scope, and pass that scope to save/reset/preview RPCs - read: [.vorch/specs/webui.md, .vorch/specs/prompts.md, .vorch/DESIGN.md], files: [webui/src/components/SystemPromptView.svelte, webui/src/components/__tests__/SystemPromptView.test.js]
- Update UI strings for scope labels, custom toggle labels, Agent-scope reset confirmation, and any empty-state text through the i18n map - read: [.vorch/specs/webui.md], files: [webui/src/lib/i18n.js, webui/src/lib/__tests__/i18n.test.js]

**Dependencies:** Phase 2 RPC contract.
**Done when:** Frontend tests show the Agent toggle sends `custom_system_prompt_enabled`, the System Prompt selector excludes disabled Agents, Agent-scope saves include scope parameters, missing fragment content displays as empty, and preview calls use the selected scope behavior.

#### Phase 4: Specs And Quality Gates
**Goal of this phase:** Keep project memory current and verify the completed change.
**Can run in parallel with:** none

- Update prompt, storage, Agent, server, and WebUI specs with the custom-prompt scope contract, file layout, enable/disable behavior, and the distinction between prompt text and tool permissions - files: [.vorch/specs/prompts.md, .vorch/specs/storage.md, .vorch/specs/agent.md, .vorch/specs/server.md, .vorch/specs/webui.md]
- Run focused backend and frontend quality gates while iterating, then run full applicable gates before completion - files: [scripts/quality.py, scripts/quality-frontend.py]

**Dependencies:** Phases 1-3 complete.
**Done when:** `python scripts/quality.py` and `python scripts/quality-frontend.py` pass, and docs describe the final persisted schema, RPC parameters, and UI behavior.

### Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Prompt text and tool permissions are conflated in UI copy | Medium | Medium | Keep copy and docs explicit: `{tools}` controls prompt context only; `allowed_tools` controls callable tools. |
| Re-enabling custom prompts overwrites user edits | Medium | High | Seed only when creating the Agent prompt scope or when files are missing; never overwrite existing Agent prompt files during normal toggle-on. |
| Missing Agent fragments accidentally fall back to default content | Medium | High | Add tests where custom `system.md` references a missing `{skills}` fragment and assert the rendered block is empty. |
| Scope selection and preview target become ambiguous | Medium | Medium | Treat the scope selector as the edited prompt source; for custom Agent scope preview that Agent, and for Default scope preserve a default preview Agent selection if needed by runtime placeholders. |
| Existing prompt RPC tests break due to new params | Low | Medium | Preserve no-param default behavior for `prompt.list`, `prompt.update`, `prompt.reset`, and current preview calls. |

**Done when:**
- An Agent response includes `custom_system_prompt_enabled` and `agent.update` can toggle it.
- Toggling on seeds `<data_dir>/agents/<agent-id>/prompts/{system,runtime,tools,channels,skills}.md` from the currently effective Default scope.
- Toggling off leaves those files untouched and chat/preview uses Default scope again.
- System Prompt UI scope selector shows only `Default` plus enabled custom Agents.
- Agent-scoped editors show all editable fragment names; missing files show empty content and are created on save.
- Custom `system.md` may be empty and may omit placeholders without hidden default fragment injection.
- Backend and frontend quality gates pass.

**Final size:** Medium. This touches multiple subsystems and UI behavior, but it extends existing Agent, prompt, storage, RPC, and Svelte patterns without adding a new subsystem or dependency.

**New Dependencies:** None.
