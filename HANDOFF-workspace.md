# Workspace and Rooted Identity Agent Handoff

**Status:** Discovery checkpoint. The core separation is confirmed, but the redesign is not implementation-ready because lifecycle, migration, permission, and failure behavior still need explicit decisions.

**Last updated:** 2026-07-12

## Why This Handoff Exists

The discussion began with a small Agent-editor problem: Workspace accepts only a manually typed path, while registered Projects already provide selectable paths. That exposed a deeper architectural mistake. vBot currently recognizes a Rooted Agent by making an Identity Agent's Workspace equal a registered Project's repository path. The same path therefore acts both as the Agent's private identity home and as its working directory.

That coupling makes the feature look convenient while giving Workspace two incompatible meanings. It places vBot-owned identity and Memory files in the Project repository merely so relative file and shell operations run there. The confirmed direction is to separate the Agent's freely chosen Workspace from the Project in which it works.

This handoff records the verified current behavior, the user's confirmed decisions, rejected interpretations, the qualified Subagent-address bug already fixed during the discussion, and the decisions that still need to be made before implementation.

## Communication Requirements for Continuing This Topic

Keep the discussion concise and use connected prose where possible. Do not turn every observation into a long bullet list or an implementation specification before the product behavior is confirmed.

Never use vague phrases such as "Project context" without naming the exact behavior meant: tool cwd, Project Files, Project Skills, Project Team addressing, Session storage, or permissions. These are separate concerns and must not be made to travel together merely because they all mention a Project.

Separate verified current behavior, confirmed user decisions, recommendations, and unresolved questions. Do not infer that an Identity Agent's Project belongs in its Sessions, that Workspace must be system-fixed, or that a selected Project changes how bare Agent names resolve.

## Vocabulary Guardrails

### Workspace

Workspace remains the Identity Agent's freely selectable private home. It supplies the Agent's identity and Memory material, including SOUL, USER, and MEMORY, and remains the home used by the Memory tool. A custom Workspace path is valid whether or not the Agent works in a Project.

Workspace is not the Project repository, not the cwd by definition, not a Session owner, and not a hidden alias for a selected Project.

Private Skills remain owned by the Identity Agent in its private Skills home inside the Agent's data-directory tree. They are retained across Rooting but are not stored in the freely selected Workspace path.

### Rooted Agent

**Working definition:** A Rooted Agent is an Identity Agent that keeps its own freely selected Workspace, Identity, Memory, private Skills, and Sessions while working in an explicitly selected registered Project. It receives its Workspace material and additionally receives the Project Files and Project Skills; relative file and shell work uses the Project repository as cwd.

A Rooted Agent is not a Config Agent or Project Agent. Rooting must not move its Sessions into a Project anchor, replace its identity configuration with a scanned Team profile, or make its bare Subagent targets resolve against the Project Team.

This definition is recorded here for the handoff. No new or changed Glossary entry has yet been approved by the user.

### Project, cwd, and Agent Address

The selected Project provides the repository directory used as cwd and the Project-owned material explicitly decided below. cwd is runtime working state; Workspace remains identity state.

Agent addressing is independent. `agent@project` explicitly addresses a Project Agent. A bare target from an Identity Agent, including a Rooted Agent, still addresses an Identity Agent. A bare target from a Project Agent retains the existing same-Project Team behavior. The selected Project of a Rooted Agent must never be used as an implicit namespace for bare Agent targets.

## Verified Current System

### Identity Agent ownership

An Identity Agent is persisted in the global Agent store and owns its Sessions there. Its Workspace is an absolute, user-editable path. Changing Workspace seeds any missing Workspace template material at the new location and does not move or delete files at the old location.

The Agent's Sessions remain under the Identity Agent even when the Agent is currently considered rooted. A rooted Identity run carries no Project Session scope; its Session and Run ownership remain the identity path.

### Current rooting mechanism

Rooting is currently inferred by path equality. When an Identity Agent's Workspace resolves to the cwd of a registered Project, the prompt-project resolver treats that Project as the Agent's home Project.

Because an identity Run has no Project Session scope, file and shell Tools fall back to Workspace as cwd. A currently rooted Agent therefore works in the repository only because its Workspace is the repository.

The same path is used to read SOUL and Memory material. Updating an Agent to a repository-rooted Workspace seeds missing Workspace template files there, which is the exact leakage the redesign must stop.

The path match also controls Project Files in the System Prompt, rooted Project Skills, prompt preview, Skill autocomplete, and the rule that prevents the Agent's own rooted Project from being injected again as a visited Project. These consumers must move together to the explicit Rooted-Agent Project reference; replacing only tool cwd would leave runtime and preview behavior inconsistent.

### Projects and Project Agents

A Project is a first-class entity with a stable id and a changeable repository cwd. Its Team consists of scanned Config Agents. Config Agents have no Workspace or Memory and own their Sessions under the Project anchor.

This is separate from a Rooted Identity Agent. A Rooted Agent visits or works in the Project while retaining identity ownership; it does not become a member of the scanned Team.

### Current Agent editor

The Agent editor exposes one editable Workspace text field. Registered Projects are not offered there. The server assigns the default Workspace at creation, while editing can replace it with any absolute path. A custom Workspace can be reset to the default, and files at the previous location remain untouched.

### Current qualified Subagent-address bug and fix

During this discussion, source verification found that the public `agent@project` address contract was implemented at RPC and Command boundaries but missing from the `subagent` and `subagent_result` Tools. Those Tools treated the argument as a bare id and only inherited the parent Run's Project scope.

This existing bug is already fixed in commit `c0a16770` (`fix(subagents): support qualified project targets`). `subagent` and `subagent_result` now accept qualified targets such as `orchestrator@vbot`. A qualified target uses its explicit target Project for child validation, Session and Run storage, queueing, cancellation, result lookup, live events, and navigation. Parent Session ownership and automatic parent continuation remain in the parent scope. Bare targets preserve their previous identity-versus-same-Project behavior exactly.

The fix deliberately does not use Workspace, cwd, rooted-project detection, Project Files, or Project Skills for Agent-name resolution. It is complete and must not be reimplemented as part of the Workspace redesign.

## Confirmed User Decisions

1. Workspace and Project are separate concepts. Workspace must mean the Agent's own Workspace rather than the repository in which it happens to work.
2. Workspace remains freely editable by the user. It is not a fixed or read-only system path.
3. Workspace remains freely editable when the Agent is rooted. Selecting a Project does not disable, replace, or silently reset the Workspace field.
4. A Rooted Agent is an Identity Agent. It keeps its own identity configuration, Workspace, Memory, private Skills, and Sessions.
5. The Sessions of a Rooted Agent belong to the Identity Agent. The selected Project must not be stored as Session ownership or move those Sessions into the Project anchor.
6. A Rooted Agent receives its Workspace files. In particular, Rooting must not make SOUL, USER, or MEMORY disappear merely because the Tools work elsewhere. Its separately stored private Skills remain available because it remains the same Identity Agent.
7. A Rooted Agent additionally receives the selected Project's Project Files and Project Skills.
8. Relative file and shell operations of a Rooted Agent work in the selected Project repository rather than in the Workspace.
9. vBot-owned Identity and Memory files must no longer be written into the Project repository merely to establish Rooting. Normal Agent work may still edit Project files through Tools.
10. The selected Project does not affect ordinary Agent addressing. A bare Subagent target from a Rooted Identity Agent remains an Identity Agent target; `agent@project` is the explicit way to address a Project Agent.
11. The existing qualified Subagent-address contract belongs in the Subagent Tools and has now been fixed independently of this redesign.

## Rejected Directions

### Registered Project as a Workspace shortcut

The original idea of inserting a registered Project's path into Workspace is superseded. It would preserve the current semantic collision and continue placing Workspace-owned vBot files in the repository.

### Fixed or read-only Workspace

The suggestion that every Identity Agent must use only a server-assigned Workspace inside its Agent directory was explicitly rejected. "Own Workspace" means owned by the Agent and separate from the Project, not immovable or unselectable by the user.

### Project stored on Identity Sessions

The suggestion to copy a selected Project into each Identity Session was explicitly rejected. It confused working location with Session ownership. Rooting belongs to the Identity Agent, while its Sessions remain its own.

### Root Project as an implicit Agent namespace

The suggestion that a bare Subagent name should resolve against a Rooted Agent's selected Project was explicitly rejected. The Project repository concerns where the Agent works; it does not rewrite normal Agent addressing.

### Project-only Workspace material

Any definition of Rooted Agent that can be read as replacing or omitting its Workspace files is wrong. The Agent receives both its private Workspace material and the separately selected Project material.

## Confirmed Target Behavior

The Agent configuration has two independent user-facing choices: an editable Workspace path and an optional selected registered Project. The Project selector is populated from the current Project list. Selecting or clearing a Project never rewrites the Workspace value.

Without a selected Project, an Identity Agent behaves as it does at home: its identity and Memory come from Workspace, its Sessions belong to it, and relative Tools use Workspace as cwd.

With a selected Project, the same Identity Agent becomes rooted: identity and Memory still come from Workspace; Sessions still belong to the Agent; Project Files and Project Skills are added; relative Tools use the Project repository as cwd. The two locations may be anywhere and are not required to be related.

The essential behavior matrix is:

| Agent kind | Workspace | Session owner | Relative Tool cwd | Identity and Memory source | Project Files and Skills | Bare Subagent target |
|---|---|---|---|---|---|---|
| Identity Agent at home | Freely selected | Identity Agent | Workspace | Workspace | None | Identity Agent |
| Rooted Identity Agent | Freely selected | Identity Agent | Selected Project repository | Workspace | Selected Project | Identity Agent |
| Project Config Agent | None | Project | Project repository | None | Project | Same Project Team |

## Concrete Examples

If an Identity Agent has Workspace `D:/agents/alice-home` and selects the registered `vBot` Project at `C:/Development/projects/vBot`, SOUL, USER, and MEMORY remain under `D:/agents/alice-home`; private Skills remain in Alice's Agent-owned private Skills home. Its Sessions remain owned by Alice. Relative Tools work in `C:/Development/projects/vBot`, and the vBot Project Files and Project Skills are available.

Changing Alice's Workspace while she remains rooted changes where her identity and Memory material live. It does not change the Project repository used by relative Tools.

Clearing Alice's selected Project leaves Workspace and Sessions untouched and makes relative Tools work in Workspace again.

From Alice, `researcher` continues to mean the Identity Agent named `researcher`. `orchestrator@vbot` explicitly means the `orchestrator` Config Agent in the `vBot` Project. Alice's selected Project is irrelevant to that address distinction.

## Required Architectural Separation

The implementation must retain separate values for identity Workspace, Session/Run ownership scope, working cwd, selected Root Project, Skill scope, and explicit Subagent target scope. Reusing one existing `project_id` for all of these would recreate the same bug under a different name.

For an Identity Run, Session and Run ownership remain identity-scoped even when the Agent is rooted. The selected Project is used only by the consumers that need its repository or Project-owned material. The qualified Subagent target continues to carry its own independent target Project.

The existing shared prompt-project and Skill-scope policies must be revised to use the explicit Agent-level Project reference instead of Workspace path equality. Chat execution, compaction rebuilds, prompt preview, Skill autocomplete, and visiting-project suppression must continue to agree through shared policy rather than separate UI or RPC guesses.

The Tool layer must continue receiving Workspace for Memory and identity-owned behavior while receiving a separate cwd for relative file and shell resolution. File mentions and other cwd-oriented browsing must follow the same cwd rule as Tools.

## UI Direction

The Agents view should keep the editable Workspace field and add a separate optional Project selector using the registered Project list. The selector must make the distinction visible: Workspace is the Agent's private home; Project is where it works.

The exact layout, labels, help text, creation flow, loading/error behavior, and whether the current Project path is shown as secondary text have not been approved. No UI should imply that choosing a Project changes Workspace.

The initial request to select current Projects "as Workspace" should therefore be implemented as a separate Project selection, not as Project paths inserted into the Workspace field.

## Open Product Decisions

1. **Agent-wide change behavior:** Because the selected Project belongs to the Agent and not its Sessions, changing it would affect future Runs in all of that Agent's existing Sessions. This consequence follows from the confirmed ownership model but has not yet been explicitly approved.
2. **Project removal:** Decide whether removal is blocked while Rooted Agents reference the Project, whether the user must explicitly unroot them first, or whether another explicit recovery flow is offered. Silent unrooting is not approved.
3. **Project re-pointing:** Decide whether Rooted Agents follow the Project's stable identity to its new cwd automatically. This is the natural consequence of storing a Project reference rather than a path, but it has not been explicitly approved.
4. **Missing or invalid Project:** Decide what the Agent and UI do when the selected Project exists in configuration but its repository is missing or its Project config cannot load. Falling back silently to Workspace would hide a material context change.
5. **Agent creation:** Decide whether a Project may be selected during Agent creation or only after creation, and whether the default is always no Project.
6. **Project visibility:** Decide how the Agent list and Chat identify a Rooted Agent's selected Project, if at all. No badge, filter, or navigation behavior has been approved.
7. **Permissions:** Decide whether a Rooted Identity Agent keeps only its own Tool and Skill permissions, whether Project Skill enable/disable rules also apply, and whether any Project Ceiling participates. The earlier recommendation that Project Tool Ceiling remains Config-Agent-only was not confirmed by the user.
8. **Project Team discoverability:** Explicit `agent@project` addressing works. Whether the Rooted Agent's prompt or Tool description should proactively list the selected Project's Team is separate and has not been discussed.
9. **Workspace relocation:** Current Workspace updates repoint and seed without moving old files. Decide whether the redesign preserves that exact behavior and how the UI warns about identity/Memory remaining at the previous location.
10. **Terminology and labels:** Decide whether the persisted/user-facing field is named `Project`, `Root Project`, or something else. The conversation used Rooted Agent as a concept but did not approve final UI copy or a Glossary entry.

## Open Migration Decisions

Existing Rooted Agents are represented only by `workspace == registered Project cwd`. The redesign needs an explicit conversion strategy because merely adding a Project reference would leave their identity files in the repository.

The project forbids permanent application-level legacy compatibility and automatic schema migrations. Any conversion should therefore be an explicit standalone operation or an intentional development data reset, not a forever fallback in runtime code.

The conversion must decide how to handle SOUL, USER, and MEMORY currently living in the Project repository; how to choose the Agent's new freely editable Workspace; what to do when destination files already exist; and whether files are copied, moved, or left for manual cleanup. No behavior here has been approved. Never overwrite or delete identity material based on an inferred match without an explicit conflict policy.

Custom Workspaces that do not match a registered Project are already valid and must remain valid. They need no inferred Project association.

## Expected Risks and Failure Modes

1. **Partial separation:** Moving only Tool cwd while leaving Project Files, Skills, preview, mentions, or visiting logic keyed to Workspace path equality would create different effective environments across surfaces.
2. **Session-scope regression:** Reusing the existing Run/Session Project scope for a Rooted Identity Agent would relocate or misaddress Sessions and could make the Agent resolve as a Config Agent.
3. **Address-scope regression:** Feeding the selected Root Project into normal Subagent target resolution would make bare Identity Agent calls unexpectedly hit Project Team members.
4. **Identity leakage:** Seeding or reading SOUL, USER, or MEMORY from the Project repository would preserve the bug even if the UI showed separate fields.
5. **Silent fallback:** If a selected Project is missing, silently running in Workspace could cause Tools to modify the wrong directory while the Agent believes it is rooted.
6. **Path lifecycle drift:** A Project can be re-pointed and a Workspace can be independently changed. Storing copied repository paths in the Agent instead of a stable Project reference risks stale rooting.
7. **Deletion drift:** Removing a referenced Project without a reference guard or explicit recovery flow can leave Agents in an ambiguous rooted state.
8. **Prompt/cache drift:** Project Files must remain in the same established prompt position and be resolved consistently for normal builds, compaction rebuilds, and preview. Accidental ordering or scope changes can alter prompt caching and Agent behavior.
9. **UI ambiguity:** A path picker labeled Workspace that also contains Projects would invite users and future agents to recreate the old coupling.

## Implementation Impact to Investigate Before Planning

This section identifies affected behavior, not a build plan.

- Agent persistence and validation need an optional explicit Project reference while preserving a freely editable absolute Workspace.
- Rooted-project resolution must stop inferring ownership from Workspace path equality and instead read the Agent's explicit selection.
- Chat and Tool dispatch need the selected Project repository as cwd without setting the Identity Session's Project ownership scope.
- System Prompt assembly, compaction rebuilds, prompt preview, Skill catalog/autocomplete, and visiting-project suppression must share the new rooted-project policy.
- File mention listing and any other cwd browser must match Tool cwd.
- Agent CRUD responses and the Agents UI need the Project list and selected value without treating Project paths as Workspace values.
- Project update/removal flows need reference-aware behavior after the lifecycle decision is approved.
- Existing rooted data requires an explicit converter or reset strategy after the migration decision is approved.
- Tests must prove the full matrix above, including independent Workspace changes, Project changes, Session ownership, prompt material, Skills, cwd, visits, mentions, and Agent addressing.
- The Agent, Projects, Chat, Prompts, Skills, Tools, Memory, Sessions, Server, and WebUI domain maps may require factual updates when implementation occurs. Domain-map work must follow the mandatory workflow immediately before editing.

## Acceptance Scenarios for the Future Design

These scenarios encode confirmed behavior and should eventually become tests:

1. A home Identity Agent reads identity and Memory from its custom Workspace, stores Sessions as an Identity Agent, and resolves relative Tools in Workspace.
2. The same Agent selects a Project and becomes rooted without changing Workspace or moving Sessions.
3. The Rooted Agent reads SOUL and Memory from Workspace, receives Project Files and Project Skills, and resolves relative Tools and file mentions in the Project repository.
4. Changing Workspace while rooted changes only identity/Memory location; Project working location remains unchanged.
5. Clearing the Project returns relative Tool cwd to Workspace without moving or replacing Sessions.
6. A Rooted Agent calling a bare Identity Agent target continues to reach that Identity Agent.
7. A Rooted Agent calling `agent@project` reaches the explicit Project Agent through the already-fixed qualified Subagent contract.
8. A Project Agent calling a bare Team member retains the existing same-Project behavior.
9. A different registered Project reached by absolute file access still triggers the visiting-project reminder, while the Rooted Agent's own selected Project is not injected twice.
10. Prompt preview, live Run, and post-compaction rebuild expose the same Workspace and Project material.

## Source Evidence

- `core/agents/agents.py` and `.vorch/domain-maps/agent.md`: editable absolute Workspace, default Workspace, seeding behavior, Agent-owned Sessions, and identity fields.
- `core/projects/resolver.py`, `core/projects/store.py`, and `.vorch/domain-maps/projects.md`: path-equality rooting, Project cwd identity, prompt-project and Skill-scope policies, Config Agent resolution, and Project anchors.
- `core/chat/chat.py`, `core/chat/tool_dispatch.py`, and `.vorch/domain-maps/chat.md`: identity versus Project Run scope, cwd resolution, prompt-project material, Skills, compaction rebuilds, and visiting-project suppression.
- `core/prompts/` and `.vorch/domain-maps/prompts.md`: SOUL and Memory read from Workspace, Project Files block, prompt preview, and Workspace placeholder/include behavior.
- `core/memory/` and `.vorch/domain-maps/memory.md`: USER and MEMORY ownership and the Workspace-backed Memory tool.
- `core/chat/file_mentions.py` and `server/rpc/catalog_methods.py`: mention-root resolution and rooted-aware Skill autocomplete.
- `webui/src/components/agents/AgentEditor.svelte`, `webui/src/components/AgentsView.svelte`, and their tests: current editable Workspace field, default reset, and available catalog loading.
- Commit `c0a16770`, `core/subagents/`, `core/tools/subagent.py`, and `.vorch/domain-maps/subagents.md`: completed qualified `agent@project` support with parent/target scope separation.

## Recommended Next Discussion Order

This is a discussion order, not an implementation plan.

1. Confirm the consequence of an Agent-level Project change for all existing Sessions' future Runs.
2. Decide Project removal, re-pointing, and missing-repository behavior as one lifecycle contract.
3. Decide Rooted Identity Agent Tool/Skill permission composition and Project Team discoverability.
4. Decide the explicit conversion strategy for existing path-rooted Agents and their identity files.
5. Approve the Agent-editor creation/editing behavior and exact user-facing labels/help text.
6. Update the Glossary definition only after the term is confirmed, then produce an implementation plan from the settled end state.
