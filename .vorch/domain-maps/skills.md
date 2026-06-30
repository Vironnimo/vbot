# Skills

Local skill metadata loading, validation diagnostics, and prompt allowlist filtering.

## Overview

`core/skills/` scans bundled skills under `resources/skills/`, user (global) skills under `<data_dir>/skills/`, per-agent private skills under `<data_dir>/agents/<id>/skills/`, project skills under `<cwd>/.opencode/skills/`, and configured extra skill directories. A directory is considered a skill only when it contains `SKILL.md`. The package is **read and write**: alongside the registry/loader, `core/skills/authoring.py` is the one validated write core (see Authoring & Write Scope) shared by every surface that creates or edits a skill.

**Project- and agent-scoped pool.** A project run uses a *merged* registry: the project's own skills under `<cwd>/.opencode/skills/` scanned **first** (so a project skill wins a name collision with a bundled one), then the same bundled scan roots the global registry uses. The single seam is `runtime.skills_for(project_id, agent_id=None)` — both `None` returns the global registry (identity runs, byte-identical), a set `project_id` returns the cached per-project merge, and an `agent_id` whose private skills home `<data_dir>/agents/<id>/skills/` exists layers that home **on top** (scanned first of all, so precedence is agent > project > global > bundled). An agent's own private skills are **always-allowed for that owner** (they bypass the agent's `allowed_skills` filter via the registry's `always_allowed` set) and stay invisible to other agents (whose registries never scan that home). The effective skill project is rooted-aware: a rooted identity agent runs with `project_id is None` but resolves skills against its home project, so it sees that project's skills. The runtime caches per-project merges by `project_id` and agent-aware registries by `(project_id, agent_id)` (like the resolver's Team cache) and drops them on the same triggers (project open, cwd change, project removal, global `reload_skills`, plus an agent skill write via `invalidate_agent_skills`) — `project.show` re-scans the Team on every call **and reloads the global skill registry from disk** (there is no filesystem watcher otherwise), so it drops the caches first and a skill hand-dropped into the global skills folder surfaces in the editor's opt-in pool without a restart — the WebUI Projects **Refresh** re-runs `project.show` for the expanded project, so it is the user gesture that picks up disk drops (runs never call `project.show`, so per-run caching is unaffected). All run-time skill consumers — prompt assembly, `/`–`$` triggers, the internal `skill` tool, and autocomplete — resolve through this one seam, so project skills never leak between projects or to the home agent.

Skills are playbooks, not normal user-managed tools. The registry exposes prompt metadata and internal activation metadata; actual activation is handled by the chat/tool pipeline. Agents can activate skills through the internal `skill` tool (which also has a **list mode**: called with no `name` it returns the current available skills grouped by origin from the live agent-aware registry — a tool result, not prompt text), while user messages can activate skills deterministically through `/skill-name` at the start of the message or `$skill-name` anywhere in the message before the provider request is sent.

## Authoring & Write Scope

`core/skills/authoring.py` (`SkillAuthoringService`) is the single validated write core, shared by every authoring surface. It operates on an **already-resolved target root** (scope→root resolution is the caller's job) and offers `create` / `edit` (full `SKILL.md` rewrite) / `patch` (one unique `old_string`→`new_string`) / `delete` plus `write_file` / `remove_file` for `scripts/`/`references/` support files. It reuses `validate_skill_metadata` + `parse_vbot_requirements` (reject missing name/description and malformed requirements), confines every path strictly under the target root (rejecting traversal, illegal names, and anything outside `SKILL.md` + the resource dirs), refuses a target at/under a protected (bundled `resources/skills`) root, and stamps provenance — `author` (`agent`/`human`) and optional `source` — under `metadata.vbot` in the written front matter (beside `requirements`). It is stricter than the lenient loader: an authored skill's front-matter `name` must equal its directory. Failures raise `SkillAuthoringError` carrying `diagnostics` that each surface forwards.

**Write-scope boundary (v1 is data-dir only — vBot never writes the repo as runtime state):**
- **Agent tool `skill_manage`** (`core/tools/skill_manage.py`, an ordinary allow-list tool, always-registered, *not* gated on having a skill): writes **only the calling agent's own home** `<data_dir>/agents/<id>/skills/` (no scope parameter), `author="agent"`; on success calls `invalidate_agent_skills(agent_id)` so the new skill is loadable by name in the same session. It is **identity-only**: even when an agent's `allowed_tools` would include it (e.g. via a wildcard), the prompt layer's identity-only visibility (`IDENTITY_ONLY_TOOLS`, see `prompts.md`) withholds it from a config/project agent (empty `workspace`), which has no private home to write to. For identity agents it is a normal per-agent toggle, and it is excluded from the Project Tool Whitelist surface entirely.
- **`/learn`**: a user-triggered internal run seeded with an authoring brief that uses `skill_manage` to author into the current agent's home (see `chat.md`).
- **RPC + WebUI** (`server/rpc/skill_methods.py`): `skill.read` (scope's own skills with content), `skill.create` / `skill.update` / `skill.delete` / `skill.write_file` / `skill.remove_file`, each scoped `global` (`<data_dir>/skills`) or `agent:<id>`, `author="human"`; a project/repo scope is rejected; global writes `reload_skills`, agent writes `invalidate_agent_skills`. The agent-scope id is validated traversal-safe before any path is built.
- **Project skills** (`<cwd>/.opencode/skills/`) stay repo-owned: authored with the ordinary `write`/`edit` file tools, validated at scan/load. The skill write core/tool/RPCs never write the repo in v1.
- **Bundled** (`resources/skills/`) is read-only — the write core refuses it.

## Data Model

- `SkillMetadata`: `name`, `description`, internal `path`, optional `license`, `compatibility`, `metadata`, `allowed_tools`, and parsed vBot requirements from YAML frontmatter.
- `SkillDiagnostic`: `name`, `path`, `valid`, `warnings`, and `loadable` for both loadable skills with warnings and rejected skill directories.
- Skill availability has three runtime states: `invalid` for malformed/non-loadable skills, `unavailable` for loadable skills with unmet required vBot requirements, and `available` when required requirements are satisfied. Optional requirements never make a skill unavailable.
- YAML frontmatter is parsed with PyYAML. Validation is lenient: name/directory mismatch and names longer than 64 characters are warnings; missing required fields make the skill non-loadable. Invalid YAML is not always fatal — a repair pass re-quotes unquoted scalar values that contain a colon-space (`key: a: b`) and re-parses; if that succeeds the skill loads with a `MALFORMED_YAML_FALLBACK_WARNING`, otherwise it is non-loadable.
- vBot-specific machine-checkable requirements live under `metadata.vbot.requirements`, not `compatibility`. Supported required/optional primitives are `env`, `binary`, and `skill`, composed with nested `all` and `any` groups. Provider requirements are intentionally not supported; model/provider-specific prerequisites should be expressed as concrete env vars or skill instructions.
- Resource paths are not stored in `SkillMetadata`; `scripts/` and `references/` are scanned at activation time.
- Bundled `resources/skills/` contains tiny loadable sample skills for normal activation flows. Warning and broken skill diagnostics are covered by tests with local fixtures rather than shipped as bundled resources.
- Activated skill content is wrapped in `<skill_content name="...">`, optionally preceded inside the wrapper by a `<resources>` list of the scanned `scripts/`/`references/` paths, followed by the skill body. It is persisted as an internal chat note so it remains available across later turns in the same Session without appearing as a normal visible chat message.

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
- Each `skill` element contains only `name` and `description` — the catalog stays **path-free**. This is a **presentation preference for prompt economy, not a hard routing rule** (vBot does not truncate the catalog). The **visiting-project reminder deliberately carries paths**: it lists a reached-into project's skills with each absolute `SKILL.md` path and a "read it with the `read` tool" instruction, because a visitor is not a project member and loads a project playbook by reading the file, not via the `skill` tool (see `chat.md`).
- The catalog **text** is **session-pinned**: snapshotted on a session's first build and reused for that session's lifetime, so a skill written mid-session never changes a running session's `<available_skills>` or its prompt cache. The `skill` tool itself is **always** offered, so **tool presence is no longer pinned** — only the catalog text is. Skill *activation* and `/`–`$` triggers stay live (a new skill is loadable at once); a new session pins a fresh snapshot. A skill that becomes available+allowed mid-session (authored, opted-in, added) is announced **once** into the running session via a tail `<system-reminder>` (additions only — removals are not announced), so the model learns of it without the pinned prompt prefix changing — see `chat.md` (the availability announcement). Mechanics live in `prompts.md` (`PinnedSkillCatalog`) and `chat.md`.
- The prompt catalog includes only skills allowed for the agent (`agent.allowed_skills`) and currently `available`, filtered against the run's agent-aware project-scoped registry (`build_system_prompt`/`provider_tool_definitions` take an optional `skill_registry` plus a pinned `skill_catalog` snapshot; the chat loop passes `runtime.skills_for(skill_project_id, agent_id)`). An agent's own private skills are always-allowed for it. Command autocomplete (`chat.commands` RPC) **is** agent-scoped: given an optional `agent_id` (a bare id or an `agent@projekt` address) it resolves the agent and returns its effective skills via `skills_for(project_id, agent_id).filter_allowed(agent.allowed_skills)`; with no address it returns the global list. The WebUI passes the active agent's address. `skill.list` still returns unavailable skills with requirement details (availability state plus missing/optional-missing) so the user can fix local prerequisites.
- Skill values inserted into the XML block must be XML-escaped.
- The bundled skills prompt must explain that `/skill-name` and `$skill-name` user tokens are activation hints once matching `<skill_content>` has been injected, so the model follows the loaded skill instructions without echoing the marker as requested output.

## Interfaces

- `core/skills/__init__.py` exports `SkillMetadata`, `SkillRegistry`, `SkillAvailability`, `SkillRequirements`, the allowlist/frontmatter constants (`WILDCARD_ALLOWLIST`, `FRONT_MATTER_DELIMITER`), the origin vocabulary (`SKILL_ORIGIN_AGENT`/`GLOBAL`/`BUNDLED`/`PROJECT_PREFIX`, `project_skill_origin`, `skill_origin_sort_key`), the scan helpers (`scan_skill_names`, `scan_project_skill_names`, `project_skills_dir`, `load_project_skill_registry`), and the authoring write core (`SkillAuthoringService`, `SkillWriteResult`, `SkillAuthoringError`, `SkillAuthor`).
- `SkillRegistry.load(skills_dir, extra_dirs=None, environment=None, always_allowed=None, origins=None) -> SkillRegistry` — missing roots mean an empty contribution. `environment` snapshots the env used for requirement checks (see Constraints & Gotchas); when omitted it defaults to `os.environ`. `always_allowed` names bypass the `allowed_skills` filter for this registry only (the runtime passes an agent's own private skills). `origins` is a per-scan-root tag list whose value lands on each loaded skill's `SkillMetadata.origin` for catalog grouping.
- `load_project_skill_registry(project_cwd, bundled_scan_roots, environment=None) -> SkillRegistry` — the project-first merge (project's own `.opencode/skills/` then the bundled roots). `scan_project_skill_names(project_cwd, environment=None) -> frozenset[str]` — only the project's own skill names (the set the resolver subtracts `skills_project_disabled` from); `scan_skill_names(skills_dir, environment=None) -> frozenset[str]` is the general one-directory scan it (and the agent-private-home scan) builds on. `project_skills_dir(cwd)` returns `<cwd>/.opencode/skills`. `SkillRegistry.load(..., always_allowed=None)` marks names that bypass the `allowed_skills` filter for that registry only. The runtime owns the caches and exposes `agent_skills_dir(agent_id)` (= `<data_dir>/agents/<id>/skills`), `skills_for(project_id, agent_id=None)`, `project_skill_names(project_id)`, `invalidate_project_skills(project_id=None)` (also drops matching agent-aware entries), and `invalidate_agent_skills(agent_id=None)`.
- `get(name) -> SkillMetadata`
- `list_all() -> list[SkillMetadata]`
- `filter_allowed(allowed_skills) -> list[SkillMetadata]`
- `availability_for(name, allowed_skills=None) -> SkillAvailability`
- `is_allowed(name, allowed_skills) -> bool`
- `diagnostics() -> list[SkillDiagnostic]`
- `invalid_diagnostics() -> list[SkillDiagnostic]`
- `warnings_for(name) -> list[str]`
- Activation helper behavior: read the skill body after YAML frontmatter, scan `scripts/` and `references/`, and build the full `<skill_content>` payload for session storage. The internal `skill` tool result returned to the model is only a minimal status envelope; it must not repeat the full skill body.

## Conventions

- `allowed_skills=['*']`, or a missing/`None` allowlist, exposes all loaded skills — this is real `_allowed_names` behavior, not just a test default.
- `allowed_skills=[]` exposes none.
- Explicit allowlists match exact skill names.
- Unknown allowlist entries are ignored because skills are not hard execution gates.
- Skill dependency requirements (`skill: other-skill`) must not bypass agent allowlists. If the dependency skill is not allowed for the current agent, the dependent skill is unavailable for that agent.
- Duplicate skill names are resolved by first-found-wins scan order and recorded as diagnostics for rejected duplicates. In a project's merged registry the project skill dir is scanned first, so a project skill **wins** a name collision with a bundled skill of the same name (one slot, the project's own playbook wins); the WebUI editor drops the shadowed bundled name from the opt-in list.
- `skill` and `skill_manage` are **ordinary allow-list tools** — controlled by `allowed_tools`, listed in the catalog (`tool.list`), and toggleable per agent in the Agents tab like any tool. Neither is gated on the agent already having a skill (a skill can be authored or activated mid-session). `skill` is seeded default-on in the Project Tool Whitelist; `skill_manage` is **identity-only** — withheld from a config/project agent (empty `workspace`) by the prompt's identity-only visibility (`IDENTITY_ONLY_TOOLS`) even under a wildcard allow-list, and excluded from the Project Tool Whitelist surface (frontend `PROJECT_TOOL_WHITELIST_EXCLUDED`).
- Full skill instructions have a single provider-visible source: the session-scoped injected `<skill_content>` note. Tool-call results for the internal `skill` tool must not include `content`, raw skill Markdown, or a `<skill_content>` block, otherwise the model sees duplicate instructions.
- `/skill-name` and `$skill-name` triggers preserve the original user message. Unknown, non-loadable, or unavailable triggers become internal system reminders rather than activations.
- `$skill-name` is a skill-only mention convention. Surfaces that provide `$` autocomplete must list only currently available skills and must not include built-in slash commands. Slash autocomplete may list both built-in commands and available skills because `/` is the shared user-entry affordance; backend command dispatch still handles only recognized built-in commands before the normal skill-trigger path.

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
- The prompt catalog and `skill.list` keep paths out as a **presentation preference** (prompt economy, clean UI), not a hard rule — vBot does not truncate the catalog, and skills load by name through the `skill` tool there. The deliberate exception is the **visiting-project reminder**, which carries each project skill's absolute `SKILL.md` path on purpose so a visiting agent can read the file directly (it has no `skill`-tool path into that project). Provenance (`metadata.vbot.author`/`source`) is internal too — it never appears in the catalog (only name/description/origin do).
- Non-loadable skill directories should be retained as diagnostics so the UI can explain invalid YAML, missing descriptions, or duplicate names.
- Skill metadata warnings are logged (WARN, `vbot.skills`) **once per process**, keyed by `(resolved SKILL.md path, warning text)`, and the message carries that path so the offending file is locatable. Registries reload on every project run/reload, so without this guard a repaired/mismatched skill floods the log; a server restart starts a fresh process and logs the current warnings once again. Only the log is deduplicated — the diagnostics returned to callers (`warnings_for`, `diagnostics`, `skill.list`/UI) still carry every warning on every load.
- The project forbids in-app legacy compatibility. Do not add automatic migrations for older `allowed_skills` formats; use explicit converter scripts if needed.
