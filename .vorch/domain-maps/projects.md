# Projects

The Projects domain turns a repository location into a persistent vBot execution boundary with its own Agent discovery, defaults, Tool and Skill ceilings, overrides, and Project Agent resolution.

## Overview

`core/projects/` owns the Project entity, its persisted `project.json` schema and load gate, its data-dir anchor, Source discovery and translation, and the resolution of a Project Agent into effective runtime configuration. A Project points at a repository through `cwd`; vBot reads supported configuration from that repository but never writes Project metadata or Session state into it. The domain consumes shared scalar rules from Settings but does not own Chat or Run lifecycle, the Session store, or the Tool and Skill implementations whose availability it constrains.

## Terms

Core terms such as Project, Project Agent, Project Context, Team and Working Project live in `.vorch/GLOSSARY.md`.

### Project Anchor
The vBot-owned directory of one Project in the data directory, holding its `project.json`: the Project's durable identity on disk (`projects/configuration.md` -> Anchor Layout). Not the repository at the Project's `cwd`; changing `cwd` does not move or replace it.

### Project Tool Whitelist
The Project-owned set of directly configurable Tools that bounds every Project Agent: resolution, a vBot per-Agent Tool override included, may narrow it for one Agent but never widen it (`projects/resolution.md` -> Effective Capabilities).

### Project Skill Whitelist
The Project-owned selection of bundled, global and discovered Project Skills that bounds every Project Agent: resolution may narrow it for one Agent but never widen it (`projects/resolution.md` -> Effective Capabilities).

### Source
An input for a Project's Agents or Skills: a repository location vBot detects or a Skill folder the user adds, inside or outside the repository. A Project keeps its Sources in one ordered, switchable list in which the earliest active definition of a name wins (`projects/scanning.md`); instruction files are not Sources.

### Agent Profile
The format-neutral definition of one Team member that a Source adapter reads from the repository, before Project defaults and overrides apply (`projects/scanning.md` -> Ownership and inputs). Not a stored Identity Agent or a runtime Agent kind.

## Boundary & Invariants

- `project_id` is stable and names the Project Anchor; `cwd` is a mutable pointer to the repository. Repository equality and working-directory equality never establish Project identity.
- Project-owned state lives under the data directory. The repository and every Skill folder Source are read-only configuration sources: adapters may read their Agent and Skill files, but removal archives only the Project Anchor and never deletes or modifies their content.
- A Project combines ordered, switchable Sources for Agents and Skills. Earlier active names win; shadowed definitions and unreadable inputs remain visible. New Sources activate on detection unless an active Agent/Skill name collides. Instruction files are no Sources: the version 1 converter turns old `*.instructions` selections into `auto_load` entries (`projects/configuration.md` -> Format version 2 conversion).
- `project.json` owns Project defaults, capability ceilings, Skill selections, and per-Agent overrides. Runtime resolution combines those values with freshly read repository Agent configuration and global defaults; it does not copy repository Agent files into the Project Anchor.
- Project Agent Sessions use `(project_id, agent_id, session_id)` addresses in `<data-dir>/sessions.db`; the Project Anchor no longer contains canonical Session files. Agent and Project identifiers still pass shared validation before entering either database addresses or filesystem anchors.
- Without a vBot Tool override, Tool access starts from the Project Tool Whitelist; Profile allowlists, rules and denials narrow it to the Agent's own Tools; followers and the Session's grants (`message_parent` for a delegated Run) still activate. A present `overrides.<agent_id>.tool_access` completely replaces that repository Tool policy and may intentionally re-enable a repo-denied Tool, while still remaining inside the Project Tool Whitelist; mode `none` can remove everything and `selected` can narrow the Project Agent to one Tool. Skill access follows `(project skills + enabled bundled skills + enabled global skills) - disabled project skills - {"*"}`; neither repository configuration nor an Agent override may exceed Project ceilings.
- Models, temperature, top_p, thinking effort, and compaction policy use the same canonical validators as global settings. Do not create Project-local validation rules or bypass the shared usable-model gate.
- Temporary Agents with an explicitly selected Project resolve through `resolver.py` without joining its Team. Like an Identity Session working in a Project they use the Project's directory, Skills and context, while their owner's snapshotted Tool selection and Skill allowlist apply as configured: the Project Tool Whitelist and Skill rule bound only the Team (user decision 2026-09-28: a Swarm profile decides what its participants may use). Chat admits that Project as the Run's working Project on both initial temporary Runs and owned continuations. A directory alone does not select a Project (`projects/resolution.md` -> Change Rules).

## Ownership Routing

- Change persisted Project fields, anchor layout, CRUD behavior, overrides, path normalization, or the Anchor's archive and restore in `core/projects/projects.py`, `core/projects/store.py`, `core/projects/paths.py`, and the Project RPC boundary; the removal workflow itself lives in `core/archive/` (`archive.md`). Read `projects/configuration.md` first.
- `_order.py` owns the Project order document; `core/utils/user_order.py` shares only pure order validation, projection and reorder decisions with Agents. Projects retains its read-without-write lifecycle (`projects/configuration.md` -> Project order).
- Change repository Agent discovery, Source adapters, detection, translation, collisions, or findings in `core/projects/sources/` and `core/projects/scan_report.py`; the Project entity validates Sources and Model mappings. Read `projects/scanning.md` first.
- Change Project Agent orchestration and working-Project helpers in `core/projects/resolver.py`; `_runtime_agent.py` defines resolved contracts, `_model_configuration.py` owns the usable-Model gate, and `_resolution_values.py` owns capability ceilings, scalar fallback and provenance. These are internal parts of the same Projects owner. Read `projects/resolution.md` first.
- Change explicit foreign Project Context loading for Identity Agents in `core/tools/project.py`; Projects owns the registered records and repository pointers it reads but does not infer context from arbitrary filesystem paths or own the Tool result (see `tools/project.md`).
- Change central model availability or scalar-setting validation in Models, Providers, or Settings, not here. Projects consumes those contracts.
- Change Session persistence, Run lifecycle, Chat behavior, or Tool/Skill implementation in their owning domains. Projects supplies identity, storage anchors, and capability/configuration inputs only.

## Constraints & Gotchas

- `normalize_cwd()` resolves an absolute real path, removes trailing separators, and preserves case. `cwd_identity_key()` additionally case-folds only on Windows; use it for duplicate detection instead of comparing display paths.
- Project ids follow PROJECT.md -> Object IDs: `ProjectStore.create()` and `restore_files` under another id refuse reserved names (`InvalidProjectIdError`), and `project.add`, which derives the id from the display name, refuses such a slug first as `invalid_request`, asking for another `display_name`. Store lookups (`get` and everything built on it, `exists`, `archive_files`, `session_owning_agents`) require an Anchor entry with exactly the requested spelling (`_stored_project_dir`), so a case variant is `ProjectNotFoundError` (RPC `project_not_found`), also through Agent resolution (`projects/resolution.md`); a `project.json` id that disagrees with its Anchor stays a plain `ProjectError` (`test_store.py`, `test_project_methods.py`).
- Project removal is an archive operation (`ArchiveService.archive_project`; workflow and compensation in `projects/configuration.md` -> Removal Coordination and `archive.md`). Live automations targeting an Agent of the Project refuse it (`project_in_use`, one message naming all of them; `Runtime.automation_references.project_references`, `automation.md`), and it runs under `ChatRunManager.project_admission_guard`, which covers Project-scoped Sessions and Identity Sessions whose Working Project is the target. Identity Sessions working in the Project are not archived: they stay readable, their Runs are refused with `WorkingProjectMissingError` until a matching restore brings the original Project back (reuse of its id does not qualify; under a new id, proven archive-time associations follow it; `sessions/ownership.md` -> Archive scope membership and provenance), and they never fall back to the Workspace (GLOSSARY -> Working Project).
- Team membership and the winning definition may be cached, but resolving a member rereads that definition and its inherited repository settings so configuration edits take effect without rebuilding the Team. New competitors use normal Team rescan/invalidation (`projects/resolution.md`). Do not turn the membership cache into a configuration cache.
- Address strings use the canonical `agent@project` parsing and formatting in `core/projects/address.py`; do not split or assemble them ad hoc.

## References

Read these only when your task matches - not by default.

- Changing `project.json`, Project CRUD/RPC mutations, overrides, paths, anchor seeding, or archive/removal behavior -> `projects/configuration.md`
- Changing repository scanning, Source adapters, detection, Agent collisions, or scan findings -> `projects/scanning.md`
- Changing Project Agent resolution, model/scalar fallback, effective-config provenance, capability ceilings, working-Project helpers, or which instruction files Project Context loads -> `projects/resolution.md`
