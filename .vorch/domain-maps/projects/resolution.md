# Project Agent Resolution

Read this reference when changing how a Project Agent becomes effective runtime configuration, including fallback order, provenance, model usability, capability ceilings, working-Project helpers, or which instruction files Project Context loads.

## Uniform Resolution Seam

`core/projects/resolver.py` owns `AgentResolver.resolve_agent(project_id | None, agent_id)`, the common boundary used by Run-producing paths. With no Project id it returns the stored Identity Agent; with a Project id it verifies Team membership and synthesizes a `ConfigAgent` from repository and Project state. Callers should not reproduce this branch or assemble Project Agent configuration themselves.

Resolution reads Agent configuration, may seed a workspace or normalize the current Session, and can wait for the Agent store's write lock. Event-Loop callers therefore use `resolve_agent_async` / `resolve_temporary_agent_async`; Chat Run admission and execution, Subagent spawns, and Reflection reviews resolve this way. Synchronous callers already on a worker or outside a Run path keep the plain methods.

The async variants follow the per-database pool rule (`database.md`). An Identity Agent read, which may seed its workspace and verify or repair its current-Session pointer, runs as one unit on the Session database's pool (`AgentStore.get_async`), and so does a temporary Session binding read (`TemporaryAgentRegistry.resolve_async`); a closed Session database raises `DatabaseUnavailableError`. Reading a Session's Agent overrides is one read on the Session database's pool too. Project Agent resolution, applying Session overrides and a temporary Agent's Project check stay on the bounded `agent-resolution` pool: they read Project, repository and configuration files and check Model usability. Their only database access is incidental: the first resolution in a Project whose Team is not cached yet runs the scan report, which lists the Project's Session-owning Agents (`ProjectStore.session_owning_agents`) in the middle of the repository scan. Splitting that one read per Team cache fill onto the Session pool would cut the scan into two hops for no gain, so it deliberately stays (`tests/core/projects/test_resolver_scan_identity.py`, `test_resolver_config_agent.py`).

Team membership and the winning definition are cached per Project. Each resolution rereads only that definition and its inherited repository settings; it does not walk or translate unrelated standalone Agents. Edits to model, instructions, Tool denials, Agent-target rules, or scalar settings take effect without a Team rebuild. New competing definitions take precedence on the next normal Team rescan or invalidation. If the selected definition disappears or changes id, resolution falls back to the ordered live search, including Source path scopes; malformed or unreadable selected metadata remains unavailable instead of falling through to a shadowed definition. Adapter-specific shared validation is in `scanning.md`.

Resolution failures are `AgentResolutionError`. A missing address part keeps its precise kind: an unknown Identity Agent, an Agent that is not on the cached Team, or a Team member whose repository source vanished raises `ResolutionAgentNotFoundError`, and an unknown Project raises `ResolutionProjectNotFoundError`; both subclass `AgentResolutionError`, so callers that treat every resolution failure alike keep catching it. Ids are exact, so a case variant such as `Builder@VBot` is one of these not-found failures. Other failures (no usable Model, an unavailable temporary Session, a Project id disagreeing with its Anchor) stay plain `AgentResolutionError`. `effective_config()` and temporary Project application use the same wrapping. RPC maps the two not-found kinds to `agent_not_found` / `project_not_found` (`server.md`).

### Session Agent overrides

A Session Model wins before the lower Model chain is required to produce a usable default, for both existing Sessions and `new_session_overrides`. Run admission refuses an unusable explicit Session Model instead of falling back. Administration and Command catalogs use `require_model=False` on the same resolver seam: owner/configuration validation remains, Session Model overrides retain their configured id without requiring availability, and an exhausted Project Agent default chain still returns an empty Model. This keeps listing, deletion, read acknowledgement, forking (both endpoints), Compaction Policy changes and `/model` discovery available for recovery.

A Session can override its Agent's `model`, `thinking_effort`, `temperature` and `top_p` (`AGENT_OVERRIDE_FIELDS`, frozen `AgentOverrides` in `_runtime_agent.py`). The resolver owns them end to end: `resolve_agent(..., session_id=...)`, `resolve_agent_async(..., session_id=...)` and the temporary variants' `session=` read the Session's stored overrides and return a replaced runtime dataclass; they never mutate the stored Identity Agent, repository Agent, Project overrides or other Sessions. Without a Session, for a Session that does not exist yet, or with nothing stored, resolution is the ordinary path. `new_session_overrides=` (exclusive with `session_id`) resolves the Agent as the first Run of a Session that does not exist yet would run it, so a new Session's override Model is checked before the Session is created. `effective_config(..., session_id=...)` reports an overridden field with source `session`.

New Chat and explicitly created Sessions capture their resolved Model and thinking effort in their creating metadata write (`AgentOverrides.capture`). Existing Sessions used by Chat capture any missing values before admission. The additive `thinking_effort_default: true` flag captures the Provider default independently of later Agent edits; new input cannot combine it with an explicit effort, and it resolves to `""`. A stored explicit effort wins if an older writer retained the then-unknown flag. Missing fields retain the former inheritance semantics; no persisted-format conversion is required. `capture_session_settings` fills only uncaptured fields and preserves unknown stored keys. Before composer/default edits, `preserve_agent_sessions` captures missing values of the Agent's existing live, non-owner-managed Sessions without requiring a usable Model. `remember_session_settings` saves the current Session's Model/effort and the same start values on its Identity Agent or Project Agent override. Reading a Session never writes those defaults. Temperature and top_p remain ordinary optional overrides.

`update_session_overrides(address, changes)` (async: `update_session_overrides_async`) updates explicit choices: unknown fields raise `ValueError`, values validate like Agent settings, a Model must pass `require_configured`, `None` clears one field, other fields and keys an unknown newer version stored survive, and an empty result removes the stored value. Storage is the Session metadata key `agent_overrides` (`sessions/lists-and-metadata.md` -> Metadata facade), so forks inherit it. A stored Model that can no longer run fails that Session's resolution with `ModelConfigurationError` instead of silently falling back.

Writers: `session.create` / `session.set_agent_overrides` (`server/session-agent-rpcs.md`), a new Session's creating write from `chat.send`/`chat.stream` `new_session.agent_overrides` or a command that creates the Session (`AgentOverrides.session_metadata()`; `vbot chat` uses `new_session` for a new Session and `session.set_agent_overrides` for an existing one), Sub-Agent spawns (`subagents.md`), and `/model`, which preserves other live Sessions, writes the Agent's Model and captures the resulting Model on the current Session (`chat/commands.md`). Every Run producer (Chat admission and execution, Queue, Compaction, Sub-Agents, `/status`, Tool status, Extension Tool Agents) resolves with the Run's Session, so a continuation keeps its Session's Model and the Provider prompt cache.

### Librarian Skill subject

A Session of the built-in Librarian (`agent/builtin-agents.md` -> Built-in Librarian) is bound to the Agent whose Skills it maintains by the Session metadata keys `skill_agent_id` (`SKILL_AGENT_ID_KEY`) and `skill_agent_created_at` (`SKILL_AGENT_CREATED_AT_KEY`), the original subject's immutable creation timestamp. New pass admission captures the persisted creation identity through `AgentStore.capture_creation_identity`, which establishes a missing legacy timestamp once; read-time defaults never become binding evidence. Resolution requires both values to match the current Identity Agent. Rename retargets the id while preserving the timestamp; restoring the original Agent preserves the binding, but creating a replacement under the same id does not. A bound Session without a nonempty timestamp is refused without rewriting its metadata: its history stays readable, and a new Librarian pass creates a verified binding. After the Session's overrides, `resolve_agent`/`resolve_agent_async` with that Session return the Librarian with `skill_agent_id` set to the subject and the subject's `allowed_skills`; Tools, Model settings, Memory mode and everything else stay the Librarian's. The binding is read only for an Identity Agent that `is_librarian`, so another Agent's Sessions, an unbound Librarian Session and Session-less resolution are the ordinary path. A subject that is no Identity Agent of the user any more (missing, replaced, or a built-in Agent) fails the resolution with `AgentResolutionError` (`SKILL_SUBJECT_MISSING_MESSAGE`: `This Session works on the Skills of Agent <id>, which no longer exists, so it cannot continue.`). `resolve_skill_scope(project_id, prompt_project, agent)` takes the resolved Agent and returns `skill_subject_id(agent)` as the identity layer, so the Run's Skill registry and pinned catalog, triggers, Compaction's catalog refresh, Tool dispatch (`ToolContext.skill_agent_id`, `tools/skill.md`), `chat.commands` and `prompt.preview` resolve the subject's Skills. Behavior: `test_resolver_connections.py::test_a_librarian_session_runs_on_the_skills_of_its_bound_agent`, `test_resolver_scan_identity.py::test_resolve_skill_scope`, `tests/core/chat/test_chat_integration.py::test_a_librarian_session_works_on_the_skills_of_its_agent`.

The admission refusal `This Librarian Session has no verified Agent identity. Its history remains available; start a new Librarian pass to continue.` explains why an older bound Session cannot continue and gives the supported recovery without suggesting an identity backfill from a possibly reused id. It adds no recurring Model prompt text.

## Model & Scalar Resolution

The model chain is:

```text
per-Agent override -> repository Agent -> Project default -> global default -> error
```

Each model tier must pass the same `ModelConfigurationChecker` before it can win. The checker validates the Provider, Model catalog entry, and an allowed usable Connection, including an explicitly pinned account suffix. `is_configured` supplies the boolean fallback/scan decision; `require_configured` and `AgentResolver.require_model_configured` expose the same invariant as a raising mutation seam for Chat `/model` and Project Agent overrides. A forbidden pin names the rejected Connection and the Model's allowed Connections; other failures use the general unusable-Model diagnostic. A syntactically present but unusable Model falls through to the next tier; if no usable Model exists, resolution fails rather than constructing a broken Agent.

An explicit Run Model does not add another fallback tier: it replaces the already resolved primary Model for that Run and must pass `ModelConfigurationChecker.require_configured` directly. It does not alter the resolved fallback Model or any non-Model field. The Run thinking effort accepts the same canonical values as Agent/Project settings, including `""` for Provider default; Model-specific snapping, toggle, and budget rendering remain Provider policy.

Repository Model wishes pass through `Project.model_mappings`; already-usable qualified Models also resolve directly. Unmapped/unusable wishes use Project/global fallback with a translation finding. `inherit` uses the delegating caller's Model, recorded in the Sub-Agent Session override; direct starts use defaults. A vBot Model override still wins. `resolve_delegated_agent_async` applies inheritance before requiring a default Model.

Temperature, top_p and thinking effort use:

```text
per-Agent override -> repository Agent -> Project default -> global default -> Provider default or None
```

`""` (effort) and `0.0` (sampling) are real stopping values, not absent ones (`_resolution_values.py`).

`effective_config()` exposes the chosen value and provenance (`override`, `agent`, `project_default`, `global_default`, or `null`) for model, temperature, top_p, and thinking effort. Preserve those labels as an API/UI contract when changing fallback behavior.

Compaction policy and `tool_loading` (On-demand Tools) are supported per-Agent Project overrides passed into the synthesized `ConfigAgent` (`test_resolver_config_agent.py`; `tool_loading` with each missing part filled from `defaults.agent.tool_loading`, `inherit_tool_loading`), but they are not among the fields in the current effective-config provenance result. Do not imply provenance coverage until that contract is deliberately extended end to end.

### Permission Rules

`RuntimeAgent.permission_rules` (GLOSSARY -> Permission Rules; the rule language: `../permissions.md`) resolves per part with `resolve_rule_set`, most specific level first, then `defaults.agent.permission_rules` (`resolve_permission_rules`, `_resolution_values.py`):

```text
Identity Agent:   agent.json -> global default
Team member:      Project override -> (global default restricted by the repository profile)
Temporary Agent:  its owner's TemporaryAgentConfig rules (+ permission_ceiling)
Temporary member: owner's rules -> Project override -> (global default restricted by the repository profile)
```

The repository profile's rules (`scanning.md` -> Permission Rules translation) only restrict the global default, never replace its parts (user decision 2026-10-22: a repository could otherwise drop the user's deny rules or widen a base): `intersect_rule_sets` keeps the stricter base, the deny rules of both, and only the allow rules the other set permits too (`permissions/rule-language.md` -> Resolution). The levels above (override, owner's rules) still replace whole parts. The defaults are read unless those levels set every part; an invalid stored default then raises `AgentResolutionError` instead of granting full access. A resolved set that restricts nothing is `None` (full access), so a vBot level storing the Full access preset (every part set) lifts the limits of every level below it. A Project chosen for an Identity Agent's Session confines nothing: Identity Agents never take Project overrides. Built-in Agents always get `None`. `effective_config(...)["permission_rules"]` is `{value, source, parts, error?}`: the effective stored-form rules (`None` = full access, also when they restrict nothing), the most specific level that set anything (`agent` for `agent.json`, `override`, `repository` for a repository profile's translation, `global_default`), the most specific level that set each part (a `repository` part keeps the default's limits too), and the invalid-default message; a member's `permission_rules_default`, what its override inherits, is the restricted default (`AgentResolver.inherited_permission_rules(member)`). Coverage: `test_resolver_config_agent.py::test_permission_rules_resolve_each_part_from_override_then_repository_then_default`, `test_resolver_scan_identity.py`.

A Team member's `rule_files` are its profile, the profile's own `AgentProfile.rule_files` (`scanning.md` -> Permission Rules translation) and `definition_locations(root, project.sources, profile.source)` (`core/projects/sources/catalog.py`): every path of each switched-on Agent Source up to and including the member's own, in Source order, also those not created yet, because a definition of the member's name there would win over or tie with its current one at the next Team rescan and carry other rules (user decision 2026-10-22: protect those write locations). Whole definition folders count, since a name can come from a file's content, so a restricted member also cannot change other definitions there; later Sources never win and stay writable. Coverage: `test_resolver_config_agent.py::test_a_restricted_member_cannot_write_a_definition_that_would_replace_its_own`.

## Effective Capabilities

Project capability configuration is a ceiling, not another fallback chain.

A Project Agent's `project_memory_enabled` selection defaults to true and a Boolean per-Agent override can disable it; resolution and effective-config provenance expose that choice. The separate Project-wide `memory_enabled` switch remains the outer gate for rendering and Tool access (`memory.md`), without changing the Agent's personal `memory_prompt_mode: off` or giving it a Workspace.

Without a vBot Tool override, effective Tool policy is:

```text
Project allowed_tools AND Profile allowlist/rules - Profile denials
```

With `overrides.<agent_id>.tool_access`, effective Tool policy is instead:

```text
vBot Tool Access Policy  AND  Project allowed_tools
```

The override fully replaces repository denials and may therefore re-enable a repo-denied Tool, but it cannot grant a directly configurable Tool omitted by the Project. `mode: all` means the complete Project Tool Whitelist subject to explicit opt-in. `tool_access.granted` survives this materialization, must stay inside the ceiling, and never comes from whitelist membership. `selected` may narrow it to exactly one or zero named Tools, and `none` disables every direct and automatic activation path. Automatic companions may follow an active in-ceiling lead and absolute `denied` names apply after every activation source. Keep foreign permission parsing in adapters and this final policy construction in the resolver.

Effective Skills are:

```text
(discovered Project Skills + enabled bundled Skills + enabled global Skills)
- explicitly disabled Project Skills
- {"*"}
```

The disabled-name subtraction applies to the combined set, so a disabled Project Skill cannot be resurrected by a bundled or global Skill with the same name. The `"*"` sentinel is configuration syntax, never an effective Skill name.

The same exact effective names form the temporary Skill grant when an Identity Session works in a Project (its Working Project) or through explicitly loaded Project Context. Runtime layers those names into the Identity-scoped `SkillRegistry.always_allowed` set beside the Agent's private Skills, so neither an empty personal `allowed_skills` nor its `excluded_skills` can prevent the Agent or its Self-Subagent from using what the Project requires; the persisted Agent configuration is not mutated.

Effective additional Agent targets are the current Project Team, excluding the calling Agent, filtered by the repository Agent's ordered `AgentTargetRule` list, with the last matching rule winning. No target rules means every other Team member; a result with no members means self-only, not that either Sub-Agent Tool is unavailable. When a Sub-Agent Tool is available, the resolver projects these additional targets into the synthesized config Agent's root `tools.subagent.allowed_agents` block; Tool availability remains owned by the effective `tool_access`, and disabling the Tool omits that runtime block without altering the repository target rules that will be applied again when the Tool returns. A Project Agent cannot address an Identity Agent or another Project even if its source policy is broad, because Project scope is the hard outer boundary.

Imported allowlists (including empty) and denials select the Agent's own Tools as an ordinary `selected` policy, so followers and the Session's grants (`message_parent`, an Extension's private Tools) still activate; how foreign settings become Tools is in `scanning.md` -> Translation and access. Unavailable Profiles cannot run. Explicit vBot overrides replace import restrictions within the ceiling.

Profile Skill rules narrow the effective Skills. Permitted available preloads include Skill instruction bodies in the Agent body, using existing `skill_content` framing and the origin directory. Missing, unavailable or oversized Skills are not loaded. Ordinary Agent-body prompt pins keep this stable during the Prompt Epoch.

`prepare_temporary_config` consumes the same Profile and translation against the owner's selection instead of Project ceilings. It snapshots resolved Model/scalars, restrictions and instructions before binding creation. A Model wish that resolves to no usable Model, with no Project or global default either, leaves the participant on its own Model. The original `repository_profile_request` makes replays idempotent across repository edits. The resolved Tool policy stays in the protected binding; an owner's `fixed` selection stays fixed.

## Working Project resolution

`AgentResolver` decides which Project a Run works in (GLOSSARY -> Working Project). There is no process-global selection, and Workspace equality never establishes Project identity or replaces explicit `project_id` routing.

- `new_session_working_project(project_id, agent, requested=AGENT_DEFAULT_PROJECT)` returns where a new Session works (`None`: the Workspace). A Project Agent works in its address Project and takes no other choice (`TEAM_WORKING_PROJECT_MESSAGE`, `AgentResolutionError`). For an Identity Agent, a Project id must exist (`ResolutionProjectNotFoundError`, RPC `project_not_found`), `None` names the Workspace, and `AGENT_DEFAULT_PROJECT` the Agent's `root_project_id`, which must exist too (`DEFAULT_PROJECT_MISSING_MESSAGE`, `AgentResolutionError`, RPC `domain_error`).
- `resolve_working_project(project_id, agent, *, session_id=None, requested=AGENT_DEFAULT_PROJECT)` and its `_async` twin: an existing Session of an Identity Agent works in its stored Working Project; without a Session, or for one that does not exist yet, the result is `new_session_working_project`'s. The Session owner returns its Project id together with the original-Project archive binding. An unresolved binding (even after archive purge) refuses a different Project reusing that id; only restoring the matching archive clears it. A stored Project that is still archived or no longer exists raises `WorkingProjectMissingError` (an `AgentResolutionError` with `.project_id`, `WORKING_PROJECT_MISSING_MESSAGE`, RPC `domain_error`); it never falls back to the Workspace. The async twin reads the Session on the Session database's pool and checks the Project on the `agent-resolution` pool.
- `session_working_project_async(address)` does the same for one Session address (a missing Session: the Workspace); Sub-Agent `send` uses it (`subagents.md`).
- Chat admission and Compaction, prompt preview, `chat.commands`, `files.list` and `@`-mentions, `session.create` and commands for a new Session resolve through these; Runtime's Extension cwd, which has no Session, uses `new_session_working_project`. `resolve_prompt_project` validates the resolved Project's repository for the prompt, and `resolve_skill_scope` uses that same Project for Project Skills.

Texts (Agent- and user-visible; `_runtime_agent.py`, `_session_resolution.py`):

- `WORKING_PROJECT_MISSING_MESSAGE`: `This Session works in Project {project_id}, which no longer exists. Restore the Project to continue this Session, or start a new Session.`
- `DEFAULT_PROJECT_MISSING_MESSAGE`: `Agent {agent_id} starts new Sessions in Project {project_id}, which no longer exists. Choose another default Project for the Agent.`
- `TEAM_WORKING_PROJECT_MESSAGE`: `A Session of Team Agent {agent_id}@{project_id} works in Project {project_id}; it cannot work in another Project.`

## Project Context instructions

The instructions of Project Context (GLOSSARY -> Project Context) are `Project.instruction_files`, the one resolver (`resolve_instruction_files`, `core/projects/instructions.py`): while `auto_instructions` is on, first the repository's main instruction file - `AGENTS.md`, else the first existing of `CLAUDE.md`, `.claude/CLAUDE.md`, `GEMINI.md`, `.github/copilot-instructions.md` in the repository root, chosen anew whenever the frame renders, so a repository that gains an `AGENTS.md` switches on its own - then the additional `auto_load` files, each file once. A candidate whose status cannot be checked counts as present, so a failing file is reported instead of being replaced by the next one. They enter under the prompt file cap (`prompts/prompt-files.md`); folder instructions of subfolders reach the Agent later as notes (`chat/request-building.md` -> Folder instructions). `_project_context.py` projects Project Context into prompt and Skill scopes.

## Change Rules

`preview_temporary_agent` and persisted temporary-Agent resolution share
`_require_temporary_project`: both check that the selected Project exists and
keep the configured Tool selection, Skill allowlist and prompt blocks unchanged,
because Project ceilings bound only the Team (`projects.md`); preview constructs
no Session binding. Evidence: `core/projects/resolver.py`,
`tests/core/projects/test_resolver_config_agent.py`, `tests/core/agents/test_temporary.py`
and `tests/core/runtime/test_runtime_extension_host.py`.

- Add or reorder a fallback tier only in the resolver and update `effective_config()` provenance, RPC/UI presentation, and tests together.
- Change model availability in the shared Models/Providers checker, not by adding a Project-only exception.
- Change repository field interpretation in the Source adapter; resolution should consume the common `AgentProfile` representation.
- Change Project defaults, override storage, or ceiling configuration in the configuration/persistence path; the resolver consumes the validated Project value.
- Keep Identity Agent and Project Agent resolution behind the same public seam so Chat, Sessions, Queue, Cron, and other Run producers do not develop incompatible rules.

## Source & Tests

- Resolution orchestration: `core/projects/resolver.py`; Session overrides and Working Project binding: `_session_resolution.py`; Team freshness and translation: `_team_resolution.py`; temporary Agent chain (bindings, repository Profiles, previews): `_temporary_resolution.py`; prompt/Skill scopes: `_project_context.py`
- Resolved Agent contracts and Session Agent overrides: `core/projects/_runtime_agent.py`
- Capability ceilings, scalar fallback and effective provenance: `core/projects/_resolution_values.py`
- Model usability and Connection gating: `ModelConfigurationChecker` in `core/projects/_model_configuration.py` (public imports remain available through `core.projects` and `resolver.py`)
- Project entity and override contract: `core/projects/projects.py`
- Repository inputs: `core/projects/sources/`
- Primary tests: `tests/core/projects/test_resolver_config_chains.py` (resolution chains and effective-config provenance), `tests/core/projects/test_resolver_config_agent.py`, `tests/core/projects/test_resolver_connections.py`, and `tests/core/projects/test_resolver_scan_identity.py` (scan findings, Identity resolution, Working Project resolution, prompt and Skill scopes); RPC codes for missing addresses: `tests/server/rpc/test_address_resolution_errors.py`
