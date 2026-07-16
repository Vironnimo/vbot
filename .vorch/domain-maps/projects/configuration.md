# Project Configuration & Persistence

Read this reference when changing the persisted Project shape, Project Anchor lifecycle, Project mutation RPCs, per-Agent overrides, or path and archive behavior.

## Persisted Project

`core/projects/projects.py` defines the immutable `Project` value and owns `validate_project_data`, `validate_project_file`, and `load_validated_project_json` for `<data-dir>/projects/<project-id>/project.json`. `ProjectStore` rebuilds the value through `build_project()` on create and update, so normalization and validation stay on one path.

The persisted contract contains:

- Identity and location: stable `project_id`, user-facing `display_name`, normalized repository `cwd`, `created_at`, and `updated_at`.
- Runtime defaults: `default_agent`, `default_model`, `default_temperature`, and `default_thinking_effort`.
- Discovery: one `source_format` (`opencode` or `claude`) and `auto_load`.
- Tool ceiling: `allowed_tools`, seeded from `PROJECT_DEFAULT_ALLOWED_TOOLS` (`read`, `write`, `edit`, `glob`, `grep`, `bash`, `process`, `web_fetch`, `web_search`, `status`, `subagent`, and `skill`). A Project requires explicit names: the all-tools wildcard `"*"` is invalid. A persisted name that is not currently a registered Project tool remains loadable so disabled Extension permissions survive; `project.show` reports it as `UNAVAILABLE_TOOL`, and the WebUI keeps it visible and removable.
- Skill ceiling: `skills_bundled_enabled`, `skills_global_enabled`, and `skills_project_disabled`.
- Per-Agent overrides: an `overrides` object keyed by Project Agent id. Supported override fields are exactly `model`, `temperature`, `thinking_effort`, and `compaction_policy`.

Project defaults are fallback inputs shared by its Agents. Overrides target one current Team member and take precedence during resolution; they are not edits to the repository Agent file.

## Anchor Layout

`core/projects/paths.py` owns all data-dir paths:

```text
<data-dir>/projects/<project-id>/
  project.json
  AGENTS.md
  agents/<agent-id>/sessions/
```

Creation seeds the anchor `AGENTS.md` if it is missing; later Project updates never reseed it. Session paths are derived from validated Project and Agent ids. Keep path construction behind these helpers rather than joining untrusted identifiers at call sites.

The repository at `cwd` remains outside the anchor and is never mutated. Changing `cwd` keeps the Project id, anchor, and existing Sessions.

## Store Contract

`core/projects/store.py` owns persistence:

- Creation rejects duplicate Project ids and duplicate normalized cwd identity keys, builds the Project through the Projects-owned validator, writes `project.json` atomically, and seeds the anchor instructions.
- Updates preserve `project_id` and `created_at`, reject a cwd already owned by another Project, rebuild the complete value, and write atomically.
- Listing is deterministic and skips corrupt Project files with a warning instead of failing the entire collection.
- `set_override()` and `clear_override()` atomically rewrite one supported field. Clearing the last field removes the Agent's override object; clearing an absent value is a no-op.
- Store deletion archives the Project Anchor rather than deleting repository content. It is a persistence primitive, not the complete user-facing removal workflow.

`normalize_cwd()` resolves an absolute real path, strips trailing separators, and preserves case. `cwd_identity_key()` additionally case-folds on Windows and is the duplicate-detection key. The Store intentionally permits a cwd that does not currently exist; the `project.add` RPC is the boundary that requires an existing directory.

## Mutation RPCs

`server/rpc/project_methods.py` exposes `project.add`, `project.list`, `project.show`, `project.set`, `project.set_override`, `project.clear_override`, `project.rm`, and `project.detect`.

- `project.add` validates that `cwd` exists, detects a source format when none is supplied, persists the Project, and returns the Project with its scan result.
- `project.show` reloads Skills, invalidates relevant caches, rescans the repository, and returns current Project plus scan information.
- `project.set` updates persisted fields. For `allowed_tools`, it accepts registered normal non-Session-scoped Project tools, excluding `memory` and `skill_manage`; an unavailable name already present may be carried forward or removed, but an RPC caller cannot introduce a new unavailable name. Changing `cwd` or `source_format` invalidates discovery/resolution caches because Team membership may change.
- Override mutation requires the target Agent to be on the current Project Team. A Model value calls `AgentResolver.require_model_configured`, the same raising domain seam used by Chat `/model`; RPC maps `ModelConfigurationError` to `invalid_request`. Temperature and thinking effort use canonical scalar validators; compaction policy uses the Settings normalizer. `OVERRIDE_FIELDS` in `core/projects/projects.py` is the authoritative supported-field set.
- Successful mutations publish the relevant `resource_changed` events so connected clients refresh Projects and Agents.

## Removal Coordination

`project.rm` coordinates domain boundaries before invoking archive storage:

- It acquires `ChatRunManager.project_admission_guard` under the server Agent-reference lock. Guard acquisition atomically rejects active or queued Project-anchored work and Identity-Agent work whose internal `working_project_id` selects the Project; while held, every Run ingress rejects new work for either relationship until removal finishes.
- It rejects removal while the Project is referenced by Cron configuration.
- It identifies Identity Agents rooted in the Project. When their Workspace moves back to the Agent default, the workflow can preserve `SOUL.md`, `USER.md`, and `MEMORY.md`, updates those Agents, and rolls back the coordinated changes if removal fails.
- It archives the Project Anchor, invalidates Team and Skill caches, and publishes Agent and Project resource changes.

Do not move these product-level guards into a low-level filesystem helper or call the Store archive primitive as a substitute for the RPC removal workflow.

## Validation Ownership

Project configuration reuses canonical owners:

- Model usability → `ModelConfigurationChecker` in `core/projects/resolver.py`, consuming Models, Providers, and credential usability. `is_configured` remains the fallback/scan query; `require_configured` is the mutation guard and preserves a precise diagnostic when a known Model is pinned to a forbidden Connection.
- Temperature and thinking effort → the validators exported by `core/settings/`.
- Compaction policy → `core/settings/normalizers.py`.
- Identifier safety and addresses → `core/projects/paths.py` and `core/projects/address.py`.
- Project Tool Whitelist membership → the live `ToolRegistry` catalog at the RPC/scan-preview boundary. `core.projects.project_tool_configurability_reason()` owns the exceptions and their machine-readable reasons; `tool.list` projects `project_configurable` plus `project_configurability_reason` so accessors never mirror Tool-name policy. Raw file validation rejects the wildcard but deliberately does not require runtime registry membership.

When adding a persisted field, decide whether it is a Project default, a capability ceiling, or a per-Agent override; update serialization, Store rebuild/update paths, RPC validation, WebUI state, resolver consumption, tests, and this reference together.

## Source & Tests

- Entity, defaults, overrides, serialization: `core/projects/projects.py`
- Persistence and archive lifecycle: `core/projects/store.py`
- Anchor and cwd path rules: `core/projects/paths.py`
- Address parsing: `core/projects/address.py`
- Public mutations and removal coordination: `server/rpc/project_methods.py`
- Primary tests: `tests/core/projects/test_projects.py`, `tests/core/projects/test_store.py`, and `tests/server/rpc/test_project_methods.py`
