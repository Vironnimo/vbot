# Skills

Local skill metadata loading, validation diagnostics, and prompt allowlist filtering.

## Overview

`core/skills/` scans bundled skills under `resources/skills/`, user skills under `<data_dir>/skills/`, and configured extra skill directories. A directory is considered a skill only when it contains `SKILL.md`.

**Project-scoped pool.** A project run uses a *merged* registry: the project's own skills under `<cwd>/.opencode/skills/` scanned **first** (so a project skill wins a name collision with a bundled one), then the same bundled scan roots the global registry uses. The single seam is `runtime.skills_for(project_id)` — `None` returns the global registry (identity runs, byte-identical), a set `project_id` returns the cached per-project merge. The runtime caches these per `project_id` (like the resolver's Team cache) and drops them on the same triggers (project open, cwd change, project removal, global `reload_skills`) — `project.show` re-scans the Team on every call, so it drops both caches first to keep the skill pool in step with the freshly scanned Team (runs never call `project.show`, so per-run caching is unaffected). All run-time skill consumers — prompt assembly, `/`–`$` triggers, the internal `skill` tool, and autocomplete — resolve through this one seam, so project skills never leak between projects or to the home agent.

Skills are playbooks, not normal user-managed tools. The registry exposes prompt metadata and internal activation metadata; actual activation is handled by the chat/tool pipeline. Agents can activate skills through the internal `skill` tool, while user messages can activate skills deterministically through `/skill-name` at the start of the message or `$skill-name` anywhere in the message before the provider request is sent.

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
  <skill>
    <name>teach</name>
    <description>Teach the user a topic so they actually understand it.</description>
  </skill>
</available_skills>
```

- `available_skills` is the root element.
- Each `skill` element contains only `name` and `description`.
- Do not expose `path`, `location`, or other local filesystem details in the prompt catalog.
- The prompt catalog includes only skills allowed for the agent (`agent.allowed_skills`) and currently `available`, filtered against the run's project-scoped registry (`build_system_prompt`/`provider_tool_definitions` take an optional `skill_registry`; the chat loop passes `runtime.skills_for(project_id)`). Command autocomplete (`chat.commands` RPC) **is** agent-scoped: given an optional `agent_id` (a bare id or an `agent@projekt` address) it resolves the agent and returns its effective skills via `skills_for(project_id).filter_allowed(agent.allowed_skills)`; with no address it returns the global list (`filter_allowed(['*'])`, today's behavior). The WebUI passes the active agent's address. `skill.list` still returns unavailable skills with requirement details (availability state plus missing/optional-missing) so the user can fix local prerequisites.
- Skill values inserted into the XML block must be XML-escaped.
- The bundled skills prompt must explain that `/skill-name` and `$skill-name` user tokens are activation hints once matching `<skill_content>` has been injected, so the model follows the loaded skill instructions without echoing the marker as requested output.

## Interfaces

- `core/skills/__init__.py` exports `SkillMetadata`, `SkillRegistry`, `SkillAvailability`, `SkillRequirements`, and the allowlist/frontmatter constants (`WILDCARD_ALLOWLIST`, `FRONT_MATTER_DELIMITER`).
- `SkillRegistry.load(skills_dir, extra_dirs=None, environment=None) -> SkillRegistry` — missing roots mean an empty contribution. `environment` snapshots the env used for requirement checks (see Constraints & Gotchas); when omitted it defaults to `os.environ`.
- `load_project_skill_registry(project_cwd, bundled_scan_roots, environment=None) -> SkillRegistry` — the project-first merge (project's own `.opencode/skills/` then the bundled roots). `scan_project_skill_names(project_cwd, environment=None) -> frozenset[str]` — only the project's own skill names (the set the resolver subtracts `skills_project_disabled` from). `project_skills_dir(cwd)` returns `<cwd>/.opencode/skills`. The runtime owns the per-project cache and exposes `skills_for(project_id)` / `project_skill_names(project_id)` / `invalidate_project_skills(project_id=None)`.
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
- The internal `skill` tool is included in provider tool definitions when an agent has at least one loadable allowed skill. It is not controlled by `allowed_tools` and must stay out of normal tool lists and Agent tool toggles.
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
- Local paths remain internal. The prompt catalog and user-facing skill list must not require agents to read files directly.
- Non-loadable skill directories should be retained as diagnostics so the UI can explain invalid YAML, missing descriptions, or duplicate names.
- Skill metadata warnings are logged (WARN, `vbot.skills`) **once per process**, keyed by `(resolved SKILL.md path, warning text)`, and the message carries that path so the offending file is locatable. Registries reload on every project run/reload, so without this guard a repaired/mismatched skill floods the log; a server restart starts a fresh process and logs the current warnings once again. Only the log is deduplicated — the diagnostics returned to callers (`warnings_for`, `diagnostics`, `skill.list`/UI) still carry every warning on every load.
- The project forbids in-app legacy compatibility. Do not add automatic migrations for older `allowed_skills` formats; use explicit converter scripts if needed.
