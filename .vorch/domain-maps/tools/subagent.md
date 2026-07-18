# Sub-Agent Tools

Registers the public sub-agent tools and delegates orchestration to `core/subagents/`.

## Data Model

- `core.tools.subagent` owns tool names, descriptions, JSON Schemas, display metadata, and registration.
- `SubAgentCoordinator` in `core/subagents/` owns queueing, cancellation, batch tracking, and result lookup.

## Interfaces

- Tool name: `subagent`
- Schema: required `content`; optional `agent_id`, `background`, and `session_id`.
- Display: summary fields `agent_id` and `content`; hides `content` from argument details.
- Tool name: `subagent_result`
- Schema: required `session_id`; optional `agent_id` and `run_id`.
- Display: summary fields `agent_id` and `session_id`.
- Registration: `register_subagent_tools(registry, coordinator)`

## Conventions

- `allowed_agents` is independent of `allowed_tools` and authorizes both `subagent` and `subagent_result` immediately after canonical address parsing, before target lookup, Session work, quota reservation, or queueing. Identity-Agent `['*']` reaches every Identity Agent and every Project Agent across registered Projects; explicit entries use bare Identity ids or qualified `agent@project` addresses. A Project Agent is always bounded to its own current Team, including when its repository policy is a wildcard.
- When effective `allowed_agents` is empty, both provider Tool definitions are omitted and dispatch rejects both Tools. When the set is explicit, the provider schema requires `agent_id` and narrows it to that target enum; wildcard access retains the optional free-address schema.
- With `session_id`, `subagent` routes into an existing Session; otherwise it creates a new persisted Session for the target Agent.
- Busy target Sessions enqueue a follow-up Run through `ChatRunManager`.
- Foreground mode waits for completion and returns the result payload; spawn/result payloads carry `activity_file: string | null` for the matching Run. Every successful `subagent` result whose file was allocated also carries `activity_note` with the concrete path and the instruction to read it if the Sub-Agent's status or progress becomes relevant.
- Background mode returns a running descriptor when a Run has started. If the target Session is still busy and the child Run is only queued, it returns a queued descriptor containing `agent_id`, `session_id`, `queue_item_id`, `status: "queued"`, and the already-created `activity_file` instead of waiting for the child Run to start.
- `subagent_result` checks live Run result first, then falls back to the last non-empty assistant message in the target Session.
- `subagent_result` returns a queued descriptor while the tracked child Run is still queued and has no `run_id` yet. The activity path points to a live temporary Markdown projection of visible Assistant text and concise Tool activity, not the canonical child Session or full Tool output. This lookup result keeps the structured `activity_file` field but does not repeat the spawn-only `activity_note`.

## Constraints & Gotchas

- The caller cannot target its own active Session.
- Authorization is repeated inside `SubAgentCoordinator` for both operations; provider-schema narrowing and Tool availability are visibility and guidance, not the security boundary.
- Depth and per-turn limits are enforced from runtime settings.
- Parent cancellation removes queued child Runs when possible and cancels already-started child Runs.
- Completed entries that were fetched are pruned from the in-memory tracker.
- When all unfetched sub-agent Runs in a batch finish, the tracker sends one internal automation trigger to continue the parent Agent via a system-reminder note. The note includes each sub-agent's complete final output (untruncated), run status, and activity-file path when available, so no follow-up `subagent_result` call is needed to read batch results.
- Tool descriptions tell callers to end their turn after a background spawn and wait for the automatic completion note, but deliberately contain no temporary-file contract. Contextual activity-file guidance belongs to the concrete successful spawn result, while `subagent_result` remains the explicit status/result lookup and does not repeat that instruction.
