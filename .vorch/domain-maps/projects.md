# Projects

The Projects domain turns a repository location into a persistent vBot execution boundary with its own Agent discovery, defaults, Tool and Skill ceilings, overrides, and per-Agent Session storage.

## Overview

`core/projects/` owns the Project entity, its persisted `project.json` schema and load gate, its data-dir anchor, repository-format scanning, and the resolution of a Project Agent into effective runtime configuration. A Project points at a repository through `cwd`; vBot reads supported configuration from that repository but never writes Project metadata or Session state into it. The domain consumes shared scalar rules from Settings but does not own Chat or Run lifecycle, the Session store, or the Tool and Skill implementations whose availability it constrains.

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

**Definition:** The Project-owned `allowed_tools` set that defines the maximum directly configurable Tools available to every Project Agent. Repository denials narrow the default policy, while an explicit vBot per-Agent Tool override may replace those denials but can never exceed this ceiling; automatic companions may follow an in-ceiling Tool.

### Project Skill Whitelist

**Definition:** The Project-owned selection of bundled and global Skills combined with discovered Project Skills; a Project Skill explicitly disabled by name remains unavailable even when a bundled or global Skill has the same name.

### Source Format

**Definition:** The Project's single declared coding-agent ecosystem (`project.json` -> `source_format`: `opencode` = `.opencode/agents|skills/`, `claude` = `.claude/agents|skills/`) that decides where **both** its Team agents (GLOSSARY -> Team) and its project skills come from. Exactly one per Project - no mixing (same-named agents/skills across ecosystems are usually the same tool tuned per harness, so merging would silently discard one copy); every consumer sees only this format's set. Auto-detected at creation (exactly one present wins; both/neither -> `opencode`), changeable later in project settings - Sessions survive, team and skills re-derive from the repo. Context files stay format-independent: `AGENTS.md` is the seeded auto-load convention for every Project; `CLAUDE.md` is never auto-loaded and its `@import` semantics are never emulated.

**Not:** The per-member `source_format` provenance tag on a scanned team member (`ScannedAgent.source_format`). Same value set, member-level fact recorded by the scan - while the Source Format is the **project-level** choice deciding which detector runs at all.

### Project Context

**Definition:** The current instructions, absolute Project path, and available Project Skills that tell an Agent how to work in a registered Project. Supplied automatically when that Project is the Agent's working Project, or loaded explicitly by an Identity Agent through the `project` Tool (`tools/project.md`).

**Not:** An Agent type, Project membership, Rooting, or a current-working-directory change; an explicit load changes none of those, and every one-shot `bash` call must set `workdir` again.

## Boundary & Invariants

- `project_id` is stable and names the Project Anchor; `cwd` is a mutable pointer to the repository. Repository equality and working-directory equality never establish Project identity.
- Project-owned state lives under the data directory. The repository is a read-only configuration source: scanners may read its Agent and Skill files, but removal archives only the Project Anchor and never deletes or modifies repository content.
- A Project selects exactly one supported `source_format` (`opencode` or `claude`) for Agent and Project Skill discovery. Format detection may report multiple candidates but does not make a Project multi-format.
- `project.json` owns Project defaults, capability ceilings, Skill selections, and per-Agent overrides. Runtime resolution combines those values with freshly read repository Agent configuration and global defaults; it does not copy repository Agent files into the Project Anchor.
- Project Agent Session paths are anchored at `<data-dir>/projects/<project-id>/agents/<agent-id>/sessions/`. Agent and Project identifiers crossing filesystem boundaries must pass the shared path-segment validation.
- Without a vBot Tool override, Tool access starts from the Project Tool Whitelist and repository Agent denials remove names. A present `overrides.<agent_id>.tool_access` completely replaces that repository Tool policy and may intentionally re-enable a repo-denied Tool, while still remaining inside the Project Tool Whitelist; mode `none` can remove everything and `selected` can narrow the Project Agent to one Tool. Skill access follows `(project skills + enabled bundled skills + enabled global skills) - disabled project skills - {"*"}`; neither repository configuration nor an Agent override may exceed Project ceilings.
- Models, temperature, thinking effort, and compaction policy use the same canonical validators as global settings. Do not create Project-local validation rules or bypass the shared usable-model gate.

## Ownership Routing

- Change persisted Project fields, anchor layout, CRUD behavior, overrides, path normalization, or removal/archive behavior in `core/projects/projects.py`, `core/projects/store.py`, `core/projects/paths.py`, and the Project RPC boundary. Read `projects/configuration.md` first.
- Change repository Agent discovery, supported source formats, format detection, collision handling, or scan findings in `core/projects/scanners/` and `core/projects/scan_report.py`; the Project entity only validates the selected `source_format`. Read `projects/scanning.md` first.
- Change how a Project Agent becomes effective runtime configuration, including model fallback, provenance, Tool/Skill computation, or working-Project helpers, in `core/projects/resolver.py`. Read `projects/resolution.md` first.
- Change explicit foreign Project Context loading for Identity Agents in `core/tools/project.py`; Projects owns the registered records and repository pointers it reads but does not infer context from arbitrary filesystem paths or own the Tool result (see `tools/project.md`).
- Change central model availability or scalar-setting validation in Models, Providers, or Settings, not here. Projects consumes those contracts.
- Change Session persistence, Run lifecycle, Chat behavior, or Tool/Skill implementation in their owning domains. Projects supplies identity, storage anchors, and capability/configuration inputs only.

## Constraints & Gotchas

- `normalize_cwd()` resolves an absolute real path, removes trailing separators, and preserves case. `cwd_identity_key()` additionally case-folds only on Windows; use it for duplicate detection instead of comparing display paths.
- `ProjectStore.create()` can persist a non-existent `cwd`, while the public `project.add` RPC requires an existing directory. Preserve this separation between storage and product-boundary validation.
- Project removal is an archive operation guarded by Cron references and an atomic `ChatRunManager.project_admission_guard` covering both Project-anchored Sessions and Identity-Agent work whose internal working Project is the target. The server holds that guard while Project-owned Terminal Sessions are terminated, rooted Identity Agents are reset, and the Project Anchor is archived, alongside the Agent-reference lock and rollback behavior. Keep this coordination at the RPC/service boundary; a raw store delete is not the complete removal workflow.
- Team membership may be cached, but resolving a member rereads its repository Agent source so configuration edits take effect without rebuilding the Team. Do not turn the membership cache into a configuration cache.
- Address strings use the canonical `agent@project` parsing and formatting in `core/projects/address.py`; do not split or assemble them ad hoc.

## References

Read these only when your task matches - not by default.

- Changing `project.json`, Project CRUD/RPC mutations, overrides, paths, anchor seeding, or archive/removal behavior -> `projects/configuration.md`
- Changing repository scanning, source-format mappings, format detection, Agent collisions, or scan findings -> `projects/scanning.md`
- Changing Project Agent resolution, model/scalar fallback, effective-config provenance, capability ceilings, or working-Project helpers -> `projects/resolution.md`
