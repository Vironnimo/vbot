# Project Configuration & Persistence

Read this reference when changing the persisted Project shape, the Project order, Project Anchor lifecycle, Project mutation RPCs, per-Agent overrides, or path and archive behavior.

## Persisted Project

`core/projects/projects.py` defines the immutable `Project` value and owns `validate_project_data`, `validate_project_file`, and `load_validated_project_json` for `<data-dir>/projects/<project-id>/project.json`. `ProjectStore` rebuilds the value through `build_project()` on create and update, so normalization and validation stay on one path.

`project.json` is a Generation 1 JSON document (`format_version` 2, unknown fields kept on update, never overwritten after a failed load; contract in `json-documents.md`; version 1 below). Besides `format_version`, a hand-edited persisted file requires `project_id` and `cwd` (they identify the Project Anchor and its repository) and the Tool ceiling `allowed_tools`. Every other field is optional and defaults during load. An override object holding only unknown fields loads as an empty entry that overrides nothing and keeps those fields on every write. The persisted contract contains:

- Identity and location: stable `project_id`, normalized repository `cwd`, optional user-facing `display_name` (missing, `null`, or blank falls back to `project_id`), and optional `created_at` / `updated_at` (missing values default to the current UTC timestamp).
- Runtime defaults: `default_agent`, `default_model`, `default_temperature`, `default_thinking_effort`, and `default_top_p` (written only when set).
- Discovery: ordered `sources` entries (`id`, `enabled`, optional repository-relative `agent_paths` patterns; a Skill folder Source is `folder:<path>` without `agent_paths`, `scanning.md` -> Ownership and inputs; older versions keep unknown ids inert), `model_mappings` from repository wishes to usable vBot Models. Unknown nested fields survive the guarded writer.
- `context_commands`: shell lines a new Session in the Project runs before its first Model request (`chat/context-commands.md`); optional, written only when non-empty, set through `project.set` only.
- Instructions: the switches `auto_instructions` (load the main instruction file) and `folder_instructions` (folder instruction notes), both on by default and written only when `false`, and `auto_load`, the additional files loaded after the main file. A converted Project whose `auto_load` still names the formerly seeded `AGENTS.md` loads it once, deduplicated against the main file.
- `memory_enabled` permits Agents to use this Project's shared Memory, on by default and written only when false. Disabling it retains the entries and their editing/history access for the user; an Agent also needs its own `project_memory_enabled` selection (`agent/configuration.md`). `ProjectStore.memory_workspace(project_id)` validates the exact live Project and returns its data-directory Anchor as the Memory home, independent of repository `cwd`.
- Tool ceiling: `allowed_tools`, seeded at creation from `PROJECT_DEFAULT_ALLOWED_TOOLS` (`read`, `apply_patch`, `search_files`, `bash`, `terminal`, `web_fetch`, `web_search`, `status`, `subagent`, and `skill`). A Project requires explicit names: the all-tools wildcard `"*"` is invalid. A persisted name that is not currently a registered Project Tool remains loadable so disabled Extension permissions survive; `project.show` reports it as `UNAVAILABLE_TOOL`, and the WebUI keeps it visible and removable.
- Skill ceiling: `skills_bundled_enabled`, `skills_global_enabled`, and `skills_project_disabled`.
- Per-Agent overrides: an `overrides` object keyed by Project Agent id. Supported override fields are exactly `model`, `temperature`, `top_p`, `thinking_effort`, `compaction_policy`, `tool_access`, `tool_loading`, `permission_rules` and `project_memory_enabled`. The last is an independent Boolean selection for that Team member, defaulting to true; it never overrides the Project-wide switch. Tool access uses the same strict policy shape as Identity Agents; an override's `allowed` and explicit `granted` names must be subsets of the Project Tool Whitelist. `tool_loading` uses the Identity Agent shape (`agent/configuration.md` -> `tool_loading`) and is stored as given, also `{"on_demand": false}` or a list without the switch; each part it leaves out is inherited from `defaults.agent.tool_loading`, so a Project Agent without it follows the shared default. It has no Project default, no ceiling check and no provenance. Team member payloads carry `tool_loading_default`, the complete inherited value (`AgentResolver.inherited_tool_loading()`), for the editor. `permission_rules` (`../permissions.md`) is normalized on write, `null` removes it, and an invalid stored set, also one with an unknown part, makes the Project invalid (`doctor config` reports it); it wins per part over the repository profile and the default (`resolution.md` -> Permission Rules).

The default Tool ceiling uses `apply_patch` as the file edit capability (its
followers `edit` and `write` are not configurable, `tools/edit.md`), and
`search_files` as its search capability. `background_tasks` follows `bash` and
`subagent` the same way and needs no entry (`tools/shell.md`). The application never rewrites explicit
persisted ceilings; an unavailable retired entry remains removable and grants
nothing.

Project defaults are fallback inputs shared by its Agents. Overrides target one current Team member and take precedence during resolution; they are not edits to the repository Agent file.

The named additive `project_sources_v1` backfill keeps Generation 1. An existing Project gets its previous Agent and Skill Sources first and active; other already-present Sources start off. For an OpenCode Project, `agent_paths` preserves the old reader's top-level Markdown scope, so newly supported JSON and nested definitions do not change its Team on open; the old Claude reader already walked `.claude/agents/`, so a Claude Project needs no limit. Those definitions remain visible in the scan report; removing the Source's path selection explicitly adopts them. The retired raw field is read only by this initializer and preserved as an unknown field. Loading does not rewrite the file. The next guarded mutation or explicit Source refresh persists the list. Agent ids, overrides and Session addresses do not change; later discoveries use the normal collision-safe activation rule.

### Format version 2 conversion

Version 2 changed what the instruction settings mean: in version 1 instruction files loaded only through `auto_load` (new Projects got `AGENTS.md` seeded) and the instruction Sources `claude.instructions` (`CLAUDE.md`, `.claude/CLAUDE.md`), `gemini.instructions` (`GEMINI.md`) and `copilot.instructions` (`.github/copilot-instructions.md`), each loading those of its files that existed. `core/projects/conversion.py` is the only code that knows version 1 (user decision 2026-10-22: keep what each existing Project loaded). Per document, it removes the instruction Sources from `sources`, appends the existing files of enabled ones to `auto_load` (each once, matched case-insensitively on Windows), and writes `auto_instructions: false` when the main instruction file was not among the loaded files (with no main file in the repository yet: unless `auto_load` named a candidate or an instruction Source was on). `main`-channel builds ran the version 2 meaning on version 1 documents from 2026-10-20 on, so a document that already holds `auto_instructions`, or has no instruction Source and was created after that change, keeps its instruction settings; a Project created later by a release build, with no instruction Source and the seeded `AGENTS.md` removed, therefore loads `AGENTS.md` after conversion. Archived Projects convert too (`core/archive/_conversion.py`: the payload, then the entry's `payload_format`), so they stay restorable. When it runs: `json-documents.md` -> Document rules. Gotcha: a rolled-back update restores live documents from the update snapshot, but no snapshot holds archive payloads, so the previous version sees a converted archived payload as `newer_format` until an update succeeds. Tests: `tests/core/projects/test_conversion.py`, the format-1 case in `tests/core/archive/test_archive_service.py`.

## Anchor Layout

`ProjectStore` in `core/projects/store.py` owns the data-dir paths; `core/projects/paths.py` owns cwd normalization and slug rules:

```text
<data-dir>/projects/
  order.json              the user's Project order (below)
  <project-id>/
    project.json
```

Older anchors may still hold an empty, unused `agents/` directory. Project-scoped Sessions are addressed by `(project_id, agent_id, session_id)` in the canonical `<data-dir>/sessions.db`; the Project Anchor contains no Session files. Keep path construction behind Store helpers rather than joining untrusted identifiers at call sites.

The repository at `cwd` remains outside the anchor and is never mutated. Changing `cwd` keeps the Project id, anchor, and existing Sessions.

## Store Contract

`core/projects/store.py` owns persistence:

- A store-local reentrant lock serializes complete lifecycle and override read-modify-write operations, including duplicate-cwd checks, across Command and RPC workers. Event Loop code takes it for reads, so every mutation admits its Project document change before the lock (`_change`): a change that waits for a data snapshot waits without it (`database.md` -> Freeze admission; `test_store.py`).
- `configuration_guard()` holds that same admission and lock for a synchronous dependent write. Skill merges use it between the Agent lifecycle and Skill authoring guards so a Project Context grant or source selection cannot change between the successor-access check and deletion; no guard spans an await.
- Creation rejects duplicate Project ids and duplicate normalized cwd identity keys, builds the Project through the Projects-owned validator, and writes `project.json` atomically. It seeds no `auto_load` entry: the main instruction file loads through `auto_instructions`. A failed write removes the newly created anchor so creation can be retried.
- Updates preserve `project_id` and `created_at`, reject a cwd already owned by another Project, rebuild the complete value, and write atomically.
- Listing follows the user's Project order and skips corrupt Project files with a warning instead of failing the entire collection. `get` and `list` use strict filesystem status: access and I/O failures propagate, including the read-error cause of a wrapped configuration failure, rather than becoming missing Projects or an empty catalog. `exists(project_id)` is validity-aware and returns false for an unreadable or invalid config rather than reporting directory presence as a usable Project.
- `set_override()` and `clear_override()` atomically rewrite one supported field. Clearing the last field removes the Agent's override object unless it still holds unknown fields, which stay on disk; clearing an absent value is a no-op. Both return the Project as persisted.
- `archive_files(project_id, tree)` moves the Project Anchor into an archive payload around the archive service's Session commit and yields the Project (`None` when its config cannot be read; the Anchor is archived all the same); repository content is never touched. The Session commit refuses while a live owner-managed (Extension) Session is in the Project, with an error naming its owner (`sessions/owned-execution.md` -> Authority); it leaves when its owner deletes or archives its group, or when core archives it after the owner was removed (`extensions/host.md` -> Owner-managed Session lifetime). Archived ones never block (`tests/core/sessions/test_archive_ledger.py`). The anchor moves through the all-or-nothing `move_tree` (`core/utils/tree_move.py`, see `agent.md`), so a failed move leaves the anchor untouched, and a failing commit moves it back; if that fails too, the error names where the Project files remain. `restore_files(source, target_id)` refuses a taken id and a repository another Project claims before anything changes, moves the Anchor back and rewrites its `project.json` to the target id (unknown fields stay); a failure writes the archived `project.json` bytes back before the Anchor returns, so the payload stays unchanged; `complete_restore_files(target_id, source_id=...)` replays an interrupted configuration rewrite after the Anchor moved, accepting only the source or target id and rechecking repository ownership; `inspect_archived` and `restore_target_problem` report what blocks a restore. These are persistence primitives, not the user-facing removal or restore workflow (`archive.md`).

## Project order

`<data-dir>/projects/order.json` (`core/projects/_order.py`, Generation 1, `doctor config` kind `project_order`) holds `{revision, project_ids}`: the one order every accessor shows Projects in, the Projects tab and Chat's by-Project Session list alike (user requirement 2026-10-20). `list()` / `list_with_order()` return Projects in it; Projects it does not name follow in id order, ids of Projects no longer registered are skipped, and a missing, unreadable or invalid document means id order with `order_revision` 0 (invalid: warned once, reported by `doctor config`, never overwritten). Reads, create, archive and restore never write it, so an archived Project restored under its id returns to its old place until a reorder drops the id. `reorder(project_ids, expected_revision)` stores exactly the registered Projects: a different id set or a stale revision raises `ProjectOrderConflictError` (RPC `project_order_conflict`), repeating the current order is a no-op, and every stored order raises the revision by one (`test_store.py`).

`normalize_cwd()` resolves an absolute real path, strips trailing separators, and preserves case. `cwd_identity_key()` additionally case-folds on Windows and is the duplicate-detection key. The Store intentionally permits a cwd that does not currently exist; the `project.add` RPC is the boundary that requires an existing directory.

## Mutation RPCs

`server/rpc/project_methods.py` exposes `project.add`, `project.list`, `project.show`, `project.set`, `project.set_override`, `project.clear_override`, `project.rm`, `project.reorder`, and `project.detect`.

- `project.list` answers the Projects in the user's order plus `order_revision`; `project.reorder {project_ids, expected_revision}` stores a new complete order (Project order above), answers like `project.list` without scans and publishes a `projects` invalidation when the order changed (`test_project_methods.py`).
- `project.add` validates that `cwd` exists, detects all supported Sources when no Source list is supplied, persists the Project, and returns the Project with its scan result.
- `project.list` with `include_scan: true` adds each Project's `scan` (Team with `effective` values plus the report including unavailable-Tool findings, no Skills) from the resolver's cached scan, scanning only a Project not yet cached; a Project whose scan fails with `OSError` lists `scan: null`. A cached Team shows a repository change only after an open (`project.show`) or a changed Source list, location or Model mapping rescans it. Chat's Agent picker and the WebUI's Agent target pickers (`agentTargetOptions.js`) use it to list every Team without a `project.show` per Project, which rescanned every repository on each picker load (`test_project_methods.py`).
- `project.show` refreshes the Project's Sources while the global Skill reload runs, drops the Team cache, rescans that Project's Skills (`refresh_project_skills`, which keeps the cached registries when nothing changed) and the Team, and returns current Project plus scan information (`skills/manager.md` -> Invalidation). Concurrent shows share one global Skill rescan that started after each request (`runtime/hot-reload.md`). `project.list` and `project.show` read Anchors, reload Skills (`reload_skills_async`), and scan on worker threads (`project` pool), never on the Event Loop; only cache invalidation runs on the loop.
- `project.add`, `project.set`, `project.set_override` and `project.clear_override` validate, write the Anchor and scan on the same `project` pool, since a write may wait for a data snapshot; they run one at a time (`serialized_mutation`, as they did on the Event Loop), and cache invalidation and resource publication stay on the loop (`test_project_methods.py`).
- `project.add` and `project.set` adopt newly listed Skill folder Sources against the (new) `cwd` and refuse a folder that is no existing directory or is already a Source (`invalid_request`); the WebUI adds a folder first among the Skill Sources and saves at once.
- `project.set` updates persisted fields. Clearing `display_name` sends `null` and restores the `project_id` fallback. For `allowed_tools`, it accepts registered Tools whose declarative activation is directly configurable and whose constraints permit Project Agents; automatic or Identity-only Tools are excluded. An unavailable name already present may be carried forward or removed, but an RPC caller cannot introduce a new unavailable name. Every material Project update invalidates discovery and Skill caches before returning the new projection: Identity Skill registries capture Project grants and labels as well as source files. No-op updates retain caches (`test_project_methods.py`).
- Override mutation requires the target Agent to be on the current Project Team. A Model value calls `AgentResolver.require_model_configured`, the same raising domain seam used by Chat `/model`; RPC maps `ModelConfigurationError` to `invalid_request`. Temperature and thinking effort use canonical scalar validators; compaction policy uses the Settings normalizer; Tool access uses `normalize_tool_access` plus the live Project Tool Whitelist subset check. `OVERRIDE_FIELDS` in `core/projects/projects.py` is the authoritative supported-field set. Clearing `tool_access` immediately restores the scanned repository Tool policy and never edits the repository Agent file.
- Every Project payload adds the read-only `instruction_status`: `main_file`, the main instruction file the repository has now (also while `auto_instructions` is off; `null` when the repository is missing), and `missing`, the `auto_load` entries that are no file. Switches and entries are never rewritten from it.
- Successful mutations publish the relevant `resource_changed` events so connected clients refresh Projects and Agents.

## Removal Coordination

`project.rm` holds the server Agent-reference lock and calls `ArchiveService.archive_project` (`archive.md`), which coordinates the domain boundaries; the RPC maps its refusals (`project_in_use`, `project_busy`) and returns the archive entry id with the Agents whose default Project it cleared and their copied and backed-up identity files:

- It acquires `ChatRunManager.project_admission_guard`. Guard acquisition atomically rejects active or queued Project-anchored work and work of Identity Sessions whose Working Project (`working_project_id`) is the Project; while held, every Run ingress rejects new work for either relationship until removal finishes.
- It rejects removal while a live Cron or Bootstrap job targets an Agent of the Project (`AutomationReferences.project_references`, `automation.md`).
- It identifies Identity Agents whose default Project it is and records each reset (`unrooted_agents`) in the entry before it happens. When their Workspace moves back to the Agent default, the workflow can preserve `SOUL.md`, `USER.md`, and `MEMORY.md`, updates those Agents, and rolls back the coordinated changes if removal fails; a restore gives them the default Project again.
- It archives the Project Anchor and Project Sessions, and invalidates Team and Skill caches; Identity Sessions working in the Project stay live and refuse Runs until a restore (`projects.md`); the RPC publishes Agent and Project resource changes.

Do not move these product-level guards into a low-level filesystem helper or call the Store archive primitive as a substitute for the archive service's removal workflow.

## Validation Ownership

Project configuration reuses canonical owners:

- Model usability -> `ModelConfigurationChecker` in `core/projects/resolver.py`, consuming Models, Providers, and credential usability. `is_configured` remains the fallback/scan query; `require_configured` is the mutation guard and preserves a precise diagnostic when a known Model is pinned to a forbidden Connection.
- Temperature and thinking effort -> the validators exported by `core/settings/`.
- Compaction policy -> `core/settings/normalizers.py`.
- Identifier safety and addresses -> `core/projects/paths.py` and `core/projects/address.py`.
- Project Tool Whitelist membership -> the live `ToolRegistry` catalog at the RPC/scan-preview boundary. `core.projects.project_tool_configurability_reason()` derives machine-readable exclusion reasons from each Tool's declarative activation and constraints; `tool.list` projects `project_configurable` plus `project_configurability_reason` so accessors never mirror Tool-name policy. Raw file validation rejects the wildcard but deliberately does not require runtime registry membership.

When adding a persisted field, decide whether it is a Project default, a capability ceiling, or a per-Agent override; update serialization, Store rebuild/update paths, RPC validation, WebUI state, resolver consumption, tests, and this reference together.

## Source & Tests

- Entity, defaults, overrides, serialization: `core/projects/projects.py`
- Persistence and the Anchor's archive and restore: `core/projects/store.py`
- Anchor paths: `core/projects/store.py`; cwd and slug rules: `core/projects/paths.py`
- Address parsing: `core/projects/address.py`
- Public mutations: `server/rpc/project_methods.py`; removal coordination: `core/archive/` (`archive.md`)
- Primary tests: `tests/core/projects/test_projects.py`, `tests/core/projects/test_store.py`, `tests/server/rpc/test_project_methods.py`, and `tests/server/rpc/test_project_methods_delete.py`
