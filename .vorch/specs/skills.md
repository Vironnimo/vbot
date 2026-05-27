# Skills

Local skill metadata loading, validation diagnostics, and prompt allowlist filtering.

## Overview

`core/skills/` scans bundled skills under `resources/skills/`, user skills under `<data_dir>/skills/`, and configured extra skill directories. A directory is considered a skill only when it contains `SKILL.md`.

Skills are playbooks, not normal user-managed tools. The registry exposes prompt metadata and internal activation metadata; actual activation is handled by the chat/tool pipeline. Agents can activate skills through the internal `skill` tool, while user messages can activate skills deterministically through `/skill-name` at the start of the message or `$skill-name` anywhere in the message before the provider request is sent.

## Data Model

- `SkillMetadata`: `name`, `description`, internal `path`, optional `license`, `compatibility`, `metadata`, and `allowed_tools` parsed from YAML frontmatter.
- `SkillDiagnostic`: `name`, `path`, `valid`, `warnings`, and `loadable` for both loadable skills with warnings and rejected skill directories.
- YAML frontmatter is parsed with PyYAML. Validation is lenient: name/directory mismatch and names longer than 64 characters are warnings; missing required fields or invalid YAML make the skill non-loadable.
- Resource paths are not stored in `SkillMetadata`; `scripts/` and `references/` are scanned at activation time.
- Bundled `resources/skills/` contains built-in operational skills plus tiny sample skills, including warning and broken examples, so UI diagnostics can be inspected manually.
- Activated skill content is wrapped in `<skill_content name="...">` and persisted as an internal chat note so it remains available across later turns in the same Session without appearing as a normal visible chat message.

## Prompt Catalog

Prompt-facing skill metadata is XML and follows the vBot agentskills.io-compatible catalog shape:

```xml
<available_skills>
  <skill>
    <name>poem-writer</name>
    <description>Write a short, polished poem for a requested theme.</description>
  </skill>
</available_skills>
```

- `available_skills` is the root element.
- Each `skill` element contains only `name` and `description`.
- Do not expose `path`, `location`, or other local filesystem details in the prompt catalog.
- Skill values inserted into the XML block must be XML-escaped.
- The bundled skills prompt must explain that `/skill-name` and `$skill-name` user tokens are activation hints once matching `<skill_content>` has been injected, so the model follows the loaded skill instructions without echoing the marker as requested output.

## Interfaces

- `core/skills/__init__.py` exports `SkillMetadata`, `SkillRegistry`, and allowlist/frontmatter constants.
- `SkillRegistry.load(skills_dir, extra_dirs=None) -> SkillRegistry` — missing roots mean an empty contribution.
- `get(name) -> SkillMetadata`
- `list_all() -> list[SkillMetadata]`
- `filter_allowed(allowed_skills) -> list[SkillMetadata]`
- `diagnostics() -> list[SkillDiagnostic]`
- `invalid_diagnostics() -> list[SkillDiagnostic]`
- `warnings_for(name) -> list[str]`
- Activation helper behavior: read the skill body after YAML frontmatter, scan `scripts/` and `references/`, and build the full `<skill_content>` payload for session storage. The internal `skill` tool result returned to the model is only a minimal status envelope; it must not repeat the full skill body.

## Conventions

- `allowed_skills=['*']` exposes all loaded skills.
- `allowed_skills=[]` exposes none.
- Explicit allowlists match exact skill names.
- Unknown allowlist entries are ignored because skills are not hard execution gates.
- Duplicate skill names are resolved by first-found-wins scan order and recorded as diagnostics for rejected duplicates.
- The internal `skill` tool is included in provider tool definitions when an agent has at least one loadable allowed skill. It is not controlled by `allowed_tools` and must stay out of normal tool lists and Agent tool toggles.
- Full skill instructions have a single provider-visible source: the session-scoped injected `<skill_content>` note. Tool-call results for the internal `skill` tool must not include `content`, raw skill Markdown, or a `<skill_content>` block, otherwise the model sees duplicate instructions.
- `/skill-name` and `$skill-name` triggers preserve the original user message. `allowed_skills=[]` exposes no skills; only a missing/`None` allowlist falls back to wildcard behavior in compatibility test stubs. Unknown or non-loadable triggers become internal system reminders.
- `$skill-name` is a skill-only mention convention. Surfaces that provide `$` autocomplete must list only loadable skills and must not include built-in slash commands. Slash autocomplete may list both built-in commands and skills because `/` is the shared user-entry affordance; backend command dispatch still handles only recognized built-in commands before the normal skill-trigger path.

## External Dependencies

- `pyyaml` is a direct core dependency for `SKILL.md` YAML frontmatter parsing.

## Constraints & Gotchas

- Local paths remain internal. The prompt catalog and user-facing skill list must not require agents to read files directly.
- Non-loadable skill directories should be retained as diagnostics so the UI can explain invalid YAML, missing descriptions, or duplicate names.
- The project forbids in-app legacy compatibility. Do not add automatic migrations for older `allowed_skills` formats; use explicit converter scripts if needed.
