# Projects

The Projects domain turns a repository location into a persistent vBot execution boundary with its own Agent discovery, defaults, Tool and Skill ceilings, overrides, and per-Agent Session storage.

## Overview

`core/projects/` owns the Project entity, its persisted `project.json` schema and load gate, its data-dir anchor, Source discovery and translation, and the resolution of a Project Agent into effective runtime configuration. A Project points at a repository through `cwd`; vBot reads supported configuration from that repository but never writes Project metadata or Session state into it. The domain consumes shared scalar rules from Settings but does not own Chat or Run lifecycle, the Session store, or the Tool and Skill implementations whose availability it constrains.

## Terms

Core terms such as Project, Agent, Session, Tool, Skill, and Provider live in `.vorch/GLOSSARY.md`.

### Project Anchor

**Definition:** The vBot-owned directory `<data-dir>/projects/<project-id>/` containing `project.json`, the seeded `AGENTS.md`, and Project-local workspace anchors. It is the durable filesystem identity of a Project; Project-scoped Sessions are relationally addressed in the canonical Session database.

**Not:** The repository at the Project's `cwd`; changing `cwd` does not move or replace the Project Anchor.

### Project Agent

**Definition:** An Agent discovered from the Project repository and resolved at runtime from repository configuration plus Project defaults, per-Agent overrides, and global defaults.

**Not:** An Identity Agent stored independently in the global Agent store.

### Ceiling

**Definition:** A Project-level upper bound on capabilities. Resolution can narrow a ceiling for an individual Project Agent but cannot widen it.

### Project Tool Whitelist

**Definition:** The Project-owned `allowed_tools` set that defines the maximum directly configurable Tools available to every Project Agent. Repository denials narrow the default policy, while an explicit vBot per-Agent Tool override may replace those denials but can never exceed this ceiling; automatic companions may follow an in-ceiling Tool.

### Project Skill Whitelist

**Definition:** The Project-owned selection of bundled and global Skills combined with discovered Project Skills; a Project Skill explicitly disabled by name remains unavailable even when a bundled or global Skill has the same name.

### Source

An independently detected repository input for Agents, Skills or always-on instructions. A Project stores one ordered list of Sources with on/off switches; the earliest active definition of an Agent or Skill name wins. Sources from several ecosystems can contribute to one Team.

### Agent Profile

The format-neutral repository definition supplied by a Source adapter: Agent id, display name, delegation description, instructions, Model wish, Tool and permission wishes, Skills, provenance and a translation report. The Profile precedes Project defaults and overrides; it is not a stored Identity Agent or a runtime Agent kind.

### Project Context

**Definition:** The current instructions, absolute Project path, and available Project Skills that tell an Agent how to work in a registered Project. Supplied automatically when that Project is the Agent's working Project, or loaded explicitly by an Identity Agent through the `project` Tool (`tools/project.md`). Each auto-load file enters whole up to 128 KiB; a larger one appears only as a notice with its size, which names its absolute path and `read` only when the Agent can call `read` (`prompts.md` -> Prompt files).

**Not:** An Agent type, Project membership, a Working Project or default Project, or a current-working-directory change; an explicit load changes none of those, and every one-shot `bash` call must set `workdir` again.

## Boundary & Invariants

- `project_id` is stable and names the Project Anchor; `cwd` is a mutable pointer to the repository. Repository equality and working-directory equality never establish Project identity.
- Project-owned state lives under the data directory. The repository is a read-only configuration source: adapters may read its Agent and Skill files, but removal archives only the Project Anchor and never deletes or modifies repository content.
- A Project combines ordered, switchable Sources for Agents, Skills and instructions. Earlier active names win; shadowed definitions and unreadable inputs remain visible. New Sources activate on detection unless an active Agent/Skill name collides; an instruction Source activates only while no instruction file loads yet.
- `project.json` owns Project defaults, capability ceilings, Skill selections, and per-Agent overrides. Runtime resolution combines those values with freshly read repository Agent configuration and global defaults; it does not copy repository Agent files into the Project Anchor.
- Project Agent Sessions use `(project_id, agent_id, session_id)` addresses in `<data-dir>/sessions.db`; the Project Anchor no longer contains canonical Session files. Agent and Project identifiers still pass shared validation before entering either database addresses or filesystem anchors.
- Without a vBot Tool override, Tool access starts from the Project Tool Whitelist; Profile allowlists, rules and denials narrow it to the Agent's own Tools; followers and the Session's grants (`message_parent` for a delegated Run) still activate. A present `overrides.<agent_id>.tool_access` completely replaces that repository Tool policy and may intentionally re-enable a repo-denied Tool, while still remaining inside the Project Tool Whitelist; mode `none` can remove everything and `selected` can narrow the Project Agent to one Tool. Skill access follows `(project skills + enabled bundled skills + enabled global skills) - disabled project skills - {"*"}`; neither repository configuration nor an Agent override may exceed Project ceilings.
- Models, temperature, top_p, thinking effort, and compaction policy use the same canonical validators as global settings. Do not create Project-local validation rules or bypass the shared usable-model gate.
- Temporary Agents with an explicitly selected Project resolve through `resolver.py` without joining its Team. Like an Identity Session working in a Project they use the Project's directory, Skills and context, while their owner's snapshotted Tool selection and Skill allowlist apply as configured: the Project Tool Whitelist and Skill rule bound only the Team (user decision 2026-09-28: a Swarm profile decides what its participants may use). A directory alone does not select a Project. Regression coverage lives in `test_resolver_config_agent.py`, `tests/core/agents/test_temporary.py` and `tests/core/runtime/test_runtime_extension_host.py`.

## Ownership Routing

- Change persisted Project fields, anchor layout, CRUD behavior, overrides, path normalization, or the Anchor's archive and restore in `core/projects/projects.py`, `core/projects/store.py`, `core/projects/paths.py`, and the Project RPC boundary; the removal workflow itself lives in `core/archive/` (`archive.md`). Read `projects/configuration.md` first.
- Change repository Agent discovery, Source adapters, detection, translation, collisions, or findings in `core/projects/sources/` and `core/projects/scan_report.py`; the Project entity validates Sources and Model mappings. Read `projects/scanning.md` first.
- Change Project Agent orchestration and working-Project helpers in `core/projects/resolver.py`; `_runtime_agent.py` defines resolved contracts, `_model_configuration.py` owns the usable-Model gate, and `_resolution_values.py` owns capability ceilings, scalar fallback and provenance. These are internal parts of the same Projects owner. Read `projects/resolution.md` first.
- Change explicit foreign Project Context loading for Identity Agents in `core/tools/project.py`; Projects owns the registered records and repository pointers it reads but does not infer context from arbitrary filesystem paths or own the Tool result (see `tools/project.md`).
- Change central model availability or scalar-setting validation in Models, Providers, or Settings, not here. Projects consumes those contracts.
- Change Session persistence, Run lifecycle, Chat behavior, or Tool/Skill implementation in their owning domains. Projects supplies identity, storage anchors, and capability/configuration inputs only.

## Constraints & Gotchas

- `normalize_cwd()` resolves an absolute real path, removes trailing separators, and preserves case. `cwd_identity_key()` additionally case-folds only on Windows; use it for duplicate detection instead of comparing display paths.
- `ProjectStore.create()` can persist a non-existent `cwd`, while the public `project.add` RPC requires an existing directory. Preserve this separation between storage and product-boundary validation.
- `ProjectStore.create()` and `restore_files` under another id refuse an id Windows reserves for a device (`aux`, `nul`, `com1`, ...) on every platform (`InvalidProjectIdError`); existing Anchors keep theirs. `project.add` derives the id from the display name and refuses such a slug first as `invalid_request`, asking for another `display_name` (`test_store.py`, `test_project_methods.py`).
- Project ids are exact on every platform. Store lookups (`get` and everything built on it, `exists`, `archive_files`, `session_owning_agents`) require an Anchor entry with exactly the requested spelling (`_stored_project_dir`), so a case variant such as `VBOT` for `vbot` is `ProjectNotFoundError` (RPC `project_not_found`) even where the filesystem would open the stored Anchor, and Agent resolution through such an address reports the same code (`projects/resolution.md`); a `project.json` id that disagrees with its Anchor stays a plain `ProjectError` (`test_store.py`).
- Project removal is an archive operation guarded by references from live automations, as `Runtime.automation_references.project_references` reports: non-terminal Bootstrap jobs and Cron jobs that can still fire (`project_in_use`, one message naming all of them; `automation.md`) and an atomic `ChatRunManager.project_admission_guard` covering Project-scoped Sessions and Identity Sessions whose Working Project is the target. `ArchiveService.archive_project` holds that guard while Project-owned Terminal Sessions are terminated, Identity Agents with the Project as default Project are reset, the Project Anchor moves into a new archive entry's payload, and all live database Sessions in that Project become archived in it; a live Extension-owned Session refuses the removal until its owner, or core after the owner's removal, releases it (`projects/configuration.md`). `ProjectStore.archive_files` compensates the Anchor move if Session archiving fails; the archive service owns the wider coordination and undoes the default-Project resets (`archive.md`). Identity Sessions working in the Project are not archived: they stay readable, their Runs are refused with `WorkingProjectMissingError` until a restore brings the Project back (under a new id, `retarget_working_project` follows it), and they never fall back to the Workspace (`sessions.md` -> Terms -> Working Project). It runs the unrooting, the archive and its compensation on the Session database's pool inside the snapshot barrier, which `archive_files` and `restore_files` enter too (`database.md` -> Snapshot consistency).
- Team membership may be cached, but resolving a member rereads its repository Agent source so configuration edits take effect without rebuilding the Team. Do not turn the membership cache into a configuration cache.
- Address strings use the canonical `agent@project` parsing and formatting in `core/projects/address.py`; do not split or assemble them ad hoc.

## References

Read these only when your task matches - not by default.

- Changing `project.json`, Project CRUD/RPC mutations, overrides, paths, anchor seeding, or archive/removal behavior -> `projects/configuration.md`
- Changing repository scanning, Source adapters, detection, Agent collisions, or scan findings -> `projects/scanning.md`
- Changing Project Agent resolution, model/scalar fallback, effective-config provenance, capability ceilings, or working-Project helpers -> `projects/resolution.md`
