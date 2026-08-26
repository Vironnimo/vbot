# Agents

Persisted agent configuration and workspace lifecycle management.

## Overview

`core/agents/` owns `agent.json` CRUD, the canonical Identity Agent roster order, and coordinated Identity Agent rename under `<data_dir>/agents/`. Creating an agent creates its sessions directory, an initial empty Session, and seeds a workspace from bundled templates; renaming moves the complete Agent-owned tree; deleting archives instead of deleting. A **per-agent private skills home** may exist at `<agent-dir>/skills/` (created on first write by `skill_manage` or skill RPCs, private and always-allowed - see `skills.md`); it moves/archives with the agent for free and is not seeded at creation.

## Data Model

Tool permission persists as root `tool_access`: mode `all`/`selected`/`none`, `selected` requires `allowed`, any mode may carry absolute `denied` names winning after activation and grants; the retired root `allowed_tools` rejects on load. `memory_prompt_mode` stays independent (prompt Memory, not permission). `<data_dir>/agents/order.json` holds `{revision, agent_ids}` collection metadata: missing preserves id-sort until first materialization, absent valid agents append, stale ids filter out, malformed files never hide agents (`doctor config` reports; explicit reorder replaces).

`id` is the only load-required field - every other field defaults when missing/null while present-but-malformed values invalidate that one Agent. Key fields:

- `id` doubles as directory name, changed only through coordinated rename.
- `model` / `fallback_models` are user-facing `<provider>/<model-id>` optionally pinned `::<connection-local-id>[:<account-id>]`; empty resolves against `defaults.agent.model` at read time without rewriting disk. `fallback_models` is the ordered fallback chain (max 5, duplicates rejected at save): when a qualifying provider error escapes built-in retries — retryable failures, rate limits (which skip remaining same-model stream restarts), or clearly model-scoped fatals such as 404/unknown-model — Chat advances candidates in order and an activated candidate stays for the rest of that Run only. Unresolvable/unusable candidates are skipped with a warning; NetworkError, auth, billing, permission, content-policy, and context-overflow errors never advance the chain.
- Persisted empty models / null temperature/effort mean "no override": `AgentStore` bakes `defaults.agent` values into `get()`/`list()`/mutation results; raw disk stays unresolved (`get_raw` skips baking for the resolver's provenance seam).
- `memory_prompt_mode`: `off` | `agent` | `agent_user` (default) controls prompt-visible memory files.
- `workspace` defaults to the data-dir location and stays independent of Project. Resolved paths are always absolute in API/UI; inside the data dir `agent.json` stores a forward-slash relative path anchored to it (making the data dir portable), external workspaces stay absolute. Relocation is always explicit: declining copy repoints with normal SOUL seeding; accepting copy moves only SOUL/USER/MEMORY, backs up replaced destinations, rolls back on failure.
- `root_project_id` is the stable Project selection: selecting/clearing changes nothing else - Workspace, Sessions, private Skills, prompt customizations, permissions, and Sub-Agent addressing stay independent. Missing means unrooted; loading never infers from Workspace.
- `current_session_id` normalizes on load (missing/dangling -> fresh empty Session, rewriting during read). After a Session leaves via move/delete, `reset_current_after_session_removed` lands on the last-active *remaining* Session instead.
- `tool_access` defaults `{"mode":"all"}`; unknown Tool names persist for temporarily unavailable Extensions; `allowed`/`denied` must not overlap; the retired `*` wildcard is invalid. Tool-owned settings live under optional root `tools.<tool>`: `tools.subagent.allowed_agents` lists only *additional* targets (`['*']` default, `[]` self-only, bare or qualified ids); `tools.bash.allowed_env` is the ordered deduplicated permanent env-grant list. Denying an owning Tool deactivates its settings without deleting them. Automatic Tools (`memory`, `session_read`, granted `history`) are not independently selected but explicitly deniable.
- `custom_system_prompt_enabled` gates reading the agent's own prompts directory; disabling ignores files without deleting them.

## Uniform Agent Resolution

Run paths resolve through one seam, `AgentResolver.resolve_agent(project_id, agent_id)` (owned by `core/projects/`; details in `projects/resolution.md`) - never `runtime.agents.get(...)` directly. Identity branch (`project_id=None`): this domain's store, unchanged behavior. Project branch synthesizes a workspace-less Config Agent from the Team scan whose policy computes inside the Project Tool Whitelist (repository denials narrow; a vBot override fully replaces scanned policy within the ceiling).

Two freshness levels: team membership comes from the scan (cached per project, refreshed on open/re-scan); a single member's config reads fresh from the repo file on every resolve, mirroring identity agents re-reading `agent.json`.

Chains: identity agents keep model -> global -> empty. Config agents resolve override -> repo model -> project default -> global -> error, where a candidate counts only if configured in this instance (registered provider, cataloged Model, usable credential permitted by its connection allowlist; pinned suffixes checked verbatim); unconfigured repository models surface as scan-time findings, and exhausted chains raise `AgentResolutionError` mapped to clean failures per caller. Temperature/thinking resolve override -> value -> project default -> global -> Provider default, where `""` (effort) and `0.0` (temperature) are real stopping values, not absent ones. Identity agents keep their two-tier injection unchanged. `effective_config(...)` exposes per-field `{value, source}` provenance using `get_raw` so persisted values distinguish from baked defaults.

## Conventions & Rules

- Agent IDs: filesystem-safe slugs, letter/digit start, letters/digits/hyphen/underscore, max 64 chars. Writes use temp-file atomic replace; relative persisted Workspaces resolve only against the active data directory, never cwd.
- The only seeded template is `SOUL.md`; USER.md/MEMORY.md belong to the memory system and create lazily on first write - a memory-off agent has neither, and deletion does not resurrect them.
- `scripts/converters/agent_tool_access.py` is the sole legacy migration path; the loader contains none.
- Mutable-field validation lives server/core-side: effort vocabulary `null|""|none|minimal|low|medium|high|xhigh|max` (null inherits, "" = provider default), temperature null or 0.0-2.0 (0.0 real), strict policy shape, shell-portable env names. Enabling custom prompts seeds the agent prompt directory once; re-enabling preserves existing files.
- Run-local model fallback never mutates persisted model/fallback fields. The `::connection[:account]` suffix stores the provider-local slug, reconstructed to full runtime form at resolution; Account semantics in `providers/connections.md`.

## Store Operations

- `create` persists config, sessions dir, first Session, and workspace seed; returned Agents are effective-resolved. `get` bakes defaults and stays strict for addressed invalid agents; `list` isolates individual unreadable configs (log+skip) and degrades to empty roster on enumeration failure so bootstrap can still run. `list_with_order`/`reorder` guard roster replacement with optimistic revision + exact-set matching (`AgentOrderConflictError` otherwise).
- `ensure_bootstrap()` creates a bootstrap Agent only when zero valid Agents exist; invalid directories stay for diagnosis and an occupied `main` shifts to the first free `main-N`.
- `rename` atomically moves the whole tree, rebasing an in-tree Workspace while preserving external paths (case-only Windows renames go through a temporary sibling), retargeting exact bare-id references in every `allowed_agents` list (never qualified addresses), and exposing compensation snapshots. `delete` archives under `archive/agents/<id>/` - deliberately a subtree, because flat `archive/<agent-id>` would collide with the sibling `sessions`/`projects` roots and delete's replace-archive rmtree would wipe them.
- `exists` is the validity-aware, never-raising probe behind identity-only gates such as private-skill layering and the skill RPC write scope. `reset_current_after_session_removed` re-homes the current pointer bypassing read-time normalization (otherwise `get` would replace the dangling pointer before landing logic runs).
- `update_with_metadata` owns transactional Workspace relocation with copied/backed-up metadata; `agents_rooted_in`/`restore_update` support Project removal compensation.

## Constraints & Gotchas

- Deletion replaces an existing same-ID archive. Seeding never overwrites existing workspace files.
- Server guards refuse deleting the last Agent, one with active/queued Runs (`agent_busy`), or one referenced by Channels/Cron (`agent_in_use`); deletion holds both the reference lock and admission guard across validation and archive. Core `AgentStore.delete()` stays a pure filesystem archive operation.
- Rename holds reference lock plus admission guards for both ids, refuses open Sub-Agent relations/collisions/invalid ids, coordinates live references (Channels, non-terminal Cron, delegation entries, functional parent links), keeps terminal history/fork provenance/logs historical, and compensates completed changes on failure.
- Roster order is the selection fallback for accessors without valid saved selection - it never changes current selections, Sessions, Rooting, or Run configuration; Config Agents come from Team scan order and are not in this document.
