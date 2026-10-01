# Project Configuration & Persistence

Read this reference when changing the persisted Project shape, Project Anchor lifecycle, Project mutation RPCs, per-Agent overrides, or path and archive behavior.

## Persisted Project

`core/projects/projects.py` defines the immutable `Project` value and owns `validate_project_data`, `validate_project_file`, and `load_validated_project_json` for `<data-dir>/projects/<project-id>/project.json`. `ProjectStore` rebuilds the value through `build_project()` on create and update, so normalization and validation stay on one path.

`project.json` is a Generation 1 JSON document (`format_version` 1, unknown fields kept on update, never overwritten after a failed load; contract in `settings.md`). Besides `format_version`, a hand-edited persisted file requires `project_id` and `cwd` (they identify the Project Anchor and its repository) and the Tool ceiling `allowed_tools`. Every other field is optional and defaults during load. An override object holding only unknown fields loads as an empty entry that overrides nothing and keeps those fields on every write. The persisted contract contains:

- Identity and location: stable `project_id`, normalized repository `cwd`, optional user-facing `display_name` (missing, `null`, or blank falls back to `project_id`), and optional `created_at` / `updated_at` (missing values default to the current UTC timestamp).
- Runtime defaults: `default_agent`, `default_model`, `default_temperature`, and `default_thinking_effort`.
- Discovery: one `source_format` (`opencode` or `claude`) and `auto_load`.
- Tool ceiling: `allowed_tools`, seeded at creation from `PROJECT_DEFAULT_ALLOWED_TOOLS` (`read`, `apply_patch`, `search_files`, `bash`, `process`, `terminal`, `web_fetch`, `web_search`, `status`, `subagent`, and `skill`). A Project requires explicit names: the all-tools wildcard `"*"` is invalid. A persisted name that is not currently a registered Project Tool remains loadable so disabled Extension permissions survive; `project.show` reports it as `UNAVAILABLE_TOOL`, and the WebUI keeps it visible and removable.
- Skill ceiling: `skills_bundled_enabled`, `skills_global_enabled`, and `skills_project_disabled`.
- Per-Agent overrides: an `overrides` object keyed by Project Agent id. Supported override fields are exactly `model`, `temperature`, `thinking_effort`, `compaction_policy`, and `tool_access`. Tool access uses the same strict policy shape as Identity Agents; an override's `allowed` and explicit `granted` names must be subsets of the Project Tool Whitelist.

The default Tool ceiling uses `apply_patch` instead of the archived `edit`, and
`search_files` as its search capability. The application never rewrites explicit
persisted ceilings; an unavailable retired entry remains removable and grants
nothing.

Project defaults are fallback inputs shared by its Agents. Overrides target one current Team member and take precedence during resolution; they are not edits to the repository Agent file.

## Anchor Layout

`core/projects/paths.py` owns all data-dir paths:

```text
<data-dir>/projects/<project-id>/
  project.json
```

Older anchors may still hold an empty, unused `agents/` directory. Creation seeds the repository-relative `AGENTS.md` as the first `auto_load` entry; later Project updates never reseed it. Project-scoped Sessions are addressed by `(project_id, agent_id, session_id)` in the canonical `<data-dir>/sessions.db`; the Project Anchor contains no Session files. Keep path construction behind Store helpers rather than joining untrusted identifiers at call sites.

The repository at `cwd` remains outside the anchor and is never mutated. Changing `cwd` keeps the Project id, anchor, and existing Sessions.

## Store Contract

`core/projects/store.py` owns persistence:

- A store-local reentrant lock serializes complete lifecycle and override read-modify-write operations, including duplicate-cwd checks, across Command and RPC workers. Event Loop code takes it for reads, so every mutation admits its Project document change before the lock (`_change`): a change that waits for a data snapshot waits without it (`database.md` -> Freeze admission; `test_store.py`).
- Creation rejects duplicate Project ids and duplicate normalized cwd identity keys, builds the Project through the Projects-owned validator, and writes `project.json` atomically. It seeds `AGENTS.md` in the auto-load list without creating repository instructions; a failed write removes the newly created anchor so creation can be retried.
- Updates preserve `project_id` and `created_at`, reject a cwd already owned by another Project, rebuild the complete value, and write atomically.
- Listing is deterministic and skips corrupt Project files with a warning instead of failing the entire collection. `exists(project_id)` is validity-aware and returns false for an unreadable or invalid config rather than reporting directory presence as a usable Project.
- `set_override()` and `clear_override()` atomically rewrite one supported field. Clearing the last field removes the Agent's override object unless it still holds unknown fields, which stay on disk; clearing an absent value is a no-op. Both return the Project as persisted.
- `archive_files(project_id, tree)` moves the Project Anchor into an archive payload around the archive service's Session commit and yields the Project (`None` when its config cannot be read; the Anchor is archived all the same); repository content is never touched. The Session commit refuses while a live owner-managed (Extension) Session is in the Project, with an error naming its owner (`sessions/owned-execution.md` -> Authority); it leaves when its owner deletes or archives its group, or when core archives it after the owner was removed (`extensions.md`). Archived ones never block (`tests/core/sessions/test_archive_ledger.py`). The anchor moves through the all-or-nothing `move_tree` (`core/utils/tree_move.py`, see `agent.md`), so a failed move leaves the anchor untouched, and a failing commit moves it back; if that fails too, the error names where the Project files remain. `restore_files(source, target_id)` refuses a taken id and a repository another Project claims before anything changes, moves the Anchor back and rewrites its `project.json` to the target id (unknown fields stay); a failure writes the archived `project.json` bytes back before the Anchor returns, so the payload stays unchanged; `inspect_archived` and `restore_target_problem` report what blocks a restore. These are persistence primitives, not the user-facing removal or restore workflow (`archive.md`).

`normalize_cwd()` resolves an absolute real path, strips trailing separators, and preserves case. `cwd_identity_key()` additionally case-folds on Windows and is the duplicate-detection key. The Store intentionally permits a cwd that does not currently exist; the `project.add` RPC is the boundary that requires an existing directory.

## Mutation RPCs

`server/rpc/project_methods.py` exposes `project.add`, `project.list`, `project.show`, `project.set`, `project.set_override`, `project.clear_override`, `project.rm`, and `project.detect`.

- `project.add` validates that `cwd` exists, detects a source format when none is supplied, persists the Project, and returns the Project with its scan result.
- `project.show` reloads Skills, invalidates relevant caches, rescans the repository, and returns current Project plus scan information. Pickers issue one show per Project, so `project.list` and `project.show` read Anchors, reload Skills (`reload_skills_async`), and scan on worker threads (`project` pool), never on the Event Loop; only cache invalidation runs on the loop.
- `project.add`, `project.set`, `project.set_override` and `project.clear_override` validate, write the Anchor and scan on the same `project` pool, since a write may wait for a data snapshot; they run one at a time (`serialized_mutation`, as they did on the Event Loop), and cache invalidation and resource publication stay on the loop (`test_project_methods.py`).
- `project.set` updates persisted fields. Clearing `display_name` sends `null` and restores the `project_id` fallback. For `allowed_tools`, it accepts registered Tools whose declarative activation is directly configurable and whose constraints permit Project Agents; automatic or Identity-only Tools are excluded. An unavailable name already present may be carried forward or removed, but an RPC caller cannot introduce a new unavailable name. Every material Project update invalidates discovery and Skill caches before returning the new projection: Identity Skill registries capture Project grants and labels as well as source files. No-op updates retain caches (`test_project_methods.py`).
- Override mutation requires the target Agent to be on the current Project Team. A Model value calls `AgentResolver.require_model_configured`, the same raising domain seam used by Chat `/model`; RPC maps `ModelConfigurationError` to `invalid_request`. Temperature and thinking effort use canonical scalar validators; compaction policy uses the Settings normalizer; Tool access uses `normalize_tool_access` plus the live Project Tool Whitelist subset check. `OVERRIDE_FIELDS` in `core/projects/projects.py` is the authoritative supported-field set. Clearing `tool_access` immediately restores the scanned repository Tool policy and never edits the repository Agent file.
- Successful mutations publish the relevant `resource_changed` events so connected clients refresh Projects and Agents.

## Removal Coordination

`project.rm` holds the server Agent-reference lock and calls `ArchiveService.archive_project` (`archive.md`), which coordinates the domain boundaries; the RPC maps its refusals (`project_in_use`, `project_busy`) and returns the archive entry id with the unrooted Agents and their copied and backed-up identity files:

- It acquires `ChatRunManager.project_admission_guard`. Guard acquisition atomically rejects active or queued Project-anchored work and Identity-Agent work whose internal `working_project_id` selects the Project; while held, every Run ingress rejects new work for either relationship until removal finishes.
- It rejects removal while a live Cron job, Bootstrap job or Calendar action targets an Agent of the Project (`AutomationReferences.project_references`, `automation.md`).
- It identifies Identity Agents rooted in the Project and records each unroot in the entry before it happens. When their Workspace moves back to the Agent default, the workflow can preserve `SOUL.md`, `USER.md`, and `MEMORY.md`, updates those Agents, and rolls back the coordinated changes if removal fails; a restore roots them again.
- It archives the Project Anchor and Sessions, and invalidates Team and Skill caches; the RPC publishes Agent and Project resource changes.

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
- Anchor and cwd path rules: `core/projects/paths.py`
- Address parsing: `core/projects/address.py`
- Public mutations: `server/rpc/project_methods.py`; removal coordination: `core/archive/` (`archive.md`)
- Primary tests: `tests/core/projects/test_projects.py`, `tests/core/projects/test_store.py`, `tests/server/rpc/test_project_methods.py`, and `tests/server/rpc/test_project_methods_delete.py`
