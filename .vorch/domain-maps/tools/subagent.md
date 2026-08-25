# Sub-Agent Tool

Registers the single public `subagent` Tool and delegates lifecycle orchestration to `core/subagents/`.

## Data Model

- `core.tools.subagent` owns the Tool name, description, flat JSON Schema, display metadata, registration, and Tool-owned System Prompt block.
- `SubAgentCoordinator` in `core/subagents/` owns admission, queueing, status, cancellation, batch tracking, automatic delivery, and Agent-facing result shaping.

## Interfaces

- Tool name: `subagent`.
- Schema: one open flat object requiring `action: "run" | "status" | "cancel"`, with optional siblings `content`, `description`, `agent_id`, `session_id`, `model`, `thinking_effort`, and `id`. `description` is a strongly preferred 3-5 word task title with no model-facing length constraint. The handler requires `content` for `run`, requires `agent_id` when continuing `session_id`, requires `id` for `status` and `cancel`, and rejects unknown or action-inapplicable fields. The model-facing schema emits no branch or conditional keywords and no `additionalProperties`. Legacy `request`, `operation`, `background`, `run_id`, and `queue_item_id` fields are not public.
- Result identity: every admitted `run` returns one stable `id`. Agent-facing results never expose the internal Run or Queue handle, including queued, running, terminal, and cancelled results.
- Delivery: a depth-0 caller receives `delivery: "automatic"` and an immediate queued/running descriptor; a nested caller receives `delivery: "inline"` only after completion. The caller cannot override this policy. At depth 0, every result ready when the current Parent Run ends is combined with other Bash/Sub-Agent completions in one automatic follow-up Run; unfinished work is delivered later.
- Display: `run` summaries prefer the short `description`, fall back to the `content` preview, and then show the optional target Agent; `status`/`cancel` summaries show the action and public id. `content` remains hidden from expanded argument details.
- Registration: `register_subagent_tools(registry, coordinator, prompt_blocks=None)` registers exactly one Tool; when the Tool prompt registry is supplied, registration also contributes the dynamic `tool:subagent` block.

## Conventions

- `tools.subagent.allowed_agents` is optional Tool-owned configuration at the root of an Identity Agent's `agent.json`, not an Agent-wide required field. It contains additional targets only: a missing block or field defaults to `['*']`, `[]` is self-only, and explicit entries use bare Identity ids or qualified `agent@project` ids. Omitting `agent_id` on a new `run` always selects the calling Agent, which is never repeated in the list. A Project Agent remains bounded to its own current Team.
- Authorization applies to `run` immediately after canonical address parsing and before target lookup, Session work, quota reservation, or queueing. `status` and `cancel` recover their already-authorized target from an id owned by the same Parent Agent Session and Project. Provider-schema narrowing and Tool visibility are guidance; `SubAgentCoordinator` remains the security boundary.
- `run` without `session_id` creates a new persisted Session whose automatic title is the whitespace-normalized `description`, or the normalized beginning of `content` when `description` is blank/omitted, capped at 48 characters. Continuing an existing Session requires both its exact `session_id` and owning `agent_id` and never retitles that Session; manual titles therefore remain stable.
- `content` is a self-contained delegation brief when `run` creates a new Session: it carries the goal, relevant context, scope, constraints, and expected result. A continuation with `session_id` may rely on that Sub-Agent Session's existing history and should carry the follow-up instruction plus any new context.
- `model` and `thinking_effort` are optional Run-local overrides for only the newly admitted Child Run. Missing fields inherit the freshly resolved target Agent; `thinking_effort: ""` selects the Provider default and `"none"` disables Reasoning. Overrides never mutate or become defaults for the target Agent, Project, or Session and do not flow into later continuations or nested calls.
- Busy target Sessions enqueue a follow-up Run through `ChatRunManager` without changing the public id.
- Successful `run` results carry `activity_file: string | null`; when allocated they also carry an `activity_note` with its concrete path. Status snapshots keep `activity_file` but do not repeat that contextual note.
- `status` is non-blocking: queued/running work returns immediately, while terminal output is accepted only from the exact Run or a matching terminal Run Summary. Intermediate Assistant output without that Summary is not completion.

## Constraints & Gotchas

- The caller cannot target its own active Session.
- Depth and per-turn limits are enforced from runtime settings.
- Cancelling the calling Run cascades only to nested foreground children; top-level background children survive. `action: "cancel"` is the separate Agent-controlled path and remains usable from a later Run in the same Parent Session while the process-local tracker owns the id.
- Completed entries are pruned after inline fetch or durable automatic delivery, so Agents should not poll top-level work merely to wait. A terminal `status` result durably persisted first withdraws its pending automatic notice. Each completion section names the public id and includes the complete final output, status, and activity path.
- The `tool:subagent` System Prompt block lists additional allowed targets and renders context-specific execution guidance from `nesting_depth`: top-level callers are told that vBot monitors the work, that they may continue independent work or finish the current Run, that dependent work may request `status`, and that only results ready at the Run boundary are combined; nested callers are told that the Tool waits inline.
