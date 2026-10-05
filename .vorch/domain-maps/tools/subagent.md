# Sub-Agent Tool

Registers the public `subagent` Tool and the Sub-Agent-only `message_parent` Tool and delegates their behavior to `core/subagents/` (`subagents.md`).

## Data Model

- `core.tools.subagent` owns both Tools' names, descriptions, flat JSON Schemas, display metadata, registration, and the Tool-owned `tool:subagent` System Prompt block. `core/tools/_subagent_arguments.py` owns call-syntax normalization (the `argument_normalizer`); `core/tools/call_syntax.py` holds the shared spelling and placeholder helpers (also used by `status` and `project`).
- `SubAgentCoordinator` in `core/subagents/` owns everything after normalization: interpretation against the caller's tree, authorization, Sessions, Runs, forwarding and every result and refusal text (`core/subagents/_constants.py`).

## Interfaces

- `subagent` schema: one open flat object with optional `action: "run" | "send" | "list" | "cancel"` and siblings `content`, `description`, `agent_id`, `id`, `model`, `thinking_effort`; `required: []`, no `minLength`, no branch keywords, no `additionalProperties`. Omitted action means `send` when the call carries an `id` and `content`, a refusal naming the `send` and `cancel` calls for an `id` alone, and `run` otherwise. `run` needs `content` and `description`; `send` needs `id` and `content`; `cancel` needs `id`; `list` takes an optional `id`. Actions ignore fields they do not use. `background` and `session_id` are accepted but unadvertised (`UNADVERTISED_PARAMETERS`); unknown fields fail at dispatch.
- `message_parent` schema: `{content}` (required). Registered with `catalog_visible=False`, `session_scoped=True` and `activation=TOOL_ACTIVATION_SESSION_GRANT`: Chat grants it only in Sessions linked to a Parent Agent (`SUBAGENT_SESSION_TOOL_NAMES`, `subagents.md` -> Conventions), so no other Session ever sees its definition. Both Tools need `open_input_schema=True` and `execution_slot_required=False`.
- Call syntax (`_subagent_arguments.py`, before schema validation): besides field and enum formatting, `operation` as action and `request`/`arguments`/action-key wrappers, the owner reads other harnesses' shapes, since Chat maps their Tool names (`Task`, `delegate_task`, `spawn_agent`, ...) to `subagent`: Claude Code/opencode `Task` (`description`, `prompt`, `subagent_type`, `run_in_background`), Hermes `delegate_task` (`goal`, `context` appended to the task as a `Context:` section), OpenClaw/pi (`task`, `label`, `agentId`, `agent`, `thinking`), and vBot's earlier `request` string and `blocking`/`non_blocking` flags. Action synonyms: `run` <- spawn, delegate, start, create, launch, ...; `send` <- message, steer, tell, continue, resume, followup, reply, answer, instruct; `list` <- status, ls, check, ..., wait; `cancel` <- stop, kill, abort, terminate, ... Placeholder values (`null`, blank or punctuation-only text, `unused`, `invalid-placeholder`, `__omit__`; plus `new` for `session_id` and `default`/`inherit` for `model`/`thinking_effort`) count as omitted. Conflicting spellings or delivery flags refuse. Non-empty `toolsets` and `tasks` refuse with the corrected call (a Sub-Agent always uses its Agent's own Tools; one task per call, sibling calls for several). `run_id`, `queue_item_id` and other unknown fields stay invalid. Coverage: `tests/core/tools/test_subagent.py` (dialects, placeholders, refusal before side effects, through real dispatch).
- Display: the row builder normalizes the persisted raw arguments first (falling back to them when normalization refuses), so dialect calls label what they meant. `run` rows show `description`, else the `content` preview, then the target Agent; other rows show the action and the public id. `content` stays hidden from expanded argument details. Detail blocks (`_subagent_detail_blocks`, `tools.md` -> Display metadata): the brief as `task` (run) or the message as `content` (send); a notice for `send` and `cancel` outcomes (steered, started, queued, stopped); a `list` shows a `results` block (title, then agent id, state and last Tool) or `No sub-agents.`. A Sub-Agent's answers are not shown on the call row: they appear in its Session and as a delivery in the Parent's conversation. `message_parent` rows show the message as `content`. Ids, notes and activity paths stay in the raw result. Coverage: `test_subagent.py::test_details_show_the_task_the_message_and_the_listed_subagents`.
- Registration: `register_subagent_tools(registry, coordinator, prompt_blocks=None)` registers both Tools; with the Tool prompt registry it also contributes the dynamic `tool:subagent` block.

## Conventions

- `tools.subagent.allowed_agents` is optional Tool-owned configuration at the root of an Identity Agent's `agent.json`, not an Agent-wide required field. It holds additional targets only: a missing block or field defaults to `['*']`, `[]` is self-only, and explicit entries use bare Identity ids or qualified `agent@project` ids. Omitting `agent_id` on a `run` always selects the calling Agent, which is never repeated in the list. Renaming an Identity Agent moves its bare id in every list, and deleting one removes it (`agent.md`). Authorization and target lookup rules: `subagents.md` -> Conventions -> Targets.
- `content` for `run` is a self-contained brief (goal, context, scope, constraints, expected result); for `send` it is the follow-up message, which can rely on the Sub-Agent's history.
- Results identify a Sub-Agent by `id`, `agent_id` (exactly the value `agent_id` accepts: `agent@project` when the Sub-Agent has a Project), `session_id` and `project_id`; they never expose internal Run or Queue handles.
- The Tool definition says what the Tool does and what each field takes; the `tool:subagent` block carries the target list and cross-turn strategy without repeating the definition: the allowed `agent_id` choices (or that only a copy of yourself is available), bounded independent work, sibling calls, non-overlapping file ownership, responsibility for integration, and `EXECUTION_GUIDANCE` (background start, automatic delivery of every answer, the status-report and facts-line semantics, no polling, `send` for corrections). Chat pins the block per prompt epoch (`chat/request-building.md` -> Dynamic prompt blocks per prompt epoch): an added, changed or removed target reaches the Model at the next Run's start as a System Reminder listing those targets (catalog title `The Agent ids under Sub-Agents`, one entry per `agent_id`); a change to the frame (`EXECUTION_GUIDANCE`) sends the block's whole current text. The block is the same at every depth.

## Constraints & Gotchas

- Provider-schema narrowing and Tool visibility are guidance; `SubAgentCoordinator` remains the security boundary.
- `message_parent` must stay out of the catalog: it only works in a linked Session and refuses elsewhere, so offering it generally would invite failing calls.

## Verification

`scripts/probe_provider_tool_call.py --scenario tool_first_use --first-use-tool subagent`
supplies natural delegation, parallel review, list, cancellation, follow-up and
unavailable-target tasks with competing Tools and production definitions/prompts.
Real Sessions, Runs, queueing and dispatch receive the requests; only the child's
Model work is a deterministic receiver. All attempts, calls, results and final claims
are retained with `--first-use-report`.

`python -m scripts.tool_lab probe scripts/tool_lab/cases/delegation.json --visible`
replays anonymized call shapes (dialects, placeholders, echoed fields, conflicts) for
`subagent`, `status` and `project` through production dispatch and shows what the
Model reads back.

## Agent-facing text

| Text | Reason |
|---|---|
| `subagent`: `Delegate tasks to Sub-Agents and manage them: run starts ..., send ..., list ..., cancel ...` | One Tool covers one resource (the caller's Sub-Agents); naming the four actions up front lets the Agent pick `send` for follow-ups instead of a new `run`. |
| `subagent`: `A Sub-Agent is a copy of yourself or an Agent listed under Sub-Agents in the System Prompt; it works in its own Session, and vBot delivers its answers to you.` | Agents invented Agent ids and waited or polled for results. |
| `action`: `run (default) starts a new Sub-Agent with content; send ...; list ...; cancel stops one.` | States the default and what each value needs. |
| `content`: `For run, the task: goal, relevant context, scope, constraints and expected result, self-contained. For send, the message.` | Briefs without context produced Sub-Agents that guessed; a `send` can rely on history. |
| `description`: `For run, a 3-5 word title that the user sees ... Required for run.` | The title names the Sub-Agent's Session and its forwarded answers; without it titles were truncated briefs. |
| `id`: `Sub-Agent id from a run result or from list. Required for send and cancel.` | Agents constructed ids or passed Session ids. |
| `model`: `Model for the Sub-Agent, as <provider>/<model-id>. Omit to use the Agent's model.` | Weak Models filled the field on every call and used bare Model names; omission is the normal case. |
| `thinking_effort`: `Thinking effort for the Sub-Agent. Omit to use the Agent's setting.` | Same as for `model`. |
| `message_parent`: `Send a message to your Parent Agent ... without ending your turn.` | A Sub-Agent's only way to reach its Parent before its final answer. |
| `message_parent`: `Use it for questions only your Parent Agent can decide and to answer its messages. Results and status reports belong in your final answer ...` | Prevents duplicate delivery: every final answer already reaches the Parent. |
| Block: `Delegate bounded work ... sibling calls ... non-overlapping ownership ... integrating and verifying` | Strategy across turns and Tools that no single definition can carry. |
| Block: `Each run starts a Sub-Agent in the background and returns its id at once. ...` | Agents waited, polled `list`, or treated a status report as the final result; corrections went to new Sub-Agents instead of `send`. |
