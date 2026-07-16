# Projects

The Projects domain turns a repository location into a persistent vBot execution boundary with its own Agent discovery, defaults, Tool and Skill ceilings, overrides, and per-Agent Session storage.

## Overview

`core/projects/` owns the Project entity, its data-dir anchor, repository-format scanning, and the resolution of a Project Agent into effective runtime configuration. A Project points at a repository through `cwd`; vBot reads supported configuration from that repository but never writes Project metadata or Session state into it. The domain does not own central setting validation, Chat or Run lifecycle, the Session store, or the Tool and Skill implementations whose availability it constrains.

## Terms

Core terms such as Project, Agent, Session, Tool, Skill, and Provider live in `.vorch/GLOSSARY.md`.

### Project Anchor

**Definition:** The vBot-owned directory `<data-dir>/projects/<project-id>/` containing `project.json`, the seeded `AGENTS.md`, and per-Agent Session directories. It is the durable identity and storage root of a Project.

**Not:** The repository at the Project's `cwd`; changing `cwd` does not move or replace the Project Anchor.

### Project Agent

**Definition:** An Agent discovered from the Project repository and resolved at runtime from repository configuration plus Project defaults, per-Agent overrides, and global defaults.

**Not:** An Identity Agent stored independently in the global Agent store.

### Ceiling

**Definition:** A Project-level upper bound on capabilities. Resolution can narrow a ceiling for an individual Project Agent but cannot widen it.

### Project Tool Whitelist

**Definition:** The Project-owned `allowed_tools` set that defines the maximum Tools available to every Project Agent; repository-level denials may only remove Tools from it.

### Project Skill Whitelist

**Definition:** The Project-owned selection of bundled and global Skills combined with discovered Project Skills; a Project Skill explicitly disabled by name remains unavailable even when a bundled or global Skill has the same name.

## Boundary & Invariants

- `project_id` is stable and names the Project Anchor; `cwd` is a mutable pointer to the repository. Repository equality and working-directory equality never establish Project identity.
- Project-owned state lives under the data directory. The repository is a read-only configuration source: scanners may read its Agent and Skill files, but removal archives only the Project Anchor and never deletes or modifies repository content.
- A Project selects exactly one supported `source_format` (`opencode` or `claude`) for Agent and Project Skill discovery. Format detection may report multiple candidates but does not make a Project multi-format.
- `project.json` owns Project defaults, capability ceilings, Skill selections, and per-Agent overrides. Runtime resolution combines those values with freshly read repository Agent configuration and global defaults; it does not copy repository Agent files into the Project Anchor.
- Project Agent Session paths are anchored at `<data-dir>/projects/<project-id>/agents/<agent-id>/sessions/`. Agent and Project identifiers crossing filesystem boundaries must pass the shared path-segment validation.
- Tool access is the Project Tool Whitelist minus repository Agent denials. Skill access follows `(project skills ∪ enabled bundled skills ∪ enabled global skills) − disabled project skills − {"*"}`; neither repository configuration nor an Agent override may exceed these Project ceilings.
- Models, temperature, thinking effort, and compaction policy use the same canonical validators as global settings. Do not create Project-local validation rules or bypass the shared usable-model gate.

## Ownership Routing

- Change persisted Project fields, anchor layout, CRUD behavior, overrides, path normalization, or removal/archive behavior in `core/projects/projects.py`, `core/projects/store.py`, `core/projects/paths.py`, and the Project RPC boundary. Read `projects/configuration.md` first.
- Change repository Agent discovery, supported source formats, format detection, collision handling, or scan findings in `core/projects/scanners/` and `core/projects/scan_report.py`; the Project entity only validates the selected `source_format`. Read `projects/scanning.md` first.
- Change how a Project Agent becomes effective runtime configuration, including model fallback, provenance, Tool/Skill computation, or working-Project helpers, in `core/projects/resolver.py`. Read `projects/resolution.md` first.
- Change central model availability or scalar-setting validation in Models, Providers, or Settings, not here. Projects consumes those contracts.
- Change Session persistence, Run lifecycle, Chat behavior, or Tool/Skill implementation in their owning domains. Projects supplies identity, storage anchors, and capability/configuration inputs only.

## Constraints & Gotchas

- `normalize_cwd()` resolves an absolute real path, removes trailing separators, and preserves case. `cwd_identity_key()` additionally case-folds only on Windows; use it for duplicate detection instead of comparing display paths.
- `ProjectStore.create()` can persist a non-existent `cwd`, while the public `project.add` RPC requires an existing directory. Preserve this separation between storage and product-boundary validation.
- Project removal is an archive operation guarded by Cron references and an atomic `ChatRunManager.project_admission_guard` covering both Project-anchored Sessions and Identity-Agent work whose internal working Project is the target. The server holds that guard while rooted Identity Agents are reset and the Project Anchor is archived, alongside the Agent-reference lock and rollback behavior. Keep this coordination at the RPC/service boundary; a raw store delete is not the complete removal workflow.
- Team membership may be cached, but resolving a member rereads its repository Agent source so configuration edits take effect without rebuilding the Team. Do not turn the membership cache into a configuration cache.
- Address strings use the canonical `agent@project` parsing and formatting in `core/projects/address.py`; do not split or assemble them ad hoc.

## References

Read these only when your task matches — not by default.

- Changing `project.json`, Project CRUD/RPC mutations, overrides, paths, anchor seeding, or archive/removal behavior → `projects/configuration.md`
- Changing repository scanning, source-format mappings, format detection, Agent collisions, or scan findings → `projects/scanning.md`
- Changing Project Agent resolution, model/scalar fallback, effective-config provenance, capability ceilings, or working-Project helpers → `projects/resolution.md`
