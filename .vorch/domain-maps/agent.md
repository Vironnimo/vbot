# Agents

Persisted agent configuration and workspace lifecycle management.

## Overview

`core/agents/` owns `agent.json` CRUD, the canonical Identity Agent roster order, and coordinated Identity Agent rename under `<data_dir>/agents/`. Creating an Agent creates an initial empty database Session and seeds a workspace from bundled templates; a later file failure deletes that new Session. Renaming moves the Agent-owned tree and retargets live global Session addresses atomically, with a compensating retarget on restore. Deleting moves filesystem state first, then archives live database Sessions and restores the filesystem on failure. A **per-agent private skills home** may exist at `<agent-dir>/skills/` (created on first write by `skill_manage` or skill RPCs, private and always-allowed - see `skills.md`); it moves/archives with the Agent and is not seeded at creation.

## Data Model

Tool permission persists as root `tool_access`: mode `all`/`selected`/`none`, `selected` requires `allowed`, any mode may carry explicit opt-ins in `granted` (also used for the vision exception in `tools/image.md`) and absolute `denied` names winning after activation and grants; the retired root `allowed_tools` is an ordinary unknown field (a `doctor config` warning, kept on disk). `memory_prompt_mode` stays independent (prompt Memory, not permission). `<data_dir>/agents/order.json` holds `{revision, agent_ids}` collection metadata: missing preserves id-sort until first materialization, absent valid agents append, stale ids filter out, malformed files never hide agents (`doctor config` reports; explicit reorder replaces). `agent.json` and `order.json` are Generation 1 JSON documents (`format_version` 1, unknown fields kept on every write, never overwritten after a failed load; contract in `settings.md`).

Besides `format_version`, `id` is the only load-required field - every other field defaults when missing/null while present-but-malformed values invalidate that one Agent. Key fields:

- `id` doubles as directory name, changed only through coordinated rename.
- `model` / `fallback_models` are user-facing `<provider>/<model-id>` optionally pinned `::<connection-local-id>[:<account-id>]`; empty resolves against `defaults.agent.model` at read time without rewriting disk. `fallback_models` is the ordered fallback chain (max 5, duplicates rejected at save). Chat owns runtime eligibility and shared retry limits, including exhausted network/timeout recovery; see `chat/run-execution.md`. Unresolvable/unusable candidates are skipped with a warning, and an activated candidate stays for the rest of that Run only without changing the persisted primary Model.
- Persisted empty models / null temperature/effort mean "no override": `AgentStore` bakes `defaults.agent` values into `get()`/`list()`/mutation results; raw disk stays unresolved (`get_raw` skips baking for the resolver's provenance seam).
- `memory_prompt_mode`: `off` | `agent` | `agent_user` (default) controls prompt-visible memory files.
- `workspace` defaults to the data-dir location and stays independent of Project. Resolved paths are always absolute in API/UI; inside the data dir `agent.json` stores a forward-slash relative path anchored to it (making the data dir portable), external workspaces stay absolute. Relocation is always explicit: declining copy repoints with normal SOUL seeding; accepting copy moves only SOUL/USER/MEMORY, backs up replaced destinations, rolls back on failure.
- `root_project_id` is the stable Project selection: selecting/clearing changes nothing else - Workspace, Sessions, private Skills, prompt customizations, permissions, and Sub-Agent addressing stay independent. Missing means unrooted; loading never infers from Workspace.
- `current_session_id` normalizes on load (missing/dangling -> fresh empty Session, rewriting during read). After a Session leaves via move/delete, `reset_current_after_session_removed` lands on the last-active *remaining* Session instead (Sessions' `newest_session_id`, one bounded page read). That write-time repair does not close the gap: the pointer (`agent.json`) and Sessions (SQLite) share no transaction, so a failed or interrupted re-aim, a restored Session store, or an offline edit can still leave it dangling. Reads that return the pointer (`get`, `list`/`list_with_order`, and the loads inside `update`/`rename`) therefore keep verifying it; a roster read verifies every pointer in one Session read (`ChatSessionManager.existing_addresses`), a single `get` in one point read. `get_raw` serves configuration provenance only and returns the stored pointer unverified (`test_agents_mutations.py`). Because of that repair, Event-Loop callers read one Agent with `get_async`, which runs `get` as one unit on the Session database's pool (`ChatSessionManager.run_async`); Agent resolution uses it, and a closed Session database raises `DatabaseUnavailableError` (`tests/core/projects/test_resolver_scan_identity.py`).
- `tool_access` defaults `{"mode":"all"}`; unknown Tool names persist for temporarily unavailable Extensions; `allowed`/`denied` must not overlap; the retired `*` wildcard is invalid. Tool-owned settings live under optional root `tools.<tool>`: `tools.subagent.allowed_agents` lists only *additional* targets (`['*']` default, `[]` self-only, bare or qualified ids); `tools.bash.allowed_env` is the ordered deduplicated permanent env-grant list. Denying an owning Tool deactivates its settings without deleting them. Automatic Tools (`memory`, granted `history`) are not independently selected but explicitly deniable.
- `custom_system_prompt_enabled` gates reading the agent's own prompts directory; disabling ignores files without deleting them.

## Uniform Agent Resolution

Owner-managed temporary Agents resolve through `AgentResolver.resolve_temporary_agent` using an exact canonical Session generation. `core/agents/temporary.py` owns their immutable configuration, indexed bindings and execution groups; Identity creation, workspace seeding and roster operations are not involved. `TemporaryExecutionGroups.owned_run`/`owned_runs` inspect exact Run ids with one indexed read (ids without an owned record raise `RunNotFoundError` or are absent), and `start` detects a replayed input by its durable input id rather than scanning the group's Run history. Explicit Project Tool/Skill ceilings still narrow profile selections. Tests: `tests/core/agents/test_temporary.py` and `tests/core/projects/test_resolver_config_agent.py`.

`TemporaryExecutionGroups.delete_group` requires a closed group and current
registration, waits for draining, deletes its bound participant Sessions through
the Session manager, and releases its in-memory group. Repeated deletion is safe;
other owners and groups are outside its scope (`test_swarm_board.py`).

Temporary configurations and persisted bindings carry optional `prompt_blocks`:
`None` inherits the normal layout; an explicit list selects all allowed System
Prompt contributions. Prompts owns its assembly semantics. Continuation input may
be empty so an owner can retain a canonical admission receipt without adding
guidance to the Model request; initial input must still be non-empty.
Evidence: `temporary.py`, `test_temporary.py`, `test_swarm_lifecycle.py`.

They also carry an optional `compaction_policy`: `None` inherits the global
Compaction Policy; a dict is validated and normalized as one complete Policy by the
shared Settings normalizer when the configuration is built, and resolves as the
participant's Agent-level Policy (`compaction.md` -> Policy Resolution). The binding
config stores the key only when set, so inheriting participants and bindings that
predate the key are identical and resolve to `None` (`test_temporary.py`).

Run paths resolve through one seam, `AgentResolver.resolve_agent(project_id, agent_id)` (owned by `core/projects/`; details in `projects/resolution.md`) - never `runtime.agents.get(...)` directly. Identity branch (`project_id=None`): this domain's store, unchanged behavior. Project branch synthesizes a workspace-less Config Agent from the Team scan whose policy computes inside the Project Tool Whitelist (repository denials narrow; a vBot override fully replaces scanned policy within the ceiling).

Two freshness levels: team membership comes from the scan (cached per project, refreshed on open/re-scan); a single member's config reads fresh from the repo file on every resolve, mirroring identity agents re-reading `agent.json`.

Chains: identity agents keep model -> global -> empty. Config agents resolve override -> repo model -> project default -> global -> error, where a candidate counts only if configured in this instance (registered provider, cataloged Model, usable credential permitted by its connection allowlist; pinned suffixes checked verbatim); unconfigured repository models surface as scan-time findings, and exhausted chains raise `AgentResolutionError` mapped to clean failures per caller. Temperature/thinking resolve override -> value -> project default -> global -> Provider default, where `""` (effort) and `0.0` (temperature) are real stopping values, not absent ones. Identity agents keep their two-tier injection unchanged. `effective_config(...)` exposes per-field `{value, source}` provenance using `get_raw` so persisted values distinguish from baked defaults.

## Conventions & Rules

- Agent IDs: filesystem-safe slugs, letter/digit start, letters/digits/hyphen/underscore, max 64 chars. Lookups are exact on every platform: `get`/`get_raw`/`exists`/update/rename/delete/current-Session repair require a directory entry with exactly the requested spelling (`_stored_agent_path`), so a case variant is `AgentNotFoundError` (RPC `agent_not_found`, also when it arrives through Agent resolution; `projects/resolution.md`) even where the filesystem would open the stored tree, while a persisted id disagreeing with its directory stays a plain `AgentError` (`test_agents.py`). A store-local reentrant lock serializes complete lifecycle read-modify-write operations, current-Session repairs, and roster revisions across worker threads. Writes use temp-file atomic replace; relative persisted Workspaces resolve only against the active data directory, never cwd.
- The only seeded template is `SOUL.md`; USER.md/MEMORY.md belong to the memory system and create lazily on first write - a memory-off agent has neither, and deletion does not resurrect them.
- The Generation 1 converter turns the retired root `allowed_tools` shape into `tool_access` (`scripts/converters/persistence_generation_1/_tool_access.py`, `database/generation-1-conversion.md`); the loader contains none.
- Mutable-field validation lives server/core-side: effort vocabulary `null|""|none|minimal|low|medium|high|xhigh|max` (null inherits, "" = provider default), temperature null or 0.0-2.0 (0.0 real), strict policy shape, shell-portable env names. Enabling custom prompts seeds the agent prompt directory once; re-enabling preserves existing files.
- Run-local model fallback never mutates persisted model/fallback fields. The `::connection[:account]` suffix stores the provider-local slug, reconstructed to full runtime form at resolution; Account semantics in `providers/connections.md`.

## Store Operations

- `agents.py` keeps catalog mutation, Session coordination and compensation. Internal `_types.py` holds records/errors, `_config.py` owns JSON validation and normalization, and `_workspace.py` owns paths, template seeding and reversible file relocation. Model fallback validation reuses Settings' `validate_fallback_chain`; Agent diagnostics and mutation errors retain their surface-specific wording.
- `create` creates the first Session (in `sessions.db`, before the Agent directory so a failure leaves no ghost directory), seeds the Workspace and persists the config; a failure removes the Session and the directory again. Returned Agents are effective-resolved. `get` bakes defaults and stays strict for addressed invalid agents; `list` isolates individual unreadable configs (log+skip) and degrades to empty roster on enumeration failure so bootstrap can still run. `list_with_order`/`reorder` guard roster replacement with optimistic revision + exact-set matching (`AgentOrderConflictError` otherwise).
- `ensure_bootstrap()` creates a bootstrap Agent only when zero valid Agents exist; invalid directories stay for diagnosis and an occupied `main` shifts to the first free `main-N`.
- `rename` atomically moves the whole tree, rebasing an in-tree Workspace while preserving external paths (case-only Windows renames go through a temporary sibling), retargeting exact bare-id references in every `allowed_agents` list (never qualified addresses), and exposing compensation snapshots. `delete` archives under `archive/agents/<id>/` - deliberately a subtree, because flat `archive/<agent-id>` would collide with the sibling `projects` root and delete's replace-archive rmtree would wipe it.
- `exists` is the validity-aware, never-raising probe behind identity-only gates such as private-skill layering and the skill RPC write scope. `reset_current_after_session_removed` re-homes the current pointer bypassing read-time normalization (otherwise `get` would replace the dangling pointer before landing logic runs).
- `lifecycle_guard` lets another domain hold the store's existing mutation lock across private-home lookup, writes and invalidation. Runtime injects it into the registered Skill authoring Tool to protect shared-owner trees from concurrent rename/archive. Guarded work runs in a worker and must not call back into the Event Loop; Agent deletion also offloads its roster read and archive (`tests/core/runtime/test_runtime_skill_lifecycle.py`).
- `update_with_metadata` owns transactional Workspace relocation with copied/backed-up metadata; `agents_rooted_in`/`restore_update` support Project removal compensation.

The Generation 1 converter replaces retired Tool names in `tool_access` with their successors without widening access; see `database/generation-1-conversion.md` -> Retired Tool names.

## Constraints & Gotchas

- Deletion replaces an existing same-ID archive only after successful archival. If archival and compensation both fail, the staged previous archive remains on disk and its recovery path is reported. Seeding never overwrites existing workspace files.
- Server guards refuse deleting the last Agent, one with active/queued Runs (`agent_busy`), or one referenced by Channels/Cron (`agent_in_use`); deletion holds both the reference lock and admission guard across validation and archive. Core `AgentStore.delete()` owns filesystem and live Session archiving with filesystem compensation on database failure; it does not own those product-level guards.
- Rename holds reference lock plus admission guards for both ids, refuses open Sub-Agent relations/collisions/invalid ids, coordinates live references (Channels, non-terminal Cron, delegation entries, functional parent links), keeps terminal history/logs historical, and compensates completed changes on failure. Fork provenance (`fork_source`) is derived from the parent Session's current row, so it follows the retargeted Sessions to the new id (`sessions.md`).
- Roster order is the selection fallback for accessors without valid saved selection - it never changes current selections, Sessions, Rooting, or Run configuration; Config Agents come from Team scan order and are not in this document.
