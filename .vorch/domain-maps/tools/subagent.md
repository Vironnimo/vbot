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
- Registration: `register_subagent_tools(registry, coordinator, prompt_blocks=None)`; when the Tool prompt registry is supplied, registration also contributes the dynamic `tool:subagent` block.

## Conventions

- `tools.subagent.allowed_agents` is optional Tool-owned configuration at the root of an Identity Agent's `agent.json`, not an Agent-wide required field. It contains additional targets only: a missing block or field defaults to `['*']`, `[]` is self-only, and explicit entries use bare Identity ids or qualified `agent@project` ids. The calling Agent is always selected by omitting `agent_id` and is never repeated in the list. Disabling both Sub-Agent Tools through `allowed_tools` leaves this block persisted but inactive, so temporary Tool changes never erase its policy. Identity-Agent `['*']` reaches every other Identity Agent and every Project Agent across registered Projects; a rooted Identity Agent remains an Identity Agent. A Project Agent is always bounded to its own current Team, including when its repository policy is a wildcard.
- Authorization covers both `subagent` and `subagent_result` immediately after canonical address parsing, before target lookup, Session work, quota reservation, or queueing. Tool availability depends only on effective `allowed_tools`; an empty `allowed_agents` list keeps both Tools available for self-delegation. An explicit list narrows the provider `agent_id` enum to the calling Agent plus the listed additional targets without making `agent_id` required; wildcard access retains the optional free-address schema. The dynamic prompt block lists only resolvable additional Agents and gates with the effective `subagent` Tool.
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
- The `tool:subagent` System Prompt block tells callers how to delegate, parallelize, continue Sessions, and wait for automatic completion, while the native Tool and parameter descriptions remain short operation/schema descriptions. Contextual activity-file guidance belongs to the concrete successful spawn result, while `subagent_result` remains the explicit status/result lookup and does not repeat that instruction.
