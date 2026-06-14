# Sub-Agent Tools

Registers the public sub-agent tools and delegates orchestration to `core/subagents/`.

## Data Model

- `core.tools.subagent` owns tool names, descriptions, JSON Schemas, display metadata, and registration.
- `SubAgentCoordinator` in `core/subagents/` owns queueing, cancellation, batch tracking, and result lookup.

## Interfaces

- Tool name: `subagent`
- Schema: required `content`; optional `agent_id`, `blocking`, and `session_id`.
- Display: summary fields `agent_id` and `content`; hides `content` from argument details.
- Tool name: `subagent_result`
- Schema: required `session_id`; optional `agent_id` and `run_id`.
- Display: summary fields `agent_id` and `session_id`.
- Registration: `register_subagent_tools(registry, coordinator)`

## Conventions

- With `session_id`, `subagent` routes into an existing Session; otherwise it creates a new persisted Session for the target Agent.
- Busy target Sessions enqueue a follow-up Run through `ChatRunManager`.
- Blocking mode waits for completion and returns the result payload.
- Non-blocking mode returns a running descriptor when a Run has started. If the target Session is still busy and the child Run is only queued, it returns a queued descriptor containing `agent_id`, `session_id`, `queue_item_id`, and `status: "queued"` instead of waiting for the child Run to start.
- `subagent_result` checks live Run result first, then falls back to the last non-empty assistant message in the target Session.
- `subagent_result` returns a queued descriptor while the tracked child Run is still queued and has no `run_id` yet.

## Constraints & Gotchas

- The caller cannot target its own active Session.
- Depth and per-turn limits are enforced from runtime settings.
- Parent cancellation removes queued child Runs when possible and cancels already-started child Runs.
- Completed entries that were fetched are pruned from the in-memory tracker.
- When all unfetched sub-agent Runs in a batch finish, the tracker sends one internal automation trigger to continue the parent Agent via a system-reminder note. The note includes each sub-agent's complete final output (untruncated) and run status, so no follow-up `subagent_result` call is needed to read batch results.
- Tool descriptions tell callers to end their turn after a non-blocking spawn and wait for the automatic completion note; `subagent_result` is reserved for explicit user-requested status checks before a batch finishes.
