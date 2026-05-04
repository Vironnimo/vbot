# Skills

Local skill metadata loading and prompt allowlist filtering.

## Overview

`core/skills/` scans `<data_dir>/skills/<skill-id>/SKILL.md` files and exposes prompt metadata. Skills are playbooks, not tools: Phase 2 only filters which skills appear in the prompt. Unknown allowlist entries are ignored because skills are not hard execution gates.

## Data Model

- `SkillMetadata`: `name`, `description`, `path`.
- Metadata is read from Markdown front matter in each `SKILL.md`.

## Interfaces

- `core/skills/__init__.py` exports `SkillMetadata`, `SkillRegistry`, and allowlist/front-matter constants.
- `SkillRegistry.load(skills_dir) -> SkillRegistry` — missing root means empty registry.
- `get(name) -> SkillMetadata`
- `list_all() -> list[SkillMetadata]`
- `filter_allowed(allowed_skills) -> list[SkillMetadata]`

## Conventions

- `allowed_skills=['*']` exposes all loaded skills.
- `allowed_skills=[]` exposes none.
- Explicit allowlists match exact skill names.

## Constraints & Gotchas

- A directory is considered a skill only when it contains `SKILL.md`.
- Duplicate skill names are rejected.
- The registry currently reads simple front matter fields only; do not depend on full YAML semantics.
