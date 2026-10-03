# Project Agent Resolution

Read this reference when changing how a Project Agent becomes effective runtime configuration, including fallback order, provenance, model usability, capability ceilings, or working-Project helpers.

## Uniform Resolution Seam

`core/projects/resolver.py` owns `AgentResolver.resolve_agent(project_id | None, agent_id)`, the common boundary used by Run-producing paths. With no Project id it returns the stored Identity Agent; with a Project id it verifies Team membership and synthesizes a `ConfigAgent` from repository and Project state. Callers should not reproduce this branch or assemble Project Agent configuration themselves.

Resolution reads Agent configuration, may seed a workspace or normalize the current Session, and can wait for the Agent store's write lock. Event-Loop callers therefore use `resolve_agent_async` / `resolve_temporary_agent_async`; Chat Run admission and execution, Subagent spawns, and Reflection reviews resolve this way. Synchronous callers already on a worker or outside a Run path keep the plain methods.

The async variants follow the per-database pool rule (`database.md`). An Identity Agent read, which may seed its workspace and verify or repair its current-Session pointer, runs as one unit on the Session database's pool (`AgentStore.get_async`), and so does a temporary Session binding read (`TemporaryAgentRegistry.resolve_async`); a closed Session database raises `DatabaseUnavailableError`. Reading a Session's Agent overrides is one read on the Session database's pool too. Project Agent resolution, applying Session overrides and a temporary Agent's Project check stay on the bounded `agent-resolution` pool: they read Project, repository and configuration files and check Model usability. Their only database access is incidental: the first resolution in a Project whose Team is not cached yet runs the scan report, which lists the Project's Session-owning Agents (`ProjectStore.session_owning_agents`) in the middle of the repository scan. Splitting that one read per Team cache fill onto the Session pool would cut the scan into two hops for no gain, so it deliberately stays (`tests/core/projects/test_resolver_scan_identity.py`, `test_resolver_config_agent.py`).

Team membership is cached per Project, but the selected repository Agent source is reread on each resolution. This gives stable, cheap membership lookup while allowing edits to model, instructions, Tool denials, Agent-target rules, or scalar settings to take effect without a Team rebuild.

Resolution failures are `AgentResolutionError`. A missing address part keeps its precise kind: an unknown Identity Agent, an Agent that is not on the cached Team, or a Team member whose repository source vanished raises `ResolutionAgentNotFoundError`, and an unknown Project raises `ResolutionProjectNotFoundError`; both subclass `AgentResolutionError`, so callers that treat every resolution failure alike keep catching it. Ids are exact, so a case variant such as `Builder@VBot` is one of these not-found failures. Other failures (no usable Model, an unavailable temporary Session, a Project id disagreeing with its Anchor) stay plain `AgentResolutionError`. `effective_config()` and temporary Project application use the same wrapping. RPC maps the two not-found kinds to `agent_not_found` / `project_not_found` (`server.md`).

### Session Agent overrides

A Session can override its Agent's `model`, `thinking_effort` and `temperature` (`AGENT_OVERRIDE_FIELDS`, frozen `AgentOverrides` in `_runtime_agent.py`). The resolver owns them end to end: `resolve_agent(..., session_id=...)`, `resolve_agent_async(..., session_id=...)` and the temporary variants' `session=` read the Session's stored overrides and return a replaced runtime dataclass; they never mutate the stored Identity Agent, repository Agent, Project overrides or other Sessions. Without a Session, for a Session that does not exist yet, or with nothing stored, resolution is the ordinary path. `effective_config(..., session_id=...)` reports an overridden field with source `session`.

### Librarian Skill subject

A Session of the built-in Librarian (`agent.md` -> Built-in Librarian) is bound to the Agent whose Skills it maintains by the Session metadata key `skill_agent_id` (`SKILL_AGENT_ID_KEY`). After the Session's overrides, `resolve_agent`/`resolve_agent_async` with that Session return the Librarian with `skill_agent_id` set to the subject and the subject's `allowed_skills`; Tools, Model settings, Memory mode and everything else stay the Librarian's. The binding is read only for an Identity Agent that `is_librarian`, so another Agent's Sessions, an unbound Librarian Session and Session-less resolution are the ordinary path. A subject that is no Identity Agent of the user any more (missing, or a built-in Agent) fails the resolution with `AgentResolutionError` (`SKILL_SUBJECT_MISSING_MESSAGE`: `This Session works on the Skills of Agent <id>, which no longer exists, so it cannot continue.`). `resolve_skill_scope(project_id, prompt_project, agent)` takes the resolved Agent and returns `skill_subject_id(agent)` as the identity layer, so the Run's Skill registry and pinned catalog, triggers, Compaction's catalog refresh, Tool dispatch (`ToolContext.skill_agent_id`, `tools/skill.md`), `chat.commands` and `prompt.preview` resolve the subject's Skills. Behavior: `test_resolver_connections.py::test_a_librarian_session_runs_on_the_skills_of_its_bound_agent`, `test_resolver_scan_identity.py::test_resolve_skill_scope`, `tests/core/chat/test_chat_integration.py::test_a_librarian_session_works_on_the_skills_of_its_agent`.

`update_session_overrides(address, changes)` (async: `update_session_overrides_async`) is the only writer: unknown fields raise `ValueError`, values validate like Agent settings, a Model must pass `require_configured`, `None` clears one field, other fields and keys an unknown newer version stored survive, and an empty result removes the stored value. Storage is the Session metadata key `agent_overrides` (`sessions.md`), so forks inherit it. A stored Model that can no longer run fails that Session's resolution with `ModelConfigurationError` instead of silently falling back.

Writers: `session.create` / `session.set_agent_overrides` (`server.md`, used by `vbot chat`), Sub-Agent spawns (`subagents.md`), and `/model`, which clears the Session's Model override after writing the Agent's Model (`chat/commands.md`). Every Run producer (Chat admission and execution, Queue, Compaction, Sub-Agents, `/status`, Tool status, Extension Tool Agents) resolves with the Run's Session, so a continuation keeps its Session's Model and the Provider prompt cache.

## Model & Scalar Resolution

The model chain is:

```text
per-Agent override -> repository Agent -> Project default -> global default -> error
```

Each model tier must pass the same `ModelConfigurationChecker` before it can win. The checker validates the Provider, Model catalog entry, and an allowed usable Connection, including an explicitly pinned account suffix. `is_configured` supplies the boolean fallback/scan decision; `require_configured` and `AgentResolver.require_model_configured` expose the same invariant as a raising mutation seam for Chat `/model` and Project Agent overrides. A forbidden pin names the rejected Connection and the Model's allowed Connections; other failures use the general unusable-Model diagnostic. A syntactically present but unusable Model falls through to the next tier; if no usable Model exists, resolution fails rather than constructing a broken Agent.

An explicit Run Model does not add another fallback tier: it replaces the already resolved primary Model for that Run and must pass `ModelConfigurationChecker.require_configured` directly. It does not alter the resolved fallback Model or any non-Model field. The Run thinking effort accepts the same canonical values as Agent/Project settings, including `""` for Provider default; Model-specific snapping, toggle, and budget rendering remain Provider policy.

Temperature and thinking effort use:

```text
per-Agent override -> repository Agent -> Project default -> global default -> Provider default or None
```

`effective_config()` exposes the chosen value and provenance (`override`, `agent`, `project_default`, `global_default`, or `null`) for model, temperature, and thinking effort. Preserve those labels as an API/UI contract when changing fallback behavior.

Compaction policy is a supported per-Agent Project override passed into the synthesized `ConfigAgent`, but it is not one of the three fields in the current effective-config provenance result. Do not imply provenance coverage until that contract is deliberately extended end to end.

## Effective Capabilities

Project capability configuration is a ceiling, not another fallback chain.

Without a vBot Tool override, effective Tool policy is:

```text
Project allowed_tools - repository Agent denied_tools
```

With `overrides.<agent_id>.tool_access`, effective Tool policy is instead:

```text
vBot Tool Access Policy  AND  Project allowed_tools
```

The override fully replaces repository denials and may therefore re-enable a repo-denied Tool, but it cannot grant a directly configurable Tool omitted by the Project. `mode: all` means the complete Project Tool Whitelist subject to explicit opt-in. `tool_access.granted` survives this materialization, must stay inside the ceiling, and never comes from whitelist membership. `selected` may narrow it to exactly one or zero named Tools, and `none` disables every direct and automatic activation path. Automatic companions may follow an active in-ceiling lead and absolute `denied` names apply after every activation source. Keep source-format permission parsing in scanners and this final policy construction in the resolver.

Effective Skills are:

```text
(discovered Project Skills + enabled bundled Skills + enabled global Skills)
- explicitly disabled Project Skills
- {"*"}
```

The disabled-name subtraction applies to the combined set, so a disabled Project Skill cannot be resurrected by a bundled or global Skill with the same name. The `"*"` sentinel is configuration syntax, never an effective Skill name.

The same exact effective names form the temporary Skill grant when an Identity Agent works through Rooting or explicitly loaded Project Context. Runtime layers those names into the Identity-scoped `SkillRegistry.always_allowed` set beside the Agent's private Skills, so neither an empty personal `allowed_skills` nor its `excluded_skills` can prevent the Agent or its Self-Subagent from using what the Project requires; the persisted Agent configuration is not mutated.

Effective additional Agent targets are the current Project Team, excluding the calling Agent, filtered by the repository Agent's ordered `AgentTargetRule` list, with the last matching rule winning. No target rules means every other Team member; a result with no members means self-only, not that either Sub-Agent Tool is unavailable. When a Sub-Agent Tool is available, the resolver projects these additional targets into the synthesized config Agent's root `tools.subagent.allowed_agents` block; Tool availability remains owned by the effective `tool_access`, and disabling the Tool omits that runtime block without altering the repository target rules that will be applied again when the Tool returns. A Project Agent cannot address an Identity Agent or another Project even if its source policy is broad, because Project scope is the hard outer boundary.

## Working-Project Helpers

The working-Project functions in `core/projects/resolver.py` derive the admitted Project from an explicit Session/address Project or an Identity Agent's saved `root_project_id`. They hold no process-global selection. Prompt lookup validates the selected repository; Skill scope uses that same resolved Project. Workspace equality does not establish Project identity or replace explicit `project_id` routing.

## Change Rules

`preview_temporary_agent` and persisted temporary-Agent resolution share
`_require_temporary_project`: both check that the selected Project exists and
keep the configured Tool selection, Skill allowlist and prompt blocks unchanged,
because Project ceilings bound only the Team (`projects.md`); preview constructs
no Session binding. Evidence: `core/projects/resolver.py`,
`tests/core/projects/test_resolver_config_agent.py`.

- Add or reorder a fallback tier only in the resolver and update `effective_config()` provenance, RPC/UI presentation, and tests together.
- Change model availability in the shared Models/Providers checker, not by adding a Project-only exception.
- Change repository field interpretation in the source-format scanner; resolution should consume the common `ScannedAgent` representation.
- Change Project defaults, override storage, or ceiling configuration in the configuration/persistence path; the resolver consumes the validated Project value.
- Keep Identity Agent and Project Agent resolution behind the same public seam so Chat, Sessions, Queue, Cron, and other Run producers do not develop incompatible rules.

## Source & Tests

- Resolution orchestration and working-Project helpers: `core/projects/resolver.py`
- Resolved Agent contracts and Session Agent overrides: `core/projects/_runtime_agent.py`
- Capability ceilings, scalar fallback and effective provenance: `core/projects/_resolution_values.py`
- Model usability and Connection gating: `ModelConfigurationChecker` in `core/projects/_model_configuration.py` (public imports remain available through `core.projects` and `resolver.py`)
- Project entity and override contract: `core/projects/projects.py`
- Repository inputs: `core/projects/scanners/`
- Primary tests: `tests/core/projects/test_resolver_config_chains.py` (resolution chains and effective-config provenance), `tests/core/projects/test_resolver_config_agent.py`, `tests/core/projects/test_resolver_connections.py`, and `tests/core/projects/test_resolver_scan_identity.py` (scan findings, Identity resolution, prompt and Skill scopes); RPC codes for missing addresses: `tests/server/rpc/test_address_resolution_errors.py`
