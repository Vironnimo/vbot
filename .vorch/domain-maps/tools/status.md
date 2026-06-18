# Status Tool

Reports current or targeted agent/session/runtime status through the same status builder used by chat commands.

## Interfaces

- Tool name: `status`
- Registration: `register_status_tool(registry, agent_resolver, sessions, models, chat_runs, started_at, providers=None, projects=None)` — resolves the target agent through the run-path `AgentResolver` seam (so a project session reports the resolved config agent), and uses the optional `ProjectStore` to label the session's project.
- Description tells agents that no arguments check the current Session, `session_id` checks another Session for the current Agent, and `session_id` plus `agent_id` checks another Agent's Session.
- Schema properties are ordered `session_id`, then `agent_id`; both are optional strings and `additionalProperties: false`.
- Targeting rules:
  - no arguments checks the calling Agent's current tool context Session.
  - `session_id` checks that Session for the calling Agent.
  - `agent_id` plus `session_id` checks that exact Agent/Session pair.
  - `agent_id` without `session_id` returns `invalid_arguments`.
- Success data contains status text built from Agent, Session, project, model, runtime, and run activity state, plus machine-readable `agent_id`, `session_id`, `activity`, `run_id`, `created_at`, and `updated_at`.
- The status text carries a `Project:` line: `<display name> (<id>)` for a project session, the placeholder for an identity session (and the bare id when the project can't be loaded). Resolved by the shared `resolve_status_project_label(projects, project_id)` helper, so the `/status` command and the tool agree.
- `activity` is only `running` or `idle`; unknown/missing Agent or Session targets return failure envelopes.
- Display: no summary. A status call must render as `status`, not `status ({})`.

## Constraints & Gotchas

- The `/status` command and status tool share the same status text builder. `/status` always reports the current Session; the tool may target another Session.
- `created_at` and `updated_at` are active Run timestamps when `activity` is `running`; they are `null` in structured data and rendered as placeholders in text when the Session is idle.
- Expected target lookup problems are represented as tool failure envelopes (`agent_not_found`, `session_not_found`, or `invalid_arguments`) instead of an `unknown` status.
