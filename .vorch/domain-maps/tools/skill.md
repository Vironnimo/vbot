# Skill Tool

Tool that activates allowed skill instructions for the current Session. An ordinary allow-list tool — toggleable per agent via `allowed_tools` like any tool, and seeded default-on in the Project Tool Whitelist (`PROJECT_DEFAULT_ALLOWED_TOOLS`).

## Interfaces

- Tool name: `skill`
- Registration: `register_skill_tool(registry, skill_registry)`
- Schema: required `name`; `additionalProperties: false`.
- Display: summary field `name`.
- `ToolContext.activate_skill(name, data)` stores skill context through the Session hook when available.

## Conventions

- `skill` is governed by both lists: `allowed_tools` decides whether the tool is offered at all (the normal per-agent toggle), and `allowed_skills` decides which skills it may activate.
- The tool result contains only minimal activation status and resource metadata.
- Full `SKILL.md` content is stored as a Session skill-context note and restored for the next provider request.

## Constraints & Gotchas

- Results must not include the raw skill body or full `<skill_content>` payload.
- When `skill` is not in the agent's `allowed_tools`, the tool is not offered; when it is, an empty `allowed_skills` still blocks activation (nothing to load).
