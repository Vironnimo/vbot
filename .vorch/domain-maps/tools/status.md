# Status Tool

Reports current or targeted agent/session/runtime status through the same status builder used by chat commands.

## Interfaces

- Tool name: `status`
- Registration: `register_status_tool(registry, agent_resolver, sessions, models, chat_runs, started_at, providers=None, projects=None)` — resolves the target agent through the run-path `AgentResolver` seam (so a project session reports the resolved config agent), and uses the optional `ProjectStore` to label the session's project.
- Description and schema expose three closed flat targeting variants: no arguments, required `session_id`, or required `session_id` plus `agent_id`. The Provider schema therefore rejects `agent_id` without `session_id` before dispatch; the handler retains the same dependency check as defense in depth.
- Targeting rules:
  - No arguments checks the calling Agent's current Tool Context Session.
  - `session_id` checks that Session for the calling Agent.
  - `agent_id` plus `session_id` checks that exact Agent/Session pair.
  - Retired nested `request.operation`, operation-key, and `action` shapes are rejected.
- Success data contains status text built from Agent, Session, project, model, runtime, run activity state, context usage, and cache usage, plus machine-readable `agent_id`, `session_id`, `activity`, `run_id`, `created_at`, and `updated_at`. Cache details are intentionally text-only in the existing `text` field: the tool does not add machine-readable cache fields.
- The status text carries a `Project:` line: `<display name> (<id>)` for a project session, the placeholder for an identity session (and the bare id when the project can't be loaded). Resolved by the shared `resolve_status_project_label(projects, project_id)` helper, so the `/status` command and the tool agree.
- The status text carries `Last request cache:` and `Session cache:` lines. They render provider-reported cache read/write tokens and hit rate only when cache fields are present on measured assistant usage; otherwise they render the placeholder, so providers without cache reporting do not look like a 0% hit.
- `activity` is only `running` or `idle`; unknown/missing Agent or Session targets return failure envelopes.
- Display: no summary. A status call must render as `status`, not `status ({})`.

## Constraints & Gotchas

- The `/status` command and status tool share the same status text builder. `/status` always reports the current Session; the tool may target another Session.
- `created_at` and `updated_at` are active Run timestamps when `activity` is `running`; they are `null` in structured data and rendered as placeholders in text when the Session is idle.
- Expected target lookup problems are represented as tool failure envelopes (`agent_not_found`, `session_not_found`, or `invalid_arguments`) instead of an `unknown` status.
