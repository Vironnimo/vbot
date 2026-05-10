# Skills

Local skill metadata loading, validation diagnostics, and prompt allowlist filtering.

## Overview

`core/skills/` scans bundled skills under `resources/skills/`, user skills under `<data_dir>/skills/`, and configured extra skill directories. A directory is considered a skill only when it contains `SKILL.md`.

Skills are playbooks, not normal user-managed tools. The registry exposes prompt metadata and internal activation metadata; actual activation is handled by the chat/tool pipeline.

## Data Model

- `SkillMetadata`: `name`, `description`, internal `path`, optional `license`, `compatibility`, `metadata`, and `allowed_tools` parsed from YAML frontmatter.
- `SkillDiagnostic`: `name`, `path`, `valid`, `warnings`, and `loadable` for both loadable skills with warnings and rejected skill directories.
- YAML frontmatter is parsed with PyYAML. Validation is lenient: name/directory mismatch and names longer than 64 characters are warnings; missing required fields or invalid YAML make the skill non-loadable.
- Resource paths are not stored in `SkillMetadata`; `scripts/` and `references/` are scanned at activation time.
- Bundled `resources/skills/` contains tiny sample skills, including warning and broken examples, so UI diagnostics can be inspected manually.

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

## Interfaces

- `core/skills/__init__.py` exports `SkillMetadata`, `SkillRegistry`, and allowlist/frontmatter constants.
- `SkillRegistry.load(skills_dir, extra_dirs=None) -> SkillRegistry` — missing roots mean an empty contribution.
- `get(name) -> SkillMetadata`
- `list_all() -> list[SkillMetadata]`
- `filter_allowed(allowed_skills) -> list[SkillMetadata]`
- `diagnostics() -> list[SkillDiagnostic]`
- `invalid_diagnostics() -> list[SkillDiagnostic]`
- `warnings_for(name) -> list[str]`

## Conventions

- `allowed_skills=['*']` exposes all loaded skills.
- `allowed_skills=[]` exposes none.
- Explicit allowlists match exact skill names.
- Unknown allowlist entries are ignored because skills are not hard execution gates.
- Duplicate skill names are resolved by first-found-wins scan order and recorded as diagnostics for rejected duplicates.

## External Dependencies

- `pyyaml` is a direct core dependency for `SKILL.md` YAML frontmatter parsing.

## Constraints & Gotchas

- Local paths remain internal. The prompt catalog and user-facing skill list must not require agents to read files directly.
- Non-loadable skill directories should be retained as diagnostics so the UI can explain invalid YAML, missing descriptions, or duplicate names.
- The project forbids in-app legacy compatibility. Do not add automatic migrations for older `allowed_skills` formats; use explicit converter scripts if needed.
