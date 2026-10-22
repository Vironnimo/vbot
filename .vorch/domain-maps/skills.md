# Skills

Skill package scanning, metadata loading, validation diagnostics, scoped registries and allowlist filtering, the Skill Policy, and every write to a Skill package with its history.

## Overview

`core/skills/` scans bundled skills under `resources/skills/`, user (global) skills under `<data_dir>/skills/`, per-agent private skills under `<data_dir>/agents/<id>/skills/`, Project Skills in the ordered active Skill Source roots (`projects.md` -> Source), configured `skill_directories`, and the `skills/` folder of every **loaded extension**. A directory counts as a skill only when it contains `SKILL.md`; packages may hold arbitrary regular support files, and `scripts/`, `references/`, `assets/` are conventions, not the read boundary. The domain is **read and write** (Authoring & Write Scope).

Not owned here: the `skill` Tool's modes, activation payload, paging and path rules (`tools/skill.md`); trigger activation, catalog pinning, availability announcements and activation dedup in requests (`chat/request-building.md` -> Skills and prompt-cache stability); the `core:skills` block (`prompts.md`); Librarian and Reflection Runs (`automation.md`); a Project's effective Skill set (`projects/resolution.md` -> Effective Capabilities).

**Skill Policy.** `<data_dir>/skills/policy.json` (`core/skills/policy.py`) holds the turned-off packages (`disabled_packages`, entries are `SkillPackageRef(source, name, location)`) and the shares. A malformed file yields diagnostics and an empty effective policy instead of breaking startup, while mutations refuse it (`skills/policy.md`).

**Extension Skills.** Each loaded Extension's `skills/` folder (folders only, no code) joins the global scan roots, so its Skills are read-only **global** Skills without allowlist bypass (allowed per Agent, or by a Project's `skills_global_enabled`); the pool refreshes on Extension enable/disable/reload.

**Scoped pool.** `runtime.skills_for(project_id, identity_agent_id=None)` merges the layers with first-found-wins precedence **own > project > shared > global > bundled**: private Skills are always allowed for their owner unless its Agent Skill Exclusions name them, the effective Project set is always allowed inside active Project Context, shared Skills insert between Project and global layers (each owner's resolved packages, never a whole home), and the Agent's `allowed_skills` minus its exclusions governs everything else. Project Agents receive the Project-derived set through their resolved allowlist. Live Runs, compaction, preview, activation and autocomplete share this policy. Shared Skills never enter the receiver's always-allowed set. Scans and caches: `skills/loading.md`.

Skills are playbooks, not Tools: a script-backed bundled Skill registers no Tool; its required binary gates visibility, activation supplies the script path and contract, and the Agent runs it through `bash`.

## Terms

The core Skill term lives in `.vorch/GLOSSARY.md`.

### Per-Agent Skill
A Skill in one Identity Agent's private Skill home, visible only to that Agent, with the highest precedence and always allowed for it unless excluded (Overview, Authoring & Write Scope). Not a global, Project or bundled Skill; Project Context never exposes another Agent's private home.

### Disabled Skill
One Skill package the user turned off, for every Agent and Project, so the next same-named package takes its place (`skills/policy.md` -> Disables, exclusions and shares). Not an allowlist omission or Agent Skill Exclusion: the switch outranks both.

### Agent Skill Exclusion
A Skill name an Identity Agent's configuration withholds from that Agent, including its own private Skill of that name, while the Skill stays loaded for others (`skills/policy.md` -> Disables, exclusions and shares). Not a Disabled Skill, which removes one package for everyone.

### Shared Skill
An Identity Agent's private Skill the Skill Policy shares with named receiver Identity Agents, which see it like their own and may update it (`skills/policy.md` -> Disables, exclusions and shares). Not a copy or move, since edits land in the owner's home, and not a broadcast to all Agents.

### Skill History
The append-only revision log beside each writable Skill home: who changed which files of which Skill, outside changes, and each Skill's origin and pin, from which people revert revisions (`skills/history.md`). Not version control or a backup, and not the Memory history (`memory/history.md`).

### Skill Archive
The folder beside a writable Skill home that a deleted package moves into, until a person restores or purges it (`skills/history.md` -> Archive, restore and purge). Not an Archive Entry (`archive.md`): no retention or `archive.*` RPC applies, though an archived Identity Agent's Entry carries its Skill Archive.

### Pinned Skill
A Skill a person pinned to keep Background Writers off it; only a person pins or unpins (`skills/history.md`). Not a write lock, since people and attended Agents still change it, and not the Prompt-Epoch Catalog.

### Background Writer
A Skill write by a background actor, a Reflection or the Librarian, which vBot restricts more than an attended Agent's write (`skills/skill-manage.md` -> Calls, `skills/history.md` -> Write rules). Not an attended Agent, which writes under the ordinary write scope.

### Prompt-Epoch Catalog
The Skill catalog text of the System Prompt, kept fixed for a prompt epoch and rebuilt after a successful Compaction (`chat/request-building.md` -> Skills and prompt-cache stability). Not a freeze on activation: the `skill` Tool and triggers resolve the live registry.

### Skill Availability Announcement
A one-time System Reminder that a Skill became available and allowed during a prompt epoch without having been shown to the Session before (`chat/request-building.md` -> Skills and prompt-cache stability). Not a removal notice or a replacement for the Prompt-Epoch Catalog.

## Authoring & Write Scope

`SkillAuthoringService` (`core/skills/authoring.py`) is the single validated write core behind the Agent Tool, RPC, WebUI, CLI, installation and `/learn`; no surface writes packages itself. Every mutation names its writer (`SkillWriter`; people pass `HUMAN_WRITER`), enforces the Background Writer rules first and records one Skill History revision after; a history failure only logs. Mechanics and `skill.*` write RPCs: `skills/authoring.md`.

Write scope (v1 is data-dir only - vBot never writes the repo as runtime state):

- **`skill_manage`** writes the calling Identity Agent's private home (in a Librarian Session, that of the Agent it maintains); update actions also reach a shared Skill in its owner's package and, for attended Agents, a global-home package; `publish` moves an own Skill into the global home (`skills/skill-manage.md`).
- **RPC and user Accessors** write `global` and `agent:<id>` (an existing Identity Agent) as `HUMAN_WRITER`. **Installation** uses the same roots and owner (`skills/installation.md`).
- **Project skills** stay Source-owned (ordinary `write`/`apply_patch`, validated at scan); folder and Extension Skills are read-only; bundled `resources/skills/` is refused.

`SkillRuntime.invalidate_agent_skills(owner_id)` owns the dependency from a private home to its shared receivers and invalidates their registries in every Project context, also when the owning Agent is archived or renamed; writers only name the owner. A global write reloads Skills and invalidates every Agent-aware registry.

## Data Model

- `SkillMetadata`: normalized `name`/`description`, internal `path`, optional license/compatibility/metadata/`allowed_tools`, parsed vBot requirements (`metadata.vbot.requirements`, `skills/loading.md`).
- Three availability states: `invalid` (malformed/non-loadable), `unavailable` (loadable, unmet required requirements), `available`; optional requirements never gate. Metadata problems are loadable warnings, never gates.
- The live registry stamps each Skill with its exact `SkillPackageRef` (source, package-directory name, location); activation persists it as internal `skill_package` metadata on the trigger Note or Tool Result, never in Provider or Compaction requests or public payloads. Statistics counts exact package use (`statistics/skill-usage.md`); Skill Policy selectors match visible names.

## Interfaces

- `core/skills/runtime.py::SkillRuntime` owns global scan layers, Project/Agent/shared resolution, manager inventory and scoped registry caches; `Runtime` exposes delegates and only recomposes Tools/Prompts after a registry replacement.
- `Runtime.refresh_skills_for(project_id, identity_agent_id=None)` is the explicit Compaction refresh seam (full reload plus invalidation); normal consumers use cached `skills_for(...)` and must not imitate rescans.
- `format_skill_catalog_entries` renders the one path-free catalog text of the `skill` Tool's list, the `core:skills` block and the new-Skill reminder: only available+allowed Skills of the registry live activation uses (`skills/loading.md` -> Catalog rendering).
- Every change that alters the Skill inventory publishes `resource_changed(kind="skills")` (publishers: `server/events-and-reconnect.md` -> `resource_changed`); Agent-side `skill_manage` writes reach the server through `SkillRuntime.add_changed_callback`/`notify_changed`, bridged in `server/_app_lifecycle.py`.

## Conventions

- `allowed_skills=['*']` or missing/`None` exposes all loaded Skills - real behavior, not test convenience; `[]` exposes none. Explicit lists match exact names; unknown entries ignore. No in-app compatibility for older `allowed_skills` formats.
- Skill dependencies never bypass allowlists: a disallowed dependency, including one withheld by an Agent Skill Exclusion, makes the dependent unavailable.
- Duplicate names resolve first-found-wins with rejected-duplicate diagnostics. Global scan order (`core/skills/runtime.py::_global_roots`, user decision): `<data_dir>/skills` > `skill_directories` (listed order) > loaded Extension `skills/` folders (load order) > bundled `resources/skills/`, so every global source outranks bundled (`test_global_skill_sources_follow_the_documented_precedence`). In merged registries the Project dir scans first, so a Project Skill wins over a same-named global or bundled one.
- `skill` and `skill_manage` are ordinary Tools under Tool Access Policy, neither gated on owning a Skill; `skill_manage` carries the `identity_agent` constraint.
- `/skill-name` and `$skill-name` preserve the user message; unknown, non-loadable or unavailable triggers become internal reminders, not activations. `$` autocomplete lists only available Skills; `/` may also list Commands, which dispatch first, so a same-named Skill stays reachable via `$name`.

## External Dependencies

- Browser automation is the bundled `playwright-cli` Skill through `bash`; its setup stays optional and missing binaries do not hide its setup guidance. Updating it or restoring the archived Browser Use Extension: `extensions/browser-use.md`.
- The bundled `skill-writing` Skill holds the authoring rules that `skill_maintenance.md`, the brief fragments and the `skill_manage` hints copy; change them together (`skills/skill-manage.md` -> skill-writing Skill).

## Constraints & Gotchas

- Requirement checks use an environment snapshot, not `os.environ` at activation. Credential reload updates it in every cached registry at once, keeping held Run references and pinned catalogs; explicit Skill reload also refreshes environment and binary availability.
- Every declared `env` requirement doubles as an activation-time Bash grant (`environment_access`, `tools/skill.md`), separate from the permanent `tools.bash.allowed_env` (`tools/shell.md`).
- Bundled scripts and the Playwright CLI run on the server host through that Run's `bash`, Python scripts on the host's `python`, not vBot's interpreter (PROJECT.md -> Development -> Python version). Outputs are server-side files the Agent reads or presents explicitly.

## Agent-facing text

This domain owns the Skill System Prompt blocks (`core:skills`, `core:skill_maintenance`), the `skill_manage` results and refusals, Repository Profile Skill preloads, the bundled `skill-writing` support-file guidance and the bundled `vbot-docs` instructions for Skill history, archive, pins and the Librarian. Before you change, add or delete a sentence of them, read `skills/agent-facing-text.md`: it records why each sentence exists. Update its row for every sentence you change and add one for every sentence you add.

## References

Read these only when your task matches - not by default.

- Changing `skill_manage` (targets, repair, patch matching, refusals, global writes, `publish`, merges, background guards, hints) or the bundled `skill-writing` Skill -> `skills/skill-manage.md`
- Changing `SkillAuthoringService` write mechanics or the `skill.*` write RPCs -> `skills/authoring.md`
- Changing `policy.json`, `skill.set_disabled`, `skill.share` or `vbot skill disable/enable` -> `skills/policy.md`
- Changing scans, registry caches, the catalog text, SKILL.md parsing, requirements metadata or metadata diagnostics -> `skills/loading.md`
- Changing package installation, source resolution, limits or replacement -> `skills/installation.md`
- Changing `skill.inventory` or `skill.inspect` payloads, manager availability, the Project scan preview's Skill pool, or a client that reads them -> `skills/manager.md`
- Changing the bundled `pdf` Skill (commands, environment, browser discovery, templates, text) -> `skills/pdf-skill.md`
- Changing how Skill writes are recorded, reverted, archived, restored, purged or pinned, their surfaces, or the `skill` Tool's answer for an archived name -> `skills/history.md`
