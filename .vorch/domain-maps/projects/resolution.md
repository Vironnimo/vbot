# Project Agent Resolution

Read this reference when changing how a Project Agent becomes effective runtime configuration, including fallback order, provenance, model usability, capability ceilings, or working-Project helpers.

## Uniform Resolution Seam

`core/projects/resolver.py` owns `AgentResolver.resolve_agent(project_id | None, agent_id)`, the common boundary used by Run-producing paths. With no Project id it returns the stored Identity Agent; with a Project id it verifies Team membership and synthesizes a `ConfigAgent` from repository and Project state. Callers should not reproduce this branch or assemble Project Agent configuration themselves.

Team membership is cached per Project, but the selected repository Agent source is reread on each resolution. This gives stable, cheap membership lookup while allowing edits to model, instructions, Tool denials, Agent-target rules, or scalar settings to take effect without a Team rebuild.

## Model & Scalar Resolution

The model chain is:

```text
per-Agent override → repository Agent → Project default → global default → error
```

Each model tier must pass the same `ModelConfigurationChecker` before it can win. The checker validates the Provider, Model catalog entry, and an allowed usable Connection, including an explicitly pinned account suffix. `is_configured` supplies the boolean fallback/scan decision; `require_configured` and `AgentResolver.require_model_configured` expose the same invariant as a raising mutation seam for Chat `/model` and Project Agent overrides. A forbidden pin names the rejected Connection and the Model's allowed Connections; other failures use the general unusable-Model diagnostic. A syntactically present but unusable Model falls through to the next tier; if no usable Model exists, resolution fails rather than constructing a broken Agent.

Temperature and thinking effort use:

```text
per-Agent override → repository Agent → Project default → global default → Provider default or None
```

`effective_config()` exposes the chosen value and provenance (`override`, `agent`, `project_default`, `global_default`, or `null`) for model, temperature, and thinking effort. Preserve those labels as an API/UI contract when changing fallback behavior.

Compaction policy is a supported per-Agent Project override passed into the synthesized `ConfigAgent`, but it is not one of the three fields in the current effective-config provenance result. Do not imply provenance coverage until that contract is deliberately extended end to end.

## Effective Capabilities

Project capability configuration is a ceiling, not another fallback chain.

Effective Tools are:

```text
Project allowed_tools − repository Agent denied_tools
```

The repository Agent cannot grant a Tool omitted by the Project. Keep source-format permission parsing in scanners and the final set computation in the resolver.

Effective Skills are:

```text
(discovered Project Skills ∪ enabled bundled Skills ∪ enabled global Skills)
− explicitly disabled Project Skills
− {"*"}
```

The disabled-name subtraction applies to the combined set, so a disabled Project Skill cannot be resurrected by a bundled or global Skill with the same name. The `"*"` sentinel is configuration syntax, never an effective Skill name.

Effective additional Agent targets are the current Project Team, excluding the calling Agent, filtered by the repository Agent's ordered `AgentTargetRule` list, with the last matching rule winning. No target rules means every other Team member; a result with no members means self-only, not that either Sub-Agent Tool is unavailable. When a Sub-Agent Tool is available, the resolver projects these additional targets into the synthesized config Agent's root `tools.subagent.allowed_agents` block; Tool availability remains owned by the effective `allowed_tools`, and disabling the Tool omits that runtime block without altering the repository target rules that will be applied again when the Tool returns. A Project Agent cannot address an Identity Agent or another Project even if its source policy is a wildcard, because Project scope is the hard outer boundary.

## Working-Project Helpers

The working-Project functions in `core/projects/resolver.py` expose a process-local snapshot used by paths that need the currently selected Project. Set and clear it through the owned helpers. It is not derived from cwd equality, does not establish Project identity, and does not replace explicit `project_id` routing in concurrent or persisted work.

## Change Rules

- Add or reorder a fallback tier only in the resolver and update `effective_config()` provenance, RPC/UI presentation, and tests together.
- Change model availability in the shared Models/Providers checker, not by adding a Project-only exception.
- Change repository field interpretation in the source-format scanner; resolution should consume the common `ScannedAgent` representation.
- Change Project defaults, override storage, or ceiling configuration in the configuration/persistence path; the resolver consumes the validated Project value.
- Keep Identity Agent and Project Agent resolution behind the same public seam so Chat, Sessions, Queue, Cron, and other Run producers do not develop incompatible rules.

## Source & Tests

- Resolver, effective configuration, capabilities, and working-Project helpers: `core/projects/resolver.py`
- Model usability and Connection gating: `ModelConfigurationChecker` in `core/projects/resolver.py`
- Project entity and override contract: `core/projects/projects.py`
- Repository inputs: `core/projects/scanners/`
- Primary tests: `tests/core/projects/test_resolver_config_chains.py`, `tests/core/projects/test_resolver_effective_config.py`, `tests/core/projects/test_resolver_connections.py`, and `tests/core/projects/test_resolver_prompt_skill_scopes.py`
