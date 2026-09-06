# Skills

Local skill metadata loading, validation diagnostics, and prompt allowlist filtering.

## Overview

`core/skills/` scans bundled skills under `resources/skills/`, user (global) skills under `<data_dir>/skills/`, per-agent private skills under `<data_dir>/agents/<id>/skills/`, project skills under the project's declared source format's directory (`projects.md` -> Source Format), configured extra directories, and the `skills/` folder of every **loaded extension**. A directory counts as a skill only when it contains `SKILL.md`; the authored package shape is that file plus optional regular files under `scripts/`, `references/`, and `assets/`. The domain is **read and write**: `core/skills/authoring.py` is the one validated write core shared by every surface that creates or edits a skill (see Authoring & Write Scope).

**Skill Policy (`core/skills/policy.py`).** One validated, versioned control-plane file at `<data_dir>/skills/policy.json`: `{"version": 2, "disabled": ["<name>"], "shared": {"<owner-agent-id>": {"<skill-name>": ["<receiver-agent-id>"]}}}`. Missing file means empty policy; a malformed file yields diagnostics plus an empty effective policy instead of breaking startup. Mutations run under one process-local lock with one atomic replace per write. Trigger-unsafe names produce warnings and drop from effective sets; unknown owner ids and names matching no scanned skill are kept in the file but ignored at resolution (surfaced as stale entries). No legacy compatibility or auto-migration. Disabled/Shared semantics live in Terms below.

**Extension-bundled skills.** An extension may ship skills under `<extension>/skills/` - folders only, no code. Every **loaded** extension's folder joins the global scan roots, so extension skills present as **global** skills without inherent allowlist bypass (an Identity Agent may allow one personally, or a Project opts in via `skills_global_enabled`). They are scanned **after** the user's own global directory, so a hand-authored skill wins name collisions. Only loaded extensions contribute; disabled/failed/overridden add nothing, and the pool refreshes live on extension enable/disable/reload. They are **read-only**: editor writes target only `global`/`agent:<id>` scopes, so customizing one means copying it into `<data_dir>/skills`.

**Project- and agent-scoped pool.** `runtime.skills_for(project_id, identity_agent_id=None)` merges the layers with first-found-wins precedence **own > project > shared > global > bundled**: private Skills are always allowed for their owner, the effective Project set is always allowed inside active Project Context, shared Skills insert between Project and global layers (each owner's individually resolved packages, never a whole home, in sorted order), while the Agent's `allowed_skills` continues to govern everything else. Config Agents receive the Project-derived set through their resolved allowlist. Live Runs, compaction, preview, activation, and autocomplete share this policy. Shared Skills carry the receiver-facing Agent origin, never enter the receiver's always-allowed set, and pass through `filter_allowed` like globals.

Skills are playbooks, not normal user-managed tools: a bundled Skill may package a script-backed capability without registering Tools - its required binary gates visibility, activation supplies script path plus operating contract, and the Agent invokes it through `bash`. The ordinary `skill` Tool has three flat modes against the live scoped registry: no arguments lists available Skills by origin; `name` activates `SKILL.md`; `name` + `file_path` returns one UTF-8 package file without activating or exposing absolute paths. A named miss rescans disk once so hand-dropped Skills work without restart. User messages activate deterministically via `/skill-name` at message start or `$skill-name` anywhere before the request.

## Terms

Domain-specific vocabulary for skills. The core Skill term lives in `.vorch/GLOSSARY.md`.

### Per-Agent Skill
**Definition:** A Skill living in one agent's private home `<data_dir>/agents/<id>/skills/` (archived with the agent like its Workspace). Visible/loadable only to that agent, highest precedence, and **always-allowed for its owner**, bypassing `allowed_skills`. Active Project Context independently always-allows that Project's effective set - it never exposes another Agent's private home. Agents author their own via `skill_manage` (and `/learn`); users curate any agent's via WebUI/RPC (`agent:<id>` scope).
**Not:** A global, project/repo-owned, or bundled skill. Shared-pool Skills normally stay subject to the receiver's allowlist; the Project Context grant bypasses it only for that Project's effective names.

### Disabled Skill
**Definition:** A Skill whose name sits in the Skill Policy. Disable is name-based across all origins (a master switch: a bundled and a private same-named skill hide together). It vanishes from every runtime consumer - catalogs, triggers, Tool list/activation, resolver sets, availability answers - including the owner's own always-allowed copy; only editor-scope loads still show it. A trigger attempt yields the standard unknown/unavailable reminder.
**Not:** A Project-level disable (`skills_project_disabled`) or an Agent allowlist omission - the policy switch outranks both.

### Shared Skill
**Definition:** An Identity Agent's private Skill the Policy marks shared with specific receiver Identity Agent ids. Each receiver sees it as an extra pool layer (own > project > shared > global > bundled), subject to its own `allowed_skills` and disable, rendered indistinguishably among its own skills (same Agent origin tag, no provenance hint). The owner keeps it unchanged as always-allowed private Skill. Config/Team Agents never see shared Skills. Receivers may co-maintain through `skill_manage`'s edit/patch/write_file/remove_file; `create` stays own-home-only and `delete` owner/human-only. Stale entries (owner gone, package vanished) ignore at load with a warning and surface in the manager listing.
**Not:** A copy or move - edits land at the owner's home; not a new origin group; not an all-agents broadcast.

### Prompt-Epoch Catalog
**Definition:** The System Prompt Skill catalog **text** (`<available_skills>`) snapshotted on a Session's first build and reused until successful Compaction (persisted as `PinnedSkillCatalog`). Compaction rescans every source and replaces the snapshot, so the next epoch advertises current additions/removals/descriptions/availability while unchanged content stays byte-identical.
**Not:** A freeze on activation - the `skill` Tool and triggers resolve the live registry; only the advertised text pins between Compactions, and activated content is canonical conversation Context outside this snapshot.

### Skill Availability Announcement
**Definition:** A one-time tail `<system-reminder>` when a Skill becomes available+allowed during a prompt epoch and was not already shown (newly authored, opted into a Project, added globally, or freshly scanned). Run setup diffs current available+allowed against `seen_skills`; the first build and each successful Compaction seed that set silently.
**Not:** A removal notice, a replacement for the Prompt-Epoch Catalog, or the live `skill({})` result.

## Authoring & Write Scope

`core/skills/authoring.py` (`SkillAuthoringService`) is the single validated write core behind the Agent Tool, RPC, WebUI, CLI, and `/learn`. It operates on an already-resolved target root with direct create/edit/patch/delete/write_file/remove_file methods; each success publishes immediately. Create/edit/SKILL.md patches reuse the loader's lenient document parsing and canonicalize complete YAML frontmatter, stamping `author` plus optional `source` under `metadata.vbot` and writing atomically; a missing name fills from the trigger-safe directory name and an explicit different name is rejected. Support-file writes accept UTF-8 only, confined to `scripts/`/`references/`/`assets/`; patches default to `SKILL.md`, target one support file otherwise, require unique match unless `replace_all=true`. Patch matching normalizes line endings (CR/CRLF tolerated in `match`), non-unique matches report their line numbers, and edit/patch/write_file preserve the target file's detected line-ending style. Every path stays under the target root; protected bundled roots are refused. Failures raise `SkillAuthoringError` with surface-neutral diagnostics.

**Write-scope boundary (v1 is data-dir only - vBot never writes the repo as runtime state):**

- **Agent Tool `skill_manage`** (always registered, not gated on owning a Skill): one open flat object requiring `action` + `name`, optional siblings only `content`/`file_path`/`match`; actions are create/edit/patch/write_file/remove_file/delete, accepting only each action's applicable fields. Agent authoring is deliberately **private-only**: every mutation targets the calling Identity Agent's home, invalidates its registry, and emits one content-free control-plane event. The single exception is **shared-target resolution**: a name not the caller's own but resolving to a shared instance lets update actions (edit/patch/write_file/remove_file) operate on the owner's package and invalidate all agent-aware registries, while create/delete refuse non-own targets. The Result reports `scope: "own"` identically for both, and the Tool description intentionally never mentions sharing - receivers discover editability through use, not provenance wording. Global authoring remains human-surface-only (RPC/WebUI/CLI); Project/repo and bundled Skills are never Agent Tool targets. `skill_manage` carries the declarative `identity_agent` constraint: withheld from Config/Project Agents at visibility and dispatch, excluded from the Project Tool Whitelist by Projects-owned metadata.
- **`/learn`**: user-triggered internal Run seeded with an authoring brief, using `skill_manage` into the current agent's home (see `chat.md`).
- **RPC + user Accessors**: `skill.read/create/update/delete/write_file/remove_file` scoped `global` or `agent:<id>`, `author="human"`; Project/repo scopes reject. Global writes reload skills, Agent writes invalidate that agent's registry. The Agent-scope id validates traversal-safe and must name an **existing Identity Agent** - unknown ids reject with `invalid_request` rather than creating unowned homes.
- **Project skills** stay repo-owned (authored with ordinary `write`/`edit`, validated at scan); **bundled** `resources/skills/` is read-only - the write core refuses it.

## Data Model

- `SkillMetadata`: normalized `name`/`description`, internal `path`, optional license/compatibility/metadata/`allowed_tools`, parsed vBot requirements. `SkillRegistry.always_allowed` is scoped metadata: Identity registries contain private names plus the active Project's effective names.
- Three availability states: `invalid` (malformed/non-loadable), `unavailable` (loadable, unmet required requirements), `available`; optional requirements never gate availability.
- Frontmatter prefers complete YAML (PyYAML) and degrades in staged fallbacks - BOM strip, re-quoting colon-space scalars, then top-level `key: value` extraction; without usable frontmatter the entire body is the Skill body. Missing name fills from the directory, missing description from the first body text line. Every fallback is a loadable warning, never a gate; name mismatches, overlong names, and trigger-unsafe characters also warn only - such a Skill loads but cannot be triggered via `/`/`$` (only explicit `skill` Tool argument).
- vBot requirements live under `metadata.vbot.requirements` (never `compatibility`); details in the Requirements section below.
- Activation returns the SKILL.md body alone as `content`, plus `resource_files {guidance, files}` with `scripts/` paths absolute (direct `bash` execution) and `references/`/`assets/` relative (read via `skill(name, file_path)`), optional `environment_access` Bash guidance, and one internal `<skill_content name="...">` context used for deterministic triggers, epoch dedup, and Compaction recognition - the wrapper never appears in Tool `content`. A `{baseDir}` marker substitutes the absolute Skill directory. A `file_path` call (including relative `scripts/...` source reads) returns `{name, status: "file_loaded", file_path, content}` with no absolute path and skips activation dedup. User-triggered activation persists an internal note; Tool activation uses its Result as durable carrier. Identical activation context deduplicates within one Compaction epoch; a changed package may re-activate before the boundary, and past a committed checkpoint a fresh carrier loads.

## Prompt Catalog

Prompt-facing metadata is XML in the agentskills.io-compatible shape:

```xml
<available_skills>
  <skill_group label="Bundled skills">
    <skill><name>teach</name><description>Teach a topic.</description></skill>
  </skill_group>
</available_skills>
```

- Skills group by **origin** (order: Bundled / Your global skills / Skills from project '<name>' / Your own skills; origin tags land at load from each scan root, project tags carry the display name). Originless registries render one untitled group.
- Each `skill` element holds only `name` and `description` - the catalog stays **path-free** as a prompt-economy presentation preference, not a hard routing rule. The explicit `project` Tool returns the loaded Project's Skills in its ordinary Result; afterwards the `skill` Tool resolves against that Project-aware registry while the pinned epoch catalog stays unchanged (see `tools/project.md`).
- Values XML-escape into the block. The bundled prompt explains that `/skill-name` and `$skill-name` are activation hints once matching `<skill_content>` is injected, that listed script paths are absolute (directly executable via `bash`), and reference/asset paths relative (readable via `skill(name, file_path)`).
- The catalog includes only available+allowed Skills filtered against the same working-Project/private registry as live activation; `chat.commands` and `prompt.preview` resolve a bare Identity Agent's `root_project_id` (qualified Config Agents get no private layer), failing closed while preserving the saved reference.

## Interfaces

- `core.skills` exports the registry/policy/authoring surfaces (`SkillRegistry`, `SkillPolicyService`, `SkillAuthoringService`), the origin vocabulary, scan helpers, and dataclasses - signatures live in code. `core/skills/runtime.py::SkillRuntime` owns global scan-layer construction, Project/Agent/shared resolution, manager inventory, and scoped registry caches; `Runtime` exposes stable delegates and performs only Tool/Prompt composition after a registry replacement.
- Non-obvious `SkillRegistry.load` behaviors: a scan root whose own path contains `SKILL.md` is a **package root** contributing exactly that package (how shared layers insert without scanning an owner's home); missing roots contribute nothing and one root's enumeration failure degrades to diagnostics without blocking startup; `environment` snapshots requirement-check inputs (defaults `os.environ`); `excluded_names` moves disabled names into a separate bucket readable via `excluded_skills()` - runtime passes them, editor loads omit them.
- `Runtime.refresh_skills_for(project_id, identity_agent_id=None)` is the explicit Compaction refresh seam: full pool reload plus cache invalidation returning the fresh scope; normal consumers use cached `skills_for(...)` and must not imitate rescans.
- Manager surface: `runtime.skill_policy` persists the policy file; `runtime.skill_inventory()` makes one exclusion-free pass over every source annotating origin/owner/share-disable state/receivers/availability/warnings plus stale entries; `agent_owns_private_skill(agent_id, name)`. RPC `skill.set_disabled` validates existence then reloads; `skill.share` requires an existing owning agent, at least one existing receiver id, none equal to the owner. Both publish `resource_changed(kind="skills")` - disable via `reload_skills()`, share via `invalidate_agent_skills(None)` (shared Skills never enter the global pool). Inventory ids identify exact source packages independently of names; `runtime.inspect_skill(id)` rescans known sources and reads that package's original text without activation or arbitrary client filesystem addressing. Inventory projects the human-editable scope only for loadable packages in the real global/private write roots; external and bundled sources stay read-only even when tagged global. Inventory and inspection RPC reads use a bounded worker pool so large collections do not block streaming. Tests: `tests/core/runtime/test_runtime_shared_skills.py` and `tests/server/rpc/test_skill_manager_methods.py`.

## Conventions

- `allowed_skills=['*']` or missing/`None` exposes all loaded skills - real behavior, not test convenience; `[]` exposes none. Explicit lists match exact names; unknown entries ignore (Skills are not hard execution gates).
- Skill dependency requirements must not bypass allowlists: a disallowed dependency makes the dependent unavailable.
- Duplicate names resolve first-found-wins with rejected-duplicate diagnostics; in merged registries the project dir scans first, so a project Skill wins over a bundled same-named one (the editor drops the shadowed bundled opt-in).
- `skill` and `skill_manage` are ordinary configurable Tools under Tool Access Policy, neither gated on already owning a Skill; `skill` seeds default-on in the Project Tool Whitelist.
- Full instructions have a single provider-visible carrier per activation (Tool Result or trigger note). `register_skill_activation` deduplicates identical content only until the next committed checkpoint; that checkpoint removes carriers from Context, revokes Skill-derived env grants, retains a names-only reminder for the finished epoch, and post-boundary reloads create fresh carriers (see `chat/request-building.md`).
- `/skill-name` and `$skill-name` preserve the original user message; unknown/non-loadable/unavailable triggers become internal reminders, not activations.
- `$` autocomplete lists only currently available Skills (never Commands); `/` autocomplete may list both because backend dispatch handles Commands before the Skill-trigger path - a same-named Skill stays reachable via `$name`.

## vBot Requirements Metadata

```yaml
metadata:
  vbot:
    requirements:
      all:
        - binary: git
        - any:
            - binary: gcc
            - binary: clang
        - any:
            - env: OPENAI_API_KEY
            - env: ANTHROPIC_API_KEY
      optional:
        - binary: jq
```

- `all` requires every child; `any` at least one; `optional` failures report without changing `available`.
- `env` checks a non-empty value in the snapshot environment (process env wins over `.env` fallback). Every declared `env` leaf doubles as an activation-time Bash grant surfaced via `environment_access` (Tool activations as sibling field, internal contexts wrapped before instructions), listing names and per-call `bash.env_keys` - permanent Agent grants are the separate `tools.bash.allowed_env` source (see `tools/bash.md`).
- `binary` resolves via `shutil.which` against the snapshot PATH - lookup, never shell execution. `skill` checks dependency loadable+available+allowed, walking chains with a cycle guard (circular -> `unavailable` with a cycle reason). Malformed requirements make the Skill invalid/non-loadable.

## External Dependencies

- `pyyaml` (core dependency) parses SKILL.md frontmatter. Browser Use and Computer Use execute through opt-in bundled Extensions (see `extensions/browser-use.md` and `extensions/computer-use.md`). Browser Use also bundles a workflow Skill; the Skill supplies instructions, while the Extension's Tool grant controls execution.

## Constraints & Gotchas

- Requirement checks run against the environment snapshot captured at registry load/reload, not live `os.environ` at activation - a newly exported key or installed binary flips availability only after reload.
- Bundled scripts execute on the machine running the server through that Run's `bash`. Browser connection modes and directly delivered screenshots belong to the Browser Use Extension; see `extensions/browser-use.md`.
- Path economy (catalog and `skill({})` results path-free; provenance internal) is presentation preference, not routing: activations expose absolute script paths and relative readable paths under `resource_files`, and `project` Tool Results may carry structured path context - but `skill(name, file_path)` accepts only relative package paths and returns only those.
- Retain non-loadable directories as diagnostics (I/O failures, malformed requirements, duplicate names) so UI can explain them; plain YAML errors and missing ordinary metadata use loadable fallbacks instead.
- Metadata diagnostics log at `DEBUG` (`vbot.skills`) **once per process**, keyed by (path, warning), because registries reload on every project run/reload - only the log deduplicates; caller-facing diagnostics always carry every warning.
- No in-app legacy compatibility for older `allowed_skills` formats - explicit converter scripts if ever needed.
