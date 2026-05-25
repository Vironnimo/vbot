# Skill Tool

Internal tool that activates allowed skill instructions for the current Session.

## Interfaces

- Tool name: `skill`
- Registration: `register_skill_tool(registry, skill_registry)`
- Schema: required `name`; `additionalProperties: false`.
- Display: summary field `name`.
- `ToolContext.activate_skill(name, data)` stores skill context through the Session hook when available.

## Conventions

- `skill` is internal and governed by `allowed_skills`, not normal `allowed_tools`.
- The tool result contains only minimal activation status and resource metadata.
- Full `SKILL.md` content is stored as a Session skill-context note and restored for the next provider request.

## Constraints & Gotchas

- Results must not include the raw skill body or full `<skill_content>` payload.
- Normal tool allowlists must not block this internal tool; empty `allowed_skills` should block activation.
