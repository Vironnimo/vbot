# Sub-Agent Tools

Starts or resumes sub-agent Runs and fetches sub-agent results.

## Data Model

- `SubAgentBatchTracker` tracks in-memory batches by parent `(agent_id, session_id, run_id)`.
- Batch tracking is process-local and does not persist across restarts.
- The tracker reserves a per-turn slot before async spawn work begins, so
  sibling tool calls cannot bypass `max_subagents_per_turn` while they are
  waiting on session or queue state.

## Interfaces

- Tool name: `subagent`
- Schema: required `content`; optional `agent_id`, `blocking`, and `session_id`.
- Display: summary fields `agent_id` and `content`; hides `content` from argument details.
- Tool name: `subagent_result`
- Schema: required `session_id`; optional `agent_id` and `run_id`.
- Display: summary fields `agent_id` and `session_id`.
- Registration: `register_subagent_tools(registry, runtime, trigger_service, batch_tracker)`

## Conventions

- With `session_id`, `subagent` routes into an existing Session; otherwise it creates a new persisted Session for the target Agent.
- Busy target Sessions enqueue a follow-up Run through `ChatRunManager`.
- Blocking mode waits for completion and returns the result payload.
- Non-blocking mode returns a running descriptor when a Run has started. If the
  target Session is still busy and the child Run is only queued, it returns a
  queued descriptor containing `agent_id`, `session_id`, `queue_item_id`, and
  `status: "queued"` instead of waiting for the child Run to start.
- `subagent_result` checks live Run result first, then falls back to the last non-empty assistant message in the target Session.
- `subagent_result` returns a queued descriptor while the tracked child Run is
  still queued and has no `run_id` yet.

## Constraints & Gotchas

- The caller cannot target its own active Session.
- Depth and per-turn limits are enforced from runtime settings.
- Parent cancellation removes queued child Runs when possible and cancels
  already-started child Runs.
- Completed entries that were fetched are pruned from the in-memory tracker.
- When all unfetched sub-agent Runs in a batch finish, the tracker sends one internal automation trigger to continue the parent Agent via a system-reminder note.
