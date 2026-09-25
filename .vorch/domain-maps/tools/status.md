# Status Tool

Reports current or targeted agent/session/runtime status through the same status builder used by chat commands.

## Interfaces

- Tool name: `status`
- Registration: `register_status_tool(registry, agent_resolver, sessions, models, chat_runs, started_at, providers=None, projects=None)` - resolves the target agent through the run-path `AgentResolver` seam (so a project session reports the resolved config agent), and uses the optional `ProjectStore` to label the session's project.
- The model-facing schema is one flat object with optional `session_id` and `agent_id` (no `minLength`), `required: []`, and no `additionalProperties` keyword. Descriptions explain all three targeting forms; dispatch rejects unknown arguments; the handler rejects malformed ones and requires `session_id` when selecting another Agent.
- Targeting rules:
  - No arguments checks the calling Agent's current Tool Context Session.
  - `session_id` checks that Session for the calling Agent.
  - `agent_id` plus `session_id` checks that exact Agent/Session pair.
  - Before validation (`_normalize_status_arguments`), known `current` action/wrapper forms, call-field formatting, and the `session`/`session_key` and `agent`/`agent_name` spellings are repaired. Placeholder values (blank, punctuation-only, `null`, `unused`, ...; for `session_id` also `current`, `this`, `active`, `mine`) count as omitted; other identifiers stay exact. An `id` field is read by its value: a `ses_` id becomes `session_id` (a different explicit `session_id` conflicts), a `sub_` Sub-Agent work id refuses naming `subagent {"action": "status", "id": ...}`, and anything else refuses naming the valid fields. Repeating the calling Agent without a Session uses the current Session. Unsupported actions and another Agent without a Session fail without lookup; the latter names the call with both ids.
- Success data contains the status text built from Agent, Session, project, model, runtime, run activity state, context usage, and cache usage, plus machine-readable `agent_id` and `session_id`. All report content, including run activity and cache details, is text-only: the tool does not add machine-readable activity or cache fields.
- The status text carries a `Project:` line: `<display name> (<id>)` for a project session, the placeholder for an identity session (and the bare id when the project can't be loaded). Resolved by the shared `resolve_status_project_label(projects, project_id)` helper, so the `/status` command and the tool agree.
- The status text carries `Last request cache:` and `Session cache:` lines. They render provider-reported cache read/write tokens and hit rate only when cache fields are present on assistant usage whose input was measured (an estimated output alone does not exclude a turn); otherwise they render the placeholder, so providers without cache reporting do not look like a 0% hit.
- The status text renders activity as `running` or `idle`; unknown/missing Agent or Session targets return failure envelopes.
- Display: no summary. A status call must render as `status`, not `status ({})`.
- Both surfaces consume the Sessions-owned `status_snapshot`: SQL supplies Session start, User-turn count, latest Assistant Usage, whole-Session Usage/cache aggregates, and no complete transcript. The Agent-callable Tool wraps its synchronous dependency gathering in the cancellation-safe Tool worker; `/status` uses the command worker path.

## Constraints & Gotchas

- The `/status` command and status tool share the same status text builder. `/status` always reports the current Session; the tool may target another Session.
- Active Run timestamps render in the status text; an idle Session renders placeholders.
- Expected target lookup problems are represented as tool failure envelopes (`agent_not_found`, `project_not_found`, `agent_unavailable`, `session_not_found`, or `invalid_arguments`) instead of an `unknown` status. Only a missing Agent or Project reports a not-found code; an Agent that exists but cannot run (for example, no usable Model) reports `agent_unavailable` (`retryable: false`) with the resolver's reason and tells the Agent to report it to the user. `session_not_found` suggests omitting `session_id` only when one was sent, and adds the owner hint (pass that Agent's `agent_id`) only when `agent_id` was omitted.
- `status` reports a chat Session; it does not track Sub-Agent work. Delegated work progress belongs to `subagent` with `action: "status"`, whose results already carry the child `agent_id`/`session_id` that this Tool accepts.
