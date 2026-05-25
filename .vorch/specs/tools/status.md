# Status Tool

Reports current agent, session, and runtime status through the same status builder used by chat commands.

## Interfaces

- Tool name: `status`
- Registration: `register_status_tool(registry, agents, sessions, models, started_at)`
- Schema: empty object; `additionalProperties: false`.
- Success data contains status text built from Agent, Session, model, and runtime state.
- Display: no summary. A status call must render as `status`, not `status ({})`.

## Constraints & Gotchas

- The handler ignores arguments because the schema has no properties.
- Expected lookup problems are represented in the status text/result rather than requiring UI-specific handling.
