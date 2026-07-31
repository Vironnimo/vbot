# Skills

Local skill metadata loading, validation diagnostics, and prompt allowlist filtering.

## Overview

`core/skills/` scans bundled skills under `resources/skills/`, user (global) skills under `<data_dir>/skills/`, per-agent private skills under `<data_dir>/agents/<id>/skills/`, project skills under the project's declared source format's directory (`PROJECT_SKILLS_SUBPATHS`: `opencode → <cwd>/.opencode/skills/`, `claude → <cwd>/.claude/skills/` — GLOSSARY → Source Format), configured extra skill directories, and the `skills/` folder of every **loaded extension** (see Extension-bundled skills). A directory is considered a skill only when it contains `SKILL.md`; the authored vBot package shape is exactly that file plus optional regular files under `scripts/`, `references/`, and `assets/`. The package is **read and write**: alongside the registry/loader, `core/skills/authoring.py` is the one validated write core (see Authoring & Write Scope) shared by every surface that creates or edits a skill.

**Extension-bundled skills.** An extension in package/directory form may ship skills under `<extension>/skills/` (`<data_dir>/extensions/<name>/skills/` or a bundled `resources/extensions/<name>/skills/`), with no code — just the folder. The runtime folds every **loaded** extension's `skills/` dir into the global scan-root list (`Runtime._extension_skill_dirs`, appended by `_skill_scan_roots`), so extension skills present as **global** skills (origin `global`), subject to the normal agent allow-list and project opt-out — never always-allowed, so nothing bypasses the project skill whitelist. They are scanned **after** the user's own `<data_dir>/skills`, so a hand-authored global skill wins a name collision. Only `loaded` extensions contribute (disabled/failed/overridden add nothing); a single-file `.py` extension has no folder for a `skills/` child. The pool is refreshed live on every extension enable/disable/reload (`reload_extensions` and `apply_extension_disabled_change` both call `reload_skills`). They are **read-only** in the skill editor: `skill.read`/`skill.*` writes target only the `global`/`agent:<id>` scope directories (never an extension folder), so an extension skill never appears in the editor's editable list and no write can reach it — to customize one, copy it into `<data_dir>/skills` (your copy then shadows it by the precedence above).

**Project- and agent-scoped pool.** `runtime.skills_for(project_id, identity_agent_id=None)` merges selected Project, global/bundled, and optional Identity-private layers. A Rooted Identity Agent passes its internal `working_project_id` plus its own id, yielding agent > project > global > bundled while retaining its own allowlist; a Config Agent passes its Session Project plus no identity id, so Project whitelist ceilings still apply only there. Live Runs, compaction, preview, activation, and autocomplete share this policy.

Skills are playbooks, not normal user-managed tools. The registry exposes prompt metadata and internal activation metadata; actual activation is handled by the chat/tool pipeline. The ordinary `skill` Tool requires `name`: without `file_path` it activates that Skill's `SKILL.md`, and with `file_path` it returns one UTF-8 package file by relative path without activating it or exposing an absolute path in the file result. The separate zero-argument `skill_list` Tool returns available Skills grouped by origin but is Session-scoped and granted only to Reflection Runs; normal Runs rely on the Prompt-Epoch Catalog plus Skill Availability Announcements. A `skill` name miss rescans disk once so a hand-dropped Skill can activate or serve a file without a restart. User messages can also activate Skills deterministically through `/skill-name` at the start of the message or `$skill-name` anywhere before the Provider request is sent.

## Terms

Domain-specific vocabulary for skills. The core Skill term lives in `.vorch/GLOSSARY.md`.

### Per-Agent Skill
**Definition:** A Skill that lives in one agent's **private** home `<data_dir>/agents/<id>/skills/` (archived with the agent on delete, like its Workspace). It is visible and loadable **only** to that agent — layered on top of the project/global/bundled pool at the highest precedence (agent > project > global > bundled) — and is **always-allowed for its owner**, bypassing the agent's `allowed_skills` filter. An agent authors its own per-agent skills with the `skill_manage` tool (and via `/learn`); the user can curate any agent's per-agent skills via the WebUI/RPC (`agent:<id>` scope).
**Not:** A global skill (`<data_dir>/skills/`, shared across the user's identity agents, user-curated), a project/team skill (the project's Source Format skill directory, e.g. `<cwd>/.opencode/skills/` or `<cwd>/.claude/skills/`, repo-owned), or a bundled skill (`resources/skills/`, read-only). Those shared-pool skills stay subject to the agent's allow-list; only an agent's own private skills bypass it.

### Prompt-Epoch Catalog
**Definition:** The System Prompt Skill catalog **text** (`<available_skills>`) snapshotted on a Session's first build and reused until successful Compaction (stored in Session metadata as a `PinnedSkillCatalog`). Compaction rescans every Skill source and replaces the snapshot, so the next prompt epoch advertises current additions, removals, descriptions, and requirement availability while unchanged content remains byte-identical.
**Not:** A freeze on using Skills or on Skill activation. The `skill` Tool and `/`–`$` triggers resolve the live registry, while only the advertised catalog text is pinned between Compactions; activated Skill content is canonical conversation Context and is outside this System Prompt snapshot.

### Skill Availability Announcement
**Definition:** A one-time `<system-reminder>` note appended at the conversation tail when a Skill becomes available+allowed during a prompt epoch and had not already been shown — whether newly authored (`skill_manage`), opted into a Project (bundled/global), added to the global pool, or freshly scanned from a repo. Run setup diffs the current available+allowed Skills against Session metadata `seen_skills`; the first build and every successful Compaction seed that set from the freshly rendered catalog without announcing.
**Not:** A removal notice (additions only), a replacement for the Prompt-Epoch Catalog, or the Reflection-only `skill_list` Tool. Successful Compaction folds current Skills into the refreshed catalog; announcements cover additions between those boundaries.

## Authoring & Write Scope

`core/skills/authoring.py` (`SkillAuthoringService`) is the single validated write core shared by the Agent Tool, RPC, WebUI, CLI, and `/learn`. It operates on an already-resolved target root and exposes direct `create` / `edit` / `patch` / `delete` / `write_file` / `remove_file` methods; each successful call changes the published Skill immediately. `create`, `edit`, and `SKILL.md` patches reuse `validate_skill_metadata` + `parse_vbot_requirements`, require the front-matter name to equal the trigger-safe directory name, stamp `author` plus optional `source` under `metadata.vbot`, and write atomically. Support-file writes accept UTF-8 text only and remain confined to `scripts/`, `references/`, or `assets/`; patches default to `SKILL.md`, may target one support file, require a unique match unless `replace_all=true`, and write atomically. The service confines every path under the target root and refuses a target at/under a protected bundled root. Failures raise `SkillAuthoringError` with surface-neutral diagnostics.

**Write-scope boundary (v1 is data-dir only — vBot never writes the repo as runtime state):**
- **Agent Tool `skill_manage`** (`core/tools/skill_manage.py`, an ordinary allow-list Tool, always registered, not gated on already having a Skill): the public contract is one flat discriminated union whose closed branches each require `action` and `name`. Actions are `create`, `edit`, `patch`, `write_file`, `remove_file`, and `delete`; each branch exposes and structurally requires only its own applicable fields: `content` for complete `SKILL.md`, `file_content` for one complete UTF-8 support file, `file_path` for a relative package path, and `old_string` / `new_string` plus optional `replace_all` for patches. The optional `scope` is `own` (default, the calling Identity Agent's private home) or `global` (only when the user explicitly requested a Skill shared by all Agents). Every successful action invalidates that Agent's registry or reloads the global pool and emits one content-free control-plane event. Project/repo and bundled Skills are never targets. `skill_manage` remains identity-only through `IDENTITY_ONLY_TOOLS` at both dispatch and prompt visibility, is toggleable for Identity Agents, and is excluded from the Project Tool Whitelist.
- **`/learn`**: a user-triggered internal run seeded with an authoring brief that uses `skill_manage` to author into the current agent's home (see `chat.md`).
- **RPC + user Accessors** (`server/rpc/skill_methods.py`, WebUI, CLI): `skill.read` (scope's own Skills with content), `skill.create` / `skill.update` / `skill.delete` / `skill.write_file` / `skill.remove_file`, each scoped `global` (`<data_dir>/skills`) or `agent:<id>`, `author="human"`; a Project/repo scope is rejected; global writes `reload_skills`, Agent writes `invalidate_agent_skills`. The Agent-scope id is validated traversal-safe before any path is built, and must name an **existing Identity Agent** — an unknown id (e.g. a Project Team slug) is rejected with `invalid_request` instead of silently creating an unowned `agents/<id>/skills` home.
- **Project skills** (the chosen format's skill directory, e.g. `<cwd>/.opencode/skills/` or `<cwd>/.claude/skills/`) stay repo-owned: authored with the ordinary `write`/`edit` file tools, validated at scan/load. The skill write core/tool/RPCs never write the repo in v1.
- **Bundled** (`resources/skills/`) is read-only — the write core refuses it.

## Data Model

- `SkillMetadata`: `name`, `description`, internal `path`, optional `license`, `compatibility`, `metadata`, `allowed_tools`, and parsed vBot requirements from YAML frontmatter.
- `SkillDiagnostic`: `name`, `path`, `valid`, `warnings`, and `loadable` for both loadable skills with warnings and rejected skill directories.
- Skill availability has three runtime states: `invalid` for malformed/non-loadable skills, `unavailable` for loadable skills with unmet required vBot requirements, and `available` when required requirements are satisfied. Optional requirements never make a skill unavailable.
- YAML frontmatter is parsed with PyYAML. Validation is lenient: name/directory mismatch, names longer than 64 characters, and names using characters outside letters/digits/`-`/`_` (or not starting with a letter or digit) are warnings only — such a name still loads but can never be triggered with `/name` or `$name` (only via the `skill` tool's explicit `name` argument); missing required fields make the skill non-loadable. Invalid YAML is not always fatal — a repair pass re-quotes unquoted scalar values that contain a colon-space (`key: a: b`) and re-parses; if that succeeds the skill loads with a `MALFORMED_YAML_FALLBACK_WARNING`, otherwise it is non-loadable.
- vBot-specific machine-checkable requirements live under `metadata.vbot.requirements`, not `compatibility`. Supported required/optional primitives are `env`, `binary`, and `skill`, composed with nested `all` and `any` groups. Provider requirements are intentionally not supported; model/provider-specific prerequisites should be expressed as concrete env vars or skill instructions.
- Resource paths are not stored in `SkillMetadata`; the resource directories (`RESOURCE_DIRECTORIES`: `scripts/`, `references/`, `assets/`) are scanned at activation time.
- Bundled `resources/skills/` contains tiny loadable sample skills for normal activation flows. Warning and broken skill diagnostics are covered by tests with local fixtures rather than shipped as bundled resources.
- Activated Skill content is wrapped in `<skill_content name="...">`. The wrapper opens with the absolute Skill directory for execution-oriented Tools, lists `scripts/` files as absolute paths for direct `bash` execution, lists `references/` and `assets/` files as relative paths for `skill(name, file_path)` reads, then includes the Skill body. An OpenClaw-compatible `{baseDir}` marker in the body is replaced with the absolute Skill directory at activation time. A `file_path` call, including one for a relative `scripts/...` path when script source is needed, returns `{name, status: "file_loaded", file_path, content}` with no absolute path and does not touch activation dedup. User-trigger activation persists an internal note; Tool activation uses its Tool Result as the durable carrier. Identical content for a name deduplicates, but a directly changed package may activate again in the same Session; the newest carrier wins for live and post-Compaction reconstruction.

## Prompt Catalog

Prompt-facing skill metadata is XML and follows the vBot agentskills.io-compatible catalog shape:

```xml
<available_skills>
  <skill_group label="Bundled skills">
    <skill>
      <name>teach</name>
      <description>Teach the user a topic so they actually understand it.</description>
    </skill>
  </skill_group>
  <skill_group label="Your own skills">
    <skill>
      <name>deploy</name>
      <description>Ship the app to the Pi.</description>
    </skill>
  </skill_group>
</available_skills>
```

- `available_skills` is the root; skills are grouped into `<skill_group label="...">` elements by **origin** (in order: Bundled / Your global skills / Skills from project '<name>' / Your own skills). The origin is a tag set at load from each skill's scan root (`SKILL_ORIGIN_*`; a project tag carries the project display name); `_format_skill_list` maps it to the English header. A registry loaded without origins renders one untitled group.
- Each `skill` element contains only `name` and `description` — the catalog stays **path-free**. This is a **presentation preference for prompt economy, not a hard routing rule** (vBot does not truncate the catalog). The explicit `project` Tool returns the loaded Project's own Skills in its ordinary Tool Result; after that result is persisted, the `skill` Tool resolves activation against that Project-aware registry while the current Prompt-Epoch Catalog stays unchanged (see `tools/project.md`).
- The catalog text is prompt-epoch-pinned: the first Session build snapshots it, ordinary Runs reuse it, and every successful manual or automatic Compaction forces a full Skill rescan and replaces it before the next request build. The refreshed catalog therefore reflects additions, removals, description changes, and requirement/environment availability from bundled, global, loaded-extension, Project, and private roots; `seen_skills` is synchronized to the same filtered catalog so those Skills are not redundantly announced. Between Compactions, newly available+allowed Skills are announced once via a tail `<system-reminder>` without changing the prefix. Tool presence and Skill activation remain independent and live.
- The prompt catalog includes only available Skills allowed by the Agent, filtered against the same explicit working-Project/private-layer registry as live activation. `chat.commands` and `prompt.preview` resolve a bare Identity Agent's current `root_project_id`; a qualified Project Config Agent receives no private layer. Lookup failure preserves the saved Project reference and fails closed.
- Skill values inserted into the XML block must be XML-escaped.
- The bundled Skills prompt must explain that `/skill-name` and `$skill-name` user tokens are activation hints once matching `<skill_content>` has been injected, so the Model follows the loaded Skill instructions without echoing the marker as requested output. Listed script paths are absolute and can be passed directly to `bash`; listed reference and asset paths are relative and are read on demand through `skill(name, file_path)`. Script source remains readable through the same Tool with its relative `scripts/...` path.

## Interfaces

- `core/skills/__init__.py` exports `SkillMetadata`, `SkillRegistry`, `SkillAvailability`, `SkillRequirements`, the allowlist/frontmatter constants (`WILDCARD_ALLOWLIST`, `FRONT_MATTER_DELIMITER`), the origin vocabulary (`SKILL_ORIGIN_AGENT`/`GLOBAL`/`BUNDLED`/`PROJECT_PREFIX`, `project_skill_origin`, `skill_origin_sort_key`), the scan helpers (`scan_skill_names`, `scan_project_skill_names`, `project_skills_dir`, `load_project_skill_registry`), and the authoring write core (`SkillAuthoringService`, `SkillWriteResult`, `SkillAuthoringError`, `SkillAuthor`).
- `SkillRegistry.load(skills_dir, extra_dirs=None, environment=None, always_allowed=None, origins=None) -> SkillRegistry` — missing roots mean an empty contribution. `environment` snapshots the env used for requirement checks (see Constraints & Gotchas); when omitted it defaults to `os.environ`. `always_allowed` names bypass the `allowed_skills` filter for this registry only (the runtime passes an agent's own private skills). `origins` is a per-scan-root tag list whose value lands on each loaded skill's `SkillMetadata.origin` for catalog grouping.
- `Runtime.refresh_skills_for(project_id, identity_agent_id=None) -> SkillRegistry` is the explicit Compaction refresh seam: it reloads the global/bundled/loaded-extension pool, invalidates every cached Project/Agent registry, and then returns the freshly resolved scope. Normal consumers use cached `skills_for(...)`; they must not imitate the full rescan independently.
- `load_project_skill_registry(project_cwd, source_format, bundled_scan_roots, environment=None) -> SkillRegistry` — the project-first merge (the format's own skill dir then the bundled roots). `scan_project_skill_names(project_cwd, source_format, environment=None) -> frozenset[str]` — only the project's own skill names (the set the resolver subtracts `skills_project_disabled` from); `scan_skill_names(skills_dir, environment=None) -> frozenset[str]` is the general one-directory scan it (and the agent-private-home scan) builds on. `project_skills_dir(cwd, source_format)` maps the format to its directory via `PROJECT_SKILLS_SUBPATHS` — the format is a **required** argument (no default; every caller states which format it means), and a test asserts the dict's keys equal `core.settings.PROJECT_SOURCE_FORMATS` so the vocabularies cannot drift. `SkillRegistry.load(..., always_allowed=None)` marks names that bypass the `allowed_skills` filter for that registry only. The runtime owns the caches and exposes `agent_skills_dir(agent_id)` (= `<data_dir>/agents/<id>/skills`), `skills_for(project_id, identity_agent_id=None)`, `project_skill_names(project_id)`, `invalidate_project_skills(project_id=None)` (also drops matching agent-aware entries), and `invalidate_agent_skills(agent_id=None)`.
- Registry read surface (return types live on the `core/skills/` dataclasses): `get(name)`, `list_all()`, `filter_allowed(allowed_skills)`, `availability_for(name, allowed_skills=None)`, `is_allowed(name, allowed_skills)`, `diagnostics()`, `invalid_diagnostics()`, `warnings_for(name)`.
- Activation helper behavior (`load_skill_content`): read the skill body after YAML frontmatter, substitute `{baseDir}`, scan the resource directories, and build the full `<skill_content>` payload (skill directory + resolution note + resources + body). That payload is the model-visible content on both activation paths: a `skill` tool load returns it inside the tool result (`data.content` — the tool result is the durable carrier), a user trigger persists it as a `[skill-context]` note under the triggering message (see `tools/skill.md` and `chat.md`).

## Conventions

- `allowed_skills=['*']`, or a missing/`None` allowlist, exposes all loaded skills — this is real `_allowed_names` behavior, not just a test default.
- `allowed_skills=[]` exposes none.
- Explicit allowlists match exact skill names.
- Unknown allowlist entries are ignored because skills are not hard execution gates.
- Skill dependency requirements (`skill: other-skill`) must not bypass agent allowlists. If the dependency skill is not allowed for the current agent, the dependent skill is unavailable for that agent.
- Duplicate skill names are resolved by first-found-wins scan order and recorded as diagnostics for rejected duplicates. In a project's merged registry the project skill dir is scanned first, so a project skill **wins** a name collision with a bundled skill of the same name (one slot, the project's own playbook wins); the WebUI editor drops the shadowed bundled name from the opt-in list.
- `skill` and `skill_manage` are **ordinary allow-list tools** — controlled by `allowed_tools`, listed in the catalog (`tool.list`), and toggleable per agent in the Agents tab like any tool. Neither is gated on the agent already having a skill (a skill can be authored or activated mid-session). `skill` is seeded default-on in the Project Tool Whitelist; `skill_manage` is **identity-only** — `IDENTITY_ONLY_TOOLS` withholds it from a config/project agent (empty `workspace`) at both the dispatch-time allowlist and the prompt's visibility pass, even under a wildcard allow-list, and it is excluded from the Project Tool Whitelist surface (frontend `PROJECT_TOOL_WHITELIST_EXCLUDED`).
- Full skill instructions have a single provider-visible source per activation: the carrier at the point of activation (the `skill` tool result, or the trigger note under the user message). Once-per-session dedup (`ChatSession.register_skill_activation`) keeps a re-load from producing a second copy; after a compaction the chat loop re-injects carriers that were summarized away, still exactly one copy per skill (see `chat.md`).
- `/skill-name` and `$skill-name` triggers preserve the original user message. Unknown, non-loadable, or unavailable triggers become internal system reminders rather than activations.
- `$skill-name` is a Skill-only mention convention. Surfaces that provide `$` autocomplete must list only currently available Skills and must not include Slash Commands. Slash autocomplete may list active Built-in and Extension Commands plus available Skills because `/` is the shared user-entry affordance; backend Command dispatch handles every recognized Command before the normal Skill-trigger path. A same-named Skill therefore remains explicitly reachable through `$skill-name`.

## vBot Requirements Metadata

Example:

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
        - skill: vbot-cli
      optional:
        - binary: jq
```

- `all` requires every child node.
- `any` requires at least one child node.
- `optional` is a list of nodes whose missing checks are reported but do not change `available` status.
- `env` checks for a non-empty value in the snapshot skill environment: process environment first, then data-dir `.env` fallback (see Constraints & Gotchas).
- `binary` looks up the snapshot environment's `PATH` with `shutil.which` — safe path lookup, not shell execution.
- `skill` checks that the dependency skill is loadable, available, and allowed for the current agent.
- `skill` dependency chains are walked with a cycle guard: a circular `skill:` requirement resolves to `unavailable` with a `skill dependency cycle: a -> b -> a` reason instead of recursing.
- Malformed `metadata.vbot.requirements` makes the skill invalid/non-loadable.

## External Dependencies

- `pyyaml` is a direct core dependency for `SKILL.md` YAML frontmatter parsing.

## Constraints & Gotchas

- Requirement `env`/`binary` checks run against an environment snapshot captured when the registry is loaded/reloaded (`.env` fallback overlaid by `os.environ`, process env winning), not live `os.environ` at activation time. A newly exported key or freshly installed `PATH` binary does not flip a skill's availability until the registry reloads.
- The prompt catalog and `skill_list` result keep paths out as a presentation preference (prompt economy, clean UI), not a hard routing rule. The explicit `project` Tool result may carry Project Skill paths as structured context metadata, but Agents activate those Skills normally by name. The activation payload exposes absolute paths for executable scripts and relative paths for readable references and assets. Trackable `skill(name, file_path)` reads still accept only relative package paths and return only that relative path. Provenance (`metadata.vbot.author`/`source`) is internal too — it never appears in the catalog (only name/description/origin do).
- Non-loadable skill directories should be retained as diagnostics so the UI can explain invalid YAML, missing descriptions, or duplicate names.
- Skill metadata warnings are logged (WARN, `vbot.skills`) **once per process**, keyed by `(resolved SKILL.md path, warning text)`, and the message carries that path so the offending file is locatable. Registries reload on every project run/reload, so without this guard a repaired/mismatched skill floods the log; a server restart starts a fresh process and logs the current warnings once again. Only the log is deduplicated — the diagnostics returned to callers (`warnings_for`, `diagnostics`, `skill.list`/UI) still carry every warning on every load.
- The project forbids in-app legacy compatibility. Do not add automatic migrations for older `allowed_skills` formats; use explicit converter scripts if needed.
