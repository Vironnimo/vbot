# Skills

Local skill metadata loading, validation diagnostics, and prompt allowlist filtering.

## Overview

`core/skills/` scans bundled skills under `resources/skills/`, user (global) skills under `<data_dir>/skills/`, per-agent private skills under `<data_dir>/agents/<id>/skills/`, project skills under the project's declared source format's directory (`PROJECT_SKILLS_SUBPATHS`: `opencode → <cwd>/.opencode/skills/`, `claude → <cwd>/.claude/skills/` — GLOSSARY → Source Format), configured extra skill directories, and the `skills/` folder of every **loaded extension** (see Extension-bundled skills). A directory is considered a skill only when it contains `SKILL.md`; the authored vBot package shape is exactly that file plus optional regular files under `scripts/`, `references/`, and `assets/`. The package is **read and write**: alongside the registry/loader, `core/skills/authoring.py` is the one validated write core (see Authoring & Write Scope) shared by every surface that creates or edits a skill.

**Extension-bundled skills.** An extension in package/directory form may ship skills under `<extension>/skills/` (`<data_dir>/extensions/<name>/skills/` or a bundled `resources/extensions/<name>/skills/`), with no code — just the folder. The runtime folds every **loaded** extension's `skills/` dir into the global scan-root list (`Runtime._extension_skill_dirs`, appended by `_skill_scan_roots`), so extension skills present as **global** skills (origin `global`), subject to the normal agent allow-list and project opt-out — never always-allowed, so nothing bypasses the project skill whitelist. They are scanned **after** the user's own `<data_dir>/skills`, so a hand-authored global skill wins a name collision. Only `loaded` extensions contribute (disabled/failed/overridden add nothing); a single-file `.py` extension has no folder for a `skills/` child. The pool is refreshed live on every extension enable/disable/reload (`reload_extensions` and `apply_extension_disabled_change` both call `reload_skills`). They are **read-only** in the skill editor: `skill.read`/`skill.*` writes target only the `global`/`agent:<id>` scope directories (never an extension folder), so an extension skill never appears in the editor's editable list and no write can reach it — to customize one, copy it into `<data_dir>/skills` (your copy then shadows it by the precedence above).

**Project- and agent-scoped pool.** `runtime.skills_for(project_id, identity_agent_id=None)` merges selected Project, global/bundled, and optional Identity-private layers. A Rooted Identity Agent passes its internal `working_project_id` plus its own id, yielding agent > project > global > bundled while retaining its own allowlist; a Config Agent passes its Session Project plus no identity id, so Project whitelist ceilings still apply only there. Live Runs, compaction, preview, activation, and autocomplete share this policy.

Skills are playbooks, not normal user-managed tools. The registry exposes prompt metadata and internal activation metadata; actual activation is handled by the chat/tool pipeline. Agents can activate skills through the internal `skill` tool (which also has a **list mode**: called with no `name` it returns the current available skills grouped by origin from the live agent-aware registry — a tool result, not prompt text — and **rescans skills from disk on a name miss** so a skill hand-dropped into a skill directory activates by name without a restart; see `tools/skill.md`), while user messages can activate skills deterministically through `/skill-name` at the start of the message or `$skill-name` anywhere in the message before the provider request is sent.

## Terms

Domain-specific vocabulary for skills. The core Skill term lives in `.vorch/GLOSSARY.md`.

### Per-Agent Skill
**Definition:** A Skill that lives in one agent's **private** home `<data_dir>/agents/<id>/skills/` (archived with the agent on delete, like its Workspace). It is visible and loadable **only** to that agent — layered on top of the project/global/bundled pool at the highest precedence (agent > project > global > bundled) — and is **always-allowed for its owner**, bypassing the agent's `allowed_skills` filter. An agent authors its own per-agent skills with the `skill_manage` tool (and via `/learn`); the user can curate any agent's per-agent skills via the WebUI/RPC (`agent:<id>` scope).
**Not:** A global skill (`<data_dir>/skills/`, shared across the user's identity agents, user-curated), a project/team skill (the project's Source Format skill directory, e.g. `<cwd>/.opencode/skills/` or `<cwd>/.claude/skills/`, repo-owned), or a bundled skill (`resources/skills/`, read-only). Those shared-pool skills stay subject to the agent's allow-list; only an agent's own private skills bypass it.

### Skill Draft
**Definition:** An isolated copy or empty package under `<data_dir>/artifacts/temp/skill-drafts/<draft-id>/package/`, bound to the creating Agent, one resolved write scope, one Skill name, and `create` or `update` mode. Storage owns this canonical placement; Skills authoring owns Draft isolation, validation, commit, abort, and cleanup. Draft mutations are invisible to discovery and activation; complete-package validation plus `commit` publishes the package, while `abort` discards only the draft.
**Not:** A loadable Skill, an autosaved edit of the live package, or a Project/repo staging area.

### Session-Pinned Catalog
**Definition:** The system-prompt skill catalog **text** (`<available_skills>`) **snapshotted on a Session's first build and reused for that Session's lifetime** (stored in session metadata as a `PinnedSkillCatalog`), so a skill written mid-session never changes a running Session's prompt prefix — keeping the provider prompt cache intact. The compaction rebuild reuses the same snapshot. **Tool presence is not part of the snapshot:** `skill` and `skill_manage` are ordinary allow-list tools offered per the agent's `allowed_tools` (`skill_manage` identity-only), so neither depends on the pin — and, like any tool, both stay stable across a Session unless the agent's config changes.
**Not:** A freeze on *using* skills, or on *learning about* them. Skill **activation** (the `skill` tool) and `/`–`$` triggers stay **live** against the current registry, so a newly authored skill is loadable by name immediately; only the *advertised catalog text* is pinned. A skill that becomes available mid-session is still surfaced to the model — by a Skill Availability Announcement (below) at the conversation tail, not by changing the pinned prefix. A **new** Session pins a fresh snapshot and therefore sees the new skill in its catalog.

### Skill Availability Announcement
**Definition:** A one-time `<system-reminder>` note appended at the conversation tail when a skill becomes available+allowed to an agent mid-Session that it had not already been shown — whether newly authored (`skill_manage`), opted into a Project (bundled/global), added to the global pool, or freshly scanned from a repo. Because the prompt's `<available_skills>` catalog is session-pinned for cache stability, this tail note is how a running Session's model learns of a new skill without the cached prompt prefix changing. Computed at run setup by diffing the agent's current available+allowed skills against a per-Session "seen" set in session metadata; the first build seeds that set without announcing.
**Not:** A removal notice (additions only — a skill going away is deliberately not announced), nor a change to the pinned catalog (the prompt prefix stays byte-identical). Not the `skill` tool's list mode either — that is a pull the agent initiates; the announcement is a push.

## Authoring & Write Scope

`core/skills/authoring.py` (`SkillAuthoringService`) is the single validated write core, shared by every authoring surface. It operates on an **already-resolved target root** (scope→root resolution is the caller's job). The package lifecycle offers published/draft inspection, `begin_draft`, UTF-8 text writes, byte-preserving source-file copies, unique text patches, support-file removal, complete-package validation, `commit_draft`, `abort_draft`, and recoverable `archive_skill`; the RPC/accessor compatibility surface still uses the direct `create` / `edit` / `patch` / `delete` / `write_file` / `remove_file` methods. It reuses `validate_skill_metadata` + `parse_vbot_requirements` (reject missing name/description and malformed requirements), confines every path strictly under the target root, refuses a target at/under a protected bundled root, and stamps `author` plus optional `source` under `metadata.vbot`. Package validation rejects missing/non-UTF-8 `SKILL.md`, symlinks, special files, and every top-level path except `SKILL.md`, `scripts/`, `references/`, and `assets/`; an authored Skill's front-matter `name` must equal its directory and match `SKILL_NAME_TRIGGER_PATTERN` so `/name` and `$name` can trigger it. Failures raise `SkillAuthoringError` with surface-neutral diagnostics.

**Write-scope boundary (v1 is data-dir only — vBot never writes the repo as runtime state):**
- **Agent tool `skill_manage`** (`core/tools/skill_manage.py`, an ordinary allow-list tool, always-registered, *not* gated on having a Skill): every call contains exactly one top-level operation object, for example `{"begin":{"name":"wiki-research","mode":"create"}}`; each operation object exposes only its valid fields, structurally requires its mandatory fields, and rejects extras. This nested operation shape keeps conditional requirements provider-visible without a root JSON-Schema union, which the Anthropic wire sanitizer must remove. `inspect` reads a published package or owned Draft manifest and may return one selected UTF-8 file; `begin` creates an isolated `create`/`update` Draft; `put_file` writes text or byte-copies a regular `source_path`; `patch` and `remove_file` mutate Draft files; `validate` checks the complete vBot package; `commit` publishes it; `abort` discards it; `delete` moves the published package under `<data_dir>/archive/skills/` and returns the recovery path. A binary `source_path` must resolve inside the current Project cwd or Workspace, and `executable=true` is valid only under `scripts/`. The `scope` is `own` (default, the calling Identity Agent's private home) or `global` (only when the user explicitly requested a global Skill); each Draft records and rechecks its actor and resolved scope root, so a leaked id cannot cross Agent or scope boundaries. Draft-only operations neither invalidate registries nor log a live mutation; only `commit` and archive invalidate/reload and emit the control-plane event. Project/repo and bundled Skills are never targets. `skill_manage` remains **identity-only** through `IDENTITY_ONLY_TOOLS` at both dispatch and prompt visibility, is toggleable for Identity Agents, and is excluded from the Project Tool Whitelist.
- **`/learn`**: a user-triggered internal run seeded with an authoring brief that uses `skill_manage` to author into the current agent's home (see `chat.md`).
- **RPC + user Accessors** (`server/rpc/skill_methods.py`, WebUI, CLI): `skill.read` (scope's own Skills with content), `skill.create` / `skill.update` / `skill.delete` / `skill.write_file` / `skill.remove_file`, each scoped `global` (`<data_dir>/skills`) or `agent:<id>`, `author="human"`; a Project/repo scope is rejected; global writes `reload_skills`, Agent writes `invalidate_agent_skills`. The Agent-scope id is validated traversal-safe before any path is built, and must name an **existing Identity Agent** — an unknown id (e.g. a Project Team slug) is rejected with `invalid_request` instead of silently creating an unowned `agents/<id>/skills` home.
- **Project skills** (the chosen format's skill directory, e.g. `<cwd>/.opencode/skills/` or `<cwd>/.claude/skills/`) stay repo-owned: authored with the ordinary `write`/`edit` file tools, validated at scan/load. The skill write core/tool/RPCs never write the repo in v1.
- **Bundled** (`resources/skills/`) is read-only — the write core refuses it.

## Data Model

- `SkillMetadata`: `name`, `description`, internal `path`, optional `license`, `compatibility`, `metadata`, `allowed_tools`, and parsed vBot requirements from YAML frontmatter.
- `SkillDraft`: opaque id, Skill name, `create`/`update` mode, and isolated package path; persisted Draft metadata additionally binds actor, resolved target root, author, and optional provenance source.
- `SkillPackageFile`: manifest entry with package-relative `path`, vBot package `kind`, byte `size`, SHA-256, MIME guess, binary flag, and executable flag. `SkillPackageInspection` returns the manifest, `SKILL.md`, structural diagnostics, and optional selected UTF-8 resource content; binary selected files remain metadata-only.
- `SkillDiagnostic`: `name`, `path`, `valid`, `warnings`, and `loadable` for both loadable skills with warnings and rejected skill directories.
- Skill availability has three runtime states: `invalid` for malformed/non-loadable skills, `unavailable` for loadable skills with unmet required vBot requirements, and `available` when required requirements are satisfied. Optional requirements never make a skill unavailable.
- YAML frontmatter is parsed with PyYAML. Validation is lenient: name/directory mismatch, names longer than 64 characters, and names using characters outside letters/digits/`-`/`_` (or not starting with a letter or digit) are warnings only — such a name still loads but can never be triggered with `/name` or `$name` (only via the `skill` tool's explicit `name` argument); missing required fields make the skill non-loadable. Invalid YAML is not always fatal — a repair pass re-quotes unquoted scalar values that contain a colon-space (`key: a: b`) and re-parses; if that succeeds the skill loads with a `MALFORMED_YAML_FALLBACK_WARNING`, otherwise it is non-loadable.
- vBot-specific machine-checkable requirements live under `metadata.vbot.requirements`, not `compatibility`. Supported required/optional primitives are `env`, `binary`, and `skill`, composed with nested `all` and `any` groups. Provider requirements are intentionally not supported; model/provider-specific prerequisites should be expressed as concrete env vars or skill instructions.
- Resource paths are not stored in `SkillMetadata`; the resource directories (`RESOURCE_DIRECTORIES`: `scripts/`, `references/`, `assets/`) are scanned at activation time.
- Bundled `resources/skills/` contains tiny loadable sample skills for normal activation flows. Warning and broken skill diagnostics are covered by tests with local fixtures rather than shipped as bundled resources.
- Activated skill content is wrapped in `<skill_content name="...">`. The wrapper always opens with the absolute skill directory (POSIX form) plus a note that relative paths resolve against it, then an optional `<resources>` list of the scanned resource-directory paths (relative to the skill directory), then the skill body. An OpenClaw-compatible `{baseDir}` marker in the body is replaced with the absolute skill directory at activation time. User-trigger activation persists an internal note; Tool activation uses its Tool Result as the durable carrier. Identical content for a name deduplicates, but a committed changed package may activate again in the same Session; the newest carrier wins for live and post-Compaction reconstruction.

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
- Each `skill` element contains only `name` and `description` — the catalog stays **path-free**. This is a **presentation preference for prompt economy, not a hard routing rule** (vBot does not truncate the catalog). The explicit `project` Tool returns the loaded Project's own Skills in its ordinary Tool Result; after that result is persisted, the `skill` Tool resolves activation against that Project-aware registry while the Session-pinned System Prompt catalog stays unchanged (see `tools/project.md`).
- The catalog **text** is **session-pinned**: snapshotted on a session's first build and reused for that session's lifetime, so a skill written mid-session never changes a running session's `<available_skills>` or its prompt cache. The `skill` tool itself is **always** offered, so **tool presence is no longer pinned** — only the catalog text is. Skill *activation* and `/`–`$` triggers stay live (a new skill is loadable at once); a new session pins a fresh snapshot. A skill that becomes available+allowed mid-session (authored, opted-in, added) is announced **once** into the running session via a tail `<system-reminder>` (additions only — removals are not announced), so the model learns of it without the pinned prompt prefix changing — see `chat.md` (the availability announcement). Mechanics live in `prompts.md` (`PinnedSkillCatalog`) and `chat.md`.
- The prompt catalog includes only available Skills allowed by the Agent, filtered against the same explicit working-Project/private-layer registry as live activation. `chat.commands` and `prompt.preview` resolve a bare Identity Agent's current `root_project_id`; a qualified Project Config Agent receives no private layer. Lookup failure preserves the saved Project reference and fails closed.
- Skill values inserted into the XML block must be XML-escaped.
- The bundled skills prompt must explain that `/skill-name` and `$skill-name` user tokens are activation hints once matching `<skill_content>` has been injected, so the model follows the loaded skill instructions without echoing the marker as requested output. It must also state the path-resolution rule: relative paths in loaded skill content resolve against the announced skill directory (not the cwd), tool calls should use absolute paths, and resource files are read on demand.

## Interfaces

- `core/skills/__init__.py` exports `SkillMetadata`, `SkillRegistry`, `SkillAvailability`, `SkillRequirements`, the allowlist/frontmatter constants (`WILDCARD_ALLOWLIST`, `FRONT_MATTER_DELIMITER`), the origin vocabulary (`SKILL_ORIGIN_AGENT`/`GLOBAL`/`BUNDLED`/`PROJECT_PREFIX`, `project_skill_origin`, `skill_origin_sort_key`), the scan helpers (`scan_skill_names`, `scan_project_skill_names`, `project_skills_dir`, `load_project_skill_registry`), and the authoring write core (`SkillAuthoringService`, `SkillWriteResult`, `SkillAuthoringError`, `SkillAuthor`).
- `SkillRegistry.load(skills_dir, extra_dirs=None, environment=None, always_allowed=None, origins=None) -> SkillRegistry` — missing roots mean an empty contribution. `environment` snapshots the env used for requirement checks (see Constraints & Gotchas); when omitted it defaults to `os.environ`. `always_allowed` names bypass the `allowed_skills` filter for this registry only (the runtime passes an agent's own private skills). `origins` is a per-scan-root tag list whose value lands on each loaded skill's `SkillMetadata.origin` for catalog grouping.
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
- The prompt catalog and `skill.list` keep paths out as a **presentation preference** (prompt economy, clean UI), not a hard rule — vBot does not truncate the catalog, and Skills load by name through the `skill` Tool there. The explicit `project` Tool result may carry Project Skill paths as structured context metadata, but agents activate those Skills normally by name; the **activation payload** remains the path-carrying complement (the agentskills.io-recommended shape for dedicated-tool activation): each `<skill_content>` states its absolute Skill directory, so the catalog can stay path-free without leaving bundled resources unreachable. Provenance (`metadata.vbot.author`/`source`) is internal too — it never appears in the catalog (only name/description/origin do).
- Non-loadable skill directories should be retained as diagnostics so the UI can explain invalid YAML, missing descriptions, or duplicate names.
- Skill metadata warnings are logged (WARN, `vbot.skills`) **once per process**, keyed by `(resolved SKILL.md path, warning text)`, and the message carries that path so the offending file is locatable. Registries reload on every project run/reload, so without this guard a repaired/mismatched skill floods the log; a server restart starts a fresh process and logs the current warnings once again. Only the log is deduplicated — the diagnostics returned to callers (`warnings_for`, `diagnostics`, `skill.list`/UI) still carry every warning on every load.
- The project forbids in-app legacy compatibility. Do not add automatic migrations for older `allowed_skills` formats; use explicit converter scripts if needed.
