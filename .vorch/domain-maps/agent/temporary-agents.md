# Temporary Agents

Read when changing Extension-owned temporary Agents in `core/agents/temporary.py` (resolution, immutable configurations and bindings) and `core/agents/temporary_execution.py` (execution groups and group deletion), including `prompt_blocks`, `compaction_policy` and `repository_profile`. How they resolve against a selected Project is owned by `../projects.md`.

## Resolution and execution groups

Temporary Agents involve no Identity creation, workspace seeding or roster operations. `TemporaryExecutionGroups.owned_run`/`owned_runs` inspect exact Run ids with one indexed read (ids without an owned record raise `RunNotFoundError` or are absent), and `start` detects a replayed input by its durable input id rather than scanning the group's Run history. A selected Project does not narrow the configured Tool and Skill selection (`projects.md`). Tests: `tests/core/agents/test_temporary.py` and `tests/core/projects/test_resolver_config_agent.py`.

Owned completion delivery carries an optional expected target Session generation through the Runtime host and `continue_completion` into Chat admission. The group's Participant generation validates ownership; a normal descendant Session has its own target generation and must also match (`automation/completion-delivery.md`).

For new temporary Sessions outside a Project, Runtime injects `AgentStore.capture_delegation_bindings` so explicit Identity targets carry the same incarnation evidence as new Identity grants (`delegation.md`). The optional private `delegation_bindings` snapshot persists and resolves with the temporary Agent; replay of the same creation request keeps its first evidence after target replacement or repository edits. Existing snapshots without the field retain their legacy meaning. Wildcard/unrestricted and Project-qualified grants keep their scope rules, and Project-scoped temporary Agents capture no Identity evidence.

`TemporaryAgentConfig.permission_rules` (the Agent's own rules, normalized like `agent.json`) and `permission_ceiling` (a strict `PermissionCeiling` dict, `source` `extension`) round-trip through the snapshot; an invalid one makes the config invalid. They resolve onto `RuntimeAgent.permission_rules` / `.permission_ceiling` (`../projects/resolution.md` -> Permission Rules). Test: `test_temporary_agent_permission_rules_and_ceiling_round_trip`.

API 40 adds `permission_ceiling_profile`. The binding retains the name. Each resolution looks up its current rules, and the Run expands them in its own path context as an Extension layer alongside the stored ceiling. A missing profile stops execution. The profile deletion guard checks Identity Agent and Project assignments. An Extension-only reference can therefore become unavailable. A plain temporary participant can select a live `permission_profile` or opt into the global Agent rules with `inherit_permission_defaults`. Older bindings retain their original configuration.

## Group deletion

`TemporaryExecutionGroups.delete_group` requires a closed group and current
registration, waits for draining, closes the Terminal Sessions and shell commands
of its live participants (the injected `SessionWorkCloser`), deletes its bound participant Sessions through the Session manager
under a no-Run guard (`release_temporary_group`, shared with `archive_group`), removes
the Scratch Areas whose root Sessions were removed (`tools/scratch.md`), and
releases its in-memory group. Repeated deletion is safe;
other owners and groups are outside its scope (`test_swarm_operations.py`).

## Prompt blocks and Compaction Policy

Temporary configurations also carry `project_memory_enabled`, default true. Only false is stored in a binding, keeping older default snapshots identical for idempotent creation; resolution and preview preserve the selection. It permits Project Memory only while the Session has a selected Project whose shared Memory is enabled, and grants no personal Memory (`memory.md`).

Temporary configurations and persisted bindings carry optional `prompt_blocks`:
`None` inherits the normal layout; an explicit list selects all allowed System
Prompt contributions. Prompts owns its assembly semantics. Continuation input may
be empty so an owner can retain a canonical admission receipt without adding
guidance to the Model request; initial input must still be non-empty.
Evidence: `temporary.py`, `test_temporary.py`, `test_swarm_start.py`,
`test_swarm_stop_resume.py`.

They also carry an optional `compaction_policy`: `None` inherits the global
Compaction Policy; a dict is validated and normalized as one complete Policy by the
shared Settings normalizer when the configuration is built, and resolves as the
participant's Agent-level Policy (`compaction.md` -> Policy Resolution). The binding
config stores the key only when set, so inheriting participants and bindings that
predate the key are identical and resolve to `None` (`test_temporary.py`).

## Repository profiles

A temporary configuration can name `repository_profile`, a Team Agent id in its selected Project. Projects resolves the neutral Profile against the owner's selection, then Agents persists its resolved configuration and original creation request. Model/scalars, instructions and the resolved Tool policy stay immutable in the binding. Replayed creation returns that snapshot after repository edits. Swarm formations expose the selection (`extensions/swarm.md`); no Agent kind is added.

Optional owner `model_override` and `thinking_effort_override` choices win after Profile resolution without changing Tool or Permission Rules. They are retained only in the original creation request; the binding stores their resolved values, preserving older requests when the fields are absent.
An empty fallback `model` is accepted only with a Profile (`repository_profile` or `identity_profile`): the Profile's Model chain must then resolve a usable Model, or an explicit `model_override` must supply it. Saved resolved bindings always contain a usable, nonempty Model.

## Identity profiles

`identity_profile` names a roster Identity Agent instead; it is exclusive with `repository_profile` and refuses built-in Agents. `TemporaryResolution._prepare_identity` (`core/projects/_temporary_resolution.py`) snapshots that Agent's Model chain, sampling, thinking effort, Tool settings, Permission Rules (owner's parts first), Compaction Policy and SOUL.md (prepended to the owner's instructions), bounded by the owner's selection: Tool access is the intersection of both selections minus both denials (`none` or `fixed` on either side stays restrictive), Skills are the owner's selection within the Agent's allowed and excluded Skills, and Project Memory needs both. The participant stays a temporary Agent: no Identity Workspace, personal Memory or Agent-private Skills. The original request lives under the binding's `repository_profile_request` key for either Profile kind (`test_resolver_config_agent.py`).

## Actual Identity Agent Sessions (API 32)

`TemporaryAgentConfig.identity_agent` plus `identity_incarnation` selects actual
Identity execution, exclusive with both Profiles. `model` may be empty; the
identity's live configuration resolves it; actual identity execution and managed
Model changes reject Models without Chat capability. Owned admission validates every fallback Model for Chat capability too. The incarnation is the persisted
creation timestamp returned by `host.identity_agents.get`; `AgentStore.identify`
establishes a missing legacy timestamp once under its lifecycle guard. Replaced
or missing identities fail closed. Existing `identity_profile` keeps snapshot
semantics and never acquires personal context.

An actual binding has the identity address anchor (`project_id=None`, actual
Agent id), a new canonical Session, and its selected Working Project. Each
binding keeps the owner's instructions, cwd, Tool/Skill selection, optional Model
choices and Permission Ceiling. Identity resolution retains Workspace, personal
Memory, private Skills, SOUL and live identity settings. Tool policies intersect;
the owner's ceiling remains independent of Session Permission Rules. Ordinary
Sessions receive none of the binding's private capabilities. Exact Session
lookups retain the bound configuration; Agent-wide temporary lookup excludes
actual identity bindings, so another Session cannot inherit one accidentally.
Group receipts, epochs, archive and owned Run attribution stay unchanged.

`host.identity_agents` is registration-bound trusted Extension integration.
`get` returns identity/proof and creation ownership. `profiles(agent_ids)` reads up to 100 exact ids in one worker call and returns a mapping of live non-built-in profiles with name, incarnation, resolved placement, registration-relative `created_by_owner` and nullable `extension_owner`. It omits missing or invalid Agents and performs no Workspace or Session repair. `create` creates a durable
Agent; `update` requires its exact incarnation and that this Extension created
it. Scope/remit checks belong to the Extension before calling this capability.
There is no delete, Workspace relocation, placement or identity-change surface.
A stable `create(request_id=...)` persists its owner/request/configuration hash
with the initial Agent document. Exact retries return the original; conflicts
refuse. Owner and creation-request markers are immutable through ordinary Agent
updates and survive archive/restore. Technical id changes are refused for Extension-owned identities because Extension records address them by that id. An installed Extension owner (also disabled or failed) prevents ordinary Agent archival. A confirmed uninstall releases this archive reference.

The canonical owned binding's `allowed_skills` is a hard Skill ceiling for its
Runs and owned descendants, including ordinary delegated target Agents. Chat
carries it through initial request construction, explicit Project Skill-context
loads and Compaction refresh; the `skill` Tool applies the same ceiling to fresh
registry reads and miss rescans. `SkillRegistry.restricted_to` supplies an
isolated scoped view, so parent/private/Project always-allowed grants cannot
widen it and shared registries are unchanged. See `subagents/delegation.md` ->
Owned-execution Skill ceilings.
