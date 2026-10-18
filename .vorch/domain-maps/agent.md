# Agents

Persisted Identity Agent configuration and Workspace lifecycle management.

## Overview

`core/agents/` owns `agent.json` CRUD and validation, the canonical Identity Agent roster order, Workspace seeding and relocation, coordinated Identity Agent rename (with Runtime), the file side of archive and restore, delegation bindings, the built-in Agents, and temporary Agent configurations and groups under `<data_dir>/agents/`. It does not own Agent resolution or Config Agents (`projects.md`), Run-local Model fallback (`chat/run-execution.md`), Memory files (`memory.md`) or Skill semantics (`skills.md`).

Creating an Agent seeds a workspace from bundled templates and creates no Session: its `current_session_id` stays empty until a first message creates one (`server/chat-rpcs.md` -> Chat submission target). Renaming moves the Agent-owned tree and retargets live global Session addresses; it is recorded durably so an interrupted rename ends fully old or fully new on the next start. Deleting archives the Agent as an archive entry (`archive.md`): the store moves its tree into the entry's payload while the archive service archives its live Sessions, and moves the tree back when that fails. A restore moves the payload back, under the archived id or another one. The Agent directory also holds its private, always-allowed Skill home `skills/` (created on first write by `skill_manage` or skill RPCs, `skills.md`), Skill History `skill-history.jsonl` and Skill Archive `skill-archive/`; they move and archive with the Agent and are not seeded.

## Terms

### Rooting

The older term for the Default Project (GLOSSARY -> Default Project). Code and stored names keep the older term Rooting (`root_project_id`, `agents_rooted_in`, `unrooted_agents`, `--copy-rooted-agent-files`).

## Data Model

`agent.json` and `<data_dir>/agents/order.json` are Generation 1 JSON documents (`format_version` 1, unknown fields kept on every write, never overwritten after a failed load; contract in `json-documents.md`). Besides `format_version`, `id` is the only load-required field - every other field defaults when missing/null while present-but-malformed values invalidate that one Agent. Field semantics, Tool permission and the roster order -> `agent/configuration.md`. Fields every change in the domain must respect:

- `id` doubles as directory name, changed only through coordinated rename.
- Persisted empty models / null temperature/top_p/effort mean "no override": `AgentStore` bakes `defaults.agent` values into `get()`/`list()`/mutation results; raw disk stays unresolved (`get_raw` skips baking for the resolver's provenance seam).
- `root_project_id` is the Agent's default Project (GLOSSARY -> Default Project; code names: Terms -> Rooting): the Working Project of each new Session whose creator names none (`sessions.md` -> Terms -> Working Project). Selecting/clearing changes nothing else - existing Sessions keep their Working Project, and Workspace, private Skills, prompt customizations, permissions, and Sub-Agent addressing stay independent. Missing means new Sessions work in the Workspace; loading never infers from Workspace.
- `current_session_id` is empty or names a live Session; empty means a new conversation whose Session does not exist yet, and no read or repair creates a Session. The pointer (`agent.json`) and Sessions (SQLite) share no transaction, so reads that return it (`get`, `list`/`list_with_order`, and the loads inside `update`/`rename`) verify it and clear a dangling one; `get_raw` serves configuration provenance only and returns the stored pointer unverified. Because of that repair, Event-Loop callers read one Agent with `get_async`, which runs `get` as one unit on the Session database's pool (`ChatSessionManager.run_async`); Agent resolution uses it, and a closed Session database raises `DatabaseUnavailableError` (`tests/core/projects/test_resolver_scan_identity.py`).

## Built-in Agents

vBot creates three hidden Identity Agents itself (GLOSSARY -> Built-in Agent): the Librarian and the two Live Agents of a Live voice call. Each is an ordinary Identity Agent directory `agents/<id>/` whose `agent.json` carries `builtin`; the constants (`BUILTIN_AGENT_IDS`, `BUILTIN_AGENT_NAMES`) and `is_builtin_agent(agent)`, `is_librarian(agent)`, `is_live_agent(agent)` (any resolved Agent) live in `core/agents/_types.py`. Shared behavior (`test_agents.py`, `tests/core/runtime/test_runtime_wiring.py`):

- `builtin` (optional, written only when set) marks a built-in Agent vBot creates itself: `librarian`, `live_voice` or `live_backend`, each valid only on its reserved id (`librarian`, `live-voice`, `live-backend`; otherwise the Agent is invalid).
- **Fixed capabilities:** every load (`_agent_from_dict`) and create forces `memory_prompt_mode` `off`, no custom System Prompt, no Project root, no Tool settings, no `tool_loading` and `librarian_enabled` false, whatever `agent.json` says, and sets `fixed` on `tool_access` (`tools/access-policy.md` -> Policy resolution: no Memory or Session-grant Tool activates unless listed; followers still follow listed Tools). The Librarian's Tools are always `selected` `skill`/`skill_manage`; a Live Agent keeps its stored policy (`agent/builtin-agents.md` -> Live Agents). Skills, compaction policy and the Model settings stay as stored.
- **Lifecycle refusals (`BuiltinAgentError`, an `AgentError`, RPC `domain_error`):** `rename`, `archive_files` (so `agent.delete`), and `update` of any field but `model`, `fallback_models`, `temperature`, `top_p`, `thinking_effort` and `current_session_id`, plus `tool_access` on a Live Agent. `_validate_new_agent_id` reserves the built-in ids in any case for `create` and a rename target; `restore_target_problem` reports them as `agent_id_taken` for a restore under another id, also while the built-in Agent is missing.
- **Hidden surfaces:** `list`/`list_with_order` (roster, `agent.list`, roster order, `/agent` directory, Sub-Agent catalogs, Skill inventory, prompt scopes, the Librarian schedule) skip built-in Agents; `list_with_builtins()` appends the available ones for Statistics, so their Runs count as their own activity. `get`/`find`/`exists` and Agent resolution still return them, so their Sessions and Runs work like any Agent's. A Sub-Agent call naming one fails `agent_not_found` like an unknown Agent; Channels refuse one as `agent_id`; `/handoff` to one and `/agent` moves to or from one are refused with a notice naming it; Reflection never reviews one (`callable_review_dimensions` reports no dimension); a Librarian pass is refused for the Librarian (`gets no pass itself`) and fails `AgentNotFoundError` for a Live Agent. Automations are not refused: their pickers read the roster.

Creation, the Librarian's Skill subject and the Live Agents -> `agent/builtin-agents.md`.

## Uniform Agent Resolution

Run paths resolve through one seam, `AgentResolver.resolve_agent(project_id, agent_id)` (owned by `core/projects/`; details in `projects/resolution.md`) - never `runtime.agents.get(...)` directly. Identity branch (`project_id=None`): this domain's store, unchanged behavior. Project branch synthesizes a workspace-less Config Agent from the Team scan whose policy computes inside the Project Tool Whitelist (repository denials narrow; a vBot override fully replaces scanned policy within the ceiling).

Owner-managed temporary Agents resolve through `AgentResolver.resolve_temporary_agent` using an exact canonical Session generation. `core/agents/temporary.py` owns their immutable configuration, indexed bindings and execution groups (-> `agent/temporary-agents.md`). Team freshness and the Config Agent Model and scalar chains are owned by `projects/resolution.md`; Identity Agents keep their two-tier injection (Data Model).

## Conventions & Rules

- Agent IDs: filesystem-safe slugs, letter/digit start, letters/digits/hyphen/underscore, max 64 chars, under PROJECT.md -> Object IDs: `create`, the new id of `rename` and a `restore_files` under another id refuse reserved names (`_validate_new_agent_id`, `InvalidAgentIdError`), and every lookup requires a directory entry with exactly the requested spelling (`_stored_agent_path`), so a case variant is `AgentNotFoundError` (RPC `agent_not_found`, also when it arrives through Agent resolution; `projects/resolution.md`), while a persisted id disagreeing with its directory stays a plain `AgentError` (`test_agents.py`).
- A store-local reentrant lock serializes complete lifecycle read-modify-write operations, current-Session repairs, and roster revisions across worker threads. Event Loop code takes it for reads, so no thread waits for a data snapshot while holding it: every mutation, and `lifecycle_guard`, admits its Agent document change before the lock (`_change`), and a read that must repair (a dangling current-Session pointer, a missing Workspace field, the roster order) while a snapshot is being taken gives the lock up, waits for the snapshot and repeats (`_repairing_read`; `database.md` -> Freeze admission). A read that repairs nothing never waits, so Agent resolution goes on during a snapshot (`test_agents_mutations.py`). Writes use temp-file atomic replace; relative persisted Workspaces resolve only against the active data directory, never cwd.
- The only seeded template is `SOUL.md`; USER.md/MEMORY.md belong to the memory system (`memory.md` -> Overview). How to write a SOUL: the bundled `vbot-docs` Skill's `references/soul.md` (`cli.md`).
- Run-local model fallback never mutates persisted model/fallback fields (Chat owns runtime eligibility, `chat/run-execution.md`).

`normalize_agent_tools()` is the canonical normalizer for per-Tool Agent settings; Runtime Extension validation uses the same rules as Agent configuration.

## Store Operations

- `agents.py` keeps the locked public store and Session coordination; internal store units and compensated lifecycle operations are mapped in `agent/lifecycle.md`. Schema normalization stays in `_config.py`, Workspace paths/seeding/relocation in `_workspace.py`, delegation bindings in `_delegation.py`, temporary Agents in `temporary.py`. Model fallback validation reuses Settings' `validate_fallback_chain`; diagnostics and mutation errors keep their surface-specific wording.
- `create` seeds the Workspace and persists the config without creating a Session; a failure removes its new Agent directory again. Returned Agents are effective-resolved. `get` bakes defaults and stays strict for addressed invalid agents; `list` isolates individual unreadable configs (log+skip) and degrades to empty roster on enumeration failure so bootstrap can still run. `list_with_order`/`reorder` guard roster replacement with optimistic revision + exact-set matching (`AgentOrderConflictError` otherwise).
- `rename`, `revert_rename`, `recover_rename`, `archive_files` and `restore_files` change `sessions.db` and the Agent tree as one unit (the archive service commits the Sessions inside the last two), and `create` writes the Agent tree with its `agent.json` (no Session), so each enters the injected snapshot barrier before its document change and the store lock (`database.md` -> Snapshot consistency).
- `exists` is the validity-aware, never-raising probe behind identity-only gates such as private-skill layering and the skill RPC write scope. `find` is the same probe returning the stored Agent (raw persisted values, no `defaults.agent` baking, no Workspace seeding or current-Session repair) or `None`; Skill scoping reads `excluded_skills` through it.
- `lifecycle_guard` lets another domain hold the store's existing mutation lock across private-home lookup, writes and invalidation; like a mutation it admits an Agent document change first, so guarded work waits for a running data snapshot before it takes the lock. Runtime injects it into the registered Skill authoring Tool to protect shared-owner trees from concurrent rename/archive. Guarded work runs in a worker and must not call back into the Event Loop; Agent deletion also offloads its roster read, and the archive service its archive (`tests/core/runtime/test_runtime_skill_lifecycle.py`).

## Constraints & Gotchas

- Archive and restore move the Agent tree through `move_tree` (`core/utils/tree_move.py`), which is all or nothing: a refused or failed move leaves its source exactly as it was and creates no destination, so compensation only moves trees back and never deletes payload content (that may be the last complete copy of the Agent's data). Rename, archive and restore mechanics -> `agent/lifecycle.md`.
- `agent.delete` refuses the last Agent; the archive service refuses one with active/queued Runs (`agent_busy`) or one referenced by Channels or by a live automation (`agent_in_use`, listed by `Runtime.agent_references`): a non-terminal identity Bootstrap job or an identity Cron job that can still fire, as `AutomationReferences.agent_references` reports (`automation.md`); the handler holds the reference lock and the service the admission guard across the check and the archive. `AgentStore.archive_files` owns only the file move and its compensation, not those product-level guards. Delegation allow-lists never block deletion: the archive records and removes the Agent's grants, and a restore re-adds them only for the same holder incarnation (`agent/delegation.md`).
- Rename holds reference lock plus admission guards for both ids, refuses open Sub-Agent relations/collisions/invalid ids, a new id that still owns live Sessions, and a new id that a reference of another owner still names - a Channel or a non-terminal identity Cron or Bootstrap job - with `AgentReferencedError` (RPC `agent_in_use`) naming each one, before anything changes. It moves every live reference and keeps terminal history/logs historical. The whole rename, retargeting and revert included, holds the snapshot barrier, so an online data snapshot never sees the Sessions renamed but the Channels or Cron jobs not (`core/runtime/_agent_rename.py`; order in `runtime/agent-rename.md`).
- Roster order is the selection fallback for accessors without valid saved selection - it never changes current selections, Sessions, default Projects, or Run configuration; Config Agents come from Team scan order and are not in this document.

## Agent-facing text

This domain owns the description suggestion prompt (`AGENT_DESCRIPTION_SYSTEM_PROMPT`, `core/runtime/_agent_description.py`), a prompt for another Model job. Before you change, add or delete a sentence of it, read `agent/agent-facing-text.md`: it records why each sentence exists. Update its row for every sentence you change and add one for every sentence you add.

## References

Read these only when your task matches - not by default.

- Changing an `agent.json` field, its validation or surfaces (`description` and its suggestion, Models and fallbacks, Workspace relocation, current-Session repair, `tool_access` and `tools.<tool>` settings, `tool_loading`, Skills, `librarian_enabled`) or `agents/order.json` -> `agent/configuration.md`
- Agent rename, archive or restore mechanics in the store (store operations, tree moves, references a rename moves or refuses, rename record and recovery, `ensure_bootstrap`) -> `agent/lifecycle.md`
- Delegation allow-list grants across archive, restore and rename: explicit target bindings, grant removal and re-adding, `agents/retirements.json`, `delegation_target_allowed` -> `agent/delegation.md`
- Built-in Agent creation and problems, the Librarian's Skill subject, the Live Agents' Tools, Models and Sessions -> `agent/builtin-agents.md`
- Extension temporary Agents: `resolve_temporary_agent`, bindings, execution groups and `delete_group`, `prompt_blocks`, `compaction_policy`, `repository_profile` -> `agent/temporary-agents.md`
