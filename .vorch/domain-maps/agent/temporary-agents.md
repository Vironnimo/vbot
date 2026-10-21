# Temporary Agents

Read when changing Extension-owned temporary Agents in `core/agents/temporary.py`: their resolution, immutable configurations and bindings, execution groups and group deletion, `prompt_blocks`, `compaction_policy` and `repository_profile`. How they resolve against a selected Project is owned by `../projects.md`.

## Resolution and execution groups

Temporary Agents involve no Identity creation, workspace seeding or roster operations. `TemporaryExecutionGroups.owned_run`/`owned_runs` inspect exact Run ids with one indexed read (ids without an owned record raise `RunNotFoundError` or are absent), and `start` detects a replayed input by its durable input id rather than scanning the group's Run history. A selected Project does not narrow the configured Tool and Skill selection (`projects.md`). Tests: `tests/core/agents/test_temporary.py` and `tests/core/projects/test_resolver_config_agent.py`.

For new temporary Sessions outside a Project, Runtime injects `AgentStore.capture_delegation_bindings` so explicit Identity targets carry the same incarnation evidence as new Identity grants (`delegation.md`). The optional private `delegation_bindings` snapshot persists and resolves with the temporary Agent; replay of the same creation request keeps its first evidence after target replacement or repository edits. Existing snapshots without the field retain their legacy meaning. Wildcard/unrestricted and Project-qualified grants keep their scope rules, and Project-scoped temporary Agents capture no Identity evidence.

`TemporaryAgentConfig.permission_rules` (the Agent's own rules, normalized like `agent.json`) and `permission_ceiling` (a strict `PermissionCeiling` dict, `source` `extension`) round-trip through the snapshot; an invalid one makes the config invalid. They resolve onto `RuntimeAgent.permission_rules` / `.permission_ceiling` (`../projects/resolution.md` -> Permission Rules). Test: `test_temporary_agent_permission_rules_and_ceiling_round_trip`.

## Group deletion

`TemporaryExecutionGroups.delete_group` requires a closed group and current
registration, waits for draining, closes the Terminal Sessions and shell commands
of its live participants (the injected `SessionWorkCloser`), deletes its bound participant Sessions through the Session manager
under a no-Run guard (`release_temporary_group`, shared with `archive_group`), removes
the Scratch Areas whose root Sessions were removed (`tools/scratch.md`), and
releases its in-memory group. Repeated deletion is safe;
other owners and groups are outside its scope (`test_swarm_operations.py`).

## Prompt blocks and Compaction Policy

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
